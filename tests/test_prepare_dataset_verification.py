"""Unit tests for prepare_dataset.py's pre-sentinel safeguards.

Covers ``verify_zip_against_source`` (the finished ZIP must match a
fresh scan of the raw folder before the move into Staging_Area/ and the
``.prep_complete`` sentinel) and the refusal to build a staging folder
in place inside Staging_Area/.

Run from the project root:

    python3 -m unittest discover -s tests -v
"""

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

    def test_zero_byte_source_wav_fails(self):
        source = make_source_dir(self.root)
        (source / "20240408_150000.WAV").write_bytes(b"")
        zip_path = zip_from_source(source, self.out)
        problems = prep.verify_zip_against_source(zip_path, source)
        self.assertTrue(any("zero bytes" in p for p in problems), problems)

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


class TestInPlaceBuildRefusal(unittest.TestCase):
    def test_output_dir_inside_staging_area_exits_nonzero(self):
        """--output-dir pointing into Staging_Area/ must be refused
        before any work happens (and before anything is created there)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = make_source_dir(root)
            collectors = root / "collectors.csv"
            collectors.write_text("ESID\n005\n", encoding="utf-8")
            target = _PROJECT_ROOT / "Staging_Area" / "ESID_005_Staging"
            result = subprocess.run(
                [
                    sys.executable, str(_TOOL), str(source),
                    "--collector-csv", str(collectors),
                    "--output-dir", str(target),
                ],
                capture_output=True, text=True, cwd=_PROJECT_ROOT,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "Refusing to build in place",
                result.stdout + result.stderr,
            )
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
