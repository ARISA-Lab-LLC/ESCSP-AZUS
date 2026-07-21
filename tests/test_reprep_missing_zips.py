"""Unit tests for Resources/reprep_missing_zips.py.

The wrapper finds ESID sites with NO completed ZIP (no staging sentinel,
not uploaded) whose raw folders hold zero-byte or pre-1980-mtime WAVs —
the two conditions that used to block ``prepare_dataset.py`` — and
re-runs the preparation on exactly those sites.

These tests prove: the symptom scan (zero-byte, pre-1980, sidecar
skipping), the selection logic (already-prepared sites skipped;
incomplete-for-other-reasons sites surfaced but not selected), and that
the re-prep subprocess is invoked once per selected site with the right
arguments.

Run from the project root:

    python3 -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "Resources"))

import prep_all_datasets  # noqa: E402
import reprep_missing_zips as reprep  # noqa: E402

_EPOCH = (0, 0)  # atime/mtime pair for 1970-01-01


class _RawTreeTestCase(unittest.TestCase):
    """Fixture: a tmp raw-data tree plus tmp Staging/Uploaded areas."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.raw = self.root / "Raw_Data"
        self.raw.mkdir()
        self.staging = self.root / "Staging_Area"
        self.uploaded = self.root / "Uploaded_Data"
        self.staging.mkdir()
        self.uploaded.mkdir()
        # already_prepared() reads these module globals.
        for name, value in (("_STAGING_AREA", self.staging),
                            ("_UPLOADED_DATA", self.uploaded)):
            patcher = mock.patch.object(prep_all_datasets, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_site(self, esid, wavs):
        """Create raw folder ESID#<esid> with the given (name, size,
        epoch_mtime) WAV specs."""
        folder = self.raw / f"ESID#{esid}"
        folder.mkdir()
        for name, size, epoch_mtime in wavs:
            path = folder / name
            path.write_bytes(b"\x00" * size)
            if epoch_mtime:
                os.utime(path, _EPOCH)
        return folder

    def mark_prepared(self, esid):
        staging = self.staging / f"ESID_{esid}_Staging"
        staging.mkdir()
        (staging / prep_all_datasets._PREP_SENTINEL).write_text("")


class TestFindBlockingWavs(_RawTreeTestCase):
    def test_zero_byte_and_pre_1980_are_detected(self):
        folder = self.make_site("001", [
            ("20240408_120000.WAV", 4000, False),   # healthy
            ("20240408_130000.WAV", 0, False),      # zero-byte
            ("19700101_000000.WAV", 3000, True),    # pre-1980 mtime
        ])
        zero, pre = reprep.find_blocking_wavs(folder)
        self.assertEqual(zero, ["20240408_130000.WAV"])
        self.assertEqual(pre, ["19700101_000000.WAV"])

    def test_zero_byte_with_epoch_mtime_appears_in_both_lists(self):
        folder = self.make_site("001", [("19700101_000000.WAV", 0, True)])
        zero, pre = reprep.find_blocking_wavs(folder)
        self.assertEqual(zero, pre)
        self.assertEqual(zero, ["19700101_000000.WAV"])

    def test_sidecars_and_non_wavs_ignored(self):
        folder = self.make_site("001", [("._20240408.WAV", 0, True)])
        (folder / "CONFIG.TXT").write_bytes(b"")
        zero, pre = reprep.find_blocking_wavs(folder)
        self.assertEqual((zero, pre), ([], []))

    def test_healthy_folder_reports_nothing(self):
        folder = self.make_site("001", [("20240408_120000.WAV", 4000, False)])
        self.assertEqual(reprep.find_blocking_wavs(folder), ([], []))


class TestDiscoverBlockedSites(_RawTreeTestCase):
    def test_selection_and_other_incomplete_split(self):
        self.make_site("001", [("20240408_130000.WAV", 0, False)])   # blocked
        self.make_site("002", [("19700101_000000.WAV", 3000, True)]) # blocked
        self.make_site("003", [("20240408_120000.WAV", 4000, False)])# other
        selected, other = reprep.discover_blocked_sites(self.raw)
        self.assertEqual([s[0] for s in selected], ["001", "002"])
        self.assertEqual(other, ["003"])

    def test_already_prepared_sites_are_skipped(self):
        self.make_site("001", [("20240408_130000.WAV", 0, False)])
        self.mark_prepared("001")
        selected, other = reprep.discover_blocked_sites(self.raw)
        self.assertEqual(selected, [])
        self.assertEqual(other, [])

    def test_uploaded_sites_are_skipped(self):
        self.make_site("001", [("20240408_130000.WAV", 0, False)])
        (self.uploaded / "ESID_001_Uploaded").mkdir()
        selected, _ = reprep.discover_blocked_sites(self.raw)
        self.assertEqual(selected, [])

    def test_staging_without_sentinel_is_still_selected(self):
        """An interrupted prep (folder but no sentinel) must be redone."""
        self.make_site("001", [("20240408_130000.WAV", 0, False)])
        (self.staging / "ESID_001_Staging").mkdir()  # no sentinel
        selected, _ = reprep.discover_blocked_sites(self.raw)
        self.assertEqual([s[0] for s in selected], ["001"])


class TestMainRunsSelectedSites(_RawTreeTestCase):
    def _run_main(self, argv):
        with mock.patch.object(reprep, "run_prepare_dataset",
                               return_value=0) as run, \
             mock.patch.object(sys, "argv", ["reprep_missing_zips.py",
                                             str(self.raw), *argv]):
            with self.assertRaises(SystemExit) as ctx:
                reprep.main()
        return ctx.exception.code, run

    def test_reprocesses_each_selected_site_once(self):
        f1 = self.make_site("001", [("20240408_130000.WAV", 0, False)])
        self.make_site("003", [("20240408_120000.WAV", 4000, False)])
        code, run = self._run_main(["--eclipse-type", "partial"])
        self.assertEqual(code, 0)
        run.assert_called_once_with(f1, "Resources/config.json", "partial")

    def test_list_only_runs_nothing(self):
        self.make_site("001", [("20240408_130000.WAV", 0, False)])
        code, run = self._run_main(["--list-only"])
        self.assertEqual(code, 0)
        run.assert_not_called()

    def test_failed_reprep_exits_1(self):
        self.make_site("001", [("20240408_130000.WAV", 0, False)])
        with mock.patch.object(reprep, "run_prepare_dataset",
                               return_value=1), \
             mock.patch.object(sys, "argv",
                               ["reprep_missing_zips.py", str(self.raw)]):
            with self.assertRaises(SystemExit) as ctx:
                reprep.main()
        self.assertEqual(ctx.exception.code, 1)

    def test_missing_raw_dir_exits_2(self):
        with mock.patch.object(sys, "argv",
                               ["reprep_missing_zips.py",
                                str(self.root / "nope")]):
            with self.assertRaises(SystemExit) as ctx:
                reprep.main()
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
