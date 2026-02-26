# AZUS Test Upload Guide

**Version:** 3.0 (Feb 25, 2026)
**Purpose:** Upload a test dataset to Zenodo using AZUS
**Time Required:** 30–60 minutes
**Skill Level:** Intermediate (command line experience helpful)

**What changed in v3.0:**
- Entry point corrected: `standalone_tasks.py` (not `standalone_upload.py`)
- Requirements file: `requirements-standalone.txt`
- Credentials file: `Resources/set_env.sh`
- Config format: `"datasets": [...]` list (not `"total": {...}` key)
- `prepare_dataset.py` now accepts `--config` to avoid duplicating the `collectors_csv` path
- `CONFIG.TXT` section corrected: comes from the AudioMoth device, not created manually
- `reserve_doi` reinstated as a config option

---

## Part 1: Environment Setup

### Step 1.1: Install Python Dependencies

```bash
# Navigate to AZUS directory
cd /path/to/azus

# Create virtual environment (recommended)
python3 -m venv azus-env
source azus-env/bin/activate       # On Windows: azus-env\Scripts\activate

# Install requirements
pip install -r requirements-standalone.txt
```

**Expected output:**
```
Successfully installed pydantic-2.x.x requests-2.x.x ...
```

### Step 1.2: Set Up Zenodo API Credentials

**Get your Zenodo API token:**
1. Go to https://sandbox.zenodo.org (for testing) or https://zenodo.org (production)
2. Click your username → Applications → Personal access tokens
3. Click "New token"
4. Name: `AZUS Test`
5. Scopes: select `deposit:write` and `deposit:actions`
6. Click "Create" — **copy the token immediately** (it won't be shown again)

**Configure credentials in `Resources/set_env.sh`:**

```bash
#!/bin/bash
# Zenodo API credentials — DO NOT commit this file to version control

# For TESTING: Zenodo Sandbox
export INVENIO_RDM_BASE_URL="https://sandbox.zenodo.org/api"
export INVENIO_RDM_ACCESS_TOKEN="your-sandbox-token-here"

# For PRODUCTION: uncomment and fill in (keep sandbox lines commented out)
# export INVENIO_RDM_BASE_URL="https://zenodo.org/api"
# export INVENIO_RDM_ACCESS_TOKEN="your-production-token-here"
```

> ⚠️ `Resources/set_env.sh` must never be committed to git. Confirm it is in
> `.gitignore` before working in a repository.

**Load credentials:**
```bash
source Resources/set_env.sh
```

**Verify:**
```bash
echo $INVENIO_RDM_BASE_URL
# Should output: https://sandbox.zenodo.org/api
```

### Step 1.3: Confirm project_config.json

`Resources/project_config.json` holds the project identity (creators, funding, license,
community ID, etc.). For the Eclipse Soundscapes project this file is already populated.
For a new project, copy `templates/project_config.json.example` and fill it in before
continuing.

---

## Part 2: Prepare Test Data

### Step 2.1: Create Test Directory Structure

```bash
mkdir -p ~/AZUS_Test_Workspace/{Raw_Data/ESID_999,Staging_Area,Records}
```

**Structure:**
```
~/AZUS_Test_Workspace/
├── Raw_Data/
│   └── ESID_999/          ← your test audio files go here
├── Staging_Area/          ← prepared datasets (auto-created by prepare_dataset.py)
└── Records/               ← upload result logs
```

### Step 2.2: Add Test Audio Files

**Option A — Use real AudioMoth recordings:**
```bash
cp /path/to/your/wav/files/*.WAV ~/AZUS_Test_Workspace/Raw_Data/ESID_999/
```

**Option B — Create dummy WAV files (testing only, requires ffmpeg):**
```bash
cd ~/AZUS_Test_Workspace/Raw_Data/ESID_999/
ffmpeg -f lavfi -i "sine=frequency=440:duration=5" -ac 1 -ar 16000 20240408_120000.WAV
ffmpeg -f lavfi -i "sine=frequency=440:duration=5" -ac 1 -ar 16000 20240408_120500.WAV
ffmpeg -f lavfi -i "sine=frequency=440:duration=5" -ac 1 -ar 16000 20240408_121000.WAV
```

### Step 2.3: CONFIG.TXT

`CONFIG.TXT` is **generated automatically by the AudioMoth device** when it records.
It will already be present in any genuine AudioMoth recording folder.

If you are testing with dummy WAV files (Option B above) and do not have a real
`CONFIG.TXT`, you can omit it — `prepare_dataset.py` will log a warning but will
proceed and create the ZIP without it. Do not create a fake `CONFIG.TXT` by hand;
any values you invent will be incorrect metadata in the Zenodo record.

**To verify a real CONFIG.TXT is present:**
```bash
ls ~/AZUS_Test_Workspace/Raw_Data/ESID_999/CONFIG.TXT
```

### Step 2.4: Create Collector CSV

The collector CSV must use the current column headers exactly. `prepare_dataset.py`
validates headers before processing and will print a clear diff if anything is wrong.

```bash
cat > ~/AZUS_Test_Workspace/Resources/test_collectors.csv << 'EOF'
ESID,Data Collector Affiliations,Latitude,Longitude,Local Eclipse Type,Eclipse Percent (%),WAV Files Time & Date Settings,Data Collector Start Time Notes,Eclipse Date,Eclipse Start Time (UTC) (1st Contact),Totality Start Time (UTC) (2nd Contact),Eclipse Maximum (UTC),Totality End Time (UTC) (3rd Contact),Eclipse End Time (UTC) (4th Contact),Version,Keywords and subjects,year
999,Test Organization,42.3601,-71.0589,Total,100,Set with Automated AudioMoth Time chime,,2024-04-08,18:15:00,19:29:00,19:30:45,19:32:30,20:45:00,2024.1.0,Solar Eclipse:Audio Recording:Citizen Science:Soundscape,2024
EOF
```

**Field notes:**
- `ESID`: Use `999` for test uploads
- `Latitude`/`Longitude`: Boston, MA shown as an example
- `Eclipse Date`: `YYYY-MM-DD` format
- All times in UTC (`HH:MM:SS`)
- `Keywords and subjects`: colon-separated list
- `Version`: `2024.1.0` for total eclipse; `2023.9.0` for annular

### Step 2.5: Create Citations CSVs (Optional)

Place these in the `Resources/` directory to apply them globally to every record,
or inside a specific `ESID_XXX/` staging directory to apply them to that record only
(the per-record file takes precedence).

**related_identifiers.csv:**
```bash
cat > ~/AZUS_Test_Workspace/Resources/related_identifiers.csv << 'EOF'
identifier,scheme,relation_type,resource_type
10.1038/s41597-024-03940-2,doi,cites,publication-article
https://eclipsesoundscapes.org,url,isSupplementTo,
EOF
```

**references.csv:**
```bash
cat > ~/AZUS_Test_Workspace/Resources/references.csv << 'EOF'
reference
"Henshaw, W. D., et al. (2024). Eclipse Soundscapes Project Data. Scientific Data, 11(1), 1098."
EOF
```

### Step 2.6: Verify Test Data

```bash
ls -lh ~/AZUS_Test_Workspace/Raw_Data/ESID_999/
# Should show WAV files and optionally CONFIG.TXT
```

---

## Part 3: Configure config.json

Create `Resources/config.json` for the test workspace. Note that `collectors_csv`
here is the **same path** that `prepare_dataset.py` will read via `--config` — there
is only one place to set it.

```bash
cat > Resources/config_test.json << EOF
{
    "_comment": "Test config — points to AZUS_Test_Workspace",
    "project_config": "Resources/project_config.json",
    "readme_template": "Resources/README_template.html",

    "uploads": {
        "datasets": [
            {
                "name": "Test Total Eclipse",
                "dataset_dir": "$HOME/AZUS_Test_Workspace/Staging_Area",
                "collectors_csv": "$HOME/AZUS_Test_Workspace/Resources/test_collectors.csv",
                "dataset_category": "Total"
            }
        ],
        "related_identifiers_csv": "$HOME/AZUS_Test_Workspace/Resources/related_identifiers.csv",
        "references_csv": "$HOME/AZUS_Test_Workspace/Resources/references.csv",
        "successful_results_file": "$HOME/AZUS_Test_Workspace/Records/successful_results.csv",
        "failure_results_file": "$HOME/AZUS_Test_Workspace/Records/failed_results.csv",
        "delete_failures": false,
        "auto_publish": false,
        "reserve_doi": false
    },

    "downloads": {
        "results_dir": "$HOME/AZUS_Test_Workspace/Records/"
    }
}
EOF
```

> `reserve_doi: false` is correct for Sandbox — Sandbox DOIs are not registered
> with DataCite. Only set this to `true` on production Zenodo for real datasets.

**Validate the JSON is well-formed:**
```bash
python3 -c "import json; json.load(open('Resources/config_test.json'))" && echo "Valid JSON"
```

---

## Part 4: Prepare Dataset for Upload

`prepare_dataset.py` handles everything in one step: creates the ZIP, generates
README.html, creates the upload manifest, and assembles all metadata files.

### Step 4.1: Run Dataset Preparation

Use `--config` to read `collectors_csv` directly from `config_test.json` — no need
to supply the path twice:

```bash
python prepare_dataset.py \
    ~/AZUS_Test_Workspace/Raw_Data/ESID_999 \
    --config Resources/config_test.json \
    --eclipse-type total \
    --output-dir ~/AZUS_Test_Workspace/Staging_Area/ESID_999
```

You can also supply `--collector-csv` directly if you prefer (it overrides `--config`):
```bash
python prepare_dataset.py \
    ~/AZUS_Test_Workspace/Raw_Data/ESID_999 \
    --collector-csv ~/AZUS_Test_Workspace/Resources/test_collectors.csv \
    --eclipse-type total \
    --output-dir ~/AZUS_Test_Workspace/Staging_Area/ESID_999
```

**Expected output:**
```
======================================================================
AZUS DATASET PREPARATION
======================================================================
ESID:           999
Source:         /Users/you/AZUS_Test_Workspace/Raw_Data/ESID_999
Output:         /Users/you/AZUS_Test_Workspace/Staging_Area/ESID_999
Collector CSV:  /Users/you/AZUS_Test_Workspace/Resources/test_collectors.csv
  (path read from config.json)
Eclipse type:   total
...
DATASET PREPARATION COMPLETE
======================================================================
```

> If you see "Collector CSV has incorrect or missing column headers" — check
> your CSV against the headers in Step 2.4 and see `Guides/CSV_FIX_GUIDE.md`.

### Step 4.2: Verify Prepared Dataset

```bash
ls -lh ~/AZUS_Test_Workspace/Staging_Area/ESID_999/
```

**You should see:**
```
ESID_999.zip
ESID_999_to_upload.csv
README.html
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

---

## Part 5: Dry Run

The dry run validates your config and credentials without making any network calls
to Zenodo.

```bash
python standalone_tasks.py --config Resources/config_test.json --dry-run
```

**Expected output:**
```
======================================================================
AZUS STANDALONE UPLOAD
======================================================================
Project:             Eclipse Soundscapes
Datasets configured: 1
  • Test Total Eclipse → .../Staging_Area
Auto-publish:        False
Delete failures:     False
Reserve DOI:         False
======================================================================
Dry run complete — configuration is valid
```

---

## Part 6: Upload

### Step 6.1: Run the Upload

```bash
source Resources/set_env.sh
python standalone_tasks.py --config Resources/config_test.json
```

AZUS will automatically run a **Zenodo connection check** (verifying connectivity
and write permissions) before proceeding. If this fails, a detailed error report
is printed and the upload is aborted cleanly.

After the connection check passes you will see a confirmation prompt:

```
⚠️  You are about to upload datasets to Zenodo.
   This will create REAL records on Zenodo.

Proceed? (yes/no):
```

Type `yes` to proceed.

**Expected output:**
```
======================================================================
ZENODO CONNECTION CHECK
======================================================================
  [PASS] Connectivity check (HTTP 200)
  [PASS] Write permission check (HTTP 201)
Connection check passed — proceeding to upload
======================================================================
...
======================================================================
UPLOAD SUMMARY
======================================================================
Total processed: 1
Successful:      1
Failed:          0
Skipped:         0
======================================================================
```

### Step 6.2: Verify Results

```bash
cat ~/AZUS_Test_Workspace/Records/successful_results.csv
```

---

## Part 7: Review on Zenodo

1. Copy the Zenodo URL from the output (e.g., `https://sandbox.zenodo.org/records/1234567`)
2. Open in browser and log in to Zenodo Sandbox

**What you should see:**
- Record is in **DRAFT** status (not published)
- Title in correct format
- Description: HTML content from README.html
- Files: ESID_999.zip, README.md, file_list.csv, etc.
- Metadata: Creators, funding, license, dates
- Related works and references (if CSVs were provided)

**Checklist:**
- [ ] ESID_999.zip present
- [ ] README.md present
- [ ] file_list.csv present
- [ ] total_eclipse_data.csv present
- [ ] All data dictionaries present
- [ ] License.txt present
- [ ] Title correct
- [ ] Creators correct
- [ ] Funding: NASA Award No. 80NSSC21M0008
- [ ] License: CC-BY-4.0
- [ ] Keywords/Subjects listed

---

## Part 8: Publish or Delete

### Option A — Publish (makes the record public)

> ⚠️ Once published, records **cannot be deleted**, only versioned.

Via web interface: click "Publish" and confirm.

Via AZUS (set `auto_publish: true` in config, then re-run):
```bash
python standalone_tasks.py --config Resources/config_test.json
```

### Option B — Delete Draft (recommended for test uploads)

Via the Zenodo web interface: click "Delete" on the draft record. This keeps
your account clean.

---

## Part 9: Troubleshooting

### "Collector CSV has incorrect or missing column headers"

Your CSV uses old column names (e.g., `ESID#` instead of `ESID`, `Type of Eclipse`
instead of `Local Eclipse Type`). The error output will show exactly which columns
are missing and which were found. See `Guides/CSV_FIX_GUIDE.md` for the full mapping.

### "Configuration file not found"

```
ERROR - Configuration file not found: Resources/config_test.json
```

Check the filename matches exactly — `config_test.json` vs `config.json`.

### "Extra data" JSON parse error

```
json.decoder.JSONDecodeError: Extra data
```

Your config.json is missing the opening `{` or closing `}`. Validate with:
```bash
python3 -c "import json; json.load(open('Resources/config_test.json'))" && echo "Valid JSON"
```

### "ZENODO CONNECTION CHECK FAILED"

```
[FAIL] Connectivity check — 401 Unauthorized
```

Token is invalid or not loaded. Run `source Resources/set_env.sh` and check
the token in `Resources/set_env.sh` matches what was generated on Zenodo.

### "No collector data found for ESID 999"

```
ERROR - No collector data found for ESID 999
```

The CSV loaded successfully (headers are correct) but row 999 is missing.
Check: `grep "^999," ~/AZUS_Test_Workspace/Resources/test_collectors.csv`

### "HTTP 500 from Zenodo"

Zenodo server error — not a problem on your end. Check
https://status.zenodo.org, then simply re-run. AZUS skips already-uploaded
records, so only the failed one will be retried.

---

## Part 10: Cleanup

```bash
# Remove test workspace (optional)
rm -rf ~/AZUS_Test_Workspace

# Deactivate virtual environment
deactivate
```

Delete any draft records from your Zenodo Sandbox account via the web interface.

---

## Quick Reference

```bash
# Load credentials
source Resources/set_env.sh

# Prepare dataset (reads collectors_csv from config.json)
python prepare_dataset.py Raw_Data/ESID_999 \
    --config Resources/config_test.json \
    --eclipse-type total \
    --output-dir Staging_Area/ESID_999

# Dry run
python standalone_tasks.py --config Resources/config_test.json --dry-run

# Upload
python standalone_tasks.py --config Resources/config_test.json

# Check results
cat Records/successful_results.csv
cat Records/failed_results.csv
```

---

## Production Checklist

Before switching from Sandbox to production Zenodo:

1. **Switch credentials in `Resources/set_env.sh`:**
   ```bash
   export INVENIO_RDM_BASE_URL="https://zenodo.org/api"
   export INVENIO_RDM_ACCESS_TOKEN="your-production-token"
   ```

2. **Enable DOI reservation (recommended for production):**
   ```json
   "reserve_doi": true
   ```
   > ⚠️ Reserved DOIs are permanent — only enable for real datasets you intend to publish.

3. **Use real ESIDs and real collector data.**

4. **Consider `auto_publish: true`** only after thorough review of draft records.

---

**Guide Version:** 3.0 (Feb 25, 2026)
**AZUS Version:** Standalone (post-Prefect refactor)
**Tested On:** Python 3.9+, macOS/Linux
