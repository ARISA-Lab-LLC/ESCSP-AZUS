"""Requirement 9: the ZIP pipeline must SKIP file-by-file ESIDs.

An ESID whose upload_state.json is marked ``mode == file_by_file`` is
owned by the file-by-file tool; standalone_tasks.py (the ZIP pipeline)
must never touch it, or the two could both write to the same Zenodo
record. Two guards enforce this: a clean skip in get_upload_data
(discovery) and a second guard at the top of _process_one_dataset_inner
(covers the discover→worker TOCTOU and any direct caller).

Run from the project root:

    python3 -m unittest discover -s tests -v
"""

import json
import sys
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "Resources"))

import azus_common  # noqa: E402
import standalone_tasks as tasks  # noqa: E402
from models.audiomoth import DataCollector, UploadData  # noqa: E402


def _make_collector(esid: str = "007") -> DataCollector:
    return DataCollector.model_validate({
        "esid": esid,
        "affiliation": "Eclipse Soundscapes : ARISA Lab",
        "files_date_time_mode": "Automatic",
        "version": "2024.1.0",
        "latitude": "35.0",
        "longitude": "-106.0",
        "eclipse_date": "2024-04-08",
        "eclipse_type": "Total",
        "eclipse_coverage": "100",
        "eclipse_start_time_utc": "17:00:00",
        "eclipse_maximum_time_utc": "18:15:00",
        "subjects": "eclipse : audiomoth",
    })


def _make_fbf_staging(root: Path, esid: str) -> Path:
    """Create a staging folder with a ZIP and a file-by-file state marker."""
    folder = root / f"ESID_{esid}_Staging"
    folder.mkdir(parents=True)
    zip_path = folder / f"ESID_{esid}.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(f"ESID_{esid}/20240408_120000.WAV", b"\x00" * 32)
    (folder / azus_common.STATE_FILENAME).write_text(
        json.dumps({"record_id": "555", "mode": "file_by_file"})
    )
    return folder


class TestGuard2ProcessInner(unittest.TestCase):
    """The per-ESID worker skips a file-by-file ESID before the ZIP gate."""

    def test_inner_skips_file_by_file_before_integrity_gate(self):
        with mock.patch.object(tasks, "verify_dataset_integrity") as gate, \
             mock.patch.object(tasks, "upload_dataset") as up, \
             __import__("tempfile").TemporaryDirectory() as td:
            folder = _make_fbf_staging(Path(td), "007")
            data = UploadData(
                esid="007",
                data_collector=_make_collector("007"),
                zip_file=str(folder / "ESID_007.zip"),
            )
            stats = {"skipped": 0}
            tasks._process_one_dataset_inner(
                index=1, total=1, data=data,
                delete_failures=False, auto_publish=False, reserve_doi=False,
                related_identifiers_csv=None, references_csv=None,
                project_config={}, successful_results_file=str(Path(td) / "s.csv"),
                failure_results_file=str(Path(td) / "f.csv"),
                tracker=mock.Mock(), tracker_lock=threading.Lock(),
                results_lock=threading.Lock(), stats=stats,
                stats_lock=threading.Lock(),
            )
            # Skipped BEFORE the ZIP integrity gate and the upload.
            gate.assert_not_called()
            up.assert_not_called()
            self.assertEqual(stats["skipped"], 1)


class TestGuard1Discovery(unittest.TestCase):
    """get_upload_data skips a file-by-file ESID cleanly (no failure row)."""

    def test_discovery_skips_file_by_file_without_failure_row(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data_dir = root / "Staging_Area"
            _make_fbf_staging(data_dir, "007")
            collectors = root / "collectors.csv"
            collectors.write_text("ESID\n")  # empty; 007 is skipped before use
            failure_csv = root / "failed.csv"
            tracker = tasks.UploadTracker(str(root / "uploaded.txt"))

            result = tasks.get_upload_data(
                data_dir=str(data_dir),
                data_collectors_file=str(collectors),
                dataset_category="Total",
                failure_results_file=str(failure_csv),
                tracker=tracker,
                project_config={},
            )
            # 007 is not in the work list...
            self.assertEqual([d.esid for d in result], [])
            # ...and it was NOT logged as a failure (clean skip, not the
            # no-ZIP failure-row path).
            failure_text = failure_csv.read_text() if failure_csv.exists() else ""
            self.assertNotIn("007", failure_text)


if __name__ == "__main__":
    unittest.main()
