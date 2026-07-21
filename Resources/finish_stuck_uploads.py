#!/usr/bin/env python3
"""Find and finish stuck Zenodo uploads.

WHAT THIS TOOL DOES
===================
After a batch upload, you may have some ESIDs that started uploading
but did not finish — typically because the large ZIP exhausted all
three retry attempts on a flaky connection.  For those ESIDs:

  * The Zenodo draft DOES exist (record_id was assigned).
  * The small files (README, CSVs, data dicts, etc.) were committed
    before the ZIP attempt.
  * Only the ZIP (and possibly a couple of late files) are missing.
  * The ESID folder is still sitting in Staging_Area/ — it was NOT
    renamed to Uploaded_Data/ because the upload never completed.

This tool finds all such stuck uploads in Staging_Area/ and finishes
them, reusing the existing resume + retry pipeline already inside
standalone_tasks.py / standalone_uploader.py.

THE STUCK MARKER
================
When standalone_tasks.py creates a draft on Zenodo, it immediately
writes a file called ``upload_state.json`` inside the ESID's staging
folder.  This file contains the draft's ``record_id`` so future runs
can re-attach to the same draft instead of creating a new one.

On a successful upload, the whole staging folder is renamed to
``Uploaded_Data/ESID_NNN_Uploaded/`` and is no longer in Staging_Area/.
So any folder still in Staging_Area/ that contains ``upload_state.json``
is, by definition, an interrupted upload — that's our stuck marker.

WHAT EACH STEP DOES (mapped to the user's spec)
===============================================
1. Scan Staging_Area/ for ESID folders containing ``upload_state.json``
   → THIS TOOL.

2. For each, read upload_state.json to learn the Zenodo record_id
   → THIS TOOL (no Zenodo query needed — the ID is right there).

3. Confirm which files are already committed
   → Done by standalone_uploader.list_draft_files() when the upload
     resumes — it queries the live Zenodo draft.  Querying the API
     is the right answer because the local state could lag (a file
     could have been committed on Zenodo even if our log didn't
     record it).

4. Re-upload missing files with up to 3 retries each
   → Existing per-file retry inside _put_file_content_with_retry()
     (30s / 90s / 270s backoff).

5. Post-upload: community submit, publish, move folder to
   Uploaded_Data/, update uploaded_files.txt, write result CSV
   → Existing logic at the end of standalone_tasks._process_one_dataset().

In short: THIS TOOL only handles steps 1 and 2 (discovery + display).
For steps 3-5 it delegates to the existing standalone_tasks.py
pipeline by shelling out with ``--esid <list> --workers N``.  Zero
duplication of upload logic.

USAGE
=====
From the project root:

    # Default: 1 ESID at a time
    python Resources/finish_stuck_uploads.py

    # Finish 3 stuck uploads concurrently
    python Resources/finish_stuck_uploads.py --workers 3

    # Just list what's stuck — do not actually re-run uploads
    python Resources/finish_stuck_uploads.py --list-only

Exit code is the exit code of the underlying standalone_tasks.py run
(0 if everything finished, 1 if any ESID still failed).
"""

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import azus_common


# ---------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------
# Distinct name ("azus.finish_stuck") makes the log easy to grep.
logger = logging.getLogger("azus.finish_stuck")


# ---------------------------------------------------------------------
# Project layout
# ---------------------------------------------------------------------
# This file lives in Resources/, so the project root is one level up.
# We resolve to an absolute path so the rest of the code does not care
# about the current working directory.
_PROJECT_ROOT = azus_common.PROJECT_ROOT
_STAGING_AREA = azus_common.STAGING_AREA

# The state-file name written by standalone_tasks.py after draft creation.
_STATE_FILENAME = azus_common.STATE_FILENAME


# ---------------------------------------------------------------------
# Regex: pull the leading numeric ESID portion out of a folder name.
# ---------------------------------------------------------------------
# Accepts every reasonable form: ESID_073, ESID_073_Staging, ESID#73.
# Tolerant in case someone created a non-standard folder name.


# =====================================================================
#  Discovery
# =====================================================================

def discover_stuck_esids() -> Tuple[List[Tuple[int, str, Path, str]], List[str]]:
    """Walk Staging_Area/ and return every ESID with a partial upload.

    A "stuck" ESID is identified by these two conditions:

      1. The folder lives in ``Staging_Area/`` (not in Uploaded_Data/).
         If the upload had finished, standalone_tasks.py would have
         renamed it to Uploaded_Data/ESID_NNN_Uploaded/ already.

      2. The folder contains ``upload_state.json``.  This file is
         written immediately after the Zenodo draft is created and
         contains the ``record_id`` we need to resume.

    Both must be true.  A folder without upload_state.json hasn't
    even gotten to the draft-creation step yet — it just needs a
    normal upload run, not a recovery.

    Returns:
        Tuple of (stuck, excluded):
          * stuck — sorted list of (numeric_esid, padded_str,
            folder_path, record_id) tuples, ascending by ESID value.
          * excluded — names of ESID folders that were SKIPPED because
            they have no upload_state.json.  Callers must surface this
            list: these folders are invisible to recovery, and hiding
            them (as this tool once did) let missing-state datasets go
            unnoticed for weeks.
    """
    found: List[Tuple[int, str, Path, str]] = []
    excluded: List[str] = []

    if not _STAGING_AREA.is_dir():
        # No staging area = no stuck uploads.  Return cleanly so the
        # caller can print a friendly "nothing to do" message.
        return found, excluded

    for entry in _STAGING_AREA.iterdir():
        # Skip files and non-ESID-named folders.  This silently filters
        # out things like .DS_Store, lost+found, manually-renamed
        # backup folders, etc.
        if not entry.is_dir():
            continue

        padded = azus_common.parse_esid(entry.name)
        if padded is None:
            logger.debug("Skipping non-ESID directory: %s", entry.name)
            continue

        # The stuck marker.  No marker → this tool cannot resume the
        # folder (there is nothing to resume TO).  Track it so main()
        # can tell the user instead of hiding it at debug level.
        state_file = entry / _STATE_FILENAME
        if not state_file.is_file():
            excluded.append(entry.name)
            logger.debug(
                "Skipping %s — no upload_state.json (not stuck)",
                entry.name,
            )
            continue

        # Try to read the state file.  A corrupt file means we cannot
        # safely resume that ESID — log a warning and skip it.  The
        # user can then inspect / delete the bad state file manually.
        try:
            state_data = json.loads(state_file.read_text(encoding="utf-8"))
            record_id = str(state_data.get("record_id") or "").strip()
        except Exception as exc:
            logger.warning(
                "Could not parse %s (%s) — skipping this ESID",
                state_file, exc,
            )
            continue

        if not record_id:
            logger.warning(
                "%s contains no record_id — skipping this ESID",
                state_file,
            )
            continue

        found.append(
            (azus_common.esid_sort_key(padded), padded, entry, record_id)
        )

    # Sort by the shared ESID key (numeric part, then suffix), not the
    # string — robust if the user has both ESID_4 and ESID_073 sitting
    # side by side.
    found.sort(key=lambda t: t[0])
    excluded.sort()
    return found, excluded


# =====================================================================
#  Delegation
# =====================================================================

def run_recovery(
    stuck_esids: List[str],
    config_path: str,
    workers: int,
    upload_attempts: int = 3,
    skip_date_check: bool = False,
) -> int:
    """Re-run standalone_tasks.py against just the stuck ESIDs.

    We shell out (rather than importing) for the same reason as
    prep_all_datasets.py: it keeps this tool decoupled from the
    internal API of standalone_tasks.py, and the standalone_tasks.py
    confirmation prompt and progress logging still work as the user
    expects (because they're running inside their own process).

    The ``--esid`` flag tells standalone_tasks.py to process only
    these ESIDs.  Because each one has an ``upload_state.json`` in
    its staging folder, standalone_tasks.py will automatically
    take the RESUME path (skip create_draft, list existing files,
    skip already-committed ones, re-upload only what's missing,
    each with up to 3 PUT retries).

    Args:
        stuck_esids: Padded ESID strings, e.g. ["007", "012", "073"].
        config_path: Passed through unchanged.
        workers: How many to upload concurrently (1 = sequential).
        upload_attempts: Total PUT attempts for the data ZIP
            (companion files keep the default), forwarded as
            ``--upload-attempts N`` to standalone_tasks.py.  Default 3
            matches standalone_tasks.py's default (historical behavior).
        skip_date_check: Forwarded as ``--skip-date-check`` — needed
            when resuming a dataset that was originally uploaded with
            the dates-not-available fallback (its WAV names still carry
            no valid recording dates).

    Returns:
        The exit code of standalone_tasks.py.  0 if all finished
        successfully, 1 if at least one still failed.
    """
    cmd = [
        sys.executable,
        str(_PROJECT_ROOT / "standalone_tasks.py"),
        "--config", config_path,
        "--esid", *stuck_esids,
        "--workers", str(workers),
        "--upload-attempts", str(upload_attempts),
    ]
    if skip_date_check:
        cmd.append("--skip-date-check")
    logger.info("Running: %s", " ".join(cmd))
    # cwd = project root so relative paths in config.json resolve correctly.
    # No timeout — multi-GB ZIPs can legitimately take hours.
    result = subprocess.run(cmd, cwd=str(_PROJECT_ROOT))
    return result.returncode


# =====================================================================
#  Main
# =====================================================================

def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(
        description=(
            "Scan Staging_Area/ for partially-completed Zenodo uploads "
            "(identified by the presence of upload_state.json), then "
            "re-run the upload pipeline against just those ESIDs to "
            "finish them.  Reuses the same retry + resume + post-upload "
            "machinery as standalone_tasks.py."
        ),
    )
    parser.add_argument(
        "--config", default="Resources/config.json",
        help=(
            "Path to AZUS config.json (default: Resources/config.json). "
            "Passed through to standalone_tasks.py."
        ),
    )
    parser.add_argument(
        "--workers", type=int, default=1, metavar="N",
        help=(
            "Number of stuck ESID datasets to finish concurrently "
            "(default: 1, sequential).  Forwarded to standalone_tasks.py "
            "as --workers N."
        ),
    )
    parser.add_argument(
        "--upload-attempts", type=int, default=3, metavar="N",
        help=(
            "Total number of PUT attempts for the DATA ZIP before it is "
            "marked failed (default: 3). N=1 means one shot per file with "
            "no retry; N=3 is the historical behavior with 30s / 90s "
            "backoffs. Valid range: 1 to 3. Forwarded to "
            "standalone_tasks.py as --upload-attempts N. Only affects "
            "file uploads (PUTs); metadata GETs are unchanged."
        ),
    )
    parser.add_argument(
        "--skip-date-check", action="store_true",
        help=(
            "Forwarded to standalone_tasks.py: upload datasets whose "
            "WAV filenames carry no valid recording dates with the "
            "dates recorded as not available (needed when resuming a "
            "dataset that was originally uploaded with this flag)."
        ),
    )
    parser.add_argument(
        "--esid", nargs="+", metavar="ESID_OR_CSV",
        help=(
            "Recover only the specified stuck ESID(s). Each value is "
            "either a literal ESID (a 1-3 digit number, or a suffixed "
            "id like 120A / 122_Part_1_of_2) OR the path to a CSV whose "
            "FIRST column lists ESIDs (e.g. a reporting tool's output; "
            "a header row is detected and skipped). Both forms can be "
            "mixed. Requested ESIDs that are not currently stuck are "
            "reported and skipped. Default: recover ALL stuck ESIDs."
        ),
    )
    parser.add_argument(
        "--list-only", action="store_true",
        help=(
            "Discover and print the stuck ESIDs, then exit WITHOUT "
            "running the recovery.  Useful for seeing what would happen "
            "before committing to a multi-hour batch."
        ),
    )
    args = parser.parse_args()

    # --- Validate --workers up front (mirror standalone_tasks.py) ---
    if args.workers < 1:
        parser.error(
            f"--workers must be at least 1 (got {args.workers})."
        )

    # --- Validate --upload-attempts (mirror standalone_tasks.py) ---
    if not (1 <= args.upload_attempts <= 3):
        parser.error(
            f"--upload-attempts must be between 1 and 3 (got "
            f"{args.upload_attempts})."
        )

    # --- Configure logging ---
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    # --- Expand --esid values (numbers and/or spreadsheet paths) ---
    requested: Optional[List[str]] = None
    if args.esid:
        try:
            requested = azus_common.load_esid_args(args.esid)
        except ValueError as exc:
            logger.error("%s", exc)
            sys.exit(2)

    # --- Banner ---
    logger.info("=" * 70)
    logger.info("AZUS STUCK-UPLOAD RECOVERY")
    logger.info("=" * 70)
    logger.info("Scanning: %s", _STAGING_AREA)
    logger.info("Marker:   %s (inside each ESID folder)", _STATE_FILENAME)
    logger.info("=" * 70)

    # --- Discover ---
    stuck, excluded = discover_stuck_esids()

    # Surface the folders this tool CANNOT recover.  These used to be
    # hidden at debug level, which let missing-state datasets silently
    # sit out every recovery run.
    if excluded:
        logger.info("")
        logger.info(
            "EXCLUDED %d folder(s) with no upload_state.json (this tool "
            "cannot resume them): %s",
            len(excluded), ", ".join(excluded),
        )
        logger.info(
            "  Run 'python Resources/diagnose_missing_states.py' to see "
            "why each one is missing its state file."
        )

    if not stuck:
        logger.info("No stuck uploads found in %s — nothing to do.", _STAGING_AREA)
        sys.exit(0)

    # --- Apply the optional --esid filter ---
    if requested is not None:
        stuck_by_esid = {padded.casefold() for _, padded, _, _ in stuck}
        not_stuck = [e for e in requested
                     if e.casefold() not in stuck_by_esid]
        for esid in not_stuck:
            logger.warning(
                "Requested ESID %s is not currently stuck (no folder "
                "with %s in %s) — skipping it.",
                esid, _STATE_FILENAME, _STAGING_AREA,
            )
        # Case-insensitive membership so a suffix case mismatch between
        # the filter and the folder name cannot hide a stuck dataset.
        wanted = {esid.casefold() for esid in requested}
        stuck = [t for t in stuck if t[1].casefold() in wanted]
        logger.info(
            "--esid filter: %d of %d requested ESID(s) are stuck and "
            "will be recovered.",
            len(stuck), len(requested),
        )
        if not stuck:
            logger.info(
                "None of the requested ESIDs are currently stuck — "
                "nothing to do."
            )
            sys.exit(0)

    logger.info("")
    logger.info("Found %d stuck upload(s) (numerical order):", len(stuck))
    for _, padded, folder, record_id in stuck:
        logger.info(
            "  ESID %s  →  Zenodo draft %s   (folder: %s)",
            padded, record_id, folder.name,
        )
    if excluded:
        logger.info(
            "  (+ %d folder(s) excluded — no upload_state.json, see above)",
            len(excluded),
        )
    logger.info("=" * 70)

    # --- If user just wants a listing, stop here ---
    if args.list_only:
        logger.info(
            "--list-only: %d stuck ESID(s) printed; not running recovery.",
            len(stuck),
        )
        sys.exit(0)

    # --- Delegate to standalone_tasks.py ---
    # standalone_tasks.py prints its own "Proceed? (yes/no)" prompt before
    # any network calls, so we do NOT duplicate that prompt here.  The
    # user gets one confirmation step, after seeing this tool's list.
    logger.info("")
    logger.info(
        "Delegating recovery to standalone_tasks.py "
        "(it will prompt for confirmation before uploading)..."
    )
    logger.info("")

    esid_args = [padded for _, padded, _, _ in stuck]
    rc = run_recovery(
        stuck_esids=esid_args,
        config_path=args.config,
        workers=args.workers,
        upload_attempts=args.upload_attempts,
        skip_date_check=args.skip_date_check,
    )

    # --- Final status ---
    logger.info("")
    logger.info("=" * 70)
    if rc == 0:
        logger.info("RECOVERY COMPLETE — standalone_tasks.py exited successfully.")
    else:
        logger.error(
            "RECOVERY FINISHED WITH FAILURES — standalone_tasks.py exited %d. "
            "Stuck ESIDs that still failed can be retried by running this "
            "tool again; their upload_state.json files are unchanged.",
            rc,
        )
    logger.info("=" * 70)
    sys.exit(rc)


if __name__ == "__main__":
    main()
