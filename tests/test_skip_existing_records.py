"""``--skip-existing-records``: skip an ESID that Zenodo already holds.

The flag asks Zenodo, by the dataset's intended title, whether a record
already exists — and skips the folder if one does, moving on to the next
ESID.  It queries the API rather than reading ``upload_state.json``, so a
folder whose state file was lost or hand-deleted is still recognised; that
is precisely the folder that would otherwise create a duplicate.

Three properties matter enough to pin:

  * the check runs BEFORE the integrity gate, so a skipped folder costs one
    search instead of re-hashing every archive;
  * a skip is counted as *skipped*, not failed, and nothing is uploaded,
    tracked, or moved;
  * a search that cannot be completed FAILS the dataset rather than falling
    through to an upload — the flag exists not to touch what already
    exists, so an undeterminable answer must not become an upload.

Also pinned: the title the search uses comes from the same
``build_record_title`` the record itself is built with.  A second copy of
that rule would search for a title no record carries, and the search would
find nothing — silently defeating both this flag and the duplicate guard.

Run from the project root:

    python3 -m unittest discover -s tests -v
"""

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "Resources"))

import standalone_tasks as tasks  # noqa: E402
from models.audiomoth import DataCollector, UploadData  # noqa: E402

_ESID = "005"
_CONFIG = {"title_template": "ESID#$esid", "minimum_recording_year": 2000}
_FAKE_ENV = {
    "INVENIO_RDM_ACCESS_TOKEN": "unit-test-token",
    "INVENIO_RDM_BASE_URL": "https://zenodo.invalid/api/",
}


def make_collector(esid: str = _ESID, **overrides) -> DataCollector:
    """Minimal valid DataCollector (same shape as the other test modules)."""
    data = {
        "esid": esid,
        "affiliation": "Eclipse Soundscapes : ARISA Lab",
        "files_date_time_mode": "Automatic",
        "version": "2024.1.0",
        "latitude": "35.0000",
        "longitude": "-106.0000",
        "eclipse_date": "2024-04-08",
        "eclipse_type": "Total",
        "eclipse_coverage": "100",
        "eclipse_start_time_utc": "17:00:00",
        "eclipse_maximum_time_utc": "18:15:00",
        "subjects": "eclipse : audiomoth",
    }
    data.update(overrides)
    return DataCollector.model_validate(data)


class _Case(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        env = mock.patch.dict("os.environ", _FAKE_ENV)
        env.start()
        self.addCleanup(env.stop)

        self.folder = self.root / f"ESID_{_ESID}_Staging"
        self.folder.mkdir()
        archive = self.folder / f"ESID_{_ESID}.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr(f"ESID_{_ESID}/20240408_120000.WAV", b"\x00" * 64)
        self.data = UploadData(
            esid=_ESID,
            data_collector=make_collector(),
            staging_folder=str(self.folder),
            archives=[str(archive)],
        )

    def run_worker(self, *, search_result=([], []), search_error=None,
                   skip_existing_records=True):
        """Run the per-ESID worker, returning (stats, mocks).

        The integrity gate and the uploader are tripwired: if the flag is
        doing its job, neither may be reached for an existing record.
        """
        stats = {"total_processed": 0, "successful": 0, "failed": 0,
                 "skipped": 0}
        tracker = mock.MagicMock()
        search = mock.Mock(
            side_effect=search_error if search_error else None,
            return_value=search_result,
        )
        with mock.patch.object(tasks, "_search_drafts_by_title", search), \
             mock.patch.object(tasks, "verify_dataset_integrity",
                               return_value=[]) as gate, \
             mock.patch.object(tasks, "upload_dataset") as up, \
             mock.patch.object(tasks, "archive_staging_to_uploaded") as move:
            up.return_value = {"successful": True, "api_response": {},
                               "error": None}
            tasks._process_one_dataset_inner(
                index=1, total=1, data=self.data,
                delete_failures=False, auto_publish=False, reserve_doi=False,
                related_identifiers_csv=None, references_csv=None,
                project_config=_CONFIG,
                successful_results_file=str(self.root / "ok.csv"),
                failure_results_file=str(self.root / "failed.csv"),
                tracker=tracker, tracker_lock=mock.MagicMock(),
                results_lock=mock.MagicMock(), stats_lock=mock.MagicMock(),
                stats=stats, verify_zip_hash=False,
                skip_existing_records=skip_existing_records,
            )
        return stats, {"search": search, "gate": gate, "upload": up,
                       "move": move, "tracker": tracker}


class TestSkipsWhatZenodoAlreadyHolds(_Case):

    def test_existing_draft_is_skipped_without_uploading(self):
        stats, m = self.run_worker(search_result=([{"id": "rec42"}], []))
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(stats["total_processed"], 0)
        m["upload"].assert_not_called()
        m["move"].assert_not_called()
        m["tracker"].mark_uploaded.assert_not_called()

    def test_published_record_is_skipped_not_failed(self):
        """A published record means the site is finished; it belongs in the
        skipped count, so failed_results.csv stays a list of real problems."""
        stats, m = self.run_worker(search_result=([], [{"id": "rec99"}]))
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["failed"], 0)
        m["upload"].assert_not_called()
        self.assertFalse((self.root / "failed.csv").exists())

    def test_the_skip_happens_before_the_integrity_gate(self):
        """The gate re-hashes every archive; a skipped folder must not pay
        for that."""
        _stats, m = self.run_worker(search_result=([{"id": "rec42"}], []))
        m["gate"].assert_not_called()

    def test_the_staging_folder_is_left_untouched(self):
        before = sorted(p.name for p in self.folder.iterdir())
        self.run_worker(search_result=([{"id": "rec42"}], []))
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), before)


class TestProceedsWhenNothingExists(_Case):

    def test_no_match_uploads_normally(self):
        stats, m = self.run_worker(search_result=([], []))
        self.assertEqual(stats["skipped"], 0)
        self.assertEqual(stats["successful"], 1)
        m["gate"].assert_called_once()
        m["upload"].assert_called_once()
        m["move"].assert_called_once()

    def test_flag_off_does_not_search_at_all(self):
        stats, m = self.run_worker(skip_existing_records=False)
        m["search"].assert_not_called()
        self.assertEqual(stats["successful"], 1)

    def test_flag_off_still_uploads_even_when_a_draft_exists(self):
        """Default behaviour is unchanged: the uploader resumes an existing
        draft rather than skipping it."""
        stats, m = self.run_worker(
            search_result=([{"id": "rec42"}], []), skip_existing_records=False
        )
        m["search"].assert_not_called()
        self.assertEqual(stats["skipped"], 0)
        m["upload"].assert_called_once()


class TestFailsClosedWhenUndeterminable(_Case):
    """An undeterminable answer must not become an upload."""

    def test_search_error_fails_the_dataset_and_does_not_upload(self):
        stats, m = self.run_worker(
            search_error=RuntimeError("Zenodo 502")
        )
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["skipped"], 0)
        m["upload"].assert_not_called()
        m["gate"].assert_not_called()

    def test_search_error_writes_a_failure_row_naming_the_cause(self):
        import csv
        self.run_worker(search_error=RuntimeError("Zenodo 502"))
        with open(self.root / "failed.csv", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(rows[0]["esid"], _ESID)
        self.assertEqual(rows[0]["error_type"], "ExistingRecordCheckFailed")
        self.assertIn("Zenodo 502", rows[0]["error_message"])


class TestSearchesTheTitleTheRecordActuallyCarries(_Case):
    """If the pre-check and the record disagreed about the title, the search
    would find nothing and the flag would silently never fire."""

    def test_the_search_uses_build_record_title(self):
        with mock.patch.object(
            tasks, "_search_drafts_by_title", return_value=([], [])
        ) as search:
            tasks.find_existing_zenodo_record(self.data, _CONFIG)
        searched_title = search.call_args.args[1]
        self.assertEqual(
            searched_title,
            tasks.build_record_title(self.data.data_collector, _CONFIG),
        )
        self.assertEqual(searched_title, f"ESID#{_ESID}")

    def test_get_draft_config_titles_the_record_the_same_way(self):
        """The one that would actually bite: the pre-check and the draft
        must render the same string from the same collector."""
        collector = make_collector(esid="122_Part_1_of_2")
        expected = tasks.build_record_title(collector, _CONFIG)
        # The ESID renders in display form, so this is a real difference
        # from the raw ESID and worth pinning.
        self.assertEqual(expected, "ESID#122 Part 1 of 2")
        readme = self.root / "README.html"
        readme.write_text("<p>description</p>", encoding="utf-8")
        config = tasks.get_draft_config(
            data_collector=collector, readme_html_path=str(readme),
            project_config=_CONFIG,
        )
        self.assertEqual(config.metadata["title"], expected)

    def test_report_names_the_record_id_for_both_kinds(self):
        with mock.patch.object(tasks, "_search_drafts_by_title",
                               return_value=([{"id": "d7"}], [])):
            self.assertIn("d7", tasks.find_existing_zenodo_record(
                self.data, _CONFIG))
        with mock.patch.object(tasks, "_search_drafts_by_title",
                               return_value=([], [{"id": "p9"}])):
            found = tasks.find_existing_zenodo_record(self.data, _CONFIG)
        self.assertIn("p9", found)
        self.assertIn("PUBLISHED", found)

    def test_no_match_reports_none(self):
        with mock.patch.object(tasks, "_search_drafts_by_title",
                               return_value=([], [])):
            self.assertIsNone(
                tasks.find_existing_zenodo_record(self.data, _CONFIG)
            )


if __name__ == "__main__":
    unittest.main()
