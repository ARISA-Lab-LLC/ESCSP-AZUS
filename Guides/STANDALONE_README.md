# AZUS Standalone Uploader

**Upload datasets to Zenodo without requiring a Prefect server**

This standalone version of AZUS provides the same upload functionality as the Prefect-based system but runs directly from the command line without any external dependencies beyond Python libraries.

## Quick Start

### 1. Install Requirements

The standalone version requires the same dependencies as the Prefect version, but you don't need to run a Prefect server:

```bash
# Activate your virtual environment
source prefect-env/bin/activate

# Install dependencies (if not already installed)
pip install -r requirements.txt
pip install requests  # Additional dependency for standalone version
```

### 2. Set Environment Variables

```bash
# Edit set_env.sh with your Zenodo credentials
source set_env.sh

# Verify they're set
echo $INVENIO_RDM_ACCESS_TOKEN
echo $INVENIO_RDM_BASE_URL
```

### 3. Run Upload

```bash
# Dry run to test configuration
python standalone_upload.py --dry-run

# Actual upload
python standalone_upload.py
```

## Features

✅ **No Prefect Server Required** - Runs directly from command line  
✅ **Progress Tracking** - Real-time upload progress with file counts and sizes  
✅ **Duplicate Prevention** - Automatically tracks uploaded files  
✅ **Error Handling** - Comprehensive error reporting and recovery  
✅ **Logging** - All output logged to `azus_upload.log`  
✅ **Interactive Confirmation** - Requires confirmation before uploading  
✅ **Same Configuration** - Uses your existing `config.json`

## Files

### New Standalone Files

1. **standalone_upload.py** - Main entry point
2. **standalone_tasks.py** - Task functions without Prefect decorators
3. **standalone_uploader.py** - Direct Zenodo API communication

### Existing Files (Reused)

- **models/audiomoth.py** - Data models (no changes needed)
- **models/invenio.py** - Zenodo metadata models (no changes needed)
- **config.json** - Configuration file (same format)

## Usage

### Basic Usage

```bash
# Upload all datasets configured in config.json
python standalone_upload.py
```

### Advanced Usage

```bash
# Use a different config file
python standalone_upload.py --config /path/to/custom_config.json

# Dry run (test without uploading)
python standalone_upload.py --dry-run
```

## Configuration

The standalone version uses the same `config.json` format as the Prefect version:

```json
{
  "uploads": {
    "total": {
      "dataset_dir": "/path/to/total_datasets",
      "collectors_csv": "/path/to/total_collectors.csv"
    },
    "annular": {
      "dataset_dir": "/path/to/annular_datasets",
      "collectors_csv": "/path/to/annular_collectors.csv"
    },
    "successful_results_file": "/path/to/successful_results.csv",
    "failure_results_file": "/path/to/failed_results.csv",
    "delete_failures": false,
    "auto_publish": false
  }
}
```

## Upload Tracking

The standalone version automatically tracks uploaded files to prevent duplicates:

- **Tracker File:** `Records/uploaded_files.txt` (created automatically)
- **Location:** Current directory
- **Format:** One file path per line

If you need to re-upload a file, remove its entry from this file.

## Output

### Console Output

Real-time progress with clear status indicators:

```
📋 Loading configuration from: config.json
✅ Zenodo credentials loaded from environment

================================================================
AZUS STANDALONE UPLOAD
================================================================
Configuration file: config.json
Annular directory: /data/annular
Total directory: /data/total
Auto-publish: False
Delete failures: False
================================================================

📂 Loading data collectors from: /data/collectors.csv
✅ Loaded 127 data collector records
📂 Scanning directory: /data/total
✅ Found 50 ZIP files
⏭️  Skipped 20 already uploaded file(s)
✅ Prepared 30 dataset(s) for upload

📦 Processing 1/30: ESID 004
🚀 Starting upload for ESID 004
   ZIP file: ESID_004.zip
   Total files: 12
📤 Uploading to Zenodo...
Creating draft record...
✅ Draft created with ID: 12345
Uploading 12 file(s)...
  [1/12] Uploading ESID_004.zip (245.3 MB)...
  ✅ Uploaded in 45.2s
  [2/12] Uploading README.md (0.05 MB)...
  ✅ Uploaded in 1.2s
...
✅ All files uploaded successfully
✅ Record created as draft (not published)
✅ ESID 004: Upload successful
```

### Log File

Detailed log saved to `azus_upload.log`:

```
2026-02-10 14:23:45,123 - __main__ - INFO - Loading configuration from: config.json
2026-02-10 14:23:45,234 - __main__ - INFO - Zenodo credentials loaded from environment
2026-02-10 14:23:45,345 - __main__ - INFO - Loaded 127 data collector records
...
```

### Result CSVs

Same format as Prefect version:

- **Successful uploads:** `successful_results.csv`
- **Failed uploads:** `failed_results.csv`

## Comparison: Standalone vs Prefect

| Feature | Standalone | Prefect |
|---------|-----------|---------|
| Prefect server required | ❌ No | ✅ Yes |
| Web dashboard | ❌ No | ✅ Yes |
| Upload tracking | ✅ File-based | ✅ Block-based |
| Progress monitoring | ✅ Console | ✅ Dashboard |
| Logging | ✅ File + console | ✅ Prefect logs |
| Configuration | ✅ config.json | ✅ config.json |
| Pause/resume | ❌ No | ✅ Yes |
| Parallel uploads | ❌ Sequential | ✅ Possible |
| Setup complexity | ✅ Simple | ⚠️ Complex |

## Error Handling

### Common Errors

**1. Environment variables not set**
```
❌ INVENIO_RDM_ACCESS_TOKEN not set or still using placeholder
   Please update set_env.sh and run: source set_env.sh
```

**Solution:**
```bash
# Edit set_env.sh with your actual token
nano set_env.sh

# Load environment variables
source set_env.sh
```

**2. Configuration file not found**
```
❌ Configuration file not found: config.json
```

**Solution:**
```bash
# Check current directory
pwd

# Ensure config.json exists
ls -la config.json

# Or specify path explicitly
python standalone_upload.py --config /full/path/to/config.json
```

**3. Network/API errors**
```
❌ Upload failed: HTTP 401: Unauthorized
```

**Solution:**
- Verify your API token is correct
- Check Zenodo is accessible: https://zenodo.org
- Ensure token has upload permissions

**4. File not found errors**
```
❌ ZIP file not found: /path/to/ESID_004.zip
```

**Solution:**
- Verify dataset directory path in config.json
- Check file permissions
- Ensure files exist: `ls -la /path/to/datasets`

## Workflow

The standalone upload workflow:

```
1. Load configuration from config.json
2. Verify Zenodo credentials
3. Initialize upload tracker
4. For each dataset directory:
   a. Scan for ZIP files
   b. Load collector CSV
   c. Match ESIDs with collectors
   d. Find all associated files
   e. For each dataset:
      - Extract recording dates from ZIP
      - Create draft metadata
      - Upload all files to Zenodo
      - Save results
      - Mark as uploaded
5. Display summary statistics
```

## Tips

### Test with One Dataset First

Before uploading all datasets, test with a single one:

1. Create a test directory with one dataset
2. Update config.json to point to test directory
3. Run upload
4. Verify on Zenodo
5. If successful, proceed with full batch

### Monitor Progress

```bash
# Watch log file in real-time
tail -f azus_upload.log

# Check uploaded files
wc -l Records/uploaded_files.txt
```

### Resume After Interruption

If the upload is interrupted:

1. Already uploaded files are tracked in `Records/uploaded_files.txt`
2. Simply run `python standalone_upload.py` again
3. Previously uploaded files will be skipped automatically

### Clean Up Failed Uploads

Failed draft records are automatically deleted if `delete_failures: true` in config.json.

To manually clean up:
1. Go to Zenodo dashboard
2. Navigate to "Uploads"
3. Delete any incomplete drafts

## Troubleshooting

### Upload Stuck

If an upload appears stuck:

1. Check your internet connection
2. Press Ctrl+C to cancel
3. Check Zenodo status: https://status.zenodo.org
4. Re-run the upload (already uploaded files will be skipped)

### Permission Errors

```bash
# Ensure files are readable
chmod 644 /path/to/datasets/*.zip

# Ensure directories are accessible
chmod 755 /path/to/datasets
```

### Memory Issues

If uploading very large files (>1GB):

- Upload runs sequentially to manage memory
- Each file is uploaded then closed before the next
- Monitor with: `top` or `htop`

## Migration from Prefect

To migrate from Prefect-based uploads to standalone:

1. **No data migration needed** - Configuration stays the same
2. **Upload tracking:** Prefect blocks → `Records/uploaded_files.txt`
3. **Logs:** Prefect logs → `azus_upload.log`
4. **Monitoring:** Web dashboard → Console output

You can run both in parallel - they track uploads independently.

## Advanced

### Custom Upload Tracker Location

```python
# Edit standalone_upload.py
tracker = UploadTracker(tracker_file="Records/uploaded_files.txt")
```

### Modify Logging

```python
# Edit standalone_upload.py, logging configuration section
logging.basicConfig(
    level=logging.DEBUG,  # More verbose
    # ... other settings
)
```

### Batch Size Limits

By default, uploads all datasets in sequence. To limit:

```python
# Edit upload_datasets() in standalone_upload.py
# Add limit to for loop:
for i, data in enumerate(annular_upload_data[:10], 1):  # Only first 10
```

## Support

For issues with the standalone uploader:

1. Check `azus_upload.log` for detailed errors
2. Verify configuration with `--dry-run`
3. Test with a single dataset first
4. Ensure environment variables are set
5. Check Zenodo service status

## License

Same license as AZUS main project (see main README.md)

---

**Version:** 1.0  
**Last Updated:** February 10, 2026  
**Tested With:** Python 3.9+, AZUS 2.0
