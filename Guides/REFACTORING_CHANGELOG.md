# AZUS Refactoring Change Log

## Summary

Transformed AZUS from an Eclipse Soundscapes–specific tool into a **generalizable
citizen science data upload platform**.  All project-specific identity is now in
configuration files, not Python code.

---

## July 2026 — `--upload-attempts`: opt-in override for per-file PUT retries

Adds a `--upload-attempts N` CLI flag to both `standalone_tasks.py` and
`Resources/finish_stuck_uploads.py`.  Range 1–3, **default 3 (unchanged
from previous behavior)** — the flag is purely opt-in.

- `N=1`: one shot per file with no retry.  Useful when the user would
  rather fail fast on a bad file and re-run `finish_stuck_uploads.py`
  (the ESID-level retry loop) later, instead of burning up to two
  minutes of per-file backoff inside the current run.
- `N=3`: historical behavior — 3 attempts with 30s/90s backoffs, same
  pending-slot cleanup on exhaustion.
- Only file uploads (PUTs) are affected.  Metadata GET retries
  (`_API_RETRY_ATTEMPTS = 3`, `5s/15s/45s` backoff) are untouched.

### Changes

- `standalone_uploader.py` — three signatures gain an `upload_attempts`
  keyword parameter defaulting to the existing `_PUT_RETRY_ATTEMPTS`
  constant, forwarded down to `_put_file_content_with_retry`.  The
  constant itself is unchanged.
- `standalone_tasks.py` — new `--upload-attempts` argparse flag with
  1–3 validation at parse time; threaded through the four-function
  plumbing (`upload_datasets` → `_process_one_dataset` →
  `upload_dataset` → `upload_to_zenodo`).  A one-line config-banner
  entry appears only when the value differs from the default, so
  no-arg runs stay quiet.
- `Resources/finish_stuck_uploads.py` — matching `--upload-attempts`
  flag with the same validation; forwarded to the shelled-out
  `standalone_tasks.py --upload-attempts N` call.

---

## July 2026 — DOI reservation: fix silent drop + guarantee before review

DOIs were never being reserved, for three stacked reasons:

1. **Dormant bug:** `get_draft_config()` built the reservation payload
   (`pids.doi.provider = "datacite"`) and `save_metadata_json()` wrote it
   to the local audit JSON — making it *look* sent — but
   `upload_to_zenodo()` never included `pids` in the draft-creation
   request. The reservation never reached Zenodo.
2. The resume path (`finish_stuck_uploads.py` → `--esid` re-run) never
   touches draft metadata at all, so existing DOI-less drafts stayed
   DOI-less.
3. `config.json` shipped with `"reserve_doi": false`.

### Fixes

- **New `ensure_doi_reserved()` in `standalone_uploader.py`** —
  idempotent check-then-reserve using the official InvenioRDM endpoint
  (`POST /records/{id}/draft/pids/doi`, the same call as Zenodo's
  "Get a DOI now" button). Re-fetches the draft if metadata is stale,
  no-ops when a DOI exists, treats HTTP 400 "already exists" as success,
  and raises on real failures so the dataset is marked failed and
  retryable rather than proceeding DOI-less.
- **Two call sites in `upload_to_zenodo()`:**
  1. Right after the draft is created/located (guarded by `reserve_doi`)
     — a `--defer-zip` phase-1 run now yields its DOI immediately.
  2. Unconditionally, immediately before community-review submission —
     the hard guarantee: acceptance from the queue publishes the record,
     so no record may enter review without a DOI. This also heals every
     DOI-less draft created by older AZUS versions on its next
     `finish_stuck_uploads.py` run, with no changes to that script.
- **Draft creation now sends `pids`** when reservation is requested
  (the original one-line omission).
- **`Resources/config.json`: `"reserve_doi": true`.** Remember to flip
  this on the production server's copy too — config.json is gitignored.

---

## July 2026 — `--defer-zip`: two-phase upload for very large ZIPs

Data ZIPs (up to 43 GB) fail at a high rate because Zenodo only accepts
them as a single HTTP PUT, and concurrent workers split the upload
bandwidth — making each huge transfer slower and more exposed to
connection drops.

### New flag: `standalone_tasks.py --defer-zip`

Phase 1 creates each record, uploads every file EXCEPT the data ZIP,
reserves the DOI, and **holds back the community-review submission**.
The dataset folder stays in `Staging_Area/` with its `upload_state.json`
— deliberately identical to the state a "stuck" upload leaves behind.

Phase 2 is the existing recovery tool, unchanged:
`python Resources/finish_stuck_uploads.py --workers 1` resumes each
draft, skips committed files, uploads the ZIP at full bandwidth (one at
a time), submits the record for review, and archives the folder to
`Uploaded_Data/`.

### Why review submission is deferred

Accepting a record from the community review queue **publishes** it, and
published records cannot accept new files. If phase 1 submitted the
ZIP-less record and a manager accepted it before phase 2 ran, the ZIP
could never be attached without a new version + new DOI. Deferral closes
that race; phase 2 submits review right after the ZIP commits.

### Changes

- `standalone_uploader.py` — `upload_to_zenodo()` gained
  `submit_review: bool = True`; the community-submission block is now
  guarded by it, with a "DEFERRED" log line when held back.
- `standalone_tasks.py` —
  - New `--defer-zip` CLI flag (help text written for non-programmers).
  - `upload_dataset()` filters the ZIP out of the upload list and passes
    `submit_review=False` when deferring.
  - `_process_one_dataset()` counts deferred successes under a new
    `deferred` stat and deliberately skips the tracker append, the
    success-CSV row, and the move to `Uploaded_Data/` (the record is not
    complete yet).
  - Summary banner shows the `Deferred` count and the exact
    `finish_stuck_uploads.py` command to finish.
- `Resources/finish_stuck_uploads.py` — **no changes needed**; deferred
  folders are indistinguishable from stuck ones by design.
- Docs: `README.md` (Upload Resilience bullet + Quick Start step 7c),
  `Guides/STANDALONE_README.md` (full two-phase section with a
  phase-by-phase table and worker guidance).

---

## June 2026 — Resilient resume: clean up pending slots + tolerate broken /draft

Two related issues:

**(1) Pending file slot from failed upload corrupts the draft for `/draft` GET.**
After a file PUT exhausted all 3 retries, the file entry was left in
`"status": "pending"` on Zenodo.  Multiple stuck drafts in this state
were observed to cause `GET /api/records/{id}/draft` to return
`HTTP 500: 'server is overloaded or there is an error in the application'`
(deterministically, not transiently).  The web UI still showed the
drafts and `GET /draft/files` continued to work fine — only the
full-record serializer choked.

**(2) `get_draft_record` and `list_draft_files` had no retry.**
A single transient 5xx during resume aborted the whole run, even
though the underlying issue was transient.

### Fixes (single file: `standalone_uploader.py`)

**Clean up pending file slot on exhausted PUT retries.**
`upload_file_to_draft` now wraps the call to
`_put_file_content_with_retry`.  If it raises after 3 attempts:

- Logs the cleanup.
- Issues `DELETE /api/records/{id}/draft/files/{key}` to remove the
  broken slot.
- Logs success or, if cleanup itself fails, a warning.
- Re-raises the original PUT exception so the dataset is correctly
  marked as failed.

Result: drafts that fail at the upload step are left in a clean state
(committed files + no pending slots).  `upload_state.json` is
untouched (it was written immediately after draft creation), so the
next resume run picks up cleanly.

**Retry on metadata GETs.**
New constants `_API_RETRY_ATTEMPTS = 3`, `_API_RETRY_BACKOFF_S = (5, 15, 45)`.
New helper `_api_get_with_retry` wraps `get_draft_record` and
`list_draft_files`.  Retries on `RequestException` and HTTP 5xx; HTTP
4xx still fails fast.  `get_draft_record` uses `allow_404=True` so a
404 still returns `None` (signaling "draft truly gone").

**Graceful degradation when `/draft` is broken.**
`upload_to_zenodo` now distinguishes three outcomes from
`get_draft_record`:

| Outcome                     | What it means                  | What happens                                  |
|-----------------------------|--------------------------------|-----------------------------------------------|
| Returns dict                | 200, draft is healthy          | Normal resume (existing behavior)             |
| Returns `None`              | 404, draft truly gone          | Fall back to creating fresh draft             |
| Raises (4xx / 5xx exhausted)| `/draft` broken, draft alive   | **NEW:** Resume with `draft_response = None`  |

In the new third case the script logs a warning and continues.  The
downstream code that touches `draft_response` was already defensive
(`bool(draft_response and ...)`), so the no-metadata path is naturally
handled: the already-submitted / already-published guards default to
`False`, which for a stuck upload is the correct assumption (those
steps come *after* file uploads).

### Why this recovers the existing stuck drafts

The user's stuck drafts each had ~15 committed files + 1 pending ZIP slot.
That pending slot was the cause of the 500.  With these changes, the
recovery flow becomes:

1. `finish_stuck_uploads.py` discovers stuck ESIDs via `upload_state.json`.
2. `standalone_tasks.py --esid <list>` resumes each.
3. `get_draft_record` returns 500 → graceful degradation logs the warning
   and proceeds with `is_resume=True` and `draft_response=None`.
4. `list_draft_files` returns 200 → script sees 15 completed entries +
   1 pending entry.
5. The pending entry is deleted (existing resume logic), then re-initialized,
   uploaded fresh with 3 retries.
6. If that PUT also exhausts retries, the new cleanup deletes the slot
   before propagating the failure — so the draft stays clean for the
   *next* retry.

### Files touched
- `standalone_uploader.py`

---

## June 2026 — Content audit + legacy sentinel back-fill (`Resources/audit_prep_completeness.py`)

A new tool that vets every prepared `Staging_Area/` and `Uploaded_Data/`
folder by its actual contents — folder files plus `unzip -l` of the
ZIP — against the truth set `prepare_dataset.py` would have produced.

### Primary purpose: legacy migration

The `.prep_complete` sentinel introduced earlier in this changelog is
the marker `prep_all_datasets.py` trusts for fast skip decisions.
Folders prepared before that change exist without the marker.  This
tool's primary job is to vet them by content and **back-fill the
sentinel on any folder it confirms as `Yes`** — bringing the whole
repository into the sentinel world without manual `touch`-ing.

### Secondary purpose: drift detection

Use `--audit-all` to ignore the sentinel fast-path and deep-audit
every folder — useful for catching folders that got corrupted after
the sentinel was written (manual edits, partial filesystem writes,
bugs).

### Behavior

For each ESID found in the raw-data folder, the tool:

1. Locates the matching prepared folder under `Staging_Area/` (preferred)
   or `Uploaded_Data/`.
2. Fast path: if `.prep_complete` exists and `--audit-all` is not set,
   marks `Yes` immediately.
3. Deep path: lists ZIP contents via `unzip -l`, compares folder
   basenames against `_HARDCODED_STAGING_FILES + resource_files_list.csv
   companions + conditional files`, compares ZIP basenames against
   `_HARDCODED_ZIP_ENTRIES + raw WAVs + raw CONFIG.TXT + companions +
   conditional files`.
4. Status decision (first match wins):
   - Fast-path sentinel → `Yes`
   - ZIP file missing → `No` (unambiguous)
   - `resource_files_list.csv` unreadable / `unzip -l` failed → `Ambiguous`
   - Required Set A or Set B miss → `No`
   - Only `related_identifiers.csv` missing → `Ambiguous` (depends on Keywords)
   - `CONFIG.TXT` absent from both raw and ZIP → `Ambiguous`
   - Otherwise → `Yes`
5. On `Yes`, `touch()` `.prep_complete` in the folder (back-fill,
   idempotent).

### Output

A 4-column CSV: `ESID#, Staging Area, Uploaded Data, Prep Completed`,
written to the current working directory with a timestamped filename
(`prep_completeness_report_YYYYMMDD_HHMMSS.csv`).  Exit code `0` if
no `No` rows, `1` otherwise.

### CLI

```
python Resources/audit_prep_completeness.py <RAW_DATA_DIR>
    [--resources-dir Resources]
    [--output PATH]
    [--audit-all]
    [--verbose]
```

### Files touched

- New: `Resources/audit_prep_completeness.py`
- `README.md`: new bullet under Upload Resilience
- `Guides/STANDALONE_README.md`: new "Content audit" section

### Verified locally (synthetic fixture)

- Fresh audit produces correct Yes/Yes/No across three ESIDs.
- Sentinel back-fill writes `.prep_complete` on confirmed `Yes`.
- Fast path returns `Yes` when sentinel is present even if contents are
  missing (intentional — trust the sentinel by default).
- `--audit-all` overrides the fast path and detects the missing content.
- Missing required file → `No` with file name in details.
- Missing ZIP → `No` (not `Ambiguous`).
- Corrupt ZIP → `Ambiguous` with explanation.
- Missing only `related_identifiers.csv` → `Ambiguous`.

---

## June 2026 — Two-phase atomic move + `.prep_complete` sentinel

Closes a partial-folder race condition between `Resources/prepare_dataset.py`
and `Resources/prep_all_datasets.py`.  Before this change, an interrupted
cross-filesystem `shutil.move()` at the end of `prepare_dataset.py` could
leave a partial directory under the final name `Staging_Area/ESID_NNN_Staging/`,
and `prep_all_datasets.already_prepared()`'s `is_dir()` check could not
distinguish it from a complete folder — so the broken ESID would be silently
skipped on the next batch run.

### Two complementary layers

**1. Two-phase atomic move (`prepare_dataset.py`)**
- Phase 1: `shutil.move(output_dir, Staging_Area/.ESID_NNN_Staging.partial)`
  — the slow, possibly cross-filesystem copy.  If interrupted, only the
  hidden `.partial` name exists; the final name never appears.
- Phase 2: `os.rename(.partial → ESID_NNN_Staging)` — same filesystem,
  metadata-only, atomic by POSIX guarantee.  Cannot be partial.
- Stale `.partial/` from a prior interrupted run is cleaned up before the
  new move, so re-prep is fully idempotent.
- `import os` added to `prepare_dataset.py` imports.

**2. `.prep_complete` sentinel — written LAST (`prepare_dataset.py`)**
- A zero-byte `.prep_complete` is `touch()`-ed inside the final folder
  as the **absolute last line** of `main()` — after the move, after the
  summary banner output.
- If the script is killed at any earlier point, the folder is present
  without the sentinel.

**3. Sentinel-aware skip check (`prep_all_datasets.py`)**
- `already_prepared()` now requires both: `Staging_Area/ESID_NNN_Staging/`
  is a directory AND `Staging_Area/ESID_NNN_Staging/.prep_complete` is a
  regular file.
- A folder missing the sentinel logs a WARNING and is treated as
  NOT-prepared, so the next prep run re-prepares it (and the existing
  partial folder is cleaned up by the move block's pre-existing
  "replacing existing staging folder" logic).
- `Uploaded_Data/ESID_NNN_Uploaded/` check unchanged — folder existence
  there implies a successful upload, which implies complete prep.

### Why both layers

- Layer 1 alone would still let a future code path that wrote to
  `Staging_Area/` outside the normal flow leave a partial folder under
  the final name.
- Layer 2 alone would not stop an interrupted copy from briefly leaving
  a partial folder under the final name (the sentinel would just be
  missing during that window, but a concurrent scan could still see the
  half-written tree).
- Together: any folder visible under `Staging_Area/ESID_NNN_Staging/`
  either carries the sentinel (complete) or doesn't (re-prep).  No
  silent skips.

### Files touched
- `Resources/prepare_dataset.py` — `import os` added; move block (lines
  ~1178–1194) replaced with the two-phase pattern; sentinel `touch()`
  appended as the new last line of `main()`.
- `Resources/prep_all_datasets.py` — new `_PREP_SENTINEL = ".prep_complete"`
  constant; `already_prepared()` updated to require the sentinel inside
  `Staging_Area/` paths.

### Backward compatibility
- Pre-existing `Staging_Area/ESID_NNN_Staging/` folders from before this
  change do NOT carry the sentinel.  After upgrade, they will be flagged
  as incomplete on the next `prep_all_datasets.py` run and re-prepared.
  To skip the re-prep, run once:
  `touch Staging_Area/ESID_*_Staging/.prep_complete`.
- Pre-existing `Uploaded_Data/ESID_NNN_Uploaded/` folders are unaffected
  — that check still passes on directory existence alone.

---

## June 2026 — Stuck-upload recovery tool (`Resources/finish_stuck_uploads.py`)

After a batch upload, some ESIDs may have exhausted all three PUT
retries on the large ZIP — the small files were committed first, so
the Zenodo draft exists with most files in place but the ZIP missing.
Re-running `standalone_tasks.py --esid <list>` already finishes them
(the resume path detects the existing draft via `upload_state.json`),
but you had to know which ESIDs were stuck.

This tool removes that requirement:
- Walks `Staging_Area/` for ESID folders containing `upload_state.json`
  (the marker written immediately after draft creation; absent from
  any folder that has been moved to `Uploaded_Data/`).
- Reads each `upload_state.json` to pull the Zenodo `record_id`.
- Lists discovered stuck ESIDs in numerical order with their draft
  IDs, so the user can see exactly what will be resumed.
- Shells out to `standalone_tasks.py --esid <list> --workers N`
  to do the actual work — zero duplication of upload, retry, or
  post-upload logic.

CLI:
```
python Resources/finish_stuck_uploads.py
    [--config Resources/config.json]
    [--workers N]
    [--list-only]      # just list the stuck ESIDs; don't run recovery
```

Robustness:
- Malformed `upload_state.json` → warn + skip that ESID.
- `upload_state.json` missing `record_id` → warn + skip that ESID.
- Non-ESID folders → silently ignored.
- No stuck uploads → clean exit with friendly "nothing to do".

---

## June 2026 — Batch preparation tool (`Resources/prep_all_datasets.py`)

A small driver that walks a top-level folder of raw ESID directories,
runs `prepare_dataset.py` on each in **numerical** order, and **skips
any ESID already prepared** (folder exists in `Staging_Area/` or
`Uploaded_Data/`).  Replaces the prior 24-line draft of the same name.

### Behavior
- Discovery: accepts folder names `ESID_NNN`, `ESID#NNN`, and unpadded
  variants (`ESID_4`).  Non-matching folders are silently ignored.
- Order: numeric sort on the extracted ESID integer, not lexicographic.
- Skip check: looks first at `Staging_Area/ESID_NNN_Staging/`, then at
  `Uploaded_Data/ESID_NNN_Uploaded/`.  Either present → skip.
- Per-ESID work runs as a `subprocess.run([sys.executable,
  "Resources/prepare_dataset.py", ...])` so this tool stays decoupled
  from `prepare_dataset.py`'s internal API and each ESID gets a fresh
  Python process.
- One failing ESID never stops the batch — failure is logged and the
  loop continues.  Exit code 1 only if at least one ESID failed.
- Heavy module-level + per-function docstrings, written for readers
  who are not full-time Python programmers.

### CLI
```
python Resources/prep_all_datasets.py <RAW_DATA_DIR>
    [--config Resources/config.json]
    [--eclipse-type total|annular|partial]
```

---

## June 2026 — Concurrent ESID uploads (`--workers`)

Motivation: many ≥40 GB ZIPs to upload, each taking ~2.5 hours sequentially.
Need to upload several at the same time to compress wall-clock time.

### Single file changed
- `standalone_tasks.py` (+ docs in README.md, STANDALONE_README.md,
  TEST_UPLOAD_GUIDE.md, and this file).

### CLI
- New `--workers N` argument (default `1`).  Validated at parse time —
  `< 1` exits with a helpful error.
- The configuration banner now logs the worker count on startup.

### Implementation
- New helper `_process_one_dataset(...)` containing everything previously
  inside the per-ESID loop body of `upload_datasets()`.  Kw-only arguments
  past the first three positionals to prevent accidental positional misuse
  in future maintenance.
- `upload_datasets()` gains a `workers: int = 1` parameter.  Two code paths:
  - `workers == 1` → direct sequential loop (identical behavior to before).
  - `workers > 1` → `concurrent.futures.ThreadPoolExecutor` with the
    requested number of workers; futures resolved via `as_completed` and
    `.result()` so unexpected exceptions surface loudly.
- Three `threading.Lock` objects guard the three pieces of shared state:
  - `tracker_lock` — `UploadTracker.mark_uploaded()` (appends to
    `Records/uploaded_files.txt`).
  - `results_lock` — `save_result()` (appends to
    `successful_results.csv` / `failed_results.csv`).
  - `stats_lock` — increments of the in-memory stats counters.
- Threads chosen over processes because uploading is I/O-bound (GIL
  released during socket waits).  No pickling, no IPC, no per-worker
  Python interpreter startup cost.
- One worker's exception cannot poison the pool: `_process_one_dataset`
  has a top-level try/except that turns any leaked exception into a
  failure result dict and continues.
- All key per-ESID log lines now carry an `[ESID XXX]` prefix so users
  can follow one dataset through interleaved log output with
  `grep '[ESID XXX]' azus_upload.log`.

### Reliability properties
- Default behavior (`--workers 1`) is byte-for-byte identical to the prior
  sequential implementation — no risk to existing workflows.
- Per-file retry (3 attempts, 30s/90s/270s backoff) and draft resume
  (`upload_state.json`) operate independently inside each worker — no
  interaction between concurrent ESIDs.
- The "Proceed? (yes/no)" confirmation prompt fires once before any
  worker starts.
- Shared output files are append-only under locks; even with `--workers 8`
  the result CSVs cannot interleave their rows.

---

## June 2026 — `--esid` filter folder-name fix

The `--esid N` filter compared against the literal folder-name suffix.
`prepare_dataset.py` now produces folders named `ESID_XXX_Staging/`,
which made the comparison `"NNN_Staging" not in {"NNN"}` always true, so
`--esid` runs found zero ZIPs while full-scan runs found them fine.

Fix: extract the leading numeric portion with a regex
(`^ESID[_#](\d+)`) instead of stripping a fixed prefix.  Now accepts
all of `ESID_073`, `ESID_073_Staging`, `ESID_073_Uploaded`, `ESID#73`.
Added `import re` to `standalone_tasks.py`; one location changed.

---

## June 2026 — Upload resilience: retry + draft resume

Motivation: a 27 GB ZIP upload died after ~2.5 hours with
`SSLEOFError(8, 'EOF occurred in violation of protocol')` — a single multi-hour
HTTPS PUT is fragile by design.  InvenioRDM multipart upload was investigated
and confirmed disabled on production Zenodo (init succeeds; per-part PUT
returns `HTTP 403 Permission denied`), so multipart is not viable.  Instead,
two narrower fixes:

### Per-PUT retry (`standalone_uploader.py`)
- New helper `_put_file_content_with_retry()` wraps the file PUT.
- 3 attempts with `30s → 90s → 270s` exponential backoff.
- Catches `RequestException` (covers `SSLError`, `ConnectionError`, `Timeout`,
  `ChunkedEncodingError`) and HTTP `5xx` server errors.
- `HTTP 4xx` fails immediately (real client/auth/payload problem; retrying
  won't fix it).
- Each attempt re-opens the file at byte 0 — InvenioRDM's single-PUT
  semantics overwrite server-side content per call, so restart is safe.
- Constants: `_PUT_RETRY_ATTEMPTS = 3`, `_PUT_RETRY_BACKOFF_S = (30, 90, 270)`.

### Draft resume (`standalone_uploader.py` + `standalone_tasks.py`)
- New helpers in `standalone_uploader.py`:
  - `get_draft_record(record_id)` — fetch an existing draft (returns `None`
    on 404 so the caller can fall back cleanly).
  - `list_draft_files(record_id)` — list existing file entries on a draft.
  - `delete_draft_file(record_id, key)` — clear a "pending" file slot before
    re-uploading.
- `upload_to_zenodo()` gains two new optional parameters:
  - `existing_draft_id` — when set, skip `create_draft_record()`, fetch the
    existing draft, list its files, skip ones already `status="completed"`,
    delete any in `status="pending"` and re-upload them.  If the draft no
    longer exists on Zenodo (404), falls back to creating a fresh draft.
  - `state_file_path` — when set, writes a small JSON file (`record_id`,
    `created_at`, `zenodo_url`, `resumed`) immediately after draft creation
    / location, used by the orchestrator to enable automatic resume on
    re-run.
- Submit-to-community and publish steps now check `parent.review` and
  `is_published` on the draft and skip if those actions already happened
  on a prior attempt — calling `submit-review` twice or republishing
  otherwise 4xx's.
- `standalone_tasks.py` writes `upload_state.json` into the ESID staging
  folder on the first attempt; on subsequent runs it reads the file and
  passes the saved `record_id` to `upload_to_zenodo()`.  The state file
  travels with the staging folder when it is renamed to
  `Uploaded_Data/ESID_XXX_Uploaded/` after success.

### Observability
- `_put_file_content_with_retry` logs both the exception class and the file
  name on each retry, e.g.
  `PUT failed for ESID_014.zip (attempt 1/3): SSLEOFError: ...`.
- Resume logs the full draft state on entry
  (`status / state / is_published / has_review`) plus a full inventory of
  every existing file entry on the draft (key, status, size, checksum)
  before any decisions are made — so any future Zenodo-side response shape
  change is immediately visible in the log.

### Files touched
- `standalone_uploader.py`
- `standalone_tasks.py`

### Diagnostic-only (not used by production code)
- `multipart_preflight.py` — one-off script that confirmed multipart upload
  is disabled on production Zenodo.  Safe to delete; safe to keep.

---

## Files Created (New)

| File | Purpose |
|------|---------|
| `Resources/project_config.json` | Eclipse Soundscapes identity — all creators, contributors, funding, community, custom fields, CSV headers, file lists |
| `Resources/README_template.html` | HTML template for Zenodo descriptions (moved from inline Python) |
| `templates/project_config.json.example` | Documented blank template for new projects |
| `templates/config.json.example` | Blank config template (replaces hardcoded annular/total sections with a `datasets` list) |
| `templates/README_template.html.example` | Documented blank README template |
| `templates/set_env.sh.example` | Blank credentials template |
| `models/__init__.py` | Package init |
| `Records/` | Directory for upload result CSVs |

## Files Refactored

### `models/audiomoth.py` (294 → 300 lines)
- **Removed** `from prefect.blocks.core import Block` — zero Prefect dependency
- **Removed** `UploadedFilesBlock` class (Prefect Block, dead code)
- **Added** `DraftConfig` model — replaces `prefect_invenio_rdm.models.records.DraftConfig`
- **Added** `Access` enum — replaces `prefect_invenio_rdm.models.records.Access`
- **Renamed** `EclipseType` → `DatasetCategory` (with backward-compatible alias)
- **Generalized** `eclipse_label()` — now handles Partial eclipses too
- **Fixed** `PersistedResult.update()` — loop over `_DIRECT_FIELDS` set instead of 12 if-blocks

### `models/invenio.py` (317 → 300 lines)
- No structural changes — already clean Pydantic models
- Enhanced docstrings with Sphinx-compatible format

### `standalone_tasks.py` (826 + 620 → 1,442 lines — two files merged)
**Merged** `standalone_upload.py` into `standalone_tasks.py` — single module for the entire pipeline.

Key changes:
- **Added** `load_project_config()` — reads `Resources/project_config.json`
- **Added** `build_creators()`, `build_contributors()`, `build_fundings()` — config-driven metadata builders replacing ~200 lines of hardcoded Python
- **Removed** all `async/await` — every function is now plain synchronous (was gratuitous async over synchronous I/O)
- **Removed** hardcoded file list (`required_files = [...]`) — now reads from `project_config.json`
- **Removed** hardcoded CSV header validation — now reads from `project_config.json`
- **Removed** hardcoded title construction — now uses `string.Template` from config
- **Removed** `from prefect_invenio_rdm.models.records import DraftConfig, Access` — uses local models
- **Eliminated** duplicate annular/total processing — `upload_datasets()` now iterates a single `datasets` list from config
- **Fixed** all `print()` → `logging.getLogger("azus")` 
- **Fixed** SHA-512 buffer: 4 KB → 64 KB (`_HASH_BUFFER_SIZE = 65_536`)
- **Added** `UploadTracker` class (moved from standalone_upload.py)
- **Added** `save_result()` helper (moved from standalone_upload.py)
- **Added** CLI `main()` with `--config` and `--dry-run` flags

### `standalone_uploader.py` (361 → 362 lines)
- **Removed** `async` from `upload_to_zenodo()` — was async but used synchronous `requests`
- **Consolidated** triple-duplicated error handling (HTTPError / RequestException / Exception) into a single `except` clause + `_cleanup_failed_draft()` helper
- **Added** `_auth_headers()` helper to eliminate repeated header construction
- **Fixed** all `print()` → `logging.getLogger("azus.uploader")`

### `prepare_dataset.py` (640 → 605 lines)
- **Removed** 80-line inline HTML template — now reads `Resources/README_template.html`
- **Added** `string.Template` substitution for `$variable` placeholders
- **Added** `--readme-template` CLI flag for custom template path
- **Fixed** SHA-512 buffer: 4 KB → 64 KB
- **Fixed** all `print()` → `logging.getLogger("azus.prepare")`

### `requirements-standalone.txt`
- **Removed** `prefect-invenio-rdm>=0.0.6` — no longer needed
- Only requires: `pydantic>=2.0.0`, `requests>=2.28.0`

## Files Removed (Dead Code)

| File | Lines | Reason |
|------|-------|--------|
| `tasks.py` | 1,018 | Deprecated Prefect workflow — functionality lives in standalone_tasks.py |
| `flows.py` | 422 | Deprecated Prefect flow — replaced by standalone_tasks.main() |
| `audiomoth.old` | 203 | Old version of audiomoth.py |
| `debug_value_error.py` | 82 | One-time debug script |
| `file_list_test_script.py` | 34 | Test script |
| `readme_test_script.py` | 30 | Test script |
| `extract_test_script.py` | 55 | Test script |
| **Total removed** | **1,844** | |

## Files Unchanged (Keep As-Is)

| File | Purpose |
|------|---------|
| `validate_csv.py` | CSV validation and repair tool |
| `create_upload_package.py` | ZIP package creator from file_list.csv |
| `batch_create_packages.py` | Batch processing wrapper |
| `escsp_azus.py` | Utility functions (file size updater, ESID extractor) |

## Config Structure Change

**Before** (config.json):
```json
{
  "uploads": {
    "annular": {"dataset_dir": "...", "collectors_csv": "..."},
    "total": {"dataset_dir": "...", "collectors_csv": "..."}
  }
}
```

**After** (config.json):
```json
{
  "project_config": "Resources/project_config.json",
  "readme_template": "Resources/README_template.html",
  "uploads": {
    "datasets": [
      {"name": "2024 Total Eclipse", "dataset_dir": "...", "collectors_csv": "...", "dataset_category": "Total"},
      {"name": "2023 Annular Eclipse", "dataset_dir": "...", "collectors_csv": "...", "dataset_category": "Annular"}
    ]
  }
}
```

This eliminates the hardcoded annular/total dichotomy and supports any number of dataset categories.

## Metrics

| Metric | Before | After | Change |
|--------|--------|-------|--------|
| Active Python lines | 3,056 | 3,010 | −46 (−1.5%) |
| Dead code lines | 1,844 | 0 | −1,844 (−100%) |
| **Total Python** | **4,900** | **3,010** | **−1,890 (−39%)** |
| External dependencies | 3 (pydantic, requests, prefect-invenio-rdm) | 2 (pydantic, requests) | −1 |
| Hardcoded identity locations | 8+ across 3+ files | 0 (config file only) | −100% |
| Files to edit for new project | 3+ Python files | 2 JSON/HTML files | −67% |
| SHA-512 buffer size | 4 KB | 64 KB | 16× improvement |
| async functions (synchronous I/O) | ~20 | 0 | −100% |
| Duplicate processing blocks | 2 (annular + total) | 0 (single loop) | −100% |
| Inline HTML template lines | ~80 | 0 | −100% |

## How to Adopt AZUS for a New Project

1. Copy `templates/project_config.json.example` → `Resources/project_config.json`
2. Fill in your project's creators, contributors, funding, community ID, etc.
3. Copy `templates/README_template.html.example` → `Resources/README_template.html`
4. Customize the HTML template with your project's description format
5. Copy `templates/config.json.example` → `Resources/config.json`
6. Set your dataset directories and CSV paths
7. Copy `templates/set_env.sh.example` → `Resources/set_env.sh`
8. Add your Zenodo API token
9. Run: `source Resources/set_env.sh && python standalone_tasks.py`

**No Python code needs to be edited.**
