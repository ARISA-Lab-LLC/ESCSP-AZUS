# AZUS Test Upload Guide - Step by Step

**Version:** Current (Feb 22, 2026)  
**Purpose:** Upload a test dataset to Zenodo using ESCSP-AZUS  
**Time Required:** 30-60 minutes  
**Skill Level:** Intermediate (command line experience helpful)

---

## Prerequisites

### ✅ Required Software
- Python 3.9 or higher
- Git (optional, for cloning repository)
- Text editor (for editing config files)
- Terminal/command line access

### ✅ Required Accounts
- Zenodo account (create at https://zenodo.org)
- Zenodo API token (generate from your Zenodo account)

### ✅ Test Data Requirements
For this guide, you'll need:
- **Test audio files:** 5-10 WAV files (AudioMoth recordings or any WAV files)
- **CONFIG.TXT file:** AudioMoth configuration file (can create a simple one)
- **Collector metadata:** Information about the recording site

---

## Part 1: Environment Setup

### Step 1.1: Install Python Dependencies

```bash
# Navigate to AZUS directory
cd /path/to/ESCSP-AZUS

# Create virtual environment (recommended)
python3 -m venv azus-env
source azus-env/bin/activate  # On Windows: azus-env\Scripts\activate

# Install requirements
pip install -r requirements.txt
```

**Expected output:**
```
Successfully installed pydantic-2.x.x requests-2.x.x ...
```

### Step 1.2: Set Up Zenodo API Credentials

**Get your Zenodo API token:**
1. Go to https://zenodo.org (or https://sandbox.zenodo.org for testing)
2. Click your username → Applications → Personal access tokens
3. Click "New token"
4. Name: "AZUS Test"
5. Scopes: Select `deposit:write` and `deposit:actions`
6. Click "Create"
7. **Copy the token** (you won't see it again!)

**Configure credentials:**

Edit `set_env.sh`:
```bash
#!/bin/bash
# Zenodo API Configuration

# For TESTING: Use Zenodo Sandbox
export INVENIO_RDM_BASE_URL="https://sandbox.zenodo.org/api"
export INVENIO_RDM_ACCESS_TOKEN="your-token-here"

# For PRODUCTION: Use Real Zenodo (commented out for safety)
# export INVENIO_RDM_BASE_URL="https://zenodo.org/api"
# export INVENIO_RDM_ACCESS_TOKEN="your-production-token-here"
```

**Load credentials:**
```bash
source set_env.sh
```

**Verify:**
```bash
echo $INVENIO_RDM_BASE_URL
# Should output: https://sandbox.zenodo.org/api
```

---

## Part 2: Prepare Test Data

### Step 2.1: Create Test Data Directory Structure

```bash
# Create workspace
mkdir -p ~/AZUS_Test_Workspace/{Raw_Data,Staging_Area,Resources,Records}

# Create test ESID folder
mkdir -p ~/AZUS_Test_Workspace/Raw_Data/ESID_999
```

**Directory structure:**
```
~/AZUS_Test_Workspace/
├── Raw_Data/
│   └── ESID_999/          # Your test audio files go here
├── Staging_Area/          # Prepared datasets (auto-created)
├── Resources/             # CSV files and templates
└── Records/               # Upload results logs
```

### Step 2.2: Add Test Audio Files

**Option A: Use Real AudioMoth Files**
```bash
# Copy your WAV files
cp /path/to/your/wav/files/*.WAV ~/AZUS_Test_Workspace/Raw_Data/ESID_999/
```

**Option B: Create Dummy WAV Files (for testing only)**
```bash
# Create small test WAV files (requires sox or ffmpeg)
cd ~/AZUS_Test_Workspace/Raw_Data/ESID_999/

# Using ffmpeg (if installed):
ffmpeg -f lavfi -i "sine=frequency=440:duration=5" -ac 1 -ar 16000 20240408_120000.WAV
ffmpeg -f lavfi -i "sine=frequency=440:duration=5" -ac 1 -ar 16000 20240408_120500.WAV
ffmpeg -f lavfi -i "sine=frequency=440:duration=5" -ac 1 -ar 16000 20240408_121000.WAV
```

### Step 2.3: Create CONFIG.TXT File

```bash
cd ~/AZUS_Test_Workspace/Raw_Data/ESID_999/

cat > CONFIG.TXT << 'EOF'
Device ID                : 24A7E5F31D2B9C40
Battery state            : 4.8V
Sample rate (Hz)         : 48000
Gain setting             : Medium (2)
Recording duration (s)   : 300
Sleep duration (s)       : 0
Recording period         : Always
Filter                   : Low pass
Firmware                 : AudioMoth-Firmware-Basic 1.8.0
EOF
```

### Step 2.4: Create Collector CSV

```bash
cd ~/AZUS_Test_Workspace/Resources/

cat > test_collectors.csv << 'EOF'
ESID,Latitude,Longitude,Local Eclipse Type,Eclipse Percent (%),Eclipse Date,Eclipse Start Time (UTC) (1st Contact),Eclipse Maximum (UTC),Eclipse End Time (UTC) (4th Contact),Totality Start Time (UTC) (2nd Contact),Totality End Time (UTC) (3rd Contact),WAV Files Time & Date Settings,Version,Subjects
999,42.3601,-71.0589,Total Solar Eclipse,100,2024-04-08,18:15:00,19:30:45,20:45:00,19:29:00,19:32:30,AudioMoth Time Chime,2024.1.0,Solar Eclipse:Audio Recording:Citizen Science:Soundscape
EOF
```

**Explanation of fields:**
- **ESID:** Eclipse Soundscapes ID (use 999 for testing)
- **Latitude/Longitude:** Recording location (example: Boston, MA)
- **Eclipse type/percent:** Total/100 or Annular/90, etc.
- **Eclipse Date:** YYYY-MM-DD format
- **Times:** All in UTC (HH:MM:SS format)
- **Version:** 2024.1.0 for total, 2023.9.0 for annular
- **Subjects:** Colon-separated keywords

### Step 2.5: Create Citations CSV (Optional)

```bash
cd ~/AZUS_Test_Workspace/Resources/

cat > related_identifiers.csv << 'EOF'
identifier,scheme,relation_type,resource_type
10.1038/s41597-024-03940-2,doi,cites,publication-article
https://eclipsesoundscapes.org,url,isSupplementTo,
EOF
```

### Step 2.6: Create References CSV (Optional)

```bash
cd ~/AZUS_Test_Workspace/Resources/

cat > references.csv << 'EOF'
reference
"Henshaw, W. D., et al. (2024). Eclipse Soundscapes Project Data. Scientific Data, 11(1), 1098."
EOF
```

### Step 2.7: Verify Test Data

```bash
# Check files are in place
ls -lh ~/AZUS_Test_Workspace/Raw_Data/ESID_999/

# Should show:
# CONFIG.TXT
# 20240408_120000.WAV
# 20240408_120500.WAV
# 20240408_121000.WAV
```

---

## Part 3: Prepare Dataset for Upload

### Step 3.1: Run Dataset Preparation Script

```bash
cd /path/to/ESCSP-AZUS

python prepare_dataset.py \
  ~/AZUS_Test_Workspace/Raw_Data/ESID_999 \
  --collector-csv ~/AZUS_Test_Workspace/Resources/test_collectors.csv \
  --eclipse-type total \
  --output-dir ~/AZUS_Test_Workspace/Staging_Area/ESID_999
```

**What this does:**
1. ✅ Creates ESID_999.zip (all WAV files + CONFIG.TXT)
2. ✅ Generates README.html (Zenodo description)
3. ✅ Generates README.md (markdown version)
4. ✅ Creates file_list.csv (file manifest with SHA-512 hashes)
5. ✅ Creates data dictionaries
6. ✅ Copies resource files

**Expected output:**
```
======================================================================
AZUS DATASET PREPARATION
======================================================================

ESID:           999
Source:         /Users/you/AZUS_Test_Workspace/Raw_Data/ESID_999
Output:         /Users/you/AZUS_Test_Workspace/Staging_Area/ESID_999
Collector CSV:  /Users/you/AZUS_Test_Workspace/Resources/test_collectors.csv
Eclipse type:   total

📦 Creating ZIP file: ESID_999.zip
   ✅ Added CONFIG.TXT
   ... added 3 WAV files

📄 Creating README.html
   ✅ Created: README.html

📄 Creating README.md
   ✅ Created: README.md

📋 Creating file_list.csv
   ✅ Created: file_list.csv

======================================================================
✅ DATASET PREPARATION COMPLETE
======================================================================

📁 Output directory: /Users/you/AZUS_Test_Workspace/Staging_Area/ESID_999

Files created:
   ✅ ESID_999.zip                                          (  0.15 MB)
   ✅ README.html                                           (  0.01 MB)
   ✅ README.md                                             (  0.01 MB)
   ✅ file_list.csv                                         (  0.00 MB)
   ✅ total_eclipse_data.csv                                (  0.00 MB)
   ✅ License.txt                                           (  0.00 MB)

📦 Ready for upload to Zenodo!

Next steps:
  1. Verify files in: /Users/you/AZUS_Test_Workspace/Staging_Area/ESID_999
  2. Update config.json to point to: /Users/you/AZUS_Test_Workspace/Staging_Area
  3. Run: python standalone_upload.py
```

### Step 3.2: Verify Prepared Dataset

```bash
# Check all files were created
ls -lh ~/AZUS_Test_Workspace/Staging_Area/ESID_999/

# View README.html (optional)
cat ~/AZUS_Test_Workspace/Staging_Area/ESID_999/README.html

# Check file_list.csv
cat ~/AZUS_Test_Workspace/Staging_Area/ESID_999/file_list.csv
```

---

## Part 4: Configure Upload Settings

### Step 4.1: Create/Update config.json

```bash
cd /path/to/ESCSP-AZUS

cat > config_test.json << EOF
{
  "uploads": {
    "total": {
      "dataset_dir": "$HOME/AZUS_Test_Workspace/Staging_Area",
      "collectors_csv": "$HOME/AZUS_Test_Workspace/Resources/test_collectors.csv"
    },
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

**Important settings:**
- **dataset_dir:** Points to Staging_Area (parent of ESID folders)
- **auto_publish:** `false` for testing (creates draft, doesn't publish)
- **delete_failures:** `false` (keeps failed records for debugging)
- **reserve_doi:** `false` for testing (no DOI assigned - safer for tests)

### Step 4.2: Understanding the `reserve_doi` Setting

⚠️ **IMPORTANT: DOI Reservation Control**

The `reserve_doi` setting controls whether Zenodo assigns a permanent DOI (Digital Object Identifier) to your records.

**`reserve_doi: false` (RECOMMENDED FOR TESTING)**
- ✅ No DOI assigned to draft records
- ✅ Safer for test uploads
- ✅ Drafts can be deleted without wasting DOIs
- ✅ No placeholder DOI shown in record
- ⚠️ Record will NOT have a DOI until you manually reserve one in Zenodo UI

**`reserve_doi: true` (FOR PRODUCTION USE)**
- 🔗 Zenodo assigns a DOI immediately when draft is created
- 📌 DOI is permanent and cannot be deleted (only new versions can be created)
- ✅ DOI can be cited before publication
- ⚠️ **Use only for records you intend to publish**

**Example output during upload:**

With `reserve_doi: false`:
```
🔗 DOI reservation: DISABLED (no DOI will be assigned)
```

With `reserve_doi: true`:
```
🔗 DOI reservation: ENABLED (Zenodo will assign a DOI)
```

**When to use each setting:**

| Scenario | Setting | Reason |
|----------|---------|--------|
| Testing uploads | `false` | Can delete drafts freely |
| Practice runs | `false` | Doesn't waste DOIs |
| Real datasets (pre-publication) | `true` | Get DOI for citations |
| Production uploads | `true` | Professional publishing |

---

## Part 5: Upload to Zenodo (DRY RUN)

### Step 5.1: Test Upload (No actual upload)

```bash
cd /path/to/ESCSP-AZUS

# Make sure credentials are loaded
source set_env.sh

# Run dry-run
python standalone_upload.py --config config_test.json --dry-run
```

**What to expect:**
```
📋 Loading configuration from: config_test.json
✅ Zenodo credentials loaded from environment

======================================================================
AZUS STANDALONE UPLOAD
======================================================================
Configuration file: config_test.json
Annular directory: Not configured
Total directory: /Users/you/AZUS_Test_Workspace/Staging_Area
Auto-publish: False
Delete failures: False
Reserve DOI: False
======================================================================

🔍 DRY RUN MODE - No uploads will be performed

======================================================================
VALIDATING CSV FILES
======================================================================

📋 Checking total CSV: test_collectors.csv
   ✅ Valid - 1 records

======================================================================
PROCESSING TOTAL ECLIPSE DATA
======================================================================

📦 Found 1 dataset(s) to process

Dataset 1/1: ESID_999
  ZIP file:    ESID_999.zip (0.15 MB)
  README:      README.html (0.01 MB)
  Files:       file_list.csv, total_eclipse_data.csv, README.md
  
✅ DRY RUN COMPLETE - No files were uploaded
```

**Common errors at this stage:**
- ❌ "README.html not found" → Run prepare_dataset.py again
- ❌ "Credentials not found" → Run `source set_env.sh`
- ❌ "ESID not in CSV" → Check test_collectors.csv has ESID 999

---

## Part 6: Upload to Zenodo (REAL UPLOAD)

### Step 6.1: Perform Actual Upload

⚠️ **This will create a real record on Zenodo Sandbox**

```bash
cd /path/to/ESCSP-AZUS

# Make sure credentials are loaded
source set_env.sh

# Run actual upload
python standalone_upload.py --config config_test.json
```

**Expected output:**
```
📋 Loading configuration from: config_test.json
✅ Zenodo credentials loaded from environment

======================================================================
AZUS STANDALONE UPLOAD
======================================================================
...

======================================================================
PROCESSING TOTAL ECLIPSE DATA
======================================================================

📦 Processing 1/1: ESID 999

🚀 Starting upload for ESID 999
   ZIP file: ESID_999.zip
   Total files: 5

✅ Using description from README.html: .../README.html
✅ Loaded 2 related identifier(s) from related_identifiers.csv
✅ Loaded 1 reference(s) from references.csv
🔗 DOI reservation: DISABLED (no DOI will be assigned)

📤 Uploading to Zenodo...
   Creating draft record...
   ✅ Draft created: https://sandbox.zenodo.org/records/1234567

   Uploading files...
   📤 Uploading ESID_999.zip (0.15 MB)...
      ✅ Uploaded (0.15 MB)
   📤 Uploading README.md (0.01 MB)...
      ✅ Uploaded (0.01 MB)
   📤 Uploading file_list.csv (0.00 MB)...
      ✅ Uploaded (0.00 MB)
   
   ✅ All files uploaded successfully

   Updating metadata...
   ✅ Metadata updated

✅ Upload successful for ESID 999

======================================================================
UPLOAD SUMMARY
======================================================================
Total processed: 1
✅ Successful:   1
❌ Failed:       0
⏭️  Skipped:      0
======================================================================

✅ 1 upload(s) successful
   Results saved to: .../successful_results.csv
```

### Step 6.2: Verify Upload Results

**Check results CSV:**
```bash
cat ~/AZUS_Test_Workspace/Records/successful_results.csv
```

**Output:**
```
esid,zenodo_id,zenodo_url,zip_file,uploaded_at
999,1234567,https://sandbox.zenodo.org/records/1234567,ESID_999.zip,2026-02-22T21:00:00
```

---

## Part 7: Review on Zenodo

### Step 7.1: View Draft Record

1. **Copy the Zenodo URL** from the output (e.g., `https://sandbox.zenodo.org/records/1234567`)
2. **Open in browser**
3. **Log in to Zenodo Sandbox** (if not already logged in)

**What you should see:**
- ✅ Record is in DRAFT status (not published)
- ✅ Title: "2024-04-08 Total Solar Eclipse ESID#999"
- ✅ Description: HTML from README.html
- ✅ Files: ESID_999.zip, README.md, file_list.csv, etc.
- ✅ Metadata: Creators, funding, license, dates
- ✅ Related works: Citations from CSV
- ✅ References: Bibliography from CSV

### Step 7.2: Check Draft Record Details

**Click on the draft record to verify:**

**Files Section:**
- [ ] ESID_999.zip (contains all WAV files + CONFIG.TXT)
- [ ] README.md
- [ ] file_list.csv
- [ ] total_eclipse_data.csv
- [ ] All data dictionaries
- [ ] License.txt

**Metadata:**
- [ ] Title: Correct format
- [ ] Creators: ARISA Lab, etc.
- [ ] Funding: NASA Award No. 80NSSC21M0008
- [ ] License: CC-BY-4.0
- [ ] Keywords/Subjects: Listed correctly

**Related Works:**
- [ ] Citation to Eclipse Soundscapes paper (DOI)
- [ ] Link to eclipsesoundscapes.org

**References:**
- [ ] Full bibliographic citations

---

## Part 8: Publish or Delete

### Option A: Publish the Record (Makes it public)

⚠️ **Once published, records CANNOT be deleted, only new versions created**

**In Zenodo web interface:**
1. Click "Publish" button
2. Confirm publication
3. Record gets a permanent DOI
4. Record becomes publicly accessible

**Or via AZUS (if auto_publish was true):**
```bash
# Set auto_publish: true in config_test.json
# Then run upload again
python standalone_upload.py --config config_test.json
```

### Option B: Delete Draft Record (Recommended for tests)

**In Zenodo web interface:**
1. Click "Delete" button
2. Confirm deletion
3. Draft is permanently removed

**This is recommended for test uploads** - keeps your account clean.

---

## Part 9: Troubleshooting

### Common Issues

#### Issue 1: "README.html not found"

**Error:**
```
FileNotFoundError: README.html file not found at: .../README.html
Please run prepare_dataset.py to generate README.html before uploading.
```

**Solution:**
```bash
# Run prepare_dataset.py again
python prepare_dataset.py ~/AZUS_Test_Workspace/Raw_Data/ESID_999 \
  --collector-csv ~/AZUS_Test_Workspace/Resources/test_collectors.csv \
  --eclipse-type total \
  --output-dir ~/AZUS_Test_Workspace/Staging_Area/ESID_999
```

#### Issue 2: "Zenodo credentials not found"

**Error:**
```
❌ INVENIO_RDM_ACCESS_TOKEN not set
   Please set INVENIO_RDM_ACCESS_TOKEN and INVENIO_RDM_BASE_URL
   Run: source set_env.sh
```

**Solution:**
```bash
# Load credentials
source set_env.sh

# Verify
echo $INVENIO_RDM_ACCESS_TOKEN
```

#### Issue 3: "ESID not found in CSV"

**Error:**
```
❌ ESID 999 not found in collector CSV
```

**Solution:**
```bash
# Check CSV has ESID 999
grep "^999," ~/AZUS_Test_Workspace/Resources/test_collectors.csv

# If not found, add it or fix the ESID
```

#### Issue 4: "Invalid CSV format"

**Error:**
```
⚠️  CSV validation failed!
Missing required column: ESID
```

**Solution:**
```bash
# Check CSV headers
head -1 ~/AZUS_Test_Workspace/Resources/test_collectors.csv

# Should be:
# ESID,Latitude,Longitude,Local Eclipse Type,...

# Fix headers to match expected format
```

#### Issue 5: "API authentication failed"

**Error:**
```
❌ Zenodo API error: 401 Unauthorized
```

**Solutions:**
1. Check token is correct in set_env.sh
2. Token might be expired - generate new one
3. Check INVENIO_RDM_BASE_URL is correct
4. Verify token has deposit:write scope

---

## Part 10: Cleanup

### After Testing

```bash
# Remove test workspace (optional)
rm -rf ~/AZUS_Test_Workspace

# Delete draft on Zenodo (via web interface)
# or leave it as a test record

# Deactivate virtual environment
deactivate
```

---

## Summary Checklist

### ✅ Before Upload:
- [ ] Python 3.9+ installed
- [ ] AZUS code downloaded
- [ ] Dependencies installed (`pip install -r requirements.txt`)
- [ ] Zenodo account created
- [ ] API token generated
- [ ] Credentials configured in set_env.sh
- [ ] Test data prepared (WAV files, CONFIG.TXT)
- [ ] Collector CSV created
- [ ] Citations CSV created (optional)

### ✅ Dataset Preparation:
- [ ] Ran prepare_dataset.py successfully
- [ ] ESID_999 folder created in Staging_Area
- [ ] All files present (ZIP, README.html, file_list.csv, etc.)
- [ ] README.html looks correct

### ✅ Upload:
- [ ] config_test.json created and configured
- [ ] Dry-run completed successfully
- [ ] Actual upload completed
- [ ] Draft record created on Zenodo
- [ ] Files uploaded correctly
- [ ] Metadata looks correct

### ✅ Verification:
- [ ] Viewed draft record on Zenodo
- [ ] All files present
- [ ] Description shows correctly
- [ ] Citations appear
- [ ] Metadata complete

### ✅ Cleanup:
- [ ] Deleted draft record (if test)
- [ ] Or published record (if keeping)
- [ ] Saved successful_results.csv for reference

---

## Next Steps

### For Production Use:

1. **Switch to Production Zenodo:**
   ```bash
   # In set_env.sh
   export INVENIO_RDM_BASE_URL="https://zenodo.org/api"
   export INVENIO_RDM_ACCESS_TOKEN="your-production-token"
   ```

2. **Use Real ESIDs:**
   - Replace test ESID 999 with actual ESIDs
   - Use real collector data from Eclipse Soundscapes

3. **Enable DOI Reservation (Recommended for Production):**
   ```json
   "reserve_doi": true
   ```
   ⚠️ This assigns permanent DOIs - only use for real datasets

4. **Enable Auto-Publish (Optional):**
   ```json
   "auto_publish": true
   ```

5. **Batch Upload Multiple Datasets:**
   - Prepare multiple ESID folders
   - All will upload in sequence

---

## Quick Reference Commands

```bash
# Setup
source set_env.sh
source azus-env/bin/activate

# Prepare dataset
python prepare_dataset.py ESID_999_folder \
  --collector-csv collectors.csv \
  --eclipse-type total

# Dry run (test)
python standalone_upload.py --config config.json --dry-run

# Real upload
python standalone_upload.py --config config.json

# Check results
cat Records/successful_results.csv
cat Records/failed_results.csv
```

---

**Guide Version:** 1.0 (Feb 22, 2026)  
**AZUS Version:** Standalone (post-Prefect)  
**Tested On:** Python 3.9+, macOS/Linux

**Questions?** Check the troubleshooting section or review the code documentation.
