"""finish_stuck_uploads.py against the per-day ZIP layout.

Most of this tool was already layout-agnostic: `discover_stuck_esids` keys
on `upload_state.json` and `run_recovery` shells out to
`standalone_tasks.py --esid …`, which handles either layout.  What was not
agnostic was a **dead end**.

A per-day folder carrying `mode == file_by_file` could be finished by no
path at all: the ZIP pipeline skipped it (Requirement 9) and the
file-by-file fallback refused it (`refuses_per_day_layout`, because the
fallback replaces one whole-site archive and cannot apply to N day
archives).  The tool then advised re-running with `--enable-file-by-file`
— the very path that refuses.

The rule that resolves it lives in
`azus_common.file_by_file_mode_blocks_zip_path`: the marker suppresses the
ZIP path only when the layout is NOT per-day.  A per-day folder's marker is
STALE — nothing else is contending for its record — so the ZIP path owns
it.  Crucially the marker is *reinterpreted*, never rewritten:
`upload_state.json` is the anti-duplicate link between a folder and its
draft and is left byte-identical.

Requirement 9 itself is unchanged for the single-archive layout, and
`tests/test_req9_skip.py` plus `tests/test_finish_stuck_file_by_file.py`
still pin that with no edits.  Case 3 below re-asserts it from this side.

Run from the project root:

    python3 -m unittest discover -s tests -v
"""

import json
import sys
import tempfile
import unittest
import zipfile
from argparse import Namespace
from pathlib import Path
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "Resources"))

import azus_common  # noqa: E402
import file_by_file_upload as fbf  # noqa: E402
import finish_stuck_uploads as fs  # noqa: E402
import prepare_dataset as prep  # noqa: E402
import standalone_tasks as tasks  # noqa: E402
import standalone_uploader as up  # noqa: E402

_ESID = "007"
_DAYS = ("2024_04_08", "2024_04_09")


class _Staging(unittest.TestCase):
    """A staging area where either layout can be built for one ESID."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.staging_root = self.root / "Staging_Area"
        self.staging_root.mkdir()
        self.raw_root = self.root / "Raw_Data"
        self.raw_root.mkdir()
        (self.raw_root / f"ESID#{_ESID}").mkdir()

    def _folder(self, esid=_ESID):
        folder = self.staging_root / f"ESID_{esid}_Staging"
        folder.mkdir(exist_ok=True)
        return folder

    def per_day(self, esid=_ESID, *, mode=None, tries=3):
        folder = self._folder(esid)
        for day in _DAYS:
            zip_path = folder / azus_common.day_zip_name(esid, day)
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr(f"{zip_path.stem}/CONFIG.TXT", "gain\n")
        return self._write_state(folder, mode, tries)

    def legacy(self, esid=_ESID, *, mode=None, tries=3):
        folder = self._folder(esid)
        with zipfile.ZipFile(folder / f"ESID_{esid}.zip", "w") as zf:
            zf.writestr(f"ESID_{esid}/CONFIG.TXT", "gain\n")
        return self._write_state(folder, mode, tries)

    def _write_state(self, folder, mode, tries):
        state = {"record_id": "555", "number_of_tries": tries}
        if mode:
            state["mode"] = mode
        (folder / azus_common.STATE_FILENAME).write_text(json.dumps(state))
        return folder

    def state_of(self, folder):
        return json.loads(
            (folder / azus_common.STATE_FILENAME).read_text(encoding="utf-8")
        )


class TestTheStaleMarkerRule(_Staging):
    """azus_common.file_by_file_mode_blocks_zip_path — the shared rule."""

    def test_per_day_marker_is_stale_so_the_zip_path_owns_it(self):
        folder = self.per_day(mode=azus_common.FILE_BY_FILE_MODE)
        self.assertEqual(
            azus_common.staging_layout(folder, _ESID), prep.ZIP_MODE_PER_DAY
        )
        self.assertFalse(
            azus_common.file_by_file_mode_blocks_zip_path(folder, _ESID)
        )

    def test_single_archive_marker_still_blocks(self):
        folder = self.legacy(mode=azus_common.FILE_BY_FILE_MODE)
        self.assertTrue(
            azus_common.file_by_file_mode_blocks_zip_path(folder, _ESID)
        )

    def test_no_marker_never_blocks_in_either_layout(self):
        self.assertFalse(
            azus_common.file_by_file_mode_blocks_zip_path(
                self.per_day(), _ESID)
        )
        self.assertFalse(
            azus_common.file_by_file_mode_blocks_zip_path(
                self.legacy(esid="008"), "008")
        )

    def test_an_archiveless_folder_still_blocks(self):
        """Indeterminate layout must keep the marker in force — only a
        positively-identified per-day folder flips the decision."""
        folder = self._write_state(
            self._folder(), azus_common.FILE_BY_FILE_MODE, 3)
        self.assertIsNone(azus_common.staging_layout(folder, _ESID))
        self.assertTrue(
            azus_common.file_by_file_mode_blocks_zip_path(folder, _ESID)
        )

    def test_the_marker_is_never_rewritten(self):
        """upload_state.json is the anti-duplicate link; reinterpreting a
        stale marker must not touch it."""
        folder = self.per_day(mode=azus_common.FILE_BY_FILE_MODE)
        before = (folder / azus_common.STATE_FILENAME).read_bytes()
        azus_common.file_by_file_mode_blocks_zip_path(folder, _ESID)
        self.assertEqual(
            (folder / azus_common.STATE_FILENAME).read_bytes(), before
        )
        self.assertEqual(
            self.state_of(folder)["mode"], azus_common.FILE_BY_FILE_MODE
        )


class TestDeadlockIsGoneEndToEnd(_Staging):
    """The ZIP pipeline must actually pick the folder up now."""

    def test_discovery_no_longer_skips_a_per_day_folder(self):
        self.per_day(mode=azus_common.FILE_BY_FILE_MODE)
        collectors = self.root / "collectors.csv"
        collectors.write_text("ESID\n")
        with mock.patch.object(
            tasks, "create_upload_data",
            return_value=([mock.Mock(esid=_ESID)], []),
        ) as build:
            tasks.get_upload_data(
                data_dir=str(self.staging_root),
                data_collectors_file=str(collectors),
                dataset_category="Total",
                failure_results_file=str(self.root / "failed.csv"),
                tracker=mock.MagicMock(is_uploaded=lambda p: False),
                project_config={"default_required_files": []},
            )
        build.assert_called_once()
        (esid, folder, archives), = build.call_args.kwargs[
            "esid_folder_archives"]
        self.assertEqual(esid, _ESID)
        self.assertEqual(len(archives), len(_DAYS))

    def test_discovery_still_skips_a_single_archive_folder(self):
        """Requirement 9 intact for the layout it was written for."""
        self.legacy(mode=azus_common.FILE_BY_FILE_MODE)
        collectors = self.root / "collectors.csv"
        collectors.write_text("ESID\n")
        result = tasks.get_upload_data(
            data_dir=str(self.staging_root),
            data_collectors_file=str(collectors),
            dataset_category="Total",
            failure_results_file=str(self.root / "failed.csv"),
            tracker=mock.MagicMock(is_uploaded=lambda p: False),
            project_config={"default_required_files": []},
        )
        self.assertEqual([d.esid for d in result], [])
        failed = self.root / "failed.csv"
        self.assertNotIn(
            _ESID, failed.read_text() if failed.exists() else ""
        )


class TestPhaseRouting(_Staging):
    """_run_with_file_by_file must route a per-day folder to Phase A."""

    def _args(self, *, force=False):
        return Namespace(
            config="Resources/config.json", workers=1, upload_attempts=3,
            skip_date_check=False, tries_threshold=3,
            raw_data_dir=str(self.raw_root),
            force=force, skip_integrity_hash=False,
        )

    def _run(self, folder, *, force=False, only_zip=False):
        stuck = [((7, ""), _ESID, folder, "555")]
        with mock.patch.object(fs, "run_recovery", return_value=0) as recovery, \
             mock.patch.object(fs, "_load_publish_config",
                               return_value=("COMM", False, False)), \
             mock.patch.object(azus_common, "find_esid_folders",
                               return_value=[((7, ""), _ESID,
                                              self.raw_root / f"ESID#{_ESID}")]), \
             mock.patch.object(up, "get_credentials_from_env",
                               return_value=mock.Mock()), \
             mock.patch.object(up, "_read_number_of_tries", return_value=3), \
             mock.patch.object(fbf, "only_zip_missing",
                               return_value=only_zip) as only_zip_mock, \
             mock.patch.object(fbf, "run_file_by_file",
                               return_value=True) as run_fbf:
            code = fs._run_with_file_by_file(stuck, self._args(force=force))
        return code, run_fbf, recovery, only_zip_mock

    def test_per_day_folder_with_stale_marker_goes_to_phase_a(self):
        folder = self.per_day(mode=azus_common.FILE_BY_FILE_MODE)
        _code, run_fbf, recovery, _ = self._run(folder)
        recovery.assert_called_once()
        self.assertEqual(
            recovery.call_args.kwargs["stuck_esids"], [_ESID],
            "the per-day folder must be handed to the ZIP shell-out",
        )
        run_fbf.assert_not_called()

    def test_per_day_folder_is_never_switched_to_file_by_file(self):
        folder = self.per_day(mode=azus_common.FILE_BY_FILE_MODE)
        _code, run_fbf, _rec, _ = self._run(folder, only_zip=True)
        run_fbf.assert_not_called()

    def test_marked_single_archive_folder_still_continues_file_by_file(self):
        folder = self.legacy(mode=azus_common.FILE_BY_FILE_MODE)
        _code, run_fbf, recovery, _ = self._run(folder)
        run_fbf.assert_called_once()
        recovery.assert_not_called()

    def test_force_does_not_switch_a_per_day_folder(self):
        folder = self.per_day()
        _code, run_fbf, recovery, _ = self._run(
            folder, force=True, only_zip=False)
        run_fbf.assert_not_called()
        recovery.assert_called_once()


class TestMessagesNameTheRealReason(_Staging):
    """only_zip_missing returns a bare False for two unrelated causes;
    reporting the wrong one sends the operator hunting."""

    def test_per_day_reason_names_the_layout_not_a_companion(self):
        folder = self.per_day()
        reason = fs._not_switchable_reason(folder, _ESID)
        self.assertIn("per_day", reason)
        self.assertNotIn("companion", reason)

    def test_single_archive_reason_keeps_the_companion_explanation(self):
        folder = self.legacy()
        reason = fs._not_switchable_reason(folder, _ESID)
        self.assertIn("companion", reason)

    def test_archiveless_folder_keeps_the_companion_explanation(self):
        folder = self._folder()
        self.assertIn("companion", fs._not_switchable_reason(folder, _ESID))


class TestShellOutFlags(_Staging):
    """--skip-existing-records must never be forwarded: every ESID here has
    a draft by definition, so forwarding it would skip them all."""

    def _cmd(self, **kw):
        with mock.patch.object(fs.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0)
            fs.run_recovery(
                stuck_esids=[_ESID], config_path="c.json", workers=1, **kw
            )
        return run.call_args.args[0]

    def test_skip_existing_records_is_not_forwarded(self):
        self.assertNotIn("--skip-existing-records", self._cmd())

    def test_esid_and_attempts_are_forwarded(self):
        cmd = self._cmd(upload_attempts=7)
        self.assertIn("--esid", cmd)
        self.assertIn(_ESID, cmd)
        self.assertIn("--upload-attempts", cmd)
        self.assertIn("7", cmd)

    def test_optional_flags_only_when_requested(self):
        self.assertNotIn("--skip-integrity-hash", self._cmd())
        self.assertIn(
            "--skip-integrity-hash", self._cmd(skip_integrity_hash=True)
        )


class TestListingShowsLayout(_Staging):
    """An operator sweeping a mixed staging area needs to see which folders
    are which, and which markers are stale."""

    def _listing(self):
        self.assertTrue(True)
        with mock.patch.object(fs, "_STAGING_AREA", self.staging_root):
            stuck, _excluded = fs.discover_stuck_esids()
        lines = []
        for _sort, padded, folder, _rec in stuck:
            layout = azus_common.staging_layout(folder, padded) or "no archive"
            stale = (
                azus_common.read_upload_mode(folder)
                == azus_common.FILE_BY_FILE_MODE
                and not azus_common.file_by_file_mode_blocks_zip_path(
                    folder, padded)
            )
            lines.append((padded, layout, stale))
        return lines

    def test_both_layouts_are_discovered_and_labelled(self):
        self.per_day(esid="007", mode=azus_common.FILE_BY_FILE_MODE)
        self.legacy(esid="008")
        rows = {esid: (layout, stale) for esid, layout, stale in self._listing()}
        self.assertEqual(rows["007"], (prep.ZIP_MODE_PER_DAY, True))
        self.assertEqual(rows["008"], (prep.ZIP_MODE_SINGLE, False))

    def test_discovery_is_layout_agnostic(self):
        """Discovery keys on upload_state.json only — both layouts appear."""
        self.per_day(esid="007")
        self.legacy(esid="008")
        self.assertEqual(
            sorted(esid for esid, _l, _s in self._listing()), ["007", "008"]
        )


if __name__ == "__main__":
    unittest.main()
