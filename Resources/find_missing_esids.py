#!/usr/bin/env python3
"""List ESIDs present in the master spreadsheet but missing from a report.

Compares the ESID numbers in the master collectors spreadsheet against
the ESID column of a report CSV (e.g. the output of
``audit_wav_integrity.py``) and prints the ESIDs that appear in the
master but NOT in the report.

DATA-QUALITY DESIGN (this tool must never be silently wrong)
============================================================
An ESID that fails to parse can never be reported missing — so nothing
is ever skipped silently:

  * Valid ESIDs are exactly 000-999 (project invariant).  Any parsed
    number outside that range is BY DEFINITION a parsing artifact and
    is flagged, never included.
  * Every blank or unparseable cell is counted and listed with its row
    number and verbatim content.
  * The ESID column in each file is chosen deterministically (exact
    header match first; a single fuzzy candidate second; multiple
    candidates are an ERROR, not a guess) and the choice is echoed
    along with sample raw->parsed values.
  * Per-file accounting is printed as an equation that must add up.
  * The final answer is self-checked against set invariants at runtime.
  * Exit code 0 is only returned when nothing is missing AND no
    data-quality problems were seen — a "false complete" cannot hide.

USAGE
=====
    python Resources/find_missing_esids.py MASTER_CSV REPORT_CSV
    python Resources/find_missing_esids.py MASTER_CSV REPORT_CSV \
        --master-column "ESID #" --report-column "ESID#"

EXIT CODES
==========
    0  nothing missing and every cell parsed cleanly (trustworthy)
    1  missing ESIDs found, OR data-quality problems mean the result
       may be incomplete (see the printed problem list)
    2  usage error (file unreadable, ESID column absent/ambiguous)
"""

import argparse
import csv
import io
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# Project invariant (per Trae): ESIDs are three digits, 000-999.
_ESID_MIN = 0
_ESID_MAX = 999

_EXCEL_FLOAT_RE = re.compile(r"^(\d+)\.0+$")           # "73.0" -> "73"
_DIGIT_GROUP_RE = re.compile(r"\d+")
_ESID_PREFIXED_RE = re.compile(r"ESID[\s_#-]*(\d+)", re.IGNORECASE)


def _die(message: str) -> None:
    """Print a usage/data error and exit with code 2 (the usage-error code).

    Does not return — calls ``sys.exit(2)`` to end the program.  (Plain
    ``sys.exit("message")`` exits with code 1, which would collide with
    the "missing ESIDs found" code — hence this helper.)

    Args:
        message: The error text written to stderr before exiting.
    """
    print(message, file=sys.stderr)
    sys.exit(2)


def parse_esid_cell(raw: object) -> Tuple[Optional[int], str]:
    """Parse one cell into an ESID, or explain exactly why it can't be.

    Never guesses: a cell with several numbers is only accepted when
    exactly one of them is ESID-prefixed (e.g. "ESID 073 (2024)" -> 73).

    Args:
        raw: The raw cell value (any type; coerced to ``str`` and
            stripped before parsing).

    Returns:
        A ``(esid, "ok")`` tuple on success, else ``(None, reason)``
        where ``reason`` is ``"blank"`` for empty cells or a
        human-readable explanation of why the cell could not be parsed.
    """
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return None, "blank"

    m = _EXCEL_FLOAT_RE.match(text)
    if m:  # Excel numeric-export artifact: "73.0" means 73, not 730
        text = m.group(1)

    groups = _DIGIT_GROUP_RE.findall(text)
    if not groups:
        return None, f"no digits found in {text!r}"

    if len(groups) == 1:
        value = int(groups[0])
        if _ESID_MIN <= value <= _ESID_MAX:
            return value, "ok"
        return None, f"out of range 000-999: {groups[0]!r}"

    prefixed = _ESID_PREFIXED_RE.findall(text)
    if len(prefixed) == 1:
        value = int(prefixed[0])
        if _ESID_MIN <= value <= _ESID_MAX:
            return value, "ok"
        return None, f"ESID-prefixed value out of range 000-999: {prefixed[0]!r}"

    return None, f"ambiguous — {len(groups)} numbers in cell {text!r}"


def _normalize_header(header: str) -> str:
    """Header form for exact matching: casefolded, all whitespace removed.

    Args:
        header: A raw column header string.

    Returns:
        The header casefolded with all whitespace removed, for
        whitespace- and case-insensitive exact comparison.
    """
    return "".join(str(header).split()).casefold()


def choose_esid_column(
    fieldnames: List[str], override: Optional[str], path: Path
) -> Tuple[str, List[str]]:
    """Pick the ESID column deterministically.

    Priority: explicit override > exact normalized match ("esid#" then
    "esid") > a SINGLE fuzzy candidate containing "esid".  Multiple
    candidates without an exact match abort via :func:`_die` (exit 2) —
    guessing among columns is how a whole comparison silently goes wrong.

    Args:
        fieldnames: The header names read from the CSV.
        override: An explicit column name to use, bypassing detection
            (from ``--master-column`` / ``--report-column``); None to
            auto-detect.
        path: The CSV path, used only for error messages.

    Returns:
        A ``(column, warnings)`` tuple: the chosen column name and a list
        of human-readable warning strings (e.g. a header that appears
        more than once).  Never returns when the column cannot be
        resolved — :func:`_die` exits with code 2 instead.
    """
    warnings: List[str] = []
    names = [f for f in (fieldnames or []) if f]
    if not names:
        _die(f"ERROR: {path} has no header row.")

    def dup_warning(chosen: str) -> None:
        """Warn if the ``chosen`` header appears more than once."""
        if names.count(chosen) > 1:
            warnings.append(
                f"header {chosen!r} appears {names.count(chosen)} times in "
                f"{path.name}; only the LAST occurrence's values are read"
            )

    if override:
        if override in names:
            dup_warning(override)
            return override, warnings
        _die(
            f"ERROR: column {override!r} not found in {path}.\n"
            f"Headers: {names}"
        )

    for target in ("esid#", "esid"):
        hits = [f for f in names if _normalize_header(f) == target]
        if len(set(hits)) > 1:
            _die(
                f"ERROR: {path} has multiple distinct headers matching "
                f"{target!r}: {sorted(set(hits))}. Pass --master-column / "
                "--report-column to choose one."
            )
        if hits:
            dup_warning(hits[0])
            return hits[0], warnings

    fuzzy = sorted({f for f in names if "esid" in f.casefold()})
    if len(fuzzy) == 1:
        dup_warning(fuzzy[0])
        return fuzzy[0], warnings
    if not fuzzy:
        _die(f"ERROR: no ESID column in {path}.\nHeaders: {names}")
    _die(
        f"ERROR: {path} has several ESID-like columns and none is an "
        f"exact 'ESID'/'ESID#' match: {fuzzy}. Pass --master-column / "
        "--report-column to choose one."
    )


@dataclass
class FileParse:
    """Everything learned from parsing one CSV's ESID column.

    Attributes:
        path: The CSV file that was parsed.
        column: The ESID column header that was read.
        esids: Set of valid ESIDs (000-999) parsed from the column.
        total_rows: Number of data rows read (excludes the header).
        parsed: Number of rows that yielded a valid ESID.
        problems: ``(row, raw, reason)`` tuples for each cell that could
            not be parsed.
        duplicates: ``{esid: count}`` for ESIDs appearing more than once.
        samples: Up to three ``(raw, display)`` pairs, echoing how raw
            cells map to parsed ESIDs.
        warnings: Human-readable warnings (e.g. a duplicated header).
        notes: Human-readable notes (e.g. an encoding or delimiter
            fallback).
    """

    path: Path
    column: str = ""
    esids: Set[int] = field(default_factory=set)
    total_rows: int = 0
    parsed: int = 0
    problems: List[Tuple[int, str, str]] = field(default_factory=list)  # (row, raw, reason)
    duplicates: Dict[int, int] = field(default_factory=dict)            # esid -> count
    samples: List[Tuple[str, str]] = field(default_factory=list)        # (raw, display)
    warnings: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def read_esids_file(path: Path, override: Optional[str]) -> FileParse:
    """Parse one CSV's ESID column with full accounting.

    Exits 2 (via :func:`_die`) on unreadable files or unresolvable
    columns.  Handles UTF-8 (with BOM), a cp1252 fallback, and
    semicolon-delimited exports, recording each fallback as a note.

    Args:
        path: The CSV file to read.
        override: Explicit ESID column name, or None to auto-detect.

    Returns:
        A :class:`FileParse` holding the parsed ESIDs plus full
        accounting (row counts, per-row problems, duplicates, samples,
        warnings, and notes).
    """
    result = FileParse(path=path)

    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = path.read_text(encoding="cp1252")
            result.notes.append("file was not UTF-8; decoded as cp1252")
        except (OSError, UnicodeDecodeError) as exc:
            _die(f"ERROR: could not decode {path}: {exc}")
    except OSError as exc:
        _die(f"ERROR: could not read {path}: {exc}")

    # Delimiter guard: a header line with semicolons and no commas is a
    # semicolon-delimited export (common from European Excel locales).
    first_line = text.splitlines()[0] if text.strip() else ""
    delimiter = ","
    if ";" in first_line and "," not in first_line:
        delimiter = ";"
        result.notes.append("semicolon-delimited CSV detected")

    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    result.column, result.warnings = choose_esid_column(
        list(reader.fieldnames or []), override, path
    )

    counts: Dict[int, int] = {}
    for row_num, row in enumerate(reader, start=2):  # row 1 is the header
        result.total_rows += 1
        raw = row.get(result.column, "")
        esid, status = parse_esid_cell(raw)
        if esid is not None:
            result.parsed += 1
            counts[esid] = counts.get(esid, 0) + 1
            if len(result.samples) < 3:
                result.samples.append((str(raw), f"{esid:03d}"))
        else:
            result.problems.append((row_num, str(raw), status))

    result.esids = set(counts)
    result.duplicates = {e: c for e, c in counts.items() if c > 1}
    return result


def print_file_audit(label: str, fp: FileParse) -> None:
    """Print the per-file audit block: column, samples, accounting, problems.

    Args:
        label: Heading for the block (e.g. ``"Master"`` or ``"Report"``).
        fp: The parsed-file accounting to render, as returned by
            :func:`read_esids_file`.
    """
    print(f"{label}: {fp.path}")
    print(f"  Column: {fp.column!r}")
    for note in fp.notes:
        print(f"  Note:   {note}")
    for warning in fp.warnings:
        print(f"  WARNING: {warning}")
    if fp.samples:
        shown = ";  ".join(f"{raw!r} -> {parsed}" for raw, parsed in fp.samples)
        print(f"  Samples: {shown}")
    print(
        f"  Accounting: {fp.total_rows} row(s) = {fp.parsed} parsed "
        f"+ {len(fp.problems)} problem(s)"
        + (f"  [{len(fp.duplicates)} value(s) appear more than once]"
           if fp.duplicates else "")
    )
    assert fp.total_rows == fp.parsed + len(fp.problems), "accounting broke"
    for row_num, raw, reason in fp.problems[:20]:
        print(f"    row {row_num}: {raw!r} — {reason}")
    if len(fp.problems) > 20:
        print(f"    ... and {len(fp.problems) - 20} more (all counted)")
    if fp.duplicates:
        dups = ", ".join(
            f"{e:03d} x{c}" for e, c in sorted(fp.duplicates.items())
        )
        print(f"  Duplicates: {dups}")
    print()


def main() -> None:
    """Command-line entry point.  See the module docstring for usage."""
    parser = argparse.ArgumentParser(
        description=(
            "Print the ESID numbers (000-999) that appear in the master "
            "spreadsheet but are missing from a report CSV (e.g. the "
            "output of audit_wav_integrity.py). Every cell that cannot "
            "be parsed is listed — nothing is skipped silently."
        ),
    )
    parser.add_argument("master_csv", metavar="MASTER_CSV",
                        help="Master collectors spreadsheet (has an ESID column).")
    parser.add_argument("report_csv", metavar="REPORT_CSV",
                        help="Report to check, e.g. my_report.csv (ESID# column).")
    parser.add_argument("--master-column", metavar="NAME", default=None,
                        help="Exact ESID column header in the master (skips auto-detection).")
    parser.add_argument("--report-column", metavar="NAME", default=None,
                        help="Exact ESID column header in the report (skips auto-detection).")
    args = parser.parse_args()

    master = read_esids_file(Path(args.master_csv), args.master_column)
    report = read_esids_file(Path(args.report_csv), args.report_column)

    print_file_audit("Master", master)
    print_file_audit("Report", report)

    missing = sorted(master.esids - report.esids)
    extra = sorted(report.esids - master.esids)

    if missing:
        print(f"MISSING from report ({len(missing)}):")
        for esid in missing:
            print(f"  ESID {esid:03d}")
    else:
        print("MISSING from report: none — every parsed master ESID is "
              "in the report.")
    if extra:
        print(f"\nNote: {len(extra)} report ESID(s) not in the master: "
              + ", ".join(f"{e:03d}" for e in extra))

    # --- Data-quality verdict: a false 'nothing missing' cannot hide ---
    problem_count = len(master.problems) + len(report.problems)
    if problem_count:
        print()
        print("=" * 66)
        print(f"RESULTS MAY BE INCOMPLETE — {len(master.problems)} master "
              f"and {len(report.problems)} report cell(s) could not be "
              "parsed (listed above).")
        print("An ESID hidden in an unparseable cell can never be flagged "
              "as missing. Fix those cells and re-run.")
        print("=" * 66)

    # --- Self-check: runtime invariants on the answer itself ---
    missing_set = set(missing)
    invariants = (
        missing_set <= master.esids,
        not (missing_set & report.esids),
        len(master.esids) == len(master.esids & report.esids) + len(missing),
    )
    if all(invariants):
        print(f"\nSelf-check: PASSED "
              f"({len(master.esids)} master = "
              f"{len(master.esids & report.esids)} matched + "
              f"{len(missing)} missing)")
    else:
        print("\nSelf-check: FAILED — internal logic error, do NOT trust "
              "this output. Please report this.")
        sys.exit(2)

    sys.exit(1 if (missing or problem_count) else 0)


if __name__ == "__main__":
    main()
