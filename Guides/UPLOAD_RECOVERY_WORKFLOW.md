# Upload Recovery Workflow — restarting uploads without creating duplicates

**Audience:** anyone operating AZUS on the production server after a batch
of uploads has partially failed, stalled, or produced duplicate records.

**The one rule:** never delete a staging folder or its `upload_state.json`
by hand. That file is the link between a folder and its Zenodo draft — the
anti-duplicate link. If a folder looks wrong, run the diagnosis tool
(Phase 1) first; it will tell you what the folder is and what to do with it.

Every phase below generates a **timestamped CSV report**. Those files are
your audit trail. Run everything from a consistent directory (recommended:
create a `Reports/` folder and run from there) so the reports accumulate in
one place.

---

## Phase 0 — Sync (once per code update)

1. Pull the latest AZUS code to the server.
2. Confirm the server's `Resources/config.json` has `"reserve_doi": true`
   (this file is gitignored — the repo copy is not the server copy).
3. Load credentials:

```bash
source Resources/set_env.sh
```

## Phase 1 — Assess (read-only; produces the paper trail)

```bash
python Resources/find_duplicate_records.py            # what's duplicated on Zenodo (drafts + published)
python Resources/list_upload_states.py                # which local folder claims which Zenodo record
python Resources/diagnose_missing_states.py --log azus_upload.log
                                                      # why each no-state folder is missing its link
```

Three CSVs: the Zenodo-side picture, the local-side picture, and the
explanation for every unlinked folder. Do not skip this phase — Phase 3's
decisions come straight from these files.

## Phase 2 — Heal local state

```bash
python Resources/diagnose_missing_states.py --restore-states
```

This re-links every folder whose `ESID_XXX_request_log.json` still holds
its draft's `record_id` (the only writes are local JSON files). Then work
through the remaining rows' **Suggested Action** column:

| Diagnosis | Action |
|---|---|
| re-prepped after a successful upload | cross-check the duplicate report; if the record is complete, remove the staging folder |
| tracker skip (stale `uploaded_files.txt` entry) | delete that line from `Records/uploaded_files.txt` if the ESID genuinely needs uploading |
| no ZIP in folder | re-run `prepare_dataset.py` for that ESID (re-prep now preserves any draft link) |
| no collectors-CSV row | add the ESID's row to the collectors CSV |
| attempt failed before draft creation | fix the recorded error, nothing else needed |

## Phase 3 — Resolve Zenodo-side duplicates (manual, BEFORE uploading)

Using the Phase 1 reports:

- **Draft strays:** cross-reference the `Record ID` columns of the
  duplicate report and the upload-state listing. A record **no local
  folder claims** is the stray — delete that draft in the Zenodo web UI.
  Keep the one your `upload_state.json` points to.
- **Published duplicates:** this is curation, not deletion. Pick the
  canonical record, remove the other from the community, and contact
  Zenodo support to withdraw it if needed. No AZUS tool touches
  published records.

## Phase 4 — Restart uploads

```bash
# 4a — fast pass: records, DOIs, companion files (no ZIPs, review deferred)
python standalone_tasks.py --config Resources/config.json --workers 3 --defer-zip

# 4b — the big ZIPs, one at a time at full bandwidth; re-run until clean
python Resources/finish_stuck_uploads.py --workers 1
```

Restarting is safe at any point because every layer is idempotent:

- Resume skips files already committed on the draft.
- The **title guard** refuses to create a record whose title already
  exists on Zenodo: an existing draft is adopted and resumed; an existing
  published record fails the dataset for human review (override only with
  `--skip-title-guard`).
- Re-prep preserves `upload_state.json` and the request log.

If a run dies, run 4b again. For stubborn ZIP failures, add
`--upload-attempts 1` to fail fast per file and lean on the outer re-run
loop instead of in-run backoff waits.

**Log lines to watch for** (these used to be silent):

```
DUPLICATE GUARD: found existing draft NNN with this title — resuming it ...
A record titled '...' already exists on Zenodo: id NNN ... — refusing ...
EXCLUDED N folder(s) with no upload_state.json ...
Tracker skip (already uploaded): ESID_NNN.zip
ESID folder has no ZIP — skipping: ...
Deferred: N (ZIP not uploaded yet)
```

## Phase 5 — Verify after each batch

```bash
python Resources/find_duplicate_records.py    # expect: no NEW duplicate groups
python Resources/list_upload_states.py        # Uploaded_Data/ rows grow, Staging_Area/ shrinks
```

The batch is healthy when the duplicate report shows no new groups and
every folder still in `Staging_Area/` is explainable by the diagnosis
tool. Archive the log between major batches so each run's log is a
discrete document:

```bash
mv azus_upload.log Reports/azus_upload_$(date +%Y%m%d).log
```

---

## Quick reference — which tool answers which question

| Question | Tool |
|---|---|
| What's duplicated on Zenodo? | `Resources/find_duplicate_records.py` |
| Which folder owns which record? | `Resources/list_upload_states.py` |
| Why is a folder missing its state file? | `Resources/diagnose_missing_states.py` |
| Re-link folders to their existing drafts | `diagnose_missing_states.py --restore-states` |
| Are the WAVs/ZIPs themselves intact? | `Resources/audit_wav_integrity.py` |
| Is a prepared folder complete? | `Resources/audit_prep_completeness.py` |
| Finish incomplete uploads | `Resources/finish_stuck_uploads.py` |
