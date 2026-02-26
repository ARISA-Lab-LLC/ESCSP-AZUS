# AZUS — Automated Zenodo Upload System

A generalizable tool for batch-uploading structured citizen science audio
datasets to Zenodo repositories.

Originally developed for the [Eclipse Soundscapes Project](https://eclipsesoundscapes.org)
(NASA Award No. 80NSSC21M0008), developed by ARISA Lab, LLC.

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements-standalone.txt

# 2. Configure credentials
cp templates/set_env.sh.example Resources/set_env.sh
# Edit Resources/set_env.sh with your Zenodo API token

# 3. Configure upload settings
cp templates/config.json.example Resources/config.json
# Edit Resources/config.json with your paths

# 4. Configure project identity
cp templates/project_config.json.example Resources/project_config.json
# Edit Resources/project_config.json with your project details

# 5. Prepare a dataset
python prepare_dataset.py Raw_Data/ESID_XXX --config Resources/config.json

# 6. Upload (dry run first)
source Resources/set_env.sh
python standalone_tasks.py --config Resources/config.json --dry-run
python standalone_tasks.py --config Resources/config.json
```

## Documentation

See the `Guides/` directory for full documentation:

- `TEST_UPLOAD_GUIDE.md` — Step-by-step test upload walkthrough
- `DIRECTORY_STRUCTURE_GUIDE.md` — Full directory and file structure reference
- `CITATIONS_USER_GUIDE.md` — How to configure citations and related works
- `CSV_FIX_GUIDE.md` — Column mapping for older spreadsheet formats
- `STANDALONE_README.md` — Architecture overview

## License

See `Resources/License.txt` — CC BY 4.0
