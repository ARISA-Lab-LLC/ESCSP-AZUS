#!/usr/bin/env python3
"""AZUS Standalone Upload Pipeline.

This module contains the complete upload pipeline for AZUS: dataset discovery,
metadata construction, Zenodo upload orchestration, and result tracking.

All project-specific identity (creators, contributors, funding, etc.) is read
from ``Resources/project_config.json``.  No Eclipse Soundscapes–specific data
is hardcoded in this file.

Usage:
    python standalone_tasks.py [--config Resources/config.json] [--dry-run]

Design notes for future Prefect integration:
    Every public function in this module is a plain synchronous function.
    To convert to Prefect tasks, simply decorate them with ``@task`` and
    call them from a ``@flow``-decorated orchestrator.  The ``upload_datasets``
    function is the natural entry point for a Prefect flow.
"""

# ---------------------------------------------------------------------------
# Standard library
# ---------------------------------------------------------------------------
import argparse
import csv
import glob
import hashlib
import json
import logging
import os
import sys
import time
import zipfile
from pathlib import Path
from string import Template
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Project models (no external dependencies beyond Pydantic)
# ---------------------------------------------------------------------------
from models.invenio import (
    Metadata,
    Identifier,
    PersonOrganization,
    Affiliation,
    Role,
    Creator,
    Contributor,
    ResourceType,
    License,
    Language,
    Date,
    DateType,
    Funder,
    Award,
    AwardTitle,
    Funding,
    Subject,
    RelationType,
    RelatedIdentifier,
    Reference,
)
from models.audiomoth import (
    DatasetCategory,
    EclipseType,        # backward-compatible alias
    DataCollector,
    UploadData,
    PersistedResult,
    DraftConfig,
    Access,
)
from standalone_uploader import upload_to_zenodo, get_credentials_from_env

# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------
logger = logging.getLogger("azus")

# Date format used in Zenodo metadata
UPLOAD_DATE_FORMAT = "%Y-%m-%d"

# SHA-512 read buffer — 64 KB gives good throughput on large files
_HASH_BUFFER_SIZE = 65_536


# ===================================================================
#  Project configuration loader
# ===================================================================

def load_project_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """Load the project identity configuration from a JSON file.

    The config file contains all project-specific metadata: creators,
    contributors, funding, community ID, custom fields, CSV header
    expectations, and default file lists.

    Args:
        config_path: Path to the project config JSON.  Defaults to
            ``Resources/project_config.json`` relative to this script.

    Returns:
        Parsed JSON dictionary.

    Raises:
        FileNotFoundError: If the config file does not exist.
        json.JSONDecodeError: If the file is not valid JSON.
    """
    if config_path is None:
        config_path = str(
            Path(__file__).parent / "Resources" / "project_config.json"
        )

    config_file = Path(config_path)
    if not config_file.exists():
        raise FileNotFoundError(
            f"Project config not found: {config_file}\n"
            f"Copy templates/project_config.json.example to "
            f"Resources/project_config.json and fill in your project details."
        )

    with open(config_file, "r", encoding="utf-8") as fh:
        config = json.load(fh)

    logger.info("Loaded project config: %s", config_file.name)
    return config


# ===================================================================
#  Metadata builders — driven by project_config.json
# ===================================================================

def _build_person_or_org(entry: Dict[str, Any]) -> PersonOrganization:
    """Build a PersonOrganization model from a config entry.

    Args:
        entry: Dictionary with keys: type, name/given_name/family_name, orcid.

    Returns:
        PersonOrganization Pydantic model.
    """
    identifiers = None
    if entry.get("orcid"):
        identifiers = [Identifier(scheme="orcid", identifier=entry["orcid"])]

    return PersonOrganization(
        type=entry["type"],
        given_name=entry.get("given_name"),
        family_name=entry.get("family_name"),
        name=entry.get("name"),
        identifiers=identifiers,
    )


def build_creators(project_config: Dict[str, Any]) -> List[Creator]:
    """Build the list of Zenodo creators from project configuration.

    Args:
        project_config: Parsed project_config.json.

    Returns:
        List of Creator models.
    """
    creators = []
    for entry in project_config.get("creators", []):
        # Filter out empty strings — Zenodo rejects {"name": ""} affiliations
        affiliations = [
            Affiliation(name=aff)
            for aff in entry.get("affiliations", [])
            if aff and aff.strip()
        ]
        creators.append(
            Creator(
                person_or_org=_build_person_or_org(entry),
                role=Role(id=entry.get("role", "other")),
                affiliations=affiliations if affiliations else None,
            )
        )
    return creators


def build_contributors(project_config: Dict[str, Any]) -> List[Contributor]:
    """Build the list of Zenodo contributors from project configuration.

    Args:
        project_config: Parsed project_config.json.

    Returns:
        List of Contributor models.
    """
    contributors = []
    for entry in project_config.get("contributors", []):
        # Filter out empty strings — Zenodo rejects {"name": ""} affiliations
        affiliations = [
            Affiliation(name=aff)
            for aff in entry.get("affiliations", [])
            if aff and aff.strip()
        ]
        contributors.append(
            Contributor(
                person_or_org=_build_person_or_org(entry),
                role=Role(id=entry.get("role", "other")),
                affiliations=affiliations if affiliations else None,
            )
        )
    return contributors


def build_fundings(project_config: Dict[str, Any]) -> List[Funding]:
    """Build the list of funding entries from project configuration.

    Args:
        project_config: Parsed project_config.json.

    Returns:
        List of Funding models.
    """
    fundings = []
    for entry in project_config.get("funding", []):
        # Build award identifiers (typically a URL)
        award_identifiers = None
        if entry.get("award_url"):
            award_identifiers = [
                Identifier(scheme="url", identifier=entry["award_url"])
            ]

        fundings.append(
            Funding(
                funder=Funder(id=entry.get("funder_id")),
                award=Award(
                    title=AwardTitle(en=entry.get("award_title", "")),
                    number=entry.get("award_number"),
                    identifiers=award_identifiers,
                ),
            )
        )
    return fundings


# ===================================================================
#  Utility functions
# ===================================================================

def calculate_sha512(filepath: str) -> str:
    """Calculate the SHA-512 hash of a file.

    Uses a 64 KB read buffer for efficient hashing of large files.

    Args:
        filepath: Path to the file.

    Returns:
        Hex-encoded SHA-512 digest string.
    """
    sha512_hash = hashlib.sha512()
    with open(filepath, "rb") as fh:
        for chunk in iter(lambda: fh.read(_HASH_BUFFER_SIZE), b""):
            sha512_hash.update(chunk)
    return sha512_hash.hexdigest()


def parse_values_from_str(string: str, delimiter: str = ":") -> List[str]:
    """Split a delimited string and strip whitespace from each value.

    Args:
        string: Input string (e.g., "value1 : value2 : value3").
        delimiter: Separator character.

    Returns:
        List of stripped strings.
    """
    return [value.strip() for value in string.split(sep=delimiter)]


# ===================================================================
#  CSV parsing and validation
# ===================================================================

def parse_collectors_csv(
    csv_file_path: str,
    dataset_category: str,
    project_config: Optional[Dict[str, Any]] = None,
) -> List[DataCollector]:
    """Parse a collectors CSV file into DataCollector models.

    Validates that all required headers (from project config) are present
    before parsing rows.

    Args:
        csv_file_path: Path to the collectors CSV.
        dataset_category: Category string (e.g., 'Total', 'Annular') used
            to determine which conditional headers are required.
        project_config: Parsed project_config.json.  If None, loads default.

    Returns:
        List of DataCollector models.

    Raises:
        ValueError: If required headers are missing or CSV is empty.
    """
    if project_config is None:
        project_config = load_project_config()

    with open(csv_file_path, mode="r", encoding="utf-8") as csv_file:
        csv_reader = csv.DictReader(csv_file)
        csv_headers = csv_reader.fieldnames

        if not csv_headers:
            raise ValueError("No headers found in the CSV file.")

        # --- Validate required headers from config ---
        expected_headers = list(project_config.get("csv_required_headers", []))

        # Add conditional headers for this dataset category
        conditional = project_config.get("csv_conditional_headers", {})
        if dataset_category in conditional:
            expected_headers.extend(conditional[dataset_category])

        missing_headers = set(expected_headers) - set(csv_headers)
        if missing_headers:
            raise ValueError(
                f"Expected CSV headers not found: {missing_headers}"
            )

        data = [DataCollector.model_validate(row) for row in csv_reader]

    logger.info("Parsed %d rows from %s", len(data), Path(csv_file_path).name)
    return data


# ===================================================================
#  Related-identifier helpers
# ===================================================================

# Map human-readable resource_type labels (as entered in Zenodo's UI or
# exported from older spreadsheets) to InvenioRDM vocabulary IDs.
# Keys are lowercased + stripped versions of whatever might appear in CSVs.
# See: https://github.com/inveniosoftware/invenio-rdm-records/blob/master/
#      invenio_rdm_records/fixtures/data/vocabularies/resource_types.yaml
_RESOURCE_TYPE_MAP: Dict[str, str] = {
    # Audio / Video
    "video/audio":              "audiovisual",
    "audiovisual":              "audiovisual",
    "audio":                    "audiovisual",
    "video":                    "audiovisual",
    # Publications
    "publication-article":      "publication-article",
    "journal article":          "publication-article",
    "article":                  "publication-article",
    "publication-report":       "publication-report",
    "publication / report":     "publication-report",
    "publication/report":       "publication-report",
    "report":                   "publication-report",
    "publication-preprint":     "publication-preprint",
    "preprint":                 "publication-preprint",
    "publication-book":         "publication-book",
    "book":                     "publication-book",
    "publication-section":      "publication-section",
    "book chapter":             "publication-section",
    "publication-thesis":       "publication-thesis",
    "thesis":                   "publication-thesis",
    "publication":              "publication",
    # Data / Software
    "dataset":                  "dataset",
    "software":                 "software",
    "image":                    "image",
    "other":                    "other",
}


def _normalize_resource_type(raw: str) -> str:
    """Map a human-readable resource_type label to an InvenioRDM vocabulary ID.

    Strips, lowercases, and looks up ``raw`` in :data:`_RESOURCE_TYPE_MAP`.
    Falls back to ``"other"`` if the value is not recognised.

    Args:
        raw: Resource type string from a CSV cell (any capitalisation).

    Returns:
        InvenioRDM vocabulary ID string.
    """
    normalised = raw.strip().lower()
    vocab_id = _RESOURCE_TYPE_MAP.get(normalised)
    if vocab_id is None:
        logger.warning(
            "Unknown resource_type %r — defaulting to 'other'.  "
            "Add it to _RESOURCE_TYPE_MAP if needed.",
            raw,
        )
        vocab_id = "other"
    return vocab_id


def read_related_identifiers_from_csv(
    csv_path: Optional[str],
) -> List[RelatedIdentifier]:
    """Read related identifiers (citations, related works) from a CSV.

    Expected CSV columns: identifier, scheme, relation_type, resource_type

    All values are normalised before being passed to the InvenioRDM API:

    * ``scheme`` — lowercased (e.g., ``"DOI"`` → ``"doi"``).
    * ``relation_type`` — stripped, lowercased, spaces removed to produce the
      InvenioRDM vocabulary ID (e.g., ``"Is supplemented by"`` →
      ``"issupplementedby"``).  Human-readable labels from Zenodo's upload form
      are accepted and translated automatically.
    * ``resource_type`` — mapped from human-readable labels to InvenioRDM
      vocabulary IDs via :data:`_RESOURCE_TYPE_MAP`.  Unknown values fall back
      to ``"other"``.

    Args:
        csv_path: Path to the CSV file.  If None/empty, returns [].

    Returns:
        List of RelatedIdentifier models.
    """
    if not csv_path or not csv_path.strip():
        return []

    csv_file = Path(csv_path)
    if not csv_file.exists():
        logger.info("Related identifiers CSV not found: %s", csv_path)
        return []

    related_identifiers: List[RelatedIdentifier] = []

    try:
        with open(csv_file, mode="r", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            required_cols = {"identifier", "scheme", "relation_type"}

            if not required_cols.issubset(set(reader.fieldnames or [])):
                logger.warning(
                    "Related identifiers CSV missing required columns: %s "
                    "(found: %s)",
                    required_cols, reader.fieldnames,
                )
                return []

            for row_num, row in enumerate(reader, start=2):
                raw_identifier = row.get("identifier", "").strip()
                if not raw_identifier:
                    continue

                # --- Normalize scheme: must be lowercase ---
                scheme = row.get("scheme", "").strip().lower()

                # --- Normalize relation_type: strip → lowercase → remove spaces
                #     This converts both already-correct IDs ("cites") and
                #     human-readable labels ("Is supplemented by") to the
                #     InvenioRDM vocabulary ID format ("issupplementedby"). ---
                raw_rt = row.get("relation_type", "").strip()
                relation_type_id = raw_rt.lower().replace(" ", "")

                # --- Normalize resource_type to InvenioRDM vocabulary ID ---
                resource_type = None
                raw_rt_type = row.get("resource_type", "").strip()
                if raw_rt_type:
                    resource_type_id = _normalize_resource_type(raw_rt_type)
                    resource_type = ResourceType(id=resource_type_id)

                try:
                    related_identifiers.append(
                        RelatedIdentifier(
                            identifier=raw_identifier,
                            scheme=scheme,
                            # InvenioRDM requires relation_type as {"id": "..."}
                            relation_type=RelationType(id=relation_type_id),
                            resource_type=resource_type,
                        )
                    )
                except Exception as exc:
                    logger.warning(
                        "Error parsing related identifier on row %d: %s "
                        "(row data: %s)",
                        row_num, exc, row,
                    )

        logger.info(
            "Loaded %d related identifier(s) from %s",
            len(related_identifiers), csv_file.name,
        )

    except Exception as exc:
        logger.warning(
            "Error reading related identifiers CSV %s: %s", csv_path, exc
        )

    return related_identifiers


def read_references_from_csv(csv_path: Optional[str]) -> List[Reference]:
    """Read bibliographic reference strings from a CSV.

    Expected CSV column: reference

    InvenioRDM requires references as ``{"reference": "..."}`` objects, NOT
    plain strings.  This function wraps each citation string in a
    :class:`~models.invenio.Reference` model automatically.

    Args:
        csv_path: Path to the CSV file.  If None/empty, returns [].

    Returns:
        List of Reference models, each containing a ``reference`` string.
    """
    if not csv_path or not csv_path.strip():
        return []

    csv_file = Path(csv_path)
    if not csv_file.exists():
        logger.info("References CSV not found: %s", csv_path)
        return []

    references: List[Reference] = []

    try:
        with open(csv_file, mode="r", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            if "reference" not in (reader.fieldnames or []):
                logger.warning("References CSV missing 'reference' column")
                return []

            for row in reader:
                ref_str = row.get("reference", "").strip()
                if ref_str:
                    references.append(Reference(reference=ref_str))

        logger.info(
            "Loaded %d reference(s) from %s", len(references), csv_file.name
        )

    except Exception as exc:
        logger.warning("Error reading references CSV %s: %s", csv_path, exc)

    return references


# ===================================================================
#  File discovery
# ===================================================================

def read_upload_manifest(
    manifest_path: Path,
    dataset_dir: Path,
) -> Dict[str, Optional[str]]:
    """Read an upload manifest CSV and locate all listed files.

    Args:
        manifest_path: Path to the ESID_XXX_to_upload.csv manifest.
        dataset_dir: Directory to search for files.

    Returns:
        Dictionary mapping filenames to their full paths.

    Raises:
        FileNotFoundError: If any files listed in the manifest are missing.
    """
    logger.info("Reading upload manifest: %s", manifest_path.name)

    files_to_upload: List[str] = []

    with open(manifest_path, "r", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if "File Name" not in (reader.fieldnames or []):
            raise ValueError(
                f"Manifest CSV missing 'File Name' column. "
                f"Found columns: {reader.fieldnames}"
            )
        for row in reader:
            filename = row.get("File Name", "").strip()
            if filename:
                files_to_upload.append(filename)

    logger.info("Manifest lists %d files to upload", len(files_to_upload))

    # Locate each file on disk
    found_files: Dict[str, Optional[str]] = {}
    missing_files: List[str] = []

    for filename in files_to_upload:
        file_path = dataset_dir / filename
        if file_path.exists() and file_path.is_file():
            found_files[filename] = str(file_path)
        else:
            found_files[filename] = None
            missing_files.append(filename)

    found_count = sum(1 for v in found_files.values() if v is not None)
    logger.info("Found %d/%d files", found_count, len(files_to_upload))

    if missing_files:
        logger.error("Missing %d files: %s", len(missing_files), missing_files[:5])
        raise FileNotFoundError(
            f"Missing {len(missing_files)} files listed in manifest. "
            f"First missing: {missing_files[0]}"
        )

    return found_files


def find_dataset_files(
    zip_file_path: str,
    required_files: Optional[List[str]] = None,
    project_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Optional[str]]:
    """Discover all files associated with a dataset.

    Checks for an ``ESID_XXX_to_upload.csv`` manifest first.  If found,
    uses that to determine files.  Otherwise falls back to the default
    required file list from project configuration.

    Args:
        zip_file_path: Path to the main ZIP file.
        required_files: Override list of filenames to look for.
        project_config: Parsed project_config.json.

    Returns:
        Dictionary mapping filenames to their full paths (None if missing).
    """
    zip_path = Path(zip_file_path)
    if not zip_path.exists():
        raise FileNotFoundError(f"ZIP file not found: {zip_file_path}")
    if not zip_path.is_file():
        raise ValueError(f"Path is not a file: {zip_file_path}")

    dataset_dir = zip_path.parent

    # Extract ESID from ZIP filename (e.g., "ESID_005.zip" → "005")
    zip_name = zip_path.stem
    esid = None
    if zip_name.startswith("ESID_"):
        esid = zip_name.replace("ESID_", "").split("_")[0]

    # --- Try upload manifest first ---
    if esid:
        manifest_path = dataset_dir / f"ESID_{esid}_to_upload.csv"
        if manifest_path.exists():
            logger.info("Found upload manifest: %s", manifest_path.name)
            return read_upload_manifest(manifest_path, dataset_dir)

    # --- Fall back to default file list ---
    logger.info("No upload manifest found, using default file discovery")

    if required_files is None:
        if project_config is None:
            project_config = load_project_config()
        required_files = project_config.get("default_required_files", [])

    found_files: Dict[str, Optional[str]] = {}
    missing_files: List[str] = []

    for filename in required_files:
        file_path = dataset_dir / filename
        if file_path.exists() and file_path.is_file():
            found_files[filename] = str(file_path)
        else:
            found_files[filename] = None
            missing_files.append(filename)

    found_count = sum(1 for v in found_files.values() if v is not None)
    logger.info(
        "Found %d/%d files for %s", found_count, len(required_files), zip_path.name
    )
    if missing_files:
        logger.warning("Missing %d files: %s", len(missing_files), ", ".join(missing_files[:5]))

    return found_files


# ===================================================================
#  Directory and file operations
# ===================================================================

def rename_dir_files(directory: str) -> None:
    """Rename files in a directory, replacing '#' with '_'.

    This normalizes ESID#XXX filenames to ESID_XXX format.

    Args:
        directory: Path to directory to scan.

    Raises:
        ValueError: If the directory is invalid.
    """
    if not directory or not os.path.isdir(directory):
        raise ValueError(f"Invalid directory: {directory}")

    for root, _, files in os.walk(directory):
        for filename in files:
            if filename.startswith("ESID#") and filename.endswith("zip"):
                old_path = os.path.join(root, filename)
                new_path = os.path.join(root, filename.replace("#", "_"))
                os.rename(old_path, new_path)
                logger.debug("Renamed %s → %s", filename, Path(new_path).name)


def list_dir_files(
    directory: str,
    file_pattern: str = "*",
) -> List[str]:
    """List files in a directory matching a glob pattern.

    Args:
        directory: Directory path.
        file_pattern: Glob pattern (default: all files).

    Returns:
        List of matching file paths.

    Raises:
        ValueError: If directory is invalid.
    """
    if not directory or not os.path.isdir(directory):
        raise ValueError(f"Invalid directory: {directory}")

    search_pattern = os.path.join(directory, file_pattern)
    return [f for f in glob.glob(search_pattern) if os.path.isfile(f)]


def get_esid_file_pairs(files: List[str]) -> List[Tuple[str, str]]:
    """Extract ESID numbers from filenames and pair with file paths.

    Args:
        files: List of file paths (e.g., ``['.../ESID_005.zip']``).

    Returns:
        List of (esid, file_path) tuples.
    """
    return [
        (Path(f).stem.split("_")[-1].strip(), f)
        for f in files
    ]


# ===================================================================
#  Recording date extraction
# ===================================================================

def get_recording_dates(
    zip_file: str,
    project_config: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    """Extract the earliest and latest recording dates from a ZIP archive.

    Reads WAV filenames inside the ZIP (without extracting) and parses
    dates from the ``YYYYMMDD_HHMMSS`` naming convention.

    Args:
        zip_file: Path to the dataset ZIP file.
        project_config: Project config (for minimum_recording_year).

    Returns:
        Tuple of (start_date, end_date) in YYYY-MM-DD format.

    Raises:
        ValueError: If no valid dates are found.
    """
    if not zip_file or not os.path.exists(zip_file):
        raise ValueError(f"Invalid or missing ZIP file: {zip_file}")

    if project_config is None:
        project_config = load_project_config()

    minimum_year = project_config.get("minimum_recording_year", 2000)

    with zipfile.ZipFile(zip_file, "r") as zf:
        wav_stems = [
            Path(name).stem
            for name in zf.namelist()
            if name.lower().endswith(".wav")
        ]

    from datetime import datetime as _dt

    dates = []
    for stem in wav_stems:
        try:
            date_str = stem.split("_")[0]
            parsed = _dt.strptime(date_str, "%Y%m%d").date()
            if parsed.year >= minimum_year:
                dates.append(parsed)
        except (ValueError, IndexError):
            continue

    if not dates:
        raise ValueError("No valid dates found in WAV file names.")

    return (
        min(dates).strftime(UPLOAD_DATE_FORMAT),
        max(dates).strftime(UPLOAD_DATE_FORMAT),
    )


# ===================================================================
#  Upload data assembly
# ===================================================================

def create_upload_data(
    esid_file_pairs: List[Tuple[str, str]],
    data_collectors: List[DataCollector],
    project_config: Optional[Dict[str, Any]] = None,
) -> Tuple[List[UploadData], List[str]]:
    """Combine ESID/file pairs with collector metadata into UploadData objects.

    Args:
        esid_file_pairs: List of (ESID, zip_file) tuples.
        data_collectors: List of DataCollector models.
        project_config: Project config (for file discovery).

    Returns:
        Tuple of (upload_data_list, unmatched_esid_list).
    """
    collector_dict = {dc.esid: dc for dc in data_collectors}
    upload_data: List[UploadData] = []
    unmatched_ids: List[str] = []

    for esid, zip_file in esid_file_pairs:
        if esid not in collector_dict:
            logger.warning("No collector info found for ESID: %s", esid)
            unmatched_ids.append(esid)
            continue

        dataset_files = find_dataset_files(
            zip_file, project_config=project_config
        )

        # find_dataset_files drives the upload manifest, which intentionally
        # excludes README.html (its content becomes the Zenodo description
        # field, it is not uploaded as a file).  Resolve README.html and
        # README.md directly from the ESID staging directory so they are
        # always found regardless of what the manifest contains.
        esid_staging_dir = Path(zip_file).parent
        readme_html_path = esid_staging_dir / "README.html"
        readme_md_path   = esid_staging_dir / "README.md"

        # Exclude files that are handled via dedicated UploadData fields:
        #   README.html  — content becomes the Zenodo description; not uploaded as a file
        #   README.md    — added explicitly via UploadData.readme_md
        #   ESID_XXX.zip — added explicitly via UploadData.zip_file
        # Without this exclusion the ZIP would appear twice in all_files
        # (once here, once from zip_file), causing a 400 "already exists" error
        # on the second upload attempt.
        zip_filename = Path(zip_file).name
        excluded = {"README.html", "README.md", zip_filename}
        additional_files = [
            path for filename, path in dataset_files.items()
            if path and filename not in excluded
        ]

        data = UploadData(
            esid=esid,
            data_collector=collector_dict[esid],
            zip_file=zip_file,
            readme_html=str(readme_html_path) if readme_html_path.exists() else None,
            readme_md=str(readme_md_path) if readme_md_path.exists() else None,
            additional_files=additional_files,
        )

        logger.info(
            "Prepared ESID %s: ZIP + %d additional files = %d total",
            esid, len(additional_files), len(data.all_files),
        )

        if not data.readme_html:
            logger.warning("ESID %s — README.html not found", esid)

        upload_data.append(data)

    return upload_data, unmatched_ids


# ===================================================================
#  Draft record configuration builder
# ===================================================================

def get_draft_config(
    data_collector: DataCollector,
    readme_html_path: Optional[str] = None,
    related_identifiers_csv: Optional[str] = None,
    references_csv: Optional[str] = None,
    project_config: Optional[Dict[str, Any]] = None,
    reserve_doi: bool = False,
) -> DraftConfig:
    """Build a complete Zenodo draft record configuration.

    All project-specific metadata (creators, contributors, funding,
    community, custom fields) is read from ``project_config``.

    Args:
        data_collector: Collector metadata for this dataset.
        readme_html_path: Path to README.html (content = Zenodo description).
        related_identifiers_csv: Path to related identifiers CSV.
        references_csv: Path to references CSV.
        project_config: Parsed project_config.json.  Loads default if None.
        reserve_doi: Reserve a DataCite DOI at draft creation time.
            Only meaningful for production Zenodo — Sandbox DOIs are not
            registered with DataCite.  Defaults to False.

    Returns:
        DraftConfig model ready for the Zenodo API.

    Raises:
        ValueError: If readme_html_path is not provided.
        FileNotFoundError: If README.html does not exist.
    """
    if project_config is None:
        project_config = load_project_config()

    # --- Read description from README.html ---
    if not readme_html_path:
        raise ValueError(
            "README.html path is required. "
            "Run prepare_dataset.py first to generate README.html."
        )

    readme_path = Path(readme_html_path)
    if not readme_path.exists():
        raise FileNotFoundError(
            f"README.html not found at: {readme_html_path}\n"
            f"Run prepare_dataset.py to generate it before uploading."
        )

    logger.info("Using description from README.html: %s", readme_html_path)
    description = readme_path.read_text(encoding="utf-8")

    # --- Build recording date metadata ---
    # Use a single EDTF date interval ("start/end") instead of two separate
    # "collected" entries.  InvenioRDM supports this natively and it produces
    # cleaner metadata than two disconnected date entries.
    from datetime import datetime as _dt

    dates: List[Date] = []
    first_day = data_collector.first_recording_day
    last_day  = data_collector.last_recording_day

    if first_day and last_day:
        if first_day == last_day:
            # Recording happened on a single day — no interval needed
            dates.append(Date(
                date=first_day,
                type=DateType(id="collected"),
                description="Day of recording",
            ))
        else:
            # EDTF interval: "YYYY-MM-DD/YYYY-MM-DD"
            dates.append(Date(
                date=f"{first_day}/{last_day}",
                type=DateType(id="collected"),
                description="Recording period",
            ))
    elif first_day:
        dates.append(Date(
            date=first_day,
            type=DateType(id="collected"),
            description="Day of recording",
        ))

    # --- Build creators (from config + volunteer) ---
    creators = build_creators(project_config)

    volunteer_label = project_config.get("volunteer_creator_label", "")
    if volunteer_label:
        # Volunteers are anonymous — use organizational type with just a label.
        # personal type requires a non-empty given_name, which we do not have.
        volunteer_affiliations = [
            Affiliation(name=aff)
            for aff in parse_values_from_str(data_collector.affiliation)
            if aff and aff.strip()   # filter empty affiliation strings
        ]
        creators.append(Creator(
            person_or_org=PersonOrganization(
                type="organizational",
                name=volunteer_label,
            ),
            role=Role(id=project_config.get("volunteer_creator_role", "datacollector")),
            affiliations=volunteer_affiliations if volunteer_affiliations else None,
        ))

    # --- Build subjects from CSV keywords ---
    subjects = [
        Subject(subject=s)
        for s in parse_values_from_str(data_collector.subjects)
    ]

    # --- Load related identifiers and references from CSV ---
    related_identifiers = read_related_identifiers_from_csv(related_identifiers_csv)
    references = read_references_from_csv(references_csv)

    # --- Build title from template ---
    title_template = Template(
        project_config.get("title_template", "$esid")
    )
    title = title_template.safe_substitute(
        esid=data_collector.esid,
        eclipse_date=data_collector.eclipse_date,
        eclipse_label=data_collector.eclipse_label(),
    )

    # --- Assemble Metadata ---
    metadata = Metadata(
        resource_type=ResourceType(
            id=project_config.get("resource_type", "dataset")
        ),
        title=title,
        publication_date=_dt.now().strftime(UPLOAD_DATE_FORMAT),
        creators=creators,
        description=description,
        funding=build_fundings(project_config),
        rights=[License(id=project_config.get("license", "cc-by-4.0"))],
        languages=[
            Language(id=lang)
            for lang in project_config.get("languages", ["eng"])
        ],
        dates=dates if dates else None,
        version=data_collector.version,
        publisher=project_config.get("publisher", "Zenodo"),
        subjects=subjects,
        related_identifiers=related_identifiers if related_identifiers else None,
        references=references if references else None,
        contributors=build_contributors(project_config) or None,
    )

    # Reserve a DOI at draft creation if requested.
    # Zenodo/InvenioRDM requires provider="datacite" and an empty identifier
    # string to trigger DOI reservation.
    pids = (
        {"doi": {"provider": "datacite", "identifier": ""}}
        if reserve_doi
        else {}
    )

    return DraftConfig(
        record_access=Access.PUBLIC,
        files_access=Access.PUBLIC,
        files_enabled=True,
        metadata=metadata.to_dict(),
        community_id=project_config.get("community_id", ""),
        custom_fields=project_config.get("custom_fields"),
        pids=pids,
    )


# ===================================================================
#  Result persistence
# ===================================================================

def save_result_csv(file: str, result: PersistedResult) -> None:
    """Append an upload result to a local CSV file.

    Creates the file and writes a header row if it does not yet exist.

    Args:
        file: CSV file path.
        result: Upload result to persist.

    Raises:
        ValueError: If file path is empty.
    """
    if not file:
        raise ValueError("Invalid file path for result CSV")

    output_file = Path(file)
    new_file = not output_file.exists()

    if new_file:
        logger.info("Creating results CSV: %s", file)
        output_file.parent.mkdir(exist_ok=True, parents=True)

    result_dict = result.model_dump()

    with open(file, mode="a", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=result_dict.keys())
        if new_file:
            writer.writeheader()
        writer.writerow(result_dict)


# ===================================================================
#  Upload tracker — prevents duplicate uploads across runs
# ===================================================================

class UploadTracker:
    """Track which files have already been uploaded to avoid duplicates.

    Persists the list of uploaded file paths to a simple text file.

    Attributes:
        tracker_file: Path to the tracker persistence file.
        uploaded_files: Set of previously uploaded file paths.
    """

    def __init__(self, tracker_file: str = ".uploaded_files.txt"):
        self.tracker_file = Path(tracker_file)
        self.uploaded_files = self._load()

    def _load(self) -> set:
        """Load previously uploaded file paths from disk."""
        if self.tracker_file.exists():
            with open(self.tracker_file, "r", encoding="utf-8") as fh:
                return {line.strip() for line in fh if line.strip()}
        return set()

    def is_uploaded(self, file_path: str) -> bool:
        """Check if a file has already been uploaded."""
        return file_path in self.uploaded_files

    def mark_uploaded(self, file_path: str) -> None:
        """Mark a file as successfully uploaded."""
        self.uploaded_files.add(file_path)
        with open(self.tracker_file, "a", encoding="utf-8") as fh:
            fh.write(f"{file_path}\n")

    def get_count(self) -> int:
        """Return the number of previously uploaded files."""
        return len(self.uploaded_files)


# ===================================================================
#  Single-dataset upload
# ===================================================================

def save_result(
    esid: str,
    zip_file: str,
    success: bool,
    success_file: str,
    failure_file: str,
    api_response: Optional[Dict[str, Any]] = None,
    error_type: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    """Save the result of an upload attempt.

    Routes the result to either the success or failure CSV file.

    Args:
        esid: Dataset identifier.
        zip_file: Path to the uploaded ZIP file.
        success: Whether the upload succeeded.
        success_file: CSV path for successful results.
        failure_file: CSV path for failed results.
        api_response: Zenodo API response dictionary.
        error_type: Error class name if failed.
        error_message: Error description if failed.
    """
    results_file = success_file if success else failure_file
    persisted_result = PersistedResult(esid=esid)

    if not success:
        persisted_result.error_type = error_type or "Unknown"
        persisted_result.error_message = error_message or "Upload failed"

    if api_response:
        persisted_result.update(api_response)

    save_result_csv(file=results_file, result=persisted_result)

    if success:
        logger.info("ESID %s: Upload successful", esid)
    else:
        logger.error("ESID %s: Upload failed — %s", esid, error_message)


# ===================================================================
#  Metadata JSON persistence
# ===================================================================

def save_metadata_json(
    config: "DraftConfig",
    esid: str,
    output_dir: Path,
) -> Optional[Path]:
    """Write the Zenodo API payload for this record to a local JSON file.

    Saves the exact JSON structure that will be sent to the Zenodo
    ``POST /records`` endpoint, plus a ``_generated_at`` timestamp and
    a ``_azus_note`` header for provenance.  The file is named
    ``ESID_XXX_metadata.json`` and sits alongside the other staging files.

    Failures are logged as warnings — a write error here must never
    abort an upload.

    Args:
        config: ``DraftConfig`` produced by ``get_draft_config()``.
        esid: ESID number string (e.g., ``'005'``).
        output_dir: Staging directory where the file will be written.

    Returns:
        Path to the written JSON file, or ``None`` if writing failed.
    """
    from datetime import datetime as _dt

    json_path = output_dir / f"ESID_{esid}_metadata.json"

    # Reconstruct the exact payload upload_to_zenodo() will POST,
    # with provenance headers prepended.
    record_access = (
        config.record_access.value
        if hasattr(config.record_access, "value")
        else config.record_access
    )
    files_access = (
        config.files_access.value
        if hasattr(config.files_access, "value")
        else config.files_access
    )

    payload: Dict[str, Any] = {
        "_azus_note": (
            "This file records the metadata submitted to Zenodo for this "
            "upload.  It is generated by AZUS immediately before upload and "
            "is for local review and auditing only — it is not uploaded to "
            "Zenodo."
        ),
        "_generated_at": _dt.now().isoformat(timespec="seconds"),
        "access": {"record": record_access, "files": files_access},
        "files": {"enabled": config.files_enabled},
        "metadata": config.metadata,
    }

    if config.pids:
        payload["pids"] = config.pids
    if config.community_id:
        payload["parent"] = {
            "communities": {"ids": [config.community_id]}
        }
    if config.custom_fields:
        payload["custom_fields"] = config.custom_fields

    try:
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        logger.info("  Metadata JSON saved: %s", json_path.name)
        return json_path
    except Exception as exc:
        logger.warning(
            "Could not write metadata JSON for ESID %s: %s", esid, exc
        )
        return None


def upload_dataset(
    data: UploadData,
    delete_failures: bool = False,
    auto_publish: bool = False,
    reserve_doi: bool = False,
    related_identifiers_csv: Optional[str] = None,
    references_csv: Optional[str] = None,
    project_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Upload a single dataset to Zenodo.

    Extracts recording dates, builds the draft configuration from the
    project config, then calls the Zenodo uploader.

    Args:
        data: UploadData bundle for this dataset.
        delete_failures: Delete the draft if upload fails.
        auto_publish: Publish the record after successful upload.
        reserve_doi: Reserve a DataCite DOI at draft creation time.
        related_identifiers_csv: Global path to related identifiers CSV.
            Overridden by a per-record file in the ESID staging directory
            if one exists.
        references_csv: Global path to references CSV.
            Overridden by a per-record file in the ESID staging directory
            if one exists.
        project_config: Parsed project_config.json.

    Returns:
        Dictionary with keys: 'successful' (bool), 'api_response', 'error'.
    """
    logger.info("Starting upload for ESID %s", data.esid)
    logger.info("  ZIP file: %s", Path(data.zip_file).name)
    logger.info("  Total files: %d", len(data.all_files))

    import traceback

    esid_dir = Path(data.zip_file).parent

    # ------------------------------------------------------------------
    # Phase 1: Build the Zenodo draft configuration.
    # Kept separate from the upload phase so that if config construction
    # fails, we can report the error cleanly without entering the upload
    # path.  The metadata JSON is saved immediately after this phase so
    # it exists on disk regardless of what happens during the upload.
    # ------------------------------------------------------------------
    try:
        # Extract recording dates from the ZIP archive
        start_date, end_date = get_recording_dates(
            zip_file=data.zip_file, project_config=project_config
        )
        logger.debug("  Recording period: %s to %s", start_date, end_date)

        data.data_collector.first_recording_day = start_date
        data.data_collector.last_recording_day = end_date

        # Per-record citation override with global fallback.
        # If related_identifiers.csv or references.csv exists inside the
        # ESID staging directory, use it instead of the global config path.
        effective_related_csv = (
            str(esid_dir / "related_identifiers.csv")
            if (esid_dir / "related_identifiers.csv").exists()
            else related_identifiers_csv
        )
        effective_references_csv = (
            str(esid_dir / "references.csv")
            if (esid_dir / "references.csv").exists()
            else references_csv
        )
        if effective_related_csv != related_identifiers_csv:
            logger.info("  Using per-record related_identifiers.csv")
        if effective_references_csv != references_csv:
            logger.info("  Using per-record references.csv")

        config = get_draft_config(
            data_collector=data.data_collector,
            readme_html_path=data.readme_html,
            related_identifiers_csv=effective_related_csv,
            references_csv=effective_references_csv,
            project_config=project_config,
            reserve_doi=reserve_doi,
        )

    except Exception as exc:
        logger.error("Failed to build draft config for ESID %s: %s", data.esid, exc)
        logger.debug("Full traceback:\n%s", traceback.format_exc())
        return {
            "successful": False,
            "error": {
                "type": type(exc).__name__,
                "error_message": str(exc),
            },
            "api_response": None,
        }

    # ------------------------------------------------------------------
    # Phase 2: Persist the metadata payload to disk.
    # Called unconditionally after a successful config build — before the
    # upload attempt — so the JSON file exists whether the upload succeeds,
    # fails, or is interrupted.  save_metadata_json() has its own internal
    # try/except and will never raise here.
    # ------------------------------------------------------------------
    save_metadata_json(
        config=config,
        esid=data.esid,
        output_dir=esid_dir,
    )

    # ------------------------------------------------------------------
    # Phase 3: Upload to Zenodo.
    # ------------------------------------------------------------------
    try:
        logger.info("Uploading to Zenodo...")
        result = upload_to_zenodo(
            files=data.all_files,
            config=config,
            delete_on_failure=delete_failures,
            auto_publish=auto_publish,
            request_log_path=str(
                esid_dir / f"ESID_{data.esid}_request_log.json"
            ),
        )
        return result

    except Exception as exc:
        logger.error("Exception during upload for ESID %s: %s", data.esid, exc)
        logger.debug("Full traceback:\n%s", traceback.format_exc())
        return {
            "successful": False,
            "error": {
                "type": type(exc).__name__,
                "error_message": str(exc),
            },
            "api_response": None,
        }


# ===================================================================
#  Multi-dataset upload orchestrator
# ===================================================================

def get_upload_data(
    data_dir: str,
    data_collectors_file: str,
    dataset_category: str,
    failure_results_file: str,
    tracker: UploadTracker,
    project_config: Optional[Dict[str, Any]] = None,
) -> List[UploadData]:
    """Discover and prepare all datasets in a directory for upload.

    Args:
        data_dir: Directory containing ESID subdirectories with ZIP files.
        data_collectors_file: Path to the collectors CSV.
        dataset_category: Category string (e.g., 'Total', 'Annular').
        failure_results_file: CSV path for logging failures.
        tracker: UploadTracker to skip already-uploaded files.
        project_config: Parsed project_config.json.

    Returns:
        List of UploadData objects ready for upload.
    """
    if not data_dir:
        raise ValueError("Missing data directory")
    if not data_collectors_file:
        raise ValueError("Missing data collectors file")

    logger.info("Loading data collectors from: %s", data_collectors_file)
    data_collectors = parse_collectors_csv(
        csv_file_path=data_collectors_file,
        dataset_category=dataset_category,
        project_config=project_config,
    )
    logger.info("Loaded %d data collector records", len(data_collectors))

    # Discover ZIP files in ESID subdirectories
    logger.info("Scanning directory: %s", data_dir)
    data_path = Path(data_dir)
    dir_files: List[str] = []

    for subdir in data_path.iterdir():
        if subdir.is_dir() and (
            subdir.name.startswith("ESID_") or subdir.name.startswith("ESID#")
        ):
            for zip_file in subdir.glob("ESID_*.zip"):
                dir_files.append(str(zip_file))

    logger.info("Found %d ZIP files in ESID subdirectories", len(dir_files))

    # Skip already-uploaded files
    original_count = len(dir_files)
    dir_files = [f for f in dir_files if not tracker.is_uploaded(f)]
    skipped = original_count - len(dir_files)
    if skipped:
        logger.info("Skipped %d already-uploaded file(s)", skipped)

    if not dir_files:
        logger.warning("No new files to upload")
        return []

    esid_file_pairs = get_esid_file_pairs(files=dir_files)

    upload_data, unmatched_ids = create_upload_data(
        esid_file_pairs=esid_file_pairs,
        data_collectors=data_collectors,
        project_config=project_config,
    )

    for esid in unmatched_ids:
        logger.warning("No collector data found for ESID: %s", esid)
        save_result_csv(
            file=failure_results_file,
            result=PersistedResult(
                esid=esid,
                error_message="Unable to find data collector info",
            ),
        )

    logger.info("Prepared %d dataset(s) for upload", len(upload_data))
    return upload_data


def upload_datasets(
    datasets: List[Dict[str, str]],
    successful_results_file: str,
    failure_results_file: str,
    related_identifiers_csv: Optional[str] = None,
    references_csv: Optional[str] = None,
    auto_publish: bool = False,
    delete_failures: bool = False,
    reserve_doi: bool = False,
    project_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, int]:
    """Upload all configured datasets to Zenodo.

    Iterates over the ``datasets`` list from config.json.  Each entry
    specifies a directory, collectors CSV, and dataset category.  This
    single loop replaces the former duplicate annular/total processing.

    Args:
        datasets: List of dataset config dicts, each with keys:
            'name', 'dataset_dir', 'collectors_csv', 'dataset_category'.
        successful_results_file: CSV path for successful uploads.
        failure_results_file: CSV path for failed uploads.
        related_identifiers_csv: Global path to related identifiers CSV.
        references_csv: Global path to references CSV.
        auto_publish: Publish records after successful upload.
        delete_failures: Delete draft records on failure.
        reserve_doi: Reserve a DataCite DOI at draft creation time.
        project_config: Parsed project_config.json.

    Returns:
        Dictionary with upload statistics:
        {'total_processed', 'successful', 'failed', 'skipped'}.
    """
    if not datasets:
        raise ValueError("No datasets configured for upload")

    tracker = UploadTracker()
    logger.info("Upload tracker: %d file(s) previously uploaded", tracker.get_count())

    stats = {"total_processed": 0, "successful": 0, "failed": 0, "skipped": 0}

    # --- Process each dataset category in a single unified loop ---
    for dataset_entry in datasets:
        dataset_name = dataset_entry.get("name", "Unnamed")
        dataset_dir = dataset_entry.get("dataset_dir", "")
        collectors_csv = dataset_entry.get("collectors_csv", "")
        dataset_category = dataset_entry.get("dataset_category", "")

        if not dataset_dir or not collectors_csv:
            logger.warning(
                "Skipping dataset '%s': missing dataset_dir or collectors_csv",
                dataset_name,
            )
            continue

        logger.info("=" * 70)
        logger.info("PROCESSING: %s", dataset_name)
        logger.info("=" * 70)

        rename_dir_files(directory=dataset_dir)

        category_upload_data = get_upload_data(
            data_dir=dataset_dir,
            data_collectors_file=collectors_csv,
            dataset_category=dataset_category,
            failure_results_file=failure_results_file,
            tracker=tracker,
            project_config=project_config,
        )

        for i, data in enumerate(category_upload_data, 1):
            logger.info(
                "Processing %d/%d: ESID %s",
                i, len(category_upload_data), data.esid,
            )

            result = upload_dataset(
                data=data,
                delete_failures=delete_failures,
                auto_publish=auto_publish,
                reserve_doi=reserve_doi,
                related_identifiers_csv=related_identifiers_csv,
                references_csv=references_csv,
                project_config=project_config,
            )

            stats["total_processed"] += 1

            if result["successful"]:
                stats["successful"] += 1
                tracker.mark_uploaded(data.zip_file)
                save_result(
                    esid=data.esid,
                    zip_file=data.zip_file,
                    success=True,
                    success_file=successful_results_file,
                    failure_file=failure_results_file,
                    api_response=result.get("api_response"),
                )
            else:
                stats["failed"] += 1
                error = result.get("error", {})
                save_result(
                    esid=data.esid,
                    zip_file=data.zip_file,
                    success=False,
                    success_file=successful_results_file,
                    failure_file=failure_results_file,
                    error_type=error.get("type"),
                    error_message=error.get("error_message"),
                )

    return stats


# ===================================================================
#  CLI entry point
# ===================================================================

def main() -> None:
    """Command-line entry point for AZUS standalone upload."""
    parser = argparse.ArgumentParser(
        description="AZUS Standalone Upload — Upload datasets to Zenodo"
    )
    parser.add_argument(
        "--config", type=str, default="Resources/config.json",
        help="Path to configuration file (default: Resources/config.json)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Validate configuration without uploading",
    )
    args = parser.parse_args()

    # --- Configure logging ---
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler("azus_upload.log"),
            logging.StreamHandler(sys.stdout),
        ],
    )

    # --- Load configuration ---
    config_path = Path(args.config)
    if not config_path.exists():
        logger.error("Configuration file not found: %s", config_path)
        sys.exit(1)

    logger.info("Loading configuration from: %s", config_path)
    with open(config_path, "r", encoding="utf-8") as fh:
        config_data = json.load(fh)

    if "uploads" not in config_data:
        logger.error("Invalid configuration: missing 'uploads' section")
        sys.exit(1)

    uploads_config = config_data["uploads"]

    # --- Load project identity ---
    project_config_path = config_data.get("project_config", None)
    project_config = load_project_config(project_config_path)

    # --- Validate credentials ---
    try:
        get_credentials_from_env()
        logger.info("Zenodo credentials loaded from environment")
    except ValueError as exc:
        logger.error("%s", exc)
        logger.error("Run: source Resources/set_env.sh")
        sys.exit(1)

    # --- Extract dataset list ---
    datasets = uploads_config.get("datasets", [])
    related_identifiers_csv = uploads_config.get("related_identifiers_csv", "")
    references_csv = uploads_config.get("references_csv", "")

    # --- Display configuration ---
    logger.info("=" * 70)
    logger.info("AZUS STANDALONE UPLOAD")
    logger.info("=" * 70)
    logger.info("Project: %s", project_config.get("project_name", "Unknown"))
    logger.info("Datasets configured: %d", len(datasets))
    for ds in datasets:
        logger.info("  • %s → %s", ds.get("name", "?"), ds.get("dataset_dir", "?"))
    logger.info("Auto-publish: %s", uploads_config.get("auto_publish", False))
    logger.info("Delete failures: %s", uploads_config.get("delete_failures", False))
    logger.info("Reserve DOI: %s", uploads_config.get("reserve_doi", False))
    logger.info("=" * 70)

    # --- CSV pre-validation ---
    if not args.dry_run:
        logger.info("VALIDATING CSV FILES")
        for ds in datasets:
            csv_file = ds.get("collectors_csv", "")
            category = ds.get("dataset_category", "")
            if csv_file:
                logger.info("Checking %s CSV: %s", ds.get("name", "?"), Path(csv_file).name)
                try:
                    collectors = parse_collectors_csv(
                        csv_file, category, project_config
                    )
                    logger.info("  Valid — %d records", len(collectors))
                except Exception as exc:
                    logger.error("  CSV validation failed: %s", exc)
                    logger.error(
                        "Fix with: python validate_csv.py %s --eclipse-type %s",
                        csv_file, category.lower(),
                    )
                    sys.exit(1)

    if args.dry_run:
        logger.info("Dry run complete — configuration is valid")
        sys.exit(0)

    # --- Confirmation prompt ---
    print("\n⚠️  You are about to upload datasets to Zenodo.")
    print("   This will create REAL records on Zenodo.")
    response = input("\nProceed? (yes/no): ")
    if response.lower() != "yes":
        logger.info("Upload cancelled by user")
        sys.exit(0)

    # --- Run upload ---
    try:
        stats = upload_datasets(
            datasets=datasets,
            successful_results_file=uploads_config.get(
                "successful_results_file", "Records/successful_results.csv"
            ),
            failure_results_file=uploads_config.get(
                "failure_results_file", "Records/failed_results.csv"
            ),
            related_identifiers_csv=related_identifiers_csv,
            references_csv=references_csv,
            auto_publish=uploads_config.get("auto_publish", False),
            delete_failures=uploads_config.get("delete_failures", False),
            reserve_doi=uploads_config.get("reserve_doi", False),
            project_config=project_config,
        )

        # --- Display summary ---
        logger.info("=" * 70)
        logger.info("UPLOAD SUMMARY")
        logger.info("=" * 70)
        logger.info("Total processed: %d", stats["total_processed"])
        logger.info("Successful:      %d", stats["successful"])
        logger.info("Failed:          %d", stats["failed"])
        logger.info("Skipped:         %d", stats["skipped"])
        logger.info("=" * 70)

        if stats["failed"]:
            logger.warning(
                "%d upload(s) failed — check %s for details",
                stats["failed"],
                uploads_config.get("failure_results_file"),
            )

        sys.exit(0 if stats["failed"] == 0 else 1)

    except KeyboardInterrupt:
        logger.warning("Upload interrupted by user")
        sys.exit(130)
    except Exception as exc:
        logger.error("Fatal error: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
