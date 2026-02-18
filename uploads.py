"""Creates deployment for uploading datasets to Zenodo."""

# pylint: disable=invalid-name

import json
from typing import Dict, Any, Tuple
from pathlib import Path
from flows import upload_datasets


def get_dataset_configs(config: Dict[str, Any]) -> Tuple[str, str]:
    """
    Parses a dictionary for the `dataset_dir` and `collectors_csv` key values.

    Args:
        config (Dict[str, Any]): A config dictionary.

    Returns:
        Tuple[str, str]: The dataset directory and collectors CSV file path.
    """

    if "dataset_dir" not in config or not config["dataset_dir"]:
        dataset_dir = ""
    else:
        dataset_dir = config["dataset_dir"]

    if "collectors_csv" not in config or not config["collectors_csv"]:
        collectors_csv = ""
    else:
        collectors_csv = config["collectors_csv"]

    return (dataset_dir, collectors_csv)


if __name__ == "__main__":
    current_dir = Path(__file__).parent
    config_path = current_dir / "config.json"

    with open(config_path, "r", encoding="utf-8") as file:
        config_data = json.load(file)

        if "uploads" not in config_data:
            raise ValueError(
                "Running deployment without a valid configuration for uploads"
            )

        uploads_config: Dict[str, Any] = config_data["uploads"]

        annular_dir = ""
        annular_csv = ""

        total_dir = ""
        total_csv = ""

        if "annular" in uploads_config:
            annular_dir, annular_csv = get_dataset_configs(uploads_config["annular"])

        if "total" in uploads_config:
            total_dir, total_csv = get_dataset_configs(uploads_config["total"])

        upload_datasets.serve(
            name="upload-datasets-deployment",
            parameters={
                # the directory containing the annular eclipse datasets (zipped files)
                "annular_dir": annular_dir,
                # the CSV file containing the data collectors information for the annular eclipse data
                "annular_data_collector_csv": annular_csv,
                # the directory containing the total eclipse datasets (zipped files)
                "total_dir": total_dir,
                # the CSV file containing the data collectors information for the total eclipse data
                "total_data_collector_csv": total_csv,
                # a CSV file to save successful results (will be created if does not exist)
                "successful_results_file": uploads_config.get(
                    "successful_results_file", None
                ),
                # a CSV file to save failed results (will be created if does not exist)
                "failure_results_file": uploads_config.get(
                    "failure_results_file", None
                ),
                # option to automatically delete any failed uploads
                "delete_failures": uploads_config.get("delete_failures", False),
                # option to automatically publish a successful upload
                "auto_publish": uploads_config.get("auto_publish", False),
            },
        )
