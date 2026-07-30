"""Unit tests for the ``--draft-only`` flag.

``--draft-only`` uploads a dataset in full — the data ZIP included — but
leaves the Zenodo record a plain, editable draft: it skips BOTH the
community-review submission and publishing.  It is threaded as
``draft_only`` down through ``upload_datasets`` →
``_process_one_dataset_inner`` → ``upload_dataset``, where it forces the
two knobs the uploader acts on: ``auto_publish`` and ``submit_review``
both off.  It is mutually exclusive with ``--defer-zip`` (that flag skips
the ZIP; this one uploads it).

These tests prove the flag's contract at the ``upload_dataset`` boundary
(what it hands to ``upload_to_zenodo``) and the CLI's rejection of the
contradictory flag pair — without any network calls.

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

_PROJECT_CONFIG = {
    "minimum_recording_year": 2023,
    "title_template": "$eclipse_date $eclipse_label ESID#$esid",
}


def make_collector(esid: str = "005", **overrides) -> DataCollector:
    """Minimal valid DataCollector (same shape as test_skip_date_check)."""
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


class TestDraftOnlyThreading(unittest.TestCase):
    """What upload_dataset hands to upload_to_zenodo under each flag."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _staged_zip(self, esid: str) -> str:
        folder = self.root / f"ESID_{esid}_Staging"
        folder.mkdir()
        zip_path = folder / f"ESID_{esid}.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(f"ESID_{esid}/20240408_120000.WAV", b"\x00" * 64)
        return str(zip_path)

    def _upload_kwargs(self, **flags):
        """Run upload_dataset with mocked network + return the kwargs it
        passed to upload_to_zenodo."""
        collector = make_collector()
        staged = self._staged_zip(collector.esid)
        data = UploadData(
            esid=collector.esid,
            data_collector=collector,
            staging_folder=str(Path(staged).parent),
            archives=[staged],
        )
        with mock.patch.object(tasks, "upload_to_zenodo") as up, \
             mock.patch.object(tasks, "get_draft_config") as cfg, \
             mock.patch.object(tasks, "save_metadata_json"):
            up.return_value = {"successful": True, "api_response": {},
                               "error": None}
            cfg.return_value = mock.MagicMock()
            tasks.upload_dataset(
                data=data, project_config=_PROJECT_CONFIG, **flags,
            )
        up.assert_called_once()
        return up.call_args.kwargs

    def test_draft_only_forces_publish_and_review_off(self):
        # Even with auto_publish requested, --draft-only wins.
        kw = self._upload_kwargs(draft_only=True, auto_publish=True)
        self.assertFalse(kw["auto_publish"])
        self.assertFalse(kw["submit_review"])
        # ...and the ZIP is still uploaded (everything, ZIP included).
        self.assertTrue(any("ESID_005.zip" in f for f in kw["files"]))

    def test_default_submits_review_and_keeps_auto_publish(self):
        # Baseline: without draft_only, both knobs pass through unchanged.
        kw = self._upload_kwargs(draft_only=False, auto_publish=True)
        self.assertTrue(kw["auto_publish"])
        self.assertTrue(kw["submit_review"])

    def test_defer_zip_skips_review_but_is_not_draft_only(self):
        # Existing behavior preserved: --defer-zip skips review AND the ZIP.
        kw = self._upload_kwargs(defer_zip=True, auto_publish=False)
        self.assertFalse(kw["submit_review"])
        self.assertFalse(any("ESID_005.zip" in f for f in kw["files"]))


class TestDraftOnlyCli(unittest.TestCase):
    def test_cli_rejects_draft_only_with_defer_zip(self):
        with mock.patch.object(sys, "argv",
                               ["standalone_tasks.py",
                                "--draft-only", "--defer-zip"]):
            with self.assertRaises(SystemExit) as ctx:
                tasks.main()
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
