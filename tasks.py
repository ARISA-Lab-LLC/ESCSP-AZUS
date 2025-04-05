"""Tasks to publish local AudioMoth data to Zenodo."""

# pylint: disable=line-too-long
import os
import json
import glob
import csv
import zipfile
import tempfile
from pathlib import Path
from typing import List, Tuple, Optional, Final, Dict, Any
from datetime import datetime
from string import Template

from prefect import task, get_run_logger
from prefect_invenio_rdm.models.records import DraftConfig, Access

from models.invenio import (
    Metadata,
    Identifier,
    PersonOrganization,
    Affiliation,
    Role,
    Creator,
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
)

from models.audiomoth import (
    EclipseType,
    DataCollector,
    UploadData,
    PersistedResult,
    UploadedFilesBlock,
)

UPLOADED_FILES_BLOCK: Final[str] = "uploaded-files"
UPLOAD_DATE_FORMAT: Final[str] = "%Y-%m-%d"


@task
async def get_files_block() -> UploadedFilesBlock:
    """
    Retrieves a Prefect block tracking files that have been
    successfully uploaded.

    Note:
        If the block does not exist, a new one will be created.

    Returns:
        Returns an instance of UploadedFilesBlock.
    """
    logger = get_run_logger()

    try:
        block = await UploadedFilesBlock.load(UPLOADED_FILES_BLOCK)
        logger.info("Loaded existing block: %s", UPLOADED_FILES_BLOCK)
    except ValueError:
        block = UploadedFilesBlock(uploaded_files=[])
        await block.save(UPLOADED_FILES_BLOCK)
        logger.info("Created new block: %s", UPLOADED_FILES_BLOCK)

    return block


@task
async def save_result_csv(file: str, result: PersistedResult) -> None:
    """
    Saves an upload result to a local CSV file.

    Args:
        file (str): The CSV file to add the result.
        result (PersistedResult): The upload result.

    Returns:
        None
    """

    logger = get_run_logger()

    if not file:
        raise ValueError("Invalid file")

    output_file = Path(file)
    new_file = False

    if not output_file.exists():
        logger.info("Creating CSV file %s", file)
        new_file = True
        output_file.parent.mkdir(exist_ok=True, parents=True)

    result_dict = result.model_dump()
    headers = result_dict.keys()

    with open(file, mode="a", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=headers)
        if new_file:
            writer.writeheader()
        writer.writerow(result_dict)


@task
async def get_recording_dates(zip_file: str) -> Tuple[str, str]:
    """
    Retrieves the start and end date of a dataset recording.

    Args:
        zip_file (str): A zipped dataset file.

    Returns:
        Tuple[str, str]: A tuple containing the start and end date.
    """

    if not zip_file:
        raise ValueError("Invalid file")

    if not os.path.exists(zip_file):
        raise ValueError(f"File does not exist: {zip_file}")

    logger = get_run_logger()

    logger.debug("Extracting files...")

    # 1. Create a temporary directory for extraction
    with tempfile.TemporaryDirectory() as temp_dir:
        with zipfile.ZipFile(zip_file, "r") as zip_ref:
            zip_ref.extractall(temp_dir)

        # 2. List all files inside the unzipped folder
        extracted_files = zip_ref.namelist()

        # 3. Filter the list for files with a .WAV extension
        wav_files = [
            Path(file).stem for file in extracted_files if file.lower().endswith(".wav")
        ]

        # 4. Parse the file names and extract dates
        dates: List[datetime.date] = []
        for file in wav_files:
            try:
                # Extract date from the filename (YYYYMMDD_HMS format)
                date_str = os.path.splitext(file)[0].split("_")[0]
                date = datetime.strptime(date_str, "%Y%m%d").date()
                if date.year < 2023:
                    # Do not consider dates of recordings before the year 2023
                    continue
                dates.append(date)
            except ValueError:
                # Ignore files that don't match the expected format
                continue

        if not dates:
            raise ValueError("No valid dates found in the .WAV file names.")

        # 5. Find the earliest and latest date (only considering YYYYMMDD)
        earliest_date = min(dates)
        latest_date = max(dates)

        # 6. Return the dates as a tuple
        dates = (
            earliest_date.strftime(UPLOAD_DATE_FORMAT),
            latest_date.strftime(UPLOAD_DATE_FORMAT),
        )

        logger.debug(dates)
        return dates


@task
async def rename_dir_files(directory: str) -> None:
    """
    Renames all files in the specified directory (including nested ones)
    and replaces all '#' character occurences with '_'.

    Args:
        directory (str): A local directory.

    Returns:
        None.
    """

    if not directory:
        raise ValueError("Invalid directory")

    if not os.path.isdir(directory):
        raise ValueError(f"Invalid directory: {directory}")

    for root, _, files in os.walk(directory):
        for file in files:
            if file.startswith("ESID#") and file.endswith("zip"):
                old_path = os.path.join(root, file)
                # Replace only the first occurrence of "#" with "_"
                new_file_name = file.replace("#", "_")
                new_path = os.path.join(root, new_file_name)
                os.rename(old_path, new_path)


@task
async def list_dir_files(
    directory: str, file_pattern: Optional[str] = "*"
) -> List[str]:
    """
    Retrieve all files in a directory matching the given pattern.

    Args:
        directory (str): A local directory.
        file_pattern (Optional[str]): A glob pattern to match file names against.

    Returns:
        List[str]: A list of matching files.
    """

    if not directory:
        raise ValueError("A valid directory path must be provided")

    if not os.path.isdir(directory):
        raise ValueError(f"Invalid directory: {directory}")

    logger = get_run_logger()

    logger.info(
        "Retrieving all matching files in dir: %s with pattern: %s",
        directory,
        file_pattern,
    )

    search_pattern = os.path.join(directory, file_pattern)
    matching_files = glob.glob(search_pattern, recursive=False)

    # Filter out directories, keeping only files
    files = [file for file in matching_files if os.path.isfile(file)]

    logger.info("Matching files:\n%s", files)

    return files


@task
async def get_esid_file_pairs(files: List[str]) -> List[Tuple[str, str]]:
    """
    Retrieve ES ID from file names and create a pair of ES IDs and their associated file.

    Args:
        files (str): A list of file paths.

    Returns:
        List[Tuple[str, str]]: A list of (ES ID, file) pair.
    """

    logger = get_run_logger()

    pairs = [(Path(file).stem.split("_")[-1].strip(), file) for file in files]

    logger.info("ES ID file pairs: %s", pairs)

    return pairs


@task
async def create_upload_data(
    esid_file_pairs: List[Tuple[str, str]],
    data_collectors: List[DataCollector],
) -> List[UploadData]:
    """
    Combines the ES IDs, data files and data collectors into a list of
    organized data for upload.

    Args:
        esid_file_pairs (List[Tuple[str, str]]): A list of (ES ID, file) pairs.
        data_collectors (List[DataCollector]): A list of data collectors.

    Raises:
        ValueError: If there is no matching `DataCollector` for one or more
            (ES ID, file) pair.

    Returns:
        List[UploadData]: A list of data for upload.
    """

    if len(esid_file_pairs) > len(data_collectors):
        raise ValueError(
            f"The number of ES IDs and data files({len(esid_file_pairs)}) do not match the "
            f"number of data collectors found({len(data_collectors)}). "
        )

    upload_data = []
    unmatched_ids = []

    for esid, file in esid_file_pairs:
        data_collector = next(
            (
                data_collector
                for data_collector in data_collectors
                if data_collector.esid == esid
            ),
            None,
        )

        if not data_collector:
            unmatched_ids.append(esid)
        else:
            upload_data.append(
                UploadData(esid=esid, data_collector=data_collector, file=file)
            )

    if unmatched_ids:
        raise ValueError(
            f"Unable to find data collector info for the following ES IDs: {unmatched_ids}"
        )

    return upload_data


@task
async def parse_collectors_csv(
    csv_file_path: str, eclipse_type: EclipseType
) -> List[DataCollector]:
    """
    Parses a CSV file for all data collectors.

    Args:
        csv_file_path (str): A CSV file.
        eclipse_type (str): The type of eclipse for the data collected.
    Raises:
        FileNotFoundError: If the CSV file does not exist.

    Returns:
        List[DataCollector]: A list of data collecters.
    """

    logger = get_run_logger()

    with open(csv_file_path, mode="r", encoding="utf-8") as csv_file:
        csv_reader = csv.DictReader(csv_file)

        # Validate headers
        csv_headers = csv_reader.fieldnames
        if not csv_headers:
            raise ValueError("No headers found in the CSV file.")

        expected_headers = [
            "ESID",
            "Data Collector Affiliations",
            "Latitude",
            "Longitude",
            "Type of Eclipse",
            "Eclipse %",
            "WAV Files Time & Date Settings",
            "Eclipse Date",
            "Eclipse Start Time (UTC)",
            "Eclipse Maximum (UTC)",
            "Eclipse End Time (UTC)",
            "Version",
            "Keywords and subjects",
        ]

        if eclipse_type == EclipseType.TOTAL:
            expected_headers.extend(
                ["Totality Start Time (UTC)", "Totality End Time (UTC)"]
            )

        if len((set(expected_headers) - set(csv_headers))) != 0:
            raise ValueError(
                f"Expected CSV headers not found: {set(expected_headers) - set(csv_headers)}"
            )

        data = [DataCollector.model_validate(row) for row in csv_reader]

        logger.info("Parsed %d rows from the CSV", len(data))

        return data


@task
async def get_draft_config(data_collector: DataCollector) -> DraftConfig:
    """
    Create a draft record configuration from the data collector info.

    Args:
        data_collector (DataCollector): A data collector.

    Returns:
        DraftConfig: A draft record configuration.
    """

    dates: Optional[List[Date]] = []
    if data_collector.first_recording_day:
        dates.append(
            Date(
                date=data_collector.first_recording_day,
                type=DateType(id="collected"),
                description="Day of first recording",
            )
        )
    if data_collector.last_recording_day:
        dates.append(
            Date(
                date=data_collector.last_recording_day,
                type=DateType(id="collected"),
                description="Day of last recording",
            )
        )

    creators = get_default_creators()

    affiliations = [
        Affiliation(name=affiliation)
        for affiliation in parse_values_from_str(data_collector.affiliation)
    ]

    creators.append(
        Creator(
            person_or_org=PersonOrganization(
                type="personal", given_name="Volunteer", family_name="Scientist"
            ),
            role=Role(id="datacollector"),
            affiliations=affiliations,
        )
    )

    subjects = [
        Subject(subject=subject)
        for subject in parse_values_from_str(data_collector.subjects)
    ]

    metadata = Metadata(
        resource_type=ResourceType(id="dataset"),
        title=f"{data_collector.eclipse_date} {data_collector.eclipse_label()} ESID#{data_collector.esid}",
        publication_date=datetime.now().strftime(UPLOAD_DATE_FORMAT),
        creators=creators,
        description=get_description(data_collector=data_collector),
        funding=get_fundings(),
        rights=[License(id="cc-by-4.0")],
        languages=[Language(id="eng")],
        dates=dates,
        version=data_collector.version,
        publisher="Zenodo",
        subjects=subjects,
    )

    return DraftConfig(
        record_access=Access.PUBLIC,
        files_access=Access.PUBLIC,
        files_enabled=True,
        metadata=metadata.to_dict(),
        community_id="2ca990ba-e151-4741-a456-6b80da71c69d",
        custom_fields={
            "code:codeRepository": "https://github.com/ARISA-Lab-LLC/ESCSP",
            "code:developmentStatus": {"id": "wip"},
            "code:programmingLanguage": [{"id": "python"}],
            "ac:captureDevice": ["AudioMoth v.1.1.0, Firmware 1.8"],
        },
        pids={},
    )


def parse_values_from_str(string: str, delimeter: str = ":") -> List[str]:
    """
    Parses values from a string that are separated by a delimiter.

    Args:
        string (str): The string to split.
        delimeter (str): The delimiter used to split the string.

    Returns:
        List[str]: A list of strings.
    """
    values = string.split(sep=delimeter)
    return [x.strip() for x in values]


def get_description(data_collector: DataCollector) -> str:
    """
    Creates a description for a draft record.

    Args:
        data_collector (DataCollector): Data collector info.

    Returns:
        str: A description.
    """
    eclipse_dt = datetime.strptime(data_collector.eclipse_date, UPLOAD_DATE_FORMAT)

    annular_location_info = Template(
        """
        <li>Eclipse Date: $eclipse_location_date</li>
        <li>Eclipse Start Time (UTC): $start_time</li>
        <li>Eclipse Maximum [when the most possible amount of the Sun in blocked] (UTC): $max_time (This is Annularity if on eclipse path)</li>
        <li>Eclipse End Time (UTC): [N/A if partial eclipse] $end_time</li>
        """
    ).substitute(
        eclipse_location_date=eclipse_dt.strftime("%m/%d/%Y"),
        start_time=data_collector.eclipse_start_time_utc,
        max_time=data_collector.eclipse_maximum_time_utc,
        end_time=data_collector.eclipse_end_time_utc,
    )

    total_location_info = Template(
        """
        <li>Eclipse Date: $eclipse_location_date</li>
        <li>Eclipse Start Time (UTC): $start_time</li>
        <li>Totality Start Time (UTC): [N/A if partial eclipse] $totality_start_time</li>
        <li>Eclipse Maximum [when the most possible amount of the Sun in blocked] (UTC): $max_time</li>
        <li>Totality End Time (UTC): [N/A if partial eclipse] $totality_end_time</li>
        <li>Eclipse End Time (UTC): [N/A if partial eclipse] $end_time</li>
        """
    ).substitute(
        eclipse_location_date=eclipse_dt.strftime("%m/%d/%Y"),
        start_time=data_collector.eclipse_start_time_utc,
        totality_start_time=data_collector.eclipse_totality_start_time_utc,
        max_time=data_collector.eclipse_maximum_time_utc,
        totality_end_time=data_collector.eclipse_totality_end_time_utc,
        end_time=data_collector.eclipse_end_time_utc,
    )

    location_info = (
        total_location_info
        if data_collector.eclipse_type == EclipseType.TOTAL
        else annular_location_info
    )

    return Template(
        """
        <p>These are audio recordings taken by an Eclipse Soundscapes (ES) Data Collector during the week of the $date $eclipse_label.&nbsp;</p>
        <p><strong>Data Site location information:</strong></p>
        <ul>
        <li>Latitude: $latitude&nbsp;</li>
        <li>Longitude:&nbsp; $longitude</li>
        <li>Type of Eclipse: $eclipse_label</li>
        <li>Eclipse %: $coverage</li>
        <li>WAV files Time &amp; Date Settings: $time_date_mode</li>
        </ul>
        <p><strong>Included Data:</strong></p>
        <ul>
        <li>
        <p><strong>Audio files in WAV format</strong>&nbsp;with the date and time in UTC within the file name: YYYYMMDD_HHMMSS meaning YearMonthDay_HourMinuteSecond<br>For example, 20240411_141600.WAV means that this audio file starts on April 11, 2024 at 14:16:00 Coordinated Universal Time (UTC)</p>
        </li>
        <li>
        <p><strong>CONFIG Text file:&nbsp;</strong>Includes AudioMoth device setting information, such as sample rate in Hertz (Hz), gain, firmware, etc.&nbsp;</p>
        </li>
        </ul>
        <p><strong>Eclipse Information for this location:</strong></p>
        <ul>
        $location_info
        </ul>
        <p><strong>Audio Data Collection During Eclipse Week&nbsp;</strong></p>
        <p>ES Data Collectors used AudioMoth devices to record audio data, known as soundscapes, over a 5-day period during the eclipse week: 2 days before the eclipse, the day of the eclipse, and 2 days after. The complete raw audio data collected by the Data Collector at the location mentioned above is provided here. This data may or may not cover the entire requested timeframe due to factors such as availability, technical issues, or other unforeseen circumstances.&nbsp;</p>
        <p><strong>ES ID# Information:</strong></p>
        <p>Each AudioMoth recording device was assigned a unique Eclipse Soundscapes Identification Number (ES ID#). This identifier connects the audio data, submitted via a MicroSD card, with the latitude and longitude information provided by the data collector through an online form. The ES team used the ES ID# to link the audio data with its corresponding location information and then uploaded this raw audio data and location details to Zenodo. This process ensures the anonymity of the ES Data Collectors while allowing them to easily search for and access their audio data on Zenodo.&nbsp;</p>
        <p><strong>TimeStamp Information:</strong></p>
        <p>The ES team and the Data Collectors took care to set the date and time on the AudioMoth recording devices using an AudioMoth time chime before deployment, ensuring that the recordings would have an automatic timestamp. However, participants also manually noted the date and start time as a backup in case the time chime setup failed. The notes above indicate whether the WAV audio files for this site were timestamped manually or with the automated AudioMoth time chime.&nbsp;</p>
        <p><strong>Common Timestamp Error:</strong></p>
        <p>Some AudioMoth devices experienced a malfunction where the timestamp on audio files reverted to a date in 1970 or before, even after initially recording correctly. Despite this issue, the affected data was still included in this ES site's collected raw audio dataset.</p>
        <p><strong>Latitude &amp; Longitude Information:</strong></p>
        <p>The latitude and longitude for each site was taken manually by data collectors and submitted to the ES team, either via a web form or on paper. It is shared in Decimal Degrees format.</p>
        <p><strong>General Project Information:</strong></p>
        <p>The Eclipse Soundscapes Project is a NASA Volunteer Science project funded by NASA Science Activation that is studying how eclipses affect life on Earth during the October 14, 2023 annular solar eclipse and the April 8, 2024 total solar eclipse. Eclipse Soundscapes revisits an eclipse study from almost 100 years ago that showed that animals and insects are affected by solar eclipses! Like this study from 100 years ago, ES asked for the public’s help. ES uses modern technology to continue to study how solar eclipses affect life on Earth! You can learn more at www.EclipseSoundscapes.org.&nbsp;</p>
        <p>Eclipse Soundscapes is an enterprise of ARISA Lab, LLC and is supported by NASA award No. 80NSSC21M0008.&nbsp;</p>
        <p><strong>Eclipse Data Version Definitions</strong></p>
        <p>{1st digit = year, 2nd digit = Eclipse type (1=Total Solar Eclipse, 9=Annular Solar Eclipse, 0=Partial Solar Eclipse), 3rd digit is unused and in place for future use}</p>
        <p><strong>2023.9.0&nbsp;</strong>= Week of October 14, 2023 Annular Eclipse Audio Data, Path of Annularity (Annular Eclipse)</p>
        <p><strong>2023.0.0&nbsp;</strong>= Week of October 14, 2023 Annular Eclipse Audio Data, OFF the Path of Annularity (Partial Eclipse)</p>
        <p><strong>2024.1.0</strong>&nbsp;= Week of April 8, 2024 Total Solar Eclipse Audio Data, Path of Totality (Total Solar Eclipse)</p>
        <p><strong>2024.0.0</strong>&nbsp;=&nbsp; Week of April 8, 2024 Total Solar Eclipse Audio Data , OFF the Path of Totality (Partial Solar Eclipse)</p>
        <p><em>*Please note that this dataset's version number is listed below.</em></p>
        <p><strong>Individual Site Citation</strong></p>
        <p><strong>Eclipse Soundscapes Team, ARISA Lab</strong>. (2025). $year Solar Eclipse Soundscapes Audio Data [Audio Dataset, ES ID# $esid]. Zenodo. <strong>{Insert DOI here}</strong></p>
        <p><strong>Collected by</strong>: Volunteer scientists as part of the Eclipse Soundscapes Project</p>
        <p><strong>Funding</strong>: The Eclipse Soundscapes Project is supported by NASA award No. 80NSSC21M0008.</p>
        <p><strong>Eclipse Community Citation</strong></p>
        <p><strong>Eclipse Soundscapes Team, ARISA Lab. (2025)</strong>. 2023 and 2024 Solar Eclipse Soundscapes Audio Data [Collection of Audio Datasets]. <strong>Eclipse Soundscapes Community, Zenodo</strong>. Retrieved from <a href="https://zenodo.org/communities/eclipsesoundscapes/">https://zenodo.org/communities/eclipsesoundscapes/</a></p>
        <p><strong>Collected by</strong>: Volunteer scientists as part of the Eclipse Soundscapes Project</p>
        <p><strong>Funding</strong>: The Eclipse Soundscapes Project is supported by NASA award No. 80NSSC21M0008.</p>
        <p>&nbsp;</p><p></p>
        """
    ).substitute(
        date=eclipse_dt.strftime("%B %d, %Y"),
        eclipse_label=data_collector.eclipse_label(),
        latitude=data_collector.latitude,
        longitude=data_collector.longitude,
        coverage=data_collector.eclipse_coverage,
        time_date_mode=data_collector.files_date_time_mode,
        location_info=location_info,
        year=eclipse_dt.year,
        esid=data_collector.esid,
    )


def get_fundings() -> List[Funding]:
    """
    Retrieves a list of project fundings.

    Returns:
        List[Funding]: A list of fundings.
    """
    return [
        Funding(
            funder=Funder(id="027ka1x80"),
            award=Award(
                title=AwardTitle(en="Eclipse Soundscapes: Citizen Science Project"),
                number="80NSSC21M0008",
                identifiers=[
                    Identifier(
                        scheme="url",
                        identifier="https://science.nasa.gov/sciact-team/eclipse-soundscapes/",
                    )
                ],
            ),
        )
    ]


def get_default_creators() -> List[Creator]:
    """
    Retrieves the default list of creators.

    Returns:
        List[Creator]: A list of creators.
    """
    return [
        Creator(
            person_or_org=PersonOrganization(
                type="organizational", name="ARISA Lab, L.L.C."
            ),
            role=Role(id="hostinginstitution"),
            affiliations=[
                Affiliation(name="National Aeronautics and Space Administration (NASA)")
            ],
        ),
        Creator(
            person_or_org=PersonOrganization(
                type="personal",
                given_name="Henry",
                family_name="Winter",
                identifiers=[
                    Identifier(scheme="orcid", identifier="0000-0002-6678-590X")
                ],
            ),
            role=Role(id="researcher"),
            affiliations=[Affiliation(name="ARISA Lab, L.L.C.")],
        ),
        Creator(
            person_or_org=PersonOrganization(
                type="personal",
                given_name="MaryKay",
                family_name="Severino",
                identifiers=[
                    Identifier(scheme="orcid", identifier="0000-0002-2363-7421")
                ],
            ),
            role=Role(id="projectleader"),
            affiliations=[Affiliation(name="ARISA Lab, L.L.C.")],
        ),
    ]


@task
async def parse_request_ids_from_response(response: Dict[str, Any]) -> List[str]:
    """
    Parses request IDs from an API response containing a page of user requests.

    Args:
        response (Dict[str, Any]): The API response.

    Returns:
        List[str]: A list of request IDs.
    """
    logger = get_run_logger()

    if "hits" not in response or "hits" not in response["hits"]:
        return []

    if "total" in response["hits"]:
        logger.info("Response contains %d requests", response["hits"]["total"])

    hits: Dict[str, Any] = response["hits"]["hits"]
    ids = []

    for request_json in hits:
        if "id" in request_json:
            ids.append(request_json["id"])

    logger.debug("Parsed request IDs: %s", ids)
    return ids


@task
async def parse_published_records_from_response(
    response: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Parses published records from an API response containing page of records.

    Args:
        response (Dict[str, Any]): The API response.

    Returns:
        List[str]: A list of records that have been published.
    """
    logger = get_run_logger()

    if "hits" not in response or "hits" not in response["hits"]:
        return []

    if "total" in response["hits"]:
        logger.info("Response contains %d records", response["hits"]["total"])

    hits: Dict[str, Any] = response["hits"]["hits"]
    records = []

    for request_json in hits:
        if "status" in request_json and request_json["status"] == "published":
            records.append(request_json)

    return records


@task
async def parse_values_from_record(
    values: List[str], records: List[Dict[str, Any]]
) -> None:
    """
    Parses the specified values from each record and returns an updated list
    of records.

    Args:
        values (List[str]): A list of values to parse from each record.
        records (List[Dict[str, Any]]): A list of records.

    Returns:
        List[Dict[str, Any]]: A list of records with only the specified values.
    """

    if not values:
        return records

    if not records:
        return records

    updated_records = []

    for record in records:
        updated_record = {}
        for value in values:
            if value in record:
                updated_record[value] = record[value]

        updated_records.append(updated_record)

    return updated_records


@task
async def save_dicts_to_csv(
    headers: List[str], records: List[Dict[str, Any]], file_path: Path
) -> None:
    """
    Saves a list of dictionaries to a CSV file in the specified file path.
    The file name will be of the form 'records_{timestamp}.csv'.

    Args:
        headers (List[str]): The CSV headers.
        records (List[Dict[str, Any]]): The list of dictionaries to save.
        file_path (Path): The path to create the CSV file.

    Returns:
        str: The full path to the created CSV file.
    """

    logger = get_run_logger()

    new_file = False
    if not file_path.exists():
        logger.info("Creating CSV file %s", file_path)
        new_file = True
        file_path.parent.mkdir(exist_ok=True, parents=True)

    with open(file_path, mode="a", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=headers)

        if new_file:
            writer.writeheader()

        for record in records:
            writer.writerow(record)
