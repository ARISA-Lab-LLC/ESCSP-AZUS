#!/usr/bin/env python3
"""Re-run prepare_dataset.py on every ESID an audit found incomplete
IN STAGING (never uploaded) — automates step 2a of the remediation
workflow described in Guides/UPLOAD_RECOVERY_WORKFLOW.md.

WHAT THIS TOOL DOES
====================
Takes the CSV report produced by ``audit_prep_completeness.py`` and,
for every row marked ``Prep Completed = No`` whose ESID has NOT already
been uploaded to Zenodo (empty ``Uploaded Data`` column), re-runs
``prepare_dataset.py`` on that ESID's raw folder — exactly the command
you would type by hand, looped over every broken row in the report.

WHY "STAGING ONLY" — THE UPLOADED-DATA CASE IS DELIBERATELY SKIPPED
=====================================================================
A row with ``Prep Completed = No`` AND a non-empty ``Uploaded Data``
column means that ESID was already uploaded to Zenodo with a broken
ZIP.  Re-running prepare_dataset.py there only rebuilds a local
staging folder — it does nothing to the already-published Zenodo
record (published files are immutable; fixing them requires uploading
a new version, a decision this tool will not make for you).  Those
rows are always logged at WARNING and left untouched, never re-prepped
automatically.

Rows with ``Prep Completed = Ambiguous`` are also left untouched —
"Ambiguous" means the audit could not determine completeness (e.g. a
corrupt ZIP it couldn't even list, or a conditional file that might be
intentionally absent), not that the folder is confirmed broken.

USAGE
=====
::

    # 1. Generate the audit report (see audit_prep_completeness.py):
    python Resources/audit_prep_completeness.py /path/to/Raw_Data \\
        --audit-all --output prep_completeness_report.csv

    # 2. Re-prep everything that report found broken in staging:
    python Resources/reprep_incomplete_staging.py \\
        prep_completeness_report.csv /path/to/Raw_Data \\
        --config Resources/config.json --eclipse-type total

    # Preview without changing anything:
    python Resources/reprep_incomplete_staging.py \\
        prep_completeness_report.csv /path/to/Raw_Data --dry-run

EXIT CODES
==========
* ``0`` — every re-prep attempted succeeded (or there was nothing to do)
* ``1`` — at least one re-prep failed
* ``2`` — usage error (missing report CSV / raw folder, malformed report)
"""

from __future__ import annotations

import argparse
import csv
import logging
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

# Sibling modules in Resources/ — reused for raw-folder discovery so
# this tool never re-derives the ESID parsing rules independently.
import audit_prep_completeness as audit
import azus_common

logger = logging.getLogger("azus.reprep")

_PROJECT_ROOT = azus_common.PROJECT_ROOT


def read_report(report_path: Path) -> List[Dict[str, str]]:
    """Read the 4-column CSV written by audit_prep_completeness.py."""
    with report_path.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        required = {"ESID#", "Staging Area", "Uploaded Data", "Prep Completed"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"{report_path} is missing column(s) {sorted(missing)} — "
                "is this a report from audit_prep_completeness.py?"
            )
        return list(reader)


def run_prepare_dataset(
    esid_folder: Path, config_path: str, eclipse_type: str
) -> int:
    """Invoke prepare_dataset.py as a subprocess; return its exit code.

    Same invocation pattern as prep_all_datasets.run_prepare_dataset():
    same interpreter, cwd pinned to the project root (config_path is a
    relative path by default), output streamed live rather than
    captured.
    """
    cmd = [
        sys.executable,
        str(_PROJECT_ROOT / "Resources" / "prepare_dataset.py"),
        str(esid_folder),
        "--config", config_path,
        "--eclipse-type", eclipse_type,
    ]
    logger.info("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, cwd=str(_PROJECT_ROOT))
    return result.returncode


def main() -> None:
    """Command-line entry point.  See module docstring for usage."""
    parser = argparse.ArgumentParser(
        description=(
            "Re-run prepare_dataset.py on every ESID an "
            "audit_prep_completeness.py report found incomplete in "
            "Staging_Area/ (never-uploaded ESIDs only)."
        ),
    )
    parser.add_argument(
        "report_csv",
        help="Path to the CSV written by audit_prep_completeness.py.",
    )
    parser.add_argument(
        "raw_data_dir",
        help=(
            "Path to the folder containing raw ESID subdirectories — the "
            "same folder passed to audit_prep_completeness.py."
        ),
    )
    parser.add_argument(
        "--config", default="Resources/config.json",
        help="Path to config.json, forwarded to prepare_dataset.py.",
    )
    parser.add_argument(
        "--eclipse-type", choices=["total", "annular", "partial"],
        default="total",
        help="Forwarded to prepare_dataset.py (default: total).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List what would be re-prepped without running anything.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    report_path = Path(args.report_csv)
    if not report_path.is_file():
        logger.error("Report CSV not found: %s", report_path)
        sys.exit(2)

    raw_data_dir = Path(args.raw_data_dir)
    if not raw_data_dir.is_dir():
        logger.error("Raw-data folder not found or not a directory: %s", raw_data_dir)
        sys.exit(2)

    try:
        rows = read_report(report_path)
    except ValueError as exc:
        logger.error("%s", exc)
        sys.exit(2)

    raw_by_esid = {
        padded: folder for _, padded, folder in audit.find_raw_esid_folders(raw_data_dir)
    }

    to_reprep: List[str] = []
    skipped_uploaded: List[str] = []
    skipped_no_raw_folder: List[str] = []

    for row in rows:
        if row["Prep Completed"] != "No":
            continue  # Yes / Ambiguous — not this tool's job
        esid = row["ESID#"]
        if row["Uploaded Data"]:
            # Already on Zenodo with a broken ZIP — needs a reviewed
            # new-version upload, never automated here.
            skipped_uploaded.append(esid)
            logger.warning(
                "[ESID %s] SKIPPED — already uploaded (%s) with "
                "Prep Completed=No. This ESID's Zenodo record needs a "
                "manually reviewed new-version upload; re-prepping here "
                "would not fix what is already published.",
                esid, row["Uploaded Data"],
            )
            continue
        if esid not in raw_by_esid:
            skipped_no_raw_folder.append(esid)
            logger.warning(
                "[ESID %s] SKIPPED — no matching raw folder found under %s",
                esid, raw_data_dir,
            )
            continue
        to_reprep.append(esid)

    logger.info("=" * 70)
    logger.info("RE-PREP PLAN")
    logger.info("=" * 70)
    logger.info("Report:            %s (%d row(s))", report_path, len(rows))
    logger.info("To re-prep:        %d ESID(s): %s", len(to_reprep), ", ".join(to_reprep) or "(none)")
    if skipped_uploaded:
        logger.info(
            "Skipped (already uploaded, needs manual review): %s",
            ", ".join(skipped_uploaded),
        )
    if skipped_no_raw_folder:
        logger.info("Skipped (no raw folder found): %s", ", ".join(skipped_no_raw_folder))
    logger.info("=" * 70)

    if not to_reprep:
        logger.info("Nothing to re-prep. Exiting.")
        sys.exit(0)

    if args.dry_run:
        logger.info("--dry-run: no changes made.")
        sys.exit(0)

    succeeded: List[str] = []
    failed: List[str] = []
    for esid in to_reprep:
        raw_folder = raw_by_esid[esid]
        logger.info("=" * 70)
        logger.info("RE-PREPPING ESID %s (%s)", esid, raw_folder)
        logger.info("=" * 70)
        returncode = run_prepare_dataset(raw_folder, args.config, args.eclipse_type)
        if returncode == 0:
            succeeded.append(esid)
            logger.info("[ESID %s] Re-prep succeeded.", esid)
        else:
            failed.append(esid)
            logger.error(
                "[ESID %s] Re-prep FAILED (exit %d) — see log above.",
                esid, returncode,
            )

    logger.info("=" * 70)
    logger.info("RE-PREP SUMMARY")
    logger.info("=" * 70)
    logger.info("Succeeded: %d — %s", len(succeeded), ", ".join(succeeded) or "(none)")
    logger.info("Failed:    %d — %s", len(failed), ", ".join(failed) or "(none)")
    logger.info("=" * 70)

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
