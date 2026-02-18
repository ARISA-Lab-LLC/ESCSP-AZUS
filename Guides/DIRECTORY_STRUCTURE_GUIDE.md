# AZUS Directory Structure Guide

## Correct Directory Structure

Your staging area should be organized with each ESID in its own subdirectory:

```
Staging_Area/                           ← Point config.json here
├── ESID_004/
│   ├── ESID_004.zip                    ← ZIP file (WAV files + CONFIG.TXT)
│   ├── ESID_004_to_upload.csv          ← Upload manifest
│   ├── README.html                     ← Used as Zenodo description
│   ├── README.md                       ← Uploaded to Zenodo
│   ├── file_list.csv
│   ├── total_eclipse_data.csv
│   ├── CONFIG.TXT
│   ├── License.txt
│   └── ... (other metadata files)
│
├── ESID_005/
│   ├── ESID_005.zip
│   ├── ESID_005_to_upload.csv
│   ├── README.html
│   ├── README.md
│   └── ... (other files)
│
└── ESID_006/
    ├── ESID_006.zip
    ├── ESID_006_to_upload.csv
    └── ... (other files)
```

## How Each Part Works

### 1. ESID Subdirectories

Each ESID has its own directory:
- **Name format:** `ESID_XXX` or `ESID#XXX`
- **Contains:** All files for that dataset
- **Self-contained:** Everything needed for upload is here

### 2. ZIP File (ESID_XXX.zip)

**Location:** Inside ESID directory  
**Contains:** All WAV audio files + CONFIG.TXT  
**Created by:** `create_upload_package.py`

```
ESID_005/ESID_005.zip contains:
├── 20240408_120000.WAV
├── 20240408_120500.WAV
├── ... (all WAV files)
└── CONFIG.TXT
```

### 3. Upload Manifest (ESID_XXX_to_upload.csv)

**Location:** Inside ESID directory  
**Purpose:** Lists exactly which files to upload to Zenodo  
**Created by:** `create_upload_package.py`

```csv
File Name
ESID_005.zip
README.md
total_eclipse_data.csv
file_list.csv
... (metadata files)
```

**Excludes:**
- Individual WAV files (in the ZIP)
- README.html (used as description)

### 4. Metadata Files

**Location:** Inside ESID directory  
**Purpose:** Documentation, data dictionaries, license  
**Uploaded:** Yes, listed in manifest

Examples:
- `README.md` - Markdown documentation
- `total_eclipse_data.csv` - Site metadata
- `file_list.csv` - Complete file listing
- `License.txt` - CC BY 4.0 license
- Data dictionary CSVs

### 5. README.html

**Location:** Inside ESID directory  
**Purpose:** Used as Zenodo record description  
**Uploaded as file:** No  
**How it's used:** Content becomes description field

## Complete Workflow

### Step 1: Create Raw Dataset Structure

Start with your raw data:

```
Raw_Data/
└── ESID#005/
    ├── 20240408_120000.WAV
    ├── 20240408_120500.WAV
    ├── ... (more WAV files)
    └── CONFIG.TXT
```

### Step 2: Prepare Dataset

Run `prepare_dataset.py`:

```bash
python prepare_dataset.py Raw_Data/ESID#005 \
    --collector-csv collectors.csv \
    --eclipse-type total \
    --output-dir Staging_Area/ESID_005
```

**Creates:**
```
Staging_Area/
└── ESID_005/
    ├── README.html              ← Generated
    ├── README.md                ← Generated
    ├── file_list.csv            ← Generated
    ├── total_eclipse_data.csv   ← Generated
    ├── CONFIG.TXT               ← Copied
    ├── License.txt              ← Copied
    └── ... (data dictionaries)  ← Copied
```

### Step 3: Create Upload Package

Run `create_upload_package.py`:

```bash
cd Staging_Area
python create_upload_package.py . 005
```

**Adds to ESID_005 directory:**
```
Staging_Area/
└── ESID_005/
    ├── ESID_005.zip             ← NEW: ZIP of WAV files
    ├── ESID_005_to_upload.csv   ← NEW: Upload manifest
    ├── README.html
    ├── README.md
    └── ... (all other files)
```

### Step 4: Configure and Upload

**config.json:**
```json
{
  "uploads": {
    "total": {
      "dataset_dir": "/path/to/Staging_Area",
      "collectors_csv": "/path/to/collectors.csv"
    }
  }
}
```

**Upload:**
```bash
python standalone_upload.py
```

**What happens:**
1. Script scans `Staging_Area/`
2. Finds `ESID_005/` subdirectory
3. Finds `ESID_005/ESID_005.zip`
4. Reads `ESID_005/ESID_005_to_upload.csv`
5. Verifies all files exist in `ESID_005/`
6. Uploads files to Zenodo

## File Discovery Logic

### How the Upload Script Finds Files

```python
# 1. Start at Staging_Area (configured in config.json)
data_dir = "/path/to/Staging_Area"

# 2. Find ESID subdirectories
for subdir in data_dir:
    if subdir.name starts with "ESID_" or "ESID#":
        
        # 3. Look for ZIP file inside
        zip_file = subdir / "ESID_XXX.zip"
        
        # 4. Look for manifest in same directory
        manifest = subdir / "ESID_XXX_to_upload.csv"
        
        # 5. Read manifest to get file list
        files_to_upload = read_manifest(manifest)
        
        # 6. Find each file in same directory (subdir)
        for filename in files_to_upload:
            file_path = subdir / filename
            upload(file_path)
```

### Key Points

✅ **ZIP location:** `Staging_Area/ESID_XXX/ESID_XXX.zip`  
✅ **Manifest location:** `Staging_Area/ESID_XXX/ESID_XXX_to_upload.csv`  
✅ **All files:** In same `ESID_XXX/` directory  
✅ **Config points to:** `Staging_Area/` (parent of ESID directories)

## Common Structures (Wrong vs Right)

### ❌ WRONG - ZIP in parent directory

```
Staging_Area/
├── ESID_005.zip              ← Wrong location
├── ESID_005_to_upload.csv    ← Wrong location
└── ESID_005/
    └── ... (other files)
```

**Problem:** Script can't find files in manifest (they're in ESID_005/ subdirectory)

### ❌ WRONG - Flat structure

```
Staging_Area/
├── ESID_005.zip
├── README.md
├── file_list.csv
└── ... (all files mixed)
```

**Problem:** Can't handle multiple datasets

### ✅ RIGHT - Subdirectory structure

```
Staging_Area/
└── ESID_005/
    ├── ESID_005.zip
    ├── ESID_005_to_upload.csv
    └── ... (all files together)
```

**Benefits:**
- Self-contained datasets
- Easy to manage multiple ESIDs
- Clear organization
- Manifest can find all files

## Batch Processing

### Multiple Datasets

```
Staging_Area/
├── ESID_004/
│   ├── ESID_004.zip
│   ├── ESID_004_to_upload.csv
│   └── ... (files)
├── ESID_005/
│   ├── ESID_005.zip
│   ├── ESID_005_to_upload.csv
│   └── ... (files)
└── ESID_006/
    ├── ESID_006.zip
    ├── ESID_006_to_upload.csv
    └── ... (files)
```

**Create all packages:**
```bash
python batch_create_packages.py Staging_Area
```

**Upload all:**
```bash
python standalone_upload.py
```

Script processes each ESID directory automatically!

## Verification Commands

### Check Structure

```bash
# List all ESID directories
ls -d Staging_Area/ESID_*/

# Check each has ZIP and manifest
ls Staging_Area/ESID_*/ESID_*.zip
ls Staging_Area/ESID_*/ESID_*_to_upload.csv

# Check one directory
ls -la Staging_Area/ESID_005/
```

### Verify Manifest

```bash
# View manifest
cat Staging_Area/ESID_005/ESID_005_to_upload.csv

# Count files in manifest
wc -l Staging_Area/ESID_005/ESID_005_to_upload.csv

# Check if all files exist
while read filename; do
    [[ "$filename" == "File Name" ]] && continue
    if [[ -f "Staging_Area/ESID_005/$filename" ]]; then
        echo "✅ $filename"
    else
        echo "❌ $filename"
    fi
done < Staging_Area/ESID_005/ESID_005_to_upload.csv
```

### Verify ZIP Contents

```bash
# List ZIP contents
unzip -l Staging_Area/ESID_005/ESID_005.zip | head -20

# Count files in ZIP
unzip -l Staging_Area/ESID_005/ESID_005.zip | wc -l
```

## Troubleshooting

### Error: No ZIP files found

```
✅ Found 0 ZIP files in ESID subdirectories
⚠️  No new files to upload
```

**Causes:**
1. ZIPs are in wrong location (parent directory)
2. ESID subdirectories named incorrectly
3. ZIP files named incorrectly

**Solution:**
```bash
# Check directory structure
ls -R Staging_Area/

# Look for ZIPs
find Staging_Area/ -name "*.zip"

# Ensure correct structure:
# Staging_Area/ESID_XXX/ESID_XXX.zip
```

### Error: Files not found

```
⚠️  Missing 5 files:
   - README.md
   - file_list.csv
   ...
```

**Cause:** Files listed in manifest don't exist in ESID directory

**Solution:**
```bash
# Check what's actually in the directory
ls -la Staging_Area/ESID_005/

# Regenerate package if needed
python create_upload_package.py Staging_Area 005
```

### Error: Manifest not found

```
ℹ️  No upload manifest found, using default file discovery
```

**Cause:** `ESID_XXX_to_upload.csv` missing from ESID directory

**Solution:**
```bash
# Create the manifest
python create_upload_package.py Staging_Area 005
```

## Best Practices

### 1. Keep Everything Together

✅ All files for one ESID in one directory  
✅ ZIP, manifest, and metadata together  
✅ Easy to move/copy/backup entire dataset

### 2. Use Consistent Naming

✅ Directories: `ESID_004`, `ESID_005`, etc.  
✅ ZIPs: `ESID_004.zip`, `ESID_005.zip`  
✅ Manifests: `ESID_004_to_upload.csv`

### 3. Verify Before Upload

```bash
# Check structure
ls -R Staging_Area/ | head -50

# Verify one dataset
ls -la Staging_Area/ESID_005/
cat Staging_Area/ESID_005/ESID_005_to_upload.csv

# Dry run
python standalone_upload.py --dry-run
```

### 4. Organize Workflow

```
1. Raw_Data/ESID_XXX/          (original WAV files)
2. Staging_Area/ESID_XXX/      (prepared with metadata)
3. Upload to Zenodo            (automatic from staging)
4. Archive/ESID_XXX/           (backup after upload)
```

## Summary

✅ **Each ESID has its own subdirectory**  
✅ **ZIP and manifest inside ESID directory**  
✅ **All files for one dataset together**  
✅ **config.json points to Staging_Area parent**  
✅ **Self-contained and organized**

This structure makes it easy to manage hundreds of datasets!
