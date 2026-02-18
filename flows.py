"""Flows to publish local AudioMoth data to Zenodo."""

from typing import List, Tuple, Optional, Dict, Any
from pathlib import Path
from datetime import datetime

from prefect import flow, get_run_logger

from prefect_invenio_rdm.flows import get_credentials, create_record_files
from prefect_invenio_rdm.tasks import (
    search_user_records,
    search_user_requests,
    accept_request,
)
from prefect_invenio_rdm.models.api import APIResult
from prefect_invenio_rdm.models.records import DraftConfig

from models.audiomoth import (
    EclipseType,
    DataCollector,
    UploadData,
    PersistedResult,
    UploadedFilesBlock,
)

from tasks import (
    list_dir_files,
    get_esid_file_pairs,
    get_draft_config,
    parse_collectors_csv,
    create_upload_data,
    save_result_csv,
    get_files_block,
    rename_dir_files,
    get_recording_dates,
    parse_request_ids_from_response,
    parse_published_records_from_response,
    parse_values_from_record,
    save_dicts_to_csv,
)


@flow(name="upload-datasets")
async def upload_datasets(
    successful_results_file: str,
    failure_results_file: str,
    annular_dir: Optional[str] = None,
    total_dir: Optional[str] = None,
    annular_data_collector_csv: Optional[str] = None,
    total_data_collector_csv: Optional[str] = None,
    auto_publish: bool = False,
    delete_failures: bool = False,
) -> None:
    """
    Uploads local AudioMoth data to Zenodo.

    Args:
        successful_results_file (str): The path to a CSV file to save successful
            upload results. If file does not exist, one will be created.
        failure_results_file (str): The path to a CSV file to save failed
            upload results. If file does not exist, one will be created.
        annular_dir (Optional[str]): The directory containing the annular eclipse data.
        total_dir (Optional[str]): The directory containing the total eclipse data.
        annular_data_collector_csv (Optional[str]): A CSV file containing info about the
            data collectors for the annular eclipse.
        total_data_collector_csv (Optional[str]): A CSV file containing info about the
            data collectors for the annular eclipse.
        auto_publish (bool): If `True`, the created record will be automatically published if
            no failures have occured. Defaults to `False`.
        delete_failures (bool): If `True`, will delete a created record if there is an
            error. Defaults to `False`.
    Returns:
        None
    """
    if not annular_dir and not total_dir:
        raise ValueError(
            "Missing directories for the annular and/or total eclipse data"
        )

    if not annular_data_collector_csv and not total_data_collector_csv:
        raise ValueError("Missing data collector files")

    if annular_dir and not annular_data_collector_csv:
        raise ValueError("Missing data collector file for the annular eclipse data")

    if total_dir and not total_data_collector_csv:
        raise ValueError("Missing data collector file for the total eclipse data")

    logger = get_run_logger()

    if annular_dir:
        await rename_dir_files(directory=annular_dir)

        annular_upload_data = await get_upload_data(
            data_dir=annular_dir,
            data_collectors_file=annular_data_collector_csv,
            eclipse_type=EclipseType.ANNULAR,
            failure_results_file=failure_results_file,
        )

        if not annular_upload_data:
            logger.info("No annular eclipse data found")

        # Upload annular eclipse data
        for data in annular_upload_data:
            result = await upload_dataset(
                data=data, delete_failures=delete_failures, auto_publish=auto_publish
            )
            await save_result(
                esid=data.esid,
                files=data.all_files,  # Pass all files instead of single file
                result=result,
                success_file=successful_results_file,
                failure_file=failure_results_file,
            )

    if total_dir:
        await rename_dir_files(directory=total_dir)

        total_upload_data = await get_upload_data(
            data_dir=total_dir,
            data_collectors_file=total_data_collector_csv,
            eclipse_type=EclipseType.TOTAL,
            failure_results_file=failure_results_file,
        )

        if not total_upload_data:
            logger.info("No total eclipse data found")

        # Upload total eclipse data
        for data in total_upload_data:
            result = await upload_dataset(
                data=data, delete_failures=delete_failures, auto_publish=auto_publish
            )
            await save_result(
                esid=data.esid,
                files=data.all_files,  # Pass all files instead of single file
                result=result,
                success_file=successful_results_file,
                failure_file=failure_results_file,
            )


@flow
async def save_result(
    esid: str, 
    files: List[str], 
    result: APIResult, 
    success_file: str, 
    failure_file: str
) -> None:
    """
    Saves the result of an upload.
    
    This function has been updated to track all uploaded files instead of just
    the main ZIP file.

    Args:
        esid (str): A unique AudioMoth ID.
        files (List[str]): List of all uploaded files.
        result (APIResult): The upload result.
        success_file (str): A CSV file to save successful results.
        failure_file (str): A CSV file to save failed results.

    Returns:
        None.
        
    Example:
        >>> await save_result(
        ...     esid="004",
        ...     files=data.all_files,
        ...     result=upload_result,
        ...     success_file="success.csv",
        ...     failure_file="failed.csv"
        ... )
    """

    if not esid:
        raise ValueError("Invalid ESID")

    if not files or len(files) == 0:
        raise ValueError("Invalid files list - must contain at least one file")

    results_file = success_file if result.successful else failure_file
    persisted_result = PersistedResult(esid=esid)

    if not result.successful:
        persisted_result.error_type = result.error.type
        persisted_result.error_message = result.error.error_message

    if not result.api_response:
        await save_result_csv(
            file=results_file,
            result=persisted_result,
        )
    else:
        persisted_result.update(result.api_response)
        await save_result_csv(file=results_file, result=persisted_result)

    if result.successful:
        # Track all successfully uploaded files
        files_block: UploadedFilesBlock = await get_files_block()
        uploaded_files = files_block.uploaded_files
        uploaded_files.extend(files)  # Add all files instead of just one
        await files_block.save(overwrite=True)


@flow
async def get_upload_data(
    data_dir: str,
    data_collectors_file: str,
    eclipse_type: EclipseType,
    failure_results_file: str,
) -> List[UploadData]:
    """
    Retrieves all the datasets to upload in the given directory.

    Args:
        data_dir (str): A directory containing the AudioMoth datasets.
        data_collectors_file (str): A CSV file containing info about the
            data collectors for each dataset.
        eclipse_type (EclipseType): The type of eclipse for the data collected.
        failure_results_file (str): The path to a CSV file to save upload
            data without a matching collector entry.
    Returns:
        List[UploadData]: A list of data to be uploaded.
    """

    if not data_dir:
        raise ValueError("Missing data directory")

    if not data_collectors_file:
        raise ValueError("Missing data collectors file")

    data_collectors: List[DataCollector] = await parse_collectors_csv(
        csv_file_path=data_collectors_file, eclipse_type=eclipse_type
    )

    dir_files: List[str] = await list_dir_files(
        directory=data_dir, file_pattern="*.zip"
    )

    # skip file if already uploaded
    files_block: UploadedFilesBlock = await get_files_block()
    uploaded_files = files_block.uploaded_files
    dir_files = [file for file in dir_files if file not in uploaded_files]

    # retrieve ESID from file name
    esid_file_pairs: List[Tuple[str, str]] = await get_esid_file_pairs(files=dir_files)

    upload_data, unmatched_ids = await create_upload_data(
        esid_file_pairs=esid_file_pairs, data_collectors=data_collectors
    )

    for esid in unmatched_ids:
        await save_result_csv(
            file=failure_results_file,
            result=PersistedResult(
                esid=esid, error_message="Unable to find data collector info"
            ),
        )

    return upload_data


@flow(flow_run_name="upload-dataset-esid-{data.esid}")
async def upload_dataset(
    data: UploadData,
    delete_failures: bool = False,
    auto_publish: bool = False,
) -> APIResult:
    """
    Uploads a dataset with all associated files to Zenodo.
    
    This function has been updated to support the new workflow where files are
    pre-generated. It now uploads the ZIP file along with all supporting files,
    and uses README.html content as the Zenodo description.

    Args:
        data (UploadData): The data to upload, including ZIP file, README files,
            and all additional supporting files.
        delete_failures (bool): If `True`, will delete a created record if there is an
            error. Defaults to `False`.
        auto_publish (bool): If `True`, the created record will be automatically published if
            no failures have occured. Defaults to `False`.
            
    Returns:
        APIResult: The upload result.
        
    Example:
        >>> result = await upload_dataset(
        ...     data=upload_data,
        ...     delete_failures=True,
        ...     auto_publish=False
        ... )
        >>> if result.successful:
        ...     print(f"Uploaded to: {result.api_response['links']['self_html']}")
    """
    logger = get_run_logger()

    # Retrieve first and last day of recording from ZIP file
    start_date, end_date = await get_recording_dates(zip_file=data.zip_file)

    # Update data collector with recording dates
    data.data_collector.first_recording_day = start_date
    data.data_collector.last_recording_day = end_date

    # Create config with README.html as description
    config: DraftConfig = await get_draft_config(
        data_collector=data.data_collector,
        readme_html_path=data.readme_html  # Use README.html for description
    )

    # Get all files to upload
    all_files = data.all_files
    
    logger.info(f"Uploading {len(all_files)} files for ESID {data.esid}:")
    for file_path in all_files:
        logger.info(f"  - {Path(file_path).name}")

    # Upload all files to Zenodo
    result: APIResult = await create_record_files(
        files=all_files,  # Upload all files instead of just ZIP
        config=config,
        delete_on_failure=delete_failures,
        auto_publish=auto_publish,
    )

    return result


@flow(flow_run_name="accept-publish-requests")
async def accept_publish_requests() -> None:
    """
    Retrieves all user requests and accepts them for publishing.

    Returns:
        None.
    """

    logger = get_run_logger()

    credentials = await get_credentials()

    responses = search_user_requests(
        credentials=credentials,
        page=1,
        sort="newest",
        size=10,
        additional_params={"is_open": True, "shared_with_me": False},
    )

    async for response in responses:
        request_ids = await parse_request_ids_from_response(response=response)
        for request_id in request_ids:
            logger.info("Accepting request ID: %s", request_id)
            await accept_request(credentials=credentials, request_id=request_id)


@flow(flow_run_name="get-published-records")
async def get_published_records(directory: str, size: int = 10) -> List[Dict[str, Any]]:
    """
    Retrieves all published user records.

    Args:
        directory (str): The directory in which to save the JSON file.
        size (int): The number of items to retrieve in each request.

    Returns:
        None.
    """

    logger = get_run_logger()

    if not directory:
        raise ValueError("Invalid directory")

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    file_name = f"records_{timestamp}.csv"
    file_path = Path(directory, file_name)

    csv_headers = {
        "id",
        "conceptrecid",
        "doi",
        "conceptdoi",
        "doi_url",
        "title",
        "recid",
        "status",
        "state",
        "submitted",
        "created",
        "modified",
        "updated",
    }

    credentials = await get_credentials()

    responses = search_user_records(
        credentials=credentials,
        page=1,
        sort="newest",
        size=size,
        additional_params={"shared_with_me": False},
    )

    logger.info("Saving records to path: %s", file_path)

    async for response in responses:
        published_records = await parse_published_records_from_response(
            response=response
        )

        updated_records = await parse_values_from_record(
            values=csv_headers, records=published_records
        )

        await save_dicts_to_csv(
            headers=csv_headers, records=updated_records, file_path=file_path
        )
