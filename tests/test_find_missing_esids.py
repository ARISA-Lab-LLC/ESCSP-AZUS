"""Unit tests for Resources/find_missing_esids.py.

Run from the project root:

    python3 -m unittest discover -s tests -v

Covers the defects found in the QC audit (July 2026): digit
concatenation, wrong-column binding, silent cell skipping, encoding and
delimiter surprises — plus regression tests for the observed
"false complete" failure and a randomized property test proving the
missing-list equals an independent set difference.
"""

import csv
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "Resources"))

from find_missing_esids import (  # noqa: E402
    parse_esid_cell,
    choose_esid_column,
    read_esids_file,
)

_TOOL = _PROJECT_ROOT / "Resources" / "find_missing_esids.py"


def write_csv(path: Path, header, rows) -> Path:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)
    return path


def run_tool(*args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_TOOL), *[str(a) for a in args]],
        capture_output=True, text=True,
    )


class TestParseEsidCell(unittest.TestCase):
    """The per-cell parser must never guess and never silently drop."""

    def ok(self, raw, expected):
        esid, status = parse_esid_cell(raw)
        self.assertEqual(status, "ok", f"{raw!r} -> {status}")
        self.assertEqual(esid, expected, f"{raw!r}")

    def bad(self, raw, expect_blank=False):
        esid, status = parse_esid_cell(raw)
        self.assertIsNone(esid, f"{raw!r} unexpectedly parsed to {esid}")
        if expect_blank:
            self.assertEqual(status, "blank")
        else:
            self.assertNotEqual(status, "ok")

    def test_plain_forms(self):
        self.ok("073", 73)
        self.ok("73", 73)
        self.ok(" 73 ", 73)
        self.ok("000", 0)          # ESID 000 is valid
        self.ok("999", 999)

    def test_prefixed_forms(self):
        self.ok("ESID_073", 73)
        self.ok("ESID#73", 73)
        self.ok("esid 073", 73)
        self.ok("ESID-073", 73)

    def test_excel_float_artifact(self):
        # "73.0" means 73 — the old code read it as 730.
        self.ok("73.0", 73)
        self.ok("73.00", 73)

    def test_prefixed_among_other_numbers(self):
        # Old code concatenated to 732024; parser must isolate the
        # ESID-prefixed group.
        self.ok("ESID 073 (2024)", 73)

    def test_ambiguous_is_rejected_not_guessed(self):
        self.bad("12 or 13")
        self.bad("ESID 12 / ESID 13")   # two prefixed groups

    def test_out_of_range_is_artifact(self):
        self.bad("0732024")   # concatenation artifact
        self.bad("1000")      # ESIDs are 000-999
        self.bad("2024")

    def test_blank_and_no_digit_cells_are_flagged(self):
        self.bad("", expect_blank=True)
        self.bad("   ", expect_blank=True)
        self.bad(None, expect_blank=True)
        self.bad("N/A")
        self.bad("pending")


class TestChooseColumn(unittest.TestCase):
    """Column choice must be deterministic, never a guess."""

    P = Path("dummy.csv")

    def test_exact_beats_fuzzy_regardless_of_order(self):
        col, _ = choose_esid_column(["Related ESIDs", "ESID #"], None, self.P)
        self.assertEqual(col, "ESID #")
        col, _ = choose_esid_column(["ESID#", "ESID Notes"], None, self.P)
        self.assertEqual(col, "ESID#")

    def test_single_fuzzy_candidate_is_used(self):
        col, _ = choose_esid_column(["Name", "Site ESID Number"], None, self.P)
        self.assertEqual(col, "Site ESID Number")

    def test_multiple_fuzzy_without_exact_aborts(self):
        with self.assertRaises(SystemExit):
            choose_esid_column(
                ["Related ESIDs", "ESID Notes"], None, self.P
            )

    def test_no_candidate_aborts(self):
        with self.assertRaises(SystemExit):
            choose_esid_column(["Name", "Site"], None, self.P)

    def test_override_wins_and_missing_override_aborts(self):
        col, _ = choose_esid_column(["A", "B"], "B", self.P)
        self.assertEqual(col, "B")
        with self.assertRaises(SystemExit):
            choose_esid_column(["A", "B"], "C", self.P)

    def test_duplicate_header_warns(self):
        col, warnings = choose_esid_column(["ESID", "ESID"], None, self.P)
        self.assertEqual(col, "ESID")
        self.assertTrue(any("LAST occurrence" in w for w in warnings))


class TestFileGuards(unittest.TestCase):
    """Encoding and delimiter surprises must not corrupt parsing."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_bom(self):
        p = self.tmp / "bom.csv"
        p.write_bytes("﻿ESID,Name\n073,x\n".encode("utf-8"))
        self.assertEqual(read_esids_file(p, None).esids, {73})

    def test_cp1252(self):
        p = self.tmp / "cp1252.csv"
        p.write_bytes("ESID,Name\n073,Jos\xe9\n".encode("cp1252"))
        fp = read_esids_file(p, None)
        self.assertEqual(fp.esids, {73})
        self.assertTrue(any("cp1252" in n for n in fp.notes))

    def test_semicolon_delimiter(self):
        p = self.tmp / "semi.csv"
        p.write_text("ESID;Name\n073;x\n074;y\n", encoding="utf-8")
        fp = read_esids_file(p, None)
        self.assertEqual(fp.esids, {73, 74})
        self.assertTrue(any("semicolon" in n for n in fp.notes))

    def test_duplicates_are_counted(self):
        p = self.tmp / "dup.csv"
        write_csv(p, ["ESID"], [["073"], ["73"], ["074"]])
        fp = read_esids_file(p, None)
        self.assertEqual(fp.esids, {73, 74})
        self.assertEqual(fp.duplicates, {73: 2})


class TestFalseCompleteRegressions(unittest.TestCase):
    """The observed failure: genuinely missing ESIDs not reported."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_garbled_master_cell_no_longer_hides_a_missing_esid(self):
        # Old code parsed "ESID 073 (2024)" as 732024, so 073 could
        # never be reported missing.
        master = write_csv(self.tmp / "m.csv", ["ESID #"],
                           [["ESID 073 (2024)"], ["074"]])
        report = write_csv(self.tmp / "r.csv", ["ESID#"], [["074"]])
        proc = run_tool(master, report)
        self.assertIn("ESID 073", proc.stdout)
        self.assertEqual(proc.returncode, 1)

    def test_wrong_column_binding_no_longer_hides_missing(self):
        # Old code bound to the FIRST header containing "esid" — here a
        # related-IDs column that happens to contain the report's ESIDs.
        master = write_csv(
            self.tmp / "m.csv", ["Related ESIDs", "ESID #"],
            [["074", "073"], ["074", "075"]],
        )
        report = write_csv(self.tmp / "r.csv", ["ESID#"], [["074"]])
        proc = run_tool(master, report)
        self.assertIn("ESID 073", proc.stdout)
        self.assertIn("ESID 075", proc.stdout)
        self.assertIn("'ESID #'", proc.stdout)   # audit echoes the column
        self.assertEqual(proc.returncode, 1)

    def test_blank_master_cell_flags_data_quality_even_when_nothing_missing(self):
        master = write_csv(self.tmp / "m.csv", ["ESID"], [["073"], [""]])
        report = write_csv(self.tmp / "r.csv", ["ESID#"], [["073"]])
        proc = run_tool(master, report)
        self.assertIn("RESULTS MAY BE INCOMPLETE", proc.stdout)
        self.assertEqual(proc.returncode, 1)   # not a trustworthy 0


class TestPropertyRandomized(unittest.TestCase):
    """The tool's answer must equal an independent set difference."""

    def test_random_sets(self):
        rng = random.Random(42)
        tmp = Path(tempfile.mkdtemp())
        for i in range(200):
            universe = range(0, 1000)
            master_set = set(rng.sample(universe, rng.randint(1, 60)))
            report_set = set(rng.sample(universe, rng.randint(0, 60)))
            master = write_csv(tmp / f"m{i}.csv", ["ESID"],
                               [[f"{e:03d}"] for e in sorted(master_set)])
            report = write_csv(tmp / f"r{i}.csv", ["ESID#"],
                               [[f"{e:03d}"] for e in sorted(report_set)])
            m = read_esids_file(master, None)
            r = read_esids_file(report, None)
            self.assertEqual(m.esids, master_set)
            self.assertEqual(r.esids, report_set)
            self.assertEqual(m.esids - r.esids, master_set - report_set)
            self.assertFalse(m.problems or r.problems)


class TestEndToEnd(unittest.TestCase):
    """Full CLI runs: exit codes, self-check, audit output."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_missing_case(self):
        master = write_csv(self.tmp / "m.csv", ["ESID #"],
                           [["001"], ["ESID_002"], ["73"], ["104"]])
        report = write_csv(self.tmp / "r.csv", ["ESID#"],
                           [["001"], ["073"]])
        proc = run_tool(master, report)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ESID 002", proc.stdout)
        self.assertIn("ESID 104", proc.stdout)
        self.assertNotIn("ESID 001\n", proc.stdout.replace("ESID 001,", ""))
        self.assertIn("Self-check: PASSED", proc.stdout)

    def test_clean_case_exit_zero(self):
        master = write_csv(self.tmp / "m.csv", ["ESID"], [["001"], ["002"]])
        report = write_csv(self.tmp / "r.csv", ["ESID#"],
                           [["001"], ["002"], ["999"]])
        proc = run_tool(master, report)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("none", proc.stdout)
        self.assertIn("999", proc.stdout)          # extras note
        self.assertIn("Self-check: PASSED", proc.stdout)

    def test_ambiguous_columns_exit_two(self):
        master = write_csv(self.tmp / "m.csv",
                           ["Related ESIDs", "ESID Notes"], [["1", "2"]])
        report = write_csv(self.tmp / "r.csv", ["ESID#"], [["001"]])
        proc = run_tool(master, report)
        self.assertEqual(proc.returncode, 2)

    def test_unreadable_file_exit_two(self):
        report = write_csv(self.tmp / "r.csv", ["ESID#"], [["001"]])
        proc = run_tool(self.tmp / "does_not_exist.csv", report)
        self.assertEqual(proc.returncode, 2)

    def test_column_override(self):
        master = write_csv(self.tmp / "m.csv",
                           ["Related ESIDs", "ESID Notes"],
                           [["999", "001"]])
        report = write_csv(self.tmp / "r.csv", ["ESID#"], [["001"]])
        proc = run_tool(master, report, "--master-column", "ESID Notes")
        self.assertEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
