# prefect-audiomoth

## Getting started

Requires an installation of Python 3.9+.

### Environment variables
To successfully authenticate with Zenodo, configure the following environment variables in 
the `set_env.sh` file:

```bash
export INVENIO_RDM_ACCESS_TOKEN=<access_token>
export INVENIO_RDM_BASE_URL=<base url>
```

### Local setup
Create a Python virtual environment and install the project dependencies:
```bash
python3 -m venv prefect-env
source prefect-env/bin/active
pip install -r requirements.txt
```

To configure concurrency for the API calls, create a global concurrency limit named `rate-limit:invenio-rdm-api`:

```bash
prefect gcl create rate-limit:invenio-rdm-api --limit 5 --slot-decay-per-second 1.0
```

Set environment variables:
```bash
source set_env.sh
```

Start the Prefect server:
```bash
prefect server start
```

Update the `upload_datasets` function call in `upload.py` with the appropriate arguments: 
```python
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
```
Start the upload:
```bash
python upload.py
```
