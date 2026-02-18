# AZUS Automated Zenodo Upload Software

##Description

AZUS is a command-line tool developed by the Eclipse Soundscapes team to automate the batch upload of structured datasets to Zenodo. Built on Zenodo’s Open API, AZUS streamlines the entire data publishing process, which includes metadata assignment, licensing, and DOI generation, and makes it possible to efficiently archive hundreds of large datasets with minimal manual effort. Originally created to support the Eclipse Soundscapes project’s goal of open, long-term data sharing, AZUS now serves as a reusable tool aligned with NASA’s Open Science policies.
AZUS is designed to:
* Automate repetitive upload tasks for large volumes of data
* Ensure each dataset includes standardized metadata and documentation
* Eliminate Zenodo’s manual one-record-at-a-time upload limitation
* Support open access, citation, and long-term data preservation
* AZUS will be released as open-source software on GitHub under a BSD 3-Clause License, with documentation and examples to support adaptation by other participatory science and research teams.


## Getting started

Requires an installation of Python 3.9+.

### Environment variables
To successfully authenticate with Zenodo, configure the following environment variables in 
the `set_env.sh` file:

```bash
export INVENIO_RDM_ACCESS_TOKEN=<access_token>
export INVENIO_RDM_BASE_URL=<base url>
```

## Copyrights, Acknowledgements, and Atrributions

Eclipse Soundscapes is an enterprise of ARISA Lab, LLC and is supported by NASA award No. 80NSSC21M0008. Any opinions, findings, and conclusions or recommendations expressed in this material are those of the author(s) and do not necessarily reflect the views of the National Aeronautics and Space Administration.


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

## Overview

The project consists of two main scripts:

- `uploads.py`: Handles the uploading of datasets to Zenodo.
- `records.py`: Downloads metadata for already uploaded Zenodo records.

Both scripts are configured using a `config.json` file.


## Configuration

The project uses a `config.json` file to control file paths and behavior. Here's an example structure of the configuration file:

```json
{
  "uploads": {
    "total": {
      "dataset_dir": "/home/joel/Desktop/zenodo/test/total",
      "collectors_csv": "/home/joel/Desktop/zenodo/2024_total_info_updated.csv"
    },
    "annular": {
      "dataset_dir": "/home/joel/Desktop/zenodo/test/annular",
      "collectors_csv": "/home/joel/Desktop/zenodo/2023_annular_info.csv"
    },
    "successful_results_file": "/home/joel/Desktop/zenodo/results/successul_results.csv",
    "failure_results_file": "/home/joel/Desktop/zenodo/results/failed_results.csv",
    "delete_failures": false,
    "auto_publish": false
  },
  "downloads": {
    "results_dir": "/home/joel/Desktop/zenodo/results/records/"
  }
}
```

### Configuration Breakdown

#### `uploads`

Controls the upload process handled by `uploads.py`.

- `total.dataset_dir`: Path to the directory containing the **total eclipse dataset**.
- `total.collectors_csv`: CSV file containing metadata for the total eclipse collectors.
- `annular.dataset_dir`: Path to the directory containing the **annular eclipse dataset**.
- `annular.collectors_csv`: CSV file containing metadata for the annular eclipse collectors.
- `successful_results_file`: File path where successfully uploaded records will be logged.
- `failure_results_file`: File path to log failed uploads.
- `delete_failures`: If `true`, files that failed to upload will be deleted.
- `auto_publish`: If `true`, records will be published automatically after upload.

#### `downloads`

Controls the download process handled by `records.py`.

- `results_dir`: Directory where the downloaded metadata records will be saved.

---

## Usage

### Uploading Datasets

In a separate terminal and with `prefect-env` activated, create a deployment:
```bash
python uploads.py
```

This starts a long-running process that monitors for work from the Prefect server.

To run the deployment, navigate to the Prefect dashboard and on the left side panel go to Deployments, select upload-datasets-deployment from the list and then click Run and select Quick run from the dropdown. 

Once the run has started, each dataset will be uploaded sequentially and can be tracked in the 'Runs' section on the left side panel. 

**Note that once a dataset has been uploaded it will be internally tracked so it's skipped in subsequent runs. Each dataset is tracked by it's file path.**

### Retrieving Published Records
Once the records have been published, the results can be retrieved and saved locally as a CSV formatted file which will be named in the following format: `records_{timestamp}`.  

In a separate terminal and with `prefect-env` activated, create a deployment:
```bash
python records.py
```

This starts a long-running process that monitors for work from the Prefect server.

To run the deployment, navigate to the Prefect dashboard and on the left side panel go to Deployments, select get-published-records-deployment from the list and then click Run and select Quick run from the dropdown. 
