#!/usr/bin/env python3
"""List every upload_state.json in Staging_Area/ and Uploaded_Data/.

WHAT THIS TOOL DOES
===================
``upload_state.json`` is written into an ESID's folder the moment its
Zenodo draft is created, and it travels with the folder for the rest of
its life.  It records WHICH Zenodo record the pipeline claims for that
ESID.  That makes these files the local source of truth when
investigating duplicates or stuck uploads:

  * Folder in ``Staging_Area/`` WITH the file  → draft exists on
    Zenodo, upload incomplete (stuck or deferred).
  * Folder in ``Staging_Area/`` WITHOUT it     → no draft yet; the
    next upload run will create a fresh record.
  * Folder in ``Uploaded_Data/``               → completed upload; the
    file says which record it became.

This tool walks both directories and writes one CSV row per state file
found: ESID, location, record id, Zenodo URL, when the draft was
created/resumed, and a note for anything unreadable.  Cross-reference
the ``Record ID`` column against ``find_duplicate_records.py`` output:
a Zenodo record for an ESID whose id does NOT appear here is a stray.

READ-ONLY: nothing is modified, nothing touches the network.

USAGE
=====
From the project root:

    python Resources/list_upload_states.py

    # Custom output path
    python Resources/list_upload_states.py --output states.csv

EXIT CODES
==========
    0  scan completed; every state file found was readable
    1  at least one state file was unreadable or lacked a record_id
    2  usage error (neither directory exists)
"""

import argparse
import csv
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger("azus.state_list")

# This file lives in Resources/; the project root is one level up.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SCAN_DIRS = (
    ("Staging", _PROJECT_ROOT / "Staging_Area"),
    ("Uploaded", _PROJECT_ROOT / "Uploaded_Data"),
)

_STATE_FILENAME = "upload_state.json"

# Same folder-name convention as the other Resources/ tools.
_ESID_FOLDER_RE = re.compile(r"^ESID[_#](\d+)", re.IGNORECASE)

_CSV_COLUMNS = [
    "ESID#",
    "Location",
    "Folder",
    "Record ID",
    "Zenodo URL",
    "State Created",
    "Resumed",
    "Notes",
]


def scan_directory(location: str, directory: Path) -> List[Dict[str, str]]:
    """Return one CSV row dict per upload_state.json under one directory.

    ESID folders without a state file are counted and logged (they are
    normal for not-yet-uploaded datasets) but get no CSV row — the CSV
    lists state files, per the tool's contract.
    """
    rows: List[Dict[str, str]] = []
    without_state = 0

    if not directory.is_dir():
        logger.warning("%s directory does not exist: %s", location, directory)
        return rows

    for entry in sorted(directory.iterdir()):
        if not entry.is_dir():
            continue
        m = _ESID_FOLDER_RE.match(entry.name)
        if m is None:
            logger.warning("Skipping non-ESID subfolder: %s", entry.name)
            continue
        esid_padded = f"{int(m.group(1)):03d}"

        state_file = entry / _STATE_FILENAME
        if not state_file.is_file():
            without_state += 1
            logger.debug("No %s in %s", _STATE_FILENAME, entry.name)
            continue

        row = {
            "ESID#": esid_padded,
            "Location": location,
            "Folder": entry.name,
            "Record ID": "",
            "Zenodo URL": "",
            "State Created": "",
            "Resumed": "",
            "Notes": "",
        }
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            row["Record ID"] = str(state.get("record_id") or "")
            row["Zenodo URL"] = str(state.get("zenodo_url") or "")
            row["State Created"] = str(state.get("created_at") or "")
            row["Resumed"] = str(state.get("resumed", ""))
            if not row["Record ID"]:
                row["Notes"] = "state file has no record_id"
        except (OSError, json.JSONDecodeError) as exc:
            row["Notes"] = f"unreadable state file: {exc}"
        rows.append(row)

    logger.info(
        "%s: %d state file(s) found, %d ESID folder(s) without one",
        location, len(rows), without_state,
    )
    return rows


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(
        description=(
            "Scan Staging_Area/ and Uploaded_Data/ for upload_state.json "
            "files and list them in a CSV (ESID, location, Zenodo record "
            "id/URL, creation time). Read-only."
        ),
    )
    parser.add_argument(
        "--output", metavar="PATH", default=None,
        help=(
            "Where to write the CSV (default: "
            "upload_states_report_YYYYMMDD_HHMMSS.csv in the current "
            "directory)."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    output_path = (
        Path(args.output)
        if args.output
        else Path.cwd() / datetime.now().strftime(
            "upload_states_report_%Y%m%d_%H%M%S.csv"
        )
    )

    logger.info("=" * 70)
    logger.info("AZUS UPLOAD-STATE LISTING (read-only)")
    logger.info("=" * 70)
    for location, directory in _SCAN_DIRS:
        logger.info("%-9s %s", location + ":", directory)
    logger.info("Output:   %s", output_path)
    logger.info("=" * 70)

    if not any(directory.is_dir() for _, directory in _SCAN_DIRS):
        logger.error(
            "Neither Staging_Area/ nor Uploaded_Data/ exists under %s — "
            "run this from a machine that has the AZUS data tree.",
            _PROJECT_ROOT,
        )
        sys.exit(2)

    rows: List[Dict[str, str]] = []
    for location, directory in _SCAN_DIRS:
        rows.extend(scan_directory(location, directory))

    # Numeric ESID order, Staging before Uploaded within one ESID.
    rows.sort(key=lambda r: (int(r["ESID#"]), r["Location"] != "Staging"))

    try:
        with open(output_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
    except OSError as exc:
        logger.error("Could not write report to %s: %s", output_path, exc)
        sys.exit(2)
    logger.info("Report written: %s (%d row(s))", output_path, len(rows))

    for row in rows:
        logger.info(
            "  ESID %s  %-8s  record %-10s %s%s",
            row["ESID#"], row["Location"],
            row["Record ID"] or "-", row["Zenodo URL"],
            f"  [{row['Notes']}]" if row["Notes"] else "",
        )

    anomalies = [r for r in rows if r["Notes"]]
    staging_count = sum(1 for r in rows if r["Location"] == "Staging")
    logger.info("=" * 70)
    logger.info("SUMMARY")
    logger.info("=" * 70)
    logger.info("State files listed:   %d", len(rows))
    logger.info("  in Staging_Area/:   %d (drafts with incomplete uploads)",
                staging_count)
    logger.info("  in Uploaded_Data/:  %d (completed uploads)",
                len(rows) - staging_count)
    logger.info("Anomalies:            %d%s",
                len(anomalies),
                " — see Notes column" if anomalies else "")
    logger.info("Report: %s", output_path)
    logger.info("=" * 70)

    sys.exit(1 if anomalies else 0)


if __name__ == "__main__":
    main()
