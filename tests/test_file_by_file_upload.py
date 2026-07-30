"""Unit tests for Resources/file_by_file_upload.py.

The file-by-file fallback uploads a stuck ESID's individual WAVs (from
Raw_Data) + CONFIG.TXT + the standalone companions to the EXISTING Zenodo
draft instead of the ZIP, and publishes/submits ONLY once the complete
required set is committed and the ZIP is gone.  These tests mock the
Zenodo API entirely (no network) and drive the safety gates the plan's
adversarial review demanded (R1, R2, R4/404, R5).

Run from the project root:

    python3 -m unittest discover -s tests -v
"""

import csv
import hashlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from requests.exceptions import RequestException

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "Resources"))

import azus_common  # noqa: E402
import file_by_file_upload as fbf  # noqa: E402
from prepare_dataset import (  # noqa: E402
    _FILE_LIST_HEADERS,
    _stash_upload_artifacts,
    create_upload_manifest,
)

_ESID = "007"
_ZIP = f"ESID_{_ESID}.zip"
_RECORD = "555"
_COMPANIONS = ["README.md", "total_eclipse_data.csv", "file_list.csv"]
_WAVS = {
    "20240408_120000.WAV": b"AUDIO-ONE-" * 50,
    "20240408_130000.WAV": b"AUDIO-TWO-" * 40,
}
_CONFIG = b"GAIN=medium\nSAMPLE_RATE=48000\n"

_UNSET = object()  # sentinel so _patch_api(draft=None) can mean a real 404


def _sha(data: bytes) -> str:
    return hashlib.sha512(data).hexdigest()


def _flrow(name, ftype, data):
    return {
        "File Name": name, "File Type": ftype,
        "Description": "x",
        "File size (KB)": f"{len(data) / 1024:.2f}",
        "File size (Bytes)": str(len(data)),
        "Associated Data Dictionary": "N/A",
        "SHA-512 Hash": _sha(data), "Notes": "",
    }


def _committed(names):
    """Fake list_draft_files() output: every name committed."""
    return [{"key": n, "status": "completed"} for n in names]


class _FixtureCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.staging = self.root / f"ESID_{_ESID}_Staging"
        self.raw = self.root / f"ESID#{_ESID}"
        self.staging.mkdir()
        self.raw.mkdir()

    def build(self, *, corrupt_wav=None, drop_wav=None):
        """Populate staging + raw for ESID 007.  Returns the set of names
        expected on a complete record."""
        # Raw files (bytes as recorded in file_list, unless corrupted).
        for name, data in _WAVS.items():
            out = data
            if name == corrupt_wav:
                out = data + b"TAMPERED"
            if name != drop_wav:
                (self.raw / name).write_bytes(out)
        (self.raw / "CONFIG.TXT").write_bytes(_CONFIG)

        # Companions in staging.
        (self.staging / "README.md").write_text("# ESID 007\n")
        (self.staging / "total_eclipse_data.csv").write_text("ESID\n007\n")

        # file_list.csv (external): ZIP row + companions + CONFIG + WAVs.
        rows = [
            _flrow(_ZIP, "ZIP Archive (.zip)", b"zipbytes"),
            _flrow("README.md", "Markdown (.md)", b"# ESID 007\n"),
            _flrow("total_eclipse_data.csv", "CSV", b"ESID\n007\n"),
            _flrow("CONFIG.TXT", "Plain Text (.txt)", _CONFIG),
        ]
        for name, data in _WAVS.items():
            rows.append(_flrow(name, "Waveform Audio File Format (.WAV)", data))
        # file_list.csv also lists itself as a companion row (name only used).
        with open(self.staging / "file_list.csv", "w", encoding="utf-8",
                  newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=_FILE_LIST_HEADERS)
            w.writeheader()
            w.writerows(rows)

        # Manifest: ZIP + companions (the standalone upload set).
        with open(self.staging / f"ESID_{_ESID}_to_upload.csv", "w",
                  encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["File Name", "File Size (KB)", "Notes"])
            w.writeheader()
            for name in [_ZIP] + _COMPANIONS:
                w.writerow({"File Name": name, "File Size (KB)": "1", "Notes": ""})

        # State file with a record_id.
        (self.staging / "upload_state.json").write_text(
            json.dumps({"record_id": _RECORD, "number_of_tries": 3})
        )
        return set(_COMPANIONS) | set(_WAVS) | {"CONFIG.TXT"}

    def _patch_api(self, *, draft=_UNSET, list_side_effect=None,
                   upload_result=None):
        """Patch every Zenodo API function fbf imported. Returns the dict
        of mocks.  Pass ``draft=None`` to simulate a 404."""
        if draft is _UNSET:
            draft = {"id": _RECORD}
        if upload_result is None:
            upload_result = {"successful": True, "api_response": {}, "error": None}
        patchers = {
            "get_draft_record": mock.patch.object(
                fbf, "get_draft_record", return_value=draft),
            "list_draft_files": mock.patch.object(
                fbf, "list_draft_files",
                side_effect=list_side_effect if list_side_effect is not None
                else [[], _committed(self._all_names)]),
            "delete_draft_file": mock.patch.object(fbf, "delete_draft_file"),
            "upload_to_zenodo": mock.patch.object(
                fbf, "upload_to_zenodo", return_value=upload_result),
            "publish_draft": mock.patch.object(fbf, "publish_draft"),
            "submit_to_community_review": mock.patch.object(
                fbf, "submit_to_community_review"),
            "ensure_doi_reserved": mock.patch.object(fbf, "ensure_doi_reserved"),
            "archive_new_version_staging": mock.patch.object(
                fbf, "archive_new_version_staging"),
        }
        started = {k: p.start() for k, p in patchers.items()}
        for p in patchers.values():
            self.addCleanup(p.stop)
        return started

    def _run(self, *, community_id="COMM", reserve_doi=False,
             auto_publish=False, upload_attempts=3):
        return fbf.run_file_by_file(
            esid=_ESID, staging_dir=self.staging, raw_dir=self.raw,
            record_id=_RECORD, credentials=mock.Mock(),
            community_id=community_id, reserve_doi=reserve_doi,
            auto_publish=auto_publish, upload_attempts=upload_attempts,
        )


class TestRequiredFiles(_FixtureCase):
    def test_derivation(self):
        self.build()
        raw_files, companions = fbf.required_files(self.staging, _ESID)
        names = {n for n, _ in raw_files}
        self.assertEqual(names, set(_WAVS) | {"CONFIG.TXT"})
        # every raw row carries a hash
        self.assertTrue(all(h for _, h in raw_files))
        self.assertEqual(set(companions), set(_COMPANIONS))
        self.assertNotIn(_ZIP, companions)


class TestRequiredFilesIsIdempotent(_FixtureCase):
    """Re-deriving the set from an ALREADY-REWRITTEN manifest must match.

    Step 6 rewrites ESID_NNN_to_upload.csv to list the whole file-by-file
    set — companions AND raw files.  Until July 2026 required_files read
    every name in that manifest back as a COMPANION, so a second run looked
    for the WAVs in the staging folder and aborted with "required file(s)
    not found locally".  That made every resume impossible: a conversion
    interrupted part-way could never be finished, and the RESUMABLE case is
    the whole point of a restartable batch.
    """

    def test_second_derivation_matches_the_first(self):
        self.build()
        first = fbf.required_files(self.staging, _ESID)

        # Exactly what step 6 writes.
        raw_files, companions = first
        fbf.rewrite_manifest_file_by_file(
            self.staging, _ESID,
            [(n, "companion") for n in companions]
            + [(n, "raw (Raw_Data)") for n, _ in raw_files],
        )
        second = fbf.required_files(self.staging, _ESID)
        self.assertEqual(second, first)

    def test_raw_names_never_become_companions(self):
        self.build()
        fbf.rewrite_manifest_file_by_file(
            self.staging, _ESID,
            [("README.md", "companion"),
             ("20240408_120000.WAV", "raw (Raw_Data)"),
             ("CONFIG.TXT", "raw (Raw_Data)")],
        )
        _raw, companions = fbf.required_files(self.staging, _ESID)
        self.assertEqual(companions, ["README.md"])

    def test_a_second_full_run_succeeds(self):
        """End to end: convert, then convert again on the same folder."""
        self._all_names = list(self.build())
        api = self._patch_api(list_side_effect=[
            [], _committed(self._all_names),          # run 1
            _committed(self._all_names),              # run 2 (ZIP gone)
            _committed(self._all_names),
        ])
        self.assertTrue(self._run())
        self.assertTrue(self._run())
        self.assertEqual(api["upload_to_zenodo"].call_count, 2)
        # The second run offered the same file set, not a shrunken one.
        first, second = api["upload_to_zenodo"].call_args_list
        self.assertEqual(
            {Path(p).name for p in second.kwargs["files"]},
            {Path(p).name for p in first.kwargs["files"]},
        )


class TestHappyPath(_FixtureCase):
    def test_uploads_the_full_set_and_leaves_a_draft(self):
        """auto_publish=False must mean DRAFT, even with a community_id.

        Until July 2026 the publish step tested `if community_id:` FIRST, so
        a truthy community_id (the production default) submitted every
        completed record for review regardless of auto_publish — and a
        manager's accept publishes permanently.
        """
        self._all_names = list(self.build())
        api = self._patch_api()
        ok = self._run(community_id="COMM", reserve_doi=True)
        self.assertTrue(ok)

        # Uploaded the full set (companions from staging + WAVs/CONFIG from raw).
        api["upload_to_zenodo"].assert_called_once()
        sent = {Path(p).name for p in api["upload_to_zenodo"].call_args.kwargs["files"]}
        self.assertEqual(sent, set(self._all_names))
        # publish OFF on the upload call (this module owns the gate).
        self.assertFalse(api["upload_to_zenodo"].call_args.kwargs["auto_publish"])
        self.assertFalse(api["upload_to_zenodo"].call_args.kwargs["submit_review"])
        # upload_attempts applies to EVERY file: no archive on this path.
        self.assertIsNone(
            api["upload_to_zenodo"].call_args.kwargs["priority_files"])

        # THE CONTRACT: nothing left draft state, and the folder stayed put
        # so the recovery tools can still see it.
        api["submit_to_community_review"].assert_not_called()
        api["publish_draft"].assert_not_called()
        api["archive_new_version_staging"].assert_not_called()
        api["delete_draft_file"].assert_not_called()

    def test_submits_to_community_only_when_auto_publish_is_on(self):
        self._all_names = list(self.build())
        api = self._patch_api()
        self.assertTrue(self._run(community_id="COMM", auto_publish=True))
        api["submit_to_community_review"].assert_called_once()
        api["publish_draft"].assert_not_called()
        api["archive_new_version_staging"].assert_called_once()

    def test_forwards_upload_attempts_to_every_file(self):
        self._all_names = list(self.build())
        api = self._patch_api()
        self.assertTrue(self._run(upload_attempts=2))
        kwargs = api["upload_to_zenodo"].call_args.kwargs
        self.assertEqual(kwargs["upload_attempts"], 2)
        self.assertIsNone(kwargs["priority_files"])

        # file_list.csv rewritten without the ZIP row; mode marked.
        rows = list(csv.DictReader(
            io.StringIO((self.staging / "file_list.csv").read_text())))
        self.assertFalse(any(r["File Name"] == _ZIP for r in rows))
        state = json.loads((self.staging / "upload_state.json").read_text())
        self.assertEqual(state["mode"], "file_by_file")

    def test_auto_publish_without_community(self):
        self._all_names = list(self.build())
        api = self._patch_api()
        ok = self._run(community_id=None, auto_publish=True)
        self.assertTrue(ok)
        api["publish_draft"].assert_called_once()
        api["submit_to_community_review"].assert_not_called()


class TestZipRemoval(_FixtureCase):
    def test_incomplete_zip_slot_is_cleared(self):
        self._all_names = list(self.build())
        # A PENDING (failed/partial) ZIP slot present; cleared, then complete.
        api = self._patch_api(list_side_effect=[
            [{"key": _ZIP, "status": "pending"}],   # step 5: NOT committed
            [],                                      # re-list after delete
            _committed(self._all_names),             # completeness gate
        ])
        ok = self._run()
        self.assertTrue(ok)
        api["delete_draft_file"].assert_called_once()
        self.assertEqual(api["delete_draft_file"].call_args.args[2], _ZIP)

    def test_committed_zip_aborts_without_deleting(self):
        """A successfully-uploaded (committed) ZIP must NOT be deleted, and the
        ESID must NOT switch to file-by-file — the whole point is that
        file-by-file only engages when the ZIP keeps FAILING."""
        self._all_names = list(self.build())
        api = self._patch_api(list_side_effect=[
            [{"key": _ZIP, "status": "completed"}],  # the ZIP SUCCEEDED
        ])
        ok = self._run()
        self.assertFalse(ok)
        api["delete_draft_file"].assert_not_called()      # good ZIP untouched
        api["upload_to_zenodo"].assert_not_called()        # no file-by-file
        api["submit_to_community_review"].assert_not_called()
        api["publish_draft"].assert_not_called()
        api["archive_new_version_staging"].assert_not_called()
        # Mode was NOT marked — the ESID stays recoverable as ZIP.
        state = json.loads((self.staging / "upload_state.json").read_text())
        self.assertNotIn("mode", state)


class TestManifestArchives(_FixtureCase):
    """The manifest rewrite must leave a legible history of both attempts."""

    def _archives(self):
        return (
            self.staging
            / azus_common.MANIFEST_ARCHIVE_ZIP_ATTEMPT.format(esid=_ESID),
            self.staging
            / azus_common.MANIFEST_ARCHIVE_FILE_BY_FILE.format(esid=_ESID),
        )

    def test_run_archives_both_manifests(self):
        self._all_names = list(self.build())
        manifest = self.staging / f"ESID_{_ESID}_to_upload.csv"
        original = manifest.read_text()
        self._patch_api()
        self.assertTrue(self._run())

        zip_attempt, fbf_copy = self._archives()
        # The ZIP-attempt snapshot is the manifest exactly as it was.
        self.assertEqual(zip_attempt.read_text(), original)
        self.assertIn(_ZIP, zip_attempt.read_text())
        # The mirror matches the rewritten live manifest, which drops the
        # ZIP and gains the individual raw files.
        live = manifest.read_text()
        self.assertEqual(fbf_copy.read_text(), live)
        self.assertNotIn(_ZIP, live)
        names = {r["File Name"] for r in csv.DictReader(io.StringIO(live))}
        self.assertTrue(set(_WAVS).issubset(names))

    def test_archives_are_never_uploaded(self):
        self._all_names = list(self.build())
        api = self._patch_api()
        self.assertTrue(self._run())
        sent = {Path(p).name
                for p in api["upload_to_zenodo"].call_args.kwargs["files"]}
        for archive in self._archives():
            self.assertNotIn(archive.name, sent)

    def test_zip_attempt_snapshot_is_write_once(self):
        """A re-run must not replace the ZIP-attempt history with a copy of
        the already-rewritten (file-by-file) manifest."""
        self.build()
        zip_attempt, _mirror = self._archives()
        fbf.rewrite_manifest_file_by_file(
            self.staging, _ESID, [("README.md", "companion")])
        first = zip_attempt.read_text()
        self.assertIn(_ZIP, first)   # captured the real ZIP-attempt manifest
        # Second rewrite: the live manifest no longer carries a ZIP row.
        fbf.rewrite_manifest_file_by_file(
            self.staging, _ESID, [("CONFIG.TXT", "raw (Raw_Data)")])
        self.assertEqual(zip_attempt.read_text(), first)

    def test_no_snapshot_when_manifest_absent(self):
        """Nothing to snapshot → no stray empty archive; mirror still written."""
        fbf.rewrite_manifest_file_by_file(
            self.staging, _ESID, [("README.md", "companion")])
        zip_attempt, mirror = self._archives()
        self.assertFalse(zip_attempt.exists())
        self.assertTrue(mirror.exists())


class TestArchivesExcludedFromPrep(_FixtureCase):
    """A re-prep must neither upload the archives nor delete them."""

    def test_create_upload_manifest_excludes_the_archives(self):
        self.build()
        zip_attempt = (self.staging
                       / azus_common.MANIFEST_ARCHIVE_ZIP_ATTEMPT.format(esid=_ESID))
        mirror = (self.staging
                  / azus_common.MANIFEST_ARCHIVE_FILE_BY_FILE.format(esid=_ESID))
        zip_attempt.write_text("File Name,Notes\n")
        mirror.write_text("File Name,Notes\n")

        manifest_path = create_upload_manifest(self.staging, _ESID)
        listed = {
            r["File Name"]
            for r in csv.DictReader(io.StringIO(manifest_path.read_text()))
        }
        self.assertNotIn(zip_attempt.name, listed)
        self.assertNotIn(mirror.name, listed)
        # Real dataset content is still listed.
        self.assertIn("README.md", listed)

    def test_archives_survive_a_reprep(self):
        """They match _UPLOAD_ARTIFACT_PATTERNS, so the re-prep stash keeps
        them instead of rmtree-ing the history away."""
        self.build()
        for name in (
            azus_common.MANIFEST_ARCHIVE_ZIP_ATTEMPT.format(esid=_ESID),
            azus_common.MANIFEST_ARCHIVE_FILE_BY_FILE.format(esid=_ESID),
        ):
            (self.staging / name).write_text("File Name,Notes\n")
        stash = self.root / ".stash"
        stashed = _stash_upload_artifacts(self.staging, stash)
        self.assertIn(
            azus_common.MANIFEST_ARCHIVE_ZIP_ATTEMPT.format(esid=_ESID), stashed)
        self.assertIn(
            azus_common.MANIFEST_ARCHIVE_FILE_BY_FILE.format(esid=_ESID), stashed)


class TestFileCountCeiling(_FixtureCase):
    """Zenodo caps a record at 100 files; refuse before the one-way door."""

    def _oversize(self, total_wavs):
        """Add file_list.csv rows for `total_wavs` WAVs that exist on disk."""
        rows = [_flrow(_ZIP, "ZIP Archive (.zip)", b"z")]
        for name in _COMPANIONS:
            rows.append(_flrow(name, "CSV", b"x"))
        rows.append(_flrow("CONFIG.TXT", "Plain Text (.txt)", _CONFIG))
        (self.raw / "CONFIG.TXT").write_bytes(_CONFIG)
        for i in range(total_wavs):
            name = f"20240408_{i:06d}.WAV"
            data = b"A" * 16
            (self.raw / name).write_bytes(data)
            rows.append(_flrow(name, "Waveform Audio File Format (.WAV)", data))
        for name in ("README.md", "total_eclipse_data.csv"):
            (self.staging / name).write_text("x")
        with open(self.staging / "file_list.csv", "w", encoding="utf-8",
                  newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=_FILE_LIST_HEADERS)
            w.writeheader()
            w.writerows(rows)
        with open(self.staging / f"ESID_{_ESID}_to_upload.csv", "w",
                  encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["File Name", "Notes"])
            w.writeheader()
            for name in [_ZIP] + _COMPANIONS:
                w.writerow({"File Name": name, "Notes": ""})
        (self.staging / "upload_state.json").write_text(
            json.dumps({"record_id": _RECORD})
        )

    def test_refuses_over_the_limit_before_the_point_of_no_return(self):
        # 3 companions + CONFIG.TXT + 97 WAVs = 101 files.
        self._oversize(97)
        self._all_names = []
        api = self._patch_api()
        with mock.patch.object(
            fbf, "mark_file_by_file_mode",
            side_effect=AssertionError("must refuse BEFORE the mode marker"),
        ):
            self.assertFalse(self._run())
        api["upload_to_zenodo"].assert_not_called()
        api["delete_draft_file"].assert_not_called()
        # Refused before any network call at all.
        api["get_draft_record"].assert_not_called()

    def test_exactly_at_the_limit_is_allowed(self):
        # 3 companions + CONFIG.TXT + 96 WAVs = 100 files.
        self._oversize(96)
        names = [f"20240408_{i:06d}.WAV" for i in range(96)]
        self._all_names = _COMPANIONS + ["CONFIG.TXT"] + names
        api = self._patch_api()
        self.assertTrue(self._run())
        api["upload_to_zenodo"].assert_called_once()

    def test_the_limit_matches_the_documented_zenodo_cap(self):
        self.assertEqual(fbf._ZENODO_MAX_FILES_PER_RECORD, 100)


class TestDraftEndpointFailure(_FixtureCase):
    """A broken /draft must not block the repair it is a symptom of."""

    def test_5xx_on_get_draft_proceeds(self):
        """ESID 797: a pending ZIP slot 500s /draft — the exact state this
        module exists to clear."""
        self._all_names = list(self.build())
        api = self._patch_api()
        api["get_draft_record"].side_effect = RequestException("HTTP 500")
        self.assertTrue(self._run())
        api["upload_to_zenodo"].assert_called_once()

    def test_true_404_still_aborts(self):
        """The invariant at risk when loosening the None check: a genuinely
        absent draft must never fall through to fresh creation."""
        self._all_names = list(self.build())
        api = self._patch_api(draft=None)
        with mock.patch.object(
            fbf, "mark_file_by_file_mode",
            side_effect=AssertionError("a 404 must abort before the marker"),
        ):
            self.assertFalse(self._run())
        api["upload_to_zenodo"].assert_not_called()

    def test_file_list_failing_too_aborts(self):
        self._all_names = list(self.build())
        api = self._patch_api(list_side_effect=RequestException("HTTP 500"))
        api["get_draft_record"].side_effect = RequestException("HTTP 500")
        self.assertFalse(self._run())
        api["upload_to_zenodo"].assert_not_called()


class TestArchiveRefusesToClobber(_FixtureCase):
    """The previous version's archive is never deleted."""

    def test_refuses_when_the_destination_exists(self):
        uploaded = self.root / "Uploaded_Data"
        destination = uploaded / f"ESID_{_ESID}_Uploaded"
        destination.mkdir(parents=True)
        (destination / "evidence.txt").write_text("do not delete me\n")
        with mock.patch.object(azus_common, "UPLOADED_DATA", uploaded):
            self.assertIsNone(
                fbf.archive_new_version_staging(self.staging, _ESID, "[t]")
            )
        self.assertTrue(self.staging.is_dir(), "staging must stay put")
        self.assertEqual(
            (destination / "evidence.txt").read_text(), "do not delete me\n"
        )

    def test_moves_when_the_destination_is_free(self):
        uploaded = self.root / "Uploaded_Data"
        (self.staging / "marker.txt").write_text("x")
        with mock.patch.object(azus_common, "UPLOADED_DATA", uploaded):
            destination = fbf.archive_new_version_staging(
                self.staging, _ESID, "[t]"
            )
        self.assertEqual(destination, uploaded / f"ESID_{_ESID}_Uploaded")
        self.assertFalse(self.staging.exists())
        self.assertTrue((destination / "marker.txt").is_file())


class TestSafetyGates(_FixtureCase):
    def test_draft_404_aborts_before_upload(self):
        self._all_names = list(self.build())
        api = self._patch_api(draft=None)  # get_draft_record → None
        self.assertFalse(self._run())
        api["upload_to_zenodo"].assert_not_called()
        api["submit_to_community_review"].assert_not_called()

    def test_sha512_mismatch_aborts(self):
        self._all_names = list(self.build(corrupt_wav="20240408_120000.WAV"))
        api = self._patch_api()
        self.assertFalse(self._run())
        # Aborted at SHA pre-check — before even touching Zenodo.
        api["get_draft_record"].assert_not_called()
        api["upload_to_zenodo"].assert_not_called()

    def test_missing_raw_file_aborts(self):
        self._all_names = list(self.build(drop_wav="20240408_130000.WAV"))
        api = self._patch_api()
        self.assertFalse(self._run())
        api["upload_to_zenodo"].assert_not_called()

    def test_incomplete_record_blocks_publish(self):
        self._all_names = list(self.build())
        # Upload "succeeds" but the record is missing one WAV afterwards.
        short = _committed([n for n in self._all_names
                            if n != "20240408_120000.WAV"])
        api = self._patch_api(list_side_effect=[[], short])
        self.assertFalse(self._run())
        api["submit_to_community_review"].assert_not_called()
        api["publish_draft"].assert_not_called()
        api["archive_new_version_staging"].assert_not_called()

    def test_upload_failure_blocks_publish(self):
        self._all_names = list(self.build())
        api = self._patch_api(
            upload_result={"successful": False,
                           "error": {"error_message": "timeout"}})
        self.assertFalse(self._run())
        api["submit_to_community_review"].assert_not_called()
        api["archive_new_version_staging"].assert_not_called()


class TestOnlyZipMissing(_FixtureCase):
    def test_true_when_companions_committed_and_no_zip(self):
        self.build()
        with mock.patch.object(fbf, "list_draft_files",
                               return_value=_committed(_COMPANIONS)):
            self.assertTrue(
                fbf.only_zip_missing(mock.Mock(), _RECORD, self.staging, _ESID))

    def test_false_when_a_companion_missing(self):
        self.build()
        with mock.patch.object(fbf, "list_draft_files",
                               return_value=_committed(_COMPANIONS[:-1])):
            self.assertFalse(
                fbf.only_zip_missing(mock.Mock(), _RECORD, self.staging, _ESID))

    def test_false_when_zip_already_committed(self):
        self.build()
        with mock.patch.object(fbf, "list_draft_files",
                               return_value=_committed(_COMPANIONS + [_ZIP])):
            self.assertFalse(
                fbf.only_zip_missing(mock.Mock(), _RECORD, self.staging, _ESID))


if __name__ == "__main__":
    unittest.main()
