# AZUS Directory Structure Guide

**Version:** 2.0 (Feb 25, 2026)  
**Reflects:** Post-Prefect standalone refactor

---

## Project Root Structure

```
azus/
├── Guides/                              ← Documentation and how-to guides
│   ├── CITATIONS_USER_GUIDE.md
│   ├── CSV_FIX_GUIDE.md
│   ├── DIRECTORY_STRUCTURE_GUIDE.md     ← This file
│   ├── PREFECT_VS_STANDALONE.md
│   ├── REFACTORING_CHANGELOG.md
│   ├── STANDALONE_README.md
│   └── TEST_UPLOAD_GUIDE.md
│
├── models/                              ← Pydantic data models (no editing needed)
│   ├── __init__.py
│   ├── audiomoth.py                     ← DataCollector, UploadData, DraftConfig
│   └── invenio.py                       ← Zenodo metadata models
│
├── Records/                             ← Upload result logs (auto-created)
│   ├── successful_results.csv           ← Created on first successful upload
│   └── failed_results.csv              ← Created on first failed upload
│
├── Resources/                           ← Config, templates, shared data files
│   ├── project_config.json              ← Project identity (creators, funding, etc.)
│   ├── config.json                      ← Upload configuration (YOU CREATE THIS)
│   ├── set_env.sh                       ← API credentials (YOU CREATE THIS)
│   ├── README_template.html             ← Template for Zenodo record descriptions
│   ├── related_identifiers.csv          ← Global citations/related works (optional)
│   ├── references.csv                   ← Global bibliography (optional)
│   ├── collectors.csv                   ← Collector metadata spreadsheet
│   ├── 2023_Annular_Zenodo_Form_Spreadsheet.csv
│   ├── 2024_Total_Zenodo_Form_Spreadsheet.csv
│   ├── CONFIG_data_dict.csv             ← Data dictionary for CONFIG.TXT fields
│   ├── WAV_data_dict.csv                ← Data dictionary for WAV file fields
│   ├── file_list_data_dict.csv          ← Data dictionary for file_list.csv
│   ├── file_list_Template.csv           ← Template for file manifests
│   ├── License.txt                      ← CC BY 4.0 license text
│   ├── AudioMoth_Operation_Manual.pdf   ← Uploaded to each Zenodo record
│   └── set_env.sh.example               ← Safe template (no real credentials)
│
├── Staging_Area/                        ← Prepared datasets ready for upload
│   └── ESID_XXX/                        ← One subdirectory per dataset
│       └── ... (see Staging Area section below)
│
├── templates/                           ← .example files for new project setup
│   ├── config.json.example
│   ├── project_config.json.example
│   ├── README_template.html.example
│   ├── related_identifiers.csv.example
│   ├── references.csv.example
│   └── set_env.sh.example
│
├── standalone_tasks.py                  ← MAIN ENTRY POINT
├── standalone_uploader.py               ← Zenodo API client
├── prepare_dataset.py                   ← Dataset preparation script
├── requirements-standalone.txt          ← Python dependencies
└── README.md
```

> ⚠️ **`Resources/config.json` and `Resources/set_env.sh` are NOT included in the
> repository** — they contain personal paths and API credentials. Create them from
> the `.example` templates in `templates/`.

---

## Staging Area Structure

Each dataset lives in its own self-contained subdirectory. The upload script
scans for `ESID_XXX/` subdirectories and processes each one independently.

```
Staging_Area/
├── ESID_004/
│   ├── ESID_004.zip                     ← Audio archive (WAV files + CONFIG.TXT)
│   ├── ESID_004_to_upload.csv           ← Upload manifest (what Zenodo receives)
│   ├── README.html                      ← Zenodo description (NOT uploaded as file)
│   ├── README.md                        ← Uploaded to Zenodo
│   ├── file_list.csv                    ← File manifest with SHA-512 hashes
│   ├── total_eclipse_data.csv           ← Site metadata for this ESID
│   ├── total_eclipse_data_data_dict.csv ← Data dictionary for above
│   ├── CONFIG_data_dict.csv
│   ├── WAV_data_dict.csv
│   ├── file_list_data_dict.csv
│   ├── AudioMoth_Operation_Manual.pdf
│   └── License.txt
│
├── ESID_005/
│   └── ... (same structure)
│
└── ESID_006/
    └── ... (same structure)
```

### File roles at a glance

| File | Uploaded to Zenodo | Purpose |
|------|--------------------|---------|
| `ESID_XXX.zip` | Yes | All WAV recordings + CONFIG.TXT |
| `ESID_XXX_to_upload.csv` | No | Tells AZUS which files to upload |
| `README.html` | No | Content becomes the Zenodo description field |
| `README.md` | Yes | Human-readable documentation |
| `file_list.csv` | Yes | Complete file listing with SHA-512 hashes |
| `total_eclipse_data.csv` | Yes | Single-row site metadata |
| `*_data_dict.csv` | Yes | Data dictionaries |
| `AudioMoth_Operation_Manual.pdf` | Yes | Device documentation |
| `License.txt` | Yes | CC BY 4.0 license |

### ZIP archive contents

```
ESID_004.zip contains:
├── 20240408_120000.WAV
├── 20240408_120500.WAV
├── ... (all WAV files)
└── CONFIG.TXT
```

### Upload manifest format

```csv
File Name
ESID_004.zip
README.md
file_list.csv
total_eclipse_data.csv
total_eclipse_data_data_dict.csv
CONFIG_data_dict.csv
WAV_data_dict.csv
file_list_data_dict.csv
AudioMoth_Operation_Manual.pdf
License.txt
```

`README.html` is deliberately excluded — its content is used as the Zenodo
description field, not uploaded as a file.

---

## Complete Workflow

### Step 1: Raw Data

Start with your raw AudioMoth data:

```
Raw_Data/
└── ESID#004/
    ├── 20240408_120000.WAV
    ├── 20240408_120500.WAV
    ├── ... (all WAV files)
    └── CONFIG.TXT                       ← Generated by AudioMoth device
```

> ⚠️ `CONFIG.TXT` is generated automatically by the AudioMoth device and will
> be present in any genuine AudioMoth recording folder. Do not create it manually.

---

### Step 2: Prepare Dataset

`prepare_dataset.py` does all the work in one step — creates the ZIP, generates
README.html from the template, creates the upload manifest, and assembles all
metadata files.

```bash
python prepare_dataset.py Raw_Data/ESID#004 \
    --collector-csv Resources/collectors.csv \
    --eclipse-type total \
    --output-dir Staging_Area/ESID_004
```

> ⚠️ Your collector CSV **must use the current column headers** exactly.
> `prepare_dataset.py` validates headers before processing and will print a
> clear error listing missing columns if the format is wrong.
> See `Guides/CSV_FIX_GUIDE.md` for the column mapping from older formats.

**What gets created:**
```
Staging_Area/
└── ESID_004/
    ├── ESID_004.zip                     ← Created (WAV files + CONFIG.TXT)
    ├── ESID_004_to_upload.csv           ← Created (upload manifest)
    ├── README.html                      ← Generated from README_template.html
    ├── README.md                        ← Generated from README.html
    ├── file_list.csv                    ← Generated (SHA-512 hashes of all files)
    ├── total_eclipse_data.csv           ← Generated (single-row metadata)
    ├── total_eclipse_data_data_dict.csv ← Copied from Resources/
    ├── CONFIG_data_dict.csv             ← Copied from Resources/
    ├── WAV_data_dict.csv                ← Copied from Resources/
    ├── file_list_data_dict.csv          ← Copied from Resources/
    ├── AudioMoth_Operation_Manual.pdf   ← Copied from Resources/
    └── License.txt                      ← Copied from Resources/
```

> There is no separate `create_upload_package.py` step. `prepare_dataset.py`
> handles everything including ZIP creation and manifest generation.

---

### Step 3: Configure

`Resources/config.json` uses a `datasets` list — one entry per batch of datasets:

```json
{
    "project_config": "Resources/project_config.json",
    "readme_template": "Resources/README_template.html",

    "uploads": {
        "datasets": [
            {
                "name": "2024 Total Eclipse",
                "dataset_dir": "/path/to/Staging_Area",
                "collectors_csv": "/path/to/Resources/collectors.csv",
                "dataset_category": "Total"
            }
        ],
        "related_identifiers_csv": "Resources/related_identifiers.csv",
        "references_csv": "Resources/references.csv",
        "successful_results_file": "Records/successful_results.csv",
        "failure_results_file": "Records/failed_results.csv",
        "delete_failures": false,
        "auto_publish": false
    }
}
```

**`dataset_dir` points to `Staging_Area/`** (the parent of ESID subdirectories),
not to any individual `ESID_XXX/` folder.

> ⚠️ **Old format no longer supported.** The previous `"total": {...}` and
> `"annular": {...}` keys have been replaced by the `"datasets": [...]` list.
> Multiple dataset categories are handled by adding additional entries to the list.

---

### Step 4: Upload

```bash
# Load credentials
source Resources/set_env.sh

# Dry run first — validates config and credentials (no network calls to Zenodo)
python standalone_tasks.py --config Resources/config.json --dry-run

# Actual upload (runs Zenodo connection check, then prompts for confirmation)
python standalone_tasks.py --config Resources/config.json
```

AZUS automatically runs a **Zenodo connection check** before uploading —
verifying both connectivity and write permissions using a minimal test draft
that is immediately deleted. If the check fails, a detailed error report is
printed and the upload is aborted cleanly.

**What happens during upload:**
1. Credentials loaded from environment variables
2. Zenodo connectivity and write-permission verified
3. Each `ESID_XXX/` subdirectory in `dataset_dir` is discovered
4. `ESID_XXX_to_upload.csv` manifest is read for each dataset
5. All listed files are verified present before any upload begins
6. Draft record created on Zenodo with full metadata
7. Files uploaded one by one
8. Results saved to `Records/successful_results.csv` or `failed_results.csv`
9. Uploaded ZIPs tracked in `.uploaded_files.txt` to prevent re-uploads on re-runs

---

## File Discovery Logic

```
standalone_tasks.py scans dataset_dir
    │
    ├── finds ESID_XXX/ subdirectories
    │       │
    │       ├── looks for ESID_XXX_to_upload.csv  (manifest)
    │       │       └── if found: uploads exactly the files listed
    │       │
    │       └── if no manifest: falls back to default_required_files
    │               from Resources/project_config.json
    │
    └── skips any ESID whose ZIP is already in .uploaded_files.txt
```

---

## Citations — Global vs Per-Record

`related_identifiers.csv` and `references.csv` can be specified globally in
`config.json` (applies the same citations to every record in the batch):

```json
"related_identifiers_csv": "Resources/related_identifiers.csv",
"references_csv": "Resources/references.csv"
```

> 🔜 **Coming soon:** Per-record citation override. Place
> `related_identifiers.csv` and/or `references.csv` directly inside an
> `ESID_XXX/` staging directory to override the global files for that record.
> If no per-record file is found, AZUS falls back to the global config paths.

---

## Common Structural Errors

### WRONG — ZIP in parent directory

```
Staging_Area/
├── ESID_005.zip              ← Wrong — must be inside ESID_005/
└── ESID_005/
    └── README.html
```

### WRONG — Flat structure (no subdirectories)

```
Staging_Area/
├── ESID_005.zip
├── README.md
└── file_list.csv
```

### WRONG — Old config format

```json
"uploads": {
    "total": { "dataset_dir": "..." }    ← Old format — no longer supported
}
```

### CORRECT — Subdirectory structure

```
Staging_Area/
└── ESID_005/
    ├── ESID_005.zip
    ├── ESID_005_to_upload.csv
    └── ... (all files together)
```

### CORRECT — New config format

```json
"uploads": {
    "datasets": [
        {
            "name": "2024 Total Eclipse",
            "dataset_dir": "/path/to/Staging_Area",
            "collectors_csv": "/path/to/collectors.csv",
            "dataset_category": "Total"
        }
    ]
}
```

---

## Verification Commands

```bash
# List all ESID directories in staging
ls -d Staging_Area/ESID_*/

# Confirm each has a ZIP and manifest
ls Staging_Area/ESID_*/ESID_*.zip
ls Staging_Area/ESID_*/ESID_*_to_upload.csv

# Inspect one dataset
ls -lh Staging_Area/ESID_004/
cat Staging_Area/ESID_004/ESID_004_to_upload.csv

# Verify ZIP contents
unzip -l Staging_Area/ESID_004/ESID_004.zip | head -20

# Validate JSON config syntax before running
python3 -c "import json; json.load(open('Resources/config.json'))" && echo "Valid JSON"

# Dry run — validates config and credentials
python standalone_tasks.py --config Resources/config.json --dry-run
```

---

## Batch Processing

Prepare multiple ESID directories, then upload all in one run:

```bash
# Prepare each dataset
python prepare_dataset.py Raw_Data/ESID#004 --collector-csv Resources/collectors.csv \
    --eclipse-type total --output-dir Staging_Area/ESID_004
python prepare_dataset.py Raw_Data/ESID#005 --collector-csv Resources/collectors.csv \
    --eclipse-type total --output-dir Staging_Area/ESID_005

# Upload all — AZUS processes every ESID_XXX/ subdirectory automatically
python standalone_tasks.py --config Resources/config.json
```

Already-uploaded ZIPs are tracked in `.uploaded_files.txt` and skipped on re-runs.

---

## Summary

- **`prepare_dataset.py`** handles ZIP creation, manifest, README, and metadata in one step
- **Each ESID** has its own self-contained subdirectory in `Staging_Area/`
- **`dataset_dir`** in config points to `Staging_Area/` (parent), not individual ESID folders
- **Entry point** is `standalone_tasks.py` — not `standalone_upload.py`
- **Config format** uses `"datasets": [...]` list — not `"total":` / `"annular":` keys
- **Zenodo connection** is verified automatically before any upload attempt
- **Credentials** live in `Resources/set_env.sh` — never hardcoded anywhere
