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

## Upload Resilience

- **Auto-retry** on transient SSL/connection drops and HTTP 5xx (3 attempts,
  30s/90s/270s backoff per file PUT).
- **Resumable drafts** — if a run dies mid-upload, re-running picks up the same
  Zenodo draft and only re-uploads files that aren't already committed
  (state tracked in `upload_state.json` inside the ESID staging folder).
- **Concurrent ESID uploads** — `--workers N` uploads N ESID datasets at the
  same time. Default `1` (sequential, identical to original behavior).
  Example: `python standalone_tasks.py --config Resources/config.json --workers 3`.
- **Interrupted-prep detection** — `prepare_dataset.py` moves into
  `Staging_Area/` via a two-phase atomic pattern and writes a `.prep_complete`
  sentinel file as its very last action. `prep_all_datasets.py`'s skip check
  requires that sentinel, so an interrupted preparation cannot be silently
  skipped on the next batch run — the partial folder is re-prepped instead.

See `Guides/STANDALONE_README.md` for the full retry/resume/concurrency behavior.

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

# 6. Prepare a dataset (single ESID)
python Resources/prepare_dataset.py Raw_Data/ESID_XXX --config Resources/config.json

# 6b. ...OR prepare every ESID in a top-level folder, in numerical order,
#     skipping any ESID already prepared in Staging_Area/ or Uploaded_Data/
python Resources/prep_all_datasets.py /path/to/Raw_Data --config Resources/config.json

# 7b. Finish any uploads that died mid-stream (e.g., ZIP exhausted its retries)
#     Auto-discovers stuck ESIDs from Staging_Area/upload_state.json files
python Resources/finish_stuck_uploads.py --workers 3

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

AZUS distributes content under three distinct licenses, each governing a
different scope:

- **Source code** — BSD 3-Clause License. See [`LICENSE`](LICENSE) at the
  repository root. This covers all Python source (`standalone_tasks.py`,
  `standalone_uploader.py`, the tools in `Resources/`, the `models/` package,
  and everything else under version control). BSD 3-Clause is on NASA's named
  list of permissive licenses approved for grantee-developed software under
  the Earth Science Data Systems Open-Source Software Policy.

- **Dataset content uploaded to Zenodo** — Creative Commons Attribution 4.0
  International (CC BY 4.0). See [`Resources/License.txt`](Resources/License.txt).
  This is the license declared in `Resources/project_config.json`
  (`"license": "cc-by-4.0"`) and bundled into every dataset upload as a
  companion file. Code-license changes do not affect this.

- **Bundled third-party documents** in `Resources/` —
  `AudioMoth_Operation_Manual.pdf` (© Open Acoustic Devices),
  `Eclipse_Soundscapes_Data_Collector_Role_Training_and_Implementation_Manual_2023-2024.pdf`,
  and the four `ES_Data_Management_*_Stage_*.pdf` files are distributed under
  the copyright stated in each document. They are not covered by the
  BSD 3-Clause `LICENSE`.
