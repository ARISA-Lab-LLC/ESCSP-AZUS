"""Unit tests for the upload-path integrity verification.

Covers the pre-upload gate ``verify_dataset_integrity`` in
standalone_tasks.py and the remote-entry verification helpers
(``_remote_entry_mismatch``, ``_calculate_md5``) in
standalone_uploader.py.  These are the checks that stop an incomplete
or corrupted ZIP from ever reaching Zenodo — or from surviving on a
draft once the local copy is fixed.

Run from the project root:

    python3 -m unittest discover -s tests -v
"""

import csv
import hashlib
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "Resources"))

import standalone_tasks as tasks  # noqa: E402
import standalone_uploader as uploader  # noqa: E402


_FILE_LIST_HEADERS = [
    "File Name", "File Type", "Description", "File size (KB)",
    "File size (Bytes)", "Associated Data Dictionary", "SHA-512 Hash",
    "Notes",
]


def _sha512(path: Path) -> str:
    return hashlib.sha512(path.read_bytes()).hexdigest()


def _write_file_list(staging: Path, rows) -> None:
    """rows: iterable of (name, size_kb, size_bytes, sha) tuples;
    size_bytes may be "" to simulate a legacy (pre-Bytes-column) row."""
    with open(staging / "file_list.csv", "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_FILE_LIST_HEADERS)
        writer.writeheader()
        for name, size_kb, size_bytes, sha in rows:
            writer.writerow({
                "File Name": name,
                "File Type": "x",
                "Description": "x",
                "File size (KB)": size_kb,
                "File size (Bytes)": size_bytes,
                "Associated Data Dictionary": "x",
                "SHA-512 Hash": sha,
                "Notes": "",
            })


def make_staging_folder(
    root: Path,
    esid: str = "005",
    wavs=None,
    sentinel: bool = True,
    bytes_column: bool = True,
) -> Path:
    """Build a valid staging folder: ZIP + matching file_list.csv + sentinel.

    ``wavs`` is a dict of {basename: content_bytes}; entries go into the
    ZIP under the ESID_XXX/ subfolder, exactly like prepare_dataset.py.
    ``bytes_column=False`` produces a legacy manifest whose
    "File size (Bytes)" cells are empty (pre-column folders).
    """
    if wavs is None:
        wavs = {
            "20240408_120000.WAV": b"\x01" * 4000,
            "20240408_121000.WAV": b"\x02" * 6000,
        }
    staging = root / f"ESID_{esid}_Staging"
    staging.mkdir()
    zip_path = staging / f"ESID_{esid}.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name, content in wavs.items():
            zf.writestr(f"ESID_{esid}/{name}", content)
    rows = [
        (
            name,
            f"{len(content) / 1024:.2f}",
            str(len(content)) if bytes_column else "",
            "unused-wav-hash",
        )
        for name, content in wavs.items()
    ]
    zip_bytes = str(zip_path.stat().st_size) if bytes_column else ""
    rows.insert(0, (zip_path.name, "0.00", zip_bytes, _sha512(zip_path)))
    _write_file_list(staging, rows)
    if sentinel:
        (staging / ".prep_complete").touch()
    return staging


class TestVerifyDatasetIntegrity(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _zip_of(self, staging: Path) -> str:
        return str(next(staging.glob("ESID_*.zip")))

    def test_clean_folder_passes(self):
        staging = make_staging_folder(self.root)
        self.assertEqual(
            tasks.verify_dataset_integrity(self._zip_of(staging)), []
        )

    def test_missing_sentinel_fails(self):
        staging = make_staging_folder(self.root, sentinel=False)
        problems = tasks.verify_dataset_integrity(self._zip_of(staging))
        self.assertTrue(any(".prep_complete" in p for p in problems))

    def test_corrupt_zip_fails(self):
        staging = make_staging_folder(self.root)
        zip_path = Path(self._zip_of(staging))
        zip_path.write_bytes(b"this is not a zip archive")
        problems = tasks.verify_dataset_integrity(str(zip_path))
        self.assertTrue(any("not a readable archive" in p for p in problems))

    def test_wav_missing_from_zip_fails(self):
        staging = make_staging_folder(self.root)
        zip_path = Path(self._zip_of(staging))
        # Rebuild the ZIP without one of the WAVs the manifest lists.
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("ESID_005/20240408_120000.WAV", b"\x01" * 4000)
        problems = tasks.verify_dataset_integrity(
            str(zip_path), verify_zip_hash=False
        )
        self.assertTrue(any("MISSING from the ZIP" in p for p in problems))

    def test_extra_wav_in_zip_fails(self):
        staging = make_staging_folder(self.root)
        zip_path = Path(self._zip_of(staging))
        with zipfile.ZipFile(zip_path, "a") as zf:
            zf.writestr("ESID_005/20240408_999999.WAV", b"\x03" * 100)
        problems = tasks.verify_dataset_integrity(
            str(zip_path), verify_zip_hash=False
        )
        self.assertTrue(any("not listed in" in p for p in problems))

    def test_size_drift_fails(self):
        staging = make_staging_folder(self.root)
        zip_path = Path(self._zip_of(staging))
        # Rebuild with one WAV truncated relative to the manifest.
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("ESID_005/20240408_120000.WAV", b"\x01" * 4000)
            zf.writestr("ESID_005/20240408_121000.WAV", b"\x02" * 100)
        problems = tasks.verify_dataset_integrity(
            str(zip_path), verify_zip_hash=False
        )
        self.assertTrue(any("differ in size" in p for p in problems))

    def test_hash_mismatch_fails_and_skip_flag_bypasses_only_hash(self):
        staging = make_staging_folder(self.root)
        zip_path = self._zip_of(staging)
        # Corrupt the recorded ZIP hash in file_list.csv.
        file_list = staging / "file_list.csv"
        with open(file_list, "r", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        rows[0]["SHA-512 Hash"] = "0" * 128
        with open(file_list, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=_FILE_LIST_HEADERS)
            writer.writeheader()
            writer.writerows(rows)

        problems = tasks.verify_dataset_integrity(zip_path)
        self.assertTrue(any("SHA-512 does not match" in p for p in problems))
        # Structural checks still pass, so skipping the hash passes overall.
        self.assertEqual(
            tasks.verify_dataset_integrity(zip_path, verify_zip_hash=False),
            [],
        )

    def test_missing_file_list_fails(self):
        staging = make_staging_folder(self.root)
        (staging / "file_list.csv").unlink()
        problems = tasks.verify_dataset_integrity(self._zip_of(staging))
        self.assertTrue(any("No file_list.csv" in p for p in problems))

    def test_file_list_without_zip_row_fails(self):
        staging = make_staging_folder(self.root)
        wav_rows = [
            ("20240408_120000.WAV", f"{4000 / 1024:.2f}", "4000", "h"),
            ("20240408_121000.WAV", f"{6000 / 1024:.2f}", "6000", "h"),
        ]
        _write_file_list(staging, wav_rows)
        problems = tasks.verify_dataset_integrity(
            self._zip_of(staging), verify_zip_hash=False
        )
        self.assertTrue(any("no row for" in p for p in problems))


class TestByteExactSizesAndDigests(unittest.TestCase):
    """Phase-4 hardening: byte-exact size checks + combined digest pass."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _zip_of(self, staging: Path) -> str:
        return str(next(staging.glob("ESID_*.zip")))

    def test_sub_kb_drift_caught_without_hash_step(self):
        """A 1-byte truncation rounds to the same 2-decimal KB, so the
        legacy KB comparison misses it; the byte-exact column must catch
        it even when --skip-integrity-hash disables the hash backstop."""
        staging = make_staging_folder(self.root)
        zip_path = Path(self._zip_of(staging))
        # Rebuild one WAV a single byte short of what the manifest says.
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("ESID_005/20240408_120000.WAV", b"\x01" * 3999)
            zf.writestr("ESID_005/20240408_121000.WAV", b"\x02" * 6000)
        problems = tasks.verify_dataset_integrity(
            str(zip_path), verify_zip_hash=False
        )
        self.assertTrue(any("differ in size" in p for p in problems))

    def test_legacy_manifest_falls_back_to_kb_and_passes_clean(self):
        staging = make_staging_folder(self.root, bytes_column=False)
        self.assertEqual(
            tasks.verify_dataset_integrity(
                self._zip_of(staging), verify_zip_hash=False
            ),
            [],
        )

    def test_digests_out_filled_only_after_verified_hash(self):
        staging = make_staging_folder(self.root)
        zip_path = self._zip_of(staging)
        digests = {}
        self.assertEqual(
            tasks.verify_dataset_integrity(zip_path, digests_out=digests), []
        )
        self.assertEqual(digests["sha512"], _sha512(Path(zip_path)))
        self.assertEqual(
            digests["md5"], hashlib.md5(Path(zip_path).read_bytes()).hexdigest()
        )

    def test_digests_out_empty_when_hash_skipped_or_mismatched(self):
        staging = make_staging_folder(self.root)
        zip_path = self._zip_of(staging)
        skipped = {}
        tasks.verify_dataset_integrity(
            zip_path, verify_zip_hash=False, digests_out=skipped
        )
        self.assertEqual(skipped, {})
        # Tamper the recorded hash: the gate must not hand back digests
        # for an archive that failed verification.
        file_list = staging / "file_list.csv"
        with open(file_list, "r", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        rows[0]["SHA-512 Hash"] = "0" * 128
        with open(file_list, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=_FILE_LIST_HEADERS)
            writer.writeheader()
            writer.writerows(rows)
        mismatched = {}
        problems = tasks.verify_dataset_integrity(
            zip_path, digests_out=mismatched
        )
        self.assertTrue(any("SHA-512 does not match" in p for p in problems))
        self.assertEqual(mismatched, {})


class TestRemoteEntryMismatch(unittest.TestCase):
    def test_size_and_checksum_match(self):
        entry = {"size": 100, "checksum": "md5:" + hashlib.md5(b"x").hexdigest()}
        self.assertIsNone(
            uploader._remote_entry_mismatch(
                entry, 100, hashlib.md5(b"x").hexdigest()
            )
        )

    def test_size_mismatch(self):
        mismatch = uploader._remote_entry_mismatch({"size": 50}, 100)
        self.assertIn("size mismatch", mismatch)

    def test_checksum_mismatch(self):
        entry = {"size": 100, "checksum": "md5:" + "0" * 32}
        mismatch = uploader._remote_entry_mismatch(entry, 100, "f" * 32)
        self.assertIn("checksum mismatch", mismatch)

    def test_size_only_check_ignores_checksum(self):
        entry = {"size": 100, "checksum": "md5:" + "0" * 32}
        self.assertIsNone(uploader._remote_entry_mismatch(entry, 100))

    def test_no_size_no_checksum_passes(self):
        self.assertIsNone(uploader._remote_entry_mismatch({}, 100, "f" * 32))

    def test_unparseable_size(self):
        mismatch = uploader._remote_entry_mismatch({"size": "huge"}, 100)
        self.assertIn("unparseable", mismatch)

    def test_string_size_is_parsed(self):
        self.assertIsNone(uploader._remote_entry_mismatch({"size": "100"}, 100))


class TestCalculateMd5(unittest.TestCase):
    def test_matches_hashlib(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.bin"
            content = b"\x00\x01\x02" * 50_000
            path.write_bytes(content)
            self.assertEqual(
                uploader._calculate_md5(str(path)),
                hashlib.md5(content).hexdigest(),
            )


if __name__ == "__main__":
    unittest.main()
