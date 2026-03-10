# AZUS — Automated Zenodo Upload System

A generalizable tool for batch-uploading structured citizen science audio
datasets to Zenodo repositories.

Originally developed for the [Eclipse Soundscapes Project](https://eclipsesoundscapes.org)
(NASA Award No. 80NSSC21M0008), developed by ARISA Lab, LLC.

## Design Goal

**Make uploading structured datasets to Zenodo as easy as possible for
non-programmer scientists and citizen science project coordinators.**

Adding new companion files, changing citations, or adapting AZUS for a new
project should require editing only human-readable CSV and JSON files — never
Python code.

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

# 5. Configure companion files (data dicts, license, manuals, etc.)
cp templates/resource_files_list.csv.example Resources/resource_files_list.csv
# Edit Resources/resource_files_list.csv — add one row per companion file

# 6. Prepare a dataset
python Resources/prepare_dataset.py Raw_Data/ESID_XXX --config Resources/config.json

# 7. Upload (dry run first)
source Resources/set_env.sh
python standalone_tasks.py --config Resources/config.json --dry-run
python standalone_tasks.py --config Resources/config.json
```

## Adding Files to Your Dataset

To include a new companion file (documentation, data dictionary, manual, etc.)
in every dataset upload:

1. Place the file in `Resources/`
2. Add one row to `Resources/resource_files_list.csv`
3. Run `prepare_dataset.py` as normal

**No Python code changes required.**

## Key Configuration Files

| File | Purpose | Edit? |
|------|---------|-------|
| `Resources/resource_files_list.csv` | Which companion files to include in every dataset | ✅ Yes — to add/remove files |
| `Resources/project_config.json` | Project identity: creators, funding, license, community | ✅ Yes — once per project |
| `Resources/config.json` | Upload paths and settings | ✅ Yes — once per machine |
| `Resources/README_template.html` | Template for Zenodo record descriptions | ✅ Yes — to customize descriptions |
| `Resources/related_identifiers.csv` | Related works / citations | ✅ Yes — to add DOI links |
| `Resources/references.csv` | Bibliography references | ✅ Yes — to add references |
| `Resources/set_env.sh` | API credentials | ✅ Yes — secret, never commit |

## Documentation

See the `Guides/` directory for full documentation:

- `TEST_UPLOAD_GUIDE.md` — Step-by-step test upload walkthrough
- `DIRECTORY_STRUCTURE_GUIDE.md` — Full directory and file structure reference
- `CITATIONS_USER_GUIDE.md` — How to configure citations and related works
- `CSV_FIX_GUIDE.md` — Column mapping for older spreadsheet formats
- `STANDALONE_README.md` — Architecture overview

## License

See `Resources/License.txt` — CC BY 4.0
