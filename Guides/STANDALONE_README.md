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
  Zenodo-ready staging package (generates README, ZIPs, manifests, metadata).
  **Default layout (July 2026): one ZIP per recording day** —
  `ESID_NNN_YYYY_MM_DD.zip`, day taken literally from the WAV filename
  prefix (an unset-clock `19700101_…` file groups under `1970_01_01` rather
  than blocking; only a WAV with no 8-digit prefix refuses the prep). Each
  day ZIP holds that day's WAVs + a copy of CONFIG.TXT and nothing else;
  the metadata companions stay standalone on the record; `file_list.csv`
  carries one ZIP row per archive and each WAV row names its archive. The
  dataset version is marked with a trailing `A` (`2024.1.0` → `2024.1.0A`)
  so per-day records are unambiguous. A site whose day count would exceed
  Zenodo's 100-files-per-record cap (~85 days with the standard
  companions) is refused before the first ZIP byte. `--single-zip`
  preserves the legacy one-archive layout with metadata appended inside.
  `standalone_tasks.py` uploads either layout: a per-day folder becomes ONE
  Zenodo record with every day archive attached to it.
- **Resources/prep_all_datasets.py** — batch-prepare every ESID under a
  top-level raw-data folder, in numerical order, skipping any ESID
  already prepared (folder exists in `Staging_Area/` or `Uploaded_Data/`).
  Preps in the per-day layout by default; `--single-zip` is forwarded to
  `prepare_dataset.py` for the legacy layout.
- **Resources/finish_stuck_uploads.py** — scan `Staging_Area/` for ESIDs
  with `upload_state.json` (interrupted uploads) and finish them via
  `standalone_tasks.py --esid <discovered list> --workers N`. With the
  opt-in `--enable-file-by-file --raw-data-dir PATH`, an ESID whose ZIP is
  the only missing file and whose `number_of_tries` has reached
  `--tries-threshold` (default 3) switches to uploading the individual
  WAVs instead of the ZIP. Use `--list-only` first: it is read-only and
  makes no network calls. `--skip-integrity-hash` is forwarded to
  `standalone_tasks.py` and drops only the full ZIP re-hash (a complete read
  of the archive, repeated on every recovery run). `--force` switches an ESID
  immediately, ignoring `--tries-threshold` and skipping the ZIP retry, so it
  can switch on the first failure; the ZIP must still be the sole missing
  file, and switching remains a one-way door.
- **Resources/finish_zip_only_drafts.py** — the same repair, but discovered
  from **Zenodo** instead of from `Staging_Area/`. It lists your account's
  drafts, matches each back to a local ESID, and classifies it: a draft whose
  companions are all committed and whose ZIP is not is `CONVERTIBLE`. This
  finds the ESIDs `finish_stuck_uploads.py` cannot see at all — a production
  scan found 138 staging folders with **no `upload_state.json`**, so their
  drafts were invisible to every recovery tool; this recovers the `record_id`
  from the listing and writes it back. **Read-only by default**: it writes two
  CSV reports (one row per draft, one row per file) and changes nothing.
  `--execute` converts; `--publish` is still OFF even then, so the normal
  outcome is a complete, inspectable DRAFT. `--limit 1` makes the first run a
  canary and `--max-consecutive-failures` stops a batch on a systemic fault
  rather than dragging hundreds of records through a one-way door. Built to be
  re-run: every run re-derives state from Zenodo and disk, so an interrupted
  conversion re-classifies as `RESUMABLE` and continues.
- **Resources/hash_raw_wavs.py** — pre-compute the SHA-512 hashes that the
  file-by-file upload verifies, caching them in a `wav_hashes.csv` inside each
  raw ESID folder. Run it whenever convenient (overnight, before a batch) so
  the upload run never pays for hashing, and so a restarted upload does not
  re-read the dataset. Reuses a cached hash only when size and mtime still
  match, so it cannot weaken the check. `--backfill-md5` also records each
  file's md5, which is what Zenodo verifies uploads against — with it, an
  interrupted file-by-file run confirms an already-committed file from the CSV
  instead of re-reading it. Adding the column never invalidates an existing
  cache.
- **Resources/new_version_upload.py** — publish a re-prepped staging package
  as a NEW VERSION of an already-published record, for the case where the
  published metadata is wrong AND its ZIP is broken. Requires `--esid` and
  `--record-id` explicitly. **Dry-run by default**, and `--publish` is OFF even
  under `--execute` — the normal outcome is an inspectable draft, which is what
  makes the operation reversible right up to the moment you publish. Read the
  dry run's metadata diff before every execute; with no sandbox available it is
  the safety review.
- **Resources/split_oversized_raw_folders.py** — for a site too large for
  Zenodo's 50 GB per-record cap, fill its `ESID#NNN_Part_2_of_2` raw folder
  from `ESID#NNN_Part_1_of_2`: copy the non-WAV companions and move the
  later half of the WAVs so the two halves are close to equal in bytes.
  **Dry-run by default** — nothing moves without `--perform-split`. Safe to
  interrupt: the plan is derived from both folders at once, so `--resume`
  finishes a partial run at the same cut. Remember that each half also
  needs its own row in the collectors spreadsheet (the tool reports whether
  they exist but never edits it).

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

### Skip ESIDs already on Zenodo (`--skip-existing-records`)

For a re-run over a raw-data folder where some sites are already done. Before
any local work, each ESID's intended record title is looked up on Zenodo; if a
record with that title exists, the folder is skipped and the run moves to the
next ESID.

```bash
python standalone_tasks.py --skip-existing-records
```

- **Both drafts and published records count as existing.** A published record
  means the site is finished. An unfinished draft is finished with
  `Resources/finish_stuck_uploads.py`, not by this run — skipping it here does
  not abandon it.
- **Skipped, not failed.** Skipped folders appear in the run summary's
  `Skipped` count and write no row to `failed_results.csv`, which stays a list
  of things that actually need attention. The staging folder is left untouched.
- **It asks Zenodo, not `upload_state.json`.** A folder whose state file was
  lost or hand-deleted is still recognised — that is exactly the folder that
  would otherwise create a duplicate record.
- **The check runs before the integrity gate.** A skipped folder costs one API
  search instead of re-hashing every archive, which is where the time goes on a
  multi-GB site.
- **It fails closed.** If the search cannot be completed (network, auth, an
  unrecognized response), the dataset is FAILED rather than uploaded — the
  point of the flag is not to touch what already exists, so an undeterminable
  answer must not become an upload. Re-run without the flag to upload anyway;
  the uploader's duplicate guard still protects the record.

Without this flag the behaviour is unchanged: an existing draft is **resumed**
(its missing files uploaded) rather than skipped.

Note that the duplicate guard inside the uploader searches again immediately
before creating a draft. That second search is deliberate — it closes the
window between this pre-check and draft creation — so a run with this flag
performs two searches for any ESID that is not skipped.

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

### Two-phase upload for very large ZIPs (`--defer-zip`)

Data ZIPs can reach 40+ GB, and Zenodo only accepts them as a **single
HTTP PUT** (multipart per-part uploads are blocked for regular API tokens).
The longer a single transfer runs, the more likely a connection drop kills
it — and with `--workers 3`, three huge ZIPs share your upload bandwidth,
so each one takes ~3× longer and is ~3× more exposed to failure.

`--defer-zip` splits the work into two phases so the huge transfers can run
one at a time at full bandwidth:

```bash
# ---- PHASE 1 (fast) ----
# Creates each Zenodo record, uploads every file EXCEPT the data ZIP,
# and reserves the DOI. Records are NOT submitted for community review.
# Each dataset folder stays in Staging_Area/ with its upload_state.json —
# exactly the state a "stuck" upload leaves behind.
python standalone_tasks.py --config Resources/config.json --workers 3 --defer-zip

# ---- PHASE 2 (the big transfers) ----
# finish_stuck_uploads.py finds every folder with an upload_state.json,
# resumes each draft, skips the already-committed files, uploads the ZIP,
# THEN submits the record for community review and archives the folder
# to Uploaded_Data/. Use --workers 1 so each ZIP gets the whole pipe.
# Re-run as many times as needed — completed ESIDs are skipped.
python Resources/finish_stuck_uploads.py --workers 1
```

**Why community review is held back in phase 1 (important):** when a
community manager **accepts** a record from the review queue, InvenioRDM
**publishes** it — and published records cannot accept new files. If
phase 1 submitted the ZIP-less record for review and a manager accepted it
before phase 2 ran, the ZIP could never be attached (it would require a
whole new record version with a new DOI). So `--defer-zip` defers the
review submission too; phase 2 submits it automatically right after the
ZIP is committed.

**What phase 1 does and does not do:**

| Action | Phase 1 (`--defer-zip`) | Phase 2 (`finish_stuck_uploads.py`) |
|---|---|---|
| Create draft record + reserve DOI | ✅ | (already exists — resumed) |
| Upload README, CSVs, manuals, etc. | ✅ | (already committed — skipped) |
| Upload the data ZIP | ❌ deferred | ✅ |
| Submit for community review | ❌ deferred | ✅ after the ZIP commits |
| Append to `uploaded_files.txt` | ❌ | ✅ |
| Row in `successful_results.csv` | ❌ | ✅ |
| Move folder to `Uploaded_Data/` | ❌ stays in `Staging_Area/` | ✅ |
| Summary counter | `Deferred` | `Successful` |

**Worker guidance for the two phases:**

- Phase 1 files are small — `--workers 3` is safe and fast.
- Phase 2 ZIPs are enormous — use `--workers 1` so each transfer gets your
  full upload bandwidth and the shortest possible transfer window. This is
  the whole point of deferring: shorter transfer time = fewer failures.

**Re-running is safe.** Running phase 1 again with `--defer-zip` resumes
each existing draft, skips every committed file, and still holds review
back — it's idempotent. Running phase 2 repeatedly is the designed retry
loop for stubborn ZIPs.

**DOIs.** With `"reserve_doi": true` in `config.json`, the DOI is reserved
during phase 1, right after the draft is created (or located, on a
re-run) — so every deferred record has its DOI immediately. Independent
of that setting, AZUS enforces a hard guarantee: **a DOI is always
reserved immediately before a record is submitted for community review**
(acceptance from the queue publishes the record, so that is the last
reliable moment to get one). Both checks are idempotent — a draft that
already has a DOI is left untouched. If a record was created DOI-less by
an older version of AZUS, the next `finish_stuck_uploads.py` run reserves
its DOI automatically before submitting it for review.

### WAV integrity audit (`Resources/audit_wav_integrity.py`)

When uploads keep failing, first rule out bad source data. This tool
walks every `ESID_NNN` subfolder of the raw-data folder and writes one
CSV row per ESID comparing, side by side:

- **Disk** — the `.WAV` files at the top level of the raw ESID folder:
  count, exact bytes, GB, zero-byte count, and "tiny" count (files
  smaller than `--tiny-threshold` bytes, default 1024 — a WAV header
  alone is 44 bytes, so tiny files contain no real audio).
- **ZIP** — the `.wav` entries recorded inside the matching
  `ESID_NNN.zip`, auto-located in `Staging_Area/ESID_NNN_Staging/`
  first, then `Uploaded_Data/ESID_NNN_Uploaded/`. Only the ZIP index
  is read (no extraction — fast even on 43 GB archives); sizes are the
  uncompressed entry sizes.
- **Match** — `YES` when disk count and bytes equal ZIP count and
  bytes; `NO` with an explanatory note (files missing from the ZIP,
  extra files in the ZIP, or differing byte totals); `N/A` when there
  is no readable ZIP.

```bash
# Report written to wav_integrity_report_YYYYMMDD_HHMMSS.csv in the cwd
python Resources/audit_wav_integrity.py /path/to/Raw_Data

# Name every offending file in the log; stricter tiny cutoff
python Resources/audit_wav_integrity.py /path/to/Raw_Data \
    --verbose --tiny-threshold 4096
```

**How to read it:** zero/tiny WAVs on disk → fix the source recordings;
zero/tiny WAVs only in the ZIP, `Match = NO`, or `ZIP unreadable` →
re-run `prepare_dataset.py` for that ESID before uploading. Exit code
is `1` when any problem is found (`ZIP Not Found` alone is
informational — the ESID may simply not be prepared yet), `0` when
everything is clean.

### Duplicate-record check (`Resources/find_duplicate_records.py`)

If an upload run loses its `upload_state.json` (for example, a re-prep
wipes and rebuilds the staging folder), the next run cannot resume the
existing Zenodo draft and creates a fresh one — same title, two
records. This read-only tool finds those duplicates.

```bash
# Full check: community records + everything under your account,
# INCLUDING drafts (needs the API token)
source Resources/set_env.sh
python Resources/find_duplicate_records.py

# Community-only check — public API, works without any token
python Resources/find_duplicate_records.py --scope community
```

It writes `duplicate_records_report_YYYYMMDD_HHMMSS.csv` (one row per
record in each duplicate group: record ID, status, published flag, DOI,
created date, URL) and reports two group types:

- **exact-title** — identical titles (case/whitespace-insensitive) on
  records that are NOT versions of one another. Versions of one record
  legitimately share a title and are never flagged.
- **same-esid** — same ESID number in the title but different title
  text: duplicates hidden by a title-template change between runs.

The summary counts how many involved records are unpublished
(drafts/in-review) — those are the safely deletable strays. Published
duplicates need a curation decision instead (published Zenodo records
cannot simply be deleted). The tool itself deletes nothing. Exit code:
`0` clean, `1` duplicates found, `2` usage/auth/API error.

### Upload-state listing (`Resources/list_upload_states.py`)

Lists every `upload_state.json` in `Staging_Area/` and `Uploaded_Data/`
in one CSV — the local record of WHICH Zenodo record each ESID belongs
to:

```bash
python Resources/list_upload_states.py
# -> upload_states_report_YYYYMMDD_HHMMSS.csv in the cwd (--output to override)
```

Columns: `ESID#`, `Location` (Staging/Uploaded), `Folder`, `Record ID`,
`Zenodo URL`, `State Created`, `Resumed`, `Notes`. Rows in
`Staging_Area/` are drafts with incomplete uploads (stuck or deferred);
rows in `Uploaded_Data/` are completed uploads. ESID folders without a
state file are counted in the log (they simply haven't uploaded yet)
but get no CSV row. Unreadable state files and ones missing a
`record_id` are flagged in `Notes` and make the tool exit `1`.

Use it together with `find_duplicate_records.py`: a Zenodo record
whose id appears in the duplicate report but NOT in this listing is a
stray that no local folder claims.

### Duplicate prevention (three layers)

A staging folder's link to its Zenodo draft lives in
`upload_state.json`. Historically, losing that link meant the next run
created a fresh draft — a duplicate record. Three layers now prevent
that:

1. **Re-prep preserves the link** — when `prepare_dataset.py` replaces
   an existing staging folder, it now carries `upload_state.json` and
   `ESID_XXX_request_log.json` over into the rebuilt folder instead of
   destroying them. *Caveat:* resuming a preserved draft does not
   re-send record metadata — if the re-prep changed the README or
   metadata, fix the record description in the Zenodo web UI after the
   upload completes.
2. **Request-log fallback** — if `upload_state.json` is missing or
   unreadable but the folder's `ESID_XXX_request_log.json` (written at
   draft creation) still holds the `record_id`, the run resumes that
   draft and rewrites the state file automatically.
3. **Title guard (last line of defense)** — before creating any fresh
   draft, the uploader searches your account for a record with the
   same title (exact match after trimming whitespace and case):
   - existing **draft** with that title → it is *resumed* instead of
     duplicated (logged loudly as `DUPLICATE GUARD`);
   - existing **published** record → the dataset **fails** with a
     `DuplicateTitleError` instead of creating a duplicate — the folder
     stays in `Staging_Area/` for review;
   - search failure → the dataset fails (fail-closed: a failed run is
     retryable, a duplicate record is permanent).
   Disable per run with `--skip-title-guard` — only for the rare case
   where a second record with an identical title is truly intended.
   Resumed uploads (including everything `finish_stuck_uploads.py`
   does) bypass the guard naturally, since they already know their
   record id.

### Missing-state diagnosis (`Resources/diagnose_missing_states.py`)

`finish_stuck_uploads.py` only attempts folders that HAVE
`upload_state.json` — folders without one are skipped at DEBUG level,
i.e. **silently excluded from every recovery run**. This tool explains
why each no-state folder ended up that way:

```bash
# Read-only diagnosis (add --log azus_upload.log to quote log evidence)
python Resources/diagnose_missing_states.py --log azus_upload.log

# Healer: where a surviving ESID_XXX_request_log.json still holds the
# draft's record_id, re-create upload_state.json pointing at it
python Resources/diagnose_missing_states.py --restore-states
```

Probable causes it distinguishes (most→least specific): draft exists on
Zenodo but the state file was lost (request log holds the record id —
**restore it, or the next run creates a duplicate record**); attempt
reached the upload phase but draft creation failed; folder re-prepped
after a successful upload (record already on Zenodo!); no ZIP in the
folder (skipped with zero logging); tracker skip
(`Records/uploaded_files.txt`); no collectors-CSV row; pre-draft
failure recorded in `failed_results.csv`; or no evidence of any
attempt (probably never covered by your `--esid` filters).

Output: `missing_state_diagnosis_YYYYMMDD_HHMMSS.csv` with the full
evidence per folder (ZIP/tracker/collector/artifact flags, prep
sentinel mtime, latest failure message) plus Probable Cause and
Suggested Action columns. `--restore-states` never overwrites an
existing state file. Exit `0` = every folder has a state file, `1` =
folders diagnosed, `2` = usage error.

### Content audit (`Resources/audit_prep_completeness.py`)

`prep_all_datasets.py`'s skip check is intentionally cheap: it trusts
the `.prep_complete` sentinel as a binary "did prep finish?" flag.
That's perfect for the day-to-day batch workflow.

But two cases call for something deeper:

1. **Legacy folders that predate the sentinel.** They're (probably)
   complete, they just don't have the marker. We want to verify the
   contents and then back-fill the sentinel so the cheap skip works
   from then on.
2. **Silent corruption.** A bug, a manual edit, or a partial filesystem
   write could leave a folder with the sentinel but missing files.
   The cheap skip wouldn't catch it.

`audit_prep_completeness.py` handles both. It walks raw ESID folders,
finds the matching `Staging_Area/` or `Uploaded_Data/` entry, and
runs a deep audit:

- Expected folder contents derived from the `prepare_dataset.py`
  hardcoded outputs + `Resources/resource_files_list.csv` companions.
- Expected ZIP contents derived from raw WAVs + raw `CONFIG.TXT` +
  the staging metadata + the same companions.
- ZIP introspection via `unzip -l` (so the result matches what a
  human sees on the shell).

For each ESID it emits one row in a 4-column CSV:

| Column | Value |
|---|---|
| `ESID#` | zero-padded 3-digit (`007`, `073`) |
| `Staging Area` | basename of the matching folder, or empty |
| `Uploaded Data` | basename of the matching folder, or empty |
| `Prep Completed` | `Yes` / `No` / `Ambiguous` |

The trichotomy:

- **Yes** — every required file is in the folder and in the ZIP.
- **No** — at least one required file is missing (or the ZIP itself
  is missing — that's unambiguously incomplete).
- **Ambiguous** — `unzip -l` failed (corrupt or missing utility);
  `resource_files_list.csv` couldn't be read; the conditional
  `related_identifiers.csv` is missing (could be intentional —
  depends on site Keywords); or `CONFIG.TXT` is absent from both
  raw and ZIP (could be intentional for sites without a real device
  config).

**Self-healing back-fill.** When the deep audit returns `Yes`, the
script touches `.prep_complete` inside that folder. The next time
`prep_all_datasets.py` runs, the cheap sentinel skip will fire for
that ESID — no need to re-audit it. This is the legacy-folder
migration path: one audit run brings the whole repo into the
sentinel world.

**Default vs. `--audit-all`.** By default the audit takes a fast
path: if `.prep_complete` is already present, it trusts the sentinel
and returns `Yes` without inspecting contents. Pass `--audit-all`
to ignore the sentinel and deep-audit every folder — that's the
"detect drift" mode.

**Usage:**

```bash
# Default: vet pre-sentinel folders + back-fill sentinels
python Resources/audit_prep_completeness.py /path/to/Raw_Data

# Verbose per-ESID details (every missing file enumerated)
python Resources/audit_prep_completeness.py /path/to/Raw_Data --verbose

# Force-audit every folder regardless of sentinel (drift check)
python Resources/audit_prep_completeness.py /path/to/Raw_Data --audit-all

# Custom output path
python Resources/audit_prep_completeness.py /path/to/Raw_Data --output /tmp/report.csv
```

The CSV is written to the current working directory by default
(`prep_completeness_report_YYYYMMDD_HHMMSS.csv`). Exit code is `0`
if no `No` rows were recorded, `1` otherwise — convenient for CI.

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
- 3 attempts by default, **30s / 90s** backoff between attempts.
- Catches `SSLError`, `SSLEOFError`, `ConnectionError`, `Timeout`,
  `ChunkedEncodingError`, and HTTP **5xx**.
- HTTP **4xx** fails immediately.
- **Override per run** with `--upload-attempts N` on both
  `standalone_tasks.py` and `Resources/finish_stuck_uploads.py`
  (valid range `1`–`3`, default `3`).  `N=1` = one shot per file, no
  retry — useful when you'd rather fail fast on a bad file and rely on
  a later `finish_stuck_uploads.py` run (the ESID-level retry loop) to
  come back to it, instead of burning up to two minutes of per-file
  backoff inside the current run.  The flag only affects PUTs; the
  metadata-GET retry policy below is untouched.

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
