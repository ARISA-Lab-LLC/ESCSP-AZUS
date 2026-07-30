"""Discovery, work-item shape and uploader wiring for the PER-DAY layout.

Where tests/test_upload_integrity_day_zips.py covers the gate, this file
covers the pipeline around it: one prepared FOLDER must become exactly one
:class:`UploadData`, and therefore exactly one Zenodo record, however many
day archives it holds.

The bugs pinned here are the ones a per-day folder hit before the rewrite,
all of which shared the same root cause — discovery emitting one work item
per archive rather than per folder:

  * N archives became N datasets, so one site made N draft-creation
    attempts against one title, one staging folder and one
    upload_state.json;
  * because prep's upload manifest is a directory scan, the archives that
    were not "the ZIP" leaked into ``additional_files`` and uploaded as
    companions, with the default retry budget instead of
    ``--upload-attempts``;
  * the first archive to finish moved the staging folder into
    Uploaded_Data/, stranding the rest on paths that no longer existed.

No network: the uploader is patched at the module boundary, and
``create_draft_record`` is tripwired so any real draft attempt fails loudly
rather than silently reaching Zenodo.

Run from the project root:

    python3 -m unittest discover -s tests -v
"""

import csv
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "Resources"))

import azus_common  # noqa: E402
import standalone_tasks as tasks  # noqa: E402
import standalone_uploader as uploader  # noqa: E402
from models.audiomoth import DataCollector, UploadData  # noqa: E402

_ESID = "005"
_DAYS = ("2024_04_08", "2024_04_09", "2024_04_10")
_COMPANIONS = ("License.txt", "WAV_data_dict.csv")


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


class _PerDayFolder(unittest.TestCase):
    """Fixture: a staging root holding one prepared per-day folder."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.staging_root = self.root / "Staging_Area"
        self.staging_root.mkdir()
        self.folder = self.staging_root / f"ESID_{_ESID}_Staging"
        self.folder.mkdir()

    def build(self, days=_DAYS, *, version="2024.1.0A", companions=_COMPANIONS):
        """Write day archives, companions, READMEs and the upload manifest."""
        self.archives = []
        for day in days:
            zip_path = self.folder / azus_common.day_zip_name(_ESID, day)
            stem = zip_path.stem
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr(f"{stem}/CONFIG.TXT", "gain: medium\n")
                zf.writestr(
                    f"{stem}/{day.replace('_', '')}_120000.WAV", b"\x00" * 64
                )
            self.archives.append(zip_path)
        for name in companions:
            (self.folder / name).write_text("x", encoding="utf-8")
        (self.folder / "README.html").write_text("<p>d</p>", encoding="utf-8")
        (self.folder / "README.md").write_text("# d", encoding="utf-8")
        # Prep records the site's collector row here, with the per-day
        # version marker already applied.  version=None simulates a folder
        # that predates the staged CSV.
        if version is not None:
            with open(self.folder / "total_eclipse_data.csv", "w",
                      encoding="utf-8", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=["ESID", "Version"])
                writer.writeheader()
                writer.writerow({"ESID": _ESID, "Version": version})
        # The manifest is a DIRECTORY SCAN in prep, so it lists every
        # archive as well as the companions.  That is what made the
        # archives leak into additional_files.
        listed = (
            [a.name for a in self.archives]
            + list(companions)
            + ["README.md"]
            + (["total_eclipse_data.csv"] if version is not None else [])
        )
        with open(self.folder / f"ESID_{_ESID}_to_upload.csv", "w",
                  encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["File Name", "Description"])
            for name in listed:
                writer.writerow([name, "x"])
        (self.folder / azus_common.PREP_SENTINEL).touch()
        return self.archives

    def discover(self, tracker=None, **kw):
        """Run discovery with the collectors CSV parse stubbed out."""
        tracker = tracker or mock.MagicMock(is_uploaded=lambda p: False)
        with mock.patch.object(
            tasks, "parse_collectors_csv", return_value=[make_collector()]
        ):
            return tasks.get_upload_data(
                data_dir=str(self.staging_root),
                data_collectors_file="collectors.csv",
                dataset_category="Total",
                failure_results_file=str(self.root / "failed.csv"),
                tracker=tracker,
                project_config={"default_required_files": []},
                **kw,
            )


class TestOneFolderIsOneDataset(_PerDayFolder):

    def test_three_archives_make_one_work_item(self):
        archives = self.build()
        data = self.discover()
        self.assertEqual(len(data), 1, "one folder must be one dataset")
        (item,) = data
        self.assertEqual(item.esid, _ESID)
        self.assertEqual(item.staging_folder, str(self.folder))
        self.assertEqual(item.archives, [str(a) for a in archives])

    def test_no_archive_leaks_into_additional_files(self):
        """The manifest lists every archive; none may arrive as a companion."""
        self.build()
        (item,) = self.discover()
        leaked = [f for f in item.additional_files if f.endswith(".zip")]
        self.assertEqual(leaked, [], f"archives leaked as companions: {leaked}")

    def test_all_files_puts_every_archive_last_in_day_order(self):
        archives = self.build()
        (item,) = self.discover()
        tail = item.all_files[-len(archives):]
        self.assertEqual(tail, [str(a) for a in archives])
        self.assertEqual(item.all_files[0], str(self.folder / "README.md"))
        # Exactly once each — a duplicate is a Zenodo 400 on the second PUT.
        for archive in archives:
            self.assertEqual(item.all_files.count(str(archive)), 1)

    def test_mixed_layout_folder_is_refused_with_a_failure_row(self):
        self.build()
        (self.folder / f"ESID_{_ESID}.zip").write_bytes(b"PK")
        self.assertEqual(self.discover(), [])
        with open(self.root / "failed.csv", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["esid"], _ESID)
        self.assertIn("mixed layout", rows[0]["error_message"])

    def test_folder_with_no_archive_is_refused_with_a_failure_row(self):
        self.build(days=())
        self.assertEqual(self.discover(), [])
        with open(self.root / "failed.csv", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertIn("No data archive", rows[0]["error_message"])


class TestTrackerSemantics(_PerDayFolder):
    """The tracker records archive paths, so a dataset is done only when
    every archive is."""

    def test_all_archives_recorded_means_skip(self):
        archives = self.build()
        recorded = {str(a) for a in archives}
        tracker = mock.MagicMock(is_uploaded=lambda p: p in recorded)
        self.assertEqual(self.discover(tracker=tracker), [])

    def test_partially_recorded_folder_is_not_skipped(self):
        """3-of-5 must re-enter the pipeline: the uploader's name+size+md5
        check is what skips the archives already committed remotely, and it
        cannot run if the dataset never becomes a work item."""
        archives = self.build()
        recorded = {str(archives[0])}
        tracker = mock.MagicMock(is_uploaded=lambda p: p in recorded)
        data = self.discover(tracker=tracker)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0].archives, [str(a) for a in archives])


class TestVersionMarker(_PerDayFolder):
    """The per-day marker prep writes into the staging folder's own
    total_eclipse_data.csv must reach the record."""

    def test_staged_version_overrides_the_collectors_csv(self):
        self.build(version="2024.1.0A")
        (item,) = self.discover()
        self.assertEqual(item.data_collector.version, "2024.1.0A")

    def test_the_shared_collector_object_is_not_mutated(self):
        self.build(version="2024.1.0A")
        collector = make_collector()
        with mock.patch.object(
            tasks, "parse_collectors_csv", return_value=[collector]
        ):
            (item,) = tasks.get_upload_data(
                data_dir=str(self.staging_root),
                data_collectors_file="collectors.csv",
                dataset_category="Total",
                failure_results_file=str(self.root / "failed.csv"),
                tracker=mock.MagicMock(is_uploaded=lambda p: False),
                project_config={"default_required_files": []},
            )
        self.assertEqual(item.data_collector.version, "2024.1.0A")
        self.assertEqual(collector.version, "2024.1.0")

    def test_absent_staged_csv_keeps_the_collectors_value(self):
        self.build(version=None)
        (item,) = self.discover()
        self.assertEqual(item.data_collector.version, "2024.1.0")


class TestRecordingDatesSpanEveryArchive(_PerDayFolder):

    def test_interval_covers_the_whole_campaign(self):
        """Reading one archive would date the record to a single day."""
        archives = self.build()
        start, end = tasks.get_recording_dates(
            [str(a) for a in archives], {"minimum_recording_year": 2000}
        )
        self.assertEqual((start, end), ("2024-04-08", "2024-04-10"))

    def test_a_single_path_is_rejected_not_iterated_as_characters(self):
        archives = self.build()
        with self.assertRaises(ValueError) as ctx:
            tasks.get_recording_dates(str(archives[0]), {})
        self.assertIn("LIST of archive paths", str(ctx.exception))


class TestUploaderWiring(_PerDayFolder):
    """What upload_dataset hands to the uploader for a per-day dataset."""

    def _upload_kwargs(self, **flags):
        archives = self.build()
        data = UploadData(
            esid=_ESID,
            data_collector=make_collector(),
            staging_folder=str(self.folder),
            archives=[str(a) for a in archives],
            readme_md=str(self.folder / "README.md"),
            additional_files=[str(self.folder / c) for c in _COMPANIONS],
        )
        with mock.patch.object(tasks, "upload_to_zenodo") as up, \
             mock.patch.object(tasks, "get_draft_config",
                               return_value=mock.MagicMock()), \
             mock.patch.object(tasks, "save_metadata_json"), \
             mock.patch.object(
                 uploader, "create_draft_record",
                 side_effect=AssertionError("must not reach Zenodo")):
            up.return_value = {"successful": True, "api_response": {},
                               "error": None}
            result = tasks.upload_dataset(
                data=data,
                project_config={"title_template": "ESID#$esid"},
                **flags,
            )
        self.assertTrue(result["successful"], result)
        return up.call_args.kwargs, archives

    def test_every_archive_gets_the_upload_attempts_budget(self):
        """Only one archive used to be named, so the rest silently fell back
        to the default 3 attempts."""
        kwargs, archives = self._upload_kwargs(upload_attempts=7)
        self.assertEqual(kwargs["upload_attempts"], 7)
        self.assertEqual(kwargs["priority_files"],
                         {a.name for a in archives})

    def test_companions_are_not_in_the_priority_set(self):
        kwargs, _ = self._upload_kwargs()
        for companion in _COMPANIONS:
            self.assertNotIn(companion, kwargs["priority_files"])

    def test_defer_zip_defers_every_archive_and_holds_review(self):
        kwargs, archives = self._upload_kwargs(defer_zip=True)
        for archive in archives:
            self.assertNotIn(str(archive), kwargs["files"])
        # The companions still upload, and review must wait for the data.
        self.assertIn(str(self.folder / "README.md"), kwargs["files"])
        self.assertFalse(kwargs["submit_review"])

    def test_default_run_uploads_every_archive(self):
        kwargs, archives = self._upload_kwargs()
        for archive in archives:
            self.assertIn(str(archive), kwargs["files"])


class TestZenodoFileCap(_PerDayFolder):
    """Prep budgets for the cap before writing archives, but the manifest
    is a directory scan, so a file added afterwards can push the real set
    over.  The dataset must fail locally, before any network work."""

    def test_over_cap_dataset_fails_the_gate_and_never_uploads(self):
        over = azus_common.ZENODO_MAX_FILES_PER_RECORD + 5
        self.build(companions=tuple(f"pad_{i}.csv" for i in range(over)))
        (item,) = self.discover()
        self.assertGreater(
            len(item.all_files), azus_common.ZENODO_MAX_FILES_PER_RECORD
        )
        stats = {"total_processed": 0, "failed": 0}
        with mock.patch.object(tasks, "upload_dataset") as up, \
             mock.patch.object(tasks, "verify_dataset_integrity",
                               return_value=[]):
            tasks._process_one_dataset_inner(
                index=1, total=1, data=item,
                delete_failures=False, auto_publish=False, reserve_doi=False,
                related_identifiers_csv=None, references_csv=None,
                project_config={}, successful_results_file=str(
                    self.root / "ok.csv"),
                failure_results_file=str(self.root / "failed.csv"),
                tracker=mock.MagicMock(), tracker_lock=mock.MagicMock(),
                results_lock=mock.MagicMock(), stats_lock=mock.MagicMock(),
                stats=stats, verify_zip_hash=False,
            )
        up.assert_not_called()
        self.assertEqual(stats["failed"], 1)
        with open(self.root / "failed.csv", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertIn(
            f"{azus_common.ZENODO_MAX_FILES_PER_RECORD}-file limit",
            rows[0]["error_message"],
        )


class TestArchiveMoveHappensOncePerFolder(_PerDayFolder):
    """The staging folder must move only after the WHOLE dataset succeeds.
    Under the old fan-out the first archive's success moved it, stranding
    the others on paths that no longer existed."""

    def test_folder_moves_once_after_the_dataset_succeeds(self):
        archives = self.build()
        data = UploadData(
            esid=_ESID,
            data_collector=make_collector(),
            staging_folder=str(self.folder),
            archives=[str(a) for a in archives],
        )
        tracker = mock.MagicMock()
        with mock.patch.object(tasks, "upload_dataset") as up, \
             mock.patch.object(tasks, "verify_dataset_integrity",
                               return_value=[]), \
             mock.patch.object(tasks, "archive_staging_to_uploaded") as move:
            up.return_value = {"successful": True, "api_response": {},
                               "error": None}
            tasks._process_one_dataset_inner(
                index=1, total=1, data=data,
                delete_failures=False, auto_publish=False, reserve_doi=False,
                related_identifiers_csv=None, references_csv=None,
                project_config={},
                successful_results_file=str(self.root / "ok.csv"),
                failure_results_file=str(self.root / "failed.csv"),
                tracker=tracker, tracker_lock=mock.MagicMock(),
                results_lock=mock.MagicMock(), stats_lock=mock.MagicMock(),
                stats={"total_processed": 0, "successful": 0},
                verify_zip_hash=False,
            )
        move.assert_called_once()
        self.assertEqual(move.call_args.args[0], self.folder.resolve())
        # And every archive is recorded, not just one.
        self.assertEqual(
            sorted(c.args[0] for c in tracker.mark_uploaded.call_args_list),
            sorted(str(a) for a in archives),
        )


if __name__ == "__main__":
    unittest.main()
