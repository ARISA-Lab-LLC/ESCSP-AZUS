"""Unit tests for the ``--skip-date-check`` dates-not-available fallback.

When every WAV filename in a dataset fails the recording-date parse
(unset AudioMoth clock → ``19700101_*`` names below
``minimum_recording_year``, or hex names from old firmware), the flag
uploads the dataset anyway with its recording dates recorded as NOT
AVAILABLE: both recording-day fields stay None and the record's
Collected-dates metadata entry is omitted entirely (Zenodo's schema
requires a valid EDTF value in any dates entry, so omission — not an
invented date or a literal string — is the honest representation).

These tests prove: the fallback engages only on parse failure and only
with the flag; good filenames keep their real dates; the no-flag error
points at the flag; and a record built from the fallback carries no
dates metadata.

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
    """Minimal valid DataCollector (same shape as test_metadata_builders)."""
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


class TestSkipDateCheckFallback(unittest.TestCase):
    """upload_dataset behavior for datasets with unusable WAV names."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _staged_zip(self, esid: str, wav_names) -> str:
        folder = self.root / f"ESID_{esid}_Staging"
        folder.mkdir()
        zip_path = folder / f"ESID_{esid}.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            for name in wav_names:
                zf.writestr(f"ESID_{esid}/{name}", b"\x00" * 64)
        return str(zip_path)

    def _upload(self, wav_names, skip_date_check, collector=None):
        collector = collector or make_collector()
        data = UploadData(
            esid=collector.esid,
            data_collector=collector,
            zip_file=self._staged_zip(collector.esid, wav_names),
        )
        with mock.patch.object(tasks, "upload_to_zenodo") as up, \
             mock.patch.object(tasks, "get_draft_config") as cfg, \
             mock.patch.object(tasks, "save_metadata_json"):
            up.return_value = {"successful": True, "api_response": {},
                               "error": None}
            cfg.return_value = mock.MagicMock()
            result = tasks.upload_dataset(
                data=data,
                project_config=_PROJECT_CONFIG,
                skip_date_check=skip_date_check,
            )
        return result, collector, up

    def test_unset_clock_names_fail_without_flag_and_mention_it(self):
        result, _, up = self._upload(
            ["19700101_000000.WAV", "19700101_001000.WAV"],
            skip_date_check=False,
        )
        self.assertFalse(result["successful"])
        self.assertIn("--skip-date-check", result["error"]["error_message"])
        up.assert_not_called()

    def test_flag_uploads_with_dates_not_available(self):
        """The fallback leaves both recording-day fields None — nothing
        is invented, and the record will carry no dates metadata."""
        result, collector, up = self._upload(
            ["19700101_000000.WAV"], skip_date_check=True
        )
        self.assertTrue(result["successful"])
        self.assertIsNone(collector.first_recording_day)
        self.assertIsNone(collector.last_recording_day)
        up.assert_called_once()

    def test_hex_firmware_names_also_upload(self):
        result, collector, _ = self._upload(
            ["5D8F3A2B.WAV", "5D8F4C11.WAV"], skip_date_check=True
        )
        self.assertTrue(result["successful"])
        self.assertIsNone(collector.first_recording_day)

    def test_good_filenames_keep_real_dates_even_with_flag(self):
        """The flag is a fallback, never an override."""
        result, collector, _ = self._upload(
            ["20240408_120000.WAV", "20240409_130000.WAV"],
            skip_date_check=True,
        )
        self.assertTrue(result["successful"])
        self.assertEqual(collector.first_recording_day, "2024-04-08")
        self.assertEqual(collector.last_recording_day, "2024-04-09")


class TestRecordCarriesNoDatesMetadata(unittest.TestCase):
    """The real get_draft_config omits the dates entry for the fallback."""

    def test_none_recording_days_omit_dates(self):
        collector = make_collector()
        collector.first_recording_day = None
        collector.last_recording_day = None
        with tempfile.TemporaryDirectory() as tmp:
            readme = Path(tmp) / "README.html"
            readme.write_text("<p>desc</p>", encoding="utf-8")
            config = tasks.get_draft_config(
                data_collector=collector,
                readme_html_path=str(readme),
                project_config={
                    "title_template": "$eclipse_date $eclipse_label ESID#$esid",
                },
            )
        # metadata serializes with exclude_none — no dates key at all.
        self.assertNotIn("dates", config.metadata)

    def test_real_dates_still_produce_interval(self):
        collector = make_collector()
        collector.first_recording_day = "2024-04-06"
        collector.last_recording_day = "2024-04-10"
        with tempfile.TemporaryDirectory() as tmp:
            readme = Path(tmp) / "README.html"
            readme.write_text("<p>desc</p>", encoding="utf-8")
            config = tasks.get_draft_config(
                data_collector=collector,
                readme_html_path=str(readme),
                project_config={
                    "title_template": "$eclipse_date $eclipse_label ESID#$esid",
                },
            )
        self.assertEqual(len(config.metadata["dates"]), 1)
        self.assertEqual(
            config.metadata["dates"][0]["date"], "2024-04-06/2024-04-10"
        )


if __name__ == "__main__":
    unittest.main()
