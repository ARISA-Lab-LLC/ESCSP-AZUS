"""The recovery tools must refuse a per-day folder loudly, not misbehave.

Per-day support for the recovery tools is a later phase.  Until it lands,
each tool that still speaks the single-archive vocabulary must decline a
per-day folder — and none of them may mutate anything on the way out.

The load-bearing case is ``file_by_file_upload``.  Its predicate
:func:`only_zip_missing_from_entries` documents itself as failing OPEN, and
under a per-day layout the failure is not merely "the fallback never
fires": :func:`required_files` classifies a day archive as a *companion*
(it is neither ``ESID_NNN.zip`` nor a raw WAV name), so once every day
archive is committed the companion test passes vacuously and the predicate
returns ``ESID_NNN.zip not in committed`` — True for a name a per-day
record never had.  A complete, healthy record would read as "only the ZIP
is missing" and authorise the one-way switch to file-by-file.  These tests
pin the guard that stops it.

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

import azus_common  # noqa: E402
import file_by_file_upload as fbf  # noqa: E402
import prepare_dataset as prep  # noqa: E402

_ESID = "064"
_DAYS = ("2024_04_08", "2024_04_09")


class _Folders(unittest.TestCase):
    """A per-day staging folder and a legacy one, side by side."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _folder(self, name):
        folder = self.root / name
        folder.mkdir()
        (folder / azus_common.PREP_SENTINEL).touch()
        return folder

    def per_day(self):
        folder = self._folder(f"ESID_{_ESID}_Staging")
        for day in _DAYS:
            zip_path = folder / azus_common.day_zip_name(_ESID, day)
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr(f"{zip_path.stem}/CONFIG.TXT", "gain\n")
        return folder

    def legacy(self):
        folder = self._folder(f"ESID_{_ESID}_Legacy")
        with zipfile.ZipFile(folder / f"ESID_{_ESID}.zip", "w") as zf:
            zf.writestr(f"ESID_{_ESID}/CONFIG.TXT", "gain\n")
        return folder


class TestFileByFileRefusesPerDay(_Folders):

    def test_the_layout_guard_fires_for_per_day_only(self):
        self.assertTrue(fbf.refuses_per_day_layout(self.per_day(), _ESID))
        self.assertFalse(fbf.refuses_per_day_layout(self.legacy(), _ESID))

    def test_an_archiveless_folder_is_left_to_the_normal_gates(self):
        """No archive is not this guard's business — the tool's existing
        manifest gates report it with a better message."""
        self.assertFalse(
            fbf.refuses_per_day_layout(self._folder("ESID_064_Empty"), _ESID)
        )

    def test_only_zip_missing_is_false_for_per_day_without_any_network(self):
        """The fail-open path: a per-day record whose archives are all
        committed must never read as 'only the ZIP is missing'."""
        folder = self.per_day()
        with mock.patch.object(
            fbf, "list_draft_files",
            side_effect=AssertionError("must not list a draft"),
        ):
            self.assertFalse(
                fbf.only_zip_missing(
                    credentials=mock.MagicMock(), record_id="rec1",
                    staging_dir=folder, esid=_ESID,
                )
            )

    def test_run_file_by_file_refuses_before_touching_anything(self):
        folder = self.per_day()
        before = sorted(p.name for p in folder.iterdir())
        with mock.patch.object(
            fbf, "list_draft_files",
            side_effect=AssertionError("must not list a draft"),
        ):
            self.assertFalse(
                fbf.run_file_by_file(
                    esid=_ESID, staging_dir=folder,
                    raw_dir=self.root / "raw", record_id="rec1",
                    credentials=mock.MagicMock(),
                )
            )
        self.assertEqual(sorted(p.name for p in folder.iterdir()), before)


class TestNewVersionRefusesPerDay(_Folders):
    """A SINGLE-DAY per-day folder holds exactly one archive, so the tool's
    existing count check (`len(zips) != 1`) passes it — the layout check is
    what actually stops it."""

    def test_single_day_folder_is_not_mistaken_for_the_legacy_layout(self):
        folder = self._folder(f"ESID_{_ESID}_OneDay")
        zip_path = folder / azus_common.day_zip_name(_ESID, "2024_04_08")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(f"{zip_path.stem}/CONFIG.TXT", "gain\n")
        self.assertEqual(len(sorted(folder.glob("ESID_*.zip"))), 1)
        self.assertEqual(
            prep.staging_zip_mode(folder, _ESID), prep.ZIP_MODE_PER_DAY
        )


class TestFinishZipOnlyDraftsRefusesPerDay(_Folders):

    def test_the_verdict_exists_and_has_a_recommended_action(self):
        import finish_zip_only_drafts as fzod
        self.assertIn(
            fzod.UNSUPPORTED_ZIP_LAYOUT, fzod._RECOMMENDED_ACTION,
            "every verdict must tell the operator what to do next",
        )
        self.assertIn(
            "standalone_tasks.py",
            fzod._RECOMMENDED_ACTION[fzod.UNSUPPORTED_ZIP_LAYOUT],
            "the action must point at the tool that DOES handle per-day",
        )


class TestRefreshReadmeReportsTheRealReason(_Folders):
    """The behaviour (skip) was already right; the reason was not."""

    def test_per_day_folder_is_skipped_naming_the_layout(self):
        import refresh_readme

        staging_root = self.root / "Staging_Area"
        staging_root.mkdir()
        folder = staging_root / f"ESID_{_ESID}_Staging"
        folder.mkdir()
        for day in _DAYS:
            zip_path = folder / azus_common.day_zip_name(_ESID, day)
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr(f"{zip_path.stem}/CONFIG.TXT", "gain\n")
        # A stale README: without the layout check this folder reported
        # "no ZIP", which is untrue and sends the operator hunting.
        (folder / "README.md").write_text("# old template", encoding="utf-8")

        _stale, skipped = refresh_readme.scan_staging(staging_root)
        reasons = [reason for _esid, _folder, reason in skipped]
        self.assertTrue(
            any("per_day" in r for r in reasons),
            f"expected the layout named in the reason, got {reasons}",
        )
        self.assertFalse(
            any("no ZIP" in r for r in reasons),
            f"per-day folders have archives; 'no ZIP' is wrong: {reasons}",
        )


if __name__ == "__main__":
    unittest.main()
