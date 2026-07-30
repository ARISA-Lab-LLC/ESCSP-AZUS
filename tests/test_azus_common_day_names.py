"""Unit tests for the per-day ZIP naming helpers in azus_common.py.

The per-day prep workflow names archives ``ESID_NNN_YYYY_MM_DD.zip``,
where the day comes LITERALLY from the WAV filename prefix.  Three
properties are load-bearing:

* ``wav_day_key`` is the single grouping rule — bogus-but-8-digit dates
  (an unset AudioMoth clock stamps 1970) group rather than block, and
  only a name with no 8-digit prefix has nothing to group by;
* ``parse_day_zip_name`` round-trips ``day_zip_name`` even for Part
  ESIDs, whose suffixes are themselves digits and underscores;
* ``parse_esid`` strips a date tail like a reserved tail — without
  that, every day-ZIP name would parse as a nonsense suffixed ESID
  (``"073_2024_04_08"``) and match no collector row, no manifest, and
  no staging folder.

Run from the project root:

    python3 -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "Resources"))

import azus_common  # noqa: E402


class TestWavDayKey(unittest.TestCase):
    """The literal 8-digit prefix is the day — no calendar judgement."""

    def test_normal_audiomoth_name(self):
        self.assertEqual(
            azus_common.wav_day_key("20240408_120000.WAV"), "2024_04_08"
        )

    def test_unset_clock_1970_groups_rather_than_blocks(self):
        self.assertEqual(
            azus_common.wav_day_key("19700101_000000.WAV"), "1970_01_01"
        )

    def test_non_calendar_digits_still_group(self):
        # 99th month of month 99: nonsense, but literal is the contract.
        self.assertEqual(
            azus_common.wav_day_key("20249999_000000.WAV"), "2024_99_99"
        )

    def test_no_eight_digit_prefix_returns_none(self):
        for name in ("Recording_1.WAV", "2024040_120000.WAV",
                     "wav20240408.WAV", "CONFIG.TXT", ""):
            self.assertIsNone(azus_common.wav_day_key(name), name)

    def test_surrounding_whitespace_is_tolerated(self):
        self.assertEqual(
            azus_common.wav_day_key(" 20240408_120000.WAV "), "2024_04_08"
        )


class TestDayZipNameRoundTrip(unittest.TestCase):
    """parse_day_zip_name inverts day_zip_name for every ESID shape."""

    def test_round_trip(self):
        for esid in ("007", "073", "120A", "122_Part_1_of_2",
                     "243_Part_2_of_2"):
            for day in ("2024_04_08", "1970_01_01"):
                name = azus_common.day_zip_name(esid, day)
                self.assertEqual(
                    azus_common.parse_day_zip_name(name), (esid, day),
                    name,
                )

    def test_the_names_the_user_specified(self):
        self.assertEqual(
            azus_common.day_zip_name("666", "2024_04_08"),
            "ESID_666_2024_04_08.zip",
        )
        self.assertEqual(
            azus_common.day_zip_name("122_Part_1_of_2", "2024_04_08"),
            "ESID_122_Part_1_of_2_2024_04_08.zip",
        )

    def test_part_suffix_is_not_eaten_by_the_date_tail(self):
        """The suffix is digits-and-underscores too; only the END is a day."""
        esid, day = azus_common.parse_day_zip_name(
            "ESID_122_Part_1_of_2_2024_04_08.zip"
        )
        self.assertEqual(esid, "122_Part_1_of_2")
        self.assertEqual(day, "2024_04_08")

    def test_legacy_single_zip_is_not_a_day_zip(self):
        for name in ("ESID_005.zip", "ESID_120A.zip",
                     "ESID_122_Part_1_of_2.zip"):
            self.assertIsNone(azus_common.parse_day_zip_name(name), name)

    def test_undated_or_malformed_names_return_none(self):
        for name in ("ESID_005_20240408.zip",      # compact date: not a tail
                     "ESID_005_2024-04-08.zip",    # hyphens: not the grammar
                     "notanesid_2024_04_08.zip",   # no ESID before the tail
                     "ESID_0055_2024_04_08.zip"):  # 4-digit ESID: malformed
            self.assertIsNone(azus_common.parse_day_zip_name(name), name)


class TestParseEsidDateTail(unittest.TestCase):
    """A day-ZIP name resolves to its real ESID, not a poisoned suffix."""

    def test_date_tail_is_stripped(self):
        for name, expected in (
            ("ESID_007_2024_04_08.zip", "007"),
            ("ESID_120A_2024_04_08.zip", "120A"),
            ("ESID_122_Part_1_of_2_2024_04_08.zip", "122_Part_1_of_2"),
            ("ESID_666_1970_01_01.zip", "666"),
        ):
            self.assertEqual(azus_common.parse_esid(name), expected, name)

    def test_plain_names_are_unaffected(self):
        for name, expected in (
            ("ESID_073", "073"), ("ESID#73", "073"), ("ESID_005.zip", "005"),
            ("ESID_120A", "120A"),
            ("ESID_122_Part_1_of_2_Staging", "122_Part_1_of_2"),
        ):
            self.assertEqual(azus_common.parse_esid(name), expected, name)

    def test_reserved_tails_still_strip(self):
        self.assertEqual(
            azus_common.parse_esid("ESID_073_to_upload.csv"), "073"
        )
        self.assertEqual(
            azus_common.parse_esid("ESID_073_request_log.json"), "073"
        )

    def test_malformed_names_still_return_none(self):
        for name in ("ESID_0733", "ESID_12A", "backup", ".DS_Store"):
            self.assertIsNone(azus_common.parse_esid(name), name)


class TestZenodoCapConstant(unittest.TestCase):
    """One constant, shared — the modules cannot drift apart."""

    def test_value(self):
        self.assertEqual(azus_common.ZENODO_MAX_FILES_PER_RECORD, 100)

    def test_file_by_file_upload_aliases_it(self):
        import file_by_file_upload as fbf
        self.assertIs(
            fbf._ZENODO_MAX_FILES_PER_RECORD,
            azus_common.ZENODO_MAX_FILES_PER_RECORD,
        )


if __name__ == "__main__":
    unittest.main()
