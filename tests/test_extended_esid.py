"""Unit tests for the extended ESID grammar (3 digits + optional suffix).

The suite-wide grammar (defined once in ``Resources/azus_common.py``):
an ESID is EXACTLY three digits (000-999) plus an optional suffix of
letters/digits/underscores starting with a non-digit — 073, 120A,
122_Part_1_of_2.  Bare 1-2 digit numbers still pad (4 → 004); a
SUFFIXED id must start with the full three digits (12A is invalid);
4+-digit runs (0733, 2024) stay malformed.  Canonical form is
underscored (folders, ZIPs, CSVs, report rows); record titles render
underscores as spaces ("ESID#122 Part 1 of 2").

These tests pin: the name parser (with reserved template tails like
``_Staging`` stripped, not swallowed into a suffix), the bare-token
normalizer, sort order, the display transform, ``--esid`` expansion,
prep's folder parsing, the collector join (case-insensitive on the
suffix), and the record title built from a suffixed ESID.

Run from the project root:

    python3 -m unittest discover -s tests -v
"""

import sys
import tempfile
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "Resources"))

import azus_common  # noqa: E402
import prepare_dataset  # noqa: E402
import standalone_tasks as tasks  # noqa: E402
from models.audiomoth import DataCollector  # noqa: E402


class TestParseEsid(unittest.TestCase):
    """Folder/file NAME parsing, including reserved template tails."""

    def test_valid_names_parse_to_canonical(self):
        cases = {
            "ESID_073": "073",
            "ESID#73": "073",
            "ESID_4": "004",
            "ESID_073_Staging": "073",
            "ESID_073_Uploaded": "073",
            "ESID_073.zip": "073",
            "ESID_073_to_upload.csv": "073",
            "ESID_073_metadata.json": "073",
            "ESID_073_request_log.json": "073",
            "ESID_120A": "120A",
            "ESID#120A": "120A",
            "esid_120A_Staging": "120A",
            "ESID_122_Part_1_of_2": "122_Part_1_of_2",
            "ESID_122_Part_1_of_2_Staging": "122_Part_1_of_2",
            "ESID_122_Part_1_of_2.zip": "122_Part_1_of_2",
            "ESID_073_backup": "073_backup",   # a suffix is a REAL ESID
        }
        for name, expected in cases.items():
            self.assertEqual(
                azus_common.parse_esid(name), expected, name
            )

    def test_malformed_names_return_none(self):
        for name in (
            "ESID_0733",          # 4-digit run
            "ESID_12A",           # suffixed ids need the full 3 digits
            "ESID_073 copy",      # space is not in the suffix alphabet
            "notes",
            ".ESID_005_Staging.artifact_stash",
            "",
        ):
            self.assertIsNone(azus_common.parse_esid(name), name)

    def test_suffix_case_is_preserved(self):
        self.assertEqual(azus_common.parse_esid("ESID_120a"), "120a")


class TestNormalizeEsid(unittest.TestCase):
    """Bare tokens/cells: CLI values, collectors-CSV ESID column."""

    def test_valid_tokens(self):
        cases = {
            "4": "004",
            "04": "004",
            "004": "004",
            "120A": "120A",
            "122_Part_1_of_2": "122_Part_1_of_2",
            "122 Part 1 of 2": "122_Part_1_of_2",   # display form
            " 073 ": "073",
        }
        for raw, expected in cases.items():
            self.assertEqual(
                azus_common.normalize_esid(raw), expected, repr(raw)
            )

    def test_invalid_tokens_raise(self):
        for raw in ("1234", "0733", "12A", "abc", "", "12.5", None):
            with self.assertRaises(ValueError, msg=repr(raw)):
                azus_common.normalize_esid(raw)


class TestSortAndDisplay(unittest.TestCase):
    def test_sort_key_orders_number_then_suffix(self):
        esids = ["121", "120A", "120", "122_Part_2_of_2",
                 "122_Part_1_of_2", "004"]
        self.assertEqual(
            sorted(esids, key=azus_common.esid_sort_key),
            ["004", "120", "120A", "121",
             "122_Part_1_of_2", "122_Part_2_of_2"],
        )

    def test_display_replaces_underscores_only(self):
        self.assertEqual(
            azus_common.esid_display("122_Part_1_of_2"), "122 Part 1 of 2"
        )
        self.assertEqual(azus_common.esid_display("120A"), "120A")
        self.assertEqual(azus_common.esid_display("004"), "004")


class TestEsidArgsAndCells(unittest.TestCase):
    """--esid expansion and spreadsheet cells accept suffixed ids."""

    def test_literal_suffixed_tokens(self):
        self.assertEqual(
            azus_common.load_esid_args(["120A", "4", "122_Part_1_of_2"]),
            ["120A", "004", "122_Part_1_of_2"],
        )

    def test_four_digit_token_still_hard_error(self):
        with self.assertRaises(ValueError):
            azus_common.load_esid_args(["1234"])

    def test_csv_with_suffixed_rows_expands(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.csv"
            path.write_text(
                "ESID#,Title\n120A,x\n004,y\n122_Part_1_of_2,z\n",
                encoding="utf-8",
            )
            self.assertEqual(
                azus_common.load_esid_args([str(path)]),
                ["120A", "004", "122_Part_1_of_2"],
            )

    def test_cell_parser_never_truncates_a_suffix(self):
        # "ESID#120A" must be 120A or nothing — "120" would credit the
        # wrong dataset.
        self.assertEqual(
            azus_common.parse_esid_cell("ESID#120A"), ("120A", "ok")
        )
        esid, reason = azus_common.parse_esid_cell("12A")
        self.assertIsNone(esid)
        self.assertIn("full 3-digit", reason)


class TestPrepFolderParsing(unittest.TestCase):
    """prepare_dataset accepts suffixed raw folders (prefixed or bare)."""

    def test_get_esid_from_folder(self):
        cases = {
            "ESID#120A": "120A",
            "ESID_122_Part_1_of_2": "122_Part_1_of_2",
            "122_Part_1_of_2": "122_Part_1_of_2",   # bare, no prefix
            "5": "005",
        }
        for name, expected in cases.items():
            self.assertEqual(
                prepare_dataset.get_esid_from_folder(name), expected, name
            )
        self.assertIsNone(prepare_dataset.get_esid_from_folder("12A"))


class TestCollectorJoinAndTitle(unittest.TestCase):
    """Suffixed ESIDs survive the CSV → model → join → title path."""

    @staticmethod
    def _collector(esid):
        return DataCollector.model_validate({
            "esid": esid,
            "affiliation": "Eclipse Soundscapes : ARISA Lab",
            "files_date_time_mode": "Automatic",
            "version": "2024.1.0",
            "latitude": "35.0000",
            "longitude": "-106.0000",
            "eclipse_date": "2024-04-08",
            "eclipse_type": "Total",
            "eclipse_coverage": "100",
            "eclipse_start_time_utc": "17:00:00",
            "eclipse_maximum_time_utc": "18:15:00",
            "subjects": "eclipse : audiomoth",
        })

    def test_model_normalizes_esid_cell(self):
        self.assertEqual(self._collector(" 5 ").esid, "005")
        self.assertEqual(self._collector("120A").esid, "120A")
        self.assertEqual(
            self._collector("122 Part 1 of 2").esid, "122_Part_1_of_2"
        )

    def test_model_rejects_malformed_esid_cell(self):
        with self.assertRaises(Exception):
            self._collector("1234")

    def test_join_is_case_insensitive_on_the_suffix(self):
        # Folder says ESID_120a, CSV says 120A — the dataset must match.
        collector = self._collector("120A")
        upload_data, unmatched = tasks.create_upload_data(
            esid_folder_archives=[
                ("120a", "/nonexistent", ["/nonexistent/ESID_120a.zip"])
            ],
            data_collectors=[collector],
            project_config={},
        )
        # The pair matched the collector (no "No collector info" drop);
        # it then failed later at file discovery (nonexistent path),
        # which proves the join itself succeeded.
        self.assertEqual(unmatched, [])

    def test_title_renders_suffix_with_spaces(self):
        for esid, rendered in (
            ("004", "ESID#004"),
            ("120A", "ESID#120A"),
            ("122_Part_1_of_2", "ESID#122 Part 1 of 2"),
        ):
            collector = self._collector(esid)
            with tempfile.TemporaryDirectory() as tmp:
                readme = Path(tmp) / "README.html"
                readme.write_text("<p>desc</p>", encoding="utf-8")
                config = tasks.get_draft_config(
                    data_collector=collector,
                    readme_html_path=str(readme),
                    project_config={
                        "title_template":
                            "$eclipse_date $eclipse_label ESID#$esid",
                    },
                )
            self.assertTrue(
                config.metadata["title"].endswith(rendered),
                config.metadata["title"],
            )


if __name__ == "__main__":
    unittest.main()
