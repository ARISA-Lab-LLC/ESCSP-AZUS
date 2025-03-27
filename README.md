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
source prefect-env/bin/activate
pip install -r requirements.txt
```

Set the environment variables:
```bash
source set_env.sh
```

To configure concurrency for the API calls, create a global concurrency limit named `rate-limit:invenio-rdm-api`:
```bash
prefect gcl create rate-limit:invenio-rdm-api --limit 5 --slot-decay-per-second 1.0
```

For debugging, set the Prefect logging level to DEBUG:
```bash
prefect config set PREFECT_LOGGING_LEVEL="DEBUG"
```

Start the Prefect server:
```bash
prefect server start
```

Open the Prefect dashboard in your browser at http://localhost:4200.

&ast; Note that once the Prefect server has been started, keep the terminal open while the server is running. When needed, the server can be stopped with Ctrl+C. 


### Uploading Datasets
Specify the datasets (and metadata) to be uploaded by updating the arguments of the `upload_datasets` function call in the `uploads.py` file with the desired values (e.g path to datasets directory and data collector CSV files): 
```python
upload_datasets(
    # the directory containing the annular eclipse datasets (zipped files)
    annular_dir="/home/user/Desktop/zenodo/test/annular",
    # the CSV file containing the data collectors information for the annular eclipse data
    annular_data_collector_csv="/home/user/Desktop/zenodo/2023_annular_info.csv",
    # the directory containing the total eclipse datasets (zipped files)
    total_dir="/home/user/Desktop/zenodo/test/total",
    # the CSV file containing the data collectors information for the total eclipse data
    total_data_collector_csv="/home/user/Desktop/zenodo/2024_total_info.csv",
    # a CSV file to save successful results (will be created if does not exist)
    successful_results_file="/home/user/Desktop/zenodo/results/successul_results.csv",
    # a CSV file to save failed results (will be created if does not exist)
    failure_results_file="/home/user/Desktop/zenodo/results/failed_results.csv",
    # option to automatically delete any failed uploads, defaults to False
    delete_failures=True,
    # option to automatically publish a successful upload, defaults to False
    auto_publish=False,
)
```
All of the parameters are optional (and can be omitted) but at least one directory (annular or total) and its respective data collector CSV file must be specified. 

In a separate terminal and with `prefect-env` activated, create a deployment:
```bash
python uploads.py
```

This starts a long-running process that monitors for work from the Prefect server.

To run the deployment, navigate to the Prefect dashboard and on the left side panel go to Deployments, select upload-datasets-deployment from the list and then click Run and select Quick run from the dropdown. 

Once the run has started, each dataset will be uploaded sequentially and can be tracked in the 'Runs' section on the left side panel. 

Note that once a dataset has been uploaded it will be internally tracked so it's skipped in subsequent runs. Each dataset is tracked by it's file path. 

### Accepting and Publishing Requests
After the datasets have been uploaded, they will need to be reviewed and accepted for publishing since they are submitted to a community (Eclipse Soundscapes Community).

In a separate terminal and with `prefect-env` activated, create a deployment:
```bash
python requests.py
```

This starts a long-running process that monitors for work from the Prefect server.

To run the deployment, navigate to the Prefect dashboard and on the left side panel go to Deployments, select accept-requests-deployment from the list and then click Run and select Quick run from the dropdown. 

### Retrieving Published Records
In a separate terminal and with `prefect-env` activated, create a deployment:
```bash
python requests.py
```

This starts a long-running process that monitors for work from the Prefect server.

To run the deployment, navigate to the Prefect dashboard and on the left side panel go to Deployments, select get-published-records-deployment from the list and then click Run and select Quick run from the dropdown. 