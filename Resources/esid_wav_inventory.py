#!/usr/bin/env python3
"""Per-folder WAV inventory for raw ``ESID#NNN`` directories — CSV report.

PURPOSE
=======
Scan the immediate subfolders of a directory and, for every folder
named ``ESID#NNN`` (a ``#``, then exactly three digits, nothing else;
case-insensitive — ``ESID_073``, ``ESID_073_Staging``, ``ESID#73`` and
other variants are deliberately ignored), summarize its top-level WAV
files into one CSV row:

    ESID#, Number of Wave files, Total size of wave files (GB),
    Number of Wave files with timestamps of 2023 or greater

The timestamp column counts files whose FILENAME carries a valid
recording date — the ``YYYYMMDD_HHMMSS.WAV`` convention the rest of the
pipeline trusts — with a year at or above the threshold (default 2023,
matching ``minimum_recording_year`` in the project config).  Filesystem
modification times are deliberately NOT used.  Names that do not parse
(hex-named files from old AudioMoth firmware) or parse to an earlier
year (``19700101_*`` from an unset clock) are counted in the file total
but NOT in the timestamp column — so the gap between the two columns
measures exactly the files that fail the upload pipeline's
recording-date check.

Follows the suite conventions: top-level files only (not recursive),
macOS ``._*`` AppleDouble sidecars excluded, exact byte sizes summed
before conversion to GB.

USAGE
=====
::

    python Resources/esid_wav_inventory.py /path/to/Raw_Data
        [--min-year 2023]
        [--output PATH]   # default: esid_wav_inventory_YYYYMMDD_HHMMSS.csv
        [--verbose]

EXIT CODES
==========
* ``0`` — report written
* ``2`` — usage error (directory missing)
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import azus_common

logger = logging.getLogger("azus.wav_inventory")

# Raw-folder hash syntax only: "ESID#" + exactly three digits, full
# match, any letter case.  Deliberately stricter than
# azus_common.parse_esid (which accepts ESID_073, suffixes like
# _Staging, and unpadded numbers) — this tool inventories the raw
# hash-named folders and nothing else.
_EXACT_ESID_FOLDER_RE = re.compile(r"^ESID#(\d{3})$", re.IGNORECASE)

_CSV_COLUMNS = [
    "ESID#",
    "Number of Wave files",
    "Total size of wave files (GB)",
    "Number of Wave files with timestamps of 2023 or greater",
]

_DEFAULT_MIN_YEAR = 2023
_BYTES_PER_GB = 1024 ** 3


def filename_year(wav_name: str) -> int | None:
    """Parse the recording year from a ``YYYYMMDD_HHMMSS.WAV`` name.

    Filesystem mtimes are deliberately not consulted — the filename
    timestamp is the pipeline's authoritative recording time.

    Args:
        wav_name: The WAV file's basename.

    Returns:
        The year as an int when the leading token is a valid calendar
        date, else None (hex-named files, malformed names).
    """
    token = Path(wav_name).stem.split("_")[0]
    try:
        return datetime.strptime(token, "%Y%m%d").year
    except ValueError:
        return None


def summarize_folder(folder: Path, min_year: int) -> Dict[str, str]:
    """Build one CSV row for a single ``ESID#NNN`` folder.

    Args:
        folder: The raw ESID folder (name already validated).
        min_year: Threshold for the timestamp column (inclusive).

    Returns:
        Row dict keyed by ``_CSV_COLUMNS``.
    """
    count = 0
    total_bytes = 0
    recent = 0
    for entry in sorted(folder.iterdir()):
        if not entry.is_file():
            continue
        if not entry.name.lower().endswith(".wav"):
            continue
        if entry.name.startswith("._"):
            continue  # macOS AppleDouble sidecar, not a recording
        count += 1
        try:
            total_bytes += entry.stat().st_size
        except OSError as exc:
            logger.warning("Could not stat %s: %s", entry, exc)
        year = filename_year(entry.name)
        if year is not None and year >= min_year:
            recent += 1
    if count and recent < count:
        logger.info(
            "ESID %s: %d of %d WAV(s) lack a >=%d filename timestamp "
            "(unset clock or old-firmware naming).",
            folder.name, count - recent, count, min_year,
        )
    return {
        "ESID#": _EXACT_ESID_FOLDER_RE.match(folder.name).group(1),
        "Number of Wave files": str(count),
        "Total size of wave files (GB)": f"{total_bytes / _BYTES_PER_GB:.3f}",
        "Number of Wave files with timestamps of 2023 or greater": str(recent),
    }


def find_exact_esid_folders(root: Path) -> List[Path]:
    """Immediate subfolders of ``root`` named exactly ``ESID#NNN``.

    Args:
        root: Directory containing raw ESID folders.

    Returns:
        Matching folders sorted by ESID number.  Non-matching entries
        (including ``ESID_NNN`` underscore variants and staging folders)
        are logged at DEBUG and skipped.
    """
    matches: List[Path] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if _EXACT_ESID_FOLDER_RE.match(entry.name):
            matches.append(entry)
        else:
            logger.debug("Skipping (not ESID#NNN syntax): %s", entry.name)
    matches.sort(key=lambda p: int(_EXACT_ESID_FOLDER_RE.match(p.name).group(1)))
    return matches


def main() -> None:
    """Command-line entry point.  See the module docstring for details."""
    parser = argparse.ArgumentParser(
        description=(
            "Inventory the WAV files of every folder named ESID#NNN "
            "(case-insensitive) inside a directory — count, total GB, "
            "and how many carry a filename timestamp (YYYYMMDD_HHMMSS) "
            "at or above the minimum year."
        ),
    )
    parser.add_argument(
        "directory",
        help="Folder containing the raw ESID#NNN subfolders.",
    )
    parser.add_argument(
        "--min-year", type=int, default=_DEFAULT_MIN_YEAR, metavar="YYYY",
        help=(
            "Inclusive year threshold for the filename-timestamp column "
            f"(default: {_DEFAULT_MIN_YEAR}, matching the project "
            "config's minimum_recording_year)."
        ),
    )
    parser.add_argument(
        "--output", default=None, metavar="PATH",
        help=(
            "Where to write the CSV (default: "
            "esid_wav_inventory_YYYYMMDD_HHMMSS.csv in the current "
            "directory)."
        ),
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Also log every skipped non-matching folder name.",
    )
    args = parser.parse_args()

    azus_common.configure_logging(verbose=args.verbose)

    root = Path(args.directory)
    if not root.is_dir():
        logger.error("Not a directory: %s", root)
        sys.exit(2)

    output_path = (
        Path(args.output)
        if args.output
        else azus_common.timestamped_output_path("esid_wav_inventory")
    )

    folders = find_exact_esid_folders(root)
    logger.info(
        "Found %d folder(s) matching ESID#NNN syntax in %s",
        len(folders), root,
    )

    rows = [summarize_folder(folder, args.min_year) for folder in folders]

    with output_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    logger.info("Report written: %s (%d row(s))", output_path, len(rows))

    total_wavs = sum(int(r["Number of Wave files"]) for r in rows)
    total_recent = sum(
        int(r["Number of Wave files with timestamps of 2023 or greater"])
        for r in rows
    )
    total_gb = sum(float(r["Total size of wave files (GB)"]) for r in rows)
    logger.info("=" * 70)
    logger.info("SUMMARY")
    logger.info("=" * 70)
    logger.info("ESID#NNN folders:            %d", len(rows))
    logger.info("WAV files total:             %d", total_wavs)
    logger.info("WAV data total:              %.3f GB", total_gb)
    logger.info("WAVs with >=%d timestamps:  %d", args.min_year, total_recent)
    if total_recent < total_wavs:
        logger.warning(
            "%d WAV(s) lack a >=%d filename timestamp — those folders "
            "will fail the upload pipeline's recording-date check.",
            total_wavs - total_recent, args.min_year,
        )
    logger.info("=" * 70)
    sys.exit(0)


if __name__ == "__main__":
    main()
