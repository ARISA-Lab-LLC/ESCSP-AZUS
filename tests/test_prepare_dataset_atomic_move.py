"""Unit tests for prepare_dataset.py's two-phase atomic move and the
on-disk upload-artifact stash.

A re-prep deletes the existing Staging_Area folder before rebuilding it.
The only link between that folder and its Zenodo draft is a pair of
upload-pipeline artifacts (``upload_state.json`` and
``ESID_XXX_request_log.json``); destroying them orphans the draft and
the next upload run creates a DUPLICATE record.  ``prepare_dataset.py``
therefore stashes those artifacts ON DISK before the ``rmtree`` and
restores them after the atomic move — the stash survives a crash in the
window between the two.

Covered here:

* ``_stash_upload_artifacts`` — copies exactly the artifact files to the
  stash dir and returns their names; empty/missing folders are no-ops.
* ``_restore_upload_artifacts`` — copies everything back and removes the
  stash only on FULL success; any failed copy keeps the stash on disk
  for the next run.
* The crash-window regression: a run killed between ``rmtree`` and
  restore leaves only the stash; the next run's restore must recover
  ``upload_state.json`` with the original record_id intact.
* End-to-end re-prep: a COPY of the script inside a throwaway fake
  project tree (so the real Staging_Area is never touched) is run twice;
  a fake ``upload_state.json`` written between the runs must survive the
  second run's stash/restore round trip.

Run from the project root:

    python3 -m unittest discover -s tests -v
"""

import json
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

import prepare_dataset as prep  # noqa: E402


# --- helpers (same WAV builders as test_prepare_dataset_verification) ------

def riff_header(declared_total: int) -> bytes:
    chunk = declared_total - 8
    return b"RIFF" + chunk.to_bytes(4, "little") + b"WAVE"


def write_wav(path: Path, total_size: int) -> None:
    """A well-formed WAV whose stat size and RIFF header agree."""
    path.write_bytes(riff_header(total_size) + b"\x00" * (total_size - 12))


_STATE_NAME = "upload_state.json"
_LOG_NAME = "ESID_005_request_log.json"
_STATE_PAYLOAD = {"record_id": "424242"}


def make_staging_with_artifacts(root: Path) -> Path:
    """A staging folder holding both upload artifacts plus a decoy file."""
    folder = root / "ESID_005_Staging"
    folder.mkdir()
    (folder / _STATE_NAME).write_text(
        json.dumps(_STATE_PAYLOAD), encoding="utf-8"
    )
    (folder / _LOG_NAME).write_text(
        json.dumps({"response": {"id": "424242"}}), encoding="utf-8"
    )
    (folder / "README.md").write_text("decoy — not an artifact", encoding="utf-8")
    return folder


class TestStashUploadArtifacts(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.stash = self.root / ".ESID_005_Staging.artifact_stash"

    def tearDown(self):
        self._tmp.cleanup()

    def test_stashes_state_and_request_log_on_disk(self):
        folder = make_staging_with_artifacts(self.root)

        stashed = prep._stash_upload_artifacts(folder, self.stash)

        self.assertEqual(sorted(stashed), sorted([_STATE_NAME, _LOG_NAME]))
        # The copies must actually be ON DISK (that is the whole point:
        # an in-memory stash would die with a crashed process).
        stashed_names = sorted(p.name for p in self.stash.iterdir())
        self.assertEqual(stashed_names, sorted([_STATE_NAME, _LOG_NAME]))
        self.assertEqual(
            json.loads((self.stash / _STATE_NAME).read_text(encoding="utf-8")),
            _STATE_PAYLOAD,
        )
        # Non-artifact files stay out of the stash.
        self.assertFalse((self.stash / "README.md").exists())

    def test_empty_folder_returns_empty_list(self):
        folder = self.root / "ESID_005_Staging"
        folder.mkdir()

        self.assertEqual(prep._stash_upload_artifacts(folder, self.stash), [])
        # No artifacts -> the stash dir is never created, so no stale
        # empty stash can confuse a later run.
        self.assertFalse(self.stash.exists())

    def test_missing_folder_returns_empty_list(self):
        folder = self.root / "does_not_exist"

        self.assertEqual(prep._stash_upload_artifacts(folder, self.stash), [])
        self.assertFalse(self.stash.exists())


class TestRestoreUploadArtifacts(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.stash = self.root / ".ESID_005_Staging.artifact_stash"
        self.fresh = self.root / "ESID_005_Staging"
        self.fresh.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _make_stash(self) -> None:
        self.stash.mkdir()
        (self.stash / _STATE_NAME).write_text(
            json.dumps(_STATE_PAYLOAD), encoding="utf-8"
        )
        (self.stash / _LOG_NAME).write_text(
            json.dumps({"response": {"id": "424242"}}), encoding="utf-8"
        )

    def test_full_restore_copies_back_and_removes_stash(self):
        self._make_stash()

        prep._restore_upload_artifacts(self.fresh, self.stash)

        self.assertEqual(
            json.loads((self.fresh / _STATE_NAME).read_text(encoding="utf-8")),
            _STATE_PAYLOAD,
        )
        self.assertTrue((self.fresh / _LOG_NAME).exists())
        # Everything restored cleanly -> the stash must be gone.
        self.assertFalse(self.stash.exists())

    def test_failed_restore_keeps_stash_on_disk(self):
        """A copy failure must NOT consume the stash — it is the only
        remaining link to the Zenodo draft, so it stays for the next run."""
        self._make_stash()

        with mock.patch.object(
            prep.shutil, "copy2", side_effect=OSError("disk full")
        ):
            with self.assertLogs("azus.prepare", level="WARNING"):
                prep._restore_upload_artifacts(self.fresh, self.stash)

        self.assertTrue(self.stash.is_dir())
        self.assertEqual(
            json.loads((self.stash / _STATE_NAME).read_text(encoding="utf-8")),
            _STATE_PAYLOAD,
        )
        self.assertFalse((self.fresh / _STATE_NAME).exists())

    def test_retry_after_failed_restore_succeeds(self):
        """The kept stash must be consumable by a later, healthy run."""
        self._make_stash()
        with mock.patch.object(
            prep.shutil, "copy2", side_effect=OSError("disk full")
        ):
            with self.assertLogs("azus.prepare", level="WARNING"):
                prep._restore_upload_artifacts(self.fresh, self.stash)

        prep._restore_upload_artifacts(self.fresh, self.stash)

        self.assertEqual(
            json.loads((self.fresh / _STATE_NAME).read_text(encoding="utf-8")),
            _STATE_PAYLOAD,
        )
        self.assertFalse(self.stash.exists())

    def test_missing_stash_is_a_noop(self):
        prep._restore_upload_artifacts(self.fresh, self.stash)

        self.assertEqual(list(self.fresh.iterdir()), [])
        self.assertFalse(self.stash.exists())


class TestCrashWindowRecovery(unittest.TestCase):
    def test_stale_stash_from_killed_run_recovers_record_id(self):
        """THE CRASH-WINDOW REGRESSION.

        Simulates a re-prep killed between the ``shutil.rmtree`` of the
        old staging folder and the artifact restore: at that moment the
        staging folder is GONE and only the on-disk stash exists.  The
        next run prepares a fresh folder and calls
        ``_restore_upload_artifacts`` on it — the record_id linking the
        folder to its Zenodo draft must come back intact.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stash = root / ".ESID_005_Staging.artifact_stash"
            stash.mkdir()
            (stash / _STATE_NAME).write_text(
                json.dumps(_STATE_PAYLOAD), encoding="utf-8"
            )
            # Deliberately NO staging folder on disk — that is the
            # crash-window state.

            fresh = root / "ESID_005_Staging"
            fresh.mkdir()  # the next run's freshly moved folder
            prep._restore_upload_artifacts(fresh, stash)

            recovered = json.loads(
                (fresh / _STATE_NAME).read_text(encoding="utf-8")
            )
            self.assertEqual(recovered.get("record_id"), "424242")
            self.assertFalse(stash.exists())


class TestEndToEndReprep(unittest.TestCase):
    """Full re-prep through the real CLI, twice, in a fake project tree.

    Hermetic: ``azus_common.STAGING_AREA`` is derived from the module's
    own file location, so running a COPY of the scripts inside a
    throwaway fake project confines the atomic move (and every other
    side effect) to that tree — the real Staging_Area/, Uploaded_Data/,
    and Records/ are never touched, and no network is involved
    (prepare_dataset.py performs no Zenodo calls).
    """

    def _build_fake_project(self, tmp: Path) -> Path:
        fake_root = tmp / "fake_project"
        fake_resources = fake_root / "Resources"
        fake_resources.mkdir(parents=True)
        # The code under test plus its two sibling imports.
        for module in (
            "prepare_dataset.py",
            "audit_wav_integrity.py",
            "azus_common.py",
        ):
            shutil.copy2(
                _PROJECT_ROOT / "Resources" / module,
                fake_resources / module,
            )
        # Real data files the prep loads: the README template (required)
        # and the resource files list (required).  License.txt is one of
        # the companion files that list names; the other companions are
        # deliberately absent — copy_resource_files only WARNS about
        # missing companions, so prep still succeeds.
        for data_file in (
            "README_template.html",
            "resource_files_list.csv",
            "License.txt",
        ):
            shutil.copy2(
                _PROJECT_ROOT / "Resources" / data_file,
                fake_resources / data_file,
            )
        (fake_root / "Staging_Area").mkdir()
        return fake_root

    def _make_raw_esid(self, tmp: Path) -> Path:
        source = tmp / "Raw_Data" / "ESID_005"
        source.mkdir(parents=True)
        write_wav(source / "20240408_120000.WAV", 4000)
        write_wav(source / "20240408_120001.WAV", 6000)
        (source / "CONFIG.TXT").write_text("gain: medium\n", encoding="utf-8")
        return source

    def _make_collectors_csv(self, tmp: Path) -> Path:
        collectors = tmp / "collectors.csv"
        collectors.write_text(
            "ESID,Eclipse Date,Local Eclipse Type,Latitude,Longitude\n"
            "005,2024-04-08,Total,35.08,-106.65\n",
            encoding="utf-8",
        )
        return collectors

    def _run_prep(self, fake_root: Path, source: Path, collectors: Path,
                  *extra: str):
        return subprocess.run(
            [
                sys.executable,
                str(fake_root / "Resources" / "prepare_dataset.py"),
                str(source),
                "--collector-csv", str(collectors),
                *extra,
            ],
            capture_output=True, text=True, cwd=fake_root,
        )

    def test_reprep_round_trips_upload_state_through_the_stash(self):
        """The legacy (--single-zip) layout end to end, twice."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_root = self._build_fake_project(root)
            source = self._make_raw_esid(root)
            collectors = self._make_collectors_csv(root)

            staging_area = fake_root / "Staging_Area"
            staged = staging_area / "ESID_005_Staging"
            partial = staging_area / ".ESID_005_Staging.partial"
            stash = staging_area / ".ESID_005_Staging.artifact_stash"
            build_dir = source.parent / "ESID_005_Staging"

            # --- First prep: raw folder -> completed staging folder ---
            result = self._run_prep(fake_root, source, collectors,
                                    "--single-zip")
            self.assertEqual(
                result.returncode, 0, result.stdout + result.stderr
            )
            self.assertTrue(staged.is_dir())
            self.assertTrue((staged / ".prep_complete").exists())
            for produced in (
                "ESID_005.zip",
                "ESID_005_to_upload.csv",
                "README.html",
                "README.md",
                "file_list.csv",
                "total_eclipse_data.csv",
                "License.txt",
            ):
                self.assertTrue(
                    (staged / produced).exists(), f"missing {produced}"
                )
            # The atomic move consumed the build dir and left no debris.
            self.assertFalse(build_dir.exists())
            self.assertFalse(partial.exists())
            self.assertFalse(stash.exists())

            # --- Link the folder to a (fake) Zenodo draft ---
            (staged / _STATE_NAME).write_text(
                json.dumps(_STATE_PAYLOAD), encoding="utf-8"
            )
            (staged / _LOG_NAME).write_text(
                json.dumps({"response": {"id": "424242"}}), encoding="utf-8"
            )

            # --- Second prep (re-prep): rmtree + rebuild + move ---
            result = self._run_prep(fake_root, source, collectors,
                                    "--single-zip")
            self.assertEqual(
                result.returncode, 0, result.stdout + result.stderr
            )
            self.assertTrue((staged / ".prep_complete").exists())

            # THE POINT: the upload artifacts survived the rmtree via
            # the on-disk stash, record_id intact.
            state = json.loads(
                (staged / _STATE_NAME).read_text(encoding="utf-8")
            )
            self.assertEqual(state.get("record_id"), "424242")
            self.assertTrue((staged / _LOG_NAME).exists())

            # Clean end state: stash consumed, no partial left behind.
            self.assertFalse(stash.exists())
            self.assertFalse(partial.exists())

            # The restored artifacts are pipeline-internal — they must
            # not leak into the public ZIP or the upload manifest (both
            # were finalized in the build dir BEFORE the restore).
            with zipfile.ZipFile(staged / "ESID_005.zip") as zf:
                self.assertNotIn(
                    f"ESID_005/{_STATE_NAME}", zf.namelist()
                )
            manifest = (staged / "ESID_005_to_upload.csv").read_text(
                encoding="utf-8"
            )
            self.assertNotIn(_STATE_NAME, manifest)

    def test_per_day_prep_moves_atomically_and_stashes_too(self):
        """The default (per-day) layout through the same atomic-move path."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_root = self._build_fake_project(root)
            source = self._make_raw_esid(root)
            # A second recording day, so the prep yields two archives.
            write_wav(source / "20240409_090000.WAV", 5000)
            collectors = self._make_collectors_csv(root)

            staging_area = fake_root / "Staging_Area"
            staged = staging_area / "ESID_005_Staging"
            partial = staging_area / ".ESID_005_Staging.partial"
            stash = staging_area / ".ESID_005_Staging.artifact_stash"

            result = self._run_prep(fake_root, source, collectors)
            self.assertEqual(
                result.returncode, 0, result.stdout + result.stderr
            )
            self.assertTrue((staged / ".prep_complete").exists())
            for produced in (
                "ESID_005_2024_04_08.zip",
                "ESID_005_2024_04_09.zip",
                "ESID_005_to_upload.csv",
                "README.html",
                "README.md",
                "file_list.csv",
                "total_eclipse_data.csv",
            ):
                self.assertTrue(
                    (staged / produced).exists(), f"missing {produced}"
                )
            # The legacy single ZIP is NOT produced in this layout.
            self.assertFalse((staged / "ESID_005.zip").exists())
            self.assertFalse(partial.exists())
            self.assertFalse(stash.exists())

            # Re-prep round-trips the draft pointer exactly as in the
            # legacy layout — the stash mechanism is layout-agnostic.
            (staged / _STATE_NAME).write_text(
                json.dumps(_STATE_PAYLOAD), encoding="utf-8"
            )
            result = self._run_prep(fake_root, source, collectors)
            self.assertEqual(
                result.returncode, 0, result.stdout + result.stderr
            )
            state = json.loads(
                (staged / _STATE_NAME).read_text(encoding="utf-8")
            )
            self.assertEqual(state.get("record_id"), "424242")
            self.assertFalse(stash.exists())


if __name__ == "__main__":
    unittest.main()
