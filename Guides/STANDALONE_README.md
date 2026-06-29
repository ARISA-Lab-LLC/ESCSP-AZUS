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
python standalone_tasks.py --dry-run

# Actual upload
python standalone_tasks.py
```

## Features

✅ **No Prefect Server Required** - Runs directly from command line  
✅ **Progress Tracking** - Real-time upload progress with file counts and sizes  
✅ **Duplicate Prevention** - Automatically tracks uploaded files  
✅ **Auto-Retry on Transport Failure** - Each file PUT is retried up to 3 times with exponential backoff (30s/90s/270s) on SSL/connection drops and HTTP 5xx — survives transient network blips that previously killed multi-hour uploads  
✅ **Resumable Drafts** - If a run fails mid-upload, re-running the script picks up where it left off: skips already-committed files on the same Zenodo draft and only uploads what's missing (state tracked in `upload_state.json` inside the ESID staging folder)  
✅ **Concurrent ESID Uploads** - `--workers N` uploads N ESID datasets at the same time (default: 1, sequential). Files within a single dataset still upload one at a time; only the outer ESID loop is parallelized. See the "Concurrent uploads" section below for guidance.  
✅ **Error Handling** - Comprehensive error reporting and recovery  
✅ **Logging** - All output logged to `azus_upload.log`  
✅ **Interactive Confirmation** - Requires confirmation before uploading  
✅ **Same Configuration** - Uses your existing `config.json`

## Files

### Standalone Code Files (project root)

1. **standalone_tasks.py** — main entry point. Loads config, discovers ESIDs,
   orchestrates uploads (sequential or concurrent via `--workers`), and
   handles all post-upload bookkeeping (tracker, result CSVs, folder move
   to `Uploaded_Data/`). CLI: `--config`, `--esid`, `--workers`, `--dry-run`.
2. **standalone_uploader.py** — direct Zenodo / InvenioRDM API client.
   `upload_to_zenodo()` handles draft create-or-resume, per-file PUT with
   retry, pending-slot cleanup on exhausted retries, community submit,
   publish, and graceful degradation when the `/draft` endpoint is broken.

### Helper Tools (in `Resources/`)

These are convenience wrappers around the main code path:

- **Resources/prepare_dataset.py** — prepare ONE raw ESID folder into a
  Zenodo-ready staging package (generates README, ZIP, manifests, metadata).
- **Resources/prep_all_datasets.py** — batch-prepare every ESID under a
  top-level raw-data folder, in numerical order, skipping any ESID
  already prepared (folder exists in `Staging_Area/` or `Uploaded_Data/`).
- **Resources/finish_stuck_uploads.py** — scan `Staging_Area/` for ESIDs
  with `upload_state.json` (interrupted uploads) and finish them via
  `standalone_tasks.py --esid <discovered list> --workers N`.

### Existing Files (Reused)

- **models/audiomoth.py** — data models (no changes needed)
- **models/invenio.py** — Zenodo metadata models (no changes needed)
- **Resources/config.json** — configuration file (same format)

## Usage

### Basic Usage

```bash
# Upload all datasets configured in config.json
python standalone_tasks.py
```

### Advanced Usage

```bash
# Use a different config file
python standalone_tasks.py --config /path/to/custom_config.json

# Dry run (test without uploading)
python standalone_tasks.py --dry-run
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
| Pause/resume | ✅ Yes (per-draft, via `upload_state.json`) | ✅ Yes |
| Parallel uploads | ✅ Yes (`--workers N`) | ✅ Possible |
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
python standalone_tasks.py --config /full/path/to/config.json
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

AZUS resumes at two levels:

**1. Dataset-level (already-completed records):**
- Successfully completed datasets are tracked in `Records/uploaded_files.txt`.
- Re-running the script skips any dataset already in that ledger.

**2. Within-dataset (partially uploaded drafts):**
- When a draft is created, AZUS writes `upload_state.json` into that ESID's
  staging folder containing the Zenodo `record_id`.
- If the script later dies mid-upload (e.g., the ZIP fails on its third retry),
  the state file stays put.
- On the next run, AZUS reads the state file, fetches the existing draft on
  Zenodo, **skips files that are already committed**, deletes any "pending"
  file slots, re-uploads only the missing pieces, and (if configured) submits
  to community / publishes.
- If you want to abandon a partial draft and start fresh, just delete
  `upload_state.json` from that ESID's staging folder — the next run will
  create a new draft.
- If the saved draft is no longer on Zenodo (e.g., you deleted it from the
  web UI), AZUS logs a warning and falls back to creating a fresh draft
  automatically.

**What you'll see in the log on resume:**
```
Resume requested for draft 20814816
  Resuming draft 20814816 (status=draft state=unsubmitted is_published=False has_review=False)
  Draft has 16 existing file entries:
    - README.html (status=completed, size=12345, checksum=md5:...)
    - ESID_014.zip (status=pending, size=None, checksum=None)
    ...
  Already uploaded, skipping: README.html (status=completed)
  Clearing existing slot for re-upload: ESID_014.zip (status=pending, size=None)
Uploading 1 file(s) (15 already committed)...
```

### Concurrent uploads (`--workers`)

By default, AZUS uploads one ESID dataset at a time. The `--workers N` flag
lets you upload **N datasets concurrently**, which can dramatically reduce
total wall-clock time when you have many ESIDs to ship.

```bash
# Sequential (default — one dataset at a time)
python standalone_tasks.py --config Resources/config.json

# Upload 3 ESID datasets concurrently
python standalone_tasks.py --config Resources/config.json --workers 3

# Combine with --esid to upload just a specific set, concurrently
python standalone_tasks.py --config Resources/config.json --esid 012 014 073 --workers 3
```

**What it does and doesn't parallelize:**

| Layer | Sequential by default? | Parallel with `--workers N`? |
|-------|---|---|
| ESID datasets (outer loop) | ✅ Yes | ✅ N at a time |
| Files within one dataset | ✅ Yes | ❌ Still sequential within one dataset |
| Retries on a single file | ✅ Yes | ❌ Still sequential per file |

Each worker takes one whole ESID dataset (all its files end-to-end — README,
CSVs, data dicts, the ZIP, plus the community submit / publish step) and
finishes it before picking up the next ESID.

**How many workers should I use?**

- **`1` (default):** Safest, most predictable. The original behavior. Use this
  unless you have a clear reason to go higher.
- **`2`–`4`:** Useful when you have many ESIDs to upload and you have plenty
  of upstream bandwidth. Three concurrent 5 MB/s uploads still total 15 MB/s
  — fine for most office/lab connections.
- **`>4`:** Likely diminishing returns. Your upstream bandwidth becomes the
  bottleneck; each worker just gets a smaller slice. Higher worker counts
  also increase the chance of bumping into Zenodo's API rate limits.

**Reading the log with concurrent uploads:**

When `--workers > 1` is in effect, log lines from different ESIDs interleave
in `azus_upload.log`. Every key per-ESID line is tagged with `[ESID XXX]` so
you can follow one dataset:

```
2026-06-24 09:00:00 ... [ESID 012] Starting (dataset 1 of 3)
2026-06-24 09:00:00 ... [ESID 014] Starting (dataset 2 of 3)
2026-06-24 09:00:00 ... [ESID 073] Starting (dataset 3 of 3)
2026-06-24 09:00:01 ... Creating draft record...               # ← from one of them
2026-06-24 09:00:01 ... Creating draft record...               # ← from another
...
2026-06-24 11:25:30 ... [ESID 014] DONE (success)
2026-06-24 11:42:11 ... [ESID 012] DONE (success)
2026-06-24 12:01:55 ... [ESID 073] DONE (success)
```

To follow ESID 012's progress only:
```bash
grep '\[ESID 012\]' azus_upload.log
```

**Reliability notes (read these):**

- One failed ESID never poisons the pool. If `ESID_014` fails, `ESID_012` and
  `ESID_073` keep running. The failure is recorded in `failed_results.csv`
  exactly as in sequential mode, with `upload_state.json` left in place so
  you can re-run that ESID later.
- The retry and resume features apply to **each worker independently**. If
  `ESID_073`'s ZIP PUT hits an SSL drop, that worker retries 3× with
  30s/90s/270s backoff while the others continue uninterrupted.
- The shared files (`Records/uploaded_files.txt`, the result CSVs) are
  written under thread locks, so concurrent workers cannot corrupt them.
- The "Proceed? (yes/no)" confirmation runs **once before any workers
  start** — exactly as in sequential mode.

### Interrupted-preparation detection

`prepare_dataset.py` does not just `shutil.move()` the finished staging
folder into `Staging_Area/` and call it done. The risk being avoided:
if the move is an interruptible cross-filesystem copy (raw data on one
drive, AZUS project root on another), a Ctrl+C / kill / power loss in
the middle could leave a half-populated directory under the final name
`Staging_Area/ESID_NNN_Staging/`. `prep_all_datasets.py`'s `is_dir()`
check alone could not tell that apart from a complete folder, so the
broken ESID would be silently skipped on the next batch run.

Two complementary protections are in place:

**1. Two-phase atomic move (`prepare_dataset.py`).**

```
Phase 1: shutil.move(output_dir, Staging_Area/.ESID_NNN_Staging.partial)
         ↑ slow, possibly cross-filesystem, can be interrupted mid-copy.
         If interrupted: only the hidden ".partial" name exists.
         The final ESID_NNN_Staging name never appears in a partial state.

Phase 2: os.rename(.partial → ESID_NNN_Staging)
         ↑ same filesystem (both inside Staging_Area/), metadata-only,
         atomic by POSIX guarantee.  Cannot be partial.
```

Any stale `.partial/` from a prior interrupted run, and any existing
final `ESID_NNN_Staging/` from a previous prep of the same ESID, are
removed before the new move — so re-prep is fully idempotent.

**2. `.prep_complete` sentinel — written LAST.**

After the move, after the summary banner, after every other action,
`prepare_dataset.py` does:

```python
(staging_folder / ".prep_complete").touch()
```

That `touch()` is literally the last line of `main()`. If the script
is killed at any earlier point, the folder exists without the sentinel.

`prep_all_datasets.already_prepared()` requires BOTH:
1. `Staging_Area/ESID_NNN_Staging/` is a directory, AND
2. `Staging_Area/ESID_NNN_Staging/.prep_complete` is a regular file.

A folder missing the sentinel logs a warning and is re-prepped:

```
WARNING - Found incomplete staging folder (no .prep_complete sentinel):
          .../Staging_Area/ESID_073_Staging — will re-prepare.
```

**`Uploaded_Data/ESID_NNN_Uploaded/` does NOT require the sentinel** —
a folder in `Uploaded_Data/` is the artifact of a successful upload,
which could only have happened against a prep-complete folder in the
first place. The sentinel rides along inside the moved folder, but the
skip check accepts `Uploaded_Data/` folders on existence alone (so you
can restore from backup without forging a sentinel).

**Forcing a re-prep of a specific ESID:**

```bash
rm Staging_Area/ESID_073_Staging/.prep_complete   # gentle nudge — keeps the folder
# OR
rm -rf Staging_Area/ESID_073_Staging/             # full clean re-prep
```

### Automatic retry on transient failures

Two retry policies are in effect — both 3 attempts with backoff, tuned to the
type of call they protect.

**File-PUT retry (long-running multi-GB uploads):**
- 3 attempts, **30s / 90s / 270s** backoff.
- Catches `SSLError`, `SSLEOFError`, `ConnectionError`, `Timeout`,
  `ChunkedEncodingError`, and HTTP **5xx**.
- HTTP **4xx** fails immediately.

Example log:
```
PUT failed for ESID_014.zip (attempt 1/3): SSLEOFError: EOF occurred in
violation of protocol. Retrying in 30s...
```

**Metadata-GET retry (sub-second resume/status calls):**
- 3 attempts, **5s / 15s / 45s** backoff (shorter — these are quick calls).
- Wraps `GET /draft` (`get_draft_record`) and `GET /draft/files`
  (`list_draft_files`).
- Same retry triggers as PUT: transport errors + HTTP 5xx, with 4xx
  failing fast.  404 on `/draft` still returns cleanly (signals
  "draft truly gone" so the script can create a fresh one).

**Pending-slot cleanup on PUT exhaustion:**
If all 3 PUT attempts fail, the script issues a `DELETE` against the
half-uploaded file slot before bailing out.  This leaves the Zenodo
draft in a clean state (committed files + no broken slots), so:

- The `GET /draft` endpoint keeps working for that draft (a pending
  slot has been observed to make Zenodo's full-record serializer
  return HTTP 500 deterministically).
- The next resume run re-initializes the file from scratch instead
  of inheriting a partial slot.

Example log:
```
PUT failed for ESID_014.zip after 3 attempts. Last error: SSLEOFError: ...
Cleaning up pending file slot for ESID_014.zip after exhausted retries...
Pending slot cleaned: ESID_014.zip. The draft remains in clean draft
state; upload_state.json still points to it and a re-run will re-initialize
this file fresh.
```

**Graceful degradation when `/draft` is broken:**
If `GET /draft` keeps returning 5xx even after retries (for example, a
draft inherited from an older buggy version of AZUS that left a pending
slot in place), the resume path now degrades gracefully: it logs a
warning, skips the metadata fetch, and proceeds with the resume using
only `/draft/files` (which uses a different code path on Zenodo's side
and is usually still healthy).  Already-submitted-to-community and
already-published guards default to "not yet" in this mode — correct
for stuck uploads, where neither step has been reached.

After all retries fail, `upload_state.json` remains in the staging
folder; re-run the script (or `finish_stuck_uploads.py`) to pick up
exactly where you left off.

### Clean Up Failed Uploads

Failed draft records are automatically deleted if `delete_failures: true` in config.json.

To manually clean up:
1. Go to Zenodo dashboard
2. Navigate to "Uploads"
3. Delete any incomplete drafts

## Troubleshooting

### Upload Stuck

If an upload appears stuck on a single file PUT:

1. Wait — AZUS now auto-retries up to 3 times (30s/90s/270s backoff) before
   reporting failure. The first dropped connection is not the end.
2. If genuinely stuck (no log activity for many minutes longer than the file
   should take), check your internet connection.
3. Press Ctrl+C to cancel.
4. Check Zenodo status: https://status.zenodo.org
5. Re-run the upload — `upload_state.json` will resume the draft from
   where it stopped; only files not yet committed will be re-uploaded.

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
# Edit standalone_tasks.py
tracker = UploadTracker(tracker_file="Records/uploaded_files.txt")
```

### Modify Logging

```python
# Edit standalone_tasks.py, logging configuration section
logging.basicConfig(
    level=logging.DEBUG,  # More verbose
    # ... other settings
)
```

### Batch Size Limits

By default, uploads all datasets in sequence. To limit:

```python
# Edit upload_datasets() in standalone_tasks.py
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
