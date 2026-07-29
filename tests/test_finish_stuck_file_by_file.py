"""Tests for finish_stuck_uploads.py's --enable-file-by-file integration.

Covers the CLI guards, the switch decision in _run_with_file_by_file (an
already-file-by-file ESID is continued; a stuck ZIP ESID is switched ONLY
when number_of_tries has reached the threshold AND the ZIP is the sole
missing file), --skip-integrity-hash being forwarded to standalone_tasks,
and --force taking an already-inevitable switch out of Phase A.

Run from the project root:

    python3 -m unittest discover -s tests -v
"""

import json
import logging
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

    def _args(self, *, force=False, skip_integrity_hash=False):
        return Namespace(
            config="Resources/config.json", workers=1, upload_attempts=3,
            skip_date_check=False, tries_threshold=3,
            raw_data_dir=str(self.raw_root),
            force=force, skip_integrity_hash=skip_integrity_hash,
        )

    def _run(self, stuck, *, tries=3, only_zip=True, fbf_ok=True,
             force=False):
        """Run the file-by-file phase. Returns (code, run_fbf, run_recovery)."""
        with mock.patch.object(fs, "run_recovery", return_value=0) as recovery, \
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
            code = fs._run_with_file_by_file(
                stuck, self._args(force=force)
            )
        return code, run_fbf, recovery

    def test_switch_fires_when_tries_and_only_zip_missing(self):
        folder = self._staging(mode=None)  # ZIP mode
        stuck = [((7, ""), "007", folder, "555")]
        code, run_fbf, _rec = self._run(stuck, tries=3, only_zip=True)
        run_fbf.assert_called_once()
        self.assertEqual(code, 0)

    def test_no_switch_when_below_threshold(self):
        folder = self._staging(mode=None)
        stuck = [((7, ""), "007", folder, "555")]
        code, run_fbf, _rec = self._run(stuck, tries=2, only_zip=True)
        run_fbf.assert_not_called()
        self.assertEqual(code, 1)  # still unfinished

    def test_no_switch_when_not_only_zip_missing(self):
        folder = self._staging(mode=None)
        stuck = [((7, ""), "007", folder, "555")]
        code, run_fbf, _rec = self._run(stuck, tries=5, only_zip=False)
        run_fbf.assert_not_called()
        self.assertEqual(code, 1)

    def test_already_file_by_file_is_continued(self):
        folder = self._staging(mode="file_by_file")
        stuck = [((7, ""), "007", folder, "555")]
        code, run_fbf, _rec = self._run(stuck)  # only_zip irrelevant here
        run_fbf.assert_called_once()
        self.assertEqual(code, 0)

    # --- Phase A behaviour, with and without --force ---------------------

    def test_phase_a_runs_by_default_even_when_the_switch_will_fire(self):
        """The ZIP gets its last attempt unless --force says otherwise."""
        folder = self._staging(mode=None)
        stuck = [((7, ""), "007", folder, "555")]
        code, run_fbf, recovery = self._run(stuck, tries=3, only_zip=True)
        recovery.assert_called_once()
        self.assertEqual(recovery.call_args.kwargs["stuck_esids"], ["007"])
        run_fbf.assert_called_once()
        self.assertEqual(code, 0)

    def test_force_switches_and_skips_phase_a(self):
        folder = self._staging(mode=None)
        stuck = [((7, ""), "007", folder, "555")]
        code, run_fbf, recovery = self._run(
            stuck, tries=3, only_zip=True, force=True
        )
        recovery.assert_not_called()
        run_fbf.assert_called_once()
        self.assertEqual(code, 0)

    def test_force_ignores_the_tries_threshold(self):
        """The whole point: switch without waiting to accumulate attempts."""
        for tries in (0, 1, 2):
            with self.subTest(tries=tries):
                self.setUp()
                folder = self._staging(mode=None)
                stuck = [((7, ""), "007", folder, "555")]
                code, run_fbf, recovery = self._run(
                    stuck, tries=tries, only_zip=True, force=True
                )
                recovery.assert_not_called()
                run_fbf.assert_called_once()
                self.assertEqual(code, 0)

    def test_force_ignores_even_a_deliberately_high_threshold(self):
        folder = self._staging(mode=None)
        stuck = [((7, ""), "007", folder, "555")]
        with mock.patch.object(fs, "run_recovery", return_value=0) as recovery, \
             mock.patch.object(fs, "_load_publish_config",
                               return_value=("COMM", False, False)), \
             mock.patch.object(azus_common, "find_esid_folders",
                               return_value=[((7, ""), "007",
                                              self.raw_root / "ESID#007")]), \
             mock.patch.object(up, "get_credentials_from_env",
                               return_value=mock.Mock()), \
             mock.patch.object(up, "_read_number_of_tries", return_value=0), \
             mock.patch.object(fbf, "only_zip_missing", return_value=True), \
             mock.patch.object(fbf, "run_file_by_file",
                               return_value=True) as run_fbf:
            args = self._args(force=True)
            args.tries_threshold = 999
            code = fs._run_with_file_by_file(stuck, args)
        recovery.assert_not_called()
        run_fbf.assert_called_once()
        self.assertEqual(code, 0)

    def test_force_still_requires_the_zip_to_be_the_sole_missing_file(self):
        folder = self._staging(mode=None)
        stuck = [((7, ""), "007", folder, "555")]
        code, run_fbf, recovery = self._run(
            stuck, tries=9, only_zip=False, force=True
        )
        recovery.assert_called_once()   # left to the normal ZIP pass
        run_fbf.assert_not_called()
        self.assertEqual(code, 1)

    def test_force_does_not_switch_an_esid_twice(self):
        folder = self._staging(mode=None)
        stuck = [((7, ""), "007", folder, "555")]
        _code, run_fbf, _rec = self._run(
            stuck, tries=3, only_zip=True, force=True
        )
        self.assertEqual(run_fbf.call_count, 1)


class TestSkipIntegrityHashForwarding(unittest.TestCase):
    """The flag must actually reach standalone_tasks.py."""

    def _cmd(self, **kwargs):
        with mock.patch.object(fs.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0)
            fs.run_recovery(stuck_esids=["007"], config_path="c.json",
                            workers=1, **kwargs)
        return run.call_args.args[0]

    def test_absent_by_default(self):
        self.assertNotIn("--skip-integrity-hash", self._cmd())

    def test_forwarded_when_requested(self):
        self.assertIn("--skip-integrity-hash",
                      self._cmd(skip_integrity_hash=True))

    def test_independent_of_skip_date_check(self):
        cmd = self._cmd(skip_date_check=True, skip_integrity_hash=False)
        self.assertIn("--skip-date-check", cmd)
        self.assertNotIn("--skip-integrity-hash", cmd)


class TestDefaultFileLogging(unittest.TestCase):
    """Screen output is always also written to Records/, and a logging
    problem never fails a recovery run."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        # basicConfig is a no-op once handlers exist, and other tests in
        # this suite may have configured the root logger already, so drive
        # configure_logging directly and restore the root logger after.
        root = logging.getLogger()
        original = list(root.handlers)
        original_level = root.level

        def restore():
            for handler in list(root.handlers):
                if handler not in original:
                    handler.close()
                    root.removeHandler(handler)
            for handler in original:
                if handler not in root.handlers:
                    root.addHandler(handler)
            root.setLevel(original_level)

        self.addCleanup(restore)
        for handler in list(root.handlers):
            root.removeHandler(handler)

    def test_default_path_is_timestamp_first_in_records(self):
        path = fs.default_log_path()
        self.assertEqual(path.parent.name, "Records")
        self.assertTrue(path.name.endswith("_finish_stuck_uploads.log"))
        stamp = path.name.split("_finish")[0]
        self.assertRegex(stamp, r"^\d{8}_\d{6}$")
        # Timestamp first so a directory listing sorts chronologically.
        self.assertTrue(path.name[0].isdigit())

    def test_writes_the_log_and_still_prints(self):
        target = self.root / "Records" / "20260729_120000_finish_stuck_uploads.log"
        opened = fs.configure_logging(log_path=target)
        self.assertEqual(opened, target)
        fs.logger.info("a distinctive line")
        logging.getLogger().handlers[-1].flush()
        self.assertIn("a distinctive line", target.read_text(encoding="utf-8"))
        self.assertTrue(
            any(isinstance(h, logging.StreamHandler)
                and not isinstance(h, logging.FileHandler)
                for h in logging.getLogger().handlers),
            "screen output must survive alongside the file",
        )

    def test_creates_the_records_directory(self):
        target = self.root / "brand" / "new" / "run.log"
        self.assertEqual(fs.configure_logging(log_path=target), target)
        self.assertTrue(target.is_file())

    def test_unwritable_log_does_not_fail_the_run(self):
        """A logging problem must never stop a recovery."""
        blocker = self.root / "blocked"
        blocker.write_text("I am a file, not a directory\n")
        target = blocker / "run.log"          # parent cannot be a directory
        self.assertIsNone(fs.configure_logging(log_path=target))
        self.assertTrue(
            any(isinstance(h, logging.StreamHandler)
                for h in logging.getLogger().handlers),
            "the screen handler must still be installed",
        )

    def test_verbose_sets_debug_level(self):
        fs.configure_logging(verbose=True,
                             log_path=self.root / "v.log")
        self.assertEqual(logging.getLogger().level, logging.DEBUG)


class TestForceCliGuard(unittest.TestCase):
    def test_force_without_enable_file_by_file_exits_2(self):
        with mock.patch.object(sys, "argv",
                               ["finish_stuck_uploads.py", "--force"]):
            with self.assertRaises(SystemExit) as ctx:
                fs.main()
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
