#!/usr/bin/env python3
"""Shared helpers for the AZUS pipeline and its Resources/ tools.

WHY THIS MODULE EXISTS
======================
Before it, the boilerplate below was copy-pasted across up to eight
tools — and the copies had already drifted (four different ESID regex
variants, three independent definitions of the ``.prep_complete``
sentinel name, two copies of ``calculate_sha512``).  Drift in these
particular helpers is dangerous: the sentinel name and the ESID parse
are semantics every tool must agree on, or one tool silently ignores a
dataset another tool reports.

Everything here is dependency-free (stdlib only) and side-effect-free
at import time, so any script can import it safely.

IMPORT PATTERNS
===============
* Tools inside ``Resources/`` (run as scripts, so ``Resources/`` is on
  ``sys.path``)::

      import azus_common

* Modules at the project root (``standalone_tasks.py``)::

      sys.path.insert(0, str(Path(__file__).resolve().parent / "Resources"))
      import azus_common

WHAT DELIBERATELY STAYS OUT
===========================
Domain logic (Zenodo API calls, WAV/RIFF analysis, upload orchestration)
stays in its owning module — this file is for the cross-cutting
constants and small pure helpers only.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger("azus.common")

# ---------------------------------------------------------------------
# Project layout
# ---------------------------------------------------------------------
# This file lives in Resources/ inside the project root.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
STAGING_AREA = PROJECT_ROOT / "Staging_Area"
UPLOADED_DATA = PROJECT_ROOT / "Uploaded_Data"

# ---------------------------------------------------------------------
# Cross-tool contract constants
# ---------------------------------------------------------------------
# Touched by prepare_dataset.py as its VERY LAST action; every other
# tool treats its presence as "preparation finished cleanly".
PREP_SENTINEL = ".prep_complete"

# Written by the uploader immediately after draft creation; links a
# staging folder to its Zenodo draft so re-runs RESUME instead of
# creating a duplicate record.
STATE_FILENAME = "upload_state.json"

# Read-buffer size for streaming file hashes (64 KB).
HASH_BUFFER_SIZE = 65_536

# ---------------------------------------------------------------------
# ESID parsing — THE single definition of a valid ESID token
# ---------------------------------------------------------------------
# Project invariant: ESIDs are 000–999, written in folder/file names as
# ESID_073, ESID#73, ESID_073_Staging, ESID_073_Uploaded, etc.
# (Title-pattern matching in the Zenodo report tools is deliberately
# separate — record titles have their own user-configurable patterns.)
_ESID_NAME_RE = re.compile(r"^ESID[_#](\d{1,3})(?!\d)", re.IGNORECASE)


def parse_esid(name: str) -> Optional[str]:
    """Extract the zero-padded 3-digit ESID from a folder/file name.

    Accepts every naming variant the pipeline produces: ``ESID_073``,
    ``ESID#73``, ``ESID_4``, ``ESID_073_Staging``, ``ESID_073.zip``.
    Enforces the project invariant that ESIDs are 000–999: a name whose
    digit run is longer than three digits (``ESID_0733``) is malformed
    and returns None rather than being silently reinterpreted.

    Args:
        name: A folder or file basename (not a full path).

    Returns:
        The ESID as a zero-padded 3-digit string (``"073"``), or None
        when the name carries no valid ESID token.
    """
    m = _ESID_NAME_RE.match(name.strip())
    if m is None:
        return None
    return f"{int(m.group(1)):03d}"


def find_esid_folders(root: Path) -> List[Tuple[int, str, Path]]:
    """Find every ESID_NNN subdirectory of ``root``, sorted numerically.

    Non-ESID directories (``.DS_Store`` siblings, ``backup``, etc.) are
    silently ignored.  The padded form is always 3-digit.

    Args:
        root: Parent folder containing ESID directories.

    Returns:
        List of ``(numeric_esid, padded_str, folder_path)`` tuples in
        ascending numeric order.  Empty when ``root`` is not a
        directory or has no matching subdirectories.
    """
    found: List[Tuple[int, str, Path]] = []
    if not root.is_dir():
        return found
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        padded = parse_esid(entry.name)
        if padded is None:
            continue
        found.append((int(padded), padded, entry))
    found.sort(key=lambda t: t[0])
    return found


# ---------------------------------------------------------------------
# ESID cell parsing + spreadsheet expansion
# ---------------------------------------------------------------------
# Cell-level parsing (spreadsheet values, as opposed to the folder/file
# NAMES parse_esid handles).  Moved here from find_missing_esids.py so
# there is exactly one definition; that module aliases these.
_ESID_MIN = 0
_ESID_MAX = 999

_EXCEL_FLOAT_RE = re.compile(r"^(\d+)\.0+$")           # "73.0" -> "73"
_DIGIT_GROUP_RE = re.compile(r"\d+")
_ESID_PREFIXED_RE = re.compile(r"ESID[\s_#-]*(\d+)", re.IGNORECASE)


def parse_esid_cell(raw: object) -> Tuple[Optional[int], str]:
    """Parse one spreadsheet cell into an ESID, or explain why not.

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


def _esids_from_csv_first_column(path: Path) -> List[str]:
    """Read zero-padded ESIDs from the FIRST column of a CSV file.

    Built for feeding the reporting tools' CSVs (all of which put
    ``ESID#`` in column one) back into the pipeline, but tolerant of any
    CSV whose first column holds ESIDs:

    * Header row optional — row 1 is skipped iff its first cell does not
      parse as an ESID.
    * Encoding: UTF-8 (BOM tolerated); falls back to cp1252.
    * Delimiter: comma; semicolon auto-detected from the first line.
    * Cells parse via :func:`parse_esid_cell` (Excel ``73.0`` floats,
      ``ESID 073`` prefixes).  Unparseable cells are logged as warnings
      and skipped; blank rows skipped; duplicates removed (order kept).

    Args:
        path: The CSV file to read.

    Returns:
        Zero-padded 3-digit ESID strings in first-seen order.

    Raises:
        ValueError: If the file is unreadable or yields no valid ESIDs —
            a bad filter file must fail the run, never silently expand
            to nothing (or worse, to "no filter").
    """
    try:
        try:
            content = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            content = path.read_text(encoding="cp1252")
            logger.warning("%s was not UTF-8; decoded as cp1252.", path)
    except OSError as exc:
        raise ValueError(f"Cannot read ESID file {path}: {exc}") from exc

    first_line = content.splitlines()[0] if content.splitlines() else ""
    delimiter = ";" if (";" in first_line and "," not in first_line) else ","

    esids: List[str] = []
    seen = set()
    skipped = 0
    for row_num, row in enumerate(csv.reader(io.StringIO(content),
                                             delimiter=delimiter), start=1):
        cell = row[0] if row else ""
        value, reason = parse_esid_cell(cell)
        if value is None:
            if reason == "blank":
                continue
            if row_num == 1:
                logger.debug("%s: row 1 (%r) treated as header.", path, cell)
                continue
            skipped += 1
            logger.warning(
                "%s row %d: cannot parse %r as an ESID (%s) — skipped.",
                path, row_num, cell, reason,
            )
            continue
        padded = f"{value:03d}"
        if padded not in seen:
            seen.add(padded)
            esids.append(padded)

    if not esids:
        raise ValueError(
            f"No valid ESIDs found in the first column of {path} — "
            "refusing to continue with an empty filter."
        )
    logger.info(
        "Loaded %d ESID(s) from %s%s.",
        len(esids), path,
        f" ({skipped} unparseable cell(s) skipped)" if skipped else "",
    )
    return esids


def load_esid_args(values: List[str]) -> List[str]:
    """Expand ``--esid`` values: literal numbers and/or CSV file paths.

    Each value is either a 1-3 digit ESID number or the path to a CSV
    whose first column lists ESIDs (header row optional) — e.g. the
    output of esid_record_report.py, list_upload_states.py, or
    esid_wav_inventory.py.  Both kinds may be mixed in one command.

    Disambiguation is deterministic: a purely numeric token is ALWAYS an
    ESID (never a path, even if a file of that name exists); any other
    token must be an existing file.

    Args:
        values: The raw ``--esid`` argument list.

    Returns:
        Zero-padded 3-digit ESID strings, deduplicated, in first-seen
        order.

    Raises:
        ValueError: If a token is neither a 1-3 digit number nor an
            existing file, or a file yields no valid ESIDs.
    """
    esids: List[str] = []
    seen = set()
    for value in values:
        token = str(value).strip()
        if token.isdigit() and 1 <= len(token) <= 3:
            expanded = [f"{int(token):03d}"]
        elif Path(token).is_file():
            expanded = _esids_from_csv_first_column(Path(token))
        else:
            raise ValueError(
                f"--esid value {token!r} is neither a 1-3 digit ESID "
                "number nor an existing spreadsheet file."
            )
        for padded in expanded:
            if padded not in seen:
                seen.add(padded)
                esids.append(padded)
    return esids


# ---------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------

def calculate_digests(filepath: str, algorithms: Tuple[str, ...]) -> dict:
    """Compute several digests of a file in ONE streaming read.

    Multi-GB ZIPs used to be read end-to-end once per algorithm (SHA-512
    for the manifest check, md5 for the Zenodo checksum) — feeding every
    hasher from the same 64 KB chunk loop halves that disk traffic.

    Args:
        filepath: Path to the file.
        algorithms: hashlib algorithm names, e.g. ``("sha512", "md5")``.

    Returns:
        Dict mapping each algorithm name to its hex digest.
    """
    hashers = {name: hashlib.new(name) for name in algorithms}
    with open(filepath, "rb") as fh:
        for chunk in iter(lambda: fh.read(HASH_BUFFER_SIZE), b""):
            for hasher in hashers.values():
                hasher.update(chunk)
    return {name: hasher.hexdigest() for name, hasher in hashers.items()}


def calculate_sha512(filepath: str) -> str:
    """Calculate the SHA-512 hash of a file, streaming in 64 KB chunks.

    Args:
        filepath: Path to the file.

    Returns:
        Hex-encoded SHA-512 digest string.
    """
    return calculate_digests(filepath, ("sha512",))["sha512"]


def configure_logging(verbose: bool = False) -> None:
    """Set up the standard AZUS log format on stdout.

    Args:
        verbose: When True, log at DEBUG instead of INFO.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def timestamped_output_path(prefix: str) -> Path:
    """Default report path: ``<prefix>_YYYYMMDD_HHMMSS.csv`` in the cwd.

    The timestamp keeps repeated runs from clobbering each other —
    reports are audit artifacts worth keeping side by side.

    Args:
        prefix: Report name prefix, e.g. ``"wav_integrity_report"``.

    Returns:
        The timestamped path in the current working directory.
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path.cwd() / f"{prefix}_{stamp}.csv"
