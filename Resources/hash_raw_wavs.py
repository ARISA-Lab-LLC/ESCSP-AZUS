#!/usr/bin/env python3
"""Pre-compute and cache SHA-512 hashes of raw WAV files, per ESID folder.

PURPOSE
=======
The file-by-file upload fallback verifies every raw WAV and ``CONFIG.TXT``
against the SHA-512 that ``prepare_dataset.py`` recorded in
``file_list.csv``, BEFORE uploading anything.  That check is what proves
the audio being published still matches the manifest published beside it —
but it reads every byte of the dataset, so on a 40 GB site it is a
complete pass over the data before a single byte moves.  Worse, it used to
be thrown away: an upload that died after two hours re-hashed everything
from scratch on the next run.

This module makes that work durable.  Each raw ESID folder gets a
``wav_hashes.csv`` recording every audio file's name, size, modification
time and SHA-512.  A later run reuses a cached hash whenever the file's
size AND mtime still match, and hashes only what is new or changed — so a
restart re-reads nothing.

Run as a CLI it pre-warms the cache for a whole ``Raw_Data`` tree, which
can be done at any convenient time (overnight, before a batch) so the
upload run itself never pays for hashing.  ``file_by_file_upload.py``
imports the same functions, so an un-warmed folder is simply hashed and
cached on first use — the CLI is an optimisation, never a prerequisite.

WHY SIZE + MTIME AND NOT JUST THE NAME
======================================
A cache keyed on filename alone would defeat the very check it serves: a
WAV modified after the cache was written would be waved through, which is
exactly the corruption the hash gate exists to catch.  So an entry is
trusted only when the file's current size and mtime both still match what
was recorded.  That costs one ``stat`` per file — microseconds — and any
mismatch simply re-hashes that one file.  The cache can never make
verification weaker than doing it from scratch; it can only make it
faster.

Note ``prepare_dataset.py`` clamps pre-1980 modification times (an unset
AudioMoth clock stamps 1970) to 1980-01-01.  A re-prep therefore
invalidates the cache entries for exactly those files, which re-hash on
next use.  That is correct, and it fails toward doing more work rather
than less.

WHAT IS HASHED
==============
Every ``*.wav``/``*.WAV`` plus ``CONFIG.TXT`` — the set the file-by-file
path uploads from ``Raw_Data``.  macOS AppleDouble sidecars (``._*``) and
any other hidden file are skipped.  The cache file itself is never
uploaded to Zenodo: the upload set comes from ``file_list.csv``, and
``prepare_dataset.py`` only ever takes WAVs and ``CONFIG.TXT`` out of a raw
folder, so a sidecar CSV here cannot reach a record.

USAGE
=====
::

    # Pre-warm every ESID folder (safe to re-run; only does new work)
    python Resources/hash_raw_wavs.py /path/to/Raw_Data

    # One site, naming each file as it is hashed
    python Resources/hash_raw_wavs.py /path/to/Raw_Data --esid 445 --verbose

    # Ignore the caches and hash everything again (a real re-verification)
    python Resources/hash_raw_wavs.py /path/to/Raw_Data --recheck

EXIT CODES
==========
* ``0`` — every folder's cache is complete
* ``1`` — at least one file could not be read (its folder's cache is
  written for everything that could be)
* ``2`` — usage error (folder missing, bad ``--esid``)
"""

import argparse
import csv
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import azus_common
# Reused so --esid behaves identically here and in prep_all_datasets:
# same accepted values, same "in the given order" semantics, same
# reporting of requested ESIDs that have no folder.  (prep_all_datasets
# imports only stdlib + azus_common, so this adds no weight.)
from prep_all_datasets import filter_and_order_discovered

logger = logging.getLogger("azus.wav_hashes")

# The per-folder cache file.  Lives inside the raw ESID folder so it
# travels with the data — including through split_oversized_raw_folders.py,
# where a same-filesystem rename preserves size and mtime and therefore
# keeps the cached hashes valid in whichever half a file lands in.
CACHE_FILENAME = "wav_hashes.csv"

_CACHE_COLUMNS = ["File Name", "File Size (Bytes)", "Modified (epoch)", "SHA-512"]


@dataclass
class HashResult:
    """Outcome of resolving hashes for one folder.

    Attributes:
        hashes: ``{name: sha512}`` for every file successfully hashed or
            served from the cache.  A name absent here could not be read.
        reused: How many hashes came from the cache unchanged.
        hashed: How many files were read and hashed this call.
        missing: Requested names that are not present in the folder.
        errors: ``"name: reason"`` for each file that could not be read.
    """

    hashes: Dict[str, str] = field(default_factory=dict)
    reused: int = 0
    hashed: int = 0
    missing: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def is_hashable_name(name: str) -> bool:
    """Report whether a file in a raw folder is part of the hashed set.

    Matches what the file-by-file path uploads out of ``Raw_Data``: audio
    plus the device config.  Hidden files — including macOS AppleDouble
    sidecars, which also end in ``.WAV`` but are not recordings — are
    excluded.

    Args:
        name: A bare file name.

    Returns:
        True for a visible WAV file or ``CONFIG.TXT`` (case-insensitive).
    """
    if name.startswith("."):
        return False
    return name.lower().endswith(".wav") or name.upper() == "CONFIG.TXT"


def cache_path(folder: Path) -> Path:
    """Return the cache file's path for one raw ESID folder.

    Args:
        folder: The raw ESID folder.

    Returns:
        Path to that folder's ``wav_hashes.csv`` (which may not exist).
    """
    return folder / CACHE_FILENAME


def load_cache(folder: Path) -> Dict[str, Tuple[int, int, str]]:
    """Read a folder's hash cache, tolerating absence and corruption.

    A missing, unreadable or malformed cache reads as empty rather than
    raising: the only consequence is that hashes get recomputed, so a bad
    cache costs time and never correctness.  Rows missing a field, or
    carrying a non-numeric size/mtime, are skipped individually.

    Args:
        folder: The raw ESID folder.

    Returns:
        ``{name: (size, mtime, sha512)}`` for every well-formed row.
    """
    path = cache_path(folder)
    if not path.is_file():
        return {}
    entries: Dict[str, Tuple[int, int, str]] = {}
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                name = (row.get("File Name") or "").strip()
                digest = (row.get("SHA-512") or "").strip()
                if not name or not digest:
                    continue
                try:
                    size = int((row.get("File Size (Bytes)") or "").strip())
                    mtime = int((row.get("Modified (epoch)") or "").strip())
                except ValueError:
                    continue
                entries[name] = (size, mtime, digest)
    except (OSError, csv.Error) as exc:
        logger.warning(
            "Could not read %s (%s) — treating the cache as empty.", path, exc,
        )
        return {}
    return entries


def write_cache(folder: Path, entries: Dict[str, Tuple[int, int, str]]) -> None:
    """Write a folder's hash cache atomically, in name order.

    Written via a hidden temp file plus ``os.replace`` so an interrupted
    write can never leave a half-formed cache under the real name.  A
    failure is logged and swallowed — losing the cache costs time on the
    next run, and must not fail an upload that is otherwise fine.

    Args:
        folder: The raw ESID folder.
        entries: ``{name: (size, mtime, sha512)}`` to record.
    """
    path = cache_path(folder)
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=_CACHE_COLUMNS)
            writer.writeheader()
            for name in sorted(entries):
                size, mtime, digest = entries[name]
                writer.writerow({
                    "File Name": name,
                    "File Size (Bytes)": size,
                    "Modified (epoch)": mtime,
                    "SHA-512": digest,
                })
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("Could not write %s (%s).", path, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def ensure_hashes(
    folder: Path,
    names: Sequence[str],
    *,
    tag: str = "",
    recheck: bool = False,
) -> HashResult:
    """Resolve SHA-512 for ``names``, reusing and updating the folder cache.

    For each requested name: if the cache holds an entry whose recorded
    size and mtime still match the file on disk, that hash is reused
    without reading a byte; otherwise the file is hashed and the cache
    entry replaced.  The cache is rewritten only when something changed.

    Args:
        folder: The raw ESID folder holding the files.
        names: File names to resolve (relative to ``folder``).
        tag: Log prefix, e.g. ``"[ESID 445]"``.
        recheck: Ignore cached hashes and re-read every file.  Use to
            genuinely re-verify rather than to go fast.

    Returns:
        A :class:`HashResult`.  Unreadable files are reported in its
        ``errors`` and omitted from ``hashes`` — never guessed at.
    """
    prefix = f"{tag} " if tag else ""
    cache = {} if recheck else load_cache(folder)
    result = HashResult()
    updated: Dict[str, Tuple[int, int, str]] = dict(cache)
    changed = False

    for name in names:
        path = folder / name
        try:
            stat = path.stat()
        except OSError:
            result.missing.append(name)
            continue
        size, mtime = stat.st_size, int(stat.st_mtime)

        cached = cache.get(name)
        if cached is not None and cached[0] == size and cached[1] == mtime:
            result.hashes[name] = cached[2]
            result.reused += 1
            continue

        logger.debug("%sHashing %s (%d bytes)...", prefix, name, size)
        try:
            digest = azus_common.calculate_sha512(str(path))
        except OSError as exc:
            result.errors.append(f"{name}: {exc}")
            updated.pop(name, None)
            changed = True
            continue
        result.hashes[name] = digest
        result.hashed += 1
        updated[name] = (size, mtime, digest)
        changed = True

    if changed:
        write_cache(folder, updated)
    return result


def _report(folder: Path, result: HashResult, tag: str) -> None:
    """Log one folder's outcome.

    Args:
        folder: The folder just processed.
        result: Its :class:`HashResult`.
        tag: Log prefix.
    """
    if result.hashed == 0 and not result.errors:
        logger.info(
            "%s already up to date — %d file(s) served from %s, nothing read.",
            tag, result.reused, cache_path(folder).name,
        )
    else:
        logger.info(
            "%s %d hashed, %d reused from cache -> %s",
            tag, result.hashed, result.reused, cache_path(folder).name,
        )
    if result.missing:
        logger.warning(
            "%s %d requested file(s) not present: %s",
            tag, len(result.missing), ", ".join(result.missing[:5]),
        )
    for problem in result.errors[:5]:
        logger.error("%s could not read %s", tag, problem)


def main() -> None:
    """Command-line entry point.  See the module docstring for details."""
    parser = argparse.ArgumentParser(
        description=(
            "Hash every raw WAV + CONFIG.TXT under a Raw_Data folder and "
            "cache the results in a wav_hashes.csv inside each ESID "
            "subfolder, so the file-by-file upload never has to re-read the "
            "dataset. Safe to re-run: only new or changed files are hashed."
        ),
    )
    parser.add_argument(
        "raw_data_dir", metavar="RAW_DATA_DIR",
        help="Folder holding the raw ESID subfolders (ESID#NNN / ESID_NNN).",
    )
    parser.add_argument(
        "--esid", nargs="+", default=None, metavar="ESID_OR_CSV",
        help=(
            "Hash only the specified ESID(s), IN THE GIVEN ORDER. Each value "
            "is either a literal ESID (1-3 digits, or a suffixed id like "
            "120A / 122_Part_1_of_2) or the path to a CSV whose first column "
            "lists ESIDs (header row optional); numbers and CSV paths may be "
            "mixed. Requested ESIDs with no raw folder are reported and "
            "skipped. Without this flag, every ESID folder is hashed in "
            "numerical order. (Same semantics as prep_all_datasets.py.)"
        ),
    )
    parser.add_argument(
        "--recheck", action="store_true",
        help=(
            "Ignore the existing caches and re-read every file. Use this to "
            "genuinely re-verify the data, not to go faster."
        ),
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Name every file as it is hashed.",
    )
    args = parser.parse_args()

    azus_common.configure_logging(args.verbose)

    raw_root = Path(args.raw_data_dir)
    if not raw_root.is_dir():
        logger.error("Raw data folder not found: %s", raw_root)
        sys.exit(2)

    folders = azus_common.find_esid_folders(raw_root)
    order_desc = "in numerical order"
    if args.esid:
        try:
            requested = azus_common.load_esid_args(args.esid)
        except ValueError as exc:
            logger.error("Invalid --esid value: %s", exc)
            sys.exit(2)
        folders, missing = filter_and_order_discovered(folders, requested)
        if missing:
            logger.warning(
                "%d requested ESID(s) have no raw folder under %s and will "
                "be skipped: %s",
                len(missing), raw_root, ", ".join(missing),
            )
        if not folders:
            logger.error(
                "None of the requested --esid value(s) match a raw folder "
                "under %s — nothing to do.", raw_root,
            )
            sys.exit(2)
        order_desc = "in --esid order"
    if not folders:
        logger.info("No ESID subfolders found in %s — nothing to do.", raw_root)
        sys.exit(0)

    logger.info("=" * 70)
    logger.info("RAW WAV HASH CACHE%s", " — RECHECK (ignoring caches)"
                if args.recheck else "")
    logger.info("=" * 70)
    logger.info("Raw data: %s", raw_root.resolve())
    logger.info("Folders:  %d (%s)", len(folders), order_desc)
    logger.info("Cache:    %s (one per ESID folder)", CACHE_FILENAME)
    logger.info("=" * 70)

    total_hashed = total_reused = 0
    problem_esids: List[str] = []
    for _sort, esid, folder in folders:
        tag = f"[ESID {esid}]"
        try:
            names = sorted(
                entry.name for entry in folder.iterdir()
                if entry.is_file() and is_hashable_name(entry.name)
            )
        except OSError as exc:
            logger.error("%s could not list %s (%s)", tag, folder, exc)
            problem_esids.append(esid)
            continue
        if not names:
            logger.warning("%s no WAV files or CONFIG.TXT found — skipped.", tag)
            continue
        result = ensure_hashes(folder, names, tag=tag, recheck=args.recheck)
        _report(folder, result, tag)
        total_hashed += result.hashed
        total_reused += result.reused
        if result.errors:
            problem_esids.append(esid)

    logger.info("=" * 70)
    logger.info("SUMMARY")
    logger.info("=" * 70)
    logger.info("Folders processed: %d", len(folders))
    logger.info("Files hashed:      %d", total_hashed)
    logger.info("Reused from cache: %d", total_reused)
    if problem_esids:
        logger.error(
            "ESID(s) with unreadable file(s): %s", ", ".join(problem_esids)
        )
    logger.info("=" * 70)
    sys.exit(1 if problem_esids else 0)


if __name__ == "__main__":
    main()
