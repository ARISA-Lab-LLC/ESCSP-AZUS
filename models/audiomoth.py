"""AudioMoth models."""

from typing import Optional, List
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
        validation_alias=AliasChoices("eclipse_type", "Type of Eclipse")
    )

    eclipse_coverage: str = Field(
        validation_alias=AliasChoices("eclipse_coverage", "Eclipse %")
    )

    eclipse_start_time_utc: str = Field(
        validation_alias=AliasChoices(
            "eclipse_start_time_utc", "Eclipse Start Time (UTC)"
        )
    )

    eclipse_totality_start_time_utc: Optional[str] = Field(
        validation_alias=AliasChoices(
            "eclipse_totality_start_time_utc", "Totality Start Time (UTC)"
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
            "eclipse_totality_end_time_utc", "Totality End Time (UTC)"
        ),
        default="N/A",
    )

    eclipse_end_time_utc: Optional[str] = Field(
        validation_alias=AliasChoices("eclipse_end_time_utc", "Eclipse End Time (UTC)"),
        default="N/A",
    )

    def eclipse_label(self) -> str:
        return (
            "Total Solar Eclipse"
            if self.eclipse_type == EclipseType.TOTAL
            else "Annular Solar Eclipse"
        )


class UploadData(BaseModel):
    """
    The necessary data to perform an upload.

    Attributes:
        esid (str): A unique AudioMoth ID.
        data_collector (DataCollector): A data collector.
        files (str): The data file.
    """

    esid: str
    data_collector: DataCollector
    file: str


class PersistedResult(BaseModel):
    """
    Data from an upload result to persist locally.

    Attributes:
        esid (Optional[str]): A unique AudioMoth ID.
        created (Optional[str]): The creation date.
        updated (Optional[str]): The updated date.
        state (Optional[str]): The state of the deposition (inprogress, done or error).
        submitted (Optional[bool]): True if the deposition has been published, False otherwise.
        link (Optional[str]): Link of the created or published deposition.
        error_type (Optional[str]): The upload error type.
        error_message (Optional[str]): The upload error message.
    """

    esid: Optional[str] = None
    created: Optional[str] = None
    updated: Optional[str] = None
    state: Optional[str] = None
    submitted: Optional[bool] = None
    link: Optional[str] = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None


class UploadedFilesBlock(Block):
    """
    Custom Prefect block to store paths of successfully uploaded files.

    Attributes:
        uploaded_files (List[str]): A list of local file paths.
    """

    uploaded_files: List[str] = []
