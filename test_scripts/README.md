# AZUS Test Scripts

This directory contains validation and testing scripts to help you verify your AZUS installation and datasets before uploading to Zenodo.

## Quick Start

**Run all tests at once:**
```bash
python run_all_tests.py
```

**Or run tests individually** (see below for details).

---

## Available Test Scripts

### 1. test_installation.py
**Purpose:** Verify AZUS installation and configuration

**What it checks:**
- Python version (3.9+)
- Required dependencies installed
- Environment variables set
- config.json exists and is valid
- AZUS files present
- Zenodo API accessible

**Usage:**
```bash
python test_installation.py
```

**When to run:**
- After first installation
- After updating dependencies
- When troubleshooting setup issues

**Example output:**
```
✅ PASS: Python version
✅ PASS: Dependencies
✅ PASS: Environment variables
✅ PASS: Configuration file
✅ PASS: AZUS files
✅ PASS: Zenodo API

Passed: 6/6 checks

✅ ALL CHECKS PASSED!
```

---

### 2. validate_all_datasets.py
**Purpose:** Check all datasets have required files

**What it checks:**
- Each dataset has all 12 required files
- ZIP file exists
- README.html and README.md present
- All data dictionary files present
- Configuration and documentation files present

**Usage:**
```bash
python validate_all_datasets.py /path/to/dataset_directory
```

**Example:**
```bash
# Validate total eclipse datasets
python validate_all_datasets.py /data/total_eclipse_datasets

# Validate annular eclipse datasets
python validate_all_datasets.py /data/annular_eclipse_datasets
```

**When to run:**
- Before any upload
- After generating new datasets
- When datasets are modified

**Example output:**
```
Validating 15 dataset(s)...

✅ VALID:   ESID_004
✅ VALID:   ESID_005
✅ VALID:   ESID_006
...

Valid datasets:   15
Invalid datasets: 0

✅ All datasets are valid and ready for upload!
```

---

### 3. test_file_discovery.py
**Purpose:** Test file discovery for a specific dataset

**What it checks:**
- All required files can be found
- File sizes are reasonable
- Files are readable

**Usage:**
```bash
python test_file_discovery.py /path/to/ESID_XXX.zip
```

**Example:**
```bash
python test_file_discovery.py /data/total/ESID_004.zip
```

**When to run:**
- When debugging why a dataset upload fails
- To verify file discovery logic works
- To check a specific problematic dataset

**Example output:**
```
✅ FOUND (11 files):
   ✓ README.html                                  (   125.4 KB)
   ✓ README.md                                    (    45.2 KB)
   ✓ file_list.csv                                (    12.8 KB)
   ...

Found:   11/11 files

✅ SUCCESS: All required files found!
```

---

### 4. test_readme_loading.py
**Purpose:** Verify README.html can be loaded and used

**What it checks:**
- README.html exists and is readable
- File encoding is UTF-8
- Content has expected structure
- File size is reasonable
- Contains expected keywords

**Usage:**
```bash
python test_readme_loading.py /path/to/README.html
```

**Example:**
```bash
python test_readme_loading.py /data/total/ESID_004/README.html
```

**When to run:**
- After generating README files
- When description appears wrong on Zenodo
- To verify README content before upload

**Example output:**
```
✅ Successfully read README.html

📊 File Statistics:
   File size:   128,456 bytes (125.4 KB)
   Characters:  127,892
   Lines:       234

✅ SUCCESS: README.html looks good!

This file will be used as the Zenodo description.
```

---

### 5. test_collector_csv.py
**Purpose:** Validate collector CSV file

**What it checks:**
- CSV can be parsed
- All required columns present
- No duplicate ESIDs
- Valid coordinates (latitude/longitude)
- Valid eclipse coverage values
- All required fields populated

**Usage:**
```bash
python test_collector_csv.py /path/to/collectors.csv [eclipse_type]
```

**Arguments:**
- `eclipse_type`: Either `total` or `annular` (default: `total`)

**Example:**
```bash
# Test total eclipse CSV
python test_collector_csv.py /data/2024_total_info_updated.csv total

# Test annular eclipse CSV
python test_collector_csv.py /data/2023_annular_info.csv annular
```

**When to run:**
- After creating/updating collector CSV
- When getting "No collector found" errors
- To verify CSV format is correct

**Example output:**
```
✅ Successfully parsed CSV file
   Found 127 collector record(s)

📊 Sample Collectors (first 5):
   1. ESID 004:
      Location:  40.7128, -74.0060
      Coverage:  99.5%
      Eclipse:   2024-04-08
      Type:      Total

✅ No duplicates found
✅ All required fields look valid

✅ SUCCESS: CSV file is valid and ready to use!
```

---

### 6. test_single_upload.py
**Purpose:** Perform a complete test upload to Zenodo

**What it does:**
- Finds all files for one dataset
- Loads collector data
- Prepares upload data
- Uploads to Zenodo (creates draft record)
- Shows Zenodo record link

**Usage:**
1. **Edit the configuration** in the script:
   ```python
   test_esid = "004"  # Your test ESID
   zip_file = "/path/to/ESID_004.zip"
   collectors_csv = "/path/to/collectors.csv"
   eclipse_type = EclipseType.TOTAL
   ```

2. **Run the script:**
   ```bash
   python test_single_upload.py
   ```

3. **Follow the interactive prompts**
4. **Verify on Zenodo** using the provided link
5. **Publish or delete** the test record

**When to run:**
- Before batch uploads
- After fixing any issues
- To verify end-to-end workflow

**Important:**
- Creates a REAL record on Zenodo
- Record is NOT auto-published
- You must manually publish or delete it
- Use Zenodo Sandbox for testing

---

### 7. run_all_tests.py
**Purpose:** Run all tests in sequence

**What it does:**
- Runs test_installation.py
- Tests all collector CSV files
- Validates all datasets
- Shows comprehensive summary

**Usage:**
```bash
python run_all_tests.py [config_file]
```

**Example:**
```bash
# Use default config.json
python run_all_tests.py

# Use specific config
python run_all_tests.py /path/to/config.json
```

**When to run:**
- Before starting any uploads
- After making configuration changes
- To get complete status check

**Example output:**
```
Test Results:
  ✅ PASS  Installation
  ✅ PASS  Total CSV
  ✅ PASS  Annular CSV
  ✅ PASS  Total Datasets
  ✅ PASS  Annular Datasets

Overall:
  Passed:  5/5
  Failed:  0/5
  Skipped: 0/5

✅ ALL TESTS PASSED!
```

---

## Recommended Testing Workflow

### Before Your First Upload

```bash
# 1. Check installation
python test_installation.py

# 2. Validate your datasets
python validate_all_datasets.py /path/to/total_datasets
python validate_all_datasets.py /path/to/annular_datasets

# 3. Test collector CSVs
python test_collector_csv.py /path/to/total_info.csv total
python test_collector_csv.py /path/to/annular_info.csv annular

# 4. Test single upload
python test_single_upload.py

# 5. If all pass, proceed with batch upload
```

**Or run all at once:**
```bash
python run_all_tests.py
```

---

## Troubleshooting

### ModuleNotFoundError

**Problem:**
```
ModuleNotFoundError: No module named 'prefect'
```

**Solution:**
```bash
# Make sure virtual environment is activated
source ../prefect-env/bin/activate

# Install dependencies
pip install -r ../requirements.txt
```

---

### File Not Found Errors

**Problem:**
```
❌ Error: ZIP file not found: /path/to/ESID_004.zip
```

**Solution:**
- Check the path is correct
- Use absolute paths, not relative
- Verify file exists: `ls -la /path/to/`

---

### CSV Parsing Errors

**Problem:**
```
ValueError: Expected CSV headers not found
```

**Solution:**
- Check CSV has correct headers (exactly as expected)
- Ensure CSV is UTF-8 encoded
- No extra blank rows at top
- Headers are in first row

---

### Environment Variable Not Set

**Problem:**
```
❌ INVENIO_RDM_ACCESS_TOKEN (not set)
```

**Solution:**
```bash
# Make sure you've loaded environment variables
source ../set_env.sh

# Verify
echo $INVENIO_RDM_ACCESS_TOKEN
```

---

## Tips

✅ **DO:**
- Run tests before uploading
- Fix all issues before proceeding
- Test with one dataset first
- Keep test scripts updated

❌ **DON'T:**
- Skip validation steps
- Upload without testing
- Ignore warnings
- Delete test scripts

---

## Getting Help

If tests fail:

1. **Read the error message** carefully
2. **Check the troubleshooting section** above
3. **Review logs** for details
4. **Fix the issue** and re-run tests
5. **All tests should pass** before uploading

---

## Test Script Summary

| Script | Purpose | Runtime | Required |
|--------|---------|---------|----------|
| test_installation.py | Check AZUS setup | 10s | ✅ Yes |
| validate_all_datasets.py | Check all datasets | 30s | ✅ Yes |
| test_file_discovery.py | Test one dataset | 5s | Optional |
| test_readme_loading.py | Test README.html | 5s | Optional |
| test_collector_csv.py | Test CSV file | 5s | ✅ Yes |
| test_single_upload.py | Test upload to Zenodo | 2-5min | ✅ Yes |
| run_all_tests.py | Run all tests | 1-2min | Recommended |

---

**Version:** 2.0  
**Last Updated:** February 7, 2026  
**Tested With:** Python 3.9+, AZUS 2.0
