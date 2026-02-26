"""AZUS data models for datasets and upload tracking.

This module defines the core data models used throughout AZUS:
  - DatasetCategory: Flexible category labels (replaces hardcoded EclipseType)
  - DataCollector: Metadata parsed from a collectors CSV row
  - UploadData: Bundle of files and metadata for a single Zenodo upload
  - PersistedResult: Upload result persisted to local CSV
  - DraftConfig: Complete Zenodo draft record configuration
  - Access: Record/file access level enum

No external dependencies beyond Pydantic. Prefect and prefect_invenio_rdm
are no longer required.
"""

from typing import Optional, List, Dict, Any
from enum import Enum
from pydantic import BaseModel, ConfigDict, Field, AliasChoices


# ---------------------------------------------------------------------------
# Dataset category (generalizes the old EclipseType enum)
# ---------------------------------------------------------------------------

class DatasetCategory(str, Enum):
    """Category of a dataset within a project.

    For Eclipse Soundscapes these map to eclipse types. Other citizen science
    projects can add their own categories or ignore this enum entirely —
    the upload pipeline uses the string value, not the enum member.

    Attributes:
        ANNULAR: Annular solar eclipse dataset.
        TOTAL: Total solar eclipse dataset.
        PARTIAL: Partial solar eclipse dataset.
    """

    ANNULAR = "Annular"
    TOTAL = "Total"
    PARTIAL = "Partial"


# Backwards-compatible alias so existing code referencing EclipseType still works
EclipseType = DatasetCategory


# ---------------------------------------------------------------------------
# Data collector — one row from the collectors CSV
# ---------------------------------------------------------------------------

class DataCollector(BaseModel):
    """Metadata for a single data collection site, parsed from CSV.

    Field aliases allow the model to be populated directly from CSV
    column headers (e.g., ``DataCollector.model_validate(row_dict)``).

    Attributes:
        esid: Unique site/dataset identifier.
        affiliation: Semicolon-delimited collector affiliations.
        files_date_time_mode: How WAV file timestamps were set.
        first_recording_day: Earliest recording date (set at upload time).
        last_recording_day: Latest recording date (set at upload time).
        version: Dataset version string (e.g., '2024.1.0').
        latitude: Site latitude in decimal degrees.
        longitude: Site longitude in decimal degrees.
        eclipse_date: Date of the eclipse event.
        eclipse_type: Category of eclipse at this site.
        eclipse_coverage: Percentage of sun obscured.
        eclipse_start_time_utc: 1st contact time.
        eclipse_totality_start_time_utc: 2nd contact time (N/A if partial).
        eclipse_maximum_time_utc: Maximum eclipse time.
        eclipse_totality_end_time_utc: 3rd contact time (N/A if partial).
        eclipse_end_time_utc: 4th contact time.
        subjects: Colon-delimited keywords/subjects string.
    """

    model_config = ConfigDict(use_enum_values=True)

    esid: str = Field(
        validation_alias=AliasChoices("esid", "ESID"))

    affiliation: str = Field(
        validation_alias=AliasChoices("affiliation", "Data Collector Affiliations"))

    files_date_time_mode: str = Field(
        validation_alias=AliasChoices(
            "files_date_time_mode", "WAV Files Time & Date Settings"))

    first_recording_day: Optional[str] = Field(
        validation_alias=AliasChoices("first_recording_day", "Day of First Recording"),
        default=None)

    last_recording_day: Optional[str] = Field(
        validation_alias=AliasChoices("last_recording_day", "Day of Last Recording"),
        default=None)

    version: str = Field(
        validation_alias=AliasChoices("version", "Version"))

    latitude: str = Field(
        validation_alias=AliasChoices("latitude", "Latitude"))

    longitude: str = Field(
        validation_alias=AliasChoices("longitude", "Longitude"))

    eclipse_date: str = Field(
        validation_alias=AliasChoices("eclipse_date", "Eclipse Date"))

    eclipse_type: DatasetCategory = Field(
        validation_alias=AliasChoices("eclipse_type", "Local Eclipse Type"))

    eclipse_coverage: str = Field(
        validation_alias=AliasChoices("eclipse_coverage", "Eclipse Percent (%)"))

    eclipse_start_time_utc: str = Field(
        validation_alias=AliasChoices(
            "eclipse_start_time_utc", "Eclipse Start Time (UTC) (1st Contact)"))

    eclipse_totality_start_time_utc: Optional[str] = Field(
        validation_alias=AliasChoices(
            "eclipse_totality_start_time_utc",
            "Totality Start Time (UTC) (2nd Contact)"),
        default="N/A")

    eclipse_maximum_time_utc: str = Field(
        validation_alias=AliasChoices(
            "eclipse_maximum_time_utc", "Eclipse Maximum (UTC)"))

    eclipse_totality_end_time_utc: Optional[str] = Field(
        validation_alias=AliasChoices(
            "eclipse_totality_end_time_utc",
            "Totality End Time (UTC) (3rd Contact)"),
        default="N/A")

    eclipse_end_time_utc: Optional[str] = Field(
        validation_alias=AliasChoices(
            "eclipse_end_time_utc",
            "Eclipse End Time (UTC) (4th Contact)"),
        default="N/A")

    subjects: Optional[str] = Field(
        validation_alias=AliasChoices("subjects", "Keywords and subjects"))

    def eclipse_label(self) -> str:
        """Generate a human-readable label from the eclipse type.

        Returns:
            Label string, e.g. 'Total Solar Eclipse'.
        """
        # use_enum_values=True means eclipse_type is already a string
        labels = {
            "Total": "Total Solar Eclipse",
            "Annular": "Annular Solar Eclipse",
            "Partial": "Partial Solar Eclipse",
        }
        return labels.get(self.eclipse_type, f"{self.eclipse_type} Solar Eclipse")


# ---------------------------------------------------------------------------
# Upload data — files and metadata for a single Zenodo upload
# ---------------------------------------------------------------------------

class UploadData(BaseModel):
    """Bundle of files and metadata for uploading one dataset to Zenodo.

    Attributes:
        esid: Unique dataset identifier.
        data_collector: Collector metadata for this site.
        zip_file: Path to the main ZIP archive.
        readme_html: Path to README.html (content used as Zenodo description).
        readme_md: Path to README.md (uploaded as a file).
        additional_files: Paths to supporting files (data dicts, license, etc.).
    """

    esid: str
    data_collector: DataCollector
    zip_file: str
    readme_html: Optional[str] = None
    readme_md: Optional[str] = None
    additional_files: List[str] = []

    @property
    def all_files(self) -> List[str]:
        """Assemble the complete list of files to upload.

        Order: README.md (if present), then additional metadata files, then the
        ZIP archive last.  Uploading small files first keeps early failures cheap
        and ensures Zenodo has full metadata context before the large binary
        transfer begins.  README.html is NOT included — its content becomes the
        Zenodo description field, it is not uploaded as a file.

        Returns:
            Complete list of file paths to upload, ZIP last.
        """
        files: List[str] = []
        if self.readme_md:
            files.append(self.readme_md)
        files.extend(self.additional_files)
        files.append(self.zip_file)  # ZIP last — largest file uploads after metadata
        return files


# ---------------------------------------------------------------------------
# Persisted result — upload outcome saved to local CSV
# ---------------------------------------------------------------------------

class PersistedResult(BaseModel):
    """Data from an upload result persisted to a local CSV file.

    Attributes:
        esid: Unique dataset identifier.
        id: Zenodo deposition identifier.
        doi: Digital Object Identifier (assigned on publish).
        recid: Record identifier.
        created: Creation timestamp.
        modified: Last modification timestamp.
        updated: Last update timestamp.
        owners: User identifiers of deposition owners.
        status: Deposition status string.
        state: Deposition state ('inprogress', 'done', 'error').
        submitted: Whether the deposition has been published.
        link: URL of the created or published deposition.
        error_type: Error type if upload failed.
        error_message: Error message if upload failed.
    """

    esid: Optional[str] = None
    id: Optional[int] = None
    doi: Optional[str] = None
    recid: Optional[str] = None
    created: Optional[str] = None
    modified: Optional[str] = None
    updated: Optional[str] = None
    owners: Optional[str] = None
    status: Optional[str] = None
    state: Optional[str] = None
    submitted: Optional[bool] = None
    link: Optional[str] = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None

    # Fields that map directly from the Zenodo API response
    _DIRECT_FIELDS = {
        "id", "doi", "recid", "created", "modified",
        "updated", "status", "state", "submitted",
    }

    def update(self, api_response: Dict[str, Any]) -> None:
        """Update this model from a Zenodo API response dictionary.

        Args:
            api_response: JSON response from the Zenodo API.
        """
        # Map simple fields that share the same name
        for field_name in self._DIRECT_FIELDS:
            if field_name in api_response:
                setattr(self, field_name, api_response[field_name])

        # Special handling for nested / renamed fields
        if "owners" in api_response:
            self.owners = str(api_response["owners"])

        if "links" in api_response and "self_html" in api_response["links"]:
            self.link = api_response["links"]["self_html"]


# ---------------------------------------------------------------------------
# Draft configuration — replaces prefect_invenio_rdm DraftConfig / Access
# ---------------------------------------------------------------------------

class Access(str, Enum):
    """Zenodo record/file access level.

    Attributes:
        PUBLIC: Publicly accessible.
        RESTRICTED: Restricted access.
    """

    PUBLIC = "public"
    RESTRICTED = "restricted"


class DraftConfig(BaseModel):
    """Complete configuration for creating a Zenodo draft record.

    This replaces the ``prefect_invenio_rdm.models.records.DraftConfig``
    so that AZUS has no dependency on Prefect packages.

    Attributes:
        record_access: Access level for the record metadata.
        files_access: Access level for uploaded files.
        files_enabled: Whether file uploads are enabled.
        metadata: Record metadata dictionary (from Metadata.to_dict()).
        community_id: Optional Zenodo community to submit to.
        custom_fields: Optional Zenodo custom metadata fields.
        pids: Optional persistent identifier configuration.
    """

    record_access: Access = Access.PUBLIC
    files_access: Access = Access.PUBLIC
    files_enabled: bool = True
    metadata: Dict[str, Any] = {}
    community_id: Optional[str] = None
    custom_fields: Optional[Dict[str, Any]] = None
    pids: Optional[Dict[str, Any]] = None
