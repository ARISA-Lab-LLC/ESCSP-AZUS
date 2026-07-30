"""End-to-end: real per-day prep output driven through the real upload path.

Every other per-day upload test builds its staging folder by hand, which
means they all share one blind spot — if prep's real output drifts from what
those fixtures imitate, they would keep passing while production broke.
This module removes that blind spot by running the ACTUAL
``Resources/prepare_dataset.py`` CLI in a hermetic project tree, then
feeding the folder it produced to the real discovery, integrity and upload
wiring, with only the Zenodo HTTP boundary mocked.

It is the test that would have caught the three original defects together:
one record per site rather than N, every day archive attached to it, and the
staging folder moved exactly once.

The prep subprocess touches no network.  The upload half runs in-process so
the uploader can be patched; ``create_draft_record`` and
``_api_get_with_retry`` are tripwired so any real HTTP attempt fails loudly
instead of escaping to Zenodo.

Run from the project root:

    python3 -m unittest discover -s tests -v
"""

import csv
import shutil
import subprocess
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

_ESID = "005"
# Three recording days; the 1970 file is an unset AudioMoth clock, which
# prep keeps deliberately and which must therefore survive into an archive.
_WAVS = {
    "20240408_120000.WAV": 4000,
    "20240408_130000.WAV": 6000,
    "20240409_090000.WAV": 5000,
    "19700101_000000.WAV": 3000,
}
_DAYS = ("1970_01_01", "2024_04_08", "2024_04_09")

_FAKE_ENV = {
    "INVENIO_RDM_ACCESS_TOKEN": "unit-test-token",
    # .invalid can never resolve: a leaked request fails DNS, not silently
    # reaching a real host.
    "INVENIO_RDM_BASE_URL": "https://zenodo.invalid/api/",
}


def riff_header(declared_total: int) -> bytes:
    chunk = declared_total - 8
    return b"RIFF" + chunk.to_bytes(4, "little") + b"WAVE"


def write_wav(path: Path, total_size: int) -> None:
    """A well-formed WAV whose stat size and RIFF header agree."""
    path.write_bytes(riff_header(total_size) + b"\x00" * (total_size - 12))


class TestPerDayPrepToUpload(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        env = mock.patch.dict("os.environ", _FAKE_ENV)
        env.start()
        self.addCleanup(env.stop)

    # --- hermetic project tree (same shape as test_prepare_dataset_day_zips) ---

    def _fake_project(self) -> Path:
        fake_root = self.root / "fake_project"
        fake_resources = fake_root / "Resources"
        fake_resources.mkdir(parents=True)
        for module in ("prepare_dataset.py", "audit_wav_integrity.py",
                       "azus_common.py"):
            shutil.copy2(_PROJECT_ROOT / "Resources" / module,
                         fake_resources / module)
        for data_file in ("README_template.html", "resource_files_list.csv",
                          "License.txt"):
            shutil.copy2(_PROJECT_ROOT / "Resources" / data_file,
                         fake_resources / data_file)
        (fake_root / "Staging_Area").mkdir()
        return fake_root

    def _raw_and_collectors(self):
        source = self.root / "Raw_Data" / f"ESID_{_ESID}"
        source.mkdir(parents=True)
        for name, size in _WAVS.items():
            write_wav(source / name, size)
        (source / "CONFIG.TXT").write_text("gain: medium\n")
        # One CSV that satisfies BOTH sides: prep reads a handful of
        # columns, while the upload side validates the full DataCollector
        # schema, so the real spreadsheet's column names are used here.
        collectors = self.root / "collectors.csv"
        headers = [
            "ESID", "Data Collector Affiliations",
            "WAV Files Time & Date Settings", "Version",
            "Latitude", "Longitude", "Eclipse Date", "Local Eclipse Type",
            "Eclipse Percent (%)",
            "Eclipse Start Time (UTC) (1st Contact)",
            "Eclipse Maximum (UTC)", "Keywords and subjects",
        ]
        row = [
            _ESID, "Eclipse Soundscapes : ARISA Lab", "Automatic",
            "2024.1.0", "35.0800", "-106.6500", "2024-04-08", "Total",
            "100", "17:00:00", "18:15:00", "eclipse : audiomoth",
        ]
        with open(collectors, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(headers)
            writer.writerow(row)
        return source, collectors

    def _prep(self, *extra) -> Path:
        """Run the real prep CLI and return the staging folder it wrote."""
        fake_root = self._fake_project()
        source, self.collectors = self._raw_and_collectors()
        result = subprocess.run(
            [sys.executable,
             str(fake_root / "Resources" / "prepare_dataset.py"),
             str(source), "--collector-csv", str(self.collectors), *extra],
            capture_output=True, text=True, cwd=fake_root,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.staging_root = fake_root / "Staging_Area"
        staged = self.staging_root / f"ESID_{_ESID}_Staging"
        self.assertTrue((staged / azus_common.PREP_SENTINEL).is_file())
        return staged

    # --- the upload half, with only the HTTP boundary mocked ---------------

    def _upload(self, *, defer_zip=False, upload_attempts=5):
        """Discover + verify + upload, returning the uploader's kwargs."""
        tracker = mock.MagicMock(is_uploaded=lambda p: False)
        project_config = {
            "default_required_files": [],
            "title_template": "ESID#$esid",
            "minimum_recording_year": 2000,
        }
        data = tasks.get_upload_data(
            data_dir=str(self.staging_root),
            data_collectors_file=str(self.collectors),
            dataset_category="Total",
            failure_results_file=str(self.root / "failed.csv"),
            tracker=tracker,
            project_config=project_config,
        )
        self.assertEqual(len(data), 1, "one prepared folder is one dataset")
        (item,) = data

        with mock.patch.object(tasks, "upload_to_zenodo") as up, \
             mock.patch.object(tasks, "save_metadata_json"), \
             mock.patch.object(tasks, "archive_staging_to_uploaded") as move, \
             mock.patch.object(
                 uploader, "create_draft_record",
                 side_effect=AssertionError("must not create a real draft")), \
             mock.patch.object(
                 uploader, "_api_get_with_retry",
                 side_effect=AssertionError("must not call Zenodo")):
            up.return_value = {"successful": True,
                               "api_response": {"id": "rec123"},
                               "error": None}
            tasks._process_one_dataset_inner(
                index=1, total=1, data=item,
                delete_failures=False, auto_publish=False, reserve_doi=False,
                related_identifiers_csv=None, references_csv=None,
                project_config=project_config,
                successful_results_file=str(self.root / "ok.csv"),
                failure_results_file=str(self.root / "failed.csv"),
                tracker=tracker, tracker_lock=mock.MagicMock(),
                results_lock=mock.MagicMock(), stats_lock=mock.MagicMock(),
                stats={"total_processed": 0, "successful": 0, "failed": 0},
                # The real gate runs; only the hash step is skipped, to keep
                # the test fast.  Structural checks are the point here.
                verify_zip_hash=False,
                defer_zip=defer_zip, upload_attempts=upload_attempts,
            )
            self.assertEqual(
                up.call_count, 1,
                "one dataset must produce exactly ONE upload call, i.e. one "
                "Zenodo record — N archives previously produced N records",
            )
            return item, up.call_args.kwargs, move, tracker

    # --- tests -------------------------------------------------------------

    def test_real_per_day_prep_output_passes_the_gate(self):
        """The gate must accept genuine prep output, not just our fixtures."""
        staged = self._prep()
        archives, mode, problems = tasks.resolve_dataset_archives(
            staged, _ESID)
        self.assertEqual(problems, [])
        self.assertEqual(mode, tasks._prep_contract.ZIP_MODE_PER_DAY)
        self.assertEqual(
            [a.name for a in archives],
            [f"ESID_{_ESID}_{day}.zip" for day in _DAYS],
        )
        self.assertEqual(
            tasks.verify_dataset_integrity(
                str(staged), _ESID, archives=[str(a) for a in archives]
            ),
            [],
        )

    def test_one_record_gets_every_day_archive_plus_companions(self):
        staged = self._prep()
        item, kwargs, move, tracker = self._upload()

        expected = [f"ESID_{_ESID}_{day}.zip" for day in _DAYS]
        uploaded = [Path(f).name for f in kwargs["files"]]
        for name in expected:
            self.assertIn(name, uploaded)
        # Archives last, in ascending day order.
        self.assertEqual(uploaded[-len(expected):], expected)
        # Companions came along, and none of them is an archive.
        self.assertIn("README.md", uploaded)
        self.assertNotIn("README.html", uploaded)
        self.assertEqual(
            [f for f in item.additional_files if f.endswith(".zip")], [],
            "an archive leaked into additional_files",
        )
        # No duplicates: a repeated key is a Zenodo 400 on the second PUT.
        self.assertEqual(len(uploaded), len(set(uploaded)))

    def test_every_day_archive_gets_the_upload_attempts_budget(self):
        self._prep()
        _item, kwargs, _move, _tracker = self._upload(upload_attempts=5)
        self.assertEqual(kwargs["upload_attempts"], 5)
        self.assertEqual(
            kwargs["priority_files"],
            {f"ESID_{_ESID}_{day}.zip" for day in _DAYS},
        )

    def test_the_record_version_carries_preps_per_day_marker(self):
        """Prep marks the version in the staged CSV; the record must show it."""
        staged = self._prep()
        with open(staged / "total_eclipse_data.csv", encoding="utf-8") as fh:
            staged_version = list(csv.DictReader(fh))[0]["Version"]
        self.assertEqual(staged_version, "2024.1.0A")
        item, _kwargs, _move, _tracker = self._upload()
        self.assertEqual(item.data_collector.version, "2024.1.0A")

    def test_recording_dates_span_every_day_not_just_one(self):
        staged = self._prep()
        archives, _, _ = tasks.resolve_dataset_archives(staged, _ESID)
        start, end = tasks.get_recording_dates(
            [str(a) for a in archives], {"minimum_recording_year": 2000}
        )
        # 1970 is below the minimum year and must not become the start date,
        # even though prep deliberately keeps that WAV in its archive.
        self.assertEqual((start, end), ("2024-04-08", "2024-04-09"))
        with zipfile.ZipFile(
            staged / f"ESID_{_ESID}_1970_01_01.zip"
        ) as zf:
            self.assertIn(
                "19700101_000000.WAV",
                {Path(n).name for n in zf.namelist()},
            )

    def test_folder_is_archived_once_and_every_archive_recorded(self):
        self._prep()
        _item, _kwargs, move, tracker = self._upload()
        move.assert_called_once()
        self.assertEqual(
            sorted(c.args[0] for c in tracker.mark_uploaded.call_args_list),
            sorted(
                str(self.staging_root / f"ESID_{_ESID}_Staging"
                    / f"ESID_{_ESID}_{day}.zip")
                for day in _DAYS
            ),
        )

    def test_defer_zip_holds_back_every_archive_and_the_move(self):
        self._prep()
        _item, kwargs, move, tracker = self._upload(defer_zip=True)
        self.assertEqual(
            [f for f in kwargs["files"] if f.endswith(".zip")], [],
            "--defer-zip must hold back EVERY day archive",
        )
        self.assertIn("README.md", [Path(f).name for f in kwargs["files"]])
        self.assertFalse(kwargs["submit_review"])
        # A deferred record is incomplete: nothing is tracked or moved.
        move.assert_not_called()
        tracker.mark_uploaded.assert_not_called()

    def test_legacy_single_zip_prep_still_uploads_as_one_record(self):
        """The permanent legacy path, driven the same way."""
        staged = self._prep("--single-zip")
        self.assertTrue((staged / f"ESID_{_ESID}.zip").is_file())
        self.assertEqual(list(staged.glob(f"ESID_{_ESID}_*_*_*.zip")), [])
        item, kwargs, move, _tracker = self._upload()
        self.assertEqual(item.archives,
                         [str(staged / f"ESID_{_ESID}.zip")])
        self.assertEqual(Path(kwargs["files"][-1]).name, f"ESID_{_ESID}.zip")
        self.assertEqual(kwargs["priority_files"], {f"ESID_{_ESID}.zip"})
        # Legacy prep does NOT mark the version.
        self.assertEqual(item.data_collector.version, "2024.1.0")
        move.assert_called_once()


if __name__ == "__main__":
    unittest.main()
