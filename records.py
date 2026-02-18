"""Creates deployment for retrieving all user published records on Zenodo."""

import json
from pathlib import Path
from flows import get_published_records

if __name__ == "__main__":

    current_dir = Path(__file__).parent
    config_path = current_dir / "config.json"

    with open(config_path, "r", encoding="utf-8") as file:
        config_data = json.load(file)

        if (
            "downloads" not in config_data
            or "results_dir" not in config_data["downloads"]
            or not config_data["downloads"]["results_dir"]
        ):
            raise ValueError(
                "Running deployment without a valid configuration for downloads"
            )

        get_published_records.serve(
            name="get-published-records-deployment",
            parameters={
                # specify the directory where retrieved records should be saved
                "directory": config_data["downloads"]["results_dir"]
            },
        )
