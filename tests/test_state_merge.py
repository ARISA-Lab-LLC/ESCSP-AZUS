"""Unit tests for the merge-preserving upload_state.json writer.

The uploader used to REBUILD upload_state.json from a fixed set of keys,
which would clobber the ``"mode": "file_by_file"`` marker the file-by-file
fallback relies on.  The writer now read-MERGES, preserving unmanaged
keys.  These tests pin that behaviour plus the shared mode reader.

Run from the project root:

    python3 -m unittest discover -s tests -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "Resources"))

import azus_common  # noqa: E402
import standalone_uploader as up  # noqa: E402


class TestWriteUploadState(unittest.TestCase):
    def test_preserves_mode_and_advances_tries(self):
        with tempfile.TemporaryDirectory() as td:
            sf = Path(td) / "upload_state.json"
            sf.write_text(json.dumps({
                "record_id": "1", "mode": "file_by_file", "number_of_tries": 2,
            }))
            up._write_upload_state(str(sf), "1", True)
            after = json.loads(sf.read_text())
            self.assertEqual(after["mode"], "file_by_file")  # NOT clobbered
            self.assertEqual(after["number_of_tries"], 3)    # advanced
            self.assertEqual(after["record_id"], "1")
            self.assertTrue(after["resumed"])

    def test_creates_state_when_absent(self):
        with tempfile.TemporaryDirectory() as td:
            sf = Path(td) / "upload_state.json"
            up._write_upload_state(str(sf), "42", False)
            after = json.loads(sf.read_text())
            self.assertEqual(after["number_of_tries"], 1)
            self.assertEqual(after["record_id"], "42")
            self.assertFalse(after["resumed"])
            self.assertNotIn("mode", after)

    def test_legacy_state_without_counter_starts_at_one(self):
        with tempfile.TemporaryDirectory() as td:
            sf = Path(td) / "upload_state.json"
            sf.write_text(json.dumps({"record_id": "9"}))  # no counter
            up._write_upload_state(str(sf), "9", True)
            self.assertEqual(json.loads(sf.read_text())["number_of_tries"], 1)


class TestReadUploadMode(unittest.TestCase):
    def test_absent_state_is_none(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(azus_common.read_upload_mode(Path(td)))

    def test_reads_file_by_file(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "upload_state.json").write_text(
                json.dumps({"record_id": "1", "mode": "file_by_file"})
            )
            self.assertEqual(
                azus_common.read_upload_mode(Path(td)),
                azus_common.FILE_BY_FILE_MODE,
            )

    def test_no_mode_key_is_none(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "upload_state.json").write_text(
                json.dumps({"record_id": "1"})
            )
            self.assertIsNone(azus_common.read_upload_mode(Path(td)))

    def test_malformed_state_is_none(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "upload_state.json").write_text("{not json")
            self.assertIsNone(azus_common.read_upload_mode(Path(td)))


if __name__ == "__main__":
    unittest.main()
