# AZUS Refactoring Change Log

## July 2026 — metadata inputs were being uploaded as record files

`related_identifiers.csv` was reaching Zenodo as a file on the record. It is an
INPUT: prep copies it into the staging folder and the upload step reads it to build
the record's related-identifiers metadata. It is not part of the dataset.

The mechanism was `create_upload_manifest` being a directory SCAN whose
`_MANIFEST_EXCLUDES` held only `README.html` and one vestigial name. Anything else
sitting in the folder was listed, and the upload side faithfully uploaded it.
Reproduced through the real prep CLI, which also exposed the sharper half of the
defect: **`file_list.csv` never listed the file**, so affected records ship a
payload file that the record's own file manifest does not document.

`README.html` was already excluded for precisely this reason, which showed the
defect was a class rather than an instance. There is a third member: the upload side
supports a per-record `references.csv` read from the staging folder
(`standalone_tasks.py`'s citation override), so the same bug would have appeared
through that supported path the moment anyone used it. All three are now named once,
in `azus_common.METADATA_INPUT_FILES`, and both sides consume that one definition.

**Excluded on BOTH sides, deliberately.** Prep stops writing them into new
manifests — the root fix — and `create_upload_data` excludes them again when it
assembles the file list. The second layer is not redundant: the manifest is written
at prep time, so every folder prepped before this rule still lists these files, and
re-prepping a multi-GB site to drop a 2 KB input file would be absurd. A test pins
that back-compat case with a deliberately stale manifest.

The files stay on disk, and the metadata path is unaffected: both CSVs are read
straight from the staging folder, never via the manifest. A test asserts
`get_draft_config` still receives both per-record paths.

**Not fixed here, and worth knowing:** records that already carry the file keep it.
The uploader's resume logic only *filters* what to upload
(`to_upload = [p for p in files if ...]`) — nothing iterates remote entries that
have no local counterpart, so an extra file on an existing draft is never pruned.
Cleaning those up is a reconciliation feature, deliberately out of scope.

Suite: 8 tests added (981 total), docstring audit 0 gaps.

## July 2026 — finish_stuck_uploads.py and the per-day layout

Most of this tool was already layout-agnostic and needed nothing:
`discover_stuck_esids` keys purely on `upload_state.json` + `parse_esid`, and
`run_recovery` shells out to `standalone_tasks.py --esid …`, which the per-day
migration had already taught both layouts. There is not one `ESID_NNN.zip` literal
or `glob("ESID_*.zip")` in the file. Phase A therefore required no functional
change at all.

**What did need fixing was a dead end.** A per-day folder carrying
`mode == file_by_file` in its `upload_state.json` could be finished by NO path: the
ZIP pipeline skipped it (Requirement 9) and the file-by-file fallback refused it
(the fallback replaces one whole-site archive and cannot apply to N day archives).
The tool then compounded it by advising a re-run with `--enable-file-by-file` —
precisely the path that refuses. Reachable for any ESID switched to file-by-file
before the migration and then re-prepped, which is the population in
`Records/20260729_135442_convertible_file_by_file.csv`.

**The fix reinterprets the marker rather than rewriting it.** New
`azus_common.file_by_file_mode_blocks_zip_path` holds the whole rule in one place:
the marker suppresses the ZIP path only when the layout is NOT per-day. For a
per-day folder there is no second tool contending for the record — file-by-file
refuses it — so the marker is stale and the ZIP path owns the folder.
`upload_state.json` is left byte-identical, which matters because it is the
anti-duplicate link between a folder and its draft and this guide's rule against
hand-editing it stays absolute. Confirmed with the user that no upload was ever
performed file-by-file, so no draft holds individually-uploaded WAVs and no
"is anything already committed?" check was needed.

The rule is applied at the Requirement-9 guards in `standalone_tasks.py`
(discovery and the per-ESID worker) as well as in this tool's Phase A/B
classification, so all three agree. Requirement 9 is unchanged for the layout it
was written for: a `single` or *indeterminate* layout still keeps the marker in
force — only a positively-identified per-day folder flips the decision.

**A thinness found by the new tests, then fixed in the code.** Phase B2 and the
`--force` loop relied on `only_zip_missing` returning False for per-day folders.
That works, but only as a side effect of a guard inside a helper whose name
promises something else — and per-day folders genuinely reach that evaluation now
that a stale marker no longer diverts them. Both branches now check the layout
*first*, via `_fallback_applies_to_layout`. Besides making the intent local, this
skips a live `list_draft_files` call that was pure waste for a folder the fallback
can never handle: in the mixed-staging demo, `only_zip_missing` went from being
called for every ESID to zero.

**Messages.** `only_zip_missing` returns a bare False for two unrelated causes, and
the tool always blamed the wrong one — "a companion also failed, or the ZIP is
already complete" — which since the migration directly contradicted the accurate
ERROR `refuses_per_day_layout` had just logged. `_not_switchable_reason` now names
the actual cause. A folder that was never a switch candidate no longer gets a "not
switching" line at all; it gets "still unfinished after Phase A … re-run to retry".
`--list-only` gained a layout column and labels a stale marker explicitly.

**A note that will save someone.** `--skip-existing-records` must NEVER be added to
`run_recovery`'s forwarded flag set: every ESID this tool handles has an
`upload_state.json` pointing at an existing draft — that is the definition of stuck
here — so forwarding it would skip all of them and turn the tool into a silent
no-op. The comment sits on the command builder, where the next person adding a
passthrough will be looking.

No existing test was edited. `tests/test_finish_stuck_file_by_file.py` is
byte-identical (all 22 pins), and `tests/test_req9_skip.py`'s two Requirement-9
assertions are untouched — verified safe because its fixture writes a legacy
`ESID_007.zip`, so its layout is `single` and the gate still skips it.

### README template reworded for per-day, and the staleness sentinel with it

`Resources/README_template.html` now tells readers the truth about the layout:
"included in a single zip file … if a single zip file upload was not possible …
audio files are included in multiple zip files based on the day of the
observation", plus a new paragraph explaining that a trailing **A** on the version
means the record carries per-day archives named `ESID_NNN_YYYY_MM_DD.zip`.

That rewording removed the exact sentence `refresh_readme._SENTINEL` matched, which
broke 14 tests in a way worth understanding: with the sentinel pointing at deleted
wording, EVERY README reads as stale, and `refresh_folder` then refuses to write one
because it re-checks the sentinel in its own freshly generated output. The sentinel
is updated to the new A-version sentence.

Two constraints on that constant, now recorded next to it. It must be **contiguous
on a single line** of the template: the check runs against README.md, whose
converter joins wrapped lines, but the HTML is also checked, and the reworded zip
sentence is split across two template lines — so the obvious choice from the new
wording does not work. And it must be updated whenever the template's wording
changes, because that is precisely the mechanism by which existing records become
stale and get refreshed.

**Operational consequence:** every README generated from the old template is now
correctly stale, so `refresh_readme.py` will want to rewrite them. It can do that
for single-archive folders. Per-day folders it refuses (per-day support for that
tool is a later phase), so those sites keep their old README until it lands — and
note the refresh is genuinely cheaper there when it does, since per-day archives
carry no README to swap.

Suite: 19 tests added (973 total), docstring audit 0 gaps.

## July 2026 — `--skip-existing-records`: skip ESIDs Zenodo already holds

Re-running a batch over a raw-data folder where some sites are already uploaded
meant either hand-maintaining an `--esid` list or letting each finished site walk
through the integrity gate — which re-hashes every archive — only to be resumed or
refused downstream. The new opt-in flag asks Zenodo, by the dataset's intended record
title, whether a record already exists, and skips the folder if one does.

**Placement is the substance of this change.** The check sits in
`_process_one_dataset_inner` immediately after the Requirement-9 file-by-file guard and
**before** the integrity gate, copying that guard's clean-skip shape: increment
`stats["skipped"]`, return, leave the folder untouched. Ahead of the gate a skipped
folder costs one API search instead of a multi-GB read, which is where the time
actually goes on a large site.

**It asks Zenodo rather than reading `upload_state.json`.** That is the deliberate
choice: a folder whose state file was lost or hand-deleted is exactly the folder that
would otherwise create a duplicate record, and a local-only check would miss it.

**Both drafts and published records count as existing**, and both are counted as
*skipped* rather than failed. A published record means the site is finished, so a
failure row would be wrong — `failed_results.csv` stays a list of things that need
attention. (Without the flag, a same-title published record still raises
`DuplicateTitleError` as before; the flag changes nothing when it is off.)

**It fails closed.** If the search cannot be completed — network, auth, or an
unrecognized response body, which `_search_drafts_by_title` already refuses to read as
"no matches" — the dataset is FAILED with an `ExistingRecordCheckFailed` row rather
than falling through to an upload. The flag exists in order not to touch what already
exists, so an undeterminable answer must not silently become an upload. This matches
the stance the integrity gate already takes on its own crash.

**The one real risk was title drift, and it is closed structurally.** The pre-check
searches by title, so if it rendered the title even slightly differently from the
record itself the search would match nothing and the flag would silently never fire —
a failure with no symptom. `get_draft_config`'s inline title construction is therefore
extracted into `build_record_title`, and both callers use it. The subtlety worth
naming: the ESID renders in *display* form, so ESID `122_Part_1_of_2` titles as
`ESID#122 Part 1 of 2`; a re-implementation would very plausibly have used the raw
ESID and matched nothing. `tests/test_skip_existing_records.py` pins the two renderings
against each other rather than against a literal.

Two notes for operators. The uploader's duplicate guard searches **again** immediately
before creating a draft; that second search is deliberate — it closes the window
between the pre-check and draft creation — so a run with this flag performs two
searches for any ESID it does not skip. And the flag is orthogonal to
`--skip-title-guard`: this one decides whether to skip a whole dataset, that one
decides whether the uploader refuses to create a duplicate.

Suite: 13 tests added (954 total), docstring audit 0 gaps.

## July 2026 — the upload pipeline learns the per-day layout

Prep has produced one archive per recording day since earlier this month, but the
upload side still modelled "a dataset" as "a ZIP file", and
`Guides/STANDALONE_README.md` carried a blocking warning telling operators not to
feed per-day folders to `standalone_tasks.py`. Retiring that warning is this
change. A prepared FOLDER is now the unit of work: one folder is one dataset is
one Zenodo record, holding one legacy archive or N day archives.

The warning was doing less than assumed. On a per-day folder the old pipeline did
not refuse cleanly — it misbehaved in three ways at once, all from a single root
cause, and the integrity gate that appeared to be holding the line had a hole in it.

**1. Discovery emitted one work item per ARCHIVE, not per folder (blocker).**
`get_upload_data` globbed `ESID_*.zip` and appended each hit to the work list, so an
N-day site became N datasets sharing one ESID, one title, one staging folder and one
`upload_state.json` — N draft-creation attempts against one record, with only the
title guard (disableable) standing between that and duplicates. Discovery now
iterates folders and resolves each folder's archives once.

**2. The other N−1 archives leaked in as low-retry companions (blocker).**
`create_upload_data` excluded only the *current* ZIP name from `additional_files`.
Because prep's upload manifest is a directory scan it lists every archive, so the
rest arrived as "companions" — and companions deliberately keep the default 3-attempt
budget rather than `--upload-attempts`, which is exactly backwards for a multi-GB
archive. Every archive name is now excluded, and `upload_to_zenodo`'s
`zip_filename: Optional[str]` became `priority_files: Optional[Set[str]]` so all of
them get the configured budget. A `Set`, not a `Collection`: a bare `str` is also a
collection, and `in` against one silently becomes a substring test.

**3. Success on the first archive moved the staging folder (blocker).**
`archive_staging_to_uploaded` runs inside `if result["successful"]`, which was
correct — but with N work items per folder the first one to finish moved the folder
into `Uploaded_Data/`, stranding items #2..N on paths that no longer existed.
Needed no change of its own: collapsing the fan-out fixed it, which is the clearest
evidence the folder was always the real subject.

**4. The integrity gate passed a single-day per-day folder (HIGH).** The gate built
`zip_wav_sizes` from ONE archive and compared it against EVERY `.wav` row in the
folder's `file_list.csv`. That union comparison refused folders with ≥2 day archives
— the behaviour mistaken for a clean refusal — but a folder with exactly ONE day
present satisfied it and uploaded, with the wrong version string. That is precisely
what an interrupted per-day prep leaves behind. The gate is now scoped per archive:
each archive's expected WAVs are the `file_list.csv` rows whose `wav_day_key` maps
back to it via `day_zip_name`, and both directions are checked — a WAV missing from
ITS archive, and a day the manifest describes whose archive is absent. Ownership is
re-derived, never read from the `Notes` column, which records the same mapping in
prose for humans. A cross-day misfile (day 8's WAV inside day 9's archive) is now
reported twice, once from each side; the old union comparison could not see it at all.

**5. The version-`A` marker never reached the record (MEDIUM).** `prepare_dataset.py`
applied the marker to the staged `total_eclipse_data.csv`, while `get_draft_config`
sourced `version` from the MASTER collectors spreadsheet, which prep never writes
back to — so a per-day record carried `2024.1.0A` in its own CSV and `2024.1.0` in
its Zenodo version field. The upload step now reads the version from the staging
folder's own CSV, so prep stays the single authority for the marker and upload merely
consumes it. This also fixes `new_version_upload.bump_version_label`, which was
already marker-aware but was being fed unmarked input. Note that
`prepare_dataset.py`'s own comment already *claimed* this behaviour; the claim is now
true rather than aspirational.

**Also in this change.** A single layout seam, `resolve_dataset_archives`, replaces
the `glob("ESID_*.zip")` discovery and the five places that recovered the staging
folder by `.parent` from an archive path; it routes on the prep contract's
`staging_zip_mode` (imported from the producer, as `audit_prep_completeness.py` does)
and refuses a mixed layout before opening anything, so a mixed 43 GB folder never
costs a full read. `UploadData.zip_file: str` became `staging_folder: str` +
`archives: List[str]` with no scalar alias — a privileged "first" archive is the
single-ZIP assumption in disguise. `--defer-zip` defers the whole archive set, since
a record must not enter community review holding a fraction of its days.
`get_recording_dates` spans every archive, so the record's date interval covers the
campaign rather than one day, and it now calls `wav_day_key` instead of
re-implementing the parse — keeping its `try/except`, because `wav_day_key` does no
calendar validation by design and `strptime` is what rejects an impossible date. The
100-file cap is re-checked against the ACTUAL file set before any network work
(prep can only budget for planned companions; the manifest is a directory scan).
`save_result`'s `zip_file` parameter was dead — never read, no such field on
`PersistedResult` — and is deleted rather than reshaped.

**Guards, not support, for the recovery tools.** Per-day support for the conversion
tools is a later phase; until then each refuses loudly. The important one is
`file_by_file_upload.py`, which failed **open**: `required_files` classifies a day
archive as a *companion*, so once every archive was committed the companion test in
`only_zip_missing_from_entries` passed vacuously and it returned
`ESID_NNN.zip not in committed` — True for a name a per-day record never had. A
complete, healthy record read as "only the ZIP is missing" and would have authorised
the one-way switch its own docstring warns about; had it fired, it would have
uploaded the day archives as companions *plus* every raw WAV, double-carrying the
audio and blowing the file cap. Guarded at `only_zip_missing` (the choke point both
`finish_stuck_uploads.py` call sites go through) and at `run_file_by_file`.
`new_version_upload.py` needed a real layout check, not its existing count check:
a single-day per-day folder holds exactly one archive and sailed through
`len(zips) != 1`. `finish_zip_only_drafts.py` gained an `UNSUPPORTED_ZIP_LAYOUT`
verdict — the suite's "every verdict has a recommended action" invariant caught the
missing entry. `refresh_readme.py` already skipped per-day folders; it reported "no
ZIP", which is untrue and sends an operator hunting, so it now names the layout.
`finish_stuck_uploads.py` needed nothing: its ZIP path shells out to
`standalone_tasks.py` and inherits this work.

**On the test suite.** No assertion was weakened. All 21 tests in
`tests/test_upload_integrity.py` keep their predicates verbatim and the file is
relabelled as the permanent `--single-zip` regression pin — only the call shape
changed (the gate takes a folder) and `digests_out` is addressed by archive
basename. Elsewhere the edits are call-shape adaptations to renames and reshapes:
`zip_filename` → `priority_files`, scalar `zip_file` → `archives`, pair → triple in
`create_upload_data`, `get_recording_dates` taking a list. The one that mattered to
get right was `tests/test_req9_skip.py`, which asserts the file-by-file guard runs
BEFORE the integrity gate via `gate.assert_not_called()` — keeping the gate's NAME
means that pin still bites; had the function been renamed the assertion would have
passed vacuously against an unused name.

`tests/test_per_day_prep_to_upload.py` is the one worth knowing about: it runs the
REAL prep CLI in a hermetic tree and drives its genuine output through the real
discovery, integrity and upload wiring with only the HTTP boundary mocked. Every
other per-day test builds its staging folder by hand and so shares one blind spot —
if prep's output drifts from what those fixtures imitate they keep passing while
production breaks. It asserts one record receives every day archive, archives last
in day order, no archive among the companions, the version reading `2024.1.0A`, the
folder moved exactly once, and the legacy `--single-zip` path still working through
the same wiring.

Two footguns were found by the tests and closed in the code rather than worked
around: `get_recording_dates([...])` given a bare string iterated it character by
character and failed with `missing ZIP file: v`, and `verify_dataset_integrity`
given an archive path would have reported a confusing "no sentinel". Both now raise
a message naming the mistake.

Suite: 56 tests added (941 total), docstring audit 0 gaps.

## July 2026 — five defects in the per-day prep, found by an adversarial audit

A five-lens adversarial review of the just-committed per-day workflow (each
finding then handed to a refute-by-default skeptic) confirmed six findings, four of
which were the same defect reported independently by four lenses. Both blockers are
reproduced and closed; the fixes are verified end-to-end through the real CLI.

**1. A stale day-ZIP from an earlier failed prep was re-staged and uploadable
(HIGH).** `verify_day_zips_against_source` derived `actual_names` from the archives
the CURRENT run created, never scanning the output directory. A prep that fails
between building and the atomic move deliberately leaves its build folder in place
(`clean_raw_staging_leftovers.py`: "Nothing in the pipeline ever cleans these up")
and `main()` only `mkdir(exist_ok=True)`s it — so if the operator removed a day's
audio and re-ran, that day's archive survived, invisible to verification. It got no
`file_list.csv` row, but `create_upload_manifest` (a directory scan) listed it, so it
moved into `Staging_Area/` with the completion sentinel and, on a single-surviving-day
site, could be PUT onto the record as **undocumented audio with no hash, size, or
description anywhere in the manifest**. Multi-day sites were contained by the upload
integrity gate; this class is new to per-day mode, since the legacy layout rewrites
its one constant ZIP name every run. Verification now enumerates `output_dir` and
refuses with a `STALE day ZIP …` message naming the folder to delete.

**2. One AppleDouble sidecar refused an entire ESID (MEDIUM, but a certain
production blocker).** macOS writes `._NAME.WAV` companions on the exFAT SD cards
this data arrives on. `audit_wav_integrity._is_wav_name` and
`hash_raw_wavs.is_hashable_name` have always skipped them; `group_wavs_by_day` did
not, so a single sidecar had no 8-digit prefix and aborted the whole site. New
`azus_common.is_raw_wav_name` is the shared rule; new `prepare_dataset.raw_wav_files`
is the one lister the per-day path uses for zipping, expectation, grouping, and the
file-list rows. `group_wavs_by_day` also skips them itself, so a sidecar is
structurally incapable of refusing a site whatever the caller passes.
`audit_day_zips` applies the same rule, so prep and the auditor cannot disagree. The
legacy path deliberately keeps its historical unfiltered glob —
`create_internal_file_list` gained an optional `wav_files` parameter rather than
changing legacy behaviour, so re-preps of published single-ZIP records produce
byte-identical output.

A related divergence surfaced and is now documented and handled:
`_is_wav_name` rejects only the `._` prefix, so it accepts `.hidden.wav` where the
per-day rule rejects it. `verify_day_zips_against_source` therefore re-filters its own
disk side rather than trusting `scan_disk_wavs`, so a hidden `.wav` cannot manufacture
a "disk WAV in no archive" mismatch.

**3. The per-day file list was built AFTER verification (MEDIUM).** The legacy path
verifies after building the internal list, which is what catches a WAV that changed
between zipping and the file list (whose per-WAV rows re-`stat` the source). Per-day
had the opposite order, leaving that window unchecked. `_run_day_zip_prep` now
mirrors legacy: zip → metadata → internal list → **verify** → per-day file list →
manifest. The grouping and cap guards still run before any archive is written, so
fail-fast on the common refusals is unchanged.

**4. `parse_day_zip_name` accepted any extension (LOW).** Callers use it to decide a
folder's LAYOUT, so a stray `ESID_005_2024_04_08.log` flipped `staging_zip_mode` to
per-day. `.zip` is now required (case-insensitive). `parse_esid`'s tail-stripping is
unaffected, so a dated folder name still resolves to its ESID.

**5. The version-`A` docstring overclaimed (LOW).** It asserted the upload phase
sources the version from the staged CSV. It does not: `get_draft_config` reads
`data_collector.version` from the MASTER collectors spreadsheet, which prep never
writes back to. A per-day record therefore carries `2024.1.0A` inside its own
`total_eclipse_data.csv` while its Zenodo version field still reads `2024.1.0`.
Corrected to state the gap explicitly and scope its closure to the upload phase.

**Also fixed: a vacuous test of my own.**
`test_refusal_happens_before_any_zip_is_written` asserted no ZIPs existed after
calling `enforce_zenodo_file_cap` directly — true wherever the guard sits, since the
guard never writes ZIPs. It now drives `_run_day_zip_prep`, which is the only thing
that exercises the ORDER, with a negative control proving the guard is not refusing
everything.

**Confirmed NOT broken by the audit:** the `parse_esid` date-tail strip breaks no
existing caller (all 14 enumerated; independently corroborated by a production run
over 408 raw folders in which all 16 `_Part_N_of_2` ESIDs and `120A` parsed
correctly), and the upload pipeline **does** refuse a per-day folder cleanly —
`verify_dataset_integrity` runs before any network call and no day-ZIP can satisfy a
`file_list.csv` that lists every day's WAVs, so nothing is created, published, or
archived. `--skip-integrity-hash` does not bypass it.

Tests: 26 added (885 total) — `TestStaleArchiveIsRefused` (incl. end-to-end: exit 1,
no staging folder, no sentinel), `TestAppleDoubleSidecars`,
`TestHiddenWavIsIgnoredConsistently`, `TestIsRawWavName`,
`TestAuditAgreesWithPrepOnSidecars`, plus the extension and cap-ordering cases.
Docstring audit 0 gaps.

## July 2026 — per-day ZIP layout: one archive per recording day (the new prep default)

Multi-GB single ZIPs are the pipeline's dominant upload failure — they time out
repeatedly, losing hours per attempt, and the 2026-07-29 production scan showed 58
of 100 stuck drafts are exactly this. Going forward every ESID is prepped as **one
ZIP per recording day** (`ESID_NNN_YYYY_MM_DD.zip`, suffixed ESIDs included:
`ESID_122_Part_1_of_2_2024_04_08.zip`); small archives rarely time out and a single
failure is cheap to retry. All of an ESID's day ZIPs still go to ONE Zenodo record.

**Decisions (user-confirmed):**

- **The day is the LITERAL 8-digit prefix of the WAV filename** — one grouping
  rule, `azus_common.wav_day_key`, shared by prep, verification, and the audits so
  a file can never be grouped one way and audited another. An unset AudioMoth
  clock's `19700101_…` files group under `1970_01_01` rather than blocking; only a
  WAV with no 8-digit prefix refuses the prep (fatal, offenders named).
- **Day ZIPs are lean**: that day's WAVs + a copy of CONFIG.TXT (first entry, as
  always) and NOTHING else. The metadata companions live once on the record;
  `add_files_to_zip` does not run in this layout, which also kills the
  internal/external file-list two-step — the archives are final the moment they
  are written.
- **Entries sit under the ZIP-stem subfolder** (`ESID_005_2024_04_08/`), so
  extracting several day archives side by side never collides.
- **`file_list.csv`: one ZIP row per archive** (first, in day order), each WAV
  row's Notes names its archive, CONFIG.TXT's row notes the per-ZIP copies.
- **The dataset version gains a trailing `A`** (`2024.1.0` → `2024.1.0A`),
  applied once to the in-memory collector row so `total_eclipse_data.csv` and the
  README inherit it. Idempotent. `new_version_upload.bump_version_label` now
  treats the marker as base version and continues its lowercase ladder after it
  (`2024.1.0A` → `2024.1.0Aa`) instead of refusing. NOTE: the Zenodo record's
  version METADATA is set at upload time from the master collectors CSV — the
  upload phase must source it from the staged CSV for the `A` to reach the record.
- **The Zenodo file cap is enforced before the first ZIP byte**:
  day count + companions vs `azus_common.ZENODO_MAX_FILES_PER_RECORD` (hoisted
  from `file_by_file_upload`, which now aliases it) — ~85 days with the standard
  companion set. Over-long deployments are refused with the remediations named.
- **`--single-zip` preserves the legacy layout byte-for-byte** (on
  `prepare_dataset.py` and forwarded by `prep_all_datasets.py`); needed for
  re-preps of already-published single-ZIP records, which
  `new_version_upload.py` expects to hold exactly one archive.

**Naming infrastructure** (`azus_common`): `wav_day_key`, `day_zip_name`,
`parse_day_zip_name` (end-anchored date tail, so Part suffixes — themselves
digits and underscores — parse correctly), and **`parse_esid` now strips a
trailing `_YYYY_MM_DD` like a reserved tail** — without that, every day-ZIP name
would parse as the nonsense suffixed ESID `"007_2024_04_08"` and match no
collector row, manifest, or staging folder. All 14 existing call sites parse
folder or ZIP basenames; none legitimately carries a date tail.

**Verification & audits**: `verify_day_zips_against_source` re-groups a FRESH
disk scan by day and requires exact per-day equality — which simultaneously
proves every disk WAV is covered exactly once, none leaked into a foreign day's
archive, and no metadata snuck in — plus CONFIG.TXT in every archive; zero-byte
WAVs warn-not-fail as before. `audit_prep_completeness` routes on the new
`staging_zip_mode` contract function (`single` / `per_day` / `mixed` — mixed is
a hard No) and audits per-day folders via `expected_day_zip_names`, the same
grouping rule prep used; single-zip folders go through the untouched legacy
rules. `audit_wav_integrity` gained `locate_zips` / `scan_zips_wavs` (merged
stats; a WAV basename in two archives is a discrepancy).

**Deliberately NOT in this phase — the upload pipeline.** `standalone_tasks.py`
still assumes one ZIP per folder: it would mint one draft per day-ZIP and its
integrity gate compares all WAVs against a single archive. **Per-day staging
folders must not be fed to `standalone_tasks.py` until the upload phase lands.**
Known touch-points are listed in the plan: `get_upload_data`,
`verify_dataset_integrity`, `get_recording_dates`, `zip_filename` retry scoping,
`known_md5s`, `archive_staging_to_uploaded`, `--defer-zip`, and the recovery
tools (`file_by_file_upload._zip_name`, `finish_zip_only_drafts`,
`new_version_upload` ZIP_AMBIGUOUS, `clean_raw_staging_leftovers._single_zip`,
`refresh_readme`'s ZIP-row precondition).

Tests: `tests/test_azus_common_day_names.py` (16),
`tests/test_prepare_dataset_day_zips.py` (29, incl. both layouts end-to-end
through the real CLI), `tests/test_audit_prep_completeness_day_zips.py` (16),
extensions to `tests/test_audit_wav_integrity.py`,
`tests/test_prepare_dataset_atomic_move.py` (per-day sibling; legacy case pinned
to `--single-zip`), and `tests/test_new_version_upload.py` (marker ladder).
Suite: 859 tests, docstring audit 0 gaps.

## July 2026 — discover ZIP-only drafts from Zenodo, not from local state files

`finish_stuck_uploads.py` finds work by scanning `Staging_Area/` for
`upload_state.json`. A production run showed why that is the wrong index: **138
staging folders had no state file**, so their drafts were invisible to every
recovery tool — while sitting on Zenodo, complete except for the ZIP, which is
exactly the state the file-by-file fallback repairs.

New tool: **`Resources/finish_zip_only_drafts.py`**. It lists the account's
drafts, matches each back to a local ESID by title, classifies it, and — with
`--execute` — hands the repairable ones to the existing
`file_by_file_upload.run_file_by_file`. The `record_id` is recovered from the
listing and written back into `upload_state.json`, which re-arms the rest of the
pipeline. Read-only by default; two CSV reports (one row per draft, one row per
file); `--publish` off even under `--execute`, so the normal outcome is a
complete, inspectable draft.

Reuse over reinvention: `esid_record_report.fetch_all_hits_verified` for the
paginated listing (deterministic short-page termination plus a `hits.total`
cross-check), `title_in_scope` / `_draft_flag_from_hit` for scoping,
`azus_common.load_esid_args` for `--esid`, `finish_stuck_uploads._load_publish_config`
for `community_id`/`reserve_doi`, `fbf.required_files` / `committed_keys` /
`is_raw_upload_name` for the file sets, and `hash_raw_wavs.load_cache` for the
report's hash columns. The genuinely new code is the classifier, the two
reports, and the batch controls.

Decisions worth recording:

- **The fail-open hole is closed structurally.**
  `only_zip_missing_from_entries` returns True for a record missing
  EVERYTHING when the companion list is empty — the companion test is
  vacuously satisfied — and that answer authorises a one-way door. The new
  `classify_from_entries` refuses an empty required set outright rather than
  relying on its caller to have checked first.
- **`RESUMABLE` is a verdict, not a skip.** A draft already marked
  `file_by_file` is continued, whatever fraction of its WAVs are committed.
  An early draft of this treated "some WAVs already on the record" as a
  skip, which would have stranded every ESID a restart interrupted — the
  common case in a weeks-long batch.
- **Duplicate drafts per ESID are refused with zero network calls.** This is
  why `prep_all_datasets.filter_and_order_discovered` is not reused for
  grouping: its `by_esid` dict is last-wins and would silently collapse the
  pair. `--esid` still goes through `load_esid_args`, so value acceptance and
  ordering match every other tool.
- **The ESID is re-validated through `azus_common.normalize_esid`.**
  `match_title` deliberately falls back to `capture[:3]` rather than aborting
  a report; lenient is wrong for a tool that deletes files. With the default
  patterns the two grammars agree so the re-check cannot currently fire — it
  is kept, and documented as such, because it makes the link between "a title
  matched" and "we will delete from this record" hold by construction.
- **Hash columns are cache-only.** `--with-hashes` fills SHA-512 from
  `file_list.csv` and md5 from `wav_hashes.csv`. Nothing in this tool ever
  reads a file's bytes to hash it; doing so during a scan would turn a
  read-only pass into a multi-day walk over terabytes.
- **`--limit` and `--max-consecutive-failures`** are the two controls that
  make a first production run survivable: a canary, and a stop after a few
  failures instead of dragging hundreds of records through a one-way door on
  one expired token.
- **`restore_upload_state` was NOT extracted into `azus_common`** as the plan
  proposed. `diagnose_missing_states.restore_state` refuses when any state
  file exists; this tool must refuse only on a record MISMATCH and be a no-op
  when the file already names the same record — that is what makes a restart
  safe. Sharing one function would have meant a behaviour flag, so the ~15
  lines live locally instead.

Tests: `tests/test_finish_zip_only_drafts.py`, no network.

### What the first production scan found, and the two fixes it forced

The first full read-only scan classified **100 in-scope drafts** (234 account
records: 100 drafts, 105 published, 29 non-ESID titles) in 54 seconds:

| Verdict | Count |
|---|---|
| `TOO_MANY_FILES` | 58 |
| `COMPANIONS_MISSING` | 24 |
| `CONVERTIBLE` | 13 |
| `NO_STAGING_FOLDER` | 5 |

**The premise held.** 6 of the 13 convertible drafts — 103, 147, 201, 211, 232,
350 — carry no `record_id` locally at all, so `finish_stuck_uploads.py` cannot
see them. Those are the drafts this tool exists to reach.

**But file-by-file serves a small minority.** The 58 oversized drafts run from
291 to 6315 WAVs; the *smallest* is still 3× Zenodo's 100-file cap, so no
`--max-files` value reaches any of them. For those the ZIP is the only vehicle,
which means ZIP upload reliability — not this tool — is where the remaining
value is. Recorded here so the next person does not re-derive it.

Two defects the scan exposed:

1. **The `TOO_MANY_FILES` advice was wrong**, on 58% of the output. It
   recommended `split_oversized_raw_folders.py`, which handles
   `_Part_N_of_2` **pairs only** and carries its own
   `a half still exceeds the limit` verdict. A 6307-WAV site needs ~78
   records, not 2. New `records_needed` / `split_advice` compute what the
   site would actually take — accounting for the companion set being
   repeated in every record, so the usable slots are `max_files -
   companions` — and recommend a split only when two records genuinely
   suffice (97–162 raw files at the default). The static
   `_RECOMMENDED_ACTION` text now points at the Notes column instead of
   guessing.
2. **`{esid: folder}` was last-wins on a one-to-many relationship.**
   `azus_common.parse_esid` strips the `_staging` / `_uploaded` tails, so a
   leftover `Raw_Data/ESID_055_Staging` resolves to ESID 055 exactly as
   `Raw_Data/ESID#055` does — and three scan rows showed such a leftover as
   their Raw Folder. The dict comprehension silently kept whichever came
   last in directory order, so the tool could have hashed and uploaded from
   a stale staging leftover. New `index_esid_folders` returns
   `{esid: [folder, ...]}`, `LocalPackage` carries both lists, and a new
   `AMBIGUOUS_LOCAL_FOLDER` verdict **refuses** — checked ahead of every
   other gate, because a count derived from the wrong folder is worse than
   no answer.

Also fixed a vacuous test: `test_hash_columns_are_empty_without_the_flag`
iterated the detail rows to assert their hash columns were empty, so zero rows
passed. It now asserts the row count first.

Suite: 788 tests, docstring audit 0 gaps.

### The bug this found: `run_file_by_file` was not idempotent

Running the new tool twice against the same folder surfaced a live defect in
the **existing** fallback, which its docstring claimed was "idempotent and
re-runnable" and which `Guides/UPLOAD_RECOVERY_WORKFLOW.md` documented a manual
workaround for.

Step 6 rewrites `ESID_NNN_to_upload.csv` to list the whole file-by-file set —
companions **and** raw files. `required_files` derived `companion_names` from
every row of that manifest, so on the **second** run it read the WAVs back as
companions, looked for them in the *staging* folder, and aborted with
"N required file(s) not found locally". Consequences:

- every resume was broken — a conversion interrupted part-way could never be
  finished, which is precisely the `RESUMABLE` case;
- `finish_stuck_uploads.py --enable-file-by-file` Phase B1 ("continue ESIDs
  already in file-by-file mode") hit the same abort;
- `only_zip_missing` inherited it too: with WAVs counted as companions, a
  partially-uploaded record read as "a companion is also missing", so
  `--force` refused to switch.

Fix: `required_files` excludes raw-upload names (`*.wav`, `CONFIG.TXT`) from
`companion_names` whichever manifest generation it is reading. A companion is
by definition not a file that lives in `Raw_Data/`, so this is correct
independent of the rewrite, and it makes the derivation idempotent by
construction rather than by convention. The no-op case (a ZIP-mode manifest,
which lists no WAVs) is unchanged.

The recovery guide's "restore the manifest before retrying" instructions are
replaced with "just re-run it", and
`ESID_NNN_zip_attempt_upload.csv` is now described as provenance rather than a
recovery mechanism.

Tests: `TestRequiredFilesIsIdempotent` in `tests/test_file_by_file_upload.py`
— a second derivation from a rewritten manifest matches the first, raw names
never become companions, and a second full `run_file_by_file` succeeds with the
same file set.

Suite: 774 tests, docstring audit 0 gaps.

## July 2026 — md5 in the raw hash cache, so a restart re-reads nothing

The hash cache made SHA-512 verification durable across restarts. It did not make the
*upload* durable, and that turned out to be the larger cost.

`upload_to_zenodo`'s resume reconciliation skips an already-committed file only after
confirming its size **and** md5 — Zenodo's own checksum. Its `known_md5s` parameter lets a
caller supply those instead; with nothing to supply, it read every byte it had already sent
back off disk to compute them. So a conversion interrupted at 90% re-read ~90% of the
dataset before uploading its first remaining file, which on a multi-day run is precisely
the cost the cache was built to remove.

`wav_hashes.csv` now carries an `MD5` column beside the `SHA-512`, and
`file_by_file_upload` passes it through as `known_md5s`.

- **`azus_common.calculate_digests(path, ("sha512", "md5"))` feeds both hashers from one
  chunk loop**, so recording md5 costs no additional reads — the same reason the ZIP path
  uses it.
- **The column is appended LAST**, so `csv.DictReader` on a pre-MD5 cache yields `None` for
  it rather than mis-aligning the other four columns. `load_cache` now returns
  `(size, mtime, sha512, md5)` with `""` for a legacy row.
- **A legacy row stays fully valid for SHA-512.** Adding the column does not invalidate a
  cache that took days to build: a caller that does not need md5 reads nothing at all. This
  is pinned by a test that counts `calculate_digests` calls and asserts zero.
- **`need_md5=True` backfills on demand.** A matching row with an empty md5 cell is read
  once, and both digests are filled. Without this a warm legacy cache would never acquire
  md5s — it would always look fresh.
- **The backfill cross-checks the cached SHA-512 for free.** The bytes are in hand anyway.
  A row whose SHA-512 disagrees with the file it describes — while size and mtime are
  unchanged, which no ordinary edit achieves — is reported as an error, and the **fresh**
  hash is served, because the file on disk is what `file_list.csv` must be compared
  against. The stale row is replaced.
- **`--backfill-md5`** pre-pays the cost overnight rather than during an upload.
- md5 is served only when size+mtime match **and** the cell is non-empty, so `known_md5s`
  always comes from the same validated row as the SHA-512. Harvesting md5s any other way
  would reintroduce the staleness hole the cache was designed to close.

Also: the two tests that simulated an unreadable file were patching `calculate_sha512`,
which `ensure_hashes` no longer calls; they now patch `calculate_digests`. And
`hash_raw_wavs.py`'s exit code 1 covers a cache disagreement as well as an unreadable
file — the summary line said "unreadable" for both.

Tests: `tests/test_hash_raw_wavs.py::TestMd5Cache`, 8 new (692 total).

## July 2026 — three defects in the file-by-file fallback, found by ESID 797

A production run on ESID 797 failed three ways at once, and the log made all three
visible. Fixing them is a prerequisite for scanning Zenodo for convertible drafts,
because at a few hundred records each becomes a mass event rather than one bad ESID.

**1. `auto_publish=False` did not leave the record a draft.** The publish step tested
`if community_id:` FIRST and only then `elif auto_publish:`. `project_config.json` holds
a real community UUID and `finish_stuck_uploads._load_publish_config` reads it — so every
completed conversion went into the community review queue whatever `auto_publish` said,
and a manager's accept publishes permanently. **`auto_publish` is now the master gate**;
`community_id` decides only *how* to publish, never *whether*. This also aligns the module
with `upload_to_zenodo`, where the two are independent parameters — this was the only place
that conflated them. Consequence, deliberately: `finish_stuck_uploads
--enable-file-by-file` reads `uploads.auto_publish` from `config.json`, which the template
sets to `false`, so it now leaves drafts. `Guides/UPLOAD_RECOVERY_WORKFLOW.md` is corrected
in the same change; set `uploads.auto_publish: true` for the old behaviour.

**2. Step 10 deleted an existing `Uploaded_Data/` twin.**
`archive_staging_to_uploaded` `rmtree`s its destination, and the fallback called it
unconditionally on success. The module now owns `archive_new_version_staging`, which
**refuses** rather than clobbering — the previous version's archive is the only local record
of what it contained. It is also only called when the record actually left draft state:
`Uploaded_Data/` means "uploaded AND published", and moving a folder there while its record
is still a draft hides it from every recovery tool (they all scan `Staging_Area/`) and
orphans the later publish.

**3. A broken `/draft` blocked the repair it was a symptom of.** A leftover pending file
slot makes Zenodo's serializer 500 on `GET /draft` — exactly what a timed-out ZIP leaves
behind. `upload_to_zenodo` has always tolerated this and resumed via the file-list endpoint
(a different Zenodo handler); the fallback's existence check did not, so it aborted on its
own symptom. It now distinguishes all three outcomes: a dict proceeds, a **404 still
aborts** (letting that fall through would mint a duplicate record), and a 5xx proceeds with
a warning, with `list_draft_files` as the corroborating call.

Also in this change:

- **A hard 100-files-per-record refusal** (`_ZENODO_MAX_FILES_PER_RECORD`, cited to
  <https://help.zenodo.org/docs/deposit/manage-files/>), checked before the hash pass and
  well before the point of no return. ESID 797 had 6270 WAVs: it could never have been
  uploaded file-by-file, and the old code would have discovered that only after deleting
  the ZIP slot. Refusing early leaves the ESID recoverable as a ZIP.
- **`upload_attempts` reaches the WAVs.** `run_file_by_file` gained the keyword and
  forwards it with `zip_filename=None`, which makes it apply to every file rather than to a
  ZIP that does not exist on this path. `finish_stuck_uploads` now forwards its existing
  `--upload-attempts` there too, instead of only using it for the ZIP pass.
- **`only_zip_missing_from_entries`** extracted as a pure predicate, with its fail-open
  behaviour documented at the top of the docstring: an empty companion list makes the test
  vacuously true, so a caller must prove the list is real before trusting a True. The
  network version delegates to it, and a caller that already listed the draft's files avoids
  a second GET.
- **HTTP 429 is now retryable** in both of `standalone_uploader`'s retry loops. Both treated
  every 4xx as fatal — right for 400/401/404, wrong for the one 4xx that retrying fixes.
  429 now routes into the existing backoff and honours `Retry-After` when Zenodo supplies it.

10 new tests (suite 674 → 684), audit 0 gaps.

## July 2026 — make the raw-file hash pass durable instead of optional

The file-by-file fallback verifies every raw WAV and `CONFIG.TXT` against the
SHA-512 in `file_list.csv` before uploading anything. That check is not
negotiable: `file_list.csv` is itself uploaded as the record's manifest, so
skipping it would let a record publish whose files disagree with the manifest
shipped beside them — and the uploader's md5 verification cannot detect that,
since it only proves Zenodo received what was sent.

But the pass read the entire dataset every time and threw the result away. An
upload that died after two hours re-hashed 40 GB from scratch on the next run,
with no log output while it did so — the reported symptom was a silent wall
after `SWITCHING to file-by-file`.

**New `Resources/hash_raw_wavs.py`** makes that work durable. Each raw ESID
folder gets a `wav_hashes.csv` recording every audio file's name, size, mtime
and SHA-512. `file_by_file_upload.py`'s step 3 now loads that cache, reuses
what is still valid, hashes only what is new or changed, and writes it back.
Same abort-on-mismatch semantics; a restart re-reads nothing. Run as a CLI it
pre-warms a whole `Raw_Data` tree so the upload run never waits on hashing —
an optimisation, never a prerequisite, since an un-cached folder is simply
hashed on first use.

**The cache cannot make verification weaker.** An entry is trusted only when
the file's size AND mtime both still match what was recorded — one `stat` per
file, microseconds — so a WAV altered after the cache was written is detected
and re-hashed rather than waved through. A cache keyed on filename alone would
have turned the integrity gate into a rubber stamp, which is the whole trap
this design exists to avoid. Verified by a test that rewrites a file to its
*identical length* and confirms it is still re-hashed. A missing, unreadable or
malformed cache reads as empty, so a bad cache costs time and never
correctness.

Two properties worth recording. `prepare_dataset.py` clamps pre-1980
modification times (an unset AudioMoth clock stamps 1970) to 1980-01-01, so a
re-prep invalidates the entries for exactly those files — correct, and it fails
toward doing more work rather than less. And because
`split_oversized_raw_folders.py` moves WAVs by same-filesystem rename, size and
mtime survive the split, so cached hashes stay valid in whichever half a file
lands in.

The cache file cannot reach Zenodo: the upload set comes from `file_list.csv`,
and `prepare_dataset.py` only ever takes `*.WAV`/`*.wav` and `CONFIG.TXT` out of
a raw folder.

Also considered and rejected in the same session: a `--skip-raw-hash` flag to
bypass the pass outright. It was implemented and reverted — going faster is not
worth being unable to prove a published record matches its own manifest. The
cache achieves the same wall-clock saving without giving anything up.

26 new tests (suite 636 → 662), audit 0 gaps.

## July 2026 — stop paying for a ZIP attempt that cannot change the outcome

`finish_stuck_uploads.py --enable-file-by-file` runs Phase A first — a full
shell-out to `standalone_tasks.py` — before the switch decision is evaluated in
Phase B2. For an ESID that is *about to be switched*, that pass re-hashes the
entire archive in `verify_dataset_integrity` and then attempts an upload of a
ZIP the run is about to abandon. On a 40 GB ZIP on an external volume that is a
long time spent on a foregone conclusion.

It happens because the `mode: file_by_file` marker is not written until the
point of no return *inside* `run_file_by_file`, so on the switching run the ESID
still looks like an ordinary ZIP-mode dataset to Phase A. Subsequent runs are
already fine — `standalone_tasks.py` skips the folder at its two
`read_upload_mode` guards.

Two additions, neither of which changes default behaviour:

- **`--skip-integrity-hash` is now forwarded.** `standalone_tasks.py` has always
  supported it, but `run_recovery` built its command line without it, so there
  was no way to reach it through this tool. It drops only the full ZIP SHA-512
  re-hash; the structural checks (sentinel, readable archive, ZIP contents vs
  `file_list.csv`) still run. Useful on the ordinary ZIP path too, where that
  hash otherwise repeats on every recovery run.
- **`--force`** switches an ESID immediately: the
  `number_of_tries >= --tries-threshold` condition is **not applied**, and the
  ESID is removed from Phase A so the ZIP is not retried. An ESID can therefore
  be switched on its very first failure. The threshold exists so one bad night
  does not abandon a ZIP that would have succeeded; `--force` is the operator
  saying they have already made that call.

`--force` deliberately does NOT bypass `only_zip_missing`. File-by-file
*replaces* the ZIP, so when a companion is also missing the problem is not ZIP
size and the switch is the wrong remedy — those ESIDs are reported by name and
left to the normal pass. Nor does it make the switch reversible: it is still a
one-way door, because reverting would mean deleting files already committed to
the record, and the log states that before acting. It requires
`--enable-file-by-file`, since the switch it modifies only exists there — the
error message points at `--skip-integrity-hash` for anyone who just wanted the
ZIP path to stop re-hashing.

This also resolves a circularity in the old design: reaching
`--tries-threshold` required Phase A runs, and each Phase A run was exactly the
multi-hour ZIP attempt the operator was trying to stop paying for. Worse,
because `verify_dataset_integrity` runs *before* `upload_to_zenodo` and
`number_of_tries` is incremented *inside* the uploader, a ZIP that fails the
integrity gate never increments its counter at all — so a genuinely corrupt ZIP
could never accumulate tries toward the threshold on its own, even though
file-by-file (which ignores the ZIP entirely) is precisely its remedy.
`--force` is the way out of both.

10 new tests (suite 626 → 636), audit 0 gaps.

## July 2026 — new versions of published records (the last manual dead-end)

Published Zenodo files are immutable, so a record with wrong metadata AND a
broken ZIP could only be fixed by a new version — and nothing here did that.
`reprep_incomplete_staging.py` has always refused those rows outright ("needs a
manually reviewed new-version upload, never automated here"), and the
remediation backlog in this file names them as the class of damage with no
remedy. `Resources/new_version_upload.py` closes it.

**Three primitives added to `standalone_uploader.py`** — it already was the
hardened REST client (13 endpoints, four retry strategies, five importers), so
no second client and no new dependency:

- `get_published_record` — `GET /records/{id}`. The only source for
  `metadata.version` (to bump), the `versions.*` flags, and
  `parent.pids.doi.identifier`, the **concept DOI, which nothing in this
  project previously recorded**. Doubles as the post-publish confirmation.
- `create_new_version_draft` — `POST /records/{id}/versions`, **deliberately
  single-shot**. This is the same non-idempotent-POST hazard documented for
  draft creation: a 5xx may still have created the draft, so a blind retry
  would either put a second draft on the chain or fail outright. A caller that
  sees it raise must re-inspect and let a human adopt or discard.
- `update_draft_metadata` — `PUT /records/{id}/draft`, the first metadata
  mutation of an existing draft anywhere in this codebase (the creation body is
  built once, inside `if not is_resume:`). Retried on 5xx *because* a
  full-representation replace is idempotent.

No generic POST/PUT retry primitive was added, on purpose: a generic helper
invites wrapping the versions POST, which is exactly the bug
`_create_draft_with_guarded_retry` exists to prevent.

**Two decisions carry most of the safety.**

*The title guard needed no change.* A new version legitimately shares its title
with the published parent, which is precisely what the guard raises
`DuplicateTitleError` on — but the conditional is
`if title_guard and not existing_draft_id`, so creating the version draft
first and handing its id to `upload_to_zenodo` bypasses the guard entirely.
The whole file-upload → verify → DOI tail then runs unchanged and
`tests/test_uploader_title_guard.py` stays green.

*`files-import` is NOT called.* Linking the previous version's files would be
free, but the reconciliation loop leaves remote entries absent from the upload
list untouched — so an imported v1 file whose name the corrected package does
not use would ride forward onto the published new version permanently. For the
case this tool exists to fix, that file is the corrupt ZIP. Starting from an
empty draft makes the invariant checkable in both directions, which is what the
completeness gate enforces: the file set on the new version equals the new
package, exactly. (The demo dry run caught a real instance of this — a
`STALE_NOTES.csv` present on the published version and dropped by the
re-prepped package.)

**Also never called: `submit_to_community_review`.** A new version inherits
community membership through the shared parent; re-submitting it would put it in
a queue where a manager's *accept* publishes it — the race `--defer-zip` exists
to avoid. Enforced by `submit_review=False` plus a tripwire test.

**Other load-bearing details.** `state_file_path=None`, and the tool's own state
file carries no `record_id` key: with one, the staging folder would match
`finish_stuck_uploads.discover_stuck_esids()` and a well-meaning recovery run
would resume the new-version draft through the main pipeline with
`submit_review=True`. A cross-tool test pins that it stays invisible.
`archive_staging_to_uploaded` is left alone — it `rmtree`s its destination,
which for a versioned ESID would destroy the previous version's archive — so the
tool owns a small replacement that moves to `ESID_NNN_Uploaded_<label>/` and
**refuses rather than deleting**. The PUT body is an echo-merge of exactly five
keys (`access`, `files`, `metadata`, `custom_fields`, `pids`): a full replace
that omitted `pids` would strip a reserved DOI, and dump-only fields would 400.

**`--publish` is OFF by default even under `--execute`, and that default is the
rollback strategy** — every state up to publication is undone by discarding one
draft, and nothing before publication can touch the record being superseded.
Version labels advance by a trailing letter (`2024.1.0` → `2024.1.0a`), refusing
anything ambiguous (`z`, uppercase, multi-letter — which is what stops
`1.0-beta` becoming `1.0-betb`) with a message naming `--version-label`.

There is no sandbox account for this project, so the **dry run is the
compensating control**: two read-only GETs, no writes, and it prints the fully
constructed URLs, a per-key metadata diff, the file plan including anything not
carried forward, and the exact READ/WRITE call sequence with a NOT-CALLED block.
Six behaviours could not be verified without a sandbox and are listed in the
tool's docstring to be checked on the first real run.

Also fixed: `Guides/TEST_UPLOAD_GUIDE.md` told you to export
`INVENIO_RDM_BASE_URL` **without** a trailing slash, while
`get_credentials_from_env` does not normalise it — yielding
`https://zenodo.org/apirecords/...`. The guide now says slash, and the new tool
refuses to run without one rather than silently normalising shared code.

75 new tests (suite 551 → 626), audit 0 gaps.

## July 2026 — splitting sites too large for one Zenodo record

**Why sites get split at all** — nothing in this repo previously recorded it,
including for the seven sites already split (012, 122, 243, 692, 929, 930, 963).
Zenodo caps a record at **50 GB**. A site whose raw data exceeds that cannot be
one record, so it becomes two: the raw folder is renamed
`ESID#NNN_Part_1_of_2` and a sibling `ESID#NNN_Part_2_of_2` takes the later half
of the recordings. The suffixed-ESID grammar in `azus_common` (canonical
`NNN_Part_1_of_2`, display form `NNN Part 1 of 2`) exists precisely to carry
these through prep, upload, titles, and the report tools.

**One thing that is easy to get wrong:** `prepare_dataset.extract_collector_data`
matches the ESID with a raw `==` against the collectors spreadsheet and exits 1
on a miss — there is no suffix stripping and no fallback to the base 3-digit
ESID. A split site therefore needs its own `NNN_Part_1_of_2` /
`NNN_Part_2_of_2` rows, and the established convention (visible in all seven
existing splits) is to *replace* the bare `NNN` row with two rows identical
except the `ESID` cell.

**New `Resources/split_oversized_raw_folders.py`** performs the mechanical fill
of Part 2: copy the non-WAV companions (SHA-512 verified), order the WAVs by
filename timestamp, and move the later half so the halves are close to equal in
bytes. Dry-run by default; `--perform-split` is the only thing that mutates.

- **The plan is derived from the UNION of both folders**, never Part 1 alone.
  The union does not change as files move, so a fresh run and a half-finished
  run compute the identical cut: an interrupted run re-plans to the same
  boundary and `--resume` moves only what is left, a completed pair re-runs as
  `ALREADY_SPLIT`, and no journal or resume state is needed — the filesystem is
  the record.
- **The move copies no bytes in the normal case.** Part 1 and Part 2 are
  siblings, so `os.rename` moves each WAV atomically; verification is by size
  and source-disappearance, because `rename(2)` within a filesystem has no
  partial state for a hash to detect and reading back tens of GB per pair would
  add hours for nothing. That post-check does catch the one real failure — a
  filesystem reporting one `st_dev` while copying anyway (firmlinks, overlay
  mounts) — and aborts the whole pair if it fires. A genuine cross-device move
  copies via `.partial`, compares SHA-512 both sides, and unlinks the source
  only after agreement, so a failure leaves a duplicate rather than a gap.
- **`upload_state.json` is never copied** — it binds a folder to ONE Zenodo
  draft, so duplicating it would point two records at the same draft. Nor are
  dotfiles (`.DS_Store`, `._*` sidecars, `.prep_complete`), ZIPs,
  subdirectories (interrupted preps leave `ESID_*_Staging/` in `Raw_Data/`), or
  symlinks. Every skip is reported.
- **Hidden files are skipped on BOTH sides**, including one with a `.wav`
  extension. `is_split_wav_name` is deliberately stricter than
  `audit_wav_integrity._is_wav_name`, which excludes only AppleDouble sidecars
  (`._foo.WAV`) and would therefore accept `.hidden.wav` as a recording. Had
  the two predicates been allowed to disagree, a dot-prefixed file would have
  been excluded from the companion copy while still being moved into a record
  as though it were audio. A hidden WAV is now left in place, excluded from the
  plan (so it cannot shift the cut), and reported.
- **Sizes must be trustworthy before the cut is believed.** A cloud placeholder
  that `stat`s as 0 bytes while being readable, or a WAV whose size cannot be
  read at all, refuses the pair — the latter matters because
  `scan_disk_wavs` omits an unstattable file from its `sizes` map entirely, so
  it would otherwise be invisible to the plan and silently stranded in Part 1.
  A *truncated* WAV is only warned about: its size is real, so the split
  arithmetic is still correct, and refusing would block data that genuinely
  exists in that state.
- Names carrying no parseable timestamp (8-hex old-firmware names like
  `5D8F3A2B.WAV`) sort last deterministically and get their own report column
  plus a warning — for those the cut is a *name* boundary, not a time boundary,
  and the report must not imply otherwise. `19700101_*` reset-clock names parse
  and sort naturally to the front. Filesystem mtimes are deliberately never
  consulted: `prepare_dataset.py` rewrites pre-1980 mtimes via `os.utime`, so
  mtime is a mutated field here, not evidence of recording time.
- Also reported, never blocking: a half that would **still** exceed the limit
  (a 120 GB site halves to 60/60 and both fail — that site needs more than two
  parts), a missing or stale collectors row, and a surviving pre-split
  `Staging_Area/ESID_NNN_Staging` whose ZIP and `file_list.csv` are now stale
  and which may hold a live draft for the un-split site.

Reuses `audit_wav_integrity`'s `scan_disk_wavs` / `_is_wav_name` / `human_size` /
`compare_file_maps` and `clean_raw_staging_leftovers`' safety model (dry-run
default, classify/mutate separation with the mutating path re-deriving every
gate, `REFUSING to …` refusals, inode-based identity, per-row CSV flush).
62 new tests (suite 486 → 548), audit 0 gaps.

## July 2026 — file-by-file fallback: keep a history of both upload manifests

The file-by-file fallback overwrites `ESID_NNN_to_upload.csv` in place, so the
manifest that the ZIP attempt actually used was lost the moment the fallback
engaged — leaving no way to see afterwards what each strategy tried. The
rewrite in `Resources/file_by_file_upload.py` now brackets itself with two
provenance copies:

- **`ESID_NNN_zip_attempt_upload.csv`** — the manifest AS IT WAS for the ZIP
  attempt, copied *before* the overwrite. Written **once**: on a re-run the
  live manifest is already the file-by-file version, so copying again would
  replace the original history with a duplicate of the new set. The snapshot
  precedes the destructive write, so an I/O failure there aborts the ESID with
  the original manifest still intact.
- **`ESID_NNN_file_by_file_upload.csv`** — a mirror of the rewritten manifest,
  refreshed each run.

Both names live in `azus_common` (`MANIFEST_ARCHIVE_ZIP_ATTEMPT`,
`MANIFEST_ARCHIVE_FILE_BY_FILE`) because `prepare_dataset.py` has to agree on
them in two places — exactly the cross-tool drift that module exists to
prevent:

- `create_upload_manifest` **excludes** them, so a re-prep never publishes
  local provenance files as dataset content.
- `_UPLOAD_ARTIFACT_PATTERNS` now **preserves** them across a re-prep. That
  constant previously covered only the draft-link artifacts
  (`upload_state.json`, `ESID_*_request_log.json`); a re-prep is precisely when
  the record of prior attempts matters most, so the rmtree must not take it.

Neither copy is ever uploaded — they are absent from the upload list the module
builds and excluded from the manifest prep generates. 6 new tests (including
first-ever coverage of `create_upload_manifest`); suite 480 → 486, audit 0 gaps.

## July 2026 — esid_record_report: Upload Date + Last Updated columns

`Resources/esid_record_report.py` (the Zenodo-side "ground truth" inventory)
gains two date columns before `ERROR?`: **Upload Date** (the record's Zenodo
`created`, date portion) and **Last Updated** (its `updated`, date portion).
A "date published on Zenodo" column was considered and dropped — the API
listings expose no clean publish timestamp (only `created`, `updated`, and the
user-set `metadata.publication_date`, which for these datasets is the eclipse
date, not the publish moment). The default output is now
`Records/YYYYMMDD_HHMMSS_esid_record_report.csv` (timestamp-first, folder
auto-created). 3 new tests.

## July 2026 — File-by-file upload fallback for ZIPs that keep timing out

Large data ZIPs sometimes time out mid-transfer, losing hours per attempt.
A new OPT-IN fallback uploads a stuck ESID's individual WAVs (straight from
`Raw_Data/`) + `CONFIG.TXT` + the standalone companions to the SAME existing
Zenodo draft, in place of the ZIP.

- **New `Resources/file_by_file_upload.py`** — reuses
  `standalone_uploader.upload_to_zenodo` (resume + per-file md5 verify) with
  a swapped file list. Safety gates: the required set is derived from the prep
  `file_list.csv` + manifest (a missing/under-collected file aborts before any
  upload); each raw WAV/CONFIG is SHA-512-checked against the manifest; a 404
  draft aborts (never mints a duplicate); a COMMITTED (successful) ZIP aborts
  the switch and is left untouched (only a failed/incomplete ZIP slot is
  cleared — file-by-file engages only when the ZIP keeps failing); and the
  record is submitted/published ONLY after a completeness gate confirms every
  required file is committed and the ZIP is gone.
- **`finish_stuck_uploads.py`** gains `--enable-file-by-file`,
  `--tries-threshold N` (default 3), `--raw-data-dir PATH`. It continues
  already-file-by-file ESIDs and auto-switches a ZIP-mode ESID only when the
  ZIP is the SOLE missing file AND `number_of_tries >= threshold`. Default
  behavior (no flag) is unchanged.
- **Requirement 9** — `standalone_tasks.py` SKIPS any staging folder marked
  `mode: file_by_file` in `upload_state.json`, at TWO guard points
  (`get_upload_data` discovery, and the top of `_process_one_dataset_inner`
  before the ZIP integrity gate), so the ZIP and file-by-file paths never
  touch the same record.
- **State-writer hardening** — `upload_state.json` is now written by
  read-MERGE (`_write_upload_state`), preserving the `mode` marker across
  resume writes; `azus_common.read_upload_mode` is the shared reader.
- 26 new tests (`test_file_by_file_upload.py`, `test_req9_skip.py`,
  `test_state_merge.py`, `test_finish_stuck_file_by_file.py`); suite at 476.
  Reverting file-by-file → ZIP is intentionally NOT automated (it would
  require deleting committed files); set `--tries-threshold` high to suppress
  auto-switching, and reset manually if a revert is ever needed.

## July 2026 — `number_of_tries` attempt counter in `upload_state.json`

Every upload attempt against an ESID record (fresh create, resume,
`--defer-zip` phase 1, `finish_stuck_uploads.py` recovery) now advances
a `number_of_tries` counter in the record's `upload_state.json` —
written in the single place the state file is produced
(`standalone_uploader.upload_to_zenodo`), logged as "attempt #N".
Semantics: initial value 0; the first attempt writes 1. A legacy state
file without the field is treated as 0 and gains the field on its next
attempt; corrupt/negative values count from 0 with a warning instead of
failing an upload over bookkeeping. `--restore-states` writes restored
links with the initial value 0 (restoring is not an attempt). The
counter survives re-prep via the artifact stash. All state-file readers
use key-specific access, so the extra field is backward/forward
compatible everywhere (test-proven). `list_upload_states.py` gains a
"Number of Tries" CSV column — legacy files show "0 (legacy)" so a
blank cell can't be misread. 10 new tests; suite at 265.

## July 2026 — Supervisory-review remediation: reliability, de-drift, write-path tests, I/O

A three-pass code review (core pipeline / tool suite / test coverage) found
strong integrity engineering with three systemic weaknesses: unattended-
reliability gaps, copy-paste drift across the 11 Resources tools, and test
coverage inverted against risk. Fixed in five phases:

### Phase 1 — Reliability quick wins

- **HTTP timeouts on all 11 Zenodo calls** (`standalone_uploader.py`,
  `_REQUEST_TIMEOUT = (10, 300)`). A half-open socket used to hang a batch
  forever WITHOUT triggering the retry machinery (no exception, no retry);
  now it surfaces as a retryable error. Read timeout is per-socket-operation,
  so healthy multi-hour PUTs are unaffected.
- **Unexpected exceptions no longer masquerade as upload failures**:
  `upload_to_zenodo` catches only the known failure families (HTTP/transport/
  DuplicateTitle/FileIntegrity/file errors); anything else propagates and is
  logged with a full traceback ("UNEXPECTED ... likely a code bug") while the
  batch continues.
- **Upload-artifact stash moved to disk** (`prepare_dataset.py`): re-prep used
  to hold `upload_state.json`/request-log only in RAM across the `rmtree` —
  a kill in that window orphaned the Zenodo draft (duplicate-record risk).
  The stash now lives in `Staging_Area/.<name>.artifact_stash/`; a stale
  stash from a killed run is restored automatically by the next run.
- **`--yes` flag** for `standalone_tasks.py`; a non-TTY run without it exits 2
  with a clear message instead of crashing on `input()` (cron/CI safe).
- **Ctrl+C responsiveness** (`--workers N`): interrupt now cancels queued
  datasets, workers check an abort event between datasets, and the uploader
  checks it between files — interrupted drafts stay resumable. New
  "Aborted" stat in the summary.
- Exit-code conformance (usage errors = 2) in `audit_prep_completeness.py`
  and `reprep_incomplete_staging.py`; the in-place-refusal test made fully
  hermetic (runs a copied script in a throwaway project tree).

### Phase 2 — `Resources/azus_common.py`: one definition of everything shared

New stdlib-only shared module: `PROJECT_ROOT`/`STAGING_AREA`/`UPLOADED_DATA`,
`PREP_SENTINEL`, `STATE_FILENAME`, `parse_esid()` (THE single ESID parser,
000–999 bounds-checked), `find_esid_folders()`, `calculate_sha512()` /
`calculate_digests()`, `configure_logging()`, `timestamped_output_path()`.
Migrated: 8 copies of project-root discovery, 6 copies of the ESID folder
regex (which had drifted into 4 variants), 3 sentinel definitions, 2 sha512
implementations, plus the divergent ESID parsers inside `standalone_tasks.py`
(`get_esid_file_pairs` could return "v2" for `ESID_005_v2.zip`; now all sites
agree). `prepare_dataset.get_esid_from_folder` now always returns the padded
3-digit form (unpadded names used to leak through and evade skip checks).

Also: **the completeness truth-set now lives in the producer** —
`prepare_dataset.py` exports `STAGING_OUTPUT_FILES`/`ZIP_METADATA_ENTRIES`/
`CONDITIONAL_FILES`/folder templates, and `audit_prep_completeness.py`
imports them instead of a hand-copied mirror. The auditor also reads ZIPs
with Python `zipfile` (ZIP64-capable) instead of parsing `unzip -l` output —
the suite's only external-binary dependency is gone.

### Phase 3 — Write-path test suites (the risk-weighted coverage gap)

Five new suites, 112 tests, all hermetic/offline:
`test_prepare_dataset_atomic_move.py` (stash/restore, crash-window recovery,
full e2e re-prep round-trip), `test_uploader_resume_heal.py` (verified-skip,
size/md5 heal, fail-closed unreadable-local, post-commit verification,
abort), `test_uploader_title_guard.py` (normalization, both serializations,
adopt-vs-fail, fail-closed search, DOI-reservation idempotency),
`test_metadata_builders.py` (creators/contributors/fundings/get_draft_config
golden payloads, recording dates, manifests, upload-data assembly),
`test_persistence_layer.py` (UploadTracker, result CSVs, request-log
recovery, stuck-ESID discovery, restore-state). Also fixed a real bug the
tests exposed: `get_draft_config` crashed on a collector with no Keywords
(`subjects=None`) and could emit empty Subject entries.

### Phase 4 — I/O efficiency on multi-GB files (safety unchanged)

- `create_zip_file` now hashes WHILE compressing (one read per source file;
  it silently read every raw WAV twice before).
- The integrity gate computes SHA-512 **and** md5 in one read
  (`calculate_digests`) and hands the verified md5 to the uploader
  (`known_md5s`), eliminating the uploader's separate full read of the same
  ZIP. Digests are only handed over when the archive VERIFIED; if the file
  changes afterwards, the post-commit checksum comparison fails the dataset.
- `file_list.csv` gains a **"File size (Bytes)"** column (documented in
  `file_list_data_dict.csv`); the integrity gate compares exact bytes (two
  sizes can round to the same 2-decimal KB), falling back to the KB compare
  for legacy manifests. Net effect for one clean prep+upload of a 43 GB
  dataset: raw data read once instead of twice, ZIP read twice (hash + PUT)
  instead of four times.

### Phase 5 — Docs and CI

README: the three newest tools (`esid_record_report.py`,
`reprep_incomplete_staging.py`, `list_esids.py`), a "Running the Tests"
section, and unattended-run guidance. New `.github/workflows/tests.yml` runs
the suite on Python 3.11/3.12 on every push/PR.

Known/documented (not fixed here): template-hygiene — an unfilled
`project_config.json` passes empty strings into Zenodo payloads with no
early validation; `save_result` accepts but does not persist the ZIP path.

Full suite after all phases: **255 tests, all passing.**

---

## July 2026 — Verified-integrity uploads: no unverified ZIP reaches Zenodo

Incomplete ZIPs (missing WAV files) were uploaded to Zenodo from the
production server and downloaded by researchers. Root cause: every
safeguard lived on the PREP side and the upload side consumed none of
them — `get_upload_data()` uploaded any `Staging_Area/` folder that
merely contained an `ESID_*.zip`; the `.prep_complete` sentinel, the
per-file SHA-512 hashes in `file_list.csv`, and the ZIP's recorded hash
were never read at upload time; and the resume path skipped
already-committed files by NAME alone, so a short ZIP committed once
stayed on the draft forever, even after the local ZIP was fixed.

Fixed with four independent layers:

### Layer 1 — pre-upload integrity gate (`standalone_tasks.py`)

New `verify_dataset_integrity()`, called from `_process_one_dataset()`
before any network work. Cheapest check first: `.prep_complete`
sentinel present → ZIP readable → every WAV in `file_list.csv` present
in the ZIP at the recorded size (and no unlisted WAVs) → ZIP SHA-512
matches the manifest's ZIP row. Any problem marks the dataset FAILED
(`DatasetIntegrityError` row in failed_results.csv) and nothing
uploads. The gate fails closed — if the check itself crashes, the
dataset fails. New `--skip-integrity-hash` flag skips ONLY the
full-archive re-hash; the structural checks always run. The revived
`calculate_sha512()` (previously dead code) does the hashing.

### Layer 2 — verified resume (`standalone_uploader.py`)

Resume skip logic now verifies each `status == "completed"` entry
against the local file: size first (cheap), then md5 when Zenodo
provides one. A mismatched remote copy is deleted and re-uploaded —
this self-heals every draft carrying a short ZIP on its next resume
(`finish_stuck_uploads.py` inherits the fix with zero changes). If the
LOCAL file cannot be read, the dataset fails via the new
`FileIntegrityError` instead of guessing (deleting the remote copy
there could destroy the only good bytes).

### Layer 3 — post-commit verification (`standalone_uploader.py`)

`upload_file_to_draft()` captures the local file's size and md5 before
upload and, after the commit, compares them against what Zenodo reports
it now holds. A mismatch deletes the corrupt slot and raises
`FileIntegrityError` — a silently corrupted transfer can no longer
survive on a record.

### Layer 4 — prep hardening (`Resources/prepare_dataset.py`)

- New Step 8b `verify_zip_against_source()`: after the ZIP is final and
  BEFORE the atomic move + sentinel, the archive is compared against a
  FRESH scan of the raw folder (reusing `audit_wav_integrity`'s
  double-checked scanners: disk stat vs RIFF header, ZIP size vs CRC;
  per-file name + size match). A short or drifted ZIP exits nonzero —
  it never becomes an uploadable staging folder and never earns
  `.prep_complete`. The fresh scan deliberately catches WAVs that
  appeared on disk mid-prep.
- In-place builds refused: `--output-dir` (or a raw folder) resolving
  inside `Staging_Area/` used to skip the two-phase atomic move, letting
  the ZIP grow non-atomically under the exact folder name the uploader
  scans — the direct path by which incomplete ZIPs became uploadable.
  Now rejected at startup with a clear error, and the old
  "already in Staging_Area" move-skip branch is a hard stop.

### Tests

New `tests/test_upload_integrity.py` (gate, remote-entry mismatch
helper, md5 helper) and `tests/test_prepare_dataset_verification.py`
(ZIP-vs-source verification incl. the late-sync case, in-place-build
refusal via subprocess). Full suite: 91 tests, all passing.

### Remediation of existing damage

- Drafts with short ZIPs: healed automatically by Layer 2 on the next
  `finish_stuck_uploads.py` run.
- Published records with short ZIPs: find them with
  `Resources/audit_wav_integrity.py` over the raw data; each needs a
  re-prep and a new Zenodo version (published files are immutable).

---

## July 2026 — `audit_wav_integrity.py` QC hardening: every size double-checked

Reported symptom: folders labelled as having zero-byte WAVs that did
not. QC audit found the tool trusted a SINGLE size measurement and had
several silent-error paths:

- `stat().st_size` was trusted blindly. On the project's Dropbox volume,
  cloud "online-only" / macOS dataless placeholder files can report
  `st_size == 0` while the real bytes are in the cloud → falsely counted
  as zero-byte. (Most likely the reported symptom.)
- Match was decided on aggregate `(count, total_bytes)`. Two different
  file sets with the same count and total (e.g. two WAVs with swapped
  sizes, or a truncation offset by padding) passed as a FALSE `Match=YES`.
- macOS AppleDouble sidecars (`._name.WAV`) end in `.wav` and were
  counted as recordings, inflating counts and creating phantom tiny WAVs.
- The `Disk WAV GB` column rounded any folder under ~5 MB to `0.0` — also
  readable as "zero".

Rewrite (same CLI; simpler pure-function core):

- **Every size measured two independent ways, with a Cross-Check column
  per side.** Disk: `stat` size vs the WAV's own RIFF/WAVE header
  (declares `filesize − 8`); disagreement is flagged, never trusted. A
  `stat=0` file whose header is readable is flagged as a placeholder —
  NOT counted as zero-byte (fixes the symptom). Truncated files
  (`stat < header`) are now caught. ZIP: `file_size` vs CRC/compressed
  size (a `size=0` entry must have `CRC=0`); a nonzero CRC means the size
  field is unreliable.
- **Match is now per-file (`{name: size}`), not aggregate** — a swapped
  or truncated WAV is caught even when counts and totals coincide.
  Duplicate basenames inside the ZIP are detected explicitly.
- **Aggregate self-check (belt-and-suspenders):** every file is recorded
  once in a per-side ledger; `WavStats.verify()` independently re-derives
  the reported count, total bytes, size map, and zero/tiny counts from
  that ledger and confirms they match the incrementally-accumulated
  fields. A summary line reports `Internal self-check: PASSED/FAILED`;
  a failure marks the ESID a problem (exit 1) and prints "DO NOT trust
  the totals for these rows". Catches accumulation-logic bugs, not just
  per-file measurement errors.
- AppleDouble `._*` sidecars skipped and counted separately.
- `Disk/ZIP WAV Size` human-readable columns (B/KB/MB/GB) replace the
  rounding-to-0.0 GB columns; exact `Bytes` columns remain authoritative.
- Exit 1 on any cross-check discrepancy, not just mismatches/zero/tiny.

Tests: `tests/test_audit_wav_integrity.py` — 33 stdlib-unittest tests.
Pure-function tests for RIFF parsing, disk classification (incl. the
placeholder-zero symptom and truncation), ZIP classification (incl. the
size-0/CRC-nonzero lie), and per-file Match (incl. the aggregate
false-YES regression: A=500/B=2000 vs A=2000/B=500 now correctly `NO`);
real-file + real-ZIP integration (sidecar/nested exclusion, duplicate
basename, corrupt ZIP); and end-to-end CLI exit codes. Full `tests/`
suite: 59 tests, all passing.

## July 2026 — `find_missing_esids.py` QC hardening + first unit-test suite

The tool produced FALSE-COMPLETE results (genuinely missing ESIDs not
reported). QC audit found the defect class: an ESID that never makes it
into the parsed master set can never be flagged missing, and three code
paths lost ESIDs silently — (1) `re.sub(r"\D","",cell)` concatenated
every digit in a cell (`"ESID 073 (2024)"` → 732024; Excel float
`"73.0"` → 730), (2) first-substring column matching could bind to the
wrong ESID-like column, (3) blank/garbled cells were skipped without a
trace.

Rewrite (same CLI + new `--master-column`/`--report-column` overrides):

- Hard project invariant enforced: ESIDs are 000–999; anything else is
  flagged as a parsing artifact, never included.
- Deterministic per-cell parsing that never guesses (single number, or
  exactly one ESID-prefixed number; Excel floats normalized; everything
  else listed with row number + verbatim cell).
- Deterministic column selection: exact `ESID#`/`ESID` match beats fuzzy;
  multiple fuzzy candidates are an exit-2 error, not a guess; the chosen
  column and sample raw→parsed values are echoed.
- Per-file accounting equation, duplicate-value report, encoding
  (UTF-8→cp1252) and semicolon-delimiter guards, runtime self-check of
  the answer's set invariants.
- Exit 0 only when nothing is missing AND every cell parsed — data-
  quality problems force exit 1 with a "RESULTS MAY BE INCOMPLETE"
  banner, so a false complete cannot hide.

New `tests/` directory (repo's first): `tests/test_find_missing_esids.py`
— 26 stdlib-unittest tests: per-cell parsing, column selection, encoding/
delimiter guards, three false-complete regression tests, a 200-iteration
randomized property test (tool output must equal an independent set
difference), and end-to-end CLI exit-code tests. The suite immediately
caught one bug in the rewrite itself (`sys.exit("msg")` exits 1, not 2 —
fixed with a `_die()` helper). Run with:
`python3 -m unittest discover -s tests`.

## Summary

Transformed AZUS from an Eclipse Soundscapes–specific tool into a **generalizable
citizen science data upload platform**.  All project-specific identity is now in
configuration files, not Python code.

---

## July 2026 — Duplicate prevention (Case-7 root fix) + four visibility defects

Duplicate Zenodo records were being created whenever a staging folder's
link to its draft (`upload_state.json`) was lost — the next run made a
fresh draft. Fixed in three layers plus the four defects that let it
happen silently.

### Layer A — re-prep preserves the draft link (`Resources/prepare_dataset.py`)

Replacing an existing staging folder used to `rmtree` it wholesale,
destroying `upload_state.json` and `ESID_XXX_request_log.json`. The move
block now stashes both in memory before the delete and restores them into
the rebuilt folder after the atomic rename
(`_stash_upload_artifacts` / `_restore_upload_artifacts`). Caveat: a
resumed draft does not re-send metadata — fix descriptions in the web UI
if the re-prep changed them.

### Layer B — request-log fallback (`standalone_tasks.py`)

When `upload_state.json` is missing/unreadable but the folder's request
log still holds the draft's `record_id`
(`_recover_draft_id_from_request_log`), the run resumes that draft; the
uploader rewrites the state file, self-healing the folder.

### Layer C — same-title guard (`standalone_uploader.py`)

Last line of defense when all local evidence is gone. Before creating a
fresh draft, `upload_to_zenodo()` searches `GET /user/records` for the
intended title (exact normalized match client-side; handles both Zenodo
serializations):

- matching unpublished draft → adopted/resumed instead of duplicated
- matching published record → new `DuplicateTitleError` → dataset fails
  with a clear message; nothing is created; folder stays for review
- search failure → fail-closed (a failed run is retryable, a duplicate
  is permanent)

New CLI escape hatch: `--skip-title-guard` (default: guard ON). Resume
paths — including everything `finish_stuck_uploads.py` does — bypass the
guard naturally. `_api_get_with_retry` gained an optional `params`
passthrough for the search query.

### Layer D — four visibility/robustness defects fixed

1. `stats["skipped"]` was initialized and printed but never incremented —
   `get_upload_data()` now takes the shared stats dict and counts tracker
   skips, and each tracker-skipped dataset is named at INFO.
2. An ESID folder with no ZIP was skipped with zero logging — now a
   WARNING plus a `failed_results.csv` row
   ("No ZIP file found in staging folder").
3. A broken upload manifest raised out of `create_upload_data()` and
   aborted the whole batch — now isolated per ESID: error logged, failure
   row written, remaining datasets continue.
4. `finish_stuck_uploads.py` hid no-state folders at DEBUG — discovery
   now returns them, and they are listed at INFO with a pointer to
   `diagnose_missing_states.py`, plus a count in the found-summary.

---

## July 2026 — `Resources/diagnose_missing_states.py`: why is upload_state.json missing?

Investigation finding: `finish_stuck_uploads.py` only attempts folders
that HAVE `upload_state.json` — no-state folders are skipped at DEBUG
level and never counted in its summary, so they are silently excluded
from every recovery run. The state file is only created once a run
reaches draft creation, and the pipeline has multiple
silent-or-nearly-silent ways to stop short of that (no ZIP in folder:
zero logging; tracker skip: aggregate count only; collectors-CSV row
missing; Phase-1/config failures; draft-POST failures; re-prep wiping
the folder; state-write failure; `--esid` filter exclusion at DEBUG).

New diagnostic (read-only by default):

- For every no-state folder in `Staging_Area/`, gathers: ZIP presence,
  tracker membership, collectors-CSV row, `ESID_XXX_metadata.json`
  (attempt reached upload phase), `ESID_XXX_request_log.json` — whose
  `record_id` proves a live draft exists on Zenodo — success/failure
  results rows, `.prep_complete` mtime, and (with `--log`, repeatable)
  per-ESID lines from azus_upload.log keyed on the pipeline's verbatim
  error strings.
- Classifies each folder with a decision tree ordered like the real
  pipeline (artifacts > success-row > no-ZIP > tracker > collectors >
  failure-rows > no-evidence) and emits Probable Cause + Suggested
  Action per row to `missing_state_diagnosis_YYYYMMDD_HHMMSS.csv`.
- **`--restore-states` (opt-in healer):** where a readable request log
  holds the record_id, writes a fresh `upload_state.json`
  (`"restored_from"` marker included) so `finish_stuck_uploads.py`
  resumes the existing draft instead of a future run minting a
  duplicate. Never overwrites an existing state file.

Known defects recorded during the investigation (separate fixes,
deliberately NOT in this change): `stats["skipped"]` never incremented
(summary always prints 0); no-ZIP folders skipped with zero logging;
`read_upload_manifest` FileNotFoundError propagates uncaught and can
abort a whole category batch; `finish_stuck_uploads.py` should surface
excluded no-state folders at INFO with a summary count.

---

## July 2026 — `Resources/list_upload_states.py`: upload-state listing

Companion diagnostic to the duplicate-record check. Scans
`Staging_Area/` and `Uploaded_Data/` for `upload_state.json` files and
writes one CSV row per file found: `ESID#`, `Location`, `Folder`,
`Record ID`, `Zenodo URL`, `State Created`, `Resumed`, `Notes`.

- Staging rows = drafts with incomplete uploads; Uploaded rows =
  completed uploads and the record each became.
- ESID folders without a state file are counted in the log only (not an
  error — they haven't uploaded yet).
- Unreadable state files / missing `record_id` are flagged in `Notes`;
  exit `1` on any such anomaly, `0` otherwise, `2` on usage errors.
- Read-only, stdlib only, no network. Cross-reference `Record ID`
  against `find_duplicate_records.py` output to spot stray records no
  local folder claims.

---

## July 2026 — `Resources/find_duplicate_records.py`: duplicate-title check on Zenodo

Duplicate records were observed on Zenodo (root cause: a lost
`upload_state.json` forces the next run to create a fresh draft, leaving
the old record behind). New read-only diagnostic:

- Fetches record titles from the project community listing (public API,
  tokenless) and/or the account's own records including drafts
  (`/api/user/records`, token required) — `--scope both|community|account`,
  default `both`, hard-requires the token for any scope touching drafts
  (no silent degradation).
- Reports two duplicate group types to a timestamped CSV: **exact-title**
  (identical normalized titles across non-version records) and
  **same-esid** (same ESID in the title, different title text — catches
  template drift). Records sharing a version group are never flagged.
- Handles BOTH Zenodo serializations (verified against production):
  InvenioRDM shape (`parent.id`, `pids.doi.identifier`, `is_published`)
  and the legacy shape the community listing returns (`conceptrecid`,
  top-level `doi`, `status`). Unauthenticated page size capped at 25 by
  Zenodo — handled automatically.
- Summary separates unpublished strays (safely deletable drafts) from
  published duplicates (curation decision). The tool deletes nothing.
- Exit codes: 0 clean / 1 duplicates found / 2 usage-auth-API error.

First live run (community scope, 83 records) found one real duplicate:
ESID#002 published twice (records 14888401 and 20990631).

---

## July 2026 — `Resources/audit_wav_integrity.py`: per-ESID WAV integrity report

New diagnostic for the ongoing upload failures: rule out bad source
data before burning another 43 GB transfer.

Walks every `ESID_NNN` subfolder of a raw-data folder and writes one
CSV row per ESID (`wav_integrity_report_YYYYMMDD_HHMMSS.csv` in the
cwd, `--output` to override) comparing side by side:

- Disk `.WAV` files (top level of the raw folder only): count, exact
  bytes, GB, zero-byte count, tiny count (`0 < size <
  --tiny-threshold`, default 1024 bytes).
- `.wav` entries inside the matching `ESID_NNN.zip` (auto-located in
  `Staging_Area/` then `Uploaded_Data/`): same columns, from the ZIP
  index only (no extraction), uncompressed sizes.
- `Match` verdict (`YES`/`NO`/`N/A`) plus a `Notes` column explaining
  every non-clean state (no ZIP, unreadable ZIP, files on one side
  only, differing totals).

`--verbose` names every offending file in the log. Exit `1` when any
zero-byte/tiny WAV, mismatch, or unreadable ZIP is found; `ZIP Not
Found` alone is informational. Stdlib only (`zipfile`), no new
dependencies, no changes to any existing file.

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
