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

- **Verified-integrity uploads (four layers)** — no ZIP reaches Zenodo
  unverified. (1) A pre-upload gate fails any dataset whose staging folder
  lacks the `.prep_complete` sentinel, whose ZIP is unreadable, whose ZIP
  contents disagree with the `file_list.csv` prep wrote (per-WAV name +
  size), or whose ZIP SHA-512 doesn't match the recorded hash
  (`--skip-integrity-hash` skips only the hash step). (2) Resume runs no
  longer skip already-committed files by name alone — each is verified
  against the local file by size and md5, and a mismatched remote copy
  (e.g. a short ZIP from an interrupted run) is deleted and re-uploaded
  automatically. (3) Every upload is verified after commit: Zenodo's
  reported size and md5 must match the local file, or the slot is deleted
  and the dataset fails. (4) `prepare_dataset.py` verifies the finished
  ZIP against a fresh cross-checked scan of the raw folder BEFORE the
  atomic move and sentinel, and refuses to build in place inside
  `Staging_Area/`. Covered by `tests/test_upload_integrity.py` and
  `tests/test_prepare_dataset_verification.py`.
- **Duplicate prevention (three layers)** — re-prepping a staging folder now
  preserves its `upload_state.json`/request-log link to the existing Zenodo
  draft; a lost state file is recovered from the request log automatically;
  and before creating any fresh record the uploader searches the account for
  a same-title record — an existing draft is resumed, an existing published
  record fails the dataset instead of duplicating it (`--skip-title-guard`
  to override).
- **Auto-retry** on transient SSL/connection drops and HTTP 5xx (3 attempts,
  30s/90s/270s backoff per file PUT). Override the attempt count with
  `--upload-attempts N` on `standalone_tasks.py` and
  `Resources/finish_stuck_uploads.py` (range 1–3; `N=1` = one shot per file,
  no retry). Default unchanged.
- **Resumable drafts** — if a run dies mid-upload, re-running picks up the same
  Zenodo draft and only re-uploads files that aren't already committed
  (state tracked in `upload_state.json` inside the ESID staging folder).
- **Concurrent ESID uploads** — `--workers N` uploads N ESID datasets at the
  same time. Default `1` (sequential, identical to original behavior).
  Example: `python standalone_tasks.py --config Resources/config.json --workers 3`.
- **Two-phase upload for huge ZIPs** — `--defer-zip` creates each record,
  uploads every file except the data ZIP, and reserves the DOI, but holds
  back the community-review submission (a manager accepting a record
  publishes it, and published records cannot take new files). The folder
  stays in `Staging_Area/` exactly like a stuck upload; a follow-up
  `finish_stuck_uploads.py --workers 1` run then transfers the ZIPs one
  at a time — each gets the full upload bandwidth and the shortest
  possible transfer window, so the fewest connection-drop failures — and
  submits each completed record for review.
- **File-by-file fallback for ZIPs that keep timing out** — when a large ZIP
  repeatedly fails with ONLY the ZIP left to upload and `number_of_tries` has
  reached a threshold, `finish_stuck_uploads.py --enable-file-by-file
  --raw-data-dir <root>` switches that ESID to uploading the individual WAVs
  (straight from `Raw_Data/`) + `CONFIG.TXT` in place of the ZIP — small files
  rarely time out and a single failure is cheap to retry. The switch is
  recorded as `mode: file_by_file` in `upload_state.json`, so the ZIP pipeline
  (`standalone_tasks.py`) skips that ESID and the two never touch the same
  record; each raw file is SHA-512-checked against the prep manifest before
  upload; and the record is submitted/published only after every WAV +
  `CONFIG.TXT` + companion file is committed on Zenodo. A ZIP that already
  uploaded successfully (committed) is **never deleted** — the switch aborts
  and leaves the good ZIP untouched; only a failed/incomplete ZIP slot is
  cleared. Opt-in and conservative (default `--tries-threshold 3`;
  set it high to keep retrying the ZIP). Covered by
  `tests/test_file_by_file_upload.py`, `tests/test_req9_skip.py`, and
  `tests/test_finish_stuck_file_by_file.py`.
- **Zenodo-driven discovery of ZIP-only drafts** —
  `Resources/finish_zip_only_drafts.py` inverts the search: instead of scanning
  `Staging_Area/` for `upload_state.json`, it asks **Zenodo** which drafts
  exist, matches each back to a local ESID, and classifies it. That finds the
  ESIDs the state-file scan cannot see at all — a production run found 138
  staging folders with no state file, whose drafts were therefore invisible to
  every recovery tool. The `record_id` is recovered from the listing and
  written back, re-arming the whole pipeline. **Read-only by default**, writing
  a per-draft summary CSV and a per-file detail CSV; `--execute` converts and
  `--publish` is still off even then, so the normal outcome is a complete,
  inspectable draft. `--limit N` makes the first run a canary and
  `--max-consecutive-failures` stops a batch on a systemic fault rather than
  dragging hundreds of records through a one-way door. There is no batch
  progress file: every run re-derives state from Zenodo and disk, so an
  interrupted conversion re-classifies as `RESUMABLE` and continues. Covered by
  `tests/test_finish_zip_only_drafts.py`.
- **Per-day ZIP layout (the prep default, July 2026)** — multi-GB single
  archives are the pipeline's dominant upload failure (58 of 100 stuck drafts
  in the 2026-07-29 scan), so `prepare_dataset.py` now packs **one ZIP per
  recording day**: `ESID_NNN_YYYY_MM_DD.zip`, the day read literally from the
  WAV filename prefix (an unset-clock `19700101_…` groups under `1970_01_01`
  rather than blocking; a WAV with no 8-digit prefix refuses the prep, naming
  the offenders). Each day ZIP holds that day's WAVs plus a copy of
  `CONFIG.TXT` and nothing else — the metadata companions stay standalone on
  the record. `file_list.csv` carries one ZIP row per archive and every WAV
  row names the archive that holds it. Records prepped this way are marked by
  a trailing `A` on the dataset version (`2024.1.0A`), and
  `new_version_upload.py`'s bump rule continues its revision ladder after the
  marker (`2024.1.0A` → `2024.1.0Aa`). A site whose day count would blow
  Zenodo's 100-files-per-record cap is refused before the first ZIP byte.
  `--single-zip` (also on `prep_all_datasets.py`) preserves the legacy
  layout. Both audit tools understand both layouts. ⚠️ The upload pipeline
  does not handle per-day folders yet — that is the next phase; do not feed
  them to `standalone_tasks.py` until it lands. Covered by
  `tests/test_prepare_dataset_day_zips.py`, `tests/test_azus_common_day_names.py`,
  and `tests/test_audit_prep_completeness_day_zips.py`.
- **Interrupted-prep detection** — `prepare_dataset.py` moves into
  `Staging_Area/` via a two-phase atomic pattern and writes a `.prep_complete`
  sentinel file as its very last action. `prep_all_datasets.py`'s skip check
  requires that sentinel, so an interrupted preparation cannot be silently
  skipped on the next batch run — the partial folder is re-prepped instead.
- **WAV integrity audit** — `Resources/audit_wav_integrity.py` walks every
  `ESID_NNN` subfolder of a raw-data folder and writes one CSV row per ESID
  comparing the `.WAV` files on disk against the `.wav` entries inside the
  matching `ESID_NNN.zip` (auto-located in `Staging_Area/` or
  `Uploaded_Data/`): counts, exact bytes + human-readable size, zero-byte
  files, tiny files (`--tiny-threshold`, default 1 KB), and a per-file
  disk-vs-ZIP `Match` verdict. **Every size is measured two independent
  ways** — disk `stat` vs the WAV's own RIFF header, and ZIP `file_size` vs
  its CRC — with a `Cross-Check` column that flags any disagreement (cloud
  placeholder files that falsely report zero bytes, truncated recordings,
  unreliable ZIP size fields). macOS `._*` sidecar files are excluded.
  Exit 1 on any problem or cross-check discrepancy. Covered by
  `tests/test_audit_wav_integrity.py`.
- **Oversized-site split** — `Resources/split_oversized_raw_folders.py` fills an
  `ESID#NNN_Part_2_of_2` raw folder from its `ESID#NNN_Part_1_of_2` twin, for
  sites too large for Zenodo's 50 GB per-record cap: it copies the non-WAV
  companions (SHA-512 verified), orders the WAVs by filename timestamp, and
  moves the later half so the two halves are close to equal in bytes. **Dry-run
  by default** — nothing moves without `--perform-split`. The plan is derived
  from the *union* of both folders, so it is invariant as files move: an
  interrupted run re-plans to the same cut and `--resume` finishes it, and a
  completed pair re-runs as a no-op. Same-filesystem moves are atomic renames
  (no bytes copied); a genuine cross-device move copies via `.partial`, verifies
  SHA-512, and only then unlinks the source. `upload_state.json` is never copied
  (it would point two records at one Zenodo draft), nor are ZIPs,
  subdirectories, or symlinks. Hidden files are skipped on both sides —
  including one with a `.wav` extension, which is left in place, kept out of
  the plan, and reported rather than moved into a record as audio. Reports
  whether the collectors spreadsheet has
  the two per-part rows that `prepare_dataset.py` requires — but never edits it.
  Covered by `tests/test_split_oversized_raw_folders.py`.
- **Raw WAV hash cache** — `Resources/hash_raw_wavs.py` walks every ESID
  subfolder of a raw-data folder, SHA-512-hashes each WAV + `CONFIG.TXT`, and
  records the results in a `wav_hashes.csv` inside that folder. The
  file-by-file upload's pre-verification pass reads the whole dataset before
  uploading a byte; this makes that work **durable**, so an upload that dies
  after two hours no longer re-hashes everything on the next run. A cached
  hash is reused only when the file's size **and** mtime still match, so the
  cache can never make verification weaker than doing it from scratch — a file
  altered afterwards is detected by the `stat` and re-hashed. Safe to re-run
  (only new or changed files are read) and safe to skip entirely: an un-warmed
  folder is hashed and cached on first use. `--recheck` ignores the caches for
  a genuine re-verification. `--backfill-md5` also records each file's md5
  beside its SHA-512 — Zenodo verifies uploads by md5, so a cache carrying one
  lets an interrupted file-by-file run confirm what it already sent instead of
  re-reading it. Both digests come from a single pass, and a row written before
  the MD5 column existed stays fully valid for SHA-512, so no existing cache is
  invalidated. Covered by `tests/test_hash_raw_wavs.py`.
- **New version of a published record** — `Resources/new_version_upload.py`
  publishes a re-prepped staging package as a **new Zenodo version** of an
  already-published record, for the case where the published metadata is wrong
  AND its ZIP is broken (published files are immutable, so a new version is the
  only fix). Requires `--esid` and `--record-id` explicitly — nothing is
  inferred. **Dry-run by default**, and `--publish` is OFF even under
  `--execute`, so the normal outcome is an inspectable draft; that default is
  the rollback plan, since an unpublished draft can be discarded and a
  published version cannot. Deliberately does NOT call `files-import` (an
  imported file the new package does not use would ride forward onto the new
  DOI permanently) and never submits to community review (a manager's accept
  would publish it). The version label advances by a trailing letter,
  `2024.1.0` → `2024.1.0a`. With no sandbox available the dry run is the
  safety review: it prints the constructed URLs, a per-key metadata diff,
  the file plan including anything not carried forward, and the exact
  READ/WRITE call sequence. Covered by `tests/test_new_version_upload.py`.
- **Duplicate-record check** — `Resources/find_duplicate_records.py` fetches
  every record title from the project community and/or your Zenodo account
  (drafts included) and reports duplicate-title groups to a CSV — with a
  guard so legitimate versions of one record are never flagged. Read-only;
  catches the stray extra records left behind when a lost
  `upload_state.json` forced a fresh draft. `--scope community` needs no
  API token; the full `--scope both` run requires `set_env.sh`.
- **Upload-state listing** — `Resources/list_upload_states.py` scans
  `Staging_Area/` and `Uploaded_Data/` for `upload_state.json` files and
  lists them in a CSV (ESID, location, Zenodo record id/URL, creation
  time). Shows at a glance which drafts are incomplete, which uploads
  completed as which records, and flags unreadable state files.
  Cross-reference its `Record ID` column against
  `find_duplicate_records.py` output to identify stray records.
- **Missing-state diagnosis** — `Resources/diagnose_missing_states.py`
  explains WHY a `Staging_Area/` folder has no `upload_state.json`
  (folders without one are silently excluded from `finish_stuck_uploads.py`
  recovery). It gathers all pipeline evidence — tracker entries, results
  CSVs, collectors CSV, metadata/request-log artifacts, prep sentinel,
  optional log grep — and reports a probable cause + suggested action per
  folder. With `--restore-states` it re-creates the state file from a
  surviving request log's `record_id`, re-linking the folder to its
  existing Zenodo draft so re-runs resume it instead of minting a
  duplicate.
- **Content audit / legacy migration** — `Resources/audit_prep_completeness.py`
  walks raw ESID folders, matches them against `Staging_Area/` and
  `Uploaded_Data/`, deep-audits each (folder contents + `unzip -l` of the ZIP)
  for completeness, and writes a 4-column CSV report (`ESID#`, `Staging Area`,
  `Uploaded Data`, `Prep Completed` = Yes / No / Ambiguous). Confirmed-Yes
  folders that predate the sentinel get the `.prep_complete` file
  automatically back-filled, bringing the whole repo into the sentinel world
  with one command. Use `--audit-all` to ignore the sentinel and force the
  deep audit on every folder (drift detection).

- **Zenodo record inventory** — `Resources/esid_record_report.py` scans the
  account (drafts included) and the public community listing and writes one
  CSV row per ESID data record: `ESID#, Title, Zenodo URL, Draft (y/n), DOI,
  ERROR?`. Titles are selected by repeatable OR patterns (`--title-pattern`,
  the last `*` = the 3-digit ESID) plus repeatable AND filters
  (`--and-title-pattern`, e.g. `"*2024*"`). Pagination is deterministic (a
  listing only counts as complete when a page comes back short — fixes a
  Zenodo quirk that silently capped scans at 100 records), per-record
  anomalies land in the `ERROR?` column (exit 1), and listing-level failures
  abort with no CSV (exit 2). Needs the API token for draft visibility.
- **Batch re-prep of broken staging folders** —
  `Resources/reprep_incomplete_staging.py` reads an
  `audit_prep_completeness.py` report and re-runs `prepare_dataset.py` on
  every `Prep Completed = No` ESID that has NOT been uploaded yet.
  Already-uploaded rows are skipped with a warning (fixing those means a
  new Zenodo version — a manual decision). `--dry-run` previews the plan.
- **ESID folder listing** — `Resources/list_esids.py MAIN_FOLDER` prints the
  unique zero-padded ESIDs among a folder's subfolders (pipe-friendly).

See `Guides/STANDALONE_README.md` for the full retry/resume/concurrency behavior.

## Running the Tests

The test suite is deterministic and fully offline (no Zenodo credentials,
no network). From the project root:

```bash
python3 -m unittest discover -s tests -v
```

CI runs the same suite on every push (`.github/workflows/tests.yml`).

## Unattended / Scripted Runs

`standalone_tasks.py` asks an interactive "Proceed? (yes/no)" question
before uploading. For cron/CI use, pass `--yes`; without it, a run whose
stdin is not a terminal exits with code 2 instead of hanging.

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

# 7c. RECOMMENDED for very large data ZIPs — two-phase upload:
#     Phase 1 (fast): create records + DOIs, upload all companion files,
#     but hold the ZIPs and the community-review submissions back
python standalone_tasks.py --config Resources/config.json --workers 3 --defer-zip
#     Phase 2: upload the ZIPs ONE AT A TIME at full bandwidth, then
#     submit each finished record for review. Re-run until clean.
python Resources/finish_stuck_uploads.py --workers 1
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

- `UPLOAD_RECOVERY_WORKFLOW.md` — Restarting failed uploads without creating duplicates (assess → heal → resolve → restart → verify)
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
