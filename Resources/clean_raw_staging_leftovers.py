#!/usr/bin/env python3
"""Classify (and optionally remove) stale ``ESID_*_Staging`` leftovers
in the Raw Data folder — CSV report.

PURPOSE
=======
``prepare_dataset.py`` builds each staging folder NEXT TO the raw data
and then moves it into ``Staging_Area/``.  Two historical mechanisms
left orphaned build folders behind in the Raw Data folder:

  * the old single-phase move, which an interruption could stop after
    the copy but before the source cleanup, and
  * prep runs that failed between the build and the move (for example
    the pre-1980 timestamp crash or the zero-byte ZIP-verification
    failure), which deliberately leave the build folder in place.

Nothing in the pipeline ever cleans these up once a valid copy exists
elsewhere.  This tool classifies every raw-side ``ESID_*_Staging``
folder against its twin in ``Staging_Area/`` (or ``Uploaded_Data/``)
and — only when explicitly asked — deletes the raw-side copy of the
sites that are PROVABLY duplicates.

SAFETY MODEL (deletion is opt-in and doubly gated)
==================================================
The tool is **dry-run by default**: without
``--delete-verified-duplicates`` it only reports.  Even with the flag,
a raw-side folder is deleted ONLY when ALL of the following hold, and
every gate is re-checked immediately before the removal:

  1. A twin folder exists in ``Staging_Area/`` (or, failing that, in
     ``Uploaded_Data/`` for already-uploaded sites) and carries the
     ``.prep_complete`` sentinel — proof it came from a fully
     successful preparation.  The sentinel is checked again AFTER the
     (possibly minutes-long) hashing step.
  2. Both sides hold exactly one data ZIP, the two ZIPs are
     INDEPENDENT physical files (neither a symlink, not the same
     inode — a hard link or filesystem alias is one copy, not two),
     and they are byte-identical (SHA-512).  The twin ZIP's identity,
     size, and mtime are captured before hashing and re-checked after,
     so a file swapped mid-verification is refused.
  3. The raw-side folder holds no upload artifact (``upload_state.json``
     / ``ESID_*_request_log.json``) that the twin lacks or that differs
     from the twin's — deleting one of those could orphan a Zenodo
     draft.
  4. All folder-identity checks compare INODES (``os.path.samefile`` /
     ``st_dev``+``st_ino``), never path strings — on the
     case-insensitive filesystems this project deploys on, a
     case-variant path is a different string for the same physical
     directory.  The folder must be a direct child of the scanned Raw
     Data folder, and its twin must not be the same directory or live
     anywhere inside it.

Anything that cannot be verified is reported with a recommended action
and never touched.  Pointing the tool at ``Staging_Area/`` or
``Uploaded_Data/`` themselves — under ANY spelling of the path — is
refused outright.

VERDICTS
========
  * ``VERIFIED_DUPLICATE``       — every gate above passes; the
    raw-side copy is safe to delete (deleted with the flag).
  * ``NO_TWIN``                  — failed-prep leftover; re-prep the
    site (``reprep_missing_zips.py`` / ``prep_all_datasets.py``).
  * ``TWIN_NO_SENTINEL``         — the Staging_Area twin is the
    suspect (old interrupted move); re-prep the site, which replaces
    the twin and removes the raw-side folder itself.
  * ``UPLOADED_TWIN_NO_SENTINEL`` — the twin is an already-uploaded
    dataset from a pre-sentinel-era prep; re-prepping would be skipped,
    so verify it with ``audit_prep_completeness.py`` instead.
  * ``CANNOT_VERIFY``            — missing/multiple/unreadable ZIPs,
    non-independent ZIP copies, differing hashes, or diverging upload
    artifacts; inspect manually.

USAGE
=====
From the project root::

    python Resources/clean_raw_staging_leftovers.py /path/to/Raw_Data
    python Resources/clean_raw_staging_leftovers.py /path/to/Raw_Data \\
        --delete-verified-duplicates
    python Resources/clean_raw_staging_leftovers.py /path/to/Raw_Data \\
        --output leftovers.csv

OUTPUT
======
A CSV (default ``raw_staging_leftovers_YYYYMMDD_HHMMSS.csv`` in the
current directory), written INCREMENTALLY — one row per leftover as it
is processed, so a crash mid-run cannot lose the record of deletions
already performed::

    ESID#, Raw Folder, Twin Location, Twin Sentinel, ZIPs Match,
    Verdict, Action Taken

EXIT CODES
==========
* ``0`` — no leftovers found, or every leftover was a verified
  duplicate and was deleted (``--delete-verified-duplicates``)
* ``1`` — leftovers remain that need attention (including verified
  duplicates reported in dry-run mode)
* ``2`` — usage error (folder missing, or the folder IS Staging_Area
  or Uploaded_Data)
"""

import argparse
import csv
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import azus_common
from prepare_dataset import _UPLOAD_ARTIFACT_PATTERNS

logger = logging.getLogger("azus.clean_leftovers")

_STAGING_AREA = azus_common.STAGING_AREA
_UPLOADED_DATA = azus_common.UPLOADED_DATA
_SENTINEL = azus_common.PREP_SENTINEL

_CSV_COLUMNS = [
    "ESID#",
    "Raw Folder",
    "Twin Location",
    "Twin Sentinel",
    "ZIPs Match",
    "Verdict",
    "Action Taken",
]

# Verdict constants — see the module docstring for their meanings.
VERIFIED_DUPLICATE = "VERIFIED_DUPLICATE"
NO_TWIN = "NO_TWIN"
TWIN_NO_SENTINEL = "TWIN_NO_SENTINEL"
UPLOADED_TWIN_NO_SENTINEL = "UPLOADED_TWIN_NO_SENTINEL"
CANNOT_VERIFY = "CANNOT_VERIFY"

_RECOMMENDED_ACTION = {
    VERIFIED_DUPLICATE: (
        "safe to delete the raw-side copy "
        "(re-run with --delete-verified-duplicates)"
    ),
    NO_TWIN: "re-prep this site (reprep_missing_zips.py / prep_all_datasets.py)",
    TWIN_NO_SENTINEL: (
        "the twin is the suspect (old interrupted move) — re-prep this "
        "site; a successful prep replaces the twin and removes this folder"
    ),
    UPLOADED_TWIN_NO_SENTINEL: (
        "twin was uploaded by a pre-sentinel-era prep — verify it with "
        "audit_prep_completeness.py; re-prepping would be skipped"
    ),
    CANNOT_VERIFY: "inspect manually before touching anything",
}


def _same_inode(a: Path, b: Path) -> bool:
    """Tell whether two paths are the same physical file/directory.

    Compares inodes via :func:`os.path.samefile`, which is immune to
    the case-variant path aliases a case-insensitive filesystem
    produces (``staging_area`` vs ``Staging_Area``) — string equality
    of resolved paths is NOT.

    Args:
        a: First path.
        b: Second path.

    Returns:
        True when both exist and are the same inode; True (fail
        closed) when the comparison itself fails while both paths
        exist is impossible to determine — callers use this in refusal
        checks, so an undeterminable identity must count as "same".
        False only when both exist and are provably different, or one
        does not exist.
    """
    try:
        return os.path.samefile(a, b)
    except OSError:
        # One side missing -> provably not the same existing inode.
        if not a.exists() or not b.exists():
            return False
        return True  # both exist but identity unknowable — fail closed


def _twin_is_or_inside(raw_folder: Path, twin: Path) -> bool:
    """Tell whether the twin IS the raw folder or lives inside it.

    Walks the twin's resolved path and every ancestor, comparing
    inodes against the raw folder — one check kills both the
    case-alias self-twin and any nested-twin arrangement.  Fails
    closed: an unstat-able ancestor counts as "inside".

    Args:
        raw_folder: The raw-side folder a deletion would remove.
        twin: The twin folder that must survive the deletion.

    Returns:
        True when deleting ``raw_folder`` could touch ``twin``.
    """
    try:
        raw_stat = raw_folder.stat()
        twin_resolved = twin.resolve()
        for ancestor in (twin_resolved, *twin_resolved.parents):
            st = ancestor.stat()
            if (st.st_dev, st.st_ino) == (raw_stat.st_dev, raw_stat.st_ino):
                return True
    except OSError:
        return True  # cannot prove independence — fail closed
    return False


def _single_zip(folder: Path) -> Optional[Path]:
    """Return the folder's one data ZIP, or None when absent/ambiguous.

    The staging layout holds exactly one ``ESID_*.zip`` per folder; a
    missing or duplicated ZIP means the folder cannot be SHA-verified
    and must be inspected by a human instead.

    Args:
        folder: A staging-layout folder (raw-side leftover or twin).

    Returns:
        The single ZIP path, or None when there is not exactly one.
    """
    zips = sorted(folder.glob("ESID_*.zip"))
    return zips[0] if len(zips) == 1 else None


def _zips_are_independent_copies(raw_zip: Path, twin_zip: Path) -> bool:
    """Tell whether the two ZIPs are physically independent files.

    A twin ZIP that is a symlink to the raw ZIP — or the same inode
    via a hard link or filesystem alias — would trivially pass a hash
    comparison while being ONE copy, so deleting the raw side would
    destroy the only data.

    Args:
        raw_zip: The raw-side folder's data ZIP.
        twin_zip: The twin folder's data ZIP.

    Returns:
        True only when neither is a symlink and they are provably
        different inodes.
    """
    try:
        if raw_zip.is_symlink() or twin_zip.is_symlink():
            return False
        return not _same_inode(raw_zip, twin_zip)
    except OSError:
        return False  # cannot prove independence — fail closed


def _diverging_upload_artifacts(raw_folder: Path, twin: Path) -> List[str]:
    """List raw-side upload artifacts the twin lacks or differs on.

    ``upload_state.json`` / ``ESID_*_request_log.json`` link a folder
    to its Zenodo draft; deleting a raw-side folder holding one the
    twin does not byte-identically share could orphan that draft.

    Args:
        raw_folder: The raw-side leftover folder.
        twin: The twin folder the raw side would be deleted in favor of.

    Returns:
        Names of raw-side artifacts that are missing from — or differ
        from — the twin (unreadable files count as diverging).
    """
    diverging: List[str] = []
    for pattern in _UPLOAD_ARTIFACT_PATTERNS:
        for artifact in sorted(raw_folder.glob(pattern)):
            twin_copy = twin / artifact.name
            try:
                if not twin_copy.is_file() or \
                        artifact.read_bytes() != twin_copy.read_bytes():
                    diverging.append(artifact.name)
            except OSError:
                diverging.append(artifact.name)
    return diverging


def find_twin(name: str) -> Optional[Path]:
    """Locate the leftover's twin folder, preferring a sentineled one.

    Probes, in order: ``Staging_Area/<name>`` (verbatim),
    ``Staging_Area/ESID_<canonical>_Staging`` (so an unpadded or
    case-variant leftover name still finds its canonical twin), then
    ``Uploaded_Data/ESID_<canonical>_Uploaded``.  Among the candidates
    that exist, the first carrying the ``.prep_complete`` sentinel
    wins; with none sentineled, the first existing one is returned so
    the caller can report why it does not qualify.

    Args:
        name: The leftover folder's basename (``ESID_<esid>_Staging``).

    Returns:
        The twin directory, or None when no candidate exists.
    """
    candidates = [_STAGING_AREA / name]
    esid = azus_common.parse_esid(name)
    if esid is not None:
        canonical_staging = _STAGING_AREA / f"ESID_{esid}_Staging"
        if canonical_staging != candidates[0]:
            candidates.append(canonical_staging)
        candidates.append(_UPLOADED_DATA / f"ESID_{esid}_Uploaded")
    existing = [c for c in candidates if c.is_dir()]
    for candidate in existing:
        if (candidate / _SENTINEL).is_file():
            return candidate
    return existing[0] if existing else None


def classify_leftover(raw_folder: Path) -> Dict[str, str]:
    """Classify one raw-side leftover against its twin.

    Applies the full safety model from the module docstring — the
    dry-run report only ever advertises ``VERIFIED_DUPLICATE`` for a
    folder the deletion gates would actually accept.

    Args:
        raw_folder: The raw-side ``ESID_*_Staging`` directory.

    Returns:
        A CSV row dict (see ``_CSV_COLUMNS``) with ``Action Taken``
        left empty — the caller records any deletion there.
    """
    esid = azus_common.parse_esid(raw_folder.name) or raw_folder.name
    row = {
        "ESID#": esid,
        "Raw Folder": str(raw_folder),
        "Twin Location": "",
        "Twin Sentinel": "no",
        "ZIPs Match": "",
        "Verdict": NO_TWIN,
        "Action Taken": "",
    }
    twin = find_twin(raw_folder.name)
    if twin is None:
        return row
    row["Twin Location"] = str(twin)

    if not (twin / _SENTINEL).is_file():
        row["Verdict"] = (
            UPLOADED_TWIN_NO_SENTINEL
            if twin.parent == _UPLOADED_DATA else TWIN_NO_SENTINEL
        )
        return row
    row["Twin Sentinel"] = "yes"

    if _twin_is_or_inside(raw_folder, twin):
        row["ZIPs Match"] = "twin IS this folder (path alias)"
        row["Verdict"] = CANNOT_VERIFY
        return row

    raw_zip = _single_zip(raw_folder)
    twin_zip = _single_zip(twin)
    if raw_zip is None or twin_zip is None:
        row["ZIPs Match"] = "cannot compare (missing or multiple ZIPs)"
        row["Verdict"] = CANNOT_VERIFY
        return row
    if not _zips_are_independent_copies(raw_zip, twin_zip):
        row["ZIPs Match"] = (
            "NOT independent copies (symlink/hard link/alias)"
        )
        row["Verdict"] = CANNOT_VERIFY
        return row

    diverging = _diverging_upload_artifacts(raw_folder, twin)
    if diverging:
        row["ZIPs Match"] = (
            "raw side holds upload artifacts the twin lacks or differs "
            "on: " + ", ".join(diverging)
        )
        row["Verdict"] = CANNOT_VERIFY
        return row

    logger.info(
        "  [ESID %s] hashing both ZIPs (%s / %s)...",
        esid, raw_zip.name, twin_zip.name,
    )
    try:
        zips_match = (
            azus_common.calculate_sha512(str(raw_zip))
            == azus_common.calculate_sha512(str(twin_zip))
        )
    except OSError as exc:
        row["ZIPs Match"] = f"cannot compare (unreadable: {exc})"
        row["Verdict"] = CANNOT_VERIFY
        return row
    if zips_match:
        row["ZIPs Match"] = "yes (SHA-512 identical)"
        row["Verdict"] = VERIFIED_DUPLICATE
    else:
        row["ZIPs Match"] = "NO — hashes differ"
        row["Verdict"] = CANNOT_VERIFY
    return row


def delete_verified_duplicate(raw_folder: Path, raw_root: Path) -> bool:
    """Delete one raw-side folder AFTER re-checking every safety gate.

    Every gate from the module docstring's safety model is re-verified
    here, independently of the earlier classification, so a filesystem
    change between the two moments cannot slip through.  The twin
    ZIP's inode, size, and mtime are captured before the hashing pass
    and compared after it, and the twin's sentinel is confirmed again
    right before the removal.

    Args:
        raw_folder: The raw-side ``ESID_*_Staging`` directory.
        raw_root: The scanned Raw Data folder; only its DIRECT
            children may be deleted.

    Returns:
        True when the folder was deleted; False when any gate failed
        (the failure is logged and the folder is left untouched).
    """
    if not _same_inode(raw_folder.parent, raw_root):
        logger.error(
            "REFUSING to delete %s — it is not a direct child of %s.",
            raw_folder, raw_root,
        )
        return False
    twin = find_twin(raw_folder.name)
    if twin is None:
        logger.error("REFUSING to delete %s — twin missing.", raw_folder)
        return False
    if _twin_is_or_inside(raw_folder, twin):
        logger.error(
            "REFUSING to delete %s — twin %s is (or is inside) this "
            "folder; the paths alias the same data.", raw_folder, twin,
        )
        return False
    if not (twin / _SENTINEL).is_file():
        logger.error(
            "REFUSING to delete %s — twin %s has no sentinel.",
            raw_folder, twin,
        )
        return False
    raw_zip = _single_zip(raw_folder)
    twin_zip = _single_zip(twin)
    if raw_zip is None or twin_zip is None:
        logger.error(
            "REFUSING to delete %s — missing or multiple ZIPs.", raw_folder,
        )
        return False
    if not _zips_are_independent_copies(raw_zip, twin_zip):
        logger.error(
            "REFUSING to delete %s — the two ZIPs are not independent "
            "physical copies.", raw_folder,
        )
        return False
    if _diverging_upload_artifacts(raw_folder, twin):
        logger.error(
            "REFUSING to delete %s — it holds upload artifacts the twin "
            "lacks or differs on (possible Zenodo draft link).", raw_folder,
        )
        return False

    try:
        before = twin_zip.stat()
        hashes_match = (
            azus_common.calculate_sha512(str(raw_zip))
            == azus_common.calculate_sha512(str(twin_zip))
        )
        after = twin_zip.stat()
    except OSError as exc:
        logger.error(
            "REFUSING to delete %s — a ZIP became unreadable during "
            "verification (%s).", raw_folder, exc,
        )
        return False
    if not hashes_match:
        logger.error(
            "REFUSING to delete %s — ZIP hashes differ.", raw_folder,
        )
        return False
    if (before.st_dev, before.st_ino, before.st_size,
            before.st_mtime_ns) != (after.st_dev, after.st_ino,
                                    after.st_size, after.st_mtime_ns):
        logger.error(
            "REFUSING to delete %s — the twin ZIP changed while it was "
            "being verified.", raw_folder,
        )
        return False
    if not (twin / _SENTINEL).is_file():
        logger.error(
            "REFUSING to delete %s — twin %s lost its sentinel during "
            "verification.", raw_folder, twin,
        )
        return False
    shutil.rmtree(raw_folder)
    logger.info("Deleted verified duplicate: %s", raw_folder)
    return True


def find_leftovers(raw_root: Path) -> List[Path]:
    """List the raw-side staging-build leftovers under ``raw_root``.

    A leftover is a directory named ``ESID_<esid>_Staging`` (any valid
    ESID, suffixed forms included; the ``_Staging`` tail is matched
    case-insensitively, like the rest of the ESID grammar) sitting
    directly in the Raw Data folder — the location
    ``prepare_dataset.py`` builds in before its move into
    ``Staging_Area/``.

    Args:
        raw_root: The Raw Data folder to scan (not recursive).

    Returns:
        Matching directories in ESID order.
    """
    leftovers = [
        entry for entry in raw_root.iterdir()
        if entry.is_dir()
        and entry.name.lower().endswith("_staging")
        and azus_common.parse_esid(entry.name) is not None
    ]
    leftovers.sort(
        key=lambda p: azus_common.esid_sort_key(
            azus_common.parse_esid(p.name)
        )
    )
    return leftovers


def main() -> None:
    """Command-line entry point.  See the module docstring for details."""
    parser = argparse.ArgumentParser(
        description=(
            "Classify stale ESID_*_Staging leftovers in a Raw Data "
            "folder against their Staging_Area/Uploaded_Data twins, and "
            "optionally delete the SHA-verified duplicates. Dry-run by "
            "default — nothing is deleted without "
            "--delete-verified-duplicates."
        ),
    )
    parser.add_argument(
        "raw_data_dir", metavar="RAW_DATA_DIR",
        help="Folder whose leftover ESID_*_Staging directories to classify.",
    )
    parser.add_argument(
        "--delete-verified-duplicates", action="store_true",
        help=(
            "Delete raw-side folders whose twin is sentineled and whose "
            "ZIP is an independent, byte-identical (SHA-512) copy. Every "
            "other verdict is reported and left untouched. Without this "
            "flag the tool only reports (dry run)."
        ),
    )
    parser.add_argument(
        "--output", metavar="PATH", default=None,
        help=(
            "CSV report path (default: "
            "raw_staging_leftovers_YYYYMMDD_HHMMSS.csv in the current "
            "directory)."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    raw_root = Path(args.raw_data_dir)
    if not raw_root.is_dir():
        logger.error("Raw data folder not found: %s", raw_root)
        sys.exit(2)
    # Inode comparison, NOT path-string comparison: on a case-
    # insensitive filesystem "staging_area" is the same physical
    # directory as "Staging_Area" but a different string, and scanning
    # it would classify every completed folder as its own duplicate.
    for area in (_STAGING_AREA, _UPLOADED_DATA):
        if area.is_dir() and _same_inode(raw_root, area):
            logger.error(
                "REFUSING to scan %s — that is the pipeline's own area "
                "(%s); every folder there would classify as its own "
                "twin. Point this tool at the RAW DATA folder.",
                raw_root, area,
            )
            sys.exit(2)

    logger.info("=" * 70)
    logger.info("RAW-SIDE STAGING LEFTOVERS — classify%s",
                " + delete verified duplicates"
                if args.delete_verified_duplicates else " (dry run)")
    logger.info("=" * 70)
    logger.info("Scanning: %s", raw_root)

    leftovers = find_leftovers(raw_root)
    if not leftovers:
        logger.info("No ESID_*_Staging leftovers found — nothing to do.")
        sys.exit(0)
    logger.info("Found %d leftover folder(s).", len(leftovers))

    output_path = (
        Path(args.output) if args.output
        else azus_common.timestamped_output_path("raw_staging_leftovers")
    )
    remaining = 0
    # The CSV is written incrementally (row + flush per leftover) so a
    # crash mid-run cannot lose the audit record of deletions already
    # performed.
    with open(output_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS)
        writer.writeheader()
        fh.flush()
        for raw_folder in leftovers:
            row = classify_leftover(raw_folder)
            verdict = row["Verdict"]
            if verdict == VERIFIED_DUPLICATE and \
                    args.delete_verified_duplicates:
                if delete_verified_duplicate(raw_folder, raw_root):
                    row["Action Taken"] = "deleted raw-side duplicate"
                else:
                    row["Action Taken"] = "deletion refused — see log"
                    remaining += 1
            else:
                row["Action Taken"] = "none (dry run)" \
                    if verdict == VERIFIED_DUPLICATE else "none"
                remaining += 1
            logger.info(
                "[ESID %s] %s — %s", row["ESID#"], verdict,
                row["Action Taken"] if row["Action Taken"] != "none"
                else _RECOMMENDED_ACTION[verdict],
            )
            writer.writerow(row)
            fh.flush()
    logger.info(
        "Report written: %s (%d row(s))", output_path, len(leftovers)
    )

    logger.info("=" * 70)
    if remaining:
        logger.info(
            "%d leftover(s) still need attention (see the Verdict "
            "column and the module docstring for recommended actions).",
            remaining,
        )
        sys.exit(1)
    logger.info("All leftovers were verified duplicates and were deleted.")
    sys.exit(0)


if __name__ == "__main__":
    main()
