"""Unit tests for prepare_dataset.py's pre-sentinel safeguards.

Covers ``verify_zip_against_source`` (the finished ZIP must match a
fresh scan of the raw folder before the move into Staging_Area/ and the
``.prep_complete`` sentinel) and the refusal to build a staging folder
in place inside Staging_Area/.

Run from the project root:

    python3 -m unittest discover -s tests -v
"""

import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "Resources"))

import prepare_dataset as prep  # noqa: E402

_TOOL = _PROJECT_ROOT / "Resources" / "prepare_dataset.py"


# --- helpers (same WAV builders as test_audit_wav_integrity) --------------

def riff_header(declared_total: int) -> bytes:
    chunk = declared_total - 8
    return b"RIFF" + chunk.to_bytes(4, "little") + b"WAVE"


def write_wav(path: Path, total_size: int) -> None:
    """A well-formed WAV whose stat size and RIFF header agree."""
    path.write_bytes(riff_header(total_size) + b"\x00" * (total_size - 12))


def write_truncated_wav(path: Path, declared: int, actual: int) -> None:
    """Header declares `declared` bytes; file is only `actual` bytes."""
    path.write_bytes(riff_header(declared) + b"\x00" * (actual - 12))


def make_source_dir(root: Path, wav_sizes=(4000, 6000)) -> Path:
    source = root / "ESID_005"
    source.mkdir()
    for i, size in enumerate(wav_sizes):
        write_wav(source / f"20240408_12000{i}.WAV", size)
    return source


def zip_from_source(source: Path, out_dir: Path) -> Path:
    """Zip the source WAVs under the ESID_005/ prefix, like prep does."""
    zip_path = out_dir / "ESID_005.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for wav in sorted(source.glob("*.WAV")):
            zf.write(wav, f"ESID_005/{wav.name}")
    return zip_path


class TestVerifyZipAgainstSource(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.out = self.root / "out"
        self.out.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_round_trip_passes(self):
        source = make_source_dir(self.root)
        zip_path = zip_from_source(source, self.out)
        self.assertEqual(prep.verify_zip_against_source(zip_path, source), [])

    def test_wav_added_after_zipping_fails(self):
        """The late-sync case: a WAV appears on disk after the ZIP was
        built — the fresh disk scan must catch it."""
        source = make_source_dir(self.root)
        zip_path = zip_from_source(source, self.out)
        write_wav(source / "20240408_130000.WAV", 5000)
        problems = prep.verify_zip_against_source(zip_path, source)
        self.assertTrue(any("missing from ZIP" in p for p in problems))

    def test_wav_missing_from_zip_fails(self):
        source = make_source_dir(self.root)
        zip_path = self.out / "ESID_005.zip"
        wavs = sorted(source.glob("*.WAV"))
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.write(wavs[0], f"ESID_005/{wavs[0].name}")  # omit the rest
        problems = prep.verify_zip_against_source(zip_path, source)
        self.assertTrue(any("missing from ZIP" in p for p in problems))

    def test_size_drift_fails(self):
        source = make_source_dir(self.root)
        zip_path = zip_from_source(source, self.out)
        # Shrink a source WAV after zipping (still header-consistent).
        write_wav(source / "20240408_120000.WAV", 2000)
        problems = prep.verify_zip_against_source(zip_path, source)
        self.assertTrue(any("differ in size" in p for p in problems))

    def test_truncated_source_wav_fails(self):
        source = make_source_dir(self.root)
        write_truncated_wav(source / "20240408_140000.WAV", 9000, 3000)
        zip_path = zip_from_source(source, self.out)
        problems = prep.verify_zip_against_source(zip_path, source)
        self.assertTrue(
            any("cross-check" in p for p in problems), problems
        )

    def test_zero_byte_source_wav_is_included_not_failed(self):
        """A genuinely empty WAV (dead recorder) belongs in the dataset:
        it ships in the ZIP as 0 bytes and verification only warns.

        Uses the REAL writer (create_zip_file, DEFLATED entries): an
        empty deflated entry has a 2-byte compressed stream, which the
        ZIP-side classifier must accept as genuinely empty.
        """
        source = make_source_dir(self.root)
        (source / "20240408_150000.WAV").write_bytes(b"")
        zip_path, _ = prep.create_zip_file(source, self.out, "005")
        with self.assertLogs("azus.prepare", level="WARNING") as captured:
            problems = prep.verify_zip_against_source(zip_path, source)
        self.assertEqual(problems, [])
        self.assertTrue(
            any("zero bytes" in m for m in captured.output), captured.output
        )
        with zipfile.ZipFile(zip_path) as zf:
            entry = [i for i in zf.infolist()
                     if i.filename.endswith("20240408_150000.WAV")]
            self.assertEqual(len(entry), 1)
            self.assertEqual(entry[0].file_size, 0)
            self.assertEqual(entry[0].compress_type, zipfile.ZIP_DEFLATED)

    def test_zero_byte_wav_missing_from_zip_still_fails(self):
        """Allowing zero-byte WAVs must not weaken the presence check:
        an empty WAV that never made it into the ZIP is still fatal."""
        source = make_source_dir(self.root)
        zip_path = zip_from_source(source, self.out)
        (source / "20240408_150000.WAV").write_bytes(b"")  # added AFTER zip
        problems = prep.verify_zip_against_source(zip_path, source)
        self.assertTrue(problems, "missing empty WAV must fail verification")

    def test_empty_source_folder_fails(self):
        source = self.root / "ESID_006"
        source.mkdir()
        zip_path = self.out / "ESID_006.zip"
        with zipfile.ZipFile(zip_path, "w"):
            pass
        problems = prep.verify_zip_against_source(zip_path, source)
        self.assertTrue(any("No WAV files found" in p for p in problems))

    def test_corrupt_zip_fails(self):
        source = make_source_dir(self.root)
        zip_path = self.out / "ESID_005.zip"
        zip_path.write_bytes(b"garbage, not an archive")
        problems = prep.verify_zip_against_source(zip_path, source)
        self.assertTrue(any("not a readable archive" in p for p in problems))

    def test_sidecar_files_ignored(self):
        source = make_source_dir(self.root)
        (source / "._20240408_120000.WAV").write_bytes(b"\x00\x05\x16\x07")
        zip_path = zip_from_source(source, self.out)
        self.assertEqual(prep.verify_zip_against_source(zip_path, source), [])


class TestPre1980Timestamps(unittest.TestCase):
    """Regression tests for 'ZIP does not support timestamps before 1980'.

    AudioMoth clocks reset to the 1970 Unix epoch on power loss, so raw
    WAVs can arrive with pre-1980 mtimes (rsync preserves them).  Both
    ZIP writers must clamp such timestamps instead of raising.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.out = self.root / "out"
        self.out.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_create_zip_file_accepts_1970_mtime_wav(self):
        source = make_source_dir(self.root)
        epoch_wav = source / "19700101_000000.WAV"
        write_wav(epoch_wav, 3000)
        os.utime(epoch_wav, (0, 0))  # mtime = 1970-01-01 (Unix epoch)

        # Used to raise ValueError from ZipInfo; must now succeed.
        zip_path, content_hashes = prep.create_zip_file(
            source, self.out, "005"
        )

        self.assertIn(epoch_wav.name, content_hashes)
        with zipfile.ZipFile(zip_path) as zf:
            info = zf.getinfo(f"ESID_005/{epoch_wav.name}")
            # strict_timestamps=False clamps to the ZIP epoch, 1980-01-01.
            self.assertGreaterEqual(info.date_time, (1980, 1, 1, 0, 0, 0))
        # The clamped entry must still pass the pre-sentinel verification.
        self.assertEqual(prep.verify_zip_against_source(zip_path, source), [])

    def test_create_zip_file_accepts_1970_mtime_config(self):
        source = make_source_dir(self.root)
        config = source / "CONFIG.TXT"
        config.write_text("gain: medium\n", encoding="utf-8")
        os.utime(config, (0, 0))

        zip_path, content_hashes = prep.create_zip_file(
            source, self.out, "005"
        )
        self.assertIn("CONFIG.TXT", content_hashes)
        with zipfile.ZipFile(zip_path) as zf:
            self.assertIn("ESID_005/CONFIG.TXT", zf.namelist())

    def test_source_mtime_clamped_metadata_only(self):
        """A pre-1980 source file gets its FILESYSTEM mtime clamped to
        the ZIP epoch — the filename and contents must be untouched."""
        source = make_source_dir(self.root)
        epoch_wav = source / "19700101_000000.WAV"
        write_wav(epoch_wav, 3000)
        os.utime(epoch_wav, (0, 0))
        original_bytes = epoch_wav.read_bytes()

        prep.create_zip_file(source, self.out, "005")

        self.assertEqual(
            epoch_wav.stat().st_mtime, prep._ZIP_MIN_MTIME,
            "source mtime must be clamped to 1980-01-01",
        )
        self.assertTrue(epoch_wav.exists(), "filename must not change")
        self.assertEqual(
            epoch_wav.read_bytes(), original_bytes,
            "file contents must not change",
        )

    def test_add_files_to_zip_accepts_1970_mtime_file(self):
        source = make_source_dir(self.root)
        zip_path, _ = prep.create_zip_file(source, self.out, "005")
        stale_meta = self.out / "file_list.csv"
        stale_meta.write_text("File Name\n", encoding="utf-8")
        os.utime(stale_meta, (0, 0))

        appended = prep.add_files_to_zip(zip_path, self.out, "005")

        self.assertEqual(appended, 1)
        with zipfile.ZipFile(zip_path) as zf:
            self.assertIn("ESID_005/file_list.csv", zf.namelist())


class TestInPlaceBuildRefusal(unittest.TestCase):
    def test_output_dir_inside_staging_area_exits_nonzero(self):
        """--output-dir pointing into Staging_Area/ must be refused
        before any work happens (and before anything is created there).

        Hermetic: the tool derives its project root (and therefore
        Staging_Area/) from its own file location, so we run a COPY of
        the script inside a throwaway fake project tree — the test can
        never touch the real repo's Staging_Area, and a real staging
        folder on disk can never affect the test.
        """
        import shutil as _shutil

        with tempfile.TemporaryDirectory() as tmp:
            fake_root = Path(tmp) / "fake_project"
            fake_resources = fake_root / "Resources"
            fake_resources.mkdir(parents=True)
            for module in (
                "prepare_dataset.py",
                "audit_wav_integrity.py",
                "azus_common.py",
            ):
                _shutil.copy2(
                    _PROJECT_ROOT / "Resources" / module,
                    fake_resources / module,
                )
            (fake_root / "Staging_Area").mkdir()

            source = make_source_dir(Path(tmp))
            collectors = Path(tmp) / "collectors.csv"
            collectors.write_text("ESID\n005\n", encoding="utf-8")
            target = fake_root / "Staging_Area" / "ESID_005_Staging"

            result = subprocess.run(
                [
                    sys.executable,
                    str(fake_resources / "prepare_dataset.py"),
                    str(source),
                    "--collector-csv", str(collectors),
                    "--output-dir", str(target),
                ],
                capture_output=True, text=True, cwd=fake_root,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "Refusing to build in place",
                result.stdout + result.stderr,
            )
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
