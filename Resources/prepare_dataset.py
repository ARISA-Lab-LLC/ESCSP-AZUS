#!/usr/bin/env python3
"""AZUS Dataset Preparation Script.

Takes a folder of raw data files (e.g., WAV audio + CONFIG.TXT) and prepares
a complete dataset package ready for upload to Zenodo.

Input:  Raw data folder (e.g., ESID#005/ with .WAV files and CONFIG.TXT)
Output: Staging directory with ZIP, README, file_list, metadata CSVs, etc.

The README.html is generated from a template file (Resources/README_template.html)
using Python string.Template substitution.  No HTML is hardcoded in this script.

Usage:
    # Read collectors_csv from config.json (recommended — single source of truth):
    python prepare_dataset.py ESID#005 --config Resources/config.json \\
        --eclipse-type total [--resources-dir Resources] [--output-dir ...]

    # Or supply the path directly (overrides config.json if both given):
    python prepare_dataset.py ESID#005 --collector-csv /path/to/collectors.csv \\
        --eclipse-type total [--resources-dir Resources] [--output-dir ...]
"""

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path
from string import Template
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# Sibling module in Resources/ — reused for the pre-sentinel ZIP
# verification (RIFF-header cross-checked disk scan + ZIP index scan).
# Resolves because Python puts this script's own directory on sys.path
# when run by path (as prep_all_datasets.py does via subprocess).
import audit_wav_integrity
import azus_common

logger = logging.getLogger("azus.prepare")



# ZIP's DOS timestamp field cannot represent dates before 1980-01-01.
# AudioMoth real-time clocks reset to the 1970 Unix epoch on power loss,
# so recordings made before the clock was set carry pre-1980 mtimes.
# Used only to log which files will have their ZIP-entry timestamp
# clamped (the recording start time lives in the WAV filename, so the
# clamp loses nothing scientific).
_ZIP_MIN_MTIME = 315_532_800  # 1980-01-01T00:00:00 UTC as a Unix epoch


# ---------------------------------------------------------------------
# THE PREP CONTRACT — what a completed staging folder contains.
# ---------------------------------------------------------------------
# audit_prep_completeness.py imports these to verify prepared folders
# and back-fill the completion sentinel, so they must live HERE, next
# to the code that produces the files.  If you add, remove, or rename a
# prep output anywhere in this module, update these tuples in the same
# change — the auditor certifies folders as complete against exactly
# this list.
#
# Files that appear DIRECTLY in the staging/uploaded folder
# (``{esid}`` = the 3-digit ESID number):
STAGING_OUTPUT_FILES: Tuple[str, ...] = (
    "ESID_{esid}.zip",
    "ESID_{esid}_to_upload.csv",
    "README.html",
    "README.md",
    "file_list.csv",
    "total_eclipse_data.csv",
)

# Prep-generated metadata files that ALSO get appended into the ZIP
# (under the ESID_NNN/ prefix), on top of the raw WAVs + CONFIG.TXT +
# resource companions:
ZIP_METADATA_ENTRIES: Tuple[str, ...] = (
    "README.md",
    "file_list.csv",
    "total_eclipse_data.csv",
)

# Files copied only conditionally (site-Keywords dependent), so their
# absence is ambiguous rather than proof of an incomplete prep:
CONDITIONAL_FILES: Tuple[str, ...] = ("related_identifiers.csv",)

# Prepared-folder naming templates:
STAGING_FOLDER_TEMPLATE = "ESID_{esid}_Staging"
UPLOADED_FOLDER_TEMPLATE = "ESID_{esid}_Uploaded"


# ===================================================================
#  Utility functions
# ===================================================================

# Shared streaming SHA-512 (one definition for the whole suite).
calculate_sha512 = azus_common.calculate_sha512


def get_esid_from_folder(folder_name: str) -> Optional[str]:
    """Extract the canonical ESID from a folder name.

    Handles the standard forms (ESID#005, ESID_005, unpadded ESID_5,
    suffixed ESID#120A / ESID_122_Part_1_of_2) via the shared
    bounds-checked parser, plus a bare folder name with no ``ESID``
    prefix ('005', '120A').  Always returns the canonical form (padded
    3-digit number plus any suffix) — unpadded input used to leak
    through unpadded, producing staging folders the batch tools' skip
    checks could not see.

    Args:
        folder_name: Name of the data folder.

    Returns:
        The canonical ESID string (e.g. '005', '120A'), or None when
        the name carries no valid ESID.
    """
    esid = azus_common.parse_esid(folder_name)
    if esid is not None:
        return esid
    try:
        return azus_common.normalize_esid(folder_name)
    except ValueError:
        return None


# ===================================================================
#  ZIP creation
# ===================================================================

def create_zip_file(
    source_dir: Path,
    output_dir: Path,
    esid: str,
) -> Tuple[Path, Dict[str, str]]:
    """Create a ZIP archive of all WAV files and CONFIG.TXT.

    All files are stored inside a subfolder named ``ESID_XXX/`` within the
    archive.  This ensures that extracting the ZIP produces a single,
    self-contained directory rather than a flat file dump.

    SHA-512 hashes are computed IN the write pass — each source file is
    read exactly once, with every 64 KB chunk feeding both the ZIP
    compressor and the hasher.  (An earlier version called
    ``zipf.write()`` and then re-hashed the file, silently reading every
    gigabyte of raw audio twice.)

    Args:
        source_dir: Directory containing raw WAV files and CONFIG.TXT.
        output_dir: Where to save the ZIP file.
        esid: ESID number string (e.g., ``'005'``).

    Returns:
        Tuple of:

        - ``zip_path``: Path to the created ZIP file.
        - ``content_hashes``: Dict mapping each archived filename
          (e.g., ``'20240408_120000.WAV'``) to its SHA-512 hex digest,
          computed during the write pass.
    """
    zip_filename = f"ESID_{esid}.zip"
    zip_path = output_dir / zip_filename

    # All entries are stored under this subfolder inside the archive
    zip_subfolder = f"ESID_{esid}"

    logger.info("Creating ZIP file: %s", zip_filename)
    logger.info("  Archive subfolder: %s/", zip_subfolder)

    # --- Locate CONFIG.TXT (case-insensitive fallback) ---
    config_file: Optional[Path] = source_dir / "CONFIG.TXT"
    if not config_file.exists():
        config_file = source_dir / "CONFIG.txt"
    if not config_file.exists():
        logger.warning("CONFIG.TXT not found in %s", source_dir)
        config_file = None

    # WAV files sorted for deterministic archive ordering
    wav_files = sorted(source_dir.glob("*.WAV")) + sorted(source_dir.glob("*.wav"))

    # Files whose mtime predates the ZIP timestamp epoch (an unset
    # AudioMoth clock stamps 1970) get their FILESYSTEM modification
    # time clamped to 1980-01-01T00:00:00 UTC — the earliest time ZIP's
    # DOS field can represent.  Only the file's system metadata changes:
    # the filename (which carries the recording time) and the file's
    # CONTENTS are never touched.  If the metadata cannot be written
    # (e.g. a read-only mount), strict_timestamps=False below still
    # clamps the ZIP-entry timestamp so the archive is created either
    # way.
    candidates = ([config_file] if config_file is not None else []) + wav_files
    pre_1980 = [f for f in candidates if f.stat().st_mtime < _ZIP_MIN_MTIME]
    if pre_1980:
        logger.warning(
            "  %d file(s) have modification times before 1980 (AudioMoth "
            "clock was likely unset) — clamping their filesystem "
            "modification times to 1980-01-01. Filenames and file "
            "contents are unaffected. First few: %s",
            len(pre_1980), ", ".join(f.name for f in pre_1980[:5]),
        )
        for f in pre_1980:
            try:
                os.utime(f, (f.stat().st_atime, _ZIP_MIN_MTIME))
            except OSError as exc:
                logger.warning(
                    "  Could not update modification time of %s (%s) — "
                    "its ZIP-entry timestamp will be clamped instead.",
                    f.name, exc,
                )

    # Populated during the write pass — the ONLY read of each source file
    content_hashes: Dict[str, str] = {}

    def _add_and_hash(zipf: zipfile.ZipFile, src: Path, arcname: str) -> None:
        """Stream ``src`` into the archive, hashing each chunk in-flight.

        Single-pass replacement for ``zipf.write()`` + a separate
        ``calculate_sha512()`` call (which read the file twice).
        ``ZipInfo.from_file`` preserves the source mtime/permissions the
        way ``zipf.write`` does; ``strict_timestamps=False`` clamps
        pre-1980 AudioMoth-epoch mtimes instead of raising.
        """
        zinfo = zipfile.ZipInfo.from_file(
            src, arcname, strict_timestamps=False
        )
        zinfo.compress_type = zipfile.ZIP_DEFLATED
        hasher = hashlib.sha512()
        with open(src, "rb") as fh, zipf.open(zinfo, "w") as dest:
            for chunk in iter(
                lambda: fh.read(azus_common.HASH_BUFFER_SIZE), b""
            ):
                hasher.update(chunk)
                dest.write(chunk)
        content_hashes[src.name] = hasher.hexdigest()

    # strict_timestamps=False clamps pre-1980 mtimes to 1980-01-01 instead
    # of raising ValueError ("ZIP does not support timestamps before 1980").
    with zipfile.ZipFile(
        zip_path, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False
    ) as zipf:

        # --- CONFIG.TXT first (small file, metadata context before audio) ---
        if config_file is not None:
            arcname = f"{zip_subfolder}/{config_file.name}"
            _add_and_hash(zipf, config_file, arcname)
            logger.info("  Added CONFIG.TXT → %s", arcname)

        # --- WAV audio files ---
        for i, wav_file in enumerate(wav_files, 1):
            arcname = f"{zip_subfolder}/{wav_file.name}"
            _add_and_hash(zipf, wav_file, arcname)
            if i % 100 == 0:
                logger.info("  ... added %d WAV files", i)

        logger.info("  Added %d WAV file(s)", len(wav_files))

    zip_size_mb = zip_path.stat().st_size / (1024 * 1024)
    logger.info(
        "  ZIP created: %.2f MB  (%d content hashes captured)",
        zip_size_mb,
        len(content_hashes),
    )

    return zip_path, content_hashes


# ===================================================================
#  Collector data extraction
# ===================================================================

def extract_collector_data(csv_file: Path, esid: str) -> Optional[Dict[str, str]]:
    """Extract collector data for a specific ESID from a CSV file.

    Args:
        csv_file: Path to the collectors CSV.
        esid: ESID to search for.

    Returns:
        Dictionary with collector row data, or None if not found.
    """
    logger.info("Extracting collector data for ESID %s", esid)

    if not csv_file.exists():
        logger.error("Collector CSV not found: %s", csv_file)
        return None

    with open(csv_file, "r", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            if row.get("ESID") == esid:
                logger.info("  Found collector data")
                return row

    logger.error("  No collector data found for ESID %s", esid)
    return None


def create_single_collector_csv(
    collector_data: Dict[str, str],
    output_dir: Path,
) -> Path:
    """Create a single-row metadata CSV for this ESID.

    Args:
        collector_data: Row data from the main collectors CSV.
        output_dir: Where to save the file.

    Returns:
        Path to created CSV.
    """
    output_file = output_dir / "total_eclipse_data.csv"
    logger.info("Creating total_eclipse_data.csv")

    with open(output_file, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=collector_data.keys())
        writer.writeheader()
        writer.writerow(collector_data)

    logger.info("  Created: %s", output_file.name)
    return output_file


# ===================================================================
#  File list generation  (two-pass: internal → ZIP finalized → external)
# ===================================================================

# Column order used by both file list versions
_FILE_LIST_HEADERS = [
    "File Name", "File Type", "Description", "File size (KB)",
    "File size (Bytes)",
    "Associated Data Dictionary", "SHA-512 Hash", "Notes",
]

# Default location of the resource files list, relative to Resources/.
# Users add new companion files by editing this CSV — no Python changes needed.
_RESOURCE_FILES_LIST_NAME = "resource_files_list.csv"


def load_resource_files_list(resources_dir: Path) -> List[Dict[str, str]]:
    """Read the CSV that controls which companion files are included in each dataset.

    ``Resources/resource_files_list.csv`` is the single source of truth for
    every file that is:

    - Copied from ``Resources/`` into the staging directory
    - Listed in ``file_list.csv`` with SHA-512 hash and size
    - Added to the ZIP archive under the ``ESID_XXX/`` subfolder
    - Uploaded as a standalone file to Zenodo

    The CSV has four columns::

        File Name,File Type,Description,Associated Data Dictionary

    Lines beginning with ``#`` are treated as comments and ignored, allowing
    the file to carry human-readable explanatory notes.

    **To add a new companion file:** place it in ``Resources/`` and add one
    row to this CSV.  No Python code changes are required.

    Args:
        resources_dir: Path to the ``Resources/`` directory containing
            ``resource_files_list.csv``.

    Returns:
        List of row dicts with keys ``File Name``, ``File Type``,
        ``Description``, and ``Associated Data Dictionary``.

    Raises:
        FileNotFoundError: If ``resource_files_list.csv`` is missing from
            ``resources_dir``.
        ValueError: If required columns are absent from the CSV.
    """
    csv_path = resources_dir / _RESOURCE_FILES_LIST_NAME

    if not csv_path.exists():
        raise FileNotFoundError(
            f"Resource files list not found: {csv_path}\n"
            f"Copy templates/resource_files_list.csv.example to "
            f"Resources/resource_files_list.csv and customize for your project."
        )

    required_columns = {"File Name", "File Type", "Description", "Associated Data Dictionary"}
    rows: List[Dict[str, str]] = []

    with open(csv_path, "r", encoding="utf-8") as fh:
        # Strip comment lines before handing to DictReader
        non_comment_lines = [
            line for line in fh if not line.lstrip().startswith("#")
        ]

    reader = csv.DictReader(non_comment_lines)
    found_columns = set(reader.fieldnames or [])
    missing_columns = required_columns - found_columns
    if missing_columns:
        raise ValueError(
            f"resource_files_list.csv is missing required columns: "
            f"{sorted(missing_columns)}\n"
            f"Expected: {sorted(required_columns)}"
        )

    for row in reader:
        filename = row.get("File Name", "").strip()
        if filename:  # skip blank rows
            rows.append({
                "File Name": filename,
                "File Type": row.get("File Type", "").strip(),
                "Description": row.get("Description", "").strip(),
                "Associated Data Dictionary": row.get(
                    "Associated Data Dictionary", "N/A"
                ).strip(),
            })

    logger.info(
        "  Loaded %d resource file entries from %s",
        len(rows),
        _RESOURCE_FILES_LIST_NAME,
    )
    return rows


def create_internal_file_list(
    output_dir: Path,
    esid: str,
    source_dir: Path,
    content_hashes: Dict[str, str],
    resource_specs: List[Dict[str, str]],
) -> Tuple[Path, List[Dict[str, str]]]:
    """Create the *internal* file_list.csv — for placement inside the ZIP.

    Documents every file that will live inside the ``ESID_XXX/`` subfolder
    of the archive: resource companion files (from the staging directory),
    auto-generated metadata files (README.md, total_eclipse_data.csv,
    file_list.csv itself), and raw data files (WAVs + CONFIG.TXT from the
    source directory).

    The ZIP itself is deliberately omitted — including it would be a circular
    reference.  Use :func:`create_external_file_list` after the ZIP is
    finalized to produce the version that adds the ZIP row.

    SHA-512 hashes for WAV files and CONFIG.TXT are taken from
    ``content_hashes`` (computed during :func:`create_zip_file`) rather than
    re-reading the raw files, avoiding a second pass over potentially
    gigabytes of audio data.

    Args:
        output_dir: Staging directory containing prepared metadata files.
        esid: ESID number string (e.g., ``'005'``).
        source_dir: Raw data directory (used for WAV/CONFIG.TXT file sizes).
        content_hashes: Dict mapping filename → SHA-512 hex digest, as
            returned by :func:`create_zip_file`.
        resource_specs: List of resource file row dicts as returned by
            :func:`load_resource_files_list`.  Drives the companion-file
            entries in the file list.

    Returns:
        Tuple of:

        - ``file_list_path``: Path to the written file_list.csv.
        - ``rows``: The list of row dicts written, so
          :func:`create_external_file_list` can reuse them without re-reading.
    """
    file_list_path = output_dir / "file_list.csv"
    logger.info("Creating internal file_list.csv (ZIP contents, no ZIP row)")

    rows: List[Dict[str, str]] = []

    # --- Auto-generated metadata files (always present, fixed descriptions) ---
    # These are produced by the pipeline itself, not copied from Resources/,
    # so they are not in resource_files_list.csv.
    auto_generated = [
        (
            "README.md",
            "Markdown (.md)",
            "Human and machine-readable documentation describing the dataset, "
            "collection methodology, site location, and data usage guidelines.",
            "N/A",
        ),
        (
            "total_eclipse_data.csv",
            "Comma Separated Variable (.CSV)",
            "Machine-readable metadata about this specific data collection site.",
            "2024_total_eclipse_data_data_dict.csv",
        ),
        (
            "file_list.csv",
            "Comma Separated Variable (.CSV)",
            "Inventory of all files in this record with file types, descriptions, "
            "sizes, and SHA-512 hashes for data integrity verification.",
            "file_list_data_dict.csv",
        ),
    ]
    for filename, file_type, description, data_dict in auto_generated:
        file_path = output_dir / filename
        if not file_path.exists():
            logger.debug("  Skipping missing auto-generated file: %s", filename)
            continue
        rows.append({
            "File Name": filename,
            "File Type": file_type,
            "Description": description,
            "File size (KB)": f"{file_path.stat().st_size / 1024:.2f}",
            "File size (Bytes)": str(file_path.stat().st_size),
            "Associated Data Dictionary": data_dict,
            "SHA-512 Hash": calculate_sha512(str(file_path)),
            "Notes": "",
        })
        logger.info("  Added %s", filename)

    # --- Resource companion files (driven by resource_files_list.csv) ---
    for spec in resource_specs:
        filename = spec["File Name"]
        file_path = output_dir / filename
        if not file_path.exists():
            logger.debug("  Skipping missing resource file: %s", filename)
            continue
        rows.append({
            "File Name": filename,
            "File Type": spec["File Type"],
            "Description": spec["Description"],
            "File size (KB)": f"{file_path.stat().st_size / 1024:.2f}",
            "File size (Bytes)": str(file_path.stat().st_size),
            "Associated Data Dictionary": spec["Associated Data Dictionary"],
            "SHA-512 Hash": calculate_sha512(str(file_path)),
            "Notes": "",
        })
        logger.info("  Added %s", filename)

    # --- CONFIG.TXT (lives in source_dir; hash already computed) ---
    config_file = source_dir / "CONFIG.TXT"
    if not config_file.exists():
        config_file = source_dir / "CONFIG.txt"
    if config_file.exists():
        config_hash = content_hashes.get(config_file.name, calculate_sha512(str(config_file)))
        rows.append({
            "File Name": config_file.name,
            "File Type": "Plain Text (.txt)",
            "Description": (
                "Recording device configuration file containing settings such as "
                "sample rate, gain level, firmware version, and recording schedule."
            ),
            "File size (KB)": f"{config_file.stat().st_size / 1024:.2f}",
            "File size (Bytes)": str(config_file.stat().st_size),
            "Associated Data Dictionary": "CONFIG_data_dict.csv",
            "SHA-512 Hash": config_hash,
            "Notes": "",
        })
        logger.info("  Added CONFIG.TXT (hash from write pass)")

    # --- WAV audio files (hash already computed during ZIP creation) ---
    wav_files = sorted(source_dir.glob("*.WAV")) + sorted(source_dir.glob("*.wav"))
    logger.info("  Adding %d WAV file entries...", len(wav_files))

    for wav_file in wav_files:
        # Fall back to on-demand hash only if missing from the write-pass dict
        wav_hash = content_hashes.get(wav_file.name, calculate_sha512(str(wav_file)))
        rows.append({
            "File Name": wav_file.name,
            "File Type": "Waveform Audio File Format (.WAV)",
            "Description": (
                "Audio recording file. Start time is encoded in the filename "
                "using YYYYMMDD_HHMMSS format (UTC)."
            ),
            "File size (KB)": f"{wav_file.stat().st_size / 1024:.2f}",
            "File size (Bytes)": str(wav_file.stat().st_size),
            "Associated Data Dictionary": "WAV_data_dict.csv",
            "SHA-512 Hash": wav_hash,
            "Notes": "",
        })

    logger.info("  Added %d WAV file entries", len(wav_files))

    # --- Write CSV ---
    with open(file_list_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_FILE_LIST_HEADERS)
        writer.writeheader()
        writer.writerows(rows)

    logger.info("  Created: %s (%d entries)", file_list_path.name, len(rows))
    return file_list_path, rows


def add_files_to_zip(zip_path: Path, output_dir: Path, esid: str) -> int:
    """Append staging-area metadata files into an existing ZIP archive.

    Opens the ZIP in append mode and adds every file from ``output_dir``
    that is destined for Zenodo, placing each under the ``ESID_XXX/``
    subfolder so the archive remains self-contained on extraction.

    The following files are intentionally excluded:

    - The ZIP itself (``ESID_XXX.zip``) — recursive inclusion.
    - ``README.html`` — its content becomes the Zenodo description field;
      it is never uploaded or archived.
    - ``ESID_XXX_to_upload.csv`` — internal upload manifest; not for public
      consumption.
    - Hidden files (names starting with ``'.'``).

    Args:
        zip_path: Path to the existing ZIP file to append to.
        output_dir: Staging directory containing the metadata files to add.
        esid: ESID number string (e.g., ``'005'``).

    Returns:
        Number of files appended to the archive.
    """
    zip_subfolder = f"ESID_{esid}"
    manifest_name = f"ESID_{esid}_to_upload.csv"

    # Files that must never enter the archive
    skip_names = {
        zip_path.name,      # the ZIP itself — recursive
        "README.html",      # → Zenodo description field, not a file
        manifest_name,      # internal upload manifest
    }

    logger.info("Appending metadata files to ZIP: %s", zip_path.name)

    appended = 0
    # strict_timestamps=False for consistency with create_zip_file() —
    # the metadata files appended here are freshly generated so a
    # pre-1980 mtime should be impossible, but both ZIP writers should
    # behave identically if one ever slips through.
    with zipfile.ZipFile(
        zip_path, "a", zipfile.ZIP_DEFLATED, strict_timestamps=False
    ) as zipf:
        for file_path in sorted(output_dir.iterdir()):
            if not file_path.is_file():
                continue
            if file_path.name.startswith("."):
                continue  # skip hidden files (e.g., .DS_Store)
            if file_path.name in skip_names:
                logger.debug("  Skipping (excluded): %s", file_path.name)
                continue

            arcname = f"{zip_subfolder}/{file_path.name}"
            zipf.write(file_path, arcname)
            logger.info("  Appended: %s → %s", file_path.name, arcname)
            appended += 1

    logger.info("  Appended %d metadata file(s) to ZIP", appended)
    return appended


def create_external_file_list(
    output_dir: Path,
    esid: str,
    internal_rows: List[Dict[str, str]],
) -> Path:
    """Create the *external* file_list.csv — the version uploaded to Zenodo.

    Prepends a row documenting the ZIP archive (with its final SHA-512 hash
    and size, computed after all metadata files have been appended) to the
    rows already produced by :func:`create_internal_file_list`.

    This overwrites the ``file_list.csv`` in ``output_dir`` so that the
    standalone Zenodo download presents a complete manifest including the
    archive itself.

    Args:
        output_dir: Staging directory containing the finalized ZIP file.
        esid: ESID number string (e.g., ``'005'``).
        internal_rows: Row dicts from :func:`create_internal_file_list`,
            reused as-is (no re-hashing of WAV/metadata files required).

    Returns:
        Path to the overwritten file_list.csv.

    Raises:
        FileNotFoundError: If the finalized ZIP is absent from
            ``output_dir`` when the ZIP row is being built.
    """
    file_list_path = output_dir / "file_list.csv"
    zip_path = output_dir / f"ESID_{esid}.zip"

    logger.info("Creating external file_list.csv (adds ZIP row — ZIP is now final)")

    if not zip_path.exists():
        raise FileNotFoundError(
            f"ZIP file not found when building external file list: {zip_path}"
        )

    # --- Build ZIP row (hash + size of the finalized archive) ---
    logger.info("  Hashing finalized ZIP: %s", zip_path.name)
    zip_hash = calculate_sha512(str(zip_path))
    zip_size_kb = zip_path.stat().st_size / 1024

    zip_row: Dict[str, str] = {
        "File Name": zip_path.name,
        "File Type": "ZIP Archive (.zip)",
        "Description": (
            "Compressed archive containing all audio recordings, CONFIG.TXT, "
            "and all companion metadata files for this data collection site."
        ),
        "File size (KB)": f"{zip_size_kb:.2f}",
        "File size (Bytes)": str(zip_path.stat().st_size),
        "Associated Data Dictionary": "N/A",
        "SHA-512 Hash": zip_hash,
        "Notes": (
            f"Extract to ESID_{esid}/ subfolder — "
            "contains audio files and all metadata"
        ),
    }

    # ZIP row goes first; internal rows follow in their original order
    all_rows = [zip_row] + internal_rows

    with open(file_list_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_FILE_LIST_HEADERS)
        writer.writeheader()
        writer.writerows(all_rows)

    logger.info(
        "  Updated: %s (%d entries, ZIP row prepended)",
        file_list_path.name,
        len(all_rows),
    )
    return file_list_path


# ===================================================================
#  Pre-sentinel ZIP verification
# ===================================================================

def verify_zip_against_source(zip_path: Path, source_dir: Path) -> List[str]:
    """Verify the finished ZIP holds every WAV the raw folder holds.

    Runs after the ZIP is final (all metadata appended) and BEFORE the
    move into Staging_Area/ and the ``.prep_complete`` sentinel — a ZIP
    that fails here never becomes an uploadable staging folder.

    The disk side is a FRESH scan of the raw folder, deliberately not
    the file list captured at zip time: files that appeared (or turned
    into placeholders) during the prep make the two sides diverge, which
    is exactly the failure this must catch.  Both scans come from
    ``audit_wav_integrity`` and cross-check every size two independent
    ways (disk stat vs RIFF header; ZIP size field vs CRC).

    Genuinely zero-byte source WAVs (a dead recorder wrote no audio;
    the cross-check corroborates the empty stat) are allowed and only
    warned about — the size-map comparison still proves each exists in
    the ZIP as exactly 0 bytes.  Placeholder-style files whose stat
    size disagrees with their readable bytes remain fatal.

    No full-CRC ``testzip()`` pass — that would decompress gigabytes,
    and the index + per-file-size comparison already catches the
    short-ZIP failure class this defends against.

    Args:
        zip_path: The finalized ZIP archive.
        source_dir: The raw ESID folder the WAVs came from.

    Returns:
        List of human-readable problem strings.  Empty list = verified.
    """
    problems: List[str] = []
    tiny = audit_wav_integrity._DEFAULT_TINY_THRESHOLD

    zip_stats, zip_err = audit_wav_integrity.scan_zip_wavs(zip_path, tiny)
    if zip_err is not None:
        return [f"ZIP is not a readable archive: {zip_err}"]

    disk_stats = audit_wav_integrity.scan_disk_wavs(source_dir, tiny)
    if disk_stats.count == 0:
        problems.append(f"No WAV files found in source folder {source_dir}")

    for name, reason in disk_stats.discrepancies:
        problems.append(f"Source WAV failed its size cross-check — {name}: {reason}")
    # Genuinely empty WAVs (0 bytes on disk AND corroborated by the
    # cross-check — a dead/failed AudioMoth writes these) are part of
    # the dataset and belong in the archive.  They are warned about,
    # not failed: the size-map comparison below still proves each one
    # exists in the ZIP as exactly 0 bytes.  Placeholder-style files
    # (stat says 0 but bytes are readable) remain fatal — they land in
    # ``discrepancies`` above, never in ``zero_names``.
    if disk_stats.zero_names:
        logger.warning(
            "%d source WAV(s) are genuinely zero bytes (recorder wrote "
            "no audio) — included in the ZIP as empty files: %s",
            len(disk_stats.zero_names),
            ", ".join(disk_stats.zero_names[:5]) + (
                " ..." if len(disk_stats.zero_names) > 5 else ""
            ),
        )
    for name, reason in zip_stats.discrepancies:
        problems.append(f"ZIP entry failed its size cross-check — {name}: {reason}")

    is_match, notes = audit_wav_integrity.compare_file_maps(
        disk_stats.sizes, zip_stats.sizes
    )
    if not is_match:
        problems.extend(f"Disk vs ZIP mismatch: {note}" for note in notes)

    if not problems:
        logger.info(
            "ZIP verified against source: %d WAV(s), %d bytes, all "
            "per-file sizes match.",
            zip_stats.count, zip_stats.total_bytes,
        )
    return problems


# ===================================================================
#  README generation from template
# ===================================================================

# Map the "WAV ... Time & Date Settings" CSV value to its README display wording.
# Keys are normalized (stripped + lowercased); unknown values pass through unchanged.
_TIME_DATE_MODE_DISPLAY: Dict[str, str] = {
    "set manually": "Needs to be set manually",
    "needs to be set manually": "Needs to be set manually",
    "set with automated audiomoth time chime": "Set with Automated AudioMoth Time Chime",
}


def create_readme_html(
    collector_data: Dict[str, str],
    output_dir: Path,
    template_path: Optional[Path] = None,
) -> Path:
    """Create README.html from an external HTML template.

    Uses Python's ``string.Template`` to substitute ``$variable`` placeholders
    in the template with values from the collector data.

    The template is read from ``template_path`` (default:
    ``Resources/README_template.html`` relative to this script).

    Args:
        collector_data: Collector metadata row.
        output_dir: Where to save the generated README.html.
        template_path: Path to the HTML template file.

    Returns:
        Path to created README.html.

    Raises:
        FileNotFoundError: If the template file does not exist.
    """
    output_file = output_dir / "README.html"
    logger.info("Creating README.html from template")

    # --- Locate template ---
    if template_path is None:
        template_path = Path(__file__).parent / "Resources" / "README_template.html"

    if not template_path.exists():
        raise FileNotFoundError(
            f"README template not found: {template_path}\n"
            f"Copy templates/README_template.html.example to "
            f"Resources/README_template.html and customize for your project."
        )

    template_content = template_path.read_text(encoding="utf-8")
    logger.info("  Template: %s", template_path.name)

    # --- Build substitution variables from collector data ---
    esid = collector_data.get("ESID", "Unknown")
    eclipse_date = collector_data.get("Eclipse Date", "Unknown")

    # Parse date for formatted display
    try:
        eclipse_dt = datetime.strptime(eclipse_date, "%Y-%m-%d")
        formatted_date = eclipse_dt.strftime("%B %d, %Y")
        year = str(eclipse_dt.year)
    except (ValueError, TypeError):
        formatted_date = eclipse_date
        year = "Unknown"

    # Determine eclipse label
    eclipse_type = collector_data.get("Local Eclipse Type", "")
    eclipse_labels = {
        "Total": "Total Solar Eclipse",
        "Annular": "Annular Solar Eclipse",
        "Partial": "Partial Solar Eclipse",
    }
    eclipse_label = eclipse_labels.get(eclipse_type, f"{eclipse_type} Solar Eclipse")

    # Resolve the WAV time/date setting, tolerant of header spelling:
    # 2024 sheets use "WAV Files Time & Date Settings"; 2023/collectors sheets
    # use "WAV files Time & Date Settings:" (lowercase "files", trailing colon).
    raw_time_date_mode = ""
    for key, value in collector_data.items():
        if key and key.strip().rstrip(":").strip().lower() == "wav files time & date settings":
            raw_time_date_mode = (value or "").strip()
            break
    time_date_mode = _TIME_DATE_MODE_DISPLAY.get(
        raw_time_date_mode.lower(), raw_time_date_mode
    )

    substitution_vars = {
        "esid": esid,
        "date": formatted_date,
        "year": year,
        "eclipse_label": eclipse_label,
        "latitude": collector_data.get("Latitude", "Unknown"),
        "longitude": collector_data.get("Longitude", "Unknown"),
        "coverage": collector_data.get("Eclipse Percent (%)", "Unknown"),
        "time_date_mode": time_date_mode,
        "start_time_notes": collector_data.get("Data Collector Start Time Notes", ""),
        "first_contact": collector_data.get(
            "Eclipse Start Time (UTC) (1st Contact)", "N/A"),
        "second_contact": collector_data.get(
            "Totality Start Time (UTC) (2nd Contact)", "N/A"),
        "maximum_time": collector_data.get("Eclipse Maximum (UTC)", "N/A"),
        "third_contact": collector_data.get(
            "Totality End Time (UTC) (3rd Contact)", "N/A"),
        "fourth_contact": collector_data.get(
            "Eclipse End Time (UTC) (4th Contact)", "N/A"),
    }

    # --- Perform template substitution ---
    # safe_substitute leaves unmatched $variables as-is instead of raising
    template = Template(template_content)
    html_content = template.safe_substitute(substitution_vars)

    with open(output_file, "w", encoding="utf-8") as fh:
        fh.write(html_content)

    logger.info("  Created: %s", output_file.name)
    return output_file


def create_readme_md(readme_html: Path, output_dir: Path) -> Path:
    """Create README.md from README.html.

    Uses ``html2text`` if available, otherwise falls back to basic tag stripping.

    Args:
        readme_html: Path to the generated README.html.
        output_dir: Where to save README.md.

    Returns:
        Path to created README.md.
    """
    output_file = output_dir / "README.md"
    logger.info("Creating README.md")

    html_content = readme_html.read_text(encoding="utf-8")

    try:
        import html2text

        converter = html2text.HTML2Text()
        converter.body_width = 0  # Don't wrap lines
        markdown = converter.handle(html_content)
        logger.info("  Converted using html2text")

    except ImportError:
        logger.info("  html2text not installed — using basic tag stripping")
        markdown = re.sub(r"<[^<]+?>", "", html_content)

    with open(output_file, "w", encoding="utf-8") as fh:
        fh.write(markdown)

    logger.info("  Created: %s", output_file.name)
    return output_file


# ===================================================================
#  Resource file copying
# ===================================================================

def copy_resource_files(
    resources_dir: Path,
    output_dir: Path,
    resource_specs: List[Dict[str, str]],
) -> None:
    """Copy companion files from ``Resources/`` into the dataset staging directory.

    The list of files to copy is driven entirely by ``resource_specs``, which
    is loaded from ``Resources/resource_files_list.csv`` by
    :func:`load_resource_files_list`.  Adding a new companion file requires
    only placing it in ``Resources/`` and adding one row to that CSV —
    no Python code changes are needed.

    Args:
        resources_dir: Source directory (``Resources/``) containing the files.
        output_dir: Destination staging directory for this ESID.
        resource_specs: List of resource file row dicts as returned by
            :func:`load_resource_files_list`.
    """
    logger.info("Copying resource files")

    copied = 0
    for spec in resource_specs:
        filename = spec["File Name"]
        src = resources_dir / filename
        dst = output_dir / filename

        if src.exists():
            shutil.copy2(src, dst)
            logger.info("  Copied: %s", filename)
            copied += 1
        else:
            logger.warning("  Missing resource file: %s", src)

    logger.info("  Copied %d/%d resource files", copied, len(resource_specs))


# ===================================================================
#  Related identifiers selection
# ===================================================================

# Keyword substring that signals an ES Data Analysis Site record.
# Case-insensitive match is applied at runtime.
_ANALYSIS_SITE_KEYWORD = "ES Data Analysis Site"


def copy_related_identifiers(
    collector_data: Dict[str, str],
    resources_dir: Path,
    output_dir: Path,
) -> Path:
    """Select and copy the correct related_identifiers.csv for this dataset.

    Reads the ``Keywords and subjects`` field from the collector data row and
    uses it to decide which file from ``resources_dir`` to copy into
    ``output_dir`` as ``related_identifiers.csv``.

    **Selection priority (first match wins):**

    1. ``output_dir/related_identifiers.csv`` already exists →
       leave it untouched (user placed it manually).
    2. Keywords contain ``"ES Data Analysis Site"`` (case-insensitive) →
       copy ``resources_dir/related_identifiers2.csv``.
    3. No keyword match →
       copy ``resources_dir/related_identifiers1.csv``.
    4. Numbered source file missing from Resources (graceful fallback) →
       copy ``resources_dir/related_identifiers.csv`` (the project default).
    5. No related_identifiers file found anywhere →
       log a warning and skip; ``standalone_tasks.py`` will use its own
       global fallback at upload time.

    Args:
        collector_data: Row dict from the collectors CSV for this ESID,
            as returned by :func:`extract_collector_data`.
        resources_dir: Directory containing the candidate CSV files
            (``related_identifiers.csv``, ``related_identifiers1.csv``,
            ``related_identifiers2.csv``).
        output_dir: Staging directory for this ESID.  The chosen file is
            copied here as ``related_identifiers.csv``.

    Returns:
        Path to the ``related_identifiers.csv`` in ``output_dir``
        (whether newly copied or pre-existing).
    """
    destination = output_dir / "related_identifiers.csv"

    # --- Priority 1: already present (manual override) ---
    if destination.exists():
        logger.info(
            "  related_identifiers.csv already present — keeping existing file"
        )
        return destination

    # --- Determine which source file to use based on keywords ---
    keywords = collector_data.get("Keywords and subjects", "") or ""
    is_analysis_site = _ANALYSIS_SITE_KEYWORD.lower() in keywords.lower()

    if is_analysis_site:
        logger.info(
            "  Keywords contain '%s' → selecting related_identifiers2.csv",
            _ANALYSIS_SITE_KEYWORD,
        )
        preferred_source = resources_dir / "related_identifiers2.csv"
    else:
        logger.info(
            "  No analysis-site keyword found → selecting related_identifiers1.csv"
        )
        preferred_source = resources_dir / "related_identifiers1.csv"

    # --- Priority 2/3: copy the keyword-selected file ---
    if preferred_source.exists():
        shutil.copy2(preferred_source, destination)
        logger.info("  Copied: %s → related_identifiers.csv", preferred_source.name)
        return destination

    # --- Priority 4: graceful fallback to project default ---
    default_source = resources_dir / "related_identifiers.csv"
    if default_source.exists():
        logger.warning(
            "  %s not found — falling back to related_identifiers.csv",
            preferred_source.name,
        )
        shutil.copy2(default_source, destination)
        logger.info("  Copied: related_identifiers.csv (default fallback)")
        return destination

    # --- Priority 5: nothing found — skip gracefully ---
    logger.warning(
        "  No related_identifiers CSV found in %s — skipping. "
        "standalone_tasks.py will use its global fallback at upload time.",
        resources_dir,
    )
    return destination


# ===================================================================
#  Upload manifest
# ===================================================================

# Files that exist in the staging directory but should NOT be uploaded
# to Zenodo. README.html content becomes the Zenodo description field;
# the 4readme CSV is an intermediate formatting artefact; the manifest
# file itself would be circular.
_MANIFEST_EXCLUDES = {
    "README.html",
    "total_eclipse_data_4readme.csv",
}


def create_upload_manifest(output_dir: Path, esid: str) -> Path:
    """Create ESID_XXX_to_upload.csv listing every file to send to Zenodo.

    Scans ``output_dir`` for all files present and writes a manifest CSV
    that ``standalone_tasks.py`` reads at upload time.  Files in
    ``_MANIFEST_EXCLUDES`` are omitted.

    The manifest CSV uses a ``File Name`` column (the only column consumed
    by ``read_upload_manifest``), plus human-readable ``File Size (KB)``
    and ``Notes`` columns.

    Args:
        output_dir: Staging directory containing prepared dataset files.
        esid: ESID number string (e.g., ``'005'``).

    Returns:
        Path to the created manifest CSV.
    """
    manifest_name = f"ESID_{esid}_to_upload.csv"
    manifest_path = output_dir / manifest_name

    # Exclude the manifest itself plus any internal-only files
    excludes = _MANIFEST_EXCLUDES | {manifest_name}

    rows: List[Dict[str, str]] = []

    for file_path in sorted(output_dir.iterdir()):
        if not file_path.is_file():
            continue
        if file_path.name.startswith("."):
            continue  # skip hidden files (e.g., .DS_Store)
        if file_path.name in excludes:
            continue

        size_kb = file_path.stat().st_size / 1024

        # Brief human note per file type — purely informational
        ext = file_path.suffix.lower()
        if ext == ".zip":
            notes = "Main data archive — WAV recordings + CONFIG.TXT"
        elif file_path.name == "README.md":
            notes = "Human-readable dataset documentation"
        elif file_path.name == "file_list.csv":
            notes = "File manifest with SHA-512 hashes"
        elif file_path.name == "License.txt":
            notes = "CC BY 4.0 license text"
        elif file_path.name.endswith("_data_dict.csv"):
            notes = "Data dictionary"
        elif ext == ".pdf":
            notes = "Device operation manual"
        elif ext == ".csv":
            notes = "Dataset metadata"
        else:
            notes = ""

        rows.append({
            "File Name": file_path.name,
            "File Size (KB)": f"{size_kb:.2f}",
            "Notes": notes,
        })

    with open(manifest_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["File Name", "File Size (KB)", "Notes"]
        )
        writer.writeheader()
        writer.writerows(rows)

    logger.info(
        "  Created upload manifest: %s (%d files)", manifest_name, len(rows)
    )
    return manifest_path

# ===================================================================
#  CLI entry point
# ===================================================================

# Upload-pipeline artifacts that must SURVIVE a re-prep.  They are the
# only link between a staging folder and its Zenodo draft:
#   * upload_state.json           — the draft's record_id (resume marker)
#   * ESID_XXX_request_log.json   — draft-creation log; also holds the id
# Destroying them (as the re-prep rmtree used to) orphans the existing
# draft on Zenodo, and the next upload run creates a DUPLICATE record.
_UPLOAD_ARTIFACT_PATTERNS = ("upload_state.json", "ESID_*_request_log.json")


def _stash_upload_artifacts(folder: Path, stash_dir: Path) -> List[str]:
    """Copy the upload-pipeline artifacts to an ON-DISK stash before the
    folder is deleted.

    On disk — not in memory — so a crash between the ``rmtree`` and the
    restore cannot orphan the Zenodo draft: the stash directory survives
    the crash and the next re-prep run restores it.

    Args:
        folder: The existing staging folder about to be deleted.
        stash_dir: Hidden directory inside Staging_Area/ to copy into
            (created on demand).

    Returns:
        Names of the artifacts stashed (empty when none exist).
    """
    stashed: List[str] = []
    if not folder.is_dir():
        return stashed
    for pattern in _UPLOAD_ARTIFACT_PATTERNS:
        for artifact in sorted(folder.glob(pattern)):
            try:
                stash_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(artifact, stash_dir / artifact.name)
                stashed.append(artifact.name)
            except OSError as exc:
                logger.warning(
                    "Could not preserve %s across re-prep: %s",
                    artifact.name, exc,
                )
    return stashed


def _restore_upload_artifacts(folder: Path, stash_dir: Path) -> None:
    """Copy stashed upload artifacts into the freshly prepared folder.

    Keeps the folder linked to its existing Zenodo draft so the next
    upload run RESUMES it instead of creating a duplicate record.  The
    stash directory is removed only after EVERY artifact restored
    cleanly — on any failure it stays on disk for the next run.

    Caveat (also documented in the guides): resuming a preserved draft
    does not re-send record metadata — if this re-prep changed the
    README/metadata, fix the record description in the Zenodo web UI
    after the upload completes.

    Args:
        folder: The freshly prepared staging folder.
        stash_dir: The on-disk stash written by
            :func:`_stash_upload_artifacts` (possibly from a prior
            interrupted run).  No-op when absent.
    """
    if not stash_dir.is_dir():
        return
    restored: List[str] = []
    all_ok = True
    for artifact in sorted(p for p in stash_dir.iterdir() if p.is_file()):
        try:
            shutil.copy2(artifact, folder / artifact.name)
            restored.append(artifact.name)
        except OSError as exc:
            all_ok = False
            logger.warning(
                "Could not restore %s after re-prep: %s — the stash is "
                "kept at %s for the next run.",
                artifact.name, exc, stash_dir,
            )
    record_id = "?"
    state_file = folder / "upload_state.json"
    if "upload_state.json" in restored:
        try:
            record_id = json.loads(
                state_file.read_text(encoding="utf-8")
            ).get("record_id", "?")
        except (OSError, ValueError, UnicodeDecodeError):
            record_id = "?"
        logger.info(
            "Preserved upload state across re-prep (record %s): %s",
            record_id, ", ".join(restored),
        )
    elif restored:
        logger.info(
            "Preserved upload artifacts across re-prep: %s",
            ", ".join(restored),
        )
    if all_ok:
        try:
            shutil.rmtree(stash_dir)
        except OSError as exc:
            logger.warning(
                "Could not remove artifact stash %s: %s (harmless — it "
                "will be reused/overwritten on the next re-prep).",
                stash_dir, exc,
            )


def main() -> None:
    """Command-line entry point for dataset preparation."""
    parser = argparse.ArgumentParser(
        description="Prepare an AZUS dataset from raw data files"
    )
    parser.add_argument(
        "folder", help="Folder containing raw data files (e.g., ESID#005)")
    parser.add_argument(
        "--config",
        help=(
            "Path to AZUS config.json (default: Resources/config.json). "
            "When provided, collectors_csv is read from the first dataset "
            "entry in uploads.datasets[].collectors_csv, eliminating the "
            "need to supply --collector-csv separately."
        ),
    )
    parser.add_argument(
        "--collector-csv",
        help=(
            "Path to collectors CSV file. "
            "If both --config and --collector-csv are given, "
            "--collector-csv takes precedence."
        ),
    )
    parser.add_argument(
        "--eclipse-type", choices=["total", "annular", "partial"],
        default="total", help="Eclipse/dataset type (default: total)")
    parser.add_argument(
        "--resources-dir", default="Resources",
        help="Directory with resource files (default: Resources)")
    parser.add_argument(
        "--readme-template",
        help="Path to README HTML template (default: Resources/README_template.html)")
    parser.add_argument(
        "--output-dir",
        help="Output directory (default: ESID_XXX_Staging)")

    args = parser.parse_args()

    # --- Configure logging ---
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    # --- Resolve paths ---
    source_dir = Path(args.folder)
    resources_dir = Path(args.resources_dir)

    if not source_dir.exists():
        logger.error("Source folder not found: %s", source_dir)
        sys.exit(1)

    # --- Resolve collector CSV — explicit arg overrides config.json ---
    collector_csv_str: Optional[str] = args.collector_csv

    if not collector_csv_str and args.config:
        # Read collectors_csv from the first dataset entry in config.json
        config_path = Path(args.config)
        if not config_path.exists():
            logger.error("Config file not found: %s", config_path)
            sys.exit(1)
        with open(config_path, "r", encoding="utf-8") as cfg_fh:
            config_data = json.load(cfg_fh)
        datasets = config_data.get("uploads", {}).get("datasets", [])
        if not datasets:
            logger.error(
                "No datasets entries found in config.json uploads.datasets — "
                "cannot determine collectors_csv path."
            )
            sys.exit(1)
        collector_csv_str = datasets[0].get("collectors_csv", "")
        if not collector_csv_str:
            logger.error(
                "collectors_csv is empty in config.json datasets[0] — "
                "add the path or use --collector-csv."
            )
            sys.exit(1)
        logger.info("Using collectors_csv from config.json: %s", collector_csv_str)

    if not collector_csv_str:
        logger.error(
            "Collector CSV path not provided. "
            "Supply --collector-csv or --config pointing to a config.json "
            "with uploads.datasets[0].collectors_csv set."
        )
        sys.exit(1)

    collector_csv = Path(collector_csv_str)
    if not collector_csv.exists():
        logger.error("Collector CSV not found: %s", collector_csv)
        sys.exit(1)

    esid = get_esid_from_folder(source_dir.name)
    if esid is None:
        logger.error(
            "Cannot extract an ESID from folder name %r — expected "
            "ESID_NNN / ESID#NNN (NNN = 000-999, optionally followed "
            "by a suffix like 120A or 122_Part_1_of_2) or a bare "
            "ESID with no prefix.", source_dir.name,
        )
        sys.exit(1)

    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        output_dir = source_dir.parent / f"ESID_{esid}_Staging"

    # Refuse to build inside Staging_Area/.  The staging folder must be
    # assembled OUTSIDE and arrive via the two-phase atomic move below —
    # building in place grows the ZIP non-atomically under the exact
    # folder name the uploader scans for, so a killed prep (or a
    # concurrent upload run) sees an incomplete ZIP as an uploadable
    # dataset.  This exact path put broken data on Zenodo.
    staging_area_dir = azus_common.STAGING_AREA
    try:
        output_dir.resolve().relative_to(staging_area_dir.resolve())
        output_dir_in_staging = True
    except ValueError:
        output_dir_in_staging = False
    if output_dir_in_staging:
        logger.error(
            "Output directory %s is inside Staging_Area/. Refusing to "
            "build in place — the folder would be visible to upload runs "
            "before it is complete. Use the default output location or "
            "an --output-dir outside Staging_Area/; the finished folder "
            "is moved into Staging_Area/ atomically at the end.",
            output_dir,
        )
        sys.exit(1)

    output_dir.mkdir(exist_ok=True, parents=True)

    readme_template_path = (
        Path(args.readme_template) if args.readme_template
        else resources_dir / "README_template.html"
    )

    # --- Banner ---
    logger.info("=" * 70)
    logger.info("AZUS DATASET PREPARATION")
    logger.info("=" * 70)
    logger.info("ESID:           %s", esid)
    logger.info("Source:         %s", source_dir)
    logger.info("Output:         %s", output_dir)
    logger.info("Collector CSV:  %s", collector_csv)
    if args.config and not args.collector_csv:
        logger.info("  (path read from config.json)")
    logger.info("Eclipse type:   %s", args.eclipse_type)
    logger.info("README template:%s", readme_template_path)

    # Step 1: Extract collector data
    collector_data = extract_collector_data(collector_csv, esid)
    if not collector_data:
        logger.error("Cannot proceed without collector data")
        logger.info("Make sure ESID %s exists in %s", esid, collector_csv)
        sys.exit(1)

    # Step 2: Create ZIP file (WAVs + CONFIG.TXT in ESID_XXX/ subfolder).
    # Returns content_hashes so WAV/CONFIG hashes are not re-computed later.
    zip_path, content_hashes = create_zip_file(source_dir, output_dir, esid)

    # Step 3: Create single-row collector CSV
    create_single_collector_csv(collector_data, output_dir)

    # Step 4: Load resource file list from CSV, then copy files to staging dir.
    # resource_files_list.csv is the single source of truth for companion files.
    # To add a new file: place it in Resources/ and add a row to that CSV.
    resource_specs: List[Dict[str, str]] = []
    if resources_dir.exists():
        try:
            resource_specs = load_resource_files_list(resources_dir)
        except (FileNotFoundError, ValueError) as exc:
            logger.error("Cannot load resource files list: %s", exc)
            sys.exit(1)
        copy_resource_files(resources_dir, output_dir, resource_specs)
    else:
        logger.warning("Resources directory not found: %s", resources_dir)

    # Step 4b: Select and copy the correct related_identifiers.csv based on
    # the Keywords and subjects field for this site.  Must run after
    # copy_resource_files (resources_dir validated above) and before
    # create_internal_file_list so the file appears in the file list and ZIP.
    if resources_dir.exists():
        logger.info("Selecting related_identifiers.csv")
        copy_related_identifiers(collector_data, resources_dir, output_dir)

    # Step 5: Create README.html from template
    readme_html = create_readme_html(
        collector_data, output_dir, template_path=readme_template_path
    )

    # Step 6: Create README.md from README.html
    create_readme_md(readme_html, output_dir)

    # Step 7: Create INTERNAL file_list.csv — documents everything going into
    # the ZIP (resource files + auto-generated metadata + WAVs + CONFIG.TXT).
    # The ZIP row is omitted because the ZIP is not yet finalized.  Hashes for
    # WAVs and CONFIG.TXT come from the write-pass dict, so no second read of
    # raw audio data.
    _, internal_rows = create_internal_file_list(
        output_dir, esid, source_dir, content_hashes, resource_specs
    )

    # Step 8: Append all staging-area metadata files into the ZIP archive
    # under the ESID_XXX/ subfolder.  The ZIP is now complete and final.
    add_files_to_zip(zip_path, output_dir, esid)

    # Step 8b: Verify the finished ZIP against a FRESH scan of the raw
    # folder — every WAV present, every size matching, both sides
    # cross-checked (disk stat vs RIFF header, ZIP size vs CRC).  A
    # failure here exits nonzero BEFORE the move and the sentinel, so an
    # incomplete ZIP can never become an uploadable staging folder.
    zip_problems = verify_zip_against_source(zip_path, source_dir)
    if zip_problems:
        for problem in zip_problems:
            logger.error("ZIP VERIFICATION FAILED: %s", problem)
        logger.error(
            "The prepared ZIP does not match the raw data — nothing was "
            "moved to Staging_Area/ and no completion sentinel was "
            "written. Fix the raw folder (or re-run when it is fully "
            "synced) and re-run this preparation."
        )
        sys.exit(1)

    # Step 9: Create EXTERNAL file_list.csv — overwrites the staging-area
    # copy with a version that prepends the finalized ZIP row (final hash +
    # size).  This is the version uploaded standalone to Zenodo.
    create_external_file_list(output_dir, esid, internal_rows)

    # Step 10: Create upload manifest — lists every file destined for Zenodo.
    # standalone_tasks.py reads this at upload time so the file list has a
    # single, reviewable source of truth per dataset.
    create_upload_manifest(output_dir, esid)

    # --- Two-phase atomic move into Staging_Area/ (success only) ---
    # Staging_Area is resolved relative to the project root (the parent of this
    # script's Resources/ directory) so the move works regardless of the current
    # working directory.  Reaching this point means preparation succeeded — the
    # earlier steps sys.exit() on failure.
    #
    # Phase 1 (slow, possibly cross-filesystem, INTERRUPTIBLE):
    #   shutil.move into a hidden ".<name>.partial" path under Staging_Area/.
    #   If interrupted mid-copy, only this hidden name exists; the final name
    #   never appears in Staging_Area/.  Tools that scan Staging_Area for ESID
    #   subfolders ignore the leading dot, so the partial is invisible to them.
    # Phase 2 (fast, same filesystem, ATOMIC by POSIX guarantee):
    #   os.rename() the partial path to the final name.  This is a metadata-
    #   only operation within Staging_Area/ — it cannot be partial.
    # Stale cleanup:
    #   Any leftover .partial/ or pre-existing final/ from a prior interrupted
    #   run is removed first, so re-prep is fully idempotent.
    final_destination = staging_area_dir / output_dir.name
    partial_destination = staging_area_dir / f".{output_dir.name}.partial"
    if output_dir.resolve() == final_destination.resolve():
        # Unreachable: the in-place refusal at startup rejects any
        # output_dir inside Staging_Area/.  Kept as a hard stop in case
        # a future code path reintroduces one.
        logger.error(
            "Staging folder was built in place inside Staging_Area/ (%s) "
            "— this bypasses the atomic move and must never happen.",
            output_dir,
        )
        sys.exit(1)
    else:
        staging_area_dir.mkdir(parents=True, exist_ok=True)
        if partial_destination.exists():
            logger.warning(
                "Removing stale partial from a prior interrupted run: %s",
                partial_destination,
            )
            shutil.rmtree(partial_destination)
        # The artifact stash lives ON DISK so a crash between the rmtree
        # below and the restore after the move cannot orphan the Zenodo
        # draft: a stale stash from an interrupted run is simply restored
        # by this run.
        artifact_stash_dir = (
            staging_area_dir / f".{output_dir.name}.artifact_stash"
        )
        if artifact_stash_dir.is_dir():
            logger.warning(
                "Found upload-artifact stash from a prior interrupted "
                "re-prep: %s — restoring its contents into the new "
                "staging folder.", artifact_stash_dir,
            )
        if final_destination.exists():
            # Preserve the folder's link to any existing Zenodo draft
            # BEFORE destroying it — otherwise the next upload run cannot
            # resume that draft and creates a duplicate record.
            _stash_upload_artifacts(final_destination, artifact_stash_dir)
            logger.warning(
                "Replacing existing staging folder: %s", final_destination
            )
            shutil.rmtree(final_destination)
        logger.info("Phase 1: copying into partial path %s", partial_destination)
        shutil.move(str(output_dir), str(partial_destination))
        logger.info("Phase 2: atomic rename -> %s", final_destination)
        os.rename(str(partial_destination), str(final_destination))
        _restore_upload_artifacts(final_destination, artifact_stash_dir)
        output_dir = final_destination

    # --- Summary ---
    logger.info("=" * 70)
    logger.info("DATASET PREPARATION COMPLETE")
    logger.info("=" * 70)
    logger.info("Output directory: %s", output_dir)
    logger.info("Files created:")
    for file in sorted(output_dir.iterdir()):
        size_mb = file.stat().st_size / (1024 * 1024)
        logger.info("  %s  (%.2f MB)", file.name, size_mb)
    logger.info("Ready for upload to Zenodo!")
    logger.info("Next steps:")
    logger.info("  1. Verify files in: %s", output_dir)
    logger.info("  2. Update Resources/config.json")
    logger.info("  3. Run: python standalone_tasks.py")

    # --- VERY LAST ACTION: completion sentinel ---
    # ``.prep_complete`` is the marker prep_all_datasets.py looks for to decide
    # whether a Staging_Area folder is fully prepared.  Writing this LAST (after
    # the move and after the summary banner) means:
    #   * If the script is killed at any earlier point, the folder is present
    #     without the sentinel and the next batch run will correctly re-prep it.
    #   * Only a fully successful prepare_dataset.py run produces a folder that
    #     other tools will treat as done.
    sentinel = output_dir / azus_common.PREP_SENTINEL
    sentinel.touch()
    logger.info("Wrote completion sentinel: %s", sentinel)


if __name__ == "__main__":
    main()
