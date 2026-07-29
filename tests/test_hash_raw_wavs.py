"""Unit tests for Resources/hash_raw_wavs.py.

The cache exists to stop an interrupted file-by-file upload re-reading a
40 GB dataset. The load-bearing property is that it can never make
verification WEAKER than hashing from scratch: an entry is trusted only
when the file's size AND mtime still match, so a file altered after the
cache was written is detected and re-hashed rather than waved through.

Run from the project root:

    python3 -m unittest discover -s tests -v
"""

import csv
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "Resources"))

import azus_common  # noqa: E402
import hash_raw_wavs as hrw  # noqa: E402

_WAVS = {
    "20240408_120000.WAV": b"AUDIO-ONE-" * 40,
    "20240408_130000.WAV": b"AUDIO-TWO-" * 30,
}
_CONFIG = b"GAIN=medium\n"


class _Case(unittest.TestCase):
    """Fixture: one raw ESID folder with two WAVs and a CONFIG.TXT."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.raw = self.root / "Raw_Data"
        self.raw.mkdir()
        self.folder = self.raw / "ESID#007"
        self.folder.mkdir()
        for name, data in _WAVS.items():
            (self.folder / name).write_bytes(data)
        (self.folder / "CONFIG.TXT").write_bytes(_CONFIG)

    def names(self):
        """Every hashable name in the folder, sorted."""
        return sorted(
            p.name for p in self.folder.iterdir()
            if p.is_file() and hrw.is_hashable_name(p.name)
        )

    def run_cli(self, *extra):
        """Run main() and return its exit code."""
        argv = ["hash_raw_wavs.py", str(self.raw), *extra]
        with mock.patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit) as ctx:
                hrw.main()
        return ctx.exception.code


class TestHashableName(unittest.TestCase):
    """Only real audio and the device config are hashed."""

    def test_accepts_wavs_and_config(self):
        for name in ("a.WAV", "a.wav", "CONFIG.TXT", "config.txt"):
            self.assertTrue(hrw.is_hashable_name(name), name)

    def test_rejects_hidden_and_other_files(self):
        for name in ("._a.WAV", ".DS_Store", ".hidden.wav", "README.md",
                     "wav_hashes.csv", "ESID_007.zip"):
            self.assertFalse(hrw.is_hashable_name(name), name)


class TestCacheRoundTrip(_Case):
    """What is written can be read back, and bad caches never raise."""

    def test_creates_the_cache_on_first_use(self):
        self.assertFalse(hrw.cache_path(self.folder).exists())
        result = hrw.ensure_hashes(self.folder, self.names())
        self.assertEqual(result.hashed, 3)
        self.assertEqual(result.reused, 0)
        self.assertTrue(hrw.cache_path(self.folder).is_file())

    def test_hashes_are_correct(self):
        result = hrw.ensure_hashes(self.folder, self.names())
        for name in self.names():
            self.assertEqual(
                result.hashes[name],
                azus_common.calculate_sha512(str(self.folder / name)),
            )

    def test_cache_columns_and_contents(self):
        hrw.ensure_hashes(self.folder, self.names())
        with open(hrw.cache_path(self.folder), newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            self.assertEqual(reader.fieldnames, hrw._CACHE_COLUMNS)
            rows = {r["File Name"]: r for r in reader}
        self.assertEqual(sorted(rows), self.names())
        row = rows["CONFIG.TXT"]
        self.assertEqual(int(row["File Size (Bytes)"]), len(_CONFIG))
        self.assertEqual(len(row["SHA-512"]), 128)

    def test_missing_cache_reads_as_empty(self):
        self.assertEqual(hrw.load_cache(self.folder), {})

    def test_corrupt_cache_reads_as_empty_and_is_rebuilt(self):
        hrw.cache_path(self.folder).write_text("this is not a csv\x00\n")
        result = hrw.ensure_hashes(self.folder, self.names())
        self.assertEqual(result.hashed, 3)
        self.assertEqual(len(hrw.load_cache(self.folder)), 3)

    def test_malformed_rows_are_skipped_individually(self):
        hrw.cache_path(self.folder).write_text(
            "File Name,File Size (Bytes),Modified (epoch),SHA-512\n"
            "CONFIG.TXT,notanumber,123,abc\n"
            ",,,\n"
        )
        self.assertEqual(hrw.load_cache(self.folder), {})


class TestCacheIsReusedButNeverTrustedBlindly(_Case):
    """The property that keeps the cache from weakening verification."""

    def test_second_call_reads_no_bytes(self):
        hrw.ensure_hashes(self.folder, self.names())
        with mock.patch.object(
            hrw.azus_common, "calculate_sha512",
            side_effect=AssertionError("must not re-hash an unchanged file"),
        ):
            result = hrw.ensure_hashes(self.folder, self.names())
        self.assertEqual(result.reused, 3)
        self.assertEqual(result.hashed, 0)

    def test_changed_content_is_rehashed(self):
        hrw.ensure_hashes(self.folder, self.names())
        target = self.folder / "20240408_120000.WAV"
        target.write_bytes(b"TAMPERED" * 20)   # different size
        result = hrw.ensure_hashes(self.folder, self.names())
        self.assertEqual(result.hashed, 1)
        self.assertEqual(result.reused, 2)
        self.assertEqual(
            result.hashes[target.name],
            azus_common.calculate_sha512(str(target)),
        )

    def test_same_size_but_new_mtime_is_rehashed(self):
        """A same-length edit must not slip through on size alone."""
        hrw.ensure_hashes(self.folder, self.names())
        target = self.folder / "20240408_130000.WAV"
        original = target.read_bytes()
        target.write_bytes(b"X" * len(original))   # identical length
        os.utime(target, (0, 0))                   # and force a new mtime
        result = hrw.ensure_hashes(self.folder, self.names())
        self.assertEqual(result.hashed, 1)
        self.assertNotEqual(
            result.hashes[target.name],
            azus_common.calculate_sha512(str(self.folder / "20240408_120000.WAV")),
        )
        self.assertEqual(
            result.hashes[target.name],
            azus_common.calculate_sha512(str(target)),
        )

    def test_touching_a_file_invalidates_its_entry(self):
        hrw.ensure_hashes(self.folder, self.names())
        target = self.folder / "CONFIG.TXT"
        os.utime(target, (1_000_000, 1_000_000))
        result = hrw.ensure_hashes(self.folder, self.names())
        self.assertEqual(result.hashed, 1)

    def test_recheck_ignores_the_cache_entirely(self):
        hrw.ensure_hashes(self.folder, self.names())
        result = hrw.ensure_hashes(self.folder, self.names(), recheck=True)
        self.assertEqual(result.hashed, 3)
        self.assertEqual(result.reused, 0)

    def test_absent_file_is_reported_not_invented(self):
        result = hrw.ensure_hashes(self.folder, ["nope.WAV"])
        self.assertEqual(result.missing, ["nope.WAV"])
        self.assertNotIn("nope.WAV", result.hashes)

    def test_stale_entries_for_removed_files_are_ignored(self):
        hrw.ensure_hashes(self.folder, self.names())
        (self.folder / "20240408_120000.WAV").unlink()
        result = hrw.ensure_hashes(self.folder, self.names())
        self.assertNotIn("20240408_120000.WAV", result.hashes)
        self.assertEqual(result.reused, 2)

    def test_unreadable_file_is_an_error_not_a_hash(self):
        real = azus_common.calculate_sha512

        def fail_one(path):
            if path.endswith("CONFIG.TXT"):
                raise OSError("simulated read failure")
            return real(path)

        with mock.patch.object(
            hrw.azus_common, "calculate_sha512", side_effect=fail_one
        ):
            result = hrw.ensure_hashes(self.folder, self.names())
        self.assertNotIn("CONFIG.TXT", result.hashes)
        self.assertTrue(any("CONFIG.TXT" in e for e in result.errors))
        self.assertEqual(result.hashed, 2)


class TestCli(_Case):
    """Walking a Raw_Data tree."""

    def test_hashes_every_esid_folder(self):
        second = self.raw / "ESID#008"
        second.mkdir()
        (second / "20240408_140000.WAV").write_bytes(b"MORE-AUDIO" * 10)
        self.assertEqual(self.run_cli(), 0)
        self.assertTrue(hrw.cache_path(self.folder).is_file())
        self.assertTrue(hrw.cache_path(second).is_file())

    def test_ignores_non_esid_directories(self):
        (self.raw / "notes").mkdir()
        self.assertEqual(self.run_cli(), 0)
        self.assertFalse(hrw.cache_path(self.raw / "notes").exists())

    def test_second_run_reuses_everything(self):
        self.run_cli()
        with mock.patch.object(
            hrw.azus_common, "calculate_sha512",
            side_effect=AssertionError("nothing should need re-hashing"),
        ):
            self.assertEqual(self.run_cli(), 0)

    def test_esid_honours_the_given_order(self):
        """Same semantics as prep_all_datasets: --esid order, not numeric."""
        second = self.raw / "ESID#008"
        second.mkdir()
        (second / "20240408_140000.WAV").write_bytes(b"MORE" * 10)
        with mock.patch.object(hrw, "ensure_hashes",
                               wraps=hrw.ensure_hashes) as spy:
            self.assertEqual(self.run_cli("--esid", "008", "007"), 0)
        order = [call.args[0].name for call in spy.call_args_list]
        self.assertEqual(order, ["ESID#008", "ESID#007"])

    def test_requested_esid_without_a_folder_is_reported_not_dropped(self):
        with self.assertLogs("azus.wav_hashes", level="WARNING") as caught:
            self.assertEqual(self.run_cli("--esid", "007", "999"), 0)
        self.assertTrue(
            any("999" in line and "no raw folder" in line
                for line in caught.output),
            caught.output,
        )

    def test_a_new_file_in_a_cached_folder_is_still_hashed(self):
        """The cache is per FILE, not per folder — a folder that already has
        a wav_hashes.csv still picks up newly added WAVs."""
        self.run_cli()
        (self.folder / "20240408_190000.WAV").write_bytes(b"BRANDNEW" * 50)
        result = hrw.ensure_hashes(self.folder, self.names())
        self.assertEqual(result.hashed, 1)
        self.assertEqual(result.reused, 3)
        self.assertIn("20240408_190000.WAV", hrw.load_cache(self.folder))

    def test_an_unchanged_folder_does_not_rewrite_its_cache(self):
        self.run_cli()
        before = hrw.cache_path(self.folder).stat().st_mtime_ns
        self.run_cli()
        self.assertEqual(
            hrw.cache_path(self.folder).stat().st_mtime_ns, before,
            "an up-to-date folder must not be rewritten",
        )

    def test_esid_accepts_a_csv_file(self):
        """--esid takes a CSV whose first column lists ESIDs, header optional."""
        second = self.raw / "ESID#008"
        second.mkdir()
        (second / "20240408_140000.WAV").write_bytes(b"MORE" * 10)
        with_header = self.root / "want.csv"
        with_header.write_text("ESID#\n008\n")
        self.assertEqual(self.run_cli("--esid", str(with_header)), 0)
        self.assertTrue(hrw.cache_path(second).is_file())
        self.assertFalse(hrw.cache_path(self.folder).exists())

        bare = self.root / "bare.csv"
        bare.write_text("007\n")
        self.assertEqual(self.run_cli("--esid", str(bare)), 0)
        self.assertTrue(hrw.cache_path(self.folder).is_file())

    def test_esid_mixes_literals_and_csv_paths_in_order(self):
        second = self.raw / "ESID#008"
        second.mkdir()
        (second / "20240408_140000.WAV").write_bytes(b"MORE" * 10)
        listing = self.root / "want.csv"
        listing.write_text("ESID#\n007\n")
        with mock.patch.object(hrw, "ensure_hashes",
                               wraps=hrw.ensure_hashes) as spy:
            self.assertEqual(self.run_cli("--esid", "008", str(listing)), 0)
        self.assertEqual(
            [call.args[0].name for call in spy.call_args_list],
            ["ESID#008", "ESID#007"],
        )

    def test_esid_rejects_a_token_that_is_neither(self):
        self.assertEqual(self.run_cli("--esid", "not-an-esid"), 2)

    def test_esid_filter(self):
        second = self.raw / "ESID#008"
        second.mkdir()
        (second / "20240408_140000.WAV").write_bytes(b"MORE" * 10)
        self.assertEqual(self.run_cli("--esid", "007"), 0)
        self.assertTrue(hrw.cache_path(self.folder).is_file())
        self.assertFalse(hrw.cache_path(second).exists())

    def test_esid_matching_nothing_is_a_usage_error(self):
        self.assertEqual(self.run_cli("--esid", "999"), 2)

    def test_missing_raw_dir_is_a_usage_error(self):
        with mock.patch.object(sys, "argv", [
            "hash_raw_wavs.py", str(self.root / "nope"),
        ]):
            with self.assertRaises(SystemExit) as ctx:
                hrw.main()
        self.assertEqual(ctx.exception.code, 2)

    def test_suffixed_part_folders_are_handled(self):
        part = self.raw / "ESID#445_Part_1_of_2"
        part.mkdir()
        (part / "20240408_160000.WAV").write_bytes(b"PART" * 10)
        self.assertEqual(self.run_cli("--esid", "445_Part_1_of_2"), 0)
        self.assertTrue(hrw.cache_path(part).is_file())

    def test_unreadable_file_exits_1(self):
        real = azus_common.calculate_sha512

        def fail_one(path):
            if path.endswith("CONFIG.TXT"):
                raise OSError("simulated")
            return real(path)

        with mock.patch.object(
            hrw.azus_common, "calculate_sha512", side_effect=fail_one
        ):
            self.assertEqual(self.run_cli(), 1)

    def test_cache_file_is_not_itself_hashed(self):
        self.run_cli()
        self.assertNotIn(hrw.CACHE_FILENAME, hrw.load_cache(self.folder))


class TestSplitPreservesTheCache(_Case):
    """A same-filesystem rename keeps size and mtime, so hashes stay valid."""

    def test_moved_file_keeps_its_cached_hash(self):
        hrw.ensure_hashes(self.folder, self.names())
        cache = hrw.load_cache(self.folder)
        part2 = self.raw / "ESID#007_Part_2_of_2"
        part2.mkdir()
        moved = "20240408_130000.WAV"
        (self.folder / moved).rename(part2 / moved)
        # Seed Part 2 with the same cache, as the split tool's copy would.
        hrw.write_cache(part2, cache)
        with mock.patch.object(
            hrw.azus_common, "calculate_sha512",
            side_effect=AssertionError("the rename preserved size and mtime"),
        ):
            result = hrw.ensure_hashes(part2, [moved])
        self.assertEqual(result.reused, 1)


if __name__ == "__main__":
    unittest.main()
