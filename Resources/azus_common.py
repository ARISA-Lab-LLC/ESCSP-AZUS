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
import json
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

# Value of upload_state.json's optional "mode" key when an ESID has been
# switched from ZIP-archive upload to per-file upload (individual WAVs +
# CONFIG.TXT instead of the ZIP). The ZIP-based pipeline
# (standalone_tasks.py) SKIPS any staging folder marked this way — only
# the file-by-file tool (finish_stuck_uploads.py --enable-file-by-file)
# finishes it — so the two never fight over the same Zenodo record.
FILE_BY_FILE_MODE = "file_by_file"

# Read-buffer size for streaming file hashes (64 KB).
HASH_BUFFER_SIZE = 65_536

# ---------------------------------------------------------------------
# ESID parsing — THE single definition of a valid ESID token
# ---------------------------------------------------------------------
# Project invariant: an ESID is EXACTLY three digits (000-999) plus an
# OPTIONAL suffix of letters/digits/underscores that starts with a
# non-digit — e.g. 073, 120A, 122_Part_1_of_2.  (The non-digit start
# keeps 4-digit runs like 0733 or 2024 malformed, exactly as before.)
# The canonical form is the zero-padded digits followed by the suffix
# verbatim (case preserved), written in folder/file names as ESID_073,
# ESID#120A, ESID_122_Part_1_of_2_Staging, etc.  In record titles and
# other human-facing text the underscores render as spaces
# ("ESID#122 Part 1 of 2") — see :func:`esid_display`.
# (Title-pattern matching in the Zenodo report tools is deliberately
# separate — record titles have their own user-configurable patterns.)
_ESID_NAME_RE = re.compile(
    r"^ESID[_#](\d{1,3})(?!\d)([A-Za-z_][A-Za-z0-9_]*)?$", re.IGNORECASE
)

# Same grammar for BARE ids (no "ESID" prefix): CLI tokens, CSV cells,
# and the collectors CSV's ESID column.
_ESID_TOKEN_RE = re.compile(r"^(\d{1,3})(?!\d)([A-Za-z_][A-Za-z0-9_]*)?$")

# A file name's extension, stripped before a name is parsed.
_NAME_EXTENSION_RE = re.compile(r"\.[A-Za-z0-9]+$")

# Template tails the name-building code appends AFTER the ESID
# (ESID_073_Staging, ESID_073.zip, ESID_073_to_upload.csv,
# ESID_073_metadata.json, ESID_073_request_log.json).  One tail is
# stripped case-insensitively before parsing so that
# "ESID_122_Part_1_of_2_Staging" yields the ESID "122_Part_1_of_2",
# not a suffix that ends in "_Staging".
_RESERVED_NAME_TAILS = (
    "_staging", "_uploaded", "_to_upload", "_metadata", "_request_log",
)


def parse_esid(name: str) -> Optional[str]:
    """Extract the canonical ESID from a folder/file name.

    Accepts every naming variant the pipeline produces: ``ESID_073``,
    ``ESID#73``, ``ESID_4``, ``ESID_073_Staging``, ``ESID_073.zip``,
    and suffixed ESIDs such as ``ESID_120A`` or
    ``ESID_122_Part_1_of_2_Staging``.  The grammar is enforced strictly:
    a digit run longer than three (``ESID_0733``) is malformed, and a
    suffixed ESID must start with the full three digits (``ESID_12A``
    is malformed, ``ESID_012A`` is not) — malformed names return None
    rather than being silently reinterpreted.

    Args:
        name: A folder or file basename (not a full path).

    Returns:
        The canonical ESID string — zero-padded 3 digits plus any
        suffix verbatim (``"073"``, ``"120A"``, ``"122_Part_1_of_2"``)
        — or None when the name carries no valid ESID token.
    """
    stem = _NAME_EXTENSION_RE.sub("", name.strip())
    lowered = stem.lower()
    for tail in _RESERVED_NAME_TAILS:
        if lowered.endswith(tail):
            stem = stem[: -len(tail)]
            break
    m = _ESID_NAME_RE.match(stem)
    if m is None:
        return None
    digits, suffix = m.group(1), m.group(2) or ""
    if suffix and len(digits) != 3:
        return None
    return f"{int(digits):03d}{suffix}"


def normalize_esid(raw: object) -> str:
    """Normalize a bare ESID value (CLI token, CSV cell) to canonical form.

    Pure 1-3 digit values are zero-padded (``4`` → ``"004"``); suffixed
    values must already start with the full three digits (``120A``,
    ``122_Part_1_of_2``).  Runs of whitespace collapse to single
    underscores first, so the display form ``"122 Part 1 of 2"`` also
    normalizes to ``"122_Part_1_of_2"``.

    Args:
        raw: The raw value (any type; coerced to ``str`` and stripped).

    Returns:
        The canonical ESID string: zero-padded 3 digits plus any suffix
        verbatim (case preserved).

    Raises:
        ValueError: If the value does not fit the ESID grammar —
            including 4+ digit numbers (``1234``) and suffixed values
            without the full 3-digit start (``12A``).
    """
    text = str(raw).strip() if raw is not None else ""
    text = re.sub(r"\s+", "_", text)
    m = _ESID_TOKEN_RE.match(text)
    if m is None:
        raise ValueError(
            f"{raw!r} is not a valid ESID: expected 1-3 digits, or "
            "exactly 3 digits followed by a suffix of letters, digits, "
            "and underscores starting with a non-digit (e.g. 004, 120A, "
            "122_Part_1_of_2)."
        )
    digits, suffix = m.group(1), m.group(2) or ""
    if suffix and len(digits) != 3:
        raise ValueError(
            f"{raw!r} is not a valid ESID: a suffixed ESID must start "
            "with the full 3-digit number (e.g. 012A, not 12A)."
        )
    return f"{int(digits):03d}{suffix}"


def esid_sort_key(esid: str) -> Tuple[int, str]:
    """Sort key that orders ESIDs numerically, then by suffix.

    ``120`` sorts before ``120A``, which sorts before ``121`` — the
    order every report and discovery scan uses.  (For canonical ESIDs
    this equals plain string order, but unlike ``int(esid)`` it never
    crashes on a suffixed id.)

    Args:
        esid: A canonical ESID string (3-digit prefix + optional
            suffix); bare 1-3 digit strings are also accepted.

    Returns:
        A ``(numeric_part, suffix)`` tuple suitable for ``sorted(key=)``.
    """
    return (int(esid[:3]), esid[3:])


def esid_display(esid: str) -> str:
    """Render an ESID for record titles and other human-facing text.

    Underscores become spaces (``122_Part_1_of_2`` → ``122 Part 1 of
    2``); a plain 3-digit ESID is returned unchanged, so existing
    record titles are unaffected.  Folder, ZIP, and report filenames
    always keep the underscored canonical form — this helper is for
    display text only.

    Args:
        esid: A canonical ESID string.

    Returns:
        The display form with underscores replaced by spaces.
    """
    return esid.replace("_", " ")


def find_esid_folders(root: Path) -> List[Tuple[Tuple[int, str], str, Path]]:
    """Find every ESID subdirectory of ``root``, in ESID order.

    Non-ESID directories (``.DS_Store`` siblings, ``backup``, etc.) are
    silently ignored.  The canonical form is the zero-padded 3-digit
    number plus any suffix (``073``, ``120A``, ``122_Part_1_of_2``).

    Args:
        root: Parent folder containing ESID directories.

    Returns:
        List of ``(sort_key, canonical_esid, folder_path)`` tuples in
        ascending :func:`esid_sort_key` order (numeric part first, then
        suffix).  Empty when ``root`` is not a directory or has no
        matching subdirectories.
    """
    found: List[Tuple[Tuple[int, str], str, Path]] = []
    if not root.is_dir():
        return found
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        canonical = parse_esid(entry.name)
        if canonical is None:
            continue
        found.append((esid_sort_key(canonical), canonical, entry))
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
_ESID_PREFIXED_RE = re.compile(
    r"ESID[\s_#-]*(\d{1,3})(?!\d)([A-Za-z_][A-Za-z0-9_]*)?", re.IGNORECASE
)


def parse_esid_cell(raw: object) -> Tuple[Optional[str], str]:
    """Parse one spreadsheet cell into a canonical ESID, or explain why not.

    Accepts bare ids (``73``, ``73.0`` Excel floats, ``120A``,
    ``122_Part_1_of_2``, the display form ``122 Part 1 of 2``) and
    ESID-prefixed cells (``ESID 073``, ``ESID#120A``).  Never guesses:
    a cell with several unrelated numbers is only accepted when exactly
    one of them is ESID-prefixed (e.g. ``"ESID 073 (2024)"`` -> 073).

    Args:
        raw: The raw cell value (any type; coerced to ``str`` and
            stripped before parsing).

    Returns:
        A ``(esid, "ok")`` tuple on success — ``esid`` is the canonical
        string (zero-padded 3 digits plus any suffix) — else
        ``(None, reason)`` where ``reason`` is ``"blank"`` for empty
        cells or a human-readable explanation of why the cell could not
        be parsed.
    """
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return None, "blank"

    m = _EXCEL_FLOAT_RE.match(text)
    if m:  # Excel numeric-export artifact: "73.0" means 73, not 730
        text = m.group(1)

    # Whole-cell ESID grammar first: bare numbers and suffixed ids
    # (whitespace collapses to underscores inside normalize_esid).
    try:
        return normalize_esid(text), "ok"
    except ValueError:
        pass

    collapsed = re.sub(r"\s+", "_", text)

    # A bare cell that LOOKS like a suffixed id but fails the grammar
    # (12A, 0733_backup) is rejected, never silently truncated to its
    # digit run.
    if re.fullmatch(r"\d+[A-Za-z_][A-Za-z0-9_]*", collapsed):
        return None, (
            f"suffixed ESID {text!r} must start with the full 3-digit "
            "number (e.g. 012A, not 12A)"
        )

    groups = _DIGIT_GROUP_RE.findall(text)
    if not groups:
        return None, f"no digits found in {text!r}"

    # ESID-prefixed form next ("ESID 073", "ESID#120A", "ESID 122 Part
    # 1 of 2") — checked BEFORE the bare digit-group fallback so a
    # prefixed suffix is never dropped.
    prefixed = _ESID_PREFIXED_RE.findall(collapsed)
    if len(prefixed) == 1:
        digits, suffix = prefixed[0]
        # An "ESID 073 (2024)" style cell picks up a dangling "_" from
        # the collapsed separator — meaningless in a suffix, drop it.
        suffix = suffix.rstrip("_")
        if suffix and len(digits) != 3:
            return None, (
                f"suffixed ESID in cell {text!r} must start with the "
                "full 3-digit number (e.g. 012A, not 12A)"
            )
        return f"{int(digits):03d}{suffix}", "ok"

    if len(groups) == 1:
        value = int(groups[0])
        if _ESID_MIN <= value <= _ESID_MAX:
            return f"{value:03d}", "ok"
        return None, f"out of range 000-999: {groups[0]!r}"

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
      ``ESID 073`` prefixes, suffixed ESIDs like ``120A``).
      Unparseable cells are logged as warnings and skipped; blank rows
      skipped; duplicates removed (order kept).

    Args:
        path: The CSV file to read.

    Returns:
        Canonical ESID strings (zero-padded 3 digits plus any suffix)
        in first-seen order.

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
        if value not in seen:
            seen.add(value)
            esids.append(value)

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
    """Expand ``--esid`` values: literal ESIDs and/or CSV file paths.

    Each value is either a literal ESID — a 1-3 digit number, or a
    suffixed id like ``120A`` / ``122_Part_1_of_2`` — or the path to a
    CSV whose first column lists ESIDs (header row optional), e.g. the
    output of esid_record_report.py, list_upload_states.py, or
    esid_wav_inventory.py.  Both kinds may be mixed in one command.

    Disambiguation is deterministic: a token that fits the ESID grammar
    is ALWAYS an ESID (never a path, even if a file of that name
    exists — real spreadsheet paths carry a ``.csv`` extension, and
    dots never fit the grammar); any other token must be an existing
    file.

    Args:
        values: The raw ``--esid`` argument list.

    Returns:
        Canonical ESID strings (zero-padded 3 digits plus any suffix),
        deduplicated, in first-seen order.

    Raises:
        ValueError: If a token is neither a valid ESID nor an existing
            file, or a file yields no valid ESIDs.
    """
    esids: List[str] = []
    seen = set()
    for value in values:
        token = str(value).strip()
        try:
            expanded: List[str] = [normalize_esid(token)]
        except ValueError:
            if Path(token).is_file():
                expanded = _esids_from_csv_first_column(Path(token))
            else:
                raise ValueError(
                    f"--esid value {token!r} is neither a valid ESID "
                    "(1-3 digits, or 3 digits plus a suffix like 120A "
                    "or 122_Part_1_of_2) nor an existing spreadsheet "
                    "file."
                ) from None
        for esid in expanded:
            if esid not in seen:
                seen.add(esid)
                esids.append(esid)
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


def read_upload_mode(staging_folder: Path) -> Optional[str]:
    """Read the optional ``"mode"`` from a staging folder's upload_state.json.

    The single source of truth for "is this ESID in file-by-file mode?",
    used by the ZIP pipeline's skip guards and by the file-by-file tool.
    Absent state file, missing key, or any read/parse error all yield
    ``None`` (treated as ordinary ZIP mode) — never raises.

    Args:
        staging_folder: The ``ESID_NNN_Staging`` folder to inspect.

    Returns:
        The ``"mode"`` string (e.g. :data:`FILE_BY_FILE_MODE`), or ``None``
        when no mode is recorded.
    """
    state_file = staging_folder / STATE_FILENAME
    if not state_file.is_file():
        return None
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        logger.warning(
            "Could not read upload mode from %s (%s) — assuming ZIP mode.",
            state_file, exc,
        )
        return None
    if not isinstance(data, dict):
        return None
    mode = data.get("mode")
    return mode if isinstance(mode, str) else None


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
