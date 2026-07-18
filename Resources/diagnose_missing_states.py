#!/usr/bin/env python3
"""Diagnose WHY Staging_Area/ folders are missing upload_state.json.

BACKGROUND
==========
``upload_state.json`` is written the moment a Zenodo draft is created.
``finish_stuck_uploads.py`` only attempts folders that HAVE the file —
folders without it are skipped at DEBUG level (invisible in normal
logs), so they are silently excluded from every recovery run.  A folder
with no state file therefore means one of two things: no run ever got
as far as creating a draft for it, or the file was created and later
destroyed (a re-prep wipes the whole folder).

This tool inspects every no-state folder in ``Staging_Area/``, gathers
all the evidence the pipeline leaves behind, and assigns a probable
cause.  Read-only by default.

EVIDENCE USED
=============
  * ``ESID_*.zip`` present in the folder?  (no ZIP → standalone_tasks
    skips the folder with NO logging at all)
  * ZIP listed in ``Records/uploaded_files.txt``?  (tracker skip —
    runs drop the dataset from the work list, aggregate log line only)
  * Row in the collectors CSV?  (no row → skipped with a warning and a
    "Unable to find data collector info" failure row)
  * ``ESID_XXX_metadata.json`` present?  (proves an attempt reached the
    upload phase)
  * ``ESID_XXX_request_log.json`` present?  (proves a DRAFT WAS CREATED
    — and contains its record_id.  These folders have a live draft on
    Zenodo that nothing points to; re-running without restoring the
    state file mints a DUPLICATE record.)
  * Rows in successful_results.csv / failed_results.csv
  * ``.prep_complete`` modification time (when the folder was last
    (re)prepped — a re-prep destroys state/request-log/metadata files)
  * Optional: per-ESID lines grepped from azus_upload.log (``--log``)

THE HEALER (opt-in)
===================
``--restore-states`` re-creates ``upload_state.json`` for folders whose
request log still holds the draft's record_id, re-linking them to their
existing Zenodo drafts so ``finish_stuck_uploads.py`` resumes them
instead of a future run creating duplicates.  Never overwrites an
existing state file.  Off by default; everything else is read-only.

USAGE
=====
From the project root:

    python Resources/diagnose_missing_states.py
    python Resources/diagnose_missing_states.py --log azus_upload.log
    python Resources/diagnose_missing_states.py --restore-states

EXIT CODES
==========
    0  every ESID folder in Staging_Area/ has upload_state.json
    1  no-state folders found and diagnosed (see CSV)
    2  usage error (Staging_Area/ missing, etc.)
"""

import argparse
import csv
import json
import logging
import re
import sys

import azus_common
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger("azus.state_diag")

# This file lives in Resources/; the project root is one level up.
_PROJECT_ROOT = azus_common.PROJECT_ROOT
_STAGING_AREA = azus_common.STAGING_AREA
_DEFAULT_CONFIG = _PROJECT_ROOT / "Resources" / "config.json"

_STATE_FILENAME = azus_common.STATE_FILENAME
_TRACKER_FILENAME = "uploaded_files.txt"

_ESID_ZIP_RE = re.compile(r"ESID[_#]?(\d+)\.zip$", re.IGNORECASE)

# Log lines worth quoting when --log is used: the verbatim strings the
# pipeline emits on each known pre-draft failure/skip path.
_LOG_KEYWORDS = (
    "Failed to build draft config",
    "No collector info found",
    "No collector data found",
    "Could not write upload state file",
    "Replacing existing staging folder",
    "Upload failed",
    "DONE (failed)",
)

_CSV_COLUMNS = [
    "ESID#",
    "Folder",
    "ZIP Present",
    "In Tracker",
    "Collector Row",
    "Metadata JSON",
    "Request Log Record ID",
    "Prior Success Row",
    "Latest Failure",
    "Prep Sentinel mtime",
    "Log Mentions",
    "Probable Cause",
    "Suggested Action",
]


@dataclass
class Evidence:
    """Everything we can learn about one no-state ESID folder."""

    esid: str
    folder: Path
    zip_present: bool = False
    in_tracker: bool = False
    collector_row: Optional[bool] = None      # None = collectors CSV unavailable
    metadata_json: bool = False
    request_log_record_id: str = ""
    request_log_path: Optional[Path] = None
    prior_success: bool = False
    latest_failure: str = ""
    prep_mtime: str = ""
    log_mentions: Optional[int] = None        # None = no --log given
    log_lines: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------
# Evidence gathering
# ---------------------------------------------------------------------

def _esids_from_results_csv(path: Path) -> Dict[int, str]:
    """Map numeric ESID -> latest error_message from a results CSV.

    Returns {} when the file is missing/unreadable (logged).  The CSVs
    are append-only, so the LAST row per ESID is the most recent.
    """
    results: Dict[int, str] = {}
    if not path.is_file():
        logger.warning("Results CSV not found (evidence unavailable): %s", path)
        return results
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                digits = re.sub(r"\D", "", str(row.get("esid", "")))
                if digits:
                    results[int(digits)] = str(row.get("error_message", "") or "")
    except (OSError, csv.Error) as exc:
        logger.warning("Could not read %s: %s", path, exc)
    return results


def _esids_from_tracker(path: Path) -> Set[int]:
    """Numeric ESIDs whose ZIPs appear in Records/uploaded_files.txt."""
    esids: Set[int] = set()
    if not path.is_file():
        logger.warning("Tracker not found (evidence unavailable): %s", path)
        return esids
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            m = _ESID_ZIP_RE.search(line.strip())
            if m:
                esids.add(int(m.group(1)))
    except OSError as exc:
        logger.warning("Could not read tracker %s: %s", path, exc)
    return esids


def _esids_from_collectors_csvs(csv_paths: List[Path]) -> Optional[Set[int]]:
    """Numeric ESIDs that have a row in ANY configured collectors CSV.

    Finds the ESID column by header substring match ("esid", case-
    insensitive) so we don't need the full parse_collectors_csv
    machinery.  Returns None when no CSV could be read at all —
    callers then report "unknown" instead of a false negative.
    """
    esids: Set[int] = set()
    any_readable = False
    for path in csv_paths:
        if not path.is_file():
            logger.warning("Collectors CSV not found: %s", path)
            continue
        try:
            with open(path, newline="", encoding="utf-8-sig") as fh:
                reader = csv.DictReader(fh)
                esid_col = next(
                    (c for c in (reader.fieldnames or [])
                     if c and "esid" in c.lower()),
                    None,
                )
                if esid_col is None:
                    logger.warning(
                        "No ESID-like column in %s (headers: %s)",
                        path, reader.fieldnames,
                    )
                    continue
                any_readable = True
                for row in reader:
                    digits = re.sub(r"\D", "", str(row.get(esid_col, "")))
                    if digits:
                        esids.add(int(digits))
        except (OSError, csv.Error) as exc:
            logger.warning("Could not read collectors CSV %s: %s", path, exc)
    return esids if any_readable else None


def _record_id_from_request_log(path: Path) -> str:
    """Extract record_id from ESID_XXX_request_log.json ('' if unreadable)."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return str(payload.get("record_id") or "")
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Request log unreadable %s: %s", path, exc)
        return ""


def _grep_logs(log_paths: List[Path], esid: str) -> Tuple[int, List[str]]:
    """Count lines mentioning the ESID across the given logs; keep the
    ones containing a known diagnostic keyword (max 5 quoted)."""
    needles = (f"ESID {esid}", f"ESID_{esid}", f"ESID {int(esid)}")
    count = 0
    keyword_lines: List[str] = []
    for log_path in log_paths:
        try:
            for line in log_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                if any(n in line for n in needles):
                    count += 1
                    if any(k in line for k in _LOG_KEYWORDS):
                        keyword_lines.append(line.strip())
        except OSError as exc:
            logger.warning("Could not read log %s: %s", log_path, exc)
    return count, keyword_lines[-5:]


def gather_evidence(
    esid: str,
    folder: Path,
    tracker_esids: Set[int],
    collector_esids: Optional[Set[int]],
    success_rows: Dict[int, str],
    failure_rows: Dict[int, str],
    log_paths: List[Path],
) -> Evidence:
    """Collect every piece of evidence for one no-state folder."""
    ev = Evidence(esid=esid, folder=folder)
    numeric = int(esid)

    ev.zip_present = any(folder.glob("ESID_*.zip")) or any(
        f for f in folder.iterdir()
        if f.is_file() and _ESID_ZIP_RE.search(f.name)
    )
    ev.in_tracker = numeric in tracker_esids
    ev.collector_row = (
        None if collector_esids is None else numeric in collector_esids
    )
    ev.metadata_json = any(folder.glob("ESID_*_metadata.json"))

    request_logs = sorted(folder.glob("ESID_*_request_log.json"))
    if request_logs:
        ev.request_log_path = request_logs[0]
        ev.request_log_record_id = _record_id_from_request_log(request_logs[0])

    ev.prior_success = numeric in success_rows
    ev.latest_failure = failure_rows.get(numeric, "")

    sentinel = folder / azus_common.PREP_SENTINEL
    if sentinel.is_file():
        ev.prep_mtime = datetime.fromtimestamp(
            sentinel.stat().st_mtime
        ).isoformat(timespec="seconds")

    if log_paths:
        ev.log_mentions, ev.log_lines = _grep_logs(log_paths, esid)

    return ev


# ---------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------

def classify(ev: Evidence) -> Tuple[str, str]:
    """Return (probable_cause, suggested_action) — most specific first.

    Order mirrors how the real pipeline works: artifacts proving a past
    attempt outrank static conditions, and the no-ZIP check outranks
    the tracker because standalone_tasks globs for the ZIP before the
    tracker filter ever sees it.
    """
    if ev.request_log_record_id:
        return (
            f"draft exists on Zenodo (record {ev.request_log_record_id}) "
            "but state file lost",
            "run with --restore-states, then finish_stuck_uploads.py",
        )
    if ev.request_log_path is not None:
        return (
            "request log present but unreadable — a draft was created "
            "but its record id is unrecoverable from the folder",
            "find the record in duplicate_records/find_duplicate_records.py "
            "output or the Zenodo web UI, then write upload_state.json by hand",
        )
    if ev.metadata_json:
        return (
            "attempt reached the upload phase; draft creation failed "
            "(network/API error before any record existed)",
            "check failed_results.csv / azus_upload.log, then re-run "
            "standalone_tasks.py --esid " + ev.esid,
        )
    if ev.prior_success:
        return (
            "re-prepped after a SUCCESSFUL upload — a record already "
            "exists on Zenodo; re-running would mint a duplicate",
            "cross-check find_duplicate_records.py before doing anything; "
            "if the record is complete, remove this staging folder",
        )
    if not ev.zip_present:
        return (
            "no ZIP in folder — standalone_tasks skips it with no "
            "logging at all",
            "re-run prepare_dataset.py for this ESID",
        )
    if ev.in_tracker:
        return (
            "tracker skip: ZIP listed in Records/uploaded_files.txt, so "
            "every run drops this ESID from the work list (aggregate "
            "log line only)",
            "verify on Zenodo; if the tracker entry is stale, delete its "
            "line from uploaded_files.txt to re-enable this ESID",
        )
    if ev.collector_row is False:
        return (
            "no row in the collectors CSV — skipped with 'Unable to "
            "find data collector info'",
            "add the ESID's row to the collectors CSV, then re-run",
        )
    if ev.latest_failure:
        return (
            f"attempt(s) failed before draft creation: {ev.latest_failure}",
            "fix the underlying error, then re-run standalone_tasks.py "
            "--esid " + ev.esid,
        )
    return (
        "no evidence of any upload attempt",
        "likely never covered by your --esid filters — run "
        "standalone_tasks.py --esid " + ev.esid + " explicitly",
    )


# ---------------------------------------------------------------------
# Restoration (opt-in)
# ---------------------------------------------------------------------

def restore_state(ev: Evidence) -> bool:
    """Write a fresh upload_state.json from the request log's record_id.

    Refuses to overwrite an existing state file.  Returns True when a
    file was written.
    """
    state_path = ev.folder / _STATE_FILENAME
    if state_path.exists():
        logger.warning(
            "[ESID %s] %s already exists — not overwriting", ev.esid,
            state_path,
        )
        return False
    if not ev.request_log_record_id:
        return False
    state = {
        "record_id": ev.request_log_record_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "zenodo_url": f"https://zenodo.org/uploads/{ev.request_log_record_id}",
        "resumed": False,
        "restored_from": ev.request_log_path.name,
        # Restoring the draft link is NOT an upload attempt — the counter
        # starts at its initial value and the next actual upload run
        # advances it to 1.
        "number_of_tries": 0,
    }
    try:
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.error("[ESID %s] Could not write %s: %s", ev.esid, state_path, exc)
        return False
    logger.info(
        "[ESID %s] RESTORED %s -> record %s (from %s)",
        ev.esid, state_path.name, ev.request_log_record_id,
        ev.request_log_path.name,
    )
    return True


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(
        description=(
            "For every ESID folder in Staging_Area/ WITHOUT "
            "upload_state.json, gather the evidence the pipeline leaves "
            "behind (tracker, results CSVs, collectors CSV, metadata/"
            "request-log artifacts, prep sentinel) and report the "
            "probable cause to a CSV. Read-only unless --restore-states."
        ),
    )
    parser.add_argument(
        "--config", default=str(_DEFAULT_CONFIG), metavar="PATH",
        help=(
            "AZUS config.json — supplies the collectors CSV path(s) and "
            f"the results-file locations (default: {_DEFAULT_CONFIG})"
        ),
    )
    parser.add_argument(
        "--log", action="append", default=[], metavar="PATH",
        help=(
            "azus_upload.log file to grep for per-ESID lines. May be "
            "given multiple times. Optional — without it the 'Log "
            "Mentions' column reads n/a."
        ),
    )
    parser.add_argument(
        "--output", default=None, metavar="PATH",
        help=(
            "Where to write the CSV (default: "
            "missing_state_diagnosis_YYYYMMDD_HHMMSS.csv in the current "
            "directory)."
        ),
    )
    parser.add_argument(
        "--restore-states", action="store_true",
        help=(
            "For folders whose request log still holds the draft's "
            "record_id, write a fresh upload_state.json pointing at that "
            "draft so finish_stuck_uploads.py can resume it (prevents "
            "duplicate records). Never overwrites an existing state "
            "file. Everything else stays read-only."
        ),
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Quote matched diagnostic log lines per ESID.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    if not _STAGING_AREA.is_dir():
        logger.error("Staging_Area/ not found at %s", _STAGING_AREA)
        sys.exit(2)

    # --- Load config-derived evidence sources (each degrades to a logged
    # warning + reduced evidence rather than aborting the diagnosis) ---
    collectors_csvs: List[Path] = []
    success_csv = failure_csv = None
    try:
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
        uploads = config.get("uploads", {})
        collectors_csvs = [
            Path(ds["collectors_csv"])
            for ds in uploads.get("datasets", [])
            if ds.get("collectors_csv")
        ]
        if uploads.get("successful_results_file"):
            success_csv = Path(uploads["successful_results_file"])
        if uploads.get("failure_results_file"):
            failure_csv = Path(uploads["failure_results_file"])
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "Could not read config %s (%s) — tracker/results/collector "
            "evidence will be unavailable.", args.config, exc,
        )

    tracker_path = (
        success_csv.parent / _TRACKER_FILENAME if success_csv else None
    )

    output_path = (
        Path(args.output)
        if args.output
        else Path.cwd() / datetime.now().strftime(
            "missing_state_diagnosis_%Y%m%d_%H%M%S.csv"
        )
    )
    log_paths = [Path(p) for p in args.log]

    logger.info("=" * 70)
    logger.info("AZUS MISSING-STATE DIAGNOSIS%s",
                "  (RESTORE MODE)" if args.restore_states else " (read-only)")
    logger.info("=" * 70)
    logger.info("Staging:    %s", _STAGING_AREA)
    logger.info("Tracker:    %s", tracker_path or "(unavailable)")
    logger.info("Collectors: %s",
                ", ".join(str(p) for p in collectors_csvs) or "(unavailable)")
    logger.info("Logs:       %s",
                ", ".join(str(p) for p in log_paths) or "(none given)")
    logger.info("Output:     %s", output_path)
    logger.info("=" * 70)

    tracker_esids = _esids_from_tracker(tracker_path) if tracker_path else set()
    collector_esids = _esids_from_collectors_csvs(collectors_csvs)
    success_rows = _esids_from_results_csv(success_csv) if success_csv else {}
    failure_rows = _esids_from_results_csv(failure_csv) if failure_csv else {}

    # --- Walk Staging_Area/ ---
    rows: List[Dict[str, str]] = []
    restored = 0
    with_state = 0
    for entry in sorted(_STAGING_AREA.iterdir()):
        if not entry.is_dir():
            continue
        esid = azus_common.parse_esid(entry.name)
        if esid is None:
            logger.warning("Skipping non-ESID subfolder: %s", entry.name)
            continue
        if (entry / _STATE_FILENAME).is_file():
            with_state += 1
            continue
        ev = gather_evidence(
            esid, entry, tracker_esids, collector_esids,
            success_rows, failure_rows, log_paths,
        )
        cause, action = classify(ev)

        if args.restore_states and ev.request_log_record_id:
            if restore_state(ev):
                restored += 1
                action = (
                    "state RESTORED this run — now run "
                    "finish_stuck_uploads.py"
                )

        logger.info("[ESID %s] %s", esid, cause)
        if args.verbose:
            for line in ev.log_lines:
                logger.info("    log: %s", line)

        rows.append({
            "ESID#": esid,
            "Folder": entry.name,
            "ZIP Present": "yes" if ev.zip_present else "no",
            "In Tracker": "yes" if ev.in_tracker else "no",
            "Collector Row": (
                "unknown" if ev.collector_row is None
                else ("yes" if ev.collector_row else "no")
            ),
            "Metadata JSON": "yes" if ev.metadata_json else "no",
            "Request Log Record ID": ev.request_log_record_id,
            "Prior Success Row": "yes" if ev.prior_success else "no",
            "Latest Failure": ev.latest_failure,
            "Prep Sentinel mtime": ev.prep_mtime,
            "Log Mentions": (
                "n/a" if ev.log_mentions is None else str(ev.log_mentions)
            ),
            "Probable Cause": cause,
            "Suggested Action": action,
        })

    try:
        with open(output_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
    except OSError as exc:
        logger.error("Could not write report to %s: %s", output_path, exc)
        sys.exit(2)

    # --- Summary ---
    cause_counts: Dict[str, int] = {}
    for row in rows:
        key = row["Probable Cause"].split(" — ")[0].split(":")[0]
        cause_counts[key] = cause_counts.get(key, 0) + 1

    logger.info("=" * 70)
    logger.info("SUMMARY")
    logger.info("=" * 70)
    logger.info("Folders with upload_state.json:    %d (fine — not examined)",
                with_state)
    logger.info("Folders WITHOUT upload_state.json: %d", len(rows))
    for cause, count in sorted(cause_counts.items(), key=lambda kv: -kv[1]):
        logger.info("  %2d x %s", count, cause)
    if args.restore_states:
        logger.info("State files restored:              %d", restored)
    logger.info("Report: %s", output_path)
    logger.info("=" * 70)

    sys.exit(1 if rows else 0)


if __name__ == "__main__":
    main()
