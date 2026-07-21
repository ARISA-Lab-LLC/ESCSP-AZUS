"""Unit tests for Resources/esid_wav_inventory.py.

Proves the strict folder matching (ONLY exact ``ESID#NNN``), the WAV
counting/sizing rules (top-level only, sidecars excluded, exact bytes),
and the filename-timestamp column (valid ``YYYYMMDD`` names at or above
the threshold year; hex names and unset-clock 1970 names excluded).

Run from the project root:

    python3 -m unittest discover -s tests -v
"""

import csv
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "Resources"))

import esid_wav_inventory as m  # noqa: E402

_TOOL = _PROJECT_ROOT / "Resources" / "esid_wav_inventory.py"


def wav(folder: Path, name: str, size: int) -> None:
    (folder / name).write_bytes(b"\x00" * size)


class _TmpTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)


class TestExactFolderMatching(_TmpTestCase):
    def test_only_exact_hash_syntax_matches(self):
        for name in (
            "ESID#073",            # match
            "ESID#004",            # match
            "ESID#120A",           # suffixed ESID — match (real ESID)
            "ESID#122_Part_1_of_2",  # suffixed ESID — match
            "ESID_073",            # underscore variant — excluded
            "ESID_073_Staging",    # staging folder — excluded
            "ESID#73",             # 2 digits — excluded
            "ESID#0733",           # 4 digits — excluded
            "ESID#12A",            # suffixed but only 2 digits — excluded
            "notes",
        ):
            (self.root / name).mkdir()
        found = m.find_exact_esid_folders(self.root)
        self.assertEqual(
            [f.name for f in found],
            ["ESID#004", "ESID#073", "ESID#120A", "ESID#122_Part_1_of_2"],
        )

    def test_suffixed_folder_row_carries_full_esid(self):
        # A suffixed raw folder is a DISTINCT ESID and its row must say
        # so — "120A" truncated to "120" would credit the wrong site.
        folder = self.root / "ESID#120A"
        folder.mkdir()
        (folder / "20240408_120000.WAV").write_bytes(b"\x00" * 32)
        row = m.summarize_folder(folder, min_year=2023)
        self.assertEqual(row["ESID#"], "120A")

    def test_case_variants_match(self):
        # Folder-name case is irrelevant: esid#073 and Esid#012 are
        # valid raw folders.  (Own directory: on case-insensitive
        # filesystems these could collide with upper-case twins.)
        (self.root / "esid#073").mkdir()
        (self.root / "Esid#012").mkdir()
        found = m.find_exact_esid_folders(self.root)
        self.assertEqual([f.name for f in found], ["Esid#012", "esid#073"])

    def test_sorted_numerically(self):
        for name in ("ESID#101", "ESID#012", "ESID#099"):
            (self.root / name).mkdir()
        found = m.find_exact_esid_folders(self.root)
        self.assertEqual(
            [f.name for f in found], ["ESID#012", "ESID#099", "ESID#101"]
        )

    def test_files_matching_the_pattern_are_ignored(self):
        (self.root / "ESID#055").write_text("a file, not a folder")
        self.assertEqual(m.find_exact_esid_folders(self.root), [])


class TestFilenameYear(unittest.TestCase):
    def test_valid_dates(self):
        self.assertEqual(m.filename_year("20240408_120000.WAV"), 2024)
        self.assertEqual(m.filename_year("20231014_090000.wav"), 2023)
        self.assertEqual(m.filename_year("19700101_000000.WAV"), 1970)

    def test_invalid_names_return_none(self):
        self.assertIsNone(m.filename_year("5D8F3A2B.WAV"))      # hex firmware
        self.assertIsNone(m.filename_year("20241301_000000.WAV"))  # month 13
        self.assertIsNone(m.filename_year("recording.WAV"))


class TestSummarizeFolder(_TmpTestCase):
    def _folder(self, esid="073"):
        folder = self.root / f"ESID#{esid}"
        folder.mkdir()
        return folder

    def test_counts_sizes_and_recent_timestamps(self):
        folder = self._folder()
        wav(folder, "20240408_120000.WAV", 3000)
        wav(folder, "20230101_000000.wav", 2000)   # lowercase ext counts
        wav(folder, "19700101_000000.WAV", 1000)   # unset clock: not recent
        wav(folder, "5D8F3A2B.WAV", 500)           # hex name: not recent
        row = m.summarize_folder(folder, min_year=2023)
        self.assertEqual(row["ESID#"], "073")
        self.assertEqual(row["Number of Wave files"], "4")
        self.assertEqual(
            row["Total size of wave files (GB)"], f"{6500 / 1024**3:.3f}"
        )
        self.assertEqual(
            row["Number of Wave files with timestamps of 2023 or greater"],
            "2",
        )

    def test_sidecars_nonwavs_and_subfolders_excluded(self):
        folder = self._folder()
        wav(folder, "20240408_120000.WAV", 3000)
        wav(folder, "._20240408_120000.WAV", 4096)   # AppleDouble sidecar
        (folder / "CONFIG.TXT").write_text("gain")
        sub = folder / "nested"
        sub.mkdir()
        wav(sub, "20240408_130000.WAV", 9000)        # nested: top-level only
        row = m.summarize_folder(folder, min_year=2023)
        self.assertEqual(row["Number of Wave files"], "1")
        self.assertEqual(
            row["Total size of wave files (GB)"], f"{3000 / 1024**3:.3f}"
        )

    def test_min_year_threshold_is_inclusive(self):
        folder = self._folder()
        wav(folder, "20230101_000000.WAV", 100)
        wav(folder, "20221231_235959.WAV", 100)
        row = m.summarize_folder(folder, min_year=2023)
        self.assertEqual(
            row["Number of Wave files with timestamps of 2023 or greater"],
            "1",
        )

    def test_empty_folder_reports_zeroes(self):
        row = m.summarize_folder(self._folder(), min_year=2023)
        self.assertEqual(row["Number of Wave files"], "0")
        self.assertEqual(row["Total size of wave files (GB)"], "0.000")


class TestCliEndToEnd(_TmpTestCase):
    def test_writes_csv_with_exact_columns(self):
        folder = self.root / "ESID#009"
        folder.mkdir()
        wav(folder, "20240408_120000.WAV", 2048)
        (self.root / "ESID_010_Staging").mkdir()  # must not appear
        out = self.root / "report.csv"
        result = subprocess.run(
            [sys.executable, str(_TOOL), str(self.root),
             "--output", str(out)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with out.open(newline="", encoding="utf-8") as fh:
            content = list(csv.reader(fh))
        self.assertEqual(content[0], [
            "ESID#",
            "Number of Wave files",
            "Total size of wave files (GB)",
            "Number of Wave files with timestamps of 2023 or greater",
        ])
        self.assertEqual(len(content), 2)
        self.assertEqual(content[1][0], "009")
        self.assertEqual(content[1][1], "1")
        self.assertEqual(content[1][3], "1")

    def test_missing_directory_exits_2(self):
        result = subprocess.run(
            [sys.executable, str(_TOOL), str(self.root / "nope")],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
