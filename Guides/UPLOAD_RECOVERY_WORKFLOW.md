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
  Zenodo support to withdraw it if needed. No AZUS tool deletes a
  published record.
- **A published record whose FILES are wrong** (broken/short ZIP, truncated
  WAVs found after the fact) is not a duplicate and not a deletion — it needs
  a new version. Re-prep the ESID, then see
  `Resources/new_version_upload.py`. Published files are immutable, so this
  is the only fix; it mints a new version DOI under the same concept DOI.

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

### 4c — when one ZIP will not go, no matter how many times you re-run

A multi-GB ZIP that times out repeatedly can be replaced, on the **same**
draft, by the individual WAVs from `Raw_Data/` plus `CONFIG.TXT` and the
standalone companions. This is **opt-in** and it is a **one-way door**.

Before a big one, pre-compute the hashes. The switch verifies every raw WAV
against `file_list.csv` before uploading anything, which is a full read of the
dataset; doing it ahead of time means the upload run does not wait on it, and a
restart does not repeat it:

```bash
python Resources/hash_raw_wavs.py /absolute/path/to/Raw_Data --esid 445 --backfill-md5
```

That is an optimisation, not a prerequisite — an un-cached folder is hashed on
first use either way.

`--backfill-md5` is worth adding for anything long-running. Zenodo verifies an
upload by md5, and the resume logic skips an already-committed file only after
confirming its size **and** md5. Given cached md5s it confirms them from the
CSV; without them it re-reads every byte it already sent. On a run interrupted
at 90% that is the difference between resuming in seconds and paying for
another full pass. The file-by-file switch asks for md5s itself, so it will
backfill them on demand — pre-paying just moves the cost off the upload run.
Both digests come from a single read, and a cache written before the `MD5`
column existed stays fully valid for SHA-512, so nothing is invalidated.

```bash
# Always look first — read-only, no network calls, writes nothing.
python Resources/finish_stuck_uploads.py --list-only

# Then, for a genuinely stuck ESID:
python Resources/finish_stuck_uploads.py \
    --esid 445 --workers 1 --upload-attempts 1 \
    --enable-file-by-file --raw-data-dir /absolute/path/to/Raw_Data

# Switch NOW — no waiting for attempts to accumulate, no ZIP retry:
python Resources/finish_stuck_uploads.py \
    --esid 445 --workers 1 --force \
    --enable-file-by-file --raw-data-dir /absolute/path/to/Raw_Data
```

An ESID is switched only when **both** hold: the ZIP is the *sole* missing
file (every companion is already committed on the record), and its
`number_of_tries` has reached `--tries-threshold` (default 3). Anything
else is reported and left alone.

**`--force` drops the second condition** — see below. The first one always
applies.

Four things to know before you run it:

- **The ZIP is attempted again first, by default.** `--enable-file-by-file`
  does not skip the normal ZIP pass — a ZIP-mode ESID goes through it before
  the switch is even evaluated, which on a 40 GB file is hours, *including a
  full SHA-512 re-hash of the archive in the pre-upload integrity gate*. Note
  the pass also *increments* `number_of_tries`, so an ESID sitting at 2 tries
  passes a threshold of 3 afterwards.

  Three levers, in increasing order of how much they give up:

  - `--upload-attempts 1` — one PUT instead of three. Still hashes.
  - `--skip-integrity-hash` — drops only the ZIP re-hash from the integrity
    gate; the structural checks (sentinel, readable archive, ZIP contents vs
    `file_list.csv`) still run. Useful on every recovery run, not just this
    path, since that hash otherwise repeats each time.
  - `--force` — switch **now**, ignoring `--tries-threshold`, and skip the
    ZIP retry entirely. An ESID can be switched on its very first failure.
    This is the flag for "I have already decided this ZIP is never going to
    upload; stop making me prove it three times." Requires
    `--enable-file-by-file`.

    Two things `--force` does **not** do. It does not bypass the
    sole-missing-file check: file-by-file *replaces* the ZIP, so if a
    companion also failed the problem is not ZIP size and the switch is the
    wrong remedy — those ESIDs are reported and left to the normal pass. And
    it does not make the switch reversible; it is still a **one-way door**,
    because going back would mean deleting files already committed to the
    record. The log says so explicitly before it acts.
- **Reverting is not automated**, because going back to a ZIP would mean
  deleting files already committed to the record. To suppress switching
  entirely while still continuing ESIDs already in this mode, set
  `--tries-threshold 999`.
- **A committed ZIP is never touched.** If the ZIP actually succeeded, the
  tool refuses the switch and leaves it alone; only a failed or pending ZIP
  slot is cleared. Every raw WAV is SHA-512-checked against the prep
  `file_list.csv` before anything is uploaded, and the record is only
  published or submitted once a completeness gate confirms the full set is
  committed and the ZIP is gone.
- **Success leaves a DRAFT unless `uploads.auto_publish` is true.** As of
  July 2026 `auto_publish` is the master publish gate: with it off (the
  shipped default) a completed file-by-file run uploads everything, checks
  completeness, and stops — nothing submitted, nothing published, and the
  staging folder stays in `Staging_Area/` so this tool can still see it.
  `community_id` now decides only *how* to publish, never *whether*.

  Before that fix the test was `if community_id:` first, so a truthy
  `community_id` — the production default — submitted every completed record
  for review regardless of `auto_publish`, and a manager's accept published
  it permanently. If you want that behaviour, set
  `uploads.auto_publish: true` in `config.json`, which is now an honest
  description of what it does.

- **A site with too many files is refused up front.** Zenodo accepts at most
  100 files per record (<https://help.zenodo.org/docs/deposit/manage-files/>),
  and a file-by-file record carries every WAV individually. An ESID whose
  required set exceeds that is refused **before** the mode marker and before
  the ZIP slot is cleared, so it stays recoverable as a ZIP. ESID 797 had
  6270 WAVs — it could never have been uploaded this way, and now says so in
  the first second instead of after a full hash pass.

- **A broken `/draft` no longer blocks the repair.** A leftover pending ZIP
  slot makes Zenodo's serializer return HTTP 500 on `GET /draft` — which is
  precisely the state a timed-out ZIP leaves, i.e. the state this fallback
  exists to clear. The switch used to abort on it. It now proceeds via the
  file-list endpoint (a different Zenodo handler); a true 404 still aborts,
  because that would otherwise mint a duplicate record.

Once switched, the ESID is marked `mode: file_by_file` in its
`upload_state.json` and the ordinary ZIP pipeline **skips it** — so
`standalone_tasks.py` and this tool never fight over the same record. That
also means Phase 4a/4b will appear to ignore the ESID from then on; that is
correct, not a bug.

**If a file-by-file run fails partway through**, restore the manifest
before retrying. The run rewrites `ESID_NNN_to_upload.csv` to list the
WAVs, and a retry reads that back and looks for them in the *staging*
folder, aborting with "N required file(s) not found locally":

```bash
cd Staging_Area/ESID_445_Staging
cp ESID_445_zip_attempt_upload.csv ESID_445_to_upload.csv
```

That snapshot is written once, before the first rewrite, and never
replaced — see the file-roles table in `DIRECTORY_STRUCTURE_GUIDE.md`.

> ⚠️ **As of 2026-07-28 this fallback is fully unit-tested but has not yet
> been exercised against the live Zenodo API.** Use `--list-only` first,
> run one ESID at a time, and read the log before trusting a batch.

**Log lines to watch for** (these used to be silent):

```
DUPLICATE GUARD: found existing draft NNN with this title — resuming it ...
A record titled '...' already exists on Zenodo: id NNN ... — refusing ...
EXCLUDED N folder(s) with no upload_state.json ...
Tracker skip (already uploaded): ESID_NNN.zip
ESID folder has no ZIP — skipping: ...
Deferred: N (ZIP not uploaded yet)
```

From a `--enable-file-by-file` run (Phase 4c):

```
[ESID NNN] SWITCHING to file-by-file (tries=3 >= 3, only the ZIP is missing)...
[ESID NNN] Not switching — number_of_tries=2 < threshold=3 ...
[ESID NNN] Not switching — the ZIP is not the sole missing file ...
Clearing INCOMPLETE ZIP slot from record: ESID_NNN.zip (status=pending)
The ZIP is already COMMITTED on the record — ... NOT switched ...
NOT publishing — N required file(s) are not committed on the record: ...
N required file(s) not found locally — aborting before any upload: ...
```

The last one is the manifest-rewrite trap: restore
`ESID_NNN_zip_attempt_upload.csv` over the live manifest and re-run.

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
| What is stuck, without touching anything? | `finish_stuck_uploads.py --list-only` |
| One ZIP keeps timing out | `finish_stuck_uploads.py --enable-file-by-file` (Phase 4c — one-way) |
| A published record's files are wrong | `Resources/new_version_upload.py` (new version; dry-run by default, `--publish` off) |
| The site is too big for Zenodo's 50 GB cap | `Resources/split_oversized_raw_folders.py` (splits the raw folder into two parts; each part then needs its own collectors-CSV row) |
