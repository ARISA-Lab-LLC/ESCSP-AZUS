#!/usr/bin/env python3
"""Audit completeness of prepared ESID staging and uploaded folders.

PURPOSE
=======
``prepare_dataset.py`` is the canonical producer of a "prepared" ESID
folder.  After it finishes, the folder contains a fixed set of files
(README, manifests, data dictionaries, the ZIP archive, etc.) and the
ZIP archive itself contains a fixed set of entries (the raw WAVs, the
CONFIG.TXT, the resource companion files, etc.).

This tool walks a top-level raw-data directory, finds every ESID
subdirectory, looks for the matching prepared folder under either
``<project_root>/Staging_Area/`` or ``<project_root>/Uploaded_Data/``,
and **verifies that every expected file is present** — both in the
folder itself and inside the ZIP archive (read via ``zipfile``).

Each ESID gets one row in a 4-column CSV report (``ESID#``,
``Staging Area``, ``Uploaded Data``, ``Prep Completed``).
"Prep Completed" is one of:

  * **Yes**       — every required file is present
  * **No**        — at least one required file is missing
  * **Ambiguous** — completeness cannot be determined
                    (corrupt ZIP, unreadable ``resource_files_list.csv``,
                    or a conditional file like ``related_identifiers.csv``
                    is missing — could be intentional, could be a real miss)

PRIMARY USE CASE — LEGACY MIGRATION
====================================
A separate change (``prepare_dataset.py``) now writes a hidden
``.prep_complete`` sentinel file as its very last action.  Older
prepared folders predate that sentinel and therefore lack it.

This tool's *primary* job is to vet those legacy folders by their
**contents** rather than by a marker they predate.  Whenever the deep
audit confirms a folder is unambiguously complete (status = "Yes"),
the tool back-fills the missing ``.prep_complete`` sentinel — bringing
the whole repository into the sentinel world without manual touching.

After running the audit once, subsequent runs of the AZUS batch tools
(notably ``prep_all_datasets.py``) can rely on the fast sentinel-based
skip check, because every confirmed-complete folder now carries one.

SECONDARY USE CASE — SILENT-CORRUPTION DETECTION
=================================================
Even sentinel-bearing folders can drift (a bug, a manual edit, a
partial filesystem write).  Run with ``--audit-all`` to force the
deep audit on every folder, ignoring the sentinel fast-path.

USAGE
=====
::

    python Resources/audit_prep_completeness.py <RAW_DATA_DIR>
        [--resources-dir Resources]
        [--output <path>]      # default: prep_completeness_report_YYYYMMDD_HHMMSS.csv in CWD
        [--audit-all]          # ignore sentinel; deep-audit every folder
        [--verbose]            # log per-file missing details at INFO level

The CSV is written to the directory where the script is invoked
(``Path.cwd()``), not to the project root.  The default filename
includes a timestamp so repeated runs do not clobber each other.

EXIT CODES
==========
* ``0`` — every ESID was either "Yes" or "Ambiguous"
* ``1`` — at least one ESID was "No" (missing files detected)
* ``2`` — usage error (missing raw-data or Resources directory)
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple


# ---------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------
# Named so messages in mixed AZUS logs are easy to grep for.
logger = logging.getLogger("azus.audit")


# ---------------------------------------------------------------------
# Project layout — same conventions as the other AZUS helper tools.
# ---------------------------------------------------------------------
import azus_common
# The audited truth-set is imported FROM THE PRODUCER: prepare_dataset.py
# owns the canonical list of files a completed prep contains, right next
# to the code that writes them.  A prep-output change now breaks loudly
# here instead of this tool silently certifying incomplete folders.
import prepare_dataset as _prep_contract

# Shared project layout (see azus_common.py).
_PROJECT_ROOT = azus_common.PROJECT_ROOT
_STAGING_AREA = azus_common.STAGING_AREA
_UPLOADED_DATA = azus_common.UPLOADED_DATA

# Files that should appear DIRECTLY in the staging/uploaded folder
# (``{esid}`` = 3-digit ESID number) — from the prep contract.
_HARDCODED_STAGING_FILES: Tuple[str, ...] = _prep_contract.STAGING_OUTPUT_FILES

# Prep-generated files that also appear INSIDE ESID_NNN.zip (compared
# by basename, after stripping the ESID_NNN/ prefix).
_HARDCODED_ZIP_ENTRIES: Tuple[str, ...] = _prep_contract.ZIP_METADATA_ENTRIES

# Files whose absence is Ambiguous (conditionally copied), not No.
_CONDITIONAL_FILES: Tuple[str, ...] = _prep_contract.CONDITIONAL_FILES

# The completion sentinel touched as the very last action of
# ``prepare_dataset.main()``.  Its presence in a folder enables this
# tool's fast-path "Yes" return.  Its absence does NOT make a folder
# incomplete — this tool's whole point is to vet pre-sentinel folders.
_PREP_SENTINEL = azus_common.PREP_SENTINEL

# Filename templates for the prepared-folder locations.
_STAGING_FOLDER_TEMPLATE = _prep_contract.STAGING_FOLDER_TEMPLATE
_UPLOADED_FOLDER_TEMPLATE = _prep_contract.UPLOADED_FOLDER_TEMPLATE

# Source of truth for which "companion" files prepare_dataset.py copies
# from Resources/ into each staging folder (and into the ZIP).
_RESOURCE_FILES_LIST_BASENAME = "resource_files_list.csv"
_RESOURCE_FILES_LIST_FILENAME_COLUMN = "File Name"


# =====================================================================
# Discovery
# =====================================================================

def find_raw_esid_folders(raw_root: Path) -> List[Tuple[int, str, Path]]:
    """Find every ESID_NNN subdirectory inside ``raw_root``, sorted numerically.

    Tolerant of every folder-naming variant the AZUS tools accept:
    ``ESID_073``, ``ESID_007``, ``ESID#73``, ``ESID_4`` (unpadded).
    The returned padded form is always 3-digit (``"004"``, ``"073"``).

    Non-ESID directories (``.DS_Store``, ``backup``, etc.) are silently
    ignored.

    Args:
        raw_root: Path to the parent folder containing raw ESID directories.

    Returns:
        List of ``(numeric_esid, padded_str, folder_path)`` tuples sorted
        in ascending numeric order.  Empty list if ``raw_root`` is not a
        directory or contains no matching subdirectories.
    """
    return azus_common.find_esid_folders(raw_root)


def find_in_staging(esid_padded: str) -> Optional[Path]:
    """Return the matching Staging_Area folder for this ESID, or ``None``.

    Args:
        esid_padded: 3-digit ESID number string (e.g. ``"073"``).

    Returns:
        The matching ``Staging_Area/`` folder, or ``None`` when no such
        directory exists.
    """
    candidate = _STAGING_AREA / _STAGING_FOLDER_TEMPLATE.format(esid=esid_padded)
    return candidate if candidate.is_dir() else None


def find_in_uploaded(esid_padded: str) -> Optional[Path]:
    """Return the matching Uploaded_Data folder for this ESID, or ``None``.

    Args:
        esid_padded: 3-digit ESID number string (e.g. ``"073"``).

    Returns:
        The matching ``Uploaded_Data/`` folder, or ``None`` when no such
        directory exists.
    """
    candidate = _UPLOADED_DATA / _UPLOADED_FOLDER_TEMPLATE.format(esid=esid_padded)
    return candidate if candidate.is_dir() else None


# =====================================================================
# Truth-set construction
# =====================================================================

def load_resource_companion_files(resources_dir: Path) -> Optional[List[str]]:
    """Read the ``File Name`` column of ``resource_files_list.csv``.

    These are the files that ``prepare_dataset.copy_resource_files()``
    copies from ``Resources/`` into every staging folder.  This tool
    needs that list to know which "companion" files to expect.

    Args:
        resources_dir: Path to the project's ``Resources/`` directory.

    Returns:
        A list of filename strings on success, or ``None`` if the CSV
        could not be read or parsed.  ``None`` is the caller's signal
        to mark affected ESIDs as Ambiguous (cannot determine the
        expected file set).
    """
    csv_path = resources_dir / _RESOURCE_FILES_LIST_BASENAME
    if not csv_path.is_file():
        logger.error("resource_files_list not found at %s", csv_path)
        return None

    try:
        with csv_path.open("r", newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            if _RESOURCE_FILES_LIST_FILENAME_COLUMN not in (reader.fieldnames or []):
                logger.error(
                    "resource_files_list at %s is missing the %r column "
                    "(found columns: %s)",
                    csv_path, _RESOURCE_FILES_LIST_FILENAME_COLUMN,
                    reader.fieldnames,
                )
                return None
            names: List[str] = []
            for row in reader:
                name = (row.get(_RESOURCE_FILES_LIST_FILENAME_COLUMN) or "").strip()
                # Skip blank rows and comment rows ``prepare_dataset.py``
                # itself ignores via its leading-``#`` filter.
                if not name or name.startswith("#"):
                    continue
                names.append(name)
            return names
    except Exception as exc:
        logger.error("Could not parse %s: %s", csv_path, exc)
        return None


def list_raw_wavs(raw_folder: Path) -> List[str]:
    """Return sorted basenames of every WAV file inside ``raw_folder``.

    Mirrors ``prepare_dataset.create_zip_file()``'s glob — both ``*.WAV``
    (uppercase) and ``*.wav`` (lowercase), sorted within each glob and
    concatenated.

    Args:
        raw_folder: The raw ESID folder to scan for WAV files.

    Returns:
        Sorted WAV basenames — uppercase matches first, then lowercase.
        Empty list when ``raw_folder`` is not a directory.
    """
    if not raw_folder.is_dir():
        return []
    wavs = sorted(p.name for p in raw_folder.glob("*.WAV"))
    wavs += sorted(p.name for p in raw_folder.glob("*.wav"))
    return wavs


def raw_has_config_txt(raw_folder: Path) -> bool:
    """Return True if either ``CONFIG.TXT`` or ``CONFIG.txt`` is in the raw folder.

    ``prepare_dataset.py`` accepts either case; this tool mirrors that
    tolerance so a site that used lowercase isn't flagged as missing.

    Args:
        raw_folder: The raw ESID folder to check for a CONFIG file.

    Returns:
        True if either ``CONFIG.TXT`` or ``CONFIG.txt`` is present.
    """
    return (raw_folder / "CONFIG.TXT").is_file() or (raw_folder / "CONFIG.txt").is_file()


def expected_folder_basenames(
    esid_padded: str,
    companions: Iterable[str],
    day_zip_names: Optional[Iterable[str]] = None,
) -> Set[str]:
    """Compute the required-file set for a staging or uploaded folder.

    Combines the hardcoded ``prepare_dataset.py`` outputs (ZIP, README,
    manifests, etc.) with the resource companions read from
    ``resource_files_list.csv``, plus the conditional files (whose
    absence is treated as Ambiguous, not No — see ``audit_one_esid``).

    NOTE: ``.prep_complete`` is intentionally NOT in this set — see the
    module docstring's "primary use case" section for why.

    Args:
        esid_padded: 3-digit ESID number string (e.g. ``"073"``).
        companions: Companion-file basenames from
            ``resource_files_list.csv``.
        day_zip_names: For a PER-DAY folder, the expected day-ZIP names
            (from ``prepare_dataset.expected_day_zip_names``); they
            replace the legacy single ``ESID_NNN.zip`` in the set.
            None (the default) means the legacy single-zip layout.

    Returns:
        The set of file basenames expected directly in the staging or
        uploaded folder (prep outputs + companions + conditionals).
    """
    if day_zip_names is None:
        expected = {
            template.format(esid=esid_padded)
            for template in _HARDCODED_STAGING_FILES
        }
    else:
        expected = {
            template.format(esid=esid_padded)
            for template in _prep_contract.STAGING_OUTPUT_FILES_COMMON
        }
        expected.update(day_zip_names)
    expected.update(companions)
    # Conditional files belong in the expected set so their absence is
    # detected; ``audit_one_esid`` then routes that detection to the
    # Ambiguous bucket rather than the No bucket.
    expected.update(_CONDITIONAL_FILES)
    return expected


def expected_zip_basenames(
    esid_padded: str, raw_folder: Path, companions: Iterable[str]
) -> Set[str]:
    """Compute the required ZIP-entry basenames for an ESID.

    Combines:
      * the WAV files actually present in the raw folder
      * the hardcoded staging-side outputs that are ALSO copied into the
        ZIP (README.md, file_list.csv, total_eclipse_data.csv)
      * the resource companions from ``resource_files_list.csv``
      * the conditional files (Ambiguous-bucket, not No)

    ``CONFIG.TXT`` is added only if it actually exists in the raw folder
    (it's optional per prepare_dataset.py's design).

    Args:
        esid_padded: 3-digit ESID number string (e.g. ``"073"``).
        raw_folder: The raw ESID folder, used to determine the WAV set
            and whether ``CONFIG.TXT`` should be expected in the ZIP.
        companions: Companion-file basenames from
            ``resource_files_list.csv``.

    Returns:
        The set of basenames expected inside ``ESID_NNN.zip`` (WAVs +
        hardcoded ZIP entries + companions + conditionals, plus
        ``CONFIG.TXT`` when present in the raw folder).
    """
    expected: Set[str] = set(_HARDCODED_ZIP_ENTRIES)
    expected.update(companions)
    # Same reasoning as expected_folder_basenames — see above.
    expected.update(_CONDITIONAL_FILES)
    expected.update(list_raw_wavs(raw_folder))
    if raw_has_config_txt(raw_folder):
        # Normalize to the canonical uppercase form for set comparison.
        # We don't care which case the ZIP uses internally — list_zip_contents
        # also normalizes via Path(name).name, and case differences on
        # macOS/Linux are typically preserved on copy.
        expected.add("CONFIG.TXT")
    return expected


# =====================================================================
# ZIP introspection
# =====================================================================

def list_zip_contents(zip_path: Path) -> Optional[Set[str]]:
    """Return the set of file basenames inside ``zip_path``.

    Reads the ZIP central directory with Python's ``zipfile`` — the same
    reader every other AZUS tool (and the prep verification itself)
    trusts against these exact archives.  This replaced an earlier
    ``unzip -l`` subprocess whose text-output parsing was fragile and
    whose external-binary dependency was the only one in the suite;
    ``zipfile`` handles ZIP64 large-file archives natively.

    Directory entries (names ending in ``/``) are stripped — only file
    entries are returned.  Each entry is reduced to its basename so
    comparisons can be done against ``expected_zip_basenames()`` without
    worrying about the ``ESID_NNN/`` prefix every entry carries.

    Args:
        zip_path: Path to the ZIP file to introspect.

    Returns:
        A set of basenames on success, or ``None`` if the ZIP is missing
        or unreadable (typically a corrupt archive).  ``None`` is the
        caller's signal to mark the ESID as Ambiguous.
    """
    if not zip_path.is_file():
        logger.debug("ZIP file not present: %s", zip_path)
        return None

    try:
        with zipfile.ZipFile(zip_path) as zf:
            return {
                name.rsplit("/", 1)[-1]
                for name in zf.namelist()
                if not name.endswith("/")
            }
    except (zipfile.BadZipFile, OSError) as exc:
        logger.warning(
            "Could not read ZIP %s (%s: %s)",
            zip_path, type(exc).__name__, exc,
        )
        return None


# =====================================================================
# Per-ESID audit
# =====================================================================

def audit_day_zips(
    esid_padded: str,
    raw_folder: Path,
    target_folder: Path,
    companions: List[str],
) -> Tuple[str, List[str]]:
    """Audit a PER-DAY staging or uploaded folder for completeness.

    The expectation comes from the prep contract's own grouping rule
    (``prepare_dataset.expected_day_zip_names``, which calls the same
    ``azus_common.wav_day_key`` the prep used), so a folder can never be
    grouped one way and audited another.  Each expected day ZIP must be
    present and readable, and its contents must be EXACTLY that day's
    raw WAVs plus CONFIG.TXT (when the raw folder has one) — exact
    per-day equality is what certifies that every raw WAV is covered
    exactly once across the archives, that none leaked into a foreign
    day's ZIP, and that no metadata entries snuck in (per-day ZIPs
    carry none by design).

    Args:
        esid_padded: Canonical ESID string (e.g. ``"073"``).
        raw_folder: The original raw ESID folder.
        target_folder: The Staging_Area or Uploaded_Data folder being
            audited.
        companions: Companion-file basenames from
            ``resource_files_list.csv``.

    Returns:
        ``(status, details)`` with the same semantics as
        :func:`audit_one_esid`.
    """
    missing: List[str] = []

    try:
        expected_zip_names = _prep_contract.expected_day_zip_names(
            esid_padded, raw_folder
        )
    except ValueError as exc:
        # A raw WAV with no 8-digit prefix cannot belong to any day ZIP,
        # so this folder cannot have been per-day prepped from THIS raw
        # folder — the truth set is underivable, not provably wrong.
        return ("Ambiguous", [f"cannot derive the day-ZIP set: {exc}"])

    # ---- Folder-side check (Set A, with day ZIPs in place of the
    #      legacy single ZIP) ----
    expected_folder = expected_folder_basenames(
        esid_padded, companions, day_zip_names=expected_zip_names
    )
    actual_folder = {p.name for p in target_folder.iterdir() if p.is_file()}
    folder_misses = expected_folder - actual_folder
    required_folder_misses = folder_misses - set(_CONDITIONAL_FILES)
    missing.extend(
        f"folder missing: {name}" for name in sorted(required_folder_misses)
    )

    # A day ZIP for a day with no raw audio is as wrong as a missing one.
    actual_zip_names = {
        p.name for p in target_folder.iterdir()
        if p.is_file()
        and (parsed := azus_common.parse_day_zip_name(p.name)) is not None
        and parsed[0] == esid_padded
    }
    missing.extend(
        f"day ZIP exists for a day with no raw audio: {name}"
        for name in sorted(actual_zip_names - set(expected_zip_names))
    )

    # ---- Per-ZIP contents: exactly that day's WAVs (+ CONFIG.TXT) ----
    raw_by_day: Dict[str, Set[str]] = {}
    for wav_name in list_raw_wavs(raw_folder):
        day = azus_common.wav_day_key(wav_name)
        if day is not None:  # None is unreachable past the guard above
            raw_by_day.setdefault(day, set()).add(wav_name)
    has_config = raw_has_config_txt(raw_folder)

    ambiguities: List[str] = []
    for zip_name in expected_zip_names:
        zip_path = target_folder / zip_name
        if not zip_path.is_file():
            continue  # already reported as a folder miss
        contents = list_zip_contents(zip_path)
        if contents is None:
            ambiguities.append(
                f"Could not read the ZIP index of {zip_name} — "
                "archive corrupt?"
            )
            continue
        parsed = azus_common.parse_day_zip_name(zip_name)
        day = parsed[1] if parsed else ""
        expected_contents = set(raw_by_day.get(day, set()))
        if has_config:
            expected_contents.add("CONFIG.TXT")
        # Normalize a lowercase CONFIG.txt copy the same way the
        # single-zip path tolerates it.
        normalized = {
            "CONFIG.TXT" if n.upper() == "CONFIG.TXT" else n
            for n in contents
        }
        missing.extend(
            f"{zip_name} missing: {name}"
            for name in sorted(expected_contents - normalized)
        )
        missing.extend(
            f"{zip_name} unexpected entry: {name}"
            for name in sorted(normalized - expected_contents)
        )

    if missing:
        return ("No", missing)

    # ---- Conditional-file and CONFIG ambiguities (mirror steps 5-6) ----
    for cond in _CONDITIONAL_FILES:
        if cond in folder_misses:
            ambiguities.append(
                f"folder missing conditional file: {cond} "
                f"(could be intentional — depends on site Keywords)"
            )
    if not has_config:
        ambiguities.append(
            "CONFIG.TXT absent from the raw folder (and therefore from "
            "every day ZIP) — could be intentional for a site without a "
            "real device config."
        )

    if ambiguities:
        return ("Ambiguous", ambiguities)
    return ("Yes", [])


def audit_one_esid(
    esid_padded: str,
    raw_folder: Path,
    target_folder: Path,
    companions: Optional[List[str]],
    *,
    audit_all: bool = False,
) -> Tuple[str, List[str]]:
    """Audit one staging or uploaded folder for completeness.

    Returns ``(status, missing_details)`` where ``status`` is one of
    ``"Yes"``, ``"No"``, ``"Ambiguous"`` and ``missing_details`` is a
    list of human-readable strings describing what's missing or why
    the status is what it is (empty for ``"Yes"`` from the fast path).

    See the module docstring's "Status-decision logic" for the rule
    order; this docstring restates it for code-review convenience:

      0. Fast path: if the sentinel exists and ``audit_all`` is False,
         return ``("Yes", [])`` immediately.
      0b. Layout routing (July 2026): no data ZIP at all → ``"No"``;
          BOTH layouts present (mixed) → ``"No"``;
          ``resource_files_list.csv`` couldn't be loaded → ``"Ambiguous"``;
          a PER-DAY folder → :func:`audit_day_zips`; a single-zip
          folder continues with the legacy rules below.
      1. ZIP missing → ``"No"`` unambiguously.
      2. ``resource_files_list.csv`` couldn't be loaded → ``"Ambiguous"``.
      3. ZIP exists but its index couldn't be read → ``"Ambiguous"``.
      4. Any required Set A or Set B file is missing → ``"No"``.
      5. The conditional ``related_identifiers.csv`` is missing → ``"Ambiguous"``.
      6. ``CONFIG.TXT`` absent from BOTH raw and ZIP → ``"Ambiguous"``.
      7. Otherwise → ``"Yes"``.

    Args:
        esid_padded: 3-digit ESID number string (e.g. ``"073"``).
        raw_folder: The original raw ESID folder under the raw-data root.
            Used to determine which WAVs and whether CONFIG.TXT should
            be in the ZIP.
        target_folder: The Staging_Area or Uploaded_Data folder being
            audited.
        companions: List of companion-file basenames from
            ``resource_files_list.csv``, or ``None`` if that CSV could
            not be read (drives the Ambiguous status in step 2).
        audit_all: When True, disable the sentinel fast-path and force
            the deep audit even if the sentinel is present.

    Returns:
        ``(status, missing_details)``.
    """
    missing: List[str] = []

    # ---- Step 0: fast path on sentinel ----
    sentinel = target_folder / _PREP_SENTINEL
    if not audit_all and sentinel.is_file():
        logger.debug(
            "[ESID %s] Fast path: %s present; skipping deep audit.",
            esid_padded, _PREP_SENTINEL,
        )
        return ("Yes", [])

    # ---- Step 0b: layout routing ----
    zip_name = f"ESID_{esid_padded}.zip"
    mode = _prep_contract.staging_zip_mode(target_folder, esid_padded)
    if mode is None:
        missing.append(
            f"ZIP archive missing: neither {zip_name} nor any "
            f"ESID_{esid_padded}_YYYY_MM_DD.zip is present"
        )
        return ("No", missing)
    if mode == _prep_contract.ZIP_MODE_MIXED:
        return ("No", [
            "both the single-site ZIP and per-day ZIPs are present — a "
            "mixed layout no prep produces; re-prep this folder"
        ])

    # ---- Step 2: companion list is required to determine truth set ----
    # (Moved ahead of the layout branch — both layouts need it.)
    if companions is None:
        return ("Ambiguous", ["Could not load Resources/resource_files_list.csv"])

    if mode == _prep_contract.ZIP_MODE_PER_DAY:
        return audit_day_zips(
            esid_padded, raw_folder, target_folder, companions
        )

    # ---- Legacy single-zip audit (steps 1/3-7, unchanged) ----
    zip_path = target_folder / zip_name

    # ---- Step 3: enumerate ZIP contents ----
    zip_contents = list_zip_contents(zip_path)
    if zip_contents is None:
        return (
            "Ambiguous",
            [f"Could not read the ZIP index of {zip_name} — archive corrupt?"],
        )

    # ---- Build the two truth sets ----
    expected_folder = expected_folder_basenames(esid_padded, companions)
    expected_zip = expected_zip_basenames(esid_padded, raw_folder, companions)

    # What the staging/uploaded folder actually contains (basenames).
    actual_folder = {p.name for p in target_folder.iterdir() if p.is_file()}

    # ---- Step 4: detect required misses (Set A + Set B) ----
    folder_misses = expected_folder - actual_folder
    zip_misses = expected_zip - zip_contents

    # Filter conditional files OUT of the No-determining sets — they
    # have their own bucket (step 5).
    required_folder_misses = folder_misses - set(_CONDITIONAL_FILES)
    required_zip_misses = zip_misses - set(_CONDITIONAL_FILES)

    # ``CONFIG.TXT`` is special: it's required in the ZIP only if it
    # was present in the raw folder.  If raw has it but ZIP doesn't,
    # that's a real "No" (step 3 above — required entry missing).
    # If raw doesn't have it and ZIP doesn't either, we treat that as
    # Ambiguous (step 6).  Strip ``CONFIG.TXT`` from required_zip_misses
    # here and re-add it later under the Ambiguous bucket if needed.
    config_missing_from_zip = (
        "CONFIG.TXT" in required_zip_misses or "CONFIG.txt" in required_zip_misses
    )
    if config_missing_from_zip:
        required_zip_misses.discard("CONFIG.TXT")
        required_zip_misses.discard("CONFIG.txt")

    if required_folder_misses:
        missing.extend(f"folder missing: {name}" for name in sorted(required_folder_misses))
    if required_zip_misses:
        missing.extend(f"ZIP missing: {name}" for name in sorted(required_zip_misses))

    # If raw had CONFIG.TXT but ZIP doesn't, that's a real No.
    if config_missing_from_zip and raw_has_config_txt(raw_folder):
        missing.append("ZIP missing: CONFIG.TXT (present in raw folder)")

    if missing:
        return ("No", missing)

    # ---- Step 5: conditional file (related_identifiers.csv) ambiguities ----
    ambiguities: List[str] = []
    for cond in _CONDITIONAL_FILES:
        if cond in folder_misses:
            ambiguities.append(
                f"folder missing conditional file: {cond} "
                f"(could be intentional — depends on site Keywords)"
            )
        if cond in zip_misses:
            ambiguities.append(
                f"ZIP missing conditional file: {cond} "
                f"(could be intentional — depends on site Keywords)"
            )

    # ---- Step 6: CONFIG.TXT absent from both raw and ZIP ----
    if config_missing_from_zip and not raw_has_config_txt(raw_folder):
        ambiguities.append(
            "CONFIG.TXT absent from BOTH raw and ZIP — "
            "could be intentional for a site without a real device config."
        )

    if ambiguities:
        return ("Ambiguous", ambiguities)

    # ---- Step 7: clean Yes ----
    return ("Yes", [])


def backfill_sentinel_if_yes(target_folder: Path, status: str) -> None:
    """Write ``.prep_complete`` into ``target_folder`` iff status is "Yes".

    The sentinel migration touch is what brings legacy folders into the
    sentinel world.  No-op for ``"No"``, ``"Ambiguous"``, or when the
    sentinel already exists (in which case ``Path.touch()`` updates
    mtime — harmless either way).

    Logged at INFO when the sentinel is newly created so the user has
    a clear record of which folders the audit migrated.

    Args:
        target_folder: The Staging_Area or Uploaded_Data folder to
            back-fill the sentinel into.
        status: The audited status; the sentinel is written only when
            this is ``"Yes"``.
    """
    if status != "Yes":
        return
    sentinel = target_folder / _PREP_SENTINEL
    already_present = sentinel.is_file()
    try:
        sentinel.touch()
    except OSError as exc:
        logger.warning(
            "Could not write sentinel %s: %s", sentinel, exc,
        )
        return
    if not already_present:
        logger.info("Back-filled sentinel: %s", sentinel)


# =====================================================================
# CSV report
# =====================================================================

def default_output_path() -> Path:
    """Return a timestamped CSV filename in the current working directory.

    Returns:
        A ``Path`` of the form
        ``prep_completeness_report_YYYYMMDD_HHMMSS.csv`` in ``Path.cwd()``.
    """
    return azus_common.timestamped_output_path("prep_completeness_report")


def write_report(rows: List[Dict[str, str]], output_path: Path) -> None:
    """Write the 4-column audit report to ``output_path``.

    Columns, in order:
      1. ``ESID#``           — zero-padded 3-digit ESID number
      2. ``Staging Area``    — basename of the matching Staging_Area folder, or ""
      3. ``Uploaded Data``   — basename of the matching Uploaded_Data folder, or ""
      4. ``Prep Completed``  — "Yes" | "No" | "Ambiguous"

    Args:
        rows: One dict per ESID, each keyed by the four column names.
        output_path: Destination path for the CSV file.
    """
    fieldnames = ["ESID#", "Staging Area", "Uploaded Data", "Prep Completed"]
    with output_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    logger.info("Report written: %s (%d rows)", output_path, len(rows))


# =====================================================================
# Main
# =====================================================================

def _print_summary(rows: List[Dict[str, str]], output_path: Path) -> None:
    """Log a one-line tally of Yes / No / Ambiguous / no-folder counts.

    Args:
        rows: The report rows (as passed to ``write_report``).
        output_path: The CSV path, echoed in the summary for reference.
    """
    counts = {"Yes": 0, "No": 0, "Ambiguous": 0}
    no_folder_yet = 0
    for row in rows:
        counts[row["Prep Completed"]] = counts.get(row["Prep Completed"], 0) + 1
        if not row["Staging Area"] and not row["Uploaded Data"]:
            no_folder_yet += 1
    logger.info("=" * 70)
    logger.info("AUDIT SUMMARY")
    logger.info("=" * 70)
    logger.info("Total ESIDs scanned:    %d", len(rows))
    logger.info("  Prep Completed = Yes:        %d", counts["Yes"])
    logger.info("  Prep Completed = No:         %d", counts["No"])
    logger.info("  Prep Completed = Ambiguous:  %d", counts["Ambiguous"])
    logger.info("  (of those, %d have no staging or uploaded folder yet)", no_folder_yet)
    logger.info("Report saved to: %s", output_path)


def main() -> None:
    """Command-line entry point.  See module docstring for usage."""
    parser = argparse.ArgumentParser(
        description=(
            "Audit completeness of prepared ESID staging and uploaded folders. "
            "Walks raw ESID directories, finds matching folders in Staging_Area/ "
            "and Uploaded_Data/, and verifies (folder contents + ZIP index of the "
            "ZIP) that every file prepare_dataset.py should have produced is "
            "present.  Writes a 4-column CSV report and back-fills the "
            ".prep_complete sentinel on folders the audit confirms."
        ),
    )
    parser.add_argument(
        "raw_data_dir",
        help=(
            "Path to the folder containing raw ESID subdirectories — the same "
            "folder you would pass to prep_all_datasets.py."
        ),
    )
    parser.add_argument(
        "--resources-dir", default="Resources",
        help=(
            "Path to the AZUS Resources/ directory (default: Resources). "
            "Used to read resource_files_list.csv, which is the source of "
            "truth for which companion files should appear in each prepared "
            "folder and inside each ZIP."
        ),
    )
    parser.add_argument(
        "--output", default=None, metavar="PATH",
        help=(
            "Write the CSV report to this path.  Default: "
            "prep_completeness_report_YYYYMMDD_HHMMSS.csv in the current "
            "working directory."
        ),
    )
    parser.add_argument(
        "--audit-all", action="store_true",
        help=(
            "Force the deep audit on every folder, even those that already "
            "carry a .prep_complete sentinel.  Use this to detect drift caused "
            "by manual edits.  Without this flag, sentinel-bearing folders "
            "are trusted and the deep audit is skipped for them."
        ),
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help=(
            "Log per-ESID details (every missing file or ambiguity reason) "
            "at INFO level.  Without this flag, only one short status line "
            "per ESID is logged."
        ),
    )
    args = parser.parse_args()

    # ---- Configure logging once, here ----
    azus_common.configure_logging(verbose=args.verbose)

    raw_data_dir = Path(args.raw_data_dir)
    if not raw_data_dir.is_dir():
        logger.error("Raw-data folder not found or not a directory: %s", raw_data_dir)
        sys.exit(2)

    resources_dir = Path(args.resources_dir)
    if not resources_dir.is_dir():
        # Try resolving relative to the project root in case the user ran
        # this from somewhere other than the project root.
        alt = _PROJECT_ROOT / args.resources_dir
        if alt.is_dir():
            resources_dir = alt
        else:
            logger.error("Resources directory not found: %s", resources_dir)
            sys.exit(2)

    output_path = Path(args.output) if args.output else default_output_path()

    # ---- Banner ----
    logger.info("=" * 70)
    logger.info("AZUS PREP-COMPLETENESS AUDIT")
    logger.info("=" * 70)
    logger.info("Raw data dir:    %s", raw_data_dir.resolve())
    logger.info("Resources dir:   %s", resources_dir.resolve())
    logger.info("Staging_Area/:   %s", _STAGING_AREA)
    logger.info("Uploaded_Data/:  %s", _UPLOADED_DATA)
    logger.info("Output:          %s", output_path)
    logger.info("Mode:            %s", "deep audit (every folder)" if args.audit_all
                else "fast path on sentinel, deep audit otherwise")
    logger.info("=" * 70)

    # ---- Discover raw ESIDs ----
    raw_esids = find_raw_esid_folders(raw_data_dir)
    if not raw_esids:
        logger.warning("No ESID_NNN subdirectories found in %s", raw_data_dir)
        write_report([], output_path)
        sys.exit(0)

    logger.info("Found %d ESID folder(s) in raw data.", len(raw_esids))

    # ---- Load the companion-file truth set once ----
    companions = load_resource_companion_files(resources_dir)
    if companions is None:
        logger.warning(
            "Could not read %s/%s — every audited folder will be Ambiguous.",
            resources_dir, _RESOURCE_FILES_LIST_BASENAME,
        )
    else:
        logger.info(
            "Loaded %d companion file name(s) from %s.",
            len(companions), _RESOURCE_FILES_LIST_BASENAME,
        )

    # ---- Audit each ESID ----
    rows: List[Dict[str, str]] = []
    for _, padded, raw_folder in raw_esids:
        staging = find_in_staging(padded)
        uploaded = find_in_uploaded(padded)
        staging_name = staging.name if staging else ""
        uploaded_name = uploaded.name if uploaded else ""

        # Choose which folder to audit.  Prefer Staging_Area if both
        # exist (Uploaded_Data is canonically "already shipped" and
        # not normally something we re-check).
        target = staging or uploaded
        if target is None:
            status = "No"
            details = ["No prepared folder exists in Staging_Area/ or Uploaded_Data/."]
            logger.info("[ESID %s] No — no prepared folder found yet.", padded)
        else:
            status, details = audit_one_esid(
                esid_padded=padded,
                raw_folder=raw_folder,
                target_folder=target,
                companions=companions,
                audit_all=args.audit_all,
            )
            # Status line — short by default, full details on --verbose.
            location = "Staging_Area" if target == staging else "Uploaded_Data"
            short_reason = details[0] if details else (
                "fast path on sentinel" if not args.audit_all
                and (target / _PREP_SENTINEL).is_file() else "all required files present"
            )
            logger.info(
                "[ESID %s] %s — %s/%s — %s",
                padded, status, location, target.name, short_reason,
            )
            if args.verbose and len(details) > 1:
                for line in details:
                    logger.debug("  [ESID %s]   %s", padded, line)
            # Back-fill the sentinel if the deep audit confirmed Yes
            # (and the fast path didn't already trust it).
            backfill_sentinel_if_yes(target, status)

        rows.append({
            "ESID#": padded,
            "Staging Area": staging_name,
            "Uploaded Data": uploaded_name,
            "Prep Completed": status,
        })

    # ---- Write the CSV and print summary ----
    write_report(rows, output_path)
    _print_summary(rows, output_path)

    # ---- Exit code: 1 if any No was recorded, else 0 ----
    if any(r["Prep Completed"] == "No" for r in rows):
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
