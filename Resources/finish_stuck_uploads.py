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

OUTPUT
======
Everything printed to the screen is ALSO written to
``Records/YYYYMMDD_HHMMSS_finish_stuck_uploads.log`` (timestamp first, so
runs sort chronologically).  That is on by default and needs no flag:
these runs are long and unattended, and a failure hours in should leave
something to read.  ``--log PATH`` moves it; a log file that cannot be
opened is reported on screen and the run continues regardless.

One gap to know about: Phase A shells out to ``standalone_tasks.py``, so
that phase's detail goes to the screen and to its own ``azus_upload.log``
— not into this tool's log.  The startup banner says so.  When diagnosing
a Phase A failure, read ``azus_upload.log``; for the file-by-file phases,
read this tool's log.

Exit code is the exit code of the underlying standalone_tasks.py run
(0 if everything finished, 1 if any ESID still failed).
"""

import argparse
import json
import logging
import subprocess
import sys
from datetime import datetime
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
#  Logging
# =====================================================================

def default_log_path() -> Path:
    """Build this run's log path: ``Records/<stamp>_finish_stuck_uploads.log``.

    Timestamp first so repeated runs sort chronologically in a directory
    listing — the same convention ``esid_record_report.py`` uses for its
    CSVs.

    Returns:
        The path for this run's log (its parent may not exist yet).
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return _PROJECT_ROOT / "Records" / f"{stamp}_finish_stuck_uploads.log"


def configure_logging(verbose: bool = False,
                      log_path: Optional[Path] = None) -> Optional[Path]:
    """Send this tool's output to BOTH the screen and a log file.

    Recovery runs are long and unattended, and until now everything this
    tool printed existed only on the terminal — a failure hours in left
    nothing to read afterwards.  (``standalone_tasks.py`` has always had
    its own ``azus_upload.log``; this tool had nothing.)

    A logging problem must never fail a recovery run, so a directory or
    file that cannot be created is reported on screen and the run
    continues with screen output only.

    Args:
        verbose: Log at DEBUG instead of INFO.
        log_path: Where to write; defaults to :func:`default_log_path`.

    Returns:
        The log file actually opened, or None when only the screen is
        being written.
    """
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    target = log_path or default_log_path()
    opened: Optional[Path] = None
    problem: Optional[str] = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(target, encoding="utf-8"))
        opened = target
    except OSError as exc:
        problem = f"{target}: {exc}"

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=handlers,
    )
    if problem is not None:
        logger.warning(
            "Could not open a log file (%s) — this run's output will only "
            "appear on screen.", problem,
        )
    return opened


# =====================================================================
#  Delegation
# =====================================================================

def run_recovery(
    stuck_esids: List[str],
    config_path: str,
    workers: int,
    upload_attempts: int = 3,
    skip_date_check: bool = False,
    skip_integrity_hash: bool = False,
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
        skip_integrity_hash: Forwarded as ``--skip-integrity-hash``, which
            drops only the full ZIP re-hash from the pre-upload integrity
            gate (the structural checks still run).  That re-hash is a
            complete read of the archive — minutes on a 40 GB ZIP, and it
            repeats on every recovery run.

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
    if skip_integrity_hash:
        cmd.append("--skip-integrity-hash")
    logger.info("Running: %s", " ".join(cmd))
    # cwd = project root so relative paths in config.json resolve correctly.
    # No timeout — multi-GB ZIPs can legitimately take hours.
    result = subprocess.run(cmd, cwd=str(_PROJECT_ROOT))
    return result.returncode


# =====================================================================
#  File-by-file fallback (opt-in: --enable-file-by-file)
# =====================================================================

def _load_publish_config(config_path: str) -> Tuple[Optional[str], bool, bool]:
    """Read the publish-gate settings the file-by-file path needs.

    ``community_id`` comes from ``project_config.json`` (referenced by
    config.json's ``project_config`` key); ``reserve_doi`` and
    ``auto_publish`` come from config.json's ``uploads`` section — the same
    sources standalone_tasks.py uses.  Any read problem is logged and
    tolerated (falling back to no community, no DOI, no auto-publish) so a
    config quirk never crashes recovery.

    Args:
        config_path: Path to AZUS config.json (relative paths resolve
            against the project root).

    Returns:
        A ``(community_id, reserve_doi, auto_publish)`` tuple.
    """
    community_id: Optional[str] = None
    reserve_doi = False
    auto_publish = False
    try:
        cfg_file = Path(config_path)
        if not cfg_file.is_absolute():
            cfg_file = _PROJECT_ROOT / config_path
        cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
        uploads = cfg.get("uploads", {}) or {}
        reserve_doi = bool(uploads.get("reserve_doi", False))
        auto_publish = bool(uploads.get("auto_publish", False))
        pc_path = cfg.get("project_config")
        if pc_path:
            pc_file = Path(pc_path)
            if not pc_file.is_absolute():
                pc_file = _PROJECT_ROOT / pc_path
            pc = json.loads(pc_file.read_text(encoding="utf-8"))
            community_id = (pc.get("community_id") or "") or None
    except (OSError, ValueError, TypeError) as exc:
        logger.warning(
            "Could not fully read publish config from %s (%s) — proceeding "
            "with community_id=%s, reserve_doi=%s, auto_publish=%s.",
            config_path, exc, community_id, reserve_doi, auto_publish,
        )
    return community_id, reserve_doi, auto_publish


def _run_with_file_by_file(
    stuck: List[Tuple[int, str, Path, str]], args: argparse.Namespace
) -> int:
    """Finish stuck ESIDs with the file-by-file fallback enabled.

    ZIP-mode ESIDs are first attempted via the normal ZIP shell-out
    (Phase A). Then (Phase B) every ESID already in file-by-file mode is
    continued, and every ZIP-mode ESID still stuck AND meeting the switch
    condition (``number_of_tries >= --tries-threshold`` AND only the ZIP is
    missing) is switched to file-by-file — all in this one run.

    ``--force`` switches immediately instead: the
    ``number_of_tries >= --tries-threshold`` condition is NOT applied, and
    the ESID is taken out of Phase A so the ZIP is not retried (which would
    only re-hash the whole archive and burn an upload window).  An ESID can
    therefore be switched on its very first failure.  ``only_zip_missing``
    still applies — file-by-file replaces the ZIP, so a run where a
    COMPANION is also missing is not a ZIP-size problem and is left to the
    normal path.  Being a one-way door, this is opt-in.

    Args:
        stuck: The discovered ``(sort_key, esid, folder, record_id)`` list
            (already ``--esid``-filtered).
        args: Parsed CLI arguments.

    Returns:
        Process exit code: 0 if every ESID is now finished, 1 if any still
        failed, 2 on a usage/environment error (missing raw dir or creds).
    """
    import file_by_file_upload as fbf
    from standalone_uploader import (
        get_credentials_from_env, _read_number_of_tries,
    )

    raw_root = Path(args.raw_data_dir)
    if not raw_root.is_dir():
        logger.error("Raw data folder not found: %s", raw_root)
        return 2
    try:
        credentials = get_credentials_from_env()
    except ValueError as exc:
        logger.error("%s", exc)
        logger.error("Run: source Resources/set_env.sh")
        return 2

    community_id, reserve_doi, auto_publish = _load_publish_config(args.config)
    raw_by_esid = {
        padded: folder
        for _, padded, folder in azus_common.find_esid_folders(raw_root)
    }

    zip_stuck = [
        t for t in stuck
        if azus_common.read_upload_mode(t[2]) != azus_common.FILE_BY_FILE_MODE
    ]
    fbf_stuck = [t for t in stuck if t not in zip_stuck]

    def _do_fbf(padded: str, folder: Path, record_id: str) -> bool:
        """Run the file-by-file helper for one ESID; False if raw is missing.

        Args:
            padded: Canonical ESID string.
            folder: The staging folder.
            record_id: The Zenodo draft/record id.

        Returns:
            True on a fully successful file-by-file finish, else False.
        """
        raw_folder = raw_by_esid.get(padded)
        if raw_folder is None:
            logger.error(
                "[ESID %s] No matching Raw_Data folder under %s — cannot "
                "upload file-by-file.", padded, raw_root,
            )
            return False
        return fbf.run_file_by_file(
            esid=padded, staging_dir=folder, raw_dir=raw_folder,
            record_id=record_id, credentials=credentials,
            community_id=community_id, reserve_doi=reserve_doi,
            auto_publish=auto_publish,
            # On this path there is no ZIP, so --upload-attempts applies to
            # every WAV rather than to a single archive.
            upload_attempts=args.upload_attempts,
        )

    failures: List[str] = []
    switched: List[str] = []

    # --force: switch NOW, without waiting for number_of_tries to reach
    # --tries-threshold and without giving the ZIP another attempt.  The
    # threshold exists so one bad night does not abandon a ZIP that would
    # have succeeded; --force is the operator saying they have already made
    # that judgement.  Phase A is skipped for these ESIDs because a ZIP
    # retry can no longer change what happens to them — it would only
    # re-hash the whole archive and burn an upload window.
    #
    # only_zip_missing() is still required: file-by-file replaces the ZIP,
    # so if a COMPANION is also missing the problem is not ZIP size and the
    # switch is the wrong remedy.  --force does not override that.
    # Without --force this list stays empty and the phase order is
    # exactly as before.
    forced: List[Tuple[int, str, Path, str]] = []
    if args.force:
        for entry in zip_stuck:
            _sort, padded, folder, record_id = entry
            if not fbf.only_zip_missing(credentials, record_id, folder, padded):
                logger.warning(
                    "[ESID %s] --force NOT applied — the ZIP is not the sole "
                    "missing file (a companion also failed) or the ZIP is "
                    "already complete. File-by-file replaces the ZIP, so it "
                    "is not the fix here. Falling back to the normal ZIP "
                    "pass.", padded,
                )
                continue
            forced.append(entry)
        if forced:
            zip_stuck = [t for t in zip_stuck if t not in forced]
            logger.warning(
                "--force: switching %d ESID(s) to file-by-file WITHOUT the "
                "number_of_tries check and WITHOUT retrying the ZIP (%s). "
                "This is a ONE-WAY DOOR — reverting would mean deleting "
                "files already committed to the record.",
                len(forced), ", ".join(p for _s, p, _f, _r in forced),
            )

    # Phase A: attempt the ZIP finish for ZIP-mode ESIDs (unchanged path).
    if zip_stuck:
        logger.info(
            "Phase A: ZIP finish for %d ESID(s) via standalone_tasks...",
            len(zip_stuck),
        )
        run_recovery(
            stuck_esids=[p for _, p, _, _ in zip_stuck],
            config_path=args.config, workers=args.workers,
            upload_attempts=args.upload_attempts,
            skip_date_check=args.skip_date_check,
            skip_integrity_hash=args.skip_integrity_hash,
        )

    # Phase A2 (--force only): switch the selected ESIDs directly.
    for _sort, padded, folder, record_id in forced:
        logger.warning(
            "[ESID %s] SWITCHING to file-by-file (--force: tries check "
            "bypassed, ZIP not retried)...", padded,
        )
        (switched if _do_fbf(padded, folder, record_id)
         else failures).append(padded)

    # Phase B1: continue ESIDs already in file-by-file mode.
    for _, padded, folder, record_id in fbf_stuck:
        logger.info("[ESID %s] Continuing file-by-file upload...", padded)
        (switched if _do_fbf(padded, folder, record_id)
         else failures).append(padded)

    # Phase B2: evaluate the switch for ZIP-mode ESIDs still stuck.
    for _, padded, folder, record_id in zip_stuck:
        if not (folder.is_dir() and (folder / _STATE_FILENAME).is_file()):
            continue  # finished by Phase A (moved to Uploaded_Data)
        tries = _read_number_of_tries(folder / _STATE_FILENAME)
        if tries < args.tries_threshold:
            logger.info(
                "[ESID %s] Not switching — number_of_tries=%d < threshold=%d "
                "(the ZIP will be retried on the next run).",
                padded, tries, args.tries_threshold,
            )
            failures.append(padded)
            continue
        if not fbf.only_zip_missing(credentials, record_id, folder, padded):
            logger.info(
                "[ESID %s] Not switching — the ZIP is not the sole missing "
                "file (a companion also failed) or the ZIP is already "
                "complete.", padded,
            )
            failures.append(padded)
            continue
        logger.warning(
            "[ESID %s] SWITCHING to file-by-file (tries=%d >= %d, only the "
            "ZIP is missing)...", padded, tries, args.tries_threshold,
        )
        (switched if _do_fbf(padded, folder, record_id)
         else failures).append(padded)

    logger.info("=" * 70)
    logger.info(
        "File-by-file recovery: %d finished via file-by-file, %d still failing.",
        len(switched), len(failures),
    )
    if switched:
        logger.info("Finished file-by-file: %s", ", ".join(switched))
    if failures:
        logger.error("Still failing (re-run to retry): %s", ", ".join(failures))
        return 1
    return 0


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
    parser.add_argument(
        "--enable-file-by-file", action="store_true",
        help=(
            "Enable the file-by-file FALLBACK. For an ESID whose ZIP finish "
            "still fails with ONLY the ZIP missing after --tries-threshold "
            "attempts, switch it to uploading the individual WAVs (from "
            "Raw_Data) + CONFIG.TXT instead of the ZIP; and CONTINUE any ESID "
            "already in file-by-file mode. Requires --raw-data-dir. Without "
            "this flag, file-by-file ESIDs are skipped by the ZIP pipeline and "
            "reported."
        ),
    )
    parser.add_argument(
        "--tries-threshold", type=int, default=3, metavar="N",
        help=(
            "With --enable-file-by-file: the number_of_tries (from "
            "upload_state.json) at or above which a ZIP-only-missing ESID is "
            "switched to file-by-file (default: 3). Set high to keep retrying "
            "the ZIP and suppress auto-switching."
        ),
    )
    parser.add_argument(
        "--raw-data-dir", metavar="PATH",
        help=(
            "Root folder holding the raw ESID#NNN subfolders (the WAVs + "
            "CONFIG.TXT). Required with --enable-file-by-file — file-by-file "
            "uploads the individual audio files straight from here."
        ),
    )
    parser.add_argument(
        "--skip-integrity-hash", action="store_true",
        help=(
            "Forwarded to standalone_tasks.py: skip only the full ZIP "
            "SHA-512 re-hash in the pre-upload integrity gate (the "
            "structural checks — sentinel, readable archive, ZIP contents "
            "vs file_list.csv — still run). That re-hash is a complete read "
            "of the archive, so it costs minutes on a 40 GB ZIP and repeats "
            "on every recovery run."
        ),
    )
    parser.add_argument(
        "--force", action="store_true",
        help=(
            "With --enable-file-by-file: switch to file-by-file NOW, "
            "IGNORING --tries-threshold, and skip the ZIP retry entirely. "
            "An ESID can be switched on its first failure. The ZIP must "
            "still be the sole missing file — file-by-file replaces the ZIP, "
            "so it is not the fix when a companion also failed; those ESIDs "
            "are reported and left to the normal path. Switching is a "
            "ONE-WAY DOOR: reverting would mean deleting files already "
            "committed to the record."
        ),
    )
    parser.add_argument(
        "--log", default=None, metavar="PATH",
        help=(
            "Write this run's screen output here instead of the default "
            "Records/YYYYMMDD_HHMMSS_finish_stuck_uploads.log. Logging to a "
            "file is always on: a recovery run is long and unattended, and a "
            "failure hours in should leave something to read."
        ),
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Log at DEBUG level (screen and log file alike).",
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

    # --- File-by-file requires a raw-data root (WAVs live only there) ---
    if args.enable_file_by_file and not args.raw_data_dir:
        parser.error(
            "--enable-file-by-file requires --raw-data-dir <Raw_Data root> "
            "(the individual WAVs + CONFIG.TXT are uploaded from there)."
        )
    # --force only has meaning for the switch decision, which only exists
    # on the file-by-file path.
    if args.force and not args.enable_file_by_file:
        parser.error(
            "--force only applies to the file-by-file switch, so it requires "
            "--enable-file-by-file. To skip just the ZIP re-hash on the "
            "ordinary ZIP path, use --skip-integrity-hash."
        )
    if args.tries_threshold < 1:
        parser.error(
            f"--tries-threshold must be at least 1 (got {args.tries_threshold})."
        )

    # --- Expand --esid values (numbers and/or spreadsheet paths) ---
    # Before logging is configured, so a mistyped --esid does not leave a
    # log file behind for a run that never started.  The error still
    # reaches the screen.
    requested: Optional[List[str]] = None
    if args.esid:
        try:
            requested = azus_common.load_esid_args(args.esid)
        except ValueError as exc:
            print(f"{exc}", file=sys.stderr)
            sys.exit(2)

    # --- Configure logging (screen + Records/<stamp>_*.log) ---
    log_file = configure_logging(
        verbose=args.verbose,
        log_path=Path(args.log) if args.log else None,
    )
    if log_file is not None:
        logger.info("Log file: %s", log_file)
        # Phase A runs standalone_tasks.py as a SUBPROCESS, so its output
        # goes to the screen and to its own azus_upload.log — not into the
        # file above.  Say so, or a Phase A failure looks like a gap.
        logger.info(
            "Note: Phase A (the ZIP pass) runs standalone_tasks.py in a "
            "subprocess; its detail lands in azus_upload.log, not here."
        )

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

    # --- File-by-file fallback path (opt-in) ---
    if args.enable_file_by_file:
        logger.info("")
        logger.info(
            "File-by-file fallback ENABLED (threshold=%d, raw=%s).",
            args.tries_threshold, args.raw_data_dir,
        )
        sys.exit(_run_with_file_by_file(stuck, args))

    # Warn about any file-by-file ESIDs the ZIP pipeline will SKIP so they
    # are never silently un-finishable.
    fbf_present = [
        padded for _, padded, folder, _ in stuck
        if azus_common.read_upload_mode(folder) == azus_common.FILE_BY_FILE_MODE
    ]
    if fbf_present:
        logger.warning(
            "%d ESID(s) are in file-by-file mode and will be SKIPPED by the "
            "ZIP pipeline: %s. Re-run with --enable-file-by-file --raw-data-dir "
            "<Raw_Data root> to finish them.",
            len(fbf_present), ", ".join(fbf_present),
        )

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
        skip_integrity_hash=args.skip_integrity_hash,
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
