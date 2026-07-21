#!/usr/bin/env python3
"""Re-prepare ESID sites whose ZIPs were blocked by zero-byte or
pre-1980-timestamp WAV files.

WHAT THIS TOOL DOES
===================
Older preparation runs could fail to produce a ZIP for two raw-data
conditions that are now handled by ``prepare_dataset.py``:

  * **Pre-1980 modification times** — an AudioMoth whose clock reset to
    the 1970 Unix epoch stamps its files with mtimes ZIP's DOS
    timestamp field cannot represent.  Preparation now clamps the
    file's filesystem modification time to 1980-01-01 (metadata only —
    filenames and file contents are never touched).
  * **Zero-byte WAVs** — a dead or failed recorder writes genuinely
    empty files, which the pre-sentinel verification used to treat as
    fatal.  They are now included in the archive (and warned about).

This wrapper scans a raw-data folder, finds every ESID site that has
NO completed ZIP (no ``.prep_complete`` sentinel in Staging_Area/ and
no Uploaded_Data/ folder) AND whose raw WAVs show one of the two
blocking symptoms, and re-runs ``prepare_dataset.py`` on each so the
updated code can finish them.

Sites that are incomplete for OTHER reasons are listed but not
re-prepped by this tool — use ``prep_all_datasets.py`` or
``reprep_incomplete_staging.py`` for the general case.

USAGE
=====
From the project root::

    python Resources/reprep_missing_zips.py /path/to/Raw_Data
    python Resources/reprep_missing_zips.py /path/to/Raw_Data --list-only
    python Resources/reprep_missing_zips.py /path/to/Raw_Data \\
        --config Resources/config.json --eclipse-type total

EXIT CODES
==========
* ``0`` — every selected site re-prepped successfully (or nothing to do,
  or ``--list-only``)
* ``1`` — at least one re-prep failed
* ``2`` — usage error (raw-data folder missing)
"""

import argparse
import logging
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

import azus_common
import prep_all_datasets
from prepare_dataset import _ZIP_MIN_MTIME

logger = logging.getLogger("azus.reprep_missing")

_PROJECT_ROOT = azus_common.PROJECT_ROOT


def find_blocking_wavs(folder: Path) -> Tuple[List[str], List[str]]:
    """Find the WAVs in one raw folder that used to block ZIP creation.

    Scans the folder's top-level ``.wav``/``.WAV`` files (not
    recursive; macOS ``._*`` sidecars skipped) for the two symptoms the
    updated ``prepare_dataset.py`` now handles.

    Args:
        folder: The raw ESID folder to scan.

    Returns:
        A ``(zero_byte_names, pre_1980_names)`` tuple of filename
        lists; a file with both symptoms appears in both lists.  Files
        whose metadata cannot be read are skipped with a warning (they
        will surface again during the re-prep itself).
    """
    zero_byte: List[str] = []
    pre_1980: List[str] = []
    for entry in sorted(folder.iterdir()):
        if not entry.is_file() or not entry.name.lower().endswith(".wav"):
            continue
        if entry.name.startswith("._"):
            continue
        try:
            stat = entry.stat()
        except OSError as exc:
            logger.warning(
                "Could not stat %s (%s) — skipping it in the scan.",
                entry, exc,
            )
            continue
        if stat.st_size == 0:
            zero_byte.append(entry.name)
        if stat.st_mtime < _ZIP_MIN_MTIME:
            pre_1980.append(entry.name)
    return zero_byte, pre_1980


def discover_blocked_sites(
    raw_root: Path,
) -> Tuple[List[Tuple[str, Path, List[str], List[str]]], List[str]]:
    """Find ESID sites with no completed ZIP and a blocking symptom.

    A site is selected when BOTH hold:

      1. It has no completed preparation —
         :func:`prep_all_datasets.already_prepared` finds neither a
         sentineled ``Staging_Area/ESID_NNN_Staging/`` nor an
         ``Uploaded_Data/ESID_NNN_Uploaded/`` folder.
      2. Its raw folder holds at least one zero-byte or pre-1980-mtime
         WAV (see :func:`find_blocking_wavs`).

    Args:
        raw_root: Folder whose ESID-named subfolders hold raw WAVs.

    Returns:
        A ``(selected, other_incomplete)`` tuple.  ``selected`` holds
        ``(esid, raw_folder, zero_byte_names, pre_1980_names)`` in ESID
        order; ``other_incomplete`` names ESIDs that also lack a
        completed ZIP but show neither symptom — callers must surface
        these so nothing disappears silently.
    """
    selected: List[Tuple[str, Path, List[str], List[str]]] = []
    other_incomplete: List[str] = []
    for _, esid, folder in azus_common.find_esid_folders(raw_root):
        if prep_all_datasets.already_prepared(esid) is not None:
            continue
        zero_byte, pre_1980 = find_blocking_wavs(folder)
        if zero_byte or pre_1980:
            selected.append((esid, folder, zero_byte, pre_1980))
        else:
            other_incomplete.append(esid)
    return selected, other_incomplete


def run_prepare_dataset(
    esid_folder: Path, config_path: str, eclipse_type: str
) -> int:
    """Invoke prepare_dataset.py as a subprocess; return its exit code.

    Same invocation pattern as the other batch tools: same interpreter,
    cwd pinned to the project root (config_path is a relative path by
    default), output streamed live rather than captured.

    Args:
        esid_folder: Raw ESID folder passed as prepare_dataset.py's
            positional ``folder`` argument.
        config_path: Path to config.json, forwarded via ``--config``.
        eclipse_type: Forwarded via ``--eclipse-type`` (``total``,
            ``annular``, or ``partial``).

    Returns:
        The subprocess exit code (0 on success).
    """
    cmd = [
        sys.executable,
        str(_PROJECT_ROOT / "Resources" / "prepare_dataset.py"),
        str(esid_folder),
        "--config", config_path,
        "--eclipse-type", eclipse_type,
    ]
    logger.info("Running: %s", " ".join(cmd))
    completed = subprocess.run(cmd, cwd=_PROJECT_ROOT)
    return completed.returncode


def main() -> None:
    """Command-line entry point.  See the module docstring for usage."""
    parser = argparse.ArgumentParser(
        description=(
            "Find ESID sites in RAW_DATA_DIR that have no completed ZIP "
            "because of zero-byte or pre-1980-timestamp WAVs, and re-run "
            "prepare_dataset.py on each with the updated handling."
        ),
    )
    parser.add_argument(
        "raw_data_dir", metavar="RAW_DATA_DIR",
        help="Folder whose ESID subfolders hold the raw .WAV files.",
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
        "--list-only", action="store_true",
        help="List what would be re-prepped without running anything.",
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

    logger.info("=" * 70)
    logger.info("RE-PREP SITES BLOCKED BY ZERO-BYTE / PRE-1980 WAVS")
    logger.info("=" * 70)
    logger.info("Scanning: %s", raw_root)

    selected, other_incomplete = discover_blocked_sites(raw_root)

    if other_incomplete:
        logger.info(
            "%d site(s) lack a completed ZIP for OTHER reasons (no "
            "zero-byte or pre-1980 WAVs) — NOT re-prepped by this tool: %s",
            len(other_incomplete), ", ".join(other_incomplete),
        )
    if not selected:
        logger.info(
            "No sites are blocked by zero-byte or pre-1980 WAVs — "
            "nothing to do."
        )
        sys.exit(0)

    logger.info("%d site(s) selected for re-prep:", len(selected))
    for esid, folder, zero_byte, pre_1980 in selected:
        symptoms = []
        if zero_byte:
            symptoms.append(f"{len(zero_byte)} zero-byte WAV(s)")
        if pre_1980:
            symptoms.append(f"{len(pre_1980)} pre-1980 WAV(s)")
        logger.info(
            "  ESID %s  (%s)  %s", esid, "; ".join(symptoms), folder,
        )

    if args.list_only:
        logger.info("--list-only: not running any re-preps.")
        sys.exit(0)

    failures: List[str] = []
    for esid, folder, _, _ in selected:
        logger.info("=" * 70)
        logger.info("[ESID %s] Re-preparing %s", esid, folder)
        returncode = run_prepare_dataset(
            folder, args.config, args.eclipse_type
        )
        if returncode == 0:
            logger.info("[ESID %s] Re-prep succeeded.", esid)
        else:
            logger.error(
                "[ESID %s] Re-prep FAILED (exit code %d).", esid, returncode
            )
            failures.append(esid)

    logger.info("=" * 70)
    logger.info(
        "Re-prep complete: %d succeeded, %d failed.",
        len(selected) - len(failures), len(failures),
    )
    if failures:
        logger.error("Failed ESIDs: %s", ", ".join(failures))
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
