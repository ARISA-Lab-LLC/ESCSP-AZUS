"""Creates a README file for each dataset."""

from typing import List, Optional, Tuple

from prefect import flow
from prefect_invenio_rdm.models.records import DraftConfig

from models.audiomoth import DataCollector, EclipseType
from tasks import (
    add_markdown_to_zip,
    create_upload_data,
    get_draft_config,
    get_esid_file_pairs,
    get_recording_dates,
    html_to_markdown,
    list_dir_files,
    parse_collectors_csv,
)
from utils import get_dataset_configs


@flow
async def add_readme(
    annular_directory: Optional[str] = None,
    total_directory: Optional[str] = None,
    annular_data_collector_csv: Optional[str] = None,
    total_data_collector_csv: Optional[str] = None,
) -> None:
    """
    Adds a README.md file to all datasets in each directory.

    Args:
       annular_dir (Optional[str]): The directory containing the annular eclipse data.
        total_dir (Optional[str]): The directory containing the total eclipse data.
        annular_data_collector_csv (Optional[str]): A CSV file containing info about the
            data collectors for the annular eclipse.
        total_data_collector_csv (Optional[str]): A CSV file containing info about the
            data collectors for the annular eclipse.
    Returns:
        None
    """

    if not annular_directory and not total_directory:
        raise ValueError(
            "Missing directories for the annular and/or total eclipse data"
        )

    if not annular_data_collector_csv and not total_data_collector_csv:
        raise ValueError("Missing data collector files")

    if annular_directory and not annular_data_collector_csv:
        raise ValueError("Missing data collector file for the annular eclipse data")

    if total_directory and not total_data_collector_csv:
        raise ValueError("Missing data collector file for the total eclipse data")

    if annular_directory:
        await add_dataset_readme(
            data_dir=annular_directory,
            data_collectors_file=annular_data_collector_csv,
            eclipse_type=EclipseType.ANNULAR,
        )

    if total_directory:
        await add_dataset_readme(
            data_dir=total_directory,
            data_collectors_file=total_data_collector_csv,
            eclipse_type=EclipseType.TOTAL,
        )


@flow
async def add_dataset_readme(
    data_dir: str,
    data_collectors_file: str,
    eclipse_type: EclipseType,
) -> None:
    """
    Generates a README.md file and adds it to the zipped dataset file.

    Args:
        data_dir (str): A directory containing the AudioMoth datasets.
        data_collectors_file (str): A CSV file containing info about the
            data collectors for each dataset.
        eclipse_type (EclipseType): The type of eclipse for the data collected.
    Returns:
        None.
    """

    data_collectors: List[DataCollector] = await parse_collectors_csv(
        csv_file_path=data_collectors_file, eclipse_type=eclipse_type
    )

    # retrieve all datasets
    dir_files: List[str] = await list_dir_files(
        directory=data_dir, file_pattern="*.zip"
    )

    # associate each ESID with its dataset
    esid_file_pairs: List[Tuple[str, str]] = await get_esid_file_pairs(files=dir_files)

    # create upload data
    upload_data, _ = await create_upload_data(
        esid_file_pairs=esid_file_pairs, data_collectors=data_collectors
    )

    for data in upload_data:
        # retrieve first and last day of recording from files
        start_date, end_date = await get_recording_dates(zip_file=data.file)

        # update data collector
        data.data_collector.first_recording_day = start_date
        data.data_collector.last_recording_day = end_date

        config: DraftConfig = await get_draft_config(data_collector=data.data_collector)

        # add dataset description to zipped file as a markdown file
        md_description: str = await html_to_markdown(config.metadata["description"])
        await add_markdown_to_zip(
            markdown_str=md_description,
            zip_path_str=data.file,
            file_name="README.md",
        )


if __name__ == "__main__":
    annular_dir, annular_csv = get_dataset_configs(dataset_type="annular")
    total_dir, total_csv = get_dataset_configs(dataset_type="total")

    add_readme.serve(
        name="create-dataset-readme-deployment",
        parameters={
            "annular_directory": annular_dir,
            "annular_data_collector_csv": annular_csv,
            "total_directory": total_dir,
            "total_data_collector_csv": total_csv,
        },
    )
