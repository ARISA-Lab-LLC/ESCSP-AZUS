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

See `Guides/STANDALONE_README.md` for the full retry/resume/concurrency behavior.

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
