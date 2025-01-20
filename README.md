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

Kickoff the upload:
```bash
python upload.py
```
