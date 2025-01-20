"""Executes flow to upload AudioMoth datasets."""

import asyncio

from flows import upload_datasets

if __name__ == "__main__":
    asyncio.run(
        upload_datasets(
            # the directory containing the annular eclipse datasets (zipped files)
            annular_dir="/home/joel/Desktop/zenodo/test/annular",
            # the CSV file containing the data collectors information for the annular eclipse data
            annular_data_collector_csv="/home/joel/Desktop/zenodo/2023_annular_info.csv",
            # the directory containing the total eclipse datasets (zipped files)
            total_dir="/home/joel/Desktop/zenodo/test/total",
            # the CSV file containing the data collectors information for the total eclipse data
            total_data_collector_csv="/home/joel/Desktop/zenodo/2024_total_info.csv",
            # a CSV file to save successful results (will be created if does not exist)
            successful_results_file="/home/joel/Desktop/zenodo/results/successul_results.csv",
            # a CSV file to save failed results (will be created if does not exist)
            failure_results_file="/home/joel/Desktop/zenodo/results/failed_results.csv",
            # option to automatically delete any failed uploads
            delete_failures=True,
            # option to automatically publish a successful upload
            auto_publish=False,
        )
    )
