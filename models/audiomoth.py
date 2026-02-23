"""AudioMoth models."""

from typing import Optional, List, Dict, Any
from enum import Enum
from pydantic import BaseModel, ConfigDict, Field, AliasChoices
from prefect.blocks.core import Block


class EclipseType(str, Enum):
    """
    * ANNULAR: Annular solar eclipse.
    * TOTAL: Total solar eclipse.
    * PARTIAL: Partial solar eclipse.
    """

    ANNULAR = "Annular"
    TOTAL = "Total"
    PARTIAL = "Partial"


class DataCollector(BaseModel):
    """
    An AudioMoth data collector.
    """

    model_config = ConfigDict(use_enum_values=True)

    esid: str = Field(validation_alias=AliasChoices("esid", "ESID"))

    affiliation: str = Field(
        validation_alias=AliasChoices("affiliation", "Data Collector Affiliations")
    )

    files_date_time_mode: str = Field(
        validation_alias=AliasChoices(
            "files_date_time_mode", "WAV Files Time & Date Settings"
        )
    )

    first_recording_day: Optional[str] = Field(
        validation_alias=AliasChoices("first_recording_day", "Day of First Recording"),
        default=None,
    )

    last_recording_day: Optional[str] = Field(
        validation_alias=AliasChoices("last_recording_day", "Day of Last Recording"),
        default=None,
    )

    version: str = Field(validation_alias=AliasChoices("version", "Version"))

    latitude: str = Field(validation_alias=AliasChoices("latitude", "Latitude"))

    longitude: str = Field(validation_alias=AliasChoices("longitude", "Longitude"))

    eclipse_date: str = Field(
        validation_alias=AliasChoices("eclipse_date", "Eclipse Date")
    )

    eclipse_type: EclipseType = Field(
        validation_alias=AliasChoices("eclipse_type", "Local Eclipse Type")
    )

    eclipse_coverage: str = Field(
        validation_alias=AliasChoices("eclipse_coverage", "Eclipse Percent (%)")
    )

    eclipse_start_time_utc: str = Field(
        validation_alias=AliasChoices(
            "eclipse_start_time_utc", "Eclipse Start Time (UTC) (1st Contact)"
        )
    )

    eclipse_totality_start_time_utc: Optional[str] = Field(
        validation_alias=AliasChoices(
            "eclipse_totality_start_time_utc", "Totality Start Time (UTC) (2nd Contact)"
        ),
        default="N/A",
    )

    eclipse_maximum_time_utc: str = Field(
        validation_alias=AliasChoices(
            "eclipse_maximum_time_utc", "Eclipse Maximum (UTC)"
        )
    )

    eclipse_totality_end_time_utc: Optional[str] = Field(
        validation_alias=AliasChoices(
            "eclipse_totality_end_time_utc", "Totality End Time (UTC) (3rd Contact)"
        ),
        default="N/A",
    )

    eclipse_end_time_utc: Optional[str] = Field(
        validation_alias=AliasChoices("eclipse_end_time_utc", "Eclipse End Time (UTC) (4th Contact)"),
        default="N/A",
    )

    subjects: Optional[str] = Field(
        validation_alias=AliasChoices("subjects", "Keywords and subjects"),
    )

    def eclipse_label(self) -> str:
        """
        Creates a label from the eclipse type.
        """
        return (
            "Total Solar Eclipse"
            if self.eclipse_type == "Total"
            else "Annular Solar Eclipse"
        )


class UploadData(BaseModel):
    """
    The necessary data to perform an upload.
    
    This model has been updated to support the new workflow where files are
    pre-generated before upload. It now tracks the main ZIP file, README files,
    and all additional supporting files that need to be uploaded to Zenodo.

    Attributes:
        esid (str): A unique AudioMoth ID.
        data_collector (DataCollector): Data collector information.
        zip_file (str): Path to the main ZIP file containing WAV audio files.
        readme_html (Optional[str]): Path to README.html file. This file's content
            will be used as the Zenodo record description.
        readme_md (Optional[str]): Path to README.md file (Markdown version).
        additional_files (List[str]): List of paths to additional files that need
            to be uploaded. These typically include:
            - 2024_total_eclipse_data_data_dict.csv
            - AudioMoth_Operation_Manual.pdf
            - CONFIG.TXT
            - CONFIG_data_dict.csv
            - License.txt
            - WAV_data_dict.csv
            - file_list.csv
            - file_list_data_dict.csv
            - total_eclipse_data.csv
    
    Example:
        >>> data = UploadData(
        ...     esid="004",
        ...     data_collector=collector,
        ...     zip_file="/path/to/ESID_004.zip",
        ...     readme_html="/path/to/README.html",
        ...     readme_md="/path/to/README.md",
        ...     additional_files=[
        ...         "/path/to/file_list.csv",
        ...         "/path/to/CONFIG.TXT",
        ...         # ... other files
        ...     ]
        ... )
        >>> print(f"Will upload {len(data.all_files)} files")
    """

    esid: str
    data_collector: DataCollector
    zip_file: str
    readme_html: Optional[str] = None
    readme_md: Optional[str] = None
    additional_files: List[str] = []
    
    @property
    def all_files(self) -> List[str]:
        """
        Get a complete list of all files to upload.
        
        This property assembles all files in the correct order:
        1. The main ZIP file (always first)
        2. README.md (if present)
        3. All additional supporting files
        
        Note: README.html is NOT included in the upload list because its
        content is used as the Zenodo description, not as a separate file.
        
        Returns:
            List[str]: Complete list of file paths to upload to Zenodo.
        
        Example:
            >>> data = UploadData(...)
            >>> files = data.all_files
            >>> for f in files:
            ...     print(f"- {Path(f).name}")
        """
        files = [self.zip_file]
        
        # Add README.md if available
        # Note: README.html is used for description, not uploaded as a file
        if self.readme_md:
            files.append(self.readme_md)
        
        # Add all additional supporting files
        files.extend(self.additional_files)
        
        return files


class PersistedResult(BaseModel):
    """
    Data from an upload result to persist locally.

    Attributes:
        esid (Optional[str]): A unique AudioMoth ID.
        id (Optional[int]): Deposition identifier.
        doi (Optional[str]): Digital Object Identifier (DOI).
            When the deposition is published, a DOI is automatically
            registered in DataCite for the upload.
        recid (Optional[str]): Record identifier.
        created (Optional[str]): The created date.
        modified (Optional[str]): Last modification time of deposition.
        updated (Optional[str]): Last modification time of deposition.
        owners (Optional[str]): User identifiers of the owners of the deposition.
        status (Optional[str]): The status of the deposition.
        state (Optional[str]): The state of the deposition (inprogress, done or error).
        submitted (Optional[bool]): True if the deposition has been published, False otherwise.
        link (Optional[str]): Link of the created or published deposition.
        error_type (Optional[str]): The upload error type.
        error_message (Optional[str]): The upload error message.
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

    def update(self, json: Dict[str, Any]) -> None:
        """
        Updates the model with values from a JSON dictionary.
        
        Args:
            json: Dictionary containing upload result data from Zenodo API.
        """
        if "id" in json:
            self.id = json["id"]
        if "doi" in json:
            self.doi = json["doi"]
        if "recid" in json:
            self.recid = json["recid"]
        if "created" in json:
            self.created = json["created"]
        if "modified" in json:
            self.modified = json["modified"]
        if "updated" in json:
            self.updated = json["updated"]
        if "status" in json:
            self.status = json["status"]
        if "state" in json:
            self.state = json["state"]  # ✅ FIXED: Was incorrectly setting self.created
        if "submitted" in json:
            self.submitted = json["submitted"]
        if "owners" in json:
            self.owners = str(json["owners"])
        if "links" in json and "self_html" in json["links"]:
            self.link = json["links"]["self_html"]


class UploadedFilesBlock(Block):
    """
    Custom Prefect block to store paths of successfully uploaded files.
    
    This block is used to track which files have already been uploaded to Zenodo
    so that they can be skipped in subsequent upload runs. This prevents
    accidental duplicate uploads.

    Attributes:
        uploaded_files (List[str]): A list of local file paths that have been
            successfully uploaded to Zenodo.
    
    Example:
        >>> # Load or create the block
        >>> block = await UploadedFilesBlock.load("uploaded-files")
        >>> 
        >>> # Check if a file has been uploaded
        >>> if "/path/to/file.zip" in block.uploaded_files:
        ...     print("Already uploaded, skipping")
        >>> 
        >>> # Add a newly uploaded file
        >>> block.uploaded_files.append("/path/to/new_file.zip")
        >>> await block.save(overwrite=True)
    """

    uploaded_files: List[str] = []
