#!/usr/bin/env python3
"""Batch dataset preparation in numerical ESID order.

WHAT THIS TOOL DOES
===================
You have a top-level folder containing many raw ESID subdirectories
(e.g., ESID_001/, ESID_002/, ESID_073/, ...).  This tool walks that
folder, runs the existing ``Resources/prepare_dataset.py`` on each
ESID **in numerical order**, and **automatically skips any ESID that
has already been prepared** (or already prepared AND uploaded).

This is the batch counterpart to ``prepare_dataset.py``: instead of
preparing one dataset at a time, you point it at the parent folder
and walk away.

WHY THE SKIP CHECK MATTERS
==========================
Preparing one site can take many minutes (computing SHA-512 hashes
on multi-GB ZIPs is the slow part).  If a batch run is interrupted
half-way through — laptop closes, ssh session drops, you hit
Ctrl+C — you should be able to re-run this tool without redoing
the work that already succeeded.

That's exactly what the skip check provides.  For each ESID this
tool is about to process, it looks in two places under the project
root:

    Staging_Area/ESID_NNN_Staging/      ← preparation succeeded
                                          (upload may or may not
                                          have happened yet)

    Uploaded_Data/ESID_NNN_Uploaded/    ← preparation succeeded AND
                                          upload succeeded.  The
                                          original staging folder
                                          was renamed by
                                          ``standalone_tasks.py``
                                          after a successful upload.

If either folder exists, the ESID is skipped and we move on to the
next one.  Safe to re-run any time.

ORDER OF OPERATIONS
===================
For each ESID, in increasing numeric order:

    1. Compute the zero-padded ESID string (e.g., 4 → "004").
    2. Check the two "already prepared" folders above.  If either
       exists, log it and skip.
    3. Otherwise, run ``prepare_dataset.py <esid_folder>
       --config <config>`` as a subprocess.  Output streams to the
       terminal so the user sees the prep details in real time.  The
       eclipse type is not passed — prepare_dataset.py reads it from
       each ESID's "Local Eclipse Type" cell in the collector CSV.
    4. If prepare_dataset.py exits 0, count as PREPARED.
    5. If it exits non-zero (or this script can't even spawn it),
       log the failure and continue to the next ESID.  One bad
       ESID never stops the batch.

At the end, a summary lists how many were prepared / skipped /
failed, with the specific ESIDs in each bucket.

WHY SUBPROCESS (NOT IMPORT)?
============================
We shell out to ``prepare_dataset.py`` rather than importing it.
This keeps this tool **decoupled** from prepare_dataset.py's
internal API: as long as its command-line interface is stable,
this tool keeps working even if its internals are refactored.
Also, each ESID runs in a fresh Python process, so a memory leak
or stuck state in one prep does not contaminate the next.

USAGE
=====
Default — run from the project root:

    python Resources/prep_all_datasets.py /path/to/Raw_Data/

With explicit options:

    python Resources/prep_all_datasets.py /path/to/Raw_Data/ \\
        --config Resources/config.json

Exit code is 0 if every ESID was either prepared successfully or
already-prepared (skipped).  Exit code 1 if any ESID failed.
"""

import argparse
import logging
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import azus_common


# ---------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------
# Named so messages are easy to grep ("azus.prep_all" prefix).
logger = logging.getLogger("azus.prep_all")


# ---------------------------------------------------------------------
# ESID folder names
# ---------------------------------------------------------------------
# Parsed by azus_common.parse_esid — the single suite-wide grammar:
#     ESID_073, ESID_073_Staging, ESID#73, ESID_4 (padded to 004),
#     and suffixed ids like ESID_120A / ESID_122_Part_1_of_2.
# The canonical form is the zero-padded 3-digit number plus any suffix.


# ---------------------------------------------------------------------
# Project layout
# ---------------------------------------------------------------------
# This file lives in ``Resources/`` inside the project root, so the
# project root is two directories up: ``Resources/<this file>`` →
# project root.  We resolve to an absolute path so the rest of the
# code does not care what the current working directory is.
_PROJECT_ROOT = azus_common.PROJECT_ROOT

# Folders the skip check looks at.  Created lazily by the rest of the
# pipeline; if they do not exist, every ESID is treated as "not yet
# prepared" — which is the correct behavior on a fresh install.
_STAGING_AREA = azus_common.STAGING_AREA
_UPLOADED_DATA = azus_common.UPLOADED_DATA

# Sentinel file that prepare_dataset.py touches as its absolute last
# action.  Its presence inside a Staging_Area/ESID_NNN_Staging/ folder
# is the authoritative signal that preparation finished cleanly.
# A folder WITHOUT this sentinel is treated as INCOMPLETE and re-prepped
# (covers interruptions like Ctrl+C, kill -9, or a partial cross-filesystem
# copy that left a fully-named but content-incomplete directory in place).
_PREP_SENTINEL = azus_common.PREP_SENTINEL


# =====================================================================
#  Discovery
# =====================================================================

def discover_esid_folders(
    top_level: Path,
) -> List[Tuple[Tuple[int, str], str, Path]]:
    """Find every ESID subdirectory inside ``top_level``.

    Returns a list of ``(sort_key, canonical_esid, folder_path)``
    tuples, sorted in ascending ESID order (numeric part first, then
    any suffix).

    Example — a folder containing ``ESID_073``, ``ESID_007``, and
    ``ESID_4`` will return::

        [((4, ""),  "004", .../ESID_4),
         ((7, ""),  "007", .../ESID_007),
         ((73, ""), "073", .../ESID_073)]

    Folders whose names do not match the ESID pattern (e.g.,
    ``.DS_Store``, ``backup/``, ``notes/``) are silently ignored —
    only the grammar-matching ones are returned.

    Args:
        top_level: The parent folder containing ESID subdirectories.

    Returns:
        Empty list if no ESID folders are found OR if ``top_level``
        is not a directory.  Otherwise a list sorted by the shared
        ESID sort key.
    """
    found: List[Tuple[Tuple[int, str], str, Path]] = []

    if not top_level.is_dir():
        return found

    for entry in top_level.iterdir():
        # We only care about actual subdirectories.  This also implicitly
        # rejects files like ``.DS_Store`` and broken symlinks.
        if not entry.is_dir():
            continue

        padded = azus_common.parse_esid(entry.name)
        if padded is None:
            # Not an ESID-named folder — log at DEBUG so it shows up
            # if the user runs with verbose logging, but does not
            # clutter normal output.
            logger.debug("Skipping non-ESID directory: %s", entry.name)
            continue

        found.append((azus_common.esid_sort_key(padded), padded, entry))

    # Sort by the shared ESID sort key (numeric part first, then any
    # suffix), NOT the raw string.  For zero-padded names ("004",
    # "007", "012") string and key sort agree, but if the input folder
    # has mixed-width names (ESID_4 alongside ESID_007) the key gives
    # the right answer where string sort would not.
    found.sort(key=lambda t: t[0])
    return found


def filter_and_order_discovered(
    discovered: List[Tuple[Tuple[int, str], str, Path]],
    requested: List[str],
) -> Tuple[List[Tuple[Tuple[int, str], str, Path]], List[str]]:
    """Restrict discovered ESID folders to ``requested``, in that order.

    Args:
        discovered: ``(sort_key, canonical_esid, folder)`` tuples from
            :func:`discover_esid_folders` (numerical order).
        requested: Canonical ESIDs from ``--esid``, in the user's given
            order (first occurrence wins; already deduplicated by
            :func:`azus_common.load_esid_args`).

    Returns:
        A ``(selected, missing)`` tuple.  ``selected`` holds the
        discovered entries whose ESID appears in ``requested``, reordered
        to match ``requested`` (compared case-insensitively so a suffix
        like ``120A`` cannot slip past); ``missing`` lists requested
        ESIDs with no matching raw folder, so the caller can surface them.
    """
    by_esid = {entry[1].casefold(): entry for entry in discovered}
    selected: List[Tuple[Tuple[int, str], str, Path]] = []
    missing: List[str] = []
    for esid in requested:
        entry = by_esid.get(esid.casefold())
        if entry is None:
            missing.append(esid)
        else:
            selected.append(entry)
    return selected, missing


# =====================================================================
#  Skip check
# =====================================================================

def already_prepared(esid_padded: str) -> Optional[Path]:
    """Tell us whether this ESID has been prepared in a previous run.

    Checks the two well-known "this ESID is done" locations under the
    project root, in this order:

        1. ``Staging_Area/ESID_NNN_Staging/`` PLUS its ``.prep_complete``
           sentinel file (both must be present).  The sentinel is the
           last thing ``prepare_dataset.py`` writes; if it is missing,
           the prep was interrupted and the folder must be re-prepped.

        2. ``Uploaded_Data/ESID_NNN_Uploaded/``
           Set by ``standalone_tasks.py`` after a successful upload.
           Means: this ESID was prepared AND uploaded.  Folder existence
           alone is sufficient here — an upload could only have completed
           against a prep-complete folder in the first place.

    Args:
        esid_padded: The 3-digit zero-padded ESID string (e.g., "073").

    Returns:
        The path of the fully prepared folder if one was found, or
        ``None`` if neither location qualifies (meaning: this ESID has
        NOT been fully prepared yet and should be processed).  A folder
        present in Staging_Area/ but missing the ``.prep_complete``
        sentinel ALSO returns ``None`` — the re-prep flow's own cleanup
        will overwrite that partial folder.
    """
    staging = _STAGING_AREA / f"ESID_{esid_padded}_Staging"
    if staging.is_dir():
        if (staging / _PREP_SENTINEL).is_file():
            return staging
        # Directory exists but the sentinel is missing — treat as INCOMPLETE.
        # This is the classic interrupted-prep recovery path.  The next
        # prepare_dataset.py run will remove this stale folder during its
        # own pre-move cleanup (see prepare_dataset.py: "Replacing existing
        # staging folder").
        logger.warning(
            "Found incomplete staging folder (no %s sentinel): %s — "
            "will re-prepare.",
            _PREP_SENTINEL, staging,
        )

    uploaded = _UPLOADED_DATA / f"ESID_{esid_padded}_Uploaded"
    if uploaded.is_dir():
        return uploaded

    return None


# =====================================================================
#  Spawn prepare_dataset.py
# =====================================================================

def run_prepare_dataset(
    esid_folder: Path,
    config_path: str,
) -> int:
    """Invoke ``prepare_dataset.py`` as a subprocess and return its exit code.

    Implementation notes for the reader:

    * ``sys.executable`` is the *same* Python interpreter that's
      running this script — important when you're inside a virtual
      environment (we want the prep to use the same env's libraries,
      not whatever ``python`` happens to be on PATH).

    * ``cwd=str(_PROJECT_ROOT)`` makes ``prepare_dataset.py`` run
      with the project root as its current directory.  This matters
      because the default ``--config Resources/config.json`` is a
      RELATIVE path; if cwd were the user's shell directory,
      that relative path could resolve to the wrong place.

    * We do NOT capture stdout/stderr.  prepare_dataset.py prints a
      lot of useful per-ESID information (file counts, hash progress,
      warnings).  Letting it stream straight to the terminal lets
      the user follow what's happening live, which is what they
      want during a multi-hour batch.

    Args:
        esid_folder: Absolute path to the raw ESID directory.
        config_path: Path to AZUS config.json, passed through unchanged.
            The eclipse type is not passed — prepare_dataset.py reads it
            from each ESID's "Local Eclipse Type" cell in the collector
            CSV named by the config.

    Returns:
        The subprocess exit code.  0 = success.  Anything else =
        failure (we treat it as such and continue to the next ESID).
    """
    cmd = [
        sys.executable,                                   # same Python interpreter
        str(_PROJECT_ROOT / "Resources" / "prepare_dataset.py"),
        str(esid_folder),                                 # positional: the raw ESID dir
        "--config", config_path,
    ]
    logger.info("Running: %s", " ".join(cmd))
    # subprocess.run blocks until the child process exits.  No timeout —
    # preparing a multi-GB site can legitimately take a long time.
    result = subprocess.run(cmd, cwd=str(_PROJECT_ROOT))
    return result.returncode


# =====================================================================
#  Main
# =====================================================================

def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(
        description=(
            "Run prepare_dataset.py on every ESID subdirectory inside a "
            "top-level raw-data folder, in numerical order, skipping any "
            "ESID that already has a folder in Staging_Area/ or "
            "Uploaded_Data/ (i.e., was already prepared by a previous run)."
        ),
    )
    parser.add_argument(
        "top_level_folder",
        help=(
            "Path to the folder containing raw ESID subdirectories "
            "(e.g., /media/tracy/ESCSPA00/2024_Total_Raw_Data/). "
            "Each subdirectory must be named ESID_NNN (or ESID#NNN); "
            "anything else is ignored."
        ),
    )
    parser.add_argument(
        "--config", default="Resources/config.json",
        help=(
            "Path to AZUS config.json (default: Resources/config.json). "
            "Passed through unchanged to prepare_dataset.py for every ESID."
        ),
    )
    parser.add_argument(
        "--esid", nargs="+", metavar="ESID_OR_CSV",
        help=(
            "Prepare only the specified ESID(s), IN THE GIVEN ORDER. Each "
            "value is either a literal ESID (1-3 digits, or a suffixed id "
            "like 120A) or the path to a CSV whose first column lists ESIDs "
            "(header row optional); numbers and CSV paths may be mixed. "
            "Without this flag, every ESID folder is prepared in numerical "
            "order."
        ),
    )
    parser.add_argument(
        "--force", action="store_true",
        help=(
            "Prepare each selected ESID even if it already has a folder in "
            "Staging_Area/ or Uploaded_Data/ (the checks that normally skip "
            "already-prepared ESIDs). prepare_dataset.py replaces an "
            "existing Staging_Area folder, preserving its Zenodo draft link. "
            "Re-preparing an ESID that was already UPLOADED is warned about "
            "loudly — the duplicate-record guard only runs at upload time."
        ),
    )
    args = parser.parse_args()

    # --- Configure logging once, here, so submodule loggers inherit it ---
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    # --- Validate the top-level folder upfront, before any work ---
    top_level = Path(args.top_level_folder)
    if not top_level.is_dir():
        logger.error("Top-level folder not found or not a directory: %s", top_level)
        sys.exit(1)

    # --- Expand --esid values (literal ESIDs and/or spreadsheet paths) ---
    requested_esids: Optional[List[str]] = None
    if args.esid:
        try:
            requested_esids = azus_common.load_esid_args(args.esid)
        except ValueError as exc:
            logger.error("Invalid --esid value: %s", exc)
            sys.exit(1)

    # --- Banner: tell the user exactly what we're about to do ---
    logger.info("=" * 70)
    logger.info("AZUS BATCH PREPARATION")
    logger.info("=" * 70)
    logger.info("Top-level folder: %s", top_level.resolve())
    logger.info("Config:           %s", args.config)
    logger.info("Eclipse type:     per-ESID, from each collector-CSV row")
    if requested_esids:
        logger.info(
            "ESID filter:      %s (in this order; all others skipped)",
            ", ".join(requested_esids),
        )
    else:
        logger.info(
            "ESID filter:      none (all discovered ESIDs, numerical order)"
        )
    logger.info(
        "Force:            %s",
        "ON — re-prepare even already-prepared/uploaded ESIDs"
        if args.force else "off (skip already-prepared ESIDs)",
    )
    logger.info("Skip-check dirs:")
    logger.info("  %s/ESID_NNN_Staging/", _STAGING_AREA)
    logger.info("  %s/ESID_NNN_Uploaded/", _UPLOADED_DATA)
    logger.info("=" * 70)

    # --- Discover ESID folders, sorted numerically ---
    discovered = discover_esid_folders(top_level)
    if not discovered:
        logger.warning(
            "No ESID_NNN subdirectories found inside %s — nothing to do.",
            top_level,
        )
        sys.exit(0)

    # --- Apply the optional --esid filter (restrict + enforce order) ---
    if requested_esids is not None:
        discovered, missing = filter_and_order_discovered(
            discovered, requested_esids
        )
        if missing:
            logger.warning(
                "%d requested ESID(s) have no raw folder under %s and will "
                "be skipped: %s",
                len(missing), top_level, ", ".join(missing),
            )
        if not discovered:
            logger.error(
                "None of the requested --esid value(s) match a raw folder "
                "under %s — nothing to do.", top_level,
            )
            sys.exit(1)
        order_desc = "in --esid order"
    else:
        order_desc = "in numerical order"

    logger.info(
        "Processing %d ESID folder(s) %s:", len(discovered), order_desc,
    )
    for _, padded, folder in discovered:
        logger.info("  ESID %s  ←  %s", padded, folder.name)
    logger.info("=" * 70)

    # --- Bookkeeping for the final summary ---
    prepared: List[str] = []
    skipped: List[Tuple[str, Path]] = []     # (ESID, where it was found)
    failed: List[Tuple[str, int]] = []        # (ESID, exit code)

    for i, (_, padded, folder) in enumerate(discovered, 1):
        logger.info("")
        logger.info(
            "[%d/%d] ESID %s — %s",
            i, len(discovered), padded, folder.name,
        )

        # ---- Skip check (bypassed by --force) ----
        # Normally the whole point of the tool — do not redo finished work.
        prior = already_prepared(padded)
        if prior is not None:
            if args.force:
                # --force: re-prepare anyway.  prepare_dataset.py replaces an
                # existing Staging_Area folder (preserving the Zenodo draft
                # link).  An Uploaded_Data twin means this ESID is already on
                # Zenodo — warn loudly; the duplicate-record guard runs at
                # upload time, not here.
                if prior.name.endswith("_Uploaded"):
                    logger.warning(
                        "  --force: ESID %s was ALREADY UPLOADED (%s) — "
                        "re-preparing into Staging_Area/. Re-uploading may "
                        "create a DUPLICATE record unless the upload title-"
                        "guard resumes the existing one.", padded, prior,
                    )
                else:
                    logger.warning(
                        "  --force: re-preparing despite existing %s", prior,
                    )
            else:
                logger.info("  SKIP — already prepared at: %s", prior)
                skipped.append((padded, prior))
                continue

        # ---- Run prepare_dataset.py ----
        # Wrapped in try/except so a subprocess spawn failure (e.g.
        # interpreter missing, OSError) does not stop the batch.
        try:
            rc = run_prepare_dataset(
                esid_folder=folder,
                config_path=args.config,
            )
        except Exception as exc:
            logger.error("  FAILED — could not spawn subprocess: %s", exc)
            failed.append((padded, -1))
            continue

        if rc == 0:
            logger.info("  PREPARED — prepare_dataset.py exited successfully")
            prepared.append(padded)
        else:
            # We log the failure and continue.  The bad ESID's staging
            # folder may be in a partially-populated state for the user
            # to inspect after the batch finishes.
            logger.error(
                "  FAILED — prepare_dataset.py exited with code %d", rc,
            )
            failed.append((padded, rc))

    # --- Final summary ---
    logger.info("")
    logger.info("=" * 70)
    logger.info("BATCH PREPARATION SUMMARY")
    logger.info("=" * 70)
    logger.info("Discovered: %d", len(discovered))
    logger.info("Prepared:   %d", len(prepared))
    logger.info("Skipped:    %d (already prepared in a prior run)", len(skipped))
    logger.info("Failed:     %d", len(failed))
    if prepared:
        logger.info("Prepared ESIDs: %s", ", ".join(prepared))
    if skipped:
        logger.info(
            "Skipped ESIDs:  %s",
            ", ".join(p for p, _ in skipped),
        )
    if failed:
        logger.error(
            "Failed ESIDs:   %s",
            ", ".join(f"{p}(exit={rc})" for p, rc in failed),
        )
    logger.info("=" * 70)

    # Exit non-zero if anything failed, so automation can detect a problem.
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
