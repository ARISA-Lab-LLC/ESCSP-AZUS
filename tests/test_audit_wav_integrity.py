"""Unit tests for Resources/audit_wav_integrity.py.

Run from the project root:

    python3 -m unittest discover -s tests -v

Every size the tool reports is measured two independent ways; these
tests prove the cross-checks catch the failure modes that a single
measurement misses — most importantly the reported symptom (files
labelled zero-byte that are not) and the aggregate-only false match.
"""

import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "Resources"))

import audit_wav_integrity as m  # noqa: E402

_TOOL = _PROJECT_ROOT / "Resources" / "audit_wav_integrity.py"


# --- helpers --------------------------------------------------------------

def riff_header(declared_total: int) -> bytes:
    """First 12 bytes of a WAV whose header declares `declared_total`."""
    chunk = declared_total - 8
    return b"RIFF" + chunk.to_bytes(4, "little") + b"WAVE"


def write_wav(path: Path, total_size: int) -> None:
    """A well-formed WAV whose actual length AND declared length are
    both `total_size` (stat and header agree)."""
    content = riff_header(total_size) + b"\x00" * (total_size - 12)
    path.write_bytes(content)


def write_truncated_wav(path: Path, declared: int, actual: int) -> None:
    """A WAV whose header declares `declared` bytes but whose file is
    only `actual` bytes (header > file = truncation)."""
    content = riff_header(declared) + b"\x00" * (actual - 12)
    path.write_bytes(content)


# --- pure: RIFF header parsing -------------------------------------------

class TestParseRiff(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(m.parse_riff_declared_size(riff_header(1000)), 1000)

    def test_too_short(self):
        self.assertIsNone(m.parse_riff_declared_size(b"RIFF"))
        self.assertIsNone(m.parse_riff_declared_size(b""))

    def test_wrong_magic(self):
        self.assertIsNone(m.parse_riff_declared_size(b"NOTAWAVE1234"))
        # RIFF but not WAVE (e.g. AVI)
        self.assertIsNone(
            m.parse_riff_declared_size(b"RIFF" + (100).to_bytes(4, "little") + b"AVI ")
        )


# --- pure: disk classification (incl. the reported symptom) --------------

class TestClassifyDisk(unittest.TestCase):
    T = 1024

    def test_normal_file_ok(self):
        v = m.classify_disk_wav(50_000, riff_header(50_000), self.T)
        self.assertFalse(v.is_zero or v.is_tiny)
        self.assertIsNone(v.discrepancy)

    def test_tiny_file(self):
        v = m.classify_disk_wav(100, riff_header(100), self.T)
        self.assertTrue(v.is_tiny)
        self.assertFalse(v.is_zero)
        self.assertIsNone(v.discrepancy)

    def test_true_zero_byte(self):
        v = m.classify_disk_wav(0, b"", self.T)
        self.assertTrue(v.is_zero)
        self.assertIsNone(v.discrepancy)

    def test_placeholder_zero_is_flagged_not_counted_zero(self):
        # THE REPORTED SYMPTOM: stat says 0 but the header is readable
        # (Dropbox online-only / stale metadata).  Must NOT be a zero-byte.
        v = m.classify_disk_wav(0, riff_header(50_000), self.T)
        self.assertFalse(v.is_zero)
        self.assertIsNotNone(v.discrepancy)
        self.assertIn("placeholder", v.discrepancy)

    def test_truncated_file_flagged(self):
        v = m.classify_disk_wav(200, riff_header(50_000), self.T)
        self.assertIsNotNone(v.discrepancy)
        self.assertIn("truncated", v.discrepancy)

    def test_no_valid_header_flagged(self):
        v = m.classify_disk_wav(500, b"GARBAGE12345", self.T)
        self.assertIsNotNone(v.discrepancy)
        self.assertIn("no valid RIFF", v.discrepancy)

    def test_oversize_skips_header_check(self):
        v = m.classify_disk_wav(m._UINT32_MAX + 10, b"RIFF", self.T)
        self.assertIsNone(v.discrepancy)   # can't RIFF-check >4GB, no false flag


# --- pure: ZIP entry classification --------------------------------------

class TestClassifyZip(unittest.TestCase):
    T = 1024

    def test_normal_stored(self):
        v = m.classify_zip_entry(50_000, 50_000, zipfile.ZIP_STORED, 0xABCD, self.T)
        self.assertIsNone(v.discrepancy)
        self.assertFalse(v.is_zero or v.is_tiny)

    def test_true_empty(self):
        v = m.classify_zip_entry(0, 0, zipfile.ZIP_STORED, 0, self.T)
        self.assertTrue(v.is_zero)
        self.assertIsNone(v.discrepancy)

    def test_size_zero_but_crc_nonzero_is_flagged(self):
        v = m.classify_zip_entry(0, 500, zipfile.ZIP_DEFLATED, 0x1234, self.T)
        self.assertFalse(v.is_zero)
        self.assertIsNotNone(v.discrepancy)
        self.assertIn("CRC", v.discrepancy)

    def test_size_zero_but_compressed_nonzero_is_flagged(self):
        v = m.classify_zip_entry(0, 500, zipfile.ZIP_DEFLATED, 0, self.T)
        self.assertFalse(v.is_zero)
        self.assertIsNotNone(v.discrepancy)

    def test_stored_size_mismatch_flagged(self):
        v = m.classify_zip_entry(1000, 900, zipfile.ZIP_STORED, 0xABCD, self.T)
        self.assertIsNotNone(v.discrepancy)


# --- pure: per-file Match (the aggregate false-YES regression) -----------

class TestCompareFileMaps(unittest.TestCase):
    def test_identical_matches(self):
        ok, notes = m.compare_file_maps({"A": 100, "B": 200}, {"A": 100, "B": 200})
        self.assertTrue(ok)
        self.assertEqual(notes, [])

    def test_swapped_sizes_is_not_a_match(self):
        # Old aggregate code: count 2==2, bytes 300==300 -> false YES.
        ok, notes = m.compare_file_maps({"A": 100, "B": 200}, {"A": 200, "B": 100})
        self.assertFalse(ok)
        self.assertTrue(any("differ in size" in n for n in notes))

    def test_missing_and_extra(self):
        ok, notes = m.compare_file_maps({"A": 1, "B": 2}, {"A": 1, "C": 9})
        self.assertFalse(ok)
        self.assertTrue(any("missing from ZIP" in n for n in notes))
        self.assertTrue(any("not on disk" in n for n in notes))


# --- integration: real files + a real ZIP --------------------------------

class TestScanDisk(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def test_counts_bytes_zero_tiny_and_excludes_sidecars(self):
        write_wav(self.d / "GOOD.WAV", 50_000)
        (self.d / "EMPTY.WAV").write_bytes(b"")           # real zero
        write_wav(self.d / "TINY.WAV", 100)               # tiny
        (self.d / "._GOOD.WAV").write_bytes(b"\x00\x05\x16\x07junk")  # sidecar
        (self.d / "notes.txt").write_text("ignore")       # non-wav
        sub = self.d / "nested"
        sub.mkdir()
        write_wav(sub / "NESTED.WAV", 40_000)             # nested (excluded)

        s = m.scan_disk_wavs(self.d, 1024)
        self.assertEqual(s.count, 3)                       # GOOD, EMPTY, TINY
        self.assertEqual(s.total_bytes, 50_000 + 0 + 100)
        self.assertEqual(s.zero_count, 1)
        self.assertEqual(s.tiny_count, 1)
        self.assertEqual(s.discrepancy_count, 0)
        self.assertEqual(s.skipped_sidecars, 1)
        self.assertNotIn("NESTED.WAV", s.names)
        self.assertNotIn("._GOOD.WAV", s.names)

    def test_truncated_file_is_flagged_on_disk(self):
        write_truncated_wav(self.d / "CUT.WAV", declared=50_000, actual=300)
        s = m.scan_disk_wavs(self.d, 1024)
        self.assertEqual(s.discrepancy_count, 1)
        self.assertIn("truncated", s.discrepancies[0][1])


class TestScanZipAndMatch(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def _zip(self, entries):
        zp = self.d / "ESID_073.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr("ESID_073/", b"")                  # a directory entry
            for name, data in entries:
                zf.writestr(f"ESID_073/{name}", data)
        return zp

    def test_zip_scan_basic(self):
        zp = self._zip([("A.WAV", b"x" * 500), ("B.WAV", b"y" * 2000),
                        ("EMPTY.WAV", b""), ("readme.txt", b"hi")])
        stats, err = m.scan_zip_wavs(zp, 1024)
        self.assertIsNone(err)
        self.assertEqual(stats.count, 3)                   # 3 wavs, txt ignored
        self.assertEqual(stats.total_bytes, 500 + 2000 + 0)
        self.assertEqual(stats.zero_count, 1)
        self.assertEqual(stats.tiny_count, 1)              # A.WAV = 500 < 1024
        self.assertEqual(stats.discrepancy_count, 0)

    def test_matching_disk_and_zip_is_yes(self):
        write_wav(self.d / "A.WAV", 500)
        write_wav(self.d / "B.WAV", 2000)
        disk = m.scan_disk_wavs(self.d, 1)                 # threshold 1 -> no tiny
        zp = self._zip([("A.WAV", riff_header(500) + b"\x00" * 488),
                        ("B.WAV", riff_header(2000) + b"\x00" * 1988)])
        zstats, _ = m.scan_zip_wavs(zp, 1)
        row = m.build_row("073", disk, "Staging", zstats, None)
        self.assertEqual(row["Match"], "YES")
        self.assertFalse(m.row_has_problem(row))

    def test_size_swap_is_no_not_false_yes(self):
        # disk A=500,B=2000 ; zip A=2000,B=500  (totals + counts identical)
        write_wav(self.d / "A.WAV", 500)
        write_wav(self.d / "B.WAV", 2000)
        disk = m.scan_disk_wavs(self.d, 1)
        zp = self._zip([("A.WAV", riff_header(2000) + b"\x00" * 1988),
                        ("B.WAV", riff_header(500) + b"\x00" * 488)])
        zstats, _ = m.scan_zip_wavs(zp, 1)
        self.assertEqual(disk.total_bytes, zstats.total_bytes)   # aggregate equal
        self.assertEqual(disk.count, zstats.count)
        row = m.build_row("073", disk, "Staging", zstats, None)
        self.assertEqual(row["Match"], "NO")                     # per-file catches it
        self.assertIn("differ in size", str(row["Notes"]))

    def test_duplicate_basename_in_zip_is_no(self):
        write_wav(self.d / "A.WAV", 500)
        zp = self.d / "ESID_073.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr("ESID_073/A.WAV", b"x" * 500)
            zf.writestr("ESID_073/sub/A.WAV", b"y" * 500)         # dup basename
        zstats, _ = m.scan_zip_wavs(zp, 1)
        disk = m.scan_disk_wavs(self.d, 1)
        row = m.build_row("073", disk, "Staging", zstats, None)
        self.assertEqual(row["Match"], "NO")
        self.assertIn("duplicate", str(row["Notes"]).lower())

    def test_corrupt_zip_is_reported(self):
        zp = self.d / "ESID_073.zip"
        zp.write_bytes(b"not a zip at all")
        stats, err = m.scan_zip_wavs(zp, 1)
        self.assertIsNone(stats)
        self.assertIsNotNone(err)


class TestSelfCheck(unittest.TestCase):
    """The belt-and-suspenders aggregate self-check (WavStats.verify)."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def test_normal_scan_verifies_clean(self):
        write_wav(self.d / "A.WAV", 50_000)
        write_wav(self.d / "B.WAV", 100)          # tiny
        (self.d / "EMPTY.WAV").write_bytes(b"")   # zero
        self.assertEqual(m.scan_disk_wavs(self.d, 1024).verify(), [])

    def test_corrupted_total_is_caught(self):
        write_wav(self.d / "A.WAV", 50_000)
        s = m.scan_disk_wavs(self.d, 1024)
        s.total_bytes += 1                        # simulate an accumulation bug
        errs = s.verify()
        self.assertTrue(any("total_bytes" in e for e in errs))

    def test_corrupted_count_is_caught(self):
        write_wav(self.d / "A.WAV", 50_000)
        s = m.scan_disk_wavs(self.d, 1024)
        s.count += 1
        self.assertTrue(any("count" in e for e in s.verify()))

    def test_corrupted_zero_count_is_caught(self):
        write_wav(self.d / "A.WAV", 50_000)
        s = m.scan_disk_wavs(self.d, 1024)
        s.zero_names.append("phantom.WAV")
        self.assertTrue(any("zero" in e for e in s.verify()))

    def test_zip_with_duplicate_basenames_still_verifies(self):
        # count includes both entries; sizes map collapses to one distinct
        # name — verify must account for that and NOT false-alarm.
        zp = self.d / "ESID_073.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr("ESID_073/A.WAV", b"x" * 500)
            zf.writestr("ESID_073/sub/A.WAV", b"y" * 700)
        stats, _ = m.scan_zip_wavs(zp, 1)
        self.assertEqual(stats.count, 2)
        self.assertEqual(len(stats.sizes), 1)
        self.assertEqual(stats.verify(), [])       # consistent despite the dup


class TestHumanSize(unittest.TestCase):
    def test_never_zero_for_nonzero(self):
        # A 4 MB folder must NOT read as '0' (the old GB column rounded to 0.0).
        self.assertNotEqual(m.human_size(4_000_000).split()[0], "0")
        self.assertEqual(m.human_size(0), "0 B")
        self.assertTrue(m.human_size(50_000_000_000).endswith("GB"))


class TestRowHasProblem(unittest.TestCase):
    def _row(self, **over):
        base = {c: "" for c in m._CSV_COLUMNS}
        base.update({"Disk Zero-Byte WAVs": 0, "Disk Tiny WAVs": 0,
                     "ZIP Zero-Byte WAVs": 0, "ZIP Tiny WAVs": 0,
                     "Match": "YES", "Disk Cross-Check": "OK",
                     "ZIP Cross-Check": "OK", "Notes": ""})
        base.update(over)
        return base

    def test_clean_is_not_problem(self):
        self.assertFalse(m.row_has_problem(self._row()))

    def test_discrepancy_is_problem(self):
        self.assertTrue(m.row_has_problem(
            self._row(**{"Disk Cross-Check": "DISCREPANCY (1)"})))

    def test_zero_byte_is_problem(self):
        self.assertTrue(m.row_has_problem(self._row(**{"Disk Zero-Byte WAVs": 2})))

    def test_match_no_is_problem(self):
        self.assertTrue(m.row_has_problem(self._row(Match="NO")))


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(_TOOL), str(self.d / "raw"), *args],
            capture_output=True, text=True,
        )

    def test_clean_disk_no_zip_exits_zero(self):
        raw = self.d / "raw" / "ESID_073"
        raw.mkdir(parents=True)
        write_wav(raw / "A.WAV", 50_000)
        out = self.d / "r.csv"
        proc = self._run("--output", str(out), "--tiny-threshold", "1")
        self.assertEqual(proc.returncode, 0)               # ZIP Not Found != problem
        header = out.read_text().splitlines()[0]
        self.assertIn("Disk Cross-Check", header)
        self.assertIn("ZIP Cross-Check", header)

    def test_zero_byte_wav_exits_one(self):
        raw = self.d / "raw" / "ESID_073"
        raw.mkdir(parents=True)
        (raw / "EMPTY.WAV").write_bytes(b"")
        proc = self._run("--output", str(self.d / "r.csv"))
        self.assertEqual(proc.returncode, 1)

    def test_bad_threshold_exits_two(self):
        (self.d / "raw").mkdir()
        proc = self._run("--tiny-threshold", "0")
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
