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
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple


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
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_STAGING_AREA = _PROJECT_ROOT / "Staging_Area"

# The state-file name written by standalone_tasks.py after draft creation.
_STATE_FILENAME = "upload_state.json"


# ---------------------------------------------------------------------
# Regex: pull the leading numeric ESID portion out of a folder name.
# ---------------------------------------------------------------------
# Accepts every reasonable form: ESID_073, ESID_073_Staging, ESID#73.
# Tolerant in case someone created a non-standard folder name.
_ESID_FOLDER_RE = re.compile(r"^ESID[_#](\d+)", re.IGNORECASE)


# =====================================================================
#  Discovery
# =====================================================================

def discover_stuck_esids() -> List[Tuple[int, str, Path, str]]:
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
        Sorted list of (numeric_esid, padded_str, folder_path, record_id)
        tuples, ordered ascending by numeric ESID value.  Empty list
        if Staging_Area/ does not exist or contains no stuck uploads.
    """
    found: List[Tuple[int, str, Path, str]] = []

    if not _STAGING_AREA.is_dir():
        # No staging area = no stuck uploads.  Return cleanly so the
        # caller can print a friendly "nothing to do" message.
        return found

    for entry in _STAGING_AREA.iterdir():
        # Skip files and non-ESID-named folders.  This silently filters
        # out things like .DS_Store, lost+found, manually-renamed
        # backup folders, etc.
        if not entry.is_dir():
            continue

        m = _ESID_FOLDER_RE.match(entry.name)
        if m is None:
            logger.debug("Skipping non-ESID directory: %s", entry.name)
            continue

        # The stuck marker.  No marker → upload didn't start, or the
        # marker was manually removed.  Either way, not stuck.
        state_file = entry / _STATE_FILENAME
        if not state_file.is_file():
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

        numeric = int(m.group(1))
        padded = f"{numeric:03d}"  # "73" → "073"
        found.append((numeric, padded, entry, record_id))

    # Sort by integer value, not string — robust if the user has
    # both ESID_4 and ESID_073 sitting side by side.
    found.sort(key=lambda t: t[0])
    return found


# =====================================================================
#  Delegation
# =====================================================================

def run_recovery(
    stuck_esids: List[str],
    config_path: str,
    workers: int,
    upload_attempts: int = 3,
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
        upload_attempts: Total PUT attempts per file, forwarded as
            ``--upload-attempts N`` to standalone_tasks.py.  Default 3
            matches standalone_tasks.py's default (historical behavior).

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
            "Total number of PUT attempts per file before that file is "
            "marked failed (default: 3). N=1 means one shot per file with "
            "no retry; N=3 is the historical behavior with 30s / 90s "
            "backoffs. Valid range: 1 to 3. Forwarded to "
            "standalone_tasks.py as --upload-attempts N. Only affects "
            "file uploads (PUTs); metadata GETs are unchanged."
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

    # --- Banner ---
    logger.info("=" * 70)
    logger.info("AZUS STUCK-UPLOAD RECOVERY")
    logger.info("=" * 70)
    logger.info("Scanning: %s", _STAGING_AREA)
    logger.info("Marker:   %s (inside each ESID folder)", _STATE_FILENAME)
    logger.info("=" * 70)

    # --- Discover ---
    stuck = discover_stuck_esids()

    if not stuck:
        logger.info("No stuck uploads found in %s — nothing to do.", _STAGING_AREA)
        sys.exit(0)

    logger.info("")
    logger.info("Found %d stuck upload(s) (numerical order):", len(stuck))
    for _, padded, folder, record_id in stuck:
        logger.info(
            "  ESID %s  →  Zenodo draft %s   (folder: %s)",
            padded, record_id, folder.name,
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
