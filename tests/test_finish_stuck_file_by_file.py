"""Tests for finish_stuck_uploads.py's --enable-file-by-file integration.

Covers the CLI guard (the flag requires --raw-data-dir) and the switch
decision in _run_with_file_by_file: an already-file-by-file ESID is
continued; a stuck ZIP ESID is switched ONLY when number_of_tries has
reached the threshold AND the ZIP is the sole missing file.

Run from the project root:

    python3 -m unittest discover -s tests -v
"""

import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "Resources"))

import azus_common  # noqa: E402
import file_by_file_upload as fbf  # noqa: E402
import finish_stuck_uploads as fs  # noqa: E402
import standalone_uploader as up  # noqa: E402


class TestCliValidation(unittest.TestCase):
    def test_enable_without_raw_data_dir_exits_2(self):
        with mock.patch.object(sys, "argv",
                               ["finish_stuck_uploads.py",
                                "--enable-file-by-file"]):
            with self.assertRaises(SystemExit) as ctx:
                fs.main()
        self.assertEqual(ctx.exception.code, 2)


class TestSwitchDecision(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.raw_root = self.root / "Raw_Data"
        self.raw_root.mkdir()
        (self.raw_root / "ESID#007").mkdir()

    def _staging(self, mode=None):
        folder = self.root / "Staging_Area" / "ESID_007_Staging"
        folder.mkdir(parents=True)
        state = {"record_id": "555", "number_of_tries": 3}
        if mode:
            state["mode"] = mode
        (folder / azus_common.STATE_FILENAME).write_text(json.dumps(state))
        return folder

    def _args(self):
        return Namespace(
            config="Resources/config.json", workers=1, upload_attempts=3,
            skip_date_check=False, tries_threshold=3,
            raw_data_dir=str(self.raw_root),
        )

    def _run(self, stuck, *, tries=3, only_zip=True, fbf_ok=True):
        with mock.patch.object(fs, "run_recovery", return_value=0), \
             mock.patch.object(fs, "_load_publish_config",
                               return_value=("COMM", False, False)), \
             mock.patch.object(azus_common, "find_esid_folders",
                               return_value=[((7, ""), "007",
                                              self.raw_root / "ESID#007")]), \
             mock.patch.object(up, "get_credentials_from_env",
                               return_value=mock.Mock()), \
             mock.patch.object(up, "_read_number_of_tries", return_value=tries), \
             mock.patch.object(fbf, "only_zip_missing", return_value=only_zip), \
             mock.patch.object(fbf, "run_file_by_file",
                               return_value=fbf_ok) as run_fbf:
            code = fs._run_with_file_by_file(stuck, self._args())
        return code, run_fbf

    def test_switch_fires_when_tries_and_only_zip_missing(self):
        folder = self._staging(mode=None)  # ZIP mode
        stuck = [((7, ""), "007", folder, "555")]
        code, run_fbf = self._run(stuck, tries=3, only_zip=True)
        run_fbf.assert_called_once()
        self.assertEqual(code, 0)

    def test_no_switch_when_below_threshold(self):
        folder = self._staging(mode=None)
        stuck = [((7, ""), "007", folder, "555")]
        code, run_fbf = self._run(stuck, tries=2, only_zip=True)
        run_fbf.assert_not_called()
        self.assertEqual(code, 1)  # still unfinished

    def test_no_switch_when_not_only_zip_missing(self):
        folder = self._staging(mode=None)
        stuck = [((7, ""), "007", folder, "555")]
        code, run_fbf = self._run(stuck, tries=5, only_zip=False)
        run_fbf.assert_not_called()
        self.assertEqual(code, 1)

    def test_already_file_by_file_is_continued(self):
        folder = self._staging(mode="file_by_file")
        stuck = [((7, ""), "007", folder, "555")]
        code, run_fbf = self._run(stuck)  # only_zip irrelevant for continue
        run_fbf.assert_called_once()
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
