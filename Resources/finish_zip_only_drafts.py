#!/usr/bin/env python3
"""Scan Zenodo for drafts that are ONLY missing a complete ZIP archive.

PURPOSE
=======
A ZIP-mode upload puts one multi-GB archive plus a handful of small
companion files on a Zenodo draft.  On the largest sites the archive
times out over and over, leaving a draft that holds every companion and
no audio.  ``file_by_file_upload.py`` repairs exactly that state — it
uploads the individual WAVs from ``Raw_Data/`` in place of the ZIP — but
until now the only way to reach it was
``finish_stuck_uploads.py --enable-file-by-file``, which discovers work
by scanning ``Staging_Area/`` for ``upload_state.json``.

That is the wrong index.  A production run found 138 staging folders with
no state file at all: the local pointer to the draft had been lost, so
those records were invisible to every recovery tool even though the
drafts were sitting on Zenodo, complete but for the ZIP.

This tool inverts the discovery.  It asks **Zenodo** what drafts exist,
matches each one back to a local ESID, and classifies it.  A draft whose
companions are all committed and whose ZIP is not is the repairable case,
and the local ``record_id`` is recovered from the listing rather than
required as input.

READ-ONLY BY DEFAULT
====================
Without ``--execute`` the tool performs one paginated listing plus one
``GET /draft/files`` per candidate and writes two CSV reports.  It makes
no writes of any kind — not to Zenodo, not to ``upload_state.json``.  Read
the summary report, confirm the ``CONVERTIBLE`` set is what you expect,
then re-run with ``--execute``.

THE ONE-WAY DOOR
================
Converting a draft deletes its pending ZIP slot and marks the staging
folder ``mode: file_by_file``.  There is no way back: reverting would mean
deleting audio already committed to the record.  Three things guard it.

* Every gate is re-derived under ``--execute`` from disk and from a fresh
  listing.  Nothing the scan decided is trusted — a draft that changed
  between the scan and the conversion is re-classified.
* ``--limit`` makes the first production run a canary, and
  ``--max-consecutive-failures`` stops the batch after a few failures
  rather than dragging hundreds of records through the door on a systemic
  fault (an expired token, an unmounted volume, a Zenodo outage).
* ``--publish`` is OFF even under ``--execute``.  The normal outcome is a
  complete, inspectable DRAFT.  Publication stays a separate, deliberate
  step.

BUILT FOR A RUN THAT LASTS WEEKS
================================
One conversion is a few hundred requests and up to ~40 GB of upload body;
across hundreds of records this runs for weeks, and an unexpected stop is
a normal event rather than an exception.

* **No batch progress file.**  Every run re-derives state from Zenodo and
  from disk, so there is nothing to corrupt or go stale.  An interrupted
  conversion re-classifies as ``RESUMABLE`` and continues; a finished one
  falls out as ``ZIP_ALREADY_COMMITTED`` or is simply complete.
* **A resume re-reads nothing.**  ``file_by_file_upload`` passes the raw
  folder's cached md5s to the uploader, which confirms an
  already-committed file from the cache instead of re-reading it.  Warm
  the cache first with ``hash_raw_wavs.py --backfill-md5``.
* **The reports never lag reality.**  Each row is written and flushed
  after that ESID is done, with its outcome in it.  A kill loses at most
  the in-flight ESID, whose state is still recoverable from Zenodo.
* **Graceful stop.**  The first Ctrl+C (or SIGTERM) finishes the current
  ESID and exits cleanly with both reports flushed.  A second Ctrl+C
  aborts immediately.

Re-running is always safe and always cheap.

USAGE
=====
::

    # Read-only: what is out there and what could be repaired?
    python Resources/finish_zip_only_drafts.py /absolute/path/to/Raw_Data

    # Canary: convert exactly one, then inspect it in the Zenodo UI
    python Resources/finish_zip_only_drafts.py /path/to/Raw_Data \\
        --execute --limit 1 --yes

    # A specific set, taken from a previous run's summary CSV
    python Resources/finish_zip_only_drafts.py /path/to/Raw_Data \\
        --esid Records/20260729_120000_finish_zip_only_drafts.csv --execute

EXIT CODES
==========
* ``0`` — nothing needs attention (every in-scope draft is benign, or was
  converted successfully)
* ``1`` — at least one draft needs attention: a ``CONVERTIBLE`` row in a
  read-only run, a skip reason worth reading, or a failed conversion
* ``2`` — credentials missing, the listing could not be completed, or a
  usage error.  **No CSV is written**: a truncated scan must never look
  complete.
"""

import argparse
import csv
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# --- Make sibling repo-root and Resources/ modules importable regardless
#     of how this module is invoked. ---
_HERE = Path(__file__).resolve()
_RESOURCES_DIR = _HERE.parent
_PROJECT_ROOT = _RESOURCES_DIR.parent
for _p in (str(_PROJECT_ROOT), str(_RESOURCES_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import azus_common  # noqa: E402
import esid_record_report as err_mod  # noqa: E402
import file_by_file_upload as fbf  # noqa: E402
import hash_raw_wavs  # noqa: E402
from finish_stuck_uploads import _load_publish_config  # noqa: E402
from standalone_uploader import (  # noqa: E402
    _PUT_RETRY_BACKOFF_S,
    Credentials,
    _auth_headers,
    get_credentials_from_env,
    list_draft_files,
)

logger = logging.getLogger("azus.zip_only_drafts")

_STAGING_AREA = azus_common.STAGING_AREA
_STATE_FILENAME = azus_common.STATE_FILENAME

# Zenodo's authenticated page size.  /api/user/records is the only listing
# that carries drafts, and it needs the API token.
_PAGE_SIZE = 100

# Requirement 2 expressed against the binding constraint.  Zenodo accepts
# 100 files per record (https://help.zenodo.org/docs/deposit/manage-files/),
# and a file-by-file record carries every WAV + CONFIG.TXT + every
# companion individually.  The default leaves four slots of headroom, which
# with the ~10 standard companions works out at roughly 85 WAVs — the
# ceiling the workflow asked for.  Checked against the DERIVED set rather
# than the WAV count alone, because it is the total that Zenodo rejects.
_DEFAULT_MAX_FILES = 96

# upload_attempts indexes _PUT_RETRY_BACKOFF_S for the wait BEFORE each
# retry, so attempt N reads index N-2.  Deriving the cap keeps this honest
# if the backoff tuple ever grows; a literal would IndexError instead.
_MAX_UPLOAD_ATTEMPTS = len(_PUT_RETRY_BACKOFF_S) + 1
_DEFAULT_UPLOAD_ATTEMPTS = 3

# Stop a batch on a systemic fault rather than iterating through it.  An
# expired token or an unmounted volume fails every ESID identically; three
# in a row is enough to conclude that and stop.
_DEFAULT_MAX_CONSECUTIVE_FAILURES = 3

# A courtesy pause between per-draft listings.  Zenodo does not document a
# rate limit for these, so this is politeness rather than a requirement.
_DEFAULT_SLEEP_S = 0.2

_SUMMARY_COLUMNS = [
    "ESID#", "Record ID", "Title", "Zenodo URL", "Staging Folder",
    "Raw Folder", "WAV Count", "Companion Count", "Total Files",
    "Entries On Record", "ZIP On Record", "Companions Committed",
    "State File Record ID", "Record ID Written", "Verdict", "Action Taken",
    "Notes",
]

_DETAIL_COLUMNS = [
    "ESID#", "Record ID", "File Name", "Source", "Size (Bytes)", "SHA-512",
    "MD5", "Committed On Record", "Remote Size", "Remote Checksum", "Status",
    "Notes",
]

# --- Verdicts ------------------------------------------------------------
# Actionable: this tool can repair these.
CONVERTIBLE = "CONVERTIBLE"
RESUMABLE = "RESUMABLE"

# Benign: nothing is wrong and nothing needs doing.
ZIP_ALREADY_COMMITTED = "ZIP_ALREADY_COMMITTED"

# Needs a human.
TITLE_UNPARSEABLE = "TITLE_UNPARSEABLE"
DRAFT_STATE_UNKNOWN = "DRAFT_STATE_UNKNOWN"
NO_RECORD_ID = "NO_RECORD_ID"
DUPLICATE_DRAFTS_FOR_ESID = "DUPLICATE_DRAFTS_FOR_ESID"
NO_STAGING_FOLDER = "NO_STAGING_FOLDER"
PREP_INCOMPLETE = "PREP_INCOMPLETE"
MANIFESTS_MISSING = "MANIFESTS_MISSING"
NO_RAW_FOLDER = "NO_RAW_FOLDER"
RAW_FILES_MISSING = "RAW_FILES_MISSING"
TOO_MANY_FILES = "TOO_MANY_FILES"
AMBIGUOUS_LOCAL_FOLDER = "AMBIGUOUS_LOCAL_FOLDER"
UPLOADED_TWIN_EXISTS = "UPLOADED_TWIN_EXISTS"
STATE_RECORD_MISMATCH = "STATE_RECORD_MISMATCH"
COMPANIONS_MISSING = "COMPANIONS_MISSING"
PARTIALLY_CONVERTED = "PARTIALLY_CONVERTED"
DRAFT_LIST_FAILED = "DRAFT_LIST_FAILED"

_RECOMMENDED_ACTION = {
    CONVERTIBLE: (
        "re-run with --execute to convert this draft to file-by-file"
    ),
    RESUMABLE: (
        "already in file-by-file mode — re-run with --execute to continue "
        "where the previous run stopped"
    ),
    ZIP_ALREADY_COMMITTED: (
        "nothing to do — the ZIP upload succeeded; publish this draft when "
        "you are ready"
    ),
    TITLE_UNPARSEABLE: (
        "the title matches the ESID pattern but the captured id is not a "
        "valid ESID — fix the record title on Zenodo"
    ),
    DRAFT_STATE_UNKNOWN: (
        "Zenodo served neither 'is_published' nor 'status' for this record; "
        "re-run, and report it if it persists (refusing to guess)"
    ),
    NO_RECORD_ID: (
        "the listing hit carried no record id — re-run the scan; report it "
        "if it persists"
    ),
    DUPLICATE_DRAFTS_FOR_ESID: (
        "two or more drafts share this ESID — resolve by hand "
        "(Resources/find_duplicate_records.py), then re-run"
    ),
    NO_STAGING_FOLDER: (
        "no local package for this draft — re-prep the ESID "
        "(Resources/prepare_dataset.py) or discard the draft"
    ),
    PREP_INCOMPLETE: (
        "the .prep_complete sentinel is missing — re-prep this ESID before "
        "uploading anything from it"
    ),
    MANIFESTS_MISSING: (
        "file_list.csv and/or the upload manifest is absent or empty, so "
        "the required file set cannot be derived — re-prep this ESID"
    ),
    NO_RAW_FOLDER: (
        "no Raw_Data folder matches this ESID — check RAW_DATA_DIR, or "
        "mount the volume holding the audio"
    ),
    RAW_FILES_MISSING: (
        "file_list.csv names WAVs that are not in the raw folder — the "
        "audio has moved or the folder was split; reconcile before "
        "converting"
    ),
    TOO_MANY_FILES: (
        "the file-by-file set exceeds --max-files (Zenodo caps a record at "
        "100 files) — read the Notes column, which says whether a 2-part "
        "split can bring this site within the limit or whether the ZIP is "
        "its only vehicle"
    ),
    AMBIGUOUS_LOCAL_FOLDER: (
        "two or more local folders resolve to this ESID (parse_esid strips "
        "_Staging/_Uploaded, so Raw_Data/ESID_NNN_Staging leftovers collide "
        "with Raw_Data/ESID#NNN) — resolve them, e.g. with "
        "Resources/clean_raw_staging_leftovers.py, then re-run"
    ),
    UPLOADED_TWIN_EXISTS: (
        "an Uploaded_Data/ESID_NNN_Uploaded folder already exists — "
        "reconcile the two local copies by hand first"
    ),
    STATE_RECORD_MISMATCH: (
        "upload_state.json names a DIFFERENT record than this draft — one "
        "of the two would be orphaned, so neither is touched. Resolve by "
        "hand"
    ),
    COMPANIONS_MISSING: (
        "a companion file is also missing from the record, so this is not a "
        "ZIP-size problem — finish it with Resources/finish_stuck_uploads.py"
    ),
    PARTIALLY_CONVERTED: (
        "individual WAVs are on the record but the staging folder is NOT "
        "marked file-by-file — something else converted it. Inspect before "
        "letting this tool near it"
    ),
    DRAFT_LIST_FAILED: (
        "the draft's file list could not be read — re-run; if it persists, "
        "inspect the draft in the Zenodo UI"
    ),
}

# A row with one of these verdicts is not a problem and not work.
_BENIGN_VERDICTS = frozenset({ZIP_ALREADY_COMMITTED})

# Verdicts this tool acts on under --execute.
_ACTIONABLE_VERDICTS = frozenset({CONVERTIBLE, RESUMABLE})

_ACTION_CONVERTED = "converted"
_ACTION_FAILED = "conversion FAILED"
_ACTION_NONE_DRY_RUN = "none (read-only run)"
_ACTION_NONE = "none"
_ACTION_SKIPPED_LIMIT = "skipped (--limit reached)"
_ACTION_SKIPPED_STOP = "skipped (stop requested)"
_ACTION_SKIPPED_BREAKER = "skipped (--max-consecutive-failures reached)"

# --- Detail-row sources and statuses ------------------------------------
_SOURCE_RAW = "raw"
_SOURCE_COMPANION = "companion"
_SOURCE_ZIP = "zip"
_SOURCE_UNEXPECTED = "unexpected"

_STATUS_TO_UPLOAD = "to_upload"
_STATUS_ALREADY_COMMITTED = "already_committed"
_STATUS_TO_DELETE = "to_delete"
_STATUS_MISSING_LOCALLY = "missing_locally"
_STATUS_ON_RECORD_ONLY = "on_record_only"


# =====================================================================
#  Graceful stop
# =====================================================================

_stop_requested = False


def stop_requested() -> bool:
    """Report whether a graceful stop has been asked for.

    Returns:
        True once SIGINT or SIGTERM has been received.
    """
    return _stop_requested


def install_stop_handlers() -> None:
    """Make the first SIGINT/SIGTERM graceful and the second immediate.

    A weeks-long batch must be stoppable without abandoning an ESID
    mid-conversion, so the first signal only sets a flag, which is checked
    BETWEEN ESIDs.  The handler then restores the default disposition, so a
    second Ctrl+C aborts at once — otherwise an operator watching a genuinely
    hung upload would have no way out.
    """
    def handle(signum, _frame):
        """Set the stop flag and restore the default signal disposition.

        Args:
            signum: The signal received.
            _frame: Unused stack frame.
        """
        global _stop_requested
        _stop_requested = True
        signal.signal(signum, signal.SIG_DFL)
        logger.warning(
            "Signal %d received — finishing the current ESID, then stopping. "
            "Both reports will be flushed. Press Ctrl+C again to abort now.",
            signum,
        )

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handle)


# =====================================================================
#  Pure helpers (no I/O)
# =====================================================================

@dataclass
class Candidate:
    """One in-scope Zenodo draft, as seen in the account listing.

    Attributes:
        esid: Canonical ESID string derived from the record title.
        record_id: Zenodo record/draft id.
        title: The record title, verbatim.
        url: Web URL for the draft (may be ``""``).
        verdict: Set when the listing itself already disqualified the hit
            (an unparseable title, an indeterminate draft state, a missing
            record id); otherwise ``""``.
        note: Explanation accompanying an early verdict.
    """

    esid: str
    record_id: str
    title: str
    url: str = ""
    verdict: str = ""
    note: str = ""


@dataclass
class LocalPackage:
    """The local side of one candidate, derived once and reused.

    Attributes:
        staging_dir: The ``ESID_NNN_Staging`` folder.
        raw_dir: The ``Raw_Data/ESID#NNN`` folder, or None if absent.
        raw_files: ``(name, sha512)`` for every WAV + CONFIG.TXT.
        companion_names: Standalone companion file names.
        state_record_id: ``record_id`` currently in upload_state.json, or
            ``""`` when there is no state file or no such key.
        already_file_by_file: True when upload_state.json marks the folder
            ``mode: file_by_file``.
        staging_options: EVERY staging folder that resolves to this ESID.
        raw_options: EVERY raw folder that resolves to this ESID.  More than
            one means the ESID is ambiguous and nothing may be derived from
            either — see :func:`local_verdict`.
    """

    staging_dir: Path
    raw_dir: Optional[Path] = None
    raw_files: List[Tuple[str, str]] = field(default_factory=list)
    companion_names: List[str] = field(default_factory=list)
    state_record_id: str = ""
    already_file_by_file: bool = False
    staging_options: List[Path] = field(default_factory=list)
    raw_options: List[Path] = field(default_factory=list)


def esid_from_hit(
    title_res: Sequence["object"],
    filter_res: Sequence["object"],
    hit: Dict,
) -> Tuple[Optional[str], str, str]:
    """Derive a trustworthy ESID from a listing hit, refusing to guess.

    ``esid_record_report.match_title`` deliberately falls back to the bare
    first three characters of its capture when the ESID grammar rejects it
    — lenient is right for a report, but this tool authorises a one-way
    door, so the value is put through
    :func:`azus_common.normalize_esid` again and a rejection becomes
    ``TITLE_UNPARSEABLE`` rather than a guess.

    With the default title patterns this re-check cannot currently fire:
    the pattern captures ``\\d{3}(?!\\d)[A-Za-z0-9_ ]*``, which
    ``normalize_esid`` accepts in full, so ``match_title`` never reaches its
    fallback.  It is kept because it is four lines and it makes the link
    between "a title matched" and "we will delete files from this record"
    hold structurally — by construction rather than by the two grammars
    happening to agree.

    Args:
        title_res: OR regexes from ``compile_title_pattern``.
        filter_res: AND regexes from ``compile_filter_pattern``.
        hit: One raw hit dict from the account listing.

    Returns:
        ``(esid, verdict, note)``.  ``esid`` is None when the title is out
        of scope entirely (not an ESID record — silently ignored) or when
        the capture is unusable, in which case ``verdict`` is
        ``TITLE_UNPARSEABLE`` and ``note`` explains.  A usable hit returns
        ``(esid, "", "")``.
    """
    title = err_mod._title_from_hit(hit)
    captured = err_mod.title_in_scope(title_res, filter_res, title)
    if captured is None:
        return None, "", ""
    try:
        return azus_common.normalize_esid(captured), "", ""
    except ValueError as exc:
        return None, TITLE_UNPARSEABLE, (
            f"title {title!r} matched the ESID pattern but {captured!r} is "
            f"not a valid ESID ({exc})"
        )


def candidates_from_hits(
    hits: Sequence[Dict],
    title_res: Sequence["object"],
    filter_res: Sequence["object"],
    web_base: str,
) -> Tuple[List[Candidate], int, int]:
    """Turn listing hits into draft candidates, ignoring published records.

    Args:
        hits: Every hit from the account listing.
        title_res: OR regexes from ``compile_title_pattern``.
        filter_res: AND regexes from ``compile_filter_pattern``.
        web_base: Site root for building draft URLs, e.g.
            ``"https://zenodo.org/"``.

    Returns:
        ``(candidates, published_count, ignored_count)``.  ``candidates``
        holds every in-scope DRAFT (including ones already carrying an
        early verdict); ``published_count`` counts in-scope published
        records, which are out of scope for this tool; ``ignored_count``
        counts hits whose titles are not ESID records at all.
    """
    candidates: List[Candidate] = []
    published = 0
    ignored = 0
    for hit in hits:
        title = err_mod._title_from_hit(hit)
        esid, verdict, note = esid_from_hit(title_res, filter_res, hit)
        if esid is None and not verdict:
            ignored += 1
            continue
        record_id = str(hit.get("id") or "")
        if esid is None:
            # Title matched but the id is unusable — report it against the
            # record rather than dropping it silently.
            candidates.append(Candidate(
                esid="", record_id=record_id, title=title,
                verdict=verdict, note=note,
            ))
            continue

        is_draft, draft_error = err_mod._draft_flag_from_hit(hit, "account")
        if is_draft is None:
            candidates.append(Candidate(
                esid=esid, record_id=record_id, title=title,
                verdict=DRAFT_STATE_UNKNOWN, note=draft_error,
            ))
            continue
        if not is_draft:
            published += 1
            continue
        if not record_id:
            candidates.append(Candidate(
                esid=esid, record_id="", title=title, verdict=NO_RECORD_ID,
                note="the listing hit carried no 'id' field",
            ))
            continue
        candidates.append(Candidate(
            esid=esid, record_id=record_id, title=title,
            url=f"{web_base}uploads/{record_id}",
        ))
    return candidates, published, ignored


def flag_duplicate_esids(candidates: List[Candidate]) -> int:
    """Mark every member of an ESID that has more than one draft.

    Two drafts for one ESID cannot be resolved automatically: converting
    either one orphans the other, and there is no evidence here for which
    is the keeper.  Deliberately NOT reusing
    ``prep_all_datasets.filter_and_order_discovered``, whose ``by_esid``
    dict is last-wins and would silently collapse the pair.

    Costs no network calls — the duplication is visible in the listing.

    Args:
        candidates: Candidates to inspect; modified in place.

    Returns:
        How many candidates were flagged.
    """
    counts: Dict[str, int] = {}
    for cand in candidates:
        if cand.esid and not cand.verdict:
            counts[cand.esid] = counts.get(cand.esid, 0) + 1
    flagged = 0
    for cand in candidates:
        if cand.verdict or counts.get(cand.esid, 0) < 2:
            continue
        others = sorted(
            c.record_id for c in candidates
            if c.esid == cand.esid and c.record_id != cand.record_id
        )
        cand.verdict = DUPLICATE_DRAFTS_FOR_ESID
        cand.note = (
            f"{counts[cand.esid]} drafts share ESID {cand.esid} "
            f"(also: {', '.join(others)})"
        )
        flagged += 1
    return flagged


def classify_from_entries(
    entries: List[Dict[str, object]],
    raw_files: List[Tuple[str, str]],
    companion_names: List[str],
    zip_name: str,
    *,
    already_file_by_file: bool,
) -> Tuple[str, str]:
    """Decide one draft's verdict from its file entries alone.

    Pure, so every branch is testable without a network.  The order is
    load-bearing:

    1. **A committed ZIP means the ZIP upload SUCCEEDED.**  Nothing to
       repair, and the good archive must never be deleted.
    2. **An empty required set is a refusal, not a pass.**  This is the
       fail-open hole in ``only_zip_missing_from_entries``: with no
       companions the "every companion is committed" test is vacuously
       true, so a record missing EVERYTHING reads as "only the ZIP is
       missing" — and that answer authorises a one-way door.  Refused here
       so the hole cannot be reached, whatever the caller does.
    3. **Already ``file_by_file`` means resume**, before the companion
       test.  The companion test exists to decide whether *switching* is
       the right remedy; once switched, a not-yet-committed companion is
       simply work remaining.  Treating it as a skip would strand every
       ESID a restart interrupted.
    4. A missing companion on a not-yet-switched record means the failure
       is not ZIP size, so file-by-file is the wrong fix.
    5. Individual WAVs on a record that is NOT marked file-by-file means
       something else converted it.  Refuse rather than join in.

    A *pending* (never-committed) ZIP slot does not disqualify anything —
    that is the normal residue of a timed-out ZIP, and clearing it is part
    of the repair.

    Args:
        entries: Raw entries from ``GET /records/{id}/draft/files``.
        raw_files: ``(name, sha512)`` for every WAV + CONFIG.TXT.
        companion_names: Standalone companions the record must hold.
        zip_name: The ESID's ZIP filename.
        already_file_by_file: Whether the staging folder is already marked
            ``mode: file_by_file``.

    Returns:
        ``(verdict, note)``.
    """
    committed = fbf.committed_keys(entries)
    if zip_name in committed:
        return ZIP_ALREADY_COMMITTED, "the ZIP is committed on the record"
    if not raw_files or not companion_names:
        return MANIFESTS_MISSING, (
            f"derived {len(raw_files)} raw file(s) and "
            f"{len(companion_names)} companion(s) — both must be non-empty "
            "before any decision about this record can be trusted"
        )
    if already_file_by_file:
        done = sum(1 for name, _ in raw_files if name in committed)
        return RESUMABLE, (
            f"already file-by-file; {done}/{len(raw_files)} raw file(s) "
            "committed"
        )
    absent = [n for n in companion_names if n not in committed]
    if absent:
        return COMPANIONS_MISSING, (
            f"{len(absent)} companion(s) not committed: "
            f"{', '.join(absent[:5])}"
        )
    stray = sorted(k for k in committed if fbf.is_raw_upload_name(str(k)))
    if stray:
        return PARTIALLY_CONVERTED, (
            f"{len(stray)} individual raw file(s) are on the record but the "
            f"staging folder is not marked file-by-file: "
            f"{', '.join(stray[:5])}"
        )
    return CONVERTIBLE, "every companion is committed; the ZIP is not"


def records_needed(raw_count: int, companion_count: int, max_files: int) -> int:
    """How many records a file-by-file upload of this site would take.

    Every record repeats the full companion set, so the usable slots per
    record are ``max_files - companion_count``, not ``max_files``.

    Args:
        raw_count: Number of WAVs + CONFIG.TXT.
        companion_count: Number of standalone companions.
        max_files: Per-record file ceiling.

    Returns:
        The number of records required, or ``0`` when the companions alone
        already fill a record (so no split can ever help).
    """
    per_record = max_files - companion_count
    if per_record <= 0:
        return 0
    return -(-raw_count // per_record)  # ceil, integer-only


def split_advice(
    raw_count: int, companion_count: int, max_files: int
) -> str:
    """Say what can actually be done about an oversized site.

    ``split_oversized_raw_folders.py`` handles ``_Part_N_of_2`` **pairs
    only** — it has its own verdict for "a half still exceeds the limit".
    So recommending a split is honest only when two records would suffice.
    The production scan showed why this matters: 58 of 100 drafts were
    oversized, most needing dozens of records, and every one of them was
    being told to split.

    Args:
        raw_count: Number of WAVs + CONFIG.TXT.
        companion_count: Number of standalone companions.
        max_files: Per-record file ceiling.

    Returns:
        A sentence naming the only viable route for this site.
    """
    needed = records_needed(raw_count, companion_count, max_files)
    if needed == 0:
        return (
            f"the {companion_count} companion(s) alone fill a record at "
            f"--max-files {max_files}, so no split can help; the ZIP is the "
            "only vehicle for this site"
        )
    if needed <= 2:
        return (
            "splitting the site into a _Part_1_of_2 / _Part_2_of_2 pair "
            "(Resources/split_oversized_raw_folders.py) would bring each "
            "half within the limit"
        )
    return (
        f"file-by-file would need ~{needed} records for this ONE ESID, and "
        "split_oversized_raw_folders.py only produces 2-part pairs — so "
        "file-by-file cannot serve this site. The ZIP is the only vehicle; "
        "its problems belong to the ZIP upload path"
    )


def local_verdict(
    package: LocalPackage,
    record_id: str,
    esid: str,
    max_files: int,
) -> Tuple[str, str]:
    """Apply every local gate, before any per-draft network call.

    Ordered cheapest-first so an oversized or unprepped ESID costs a few
    ``stat`` calls rather than a listing.

    Args:
        package: The derived local package.
        record_id: The draft id this ESID was matched to.
        esid: Canonical ESID string.
        max_files: Ceiling for ``len(raw) + len(companions)``.

    Returns:
        ``(verdict, note)``, or ``("", "")`` when every local gate passes
        and the draft's file list should be fetched.
    """
    # Ambiguity first: everything below is derived FROM these folders, so a
    # count or a hash taken from the wrong one is worse than no answer.
    for label, options in (
        ("staging", package.staging_options), ("raw", package.raw_options),
    ):
        if len(options) > 1:
            return AMBIGUOUS_LOCAL_FOLDER, (
                f"{len(options)} {label} folders resolve to ESID {esid}: "
                f"{', '.join(p.name for p in options)}"
            )

    if not package.staging_dir.is_dir():
        return NO_STAGING_FOLDER, f"no folder at {package.staging_dir}"
    if not (package.staging_dir / azus_common.PREP_SENTINEL).is_file():
        return PREP_INCOMPLETE, (
            f"{azus_common.PREP_SENTINEL} is absent from "
            f"{package.staging_dir.name}"
        )
    if not package.raw_files or not package.companion_names:
        return MANIFESTS_MISSING, (
            f"file_list.csv yielded {len(package.raw_files)} raw row(s) and "
            f"the upload manifest yielded {len(package.companion_names)} "
            "companion(s); both must be non-empty"
        )

    total = len(package.raw_files) + len(package.companion_names)
    if total > max_files:
        return TOO_MANY_FILES, (
            f"{total} file(s) ({len(package.raw_files)} raw + "
            f"{len(package.companion_names)} companion) exceeds "
            f"--max-files {max_files} — "
            + split_advice(
                len(package.raw_files), len(package.companion_names), max_files
            )
        )

    if package.raw_dir is None:
        return NO_RAW_FOLDER, f"no raw folder matches ESID {esid}"
    absent = [
        name for name, _ in package.raw_files
        if not (package.raw_dir / name).is_file()
    ]
    if absent:
        return RAW_FILES_MISSING, (
            f"{len(absent)} file(s) named in file_list.csv are not in "
            f"{package.raw_dir.name}: {', '.join(absent[:5])}"
        )

    twin = azus_common.UPLOADED_DATA / f"ESID_{esid}_Uploaded"
    if twin.exists():
        return UPLOADED_TWIN_EXISTS, f"{twin} already exists"

    if package.state_record_id and package.state_record_id != record_id:
        return STATE_RECORD_MISMATCH, (
            f"upload_state.json names record {package.state_record_id} but "
            f"this draft is {record_id}"
        )
    return "", ""


def row_needs_attention(row: Dict[str, str]) -> bool:
    """Report whether a summary row should make the run exit non-zero.

    A benign verdict never does.  Anything else does unless this run
    actually converted it — including ``CONVERTIBLE`` in a read-only run,
    which is real work still outstanding.

    Args:
        row: A summary report row.

    Returns:
        True when the row needs a human to look at it.
    """
    if row.get("Verdict") in _BENIGN_VERDICTS:
        return False
    return row.get("Action Taken") != _ACTION_CONVERTED


def bounded_upload_attempts(requested: int) -> int:
    """Clamp ``--upload-attempts`` to what the backoff tuple supports.

    Args:
        requested: The requested number of PUT attempts per file.

    Returns:
        ``requested`` clamped to ``[1, _MAX_UPLOAD_ATTEMPTS]``.
    """
    return max(1, min(requested, _MAX_UPLOAD_ATTEMPTS))


# =====================================================================
#  Local derivation
# =====================================================================

def read_state_record_id(staging_dir: Path) -> str:
    """Read ``record_id`` out of a staging folder's upload_state.json.

    Args:
        staging_dir: The staging folder to read.

    Returns:
        The recorded record id, or ``""`` when there is no state file, no
        such key, or the file cannot be parsed.
    """
    state_file = staging_dir / _STATE_FILENAME
    try:
        loaded = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(loaded, dict):
        return ""
    return str(loaded.get("record_id") or "")


def index_esid_folders(root: Path) -> Dict[str, List[Path]]:
    """Map each canonical ESID to EVERY folder under ``root`` claiming it.

    One-to-MANY deliberately.  ``azus_common.parse_esid`` strips the
    ``_staging`` / ``_uploaded`` tails, so ``Raw_Data/ESID_055_Staging`` — a
    leftover from an interrupted prep — resolves to ESID 055 exactly as
    ``Raw_Data/ESID#055`` does.  A ``{esid: folder}`` comprehension over
    ``find_esid_folders`` is last-wins, so one of the two would silently
    win and this tool would hash and upload from whichever came last in
    directory order.  Returning both lets the caller REFUSE instead.

    Args:
        root: Folder containing ESID subdirectories.

    Returns:
        ``{canonical_esid: [folder, ...]}`` in :func:`find_esid_folders`
        order.
    """
    index: Dict[str, List[Path]] = {}
    for _sort, esid, folder in azus_common.find_esid_folders(root):
        index.setdefault(esid, []).append(folder)
    return index


def derive_local_package(
    esid: str,
    staging_index: Dict[str, List[Path]],
    raw_index: Dict[str, List[Path]],
) -> LocalPackage:
    """Gather everything local about one ESID, with no network calls.

    Args:
        esid: Canonical ESID string.
        staging_index: From :func:`index_esid_folders` over ``Staging_Area``.
        raw_index: From :func:`index_esid_folders` over the raw-data folder.

    Returns:
        The :class:`LocalPackage`.  When the staging folder is absent the
        manifest-derived fields are simply empty; the caller's gates
        report that.  When either side is ambiguous the first folder is
        used for reporting only — :func:`local_verdict` refuses before
        anything derived from it is trusted.
    """
    staging_options = staging_index.get(esid, [])
    raw_options = raw_index.get(esid, [])
    staging_dir = (
        staging_options[0] if staging_options
        else _STAGING_AREA / f"ESID_{esid}_Staging"
    )
    package = LocalPackage(
        staging_dir=staging_dir,
        raw_dir=raw_options[0] if raw_options else None,
        staging_options=staging_options,
        raw_options=raw_options,
    )
    if not staging_dir.is_dir():
        return package
    package.raw_files, package.companion_names = fbf.required_files(
        staging_dir, esid
    )
    package.state_record_id = read_state_record_id(staging_dir)
    package.already_file_by_file = (
        azus_common.read_upload_mode(staging_dir) == azus_common.FILE_BY_FILE_MODE
    )
    return package


def write_recovered_state(
    staging_dir: Path, record_id: str, *, title: str, tag: str
) -> bool:
    """Write the Zenodo-recovered ``record_id`` into upload_state.json.

    This is the point of scanning Zenodo: a staging folder whose state file
    was lost has no local pointer to its draft, and every recovery tool
    keys off that pointer.  Writing it back re-arms the whole pipeline —
    including ``standalone_tasks.py``, which will adopt this draft on the
    ZIP path.  That is desirable, and stated here rather than left as a
    surprise.

    Refuses to touch a state file that names a DIFFERENT record: one of the
    two drafts would be orphaned and there is no evidence for which.  A
    state file already naming this record is left exactly as it is —
    ``number_of_tries`` and any other keys are preserved, so a restart does
    not reset the history.

    ``number_of_tries`` starts at 0 on a fresh write: recovering a pointer
    is not an upload attempt, and starting at 1 would push ESIDs toward
    ``finish_stuck_uploads.py``'s ``--tries-threshold`` for work nobody did.

    Args:
        staging_dir: The staging folder to write into.
        record_id: The draft id recovered from the Zenodo listing.
        title: The record title, stored as provenance.
        tag: Log prefix, e.g. ``"[ESID 073]"``.

    Returns:
        True when a fresh state file was written; False when one already
        existed (whether or not it matched) or the write failed.
    """
    state_file = staging_dir / _STATE_FILENAME
    existing = read_state_record_id(staging_dir)
    if state_file.exists():
        if existing != record_id:
            logger.error(
                "%s REFUSING to overwrite %s — it names record %s, not %s.",
                tag, state_file.name, existing or "(none)", record_id,
            )
        return False
    state = {
        "record_id": record_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "zenodo_url": f"https://zenodo.org/uploads/{record_id}",
        "resumed": False,
        "restored_from": "Zenodo account listing (finish_zip_only_drafts.py)",
        "restored_title": title,
        "number_of_tries": 0,
    }
    try:
        tmp = state_file.with_name(f".{state_file.name}.tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, state_file)
    except OSError as exc:
        logger.error("%s Could not write %s: %s", tag, state_file, exc)
        return False
    logger.info(
        "%s Recovered the draft pointer: wrote %s -> record %s.",
        tag, state_file.name, record_id,
    )
    return True


# =====================================================================
#  Reporting
# =====================================================================

def build_detail_rows(
    esid: str,
    record_id: str,
    package: LocalPackage,
    entries: List[Dict[str, object]],
    zip_name: str,
    *,
    with_hashes: bool,
) -> List[Dict[str, str]]:
    """Build one detail row per file, local and remote reconciled.

    **Never reads a file's bytes.**  Sizes come from ``stat``; the SHA-512
    comes from ``file_list.csv`` (prep's record) and the md5 from the raw
    folder's ``wav_hashes.csv`` — both already on disk.  Hashing every WAV
    of every candidate would turn a read-only scan into a multi-day pass
    over terabytes, so the hash columns are cache-only and are filled only
    when asked for.

    Args:
        esid: Canonical ESID string.
        record_id: The draft id.
        package: The derived local package.
        entries: Raw entries from the draft's file listing.
        zip_name: The ESID's ZIP filename.
        with_hashes: Fill the SHA-512 and MD5 columns from the manifests
            and the hash cache.

    Returns:
        Detail rows in upload order: companions, then raw files, then any
        ZIP slot, then anything on the record that is in neither list.
    """
    by_key = {str(e.get("key")): e for e in entries}
    committed = fbf.committed_keys(entries)
    cached_md5: Dict[str, str] = {}
    if with_hashes and package.raw_dir is not None:
        cached_md5 = {
            name: row[3]
            for name, row in hash_raw_wavs.load_cache(package.raw_dir).items()
            if row[3]
        }

    def _row(name: str, source: str, base_dir: Optional[Path],
             sha512: str = "") -> Dict[str, str]:
        """Assemble one detail row.

        Args:
            name: The file name.
            source: One of the ``_SOURCE_*`` constants.
            base_dir: Folder the file should live in, or None.
            sha512: Prep's recorded SHA-512, if known.

        Returns:
            The detail row.
        """
        local = base_dir / name if base_dir is not None else None
        size = ""
        present = False
        if local is not None:
            try:
                size = str(local.stat().st_size)
                present = True
            except OSError:
                present = False
        entry = by_key.get(name)
        if name in committed:
            status = _STATUS_ALREADY_COMMITTED
        elif not present and base_dir is not None:
            status = _STATUS_MISSING_LOCALLY
        else:
            status = _STATUS_TO_UPLOAD
        return {
            "ESID#": esid,
            "Record ID": record_id,
            "File Name": name,
            "Source": source,
            "Size (Bytes)": size,
            "SHA-512": sha512 if with_hashes else "",
            "MD5": cached_md5.get(name, "") if with_hashes else "",
            "Committed On Record": "y" if name in committed else "n",
            "Remote Size": str(entry.get("size", "")) if entry else "",
            "Remote Checksum": str(entry.get("checksum", "")) if entry else "",
            "Status": status,
            "Notes": "",
        }

    rows = [
        _row(name, _SOURCE_COMPANION, package.staging_dir)
        for name in package.companion_names
    ]
    rows += [
        _row(name, _SOURCE_RAW, package.raw_dir, sha512)
        for name, sha512 in package.raw_files
    ]

    zip_entry = by_key.get(zip_name)
    if zip_entry is not None:
        row = _row(zip_name, _SOURCE_ZIP, None)
        row["Status"] = (
            _STATUS_ALREADY_COMMITTED if zip_name in committed
            else _STATUS_TO_DELETE
        )
        row["Notes"] = (
            "committed — this record is NOT a file-by-file candidate"
            if zip_name in committed
            else f"pending slot (status={zip_entry.get('status')}) to be "
                 "cleared by the conversion"
        )
        rows.append(row)

    known = (
        {n for n in package.companion_names}
        | {n for n, _ in package.raw_files}
        | {zip_name}
    )
    for key in sorted(k for k in by_key if k not in known):
        row = _row(key, _SOURCE_UNEXPECTED, None)
        row["Status"] = _STATUS_ON_RECORD_ONLY
        row["Notes"] = (
            "on the record but in neither file_list.csv nor the upload "
            "manifest"
        )
        rows.append(row)
    return rows


def summary_row(
    cand: Candidate,
    package: Optional[LocalPackage],
    entries: Optional[List[Dict[str, object]]],
    verdict: str,
    note: str,
) -> Dict[str, str]:
    """Assemble one summary row.

    Args:
        cand: The candidate draft.
        package: Its local package, or None when nothing local was derived.
        entries: The draft's file entries, or None when not fetched.
        verdict: The verdict for this draft.
        note: Supporting detail for the verdict.

    Returns:
        The summary row, with ``Action Taken`` left for the caller to set.
    """
    committed = fbf.committed_keys(entries) if entries is not None else set()
    zip_name = f"ESID_{cand.esid}.zip" if cand.esid else ""
    companions = package.companion_names if package else []
    return {
        "ESID#": cand.esid,
        "Record ID": cand.record_id,
        "Title": cand.title,
        "Zenodo URL": cand.url,
        "Staging Folder": str(package.staging_dir) if package else "",
        "Raw Folder": str(package.raw_dir) if package and package.raw_dir else "",
        "WAV Count": str(len(package.raw_files)) if package else "",
        "Companion Count": str(len(companions)) if package else "",
        "Total Files": (
            str(len(package.raw_files) + len(companions)) if package else ""
        ),
        "Entries On Record": str(len(entries)) if entries is not None else "",
        "ZIP On Record": (
            "" if entries is None
            else "committed" if zip_name in committed
            else "pending" if any(str(e.get("key")) == zip_name for e in entries)
            else "no"
        ),
        "Companions Committed": (
            "" if entries is None
            else f"{sum(1 for n in companions if n in committed)}/"
                 f"{len(companions)}"
        ),
        "State File Record ID": package.state_record_id if package else "",
        "Record ID Written": "n",
        "Verdict": verdict,
        "Action Taken": _ACTION_NONE,
        "Notes": note,
    }


def default_output_paths() -> Tuple[Path, Path]:
    """Build this run's two report paths under ``Records/``.

    Timestamp first so repeated runs sort chronologically, matching
    ``esid_record_report.py`` and ``finish_stuck_uploads.py``.

    Returns:
        ``(summary_path, detail_path)``.
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    records = _PROJECT_ROOT / "Records"
    return (
        records / f"{stamp}_finish_zip_only_drafts.csv",
        records / f"{stamp}_finish_zip_only_drafts_files.csv",
    )


def default_log_path() -> Path:
    """Build this run's log path under ``Records/``.

    Returns:
        The path for this run's log (its parent may not exist yet).
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return _PROJECT_ROOT / "Records" / f"{stamp}_finish_zip_only_drafts.log"


def configure_logging(
    verbose: bool = False, log_path: Optional[Path] = None
) -> Optional[Path]:
    """Send output to both the screen and a log file.

    A run can last weeks unattended, so the log is the only record of what
    happened.  A logging problem must never fail the run: a file that
    cannot be opened is reported on screen and the run continues.

    Args:
        verbose: Log at DEBUG instead of INFO.
        log_path: Where to write; defaults to :func:`default_log_path`.

    Returns:
        The log file actually opened, or None when only the screen is used.
    """
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    target = log_path or default_log_path()
    opened: Optional[Path] = None
    problem: Optional[str] = None
    file_handler: Optional[logging.Handler] = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(target, encoding="utf-8")
        handlers.append(file_handler)
        opened = target
    except OSError as exc:
        problem = f"{target}: {exc}"
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=handlers,
    )
    # basicConfig is a NO-OP once the root logger has handlers, which is the
    # case when this runs a second time in one process (the test suite).  The
    # FileHandler is constructed either way and would be left holding an open
    # descriptor, so close it when it was not adopted.
    if file_handler is not None and file_handler not in logging.root.handlers:
        file_handler.close()
        opened = None
    if problem is not None:
        logger.warning(
            "Could not open a log file (%s) — this run's output will only "
            "appear on screen.", problem,
        )
    return opened


# =====================================================================
#  Discovery
# =====================================================================

def discover_drafts(
    credentials: Credentials,
    title_res: Sequence["object"],
    filter_res: Sequence["object"],
) -> Tuple[List[Candidate], int, int]:
    """List the account's records and return the in-scope drafts.

    Drafts are visible only on ``/api/user/records``, which needs the API
    token.  Completeness comes from
    ``esid_record_report.fetch_all_hits_verified``: it stops only on a
    short page and cross-checks the API's own total, raising rather than
    returning a truncated set.

    Args:
        credentials: Zenodo API credentials.
        title_res: OR regexes from ``compile_title_pattern``.
        filter_res: AND regexes from ``compile_filter_pattern``.

    Returns:
        ``(candidates, published_count, ignored_count)``.

    Raises:
        esid_record_report.ReportError: When the listing cannot be proven
            complete.
    """
    base_url = credentials.base_url
    web_base = (
        base_url[:-4] if base_url.endswith("api/") else "https://zenodo.org/"
    )
    hits = err_mod.fetch_all_hits_verified(
        f"{base_url}user/records?size={_PAGE_SIZE}",
        _auth_headers(credentials), "account records", _PAGE_SIZE,
    )
    candidates, published, ignored = candidates_from_hits(
        hits, title_res, filter_res, web_base
    )
    flagged = flag_duplicate_esids(candidates)
    if flagged:
        logger.error(
            "%d draft(s) share an ESID with another draft — every one is "
            "reported as %s and NONE will be touched.",
            flagged, DUPLICATE_DRAFTS_FOR_ESID,
        )
    candidates.sort(
        key=lambda c: (azus_common.esid_sort_key(c.esid) if c.esid else (999, ""),
                       c.record_id)
    )
    return candidates, published, ignored


def classify_candidate(
    cand: Candidate,
    credentials: Credentials,
    staging_index: Dict[str, List[Path]],
    raw_index: Dict[str, List[Path]],
    max_files: int,
    *,
    with_hashes: bool,
) -> Tuple[Dict[str, str], List[Dict[str, str]], Optional[LocalPackage]]:
    """Classify one draft: local gates first, then one file listing.

    Args:
        cand: The candidate draft.
        credentials: Zenodo API credentials.
        staging_index: From :func:`index_esid_folders` over ``Staging_Area``.
        raw_index: From :func:`index_esid_folders` over the raw-data folder.
        max_files: Ceiling for ``len(raw) + len(companions)``.
        with_hashes: Fill the detail report's hash columns from cache.

    Returns:
        ``(summary_row, detail_rows, package)``.  ``package`` is None when
        the candidate was disqualified by the listing alone.
    """
    if cand.verdict:
        return summary_row(cand, None, None, cand.verdict, cand.note), [], None

    package = derive_local_package(cand.esid, staging_index, raw_index)
    verdict, note = local_verdict(
        package, cand.record_id, cand.esid, max_files
    )
    if verdict:
        return summary_row(cand, package, None, verdict, note), [], package

    try:
        entries = list_draft_files(credentials, cand.record_id)
    except Exception as exc:  # noqa: BLE001 — one bad draft must not stop the scan
        return summary_row(
            cand, package, None, DRAFT_LIST_FAILED,
            f"{type(exc).__name__}: {exc}",
        ), [], package

    verdict, note = classify_from_entries(
        entries, package.raw_files, package.companion_names,
        f"ESID_{cand.esid}.zip",
        already_file_by_file=package.already_file_by_file,
    )
    rows = build_detail_rows(
        cand.esid, cand.record_id, package, entries,
        f"ESID_{cand.esid}.zip", with_hashes=with_hashes,
    )
    return summary_row(cand, package, entries, verdict, note), rows, package


def convert_candidate(
    cand: Candidate,
    package: LocalPackage,
    credentials: Credentials,
    row: Dict[str, str],
    args: argparse.Namespace,
    publish_config: Tuple[Optional[str], bool],
) -> bool:
    """Convert one draft to file-by-file, re-deriving every gate.

    Nothing from the scan is trusted: :func:`file_by_file_upload.run_file_by_file`
    independently re-reads the manifests, re-verifies every raw hash
    against ``file_list.csv``, re-lists the draft's files, re-checks the
    100-file ceiling, and refuses if a committed ZIP has appeared in the
    meantime.

    Args:
        cand: The candidate draft.
        package: Its derived local package.
        credentials: Zenodo API credentials.
        row: This ESID's summary row, updated in place.
        args: Parsed CLI arguments.
        publish_config: ``(community_id, reserve_doi)`` from the config.

    Returns:
        True when the record is complete on Zenodo afterwards.
    """
    tag = f"[ESID {cand.esid}]"
    community_id, reserve_doi = publish_config

    # Recover the draft pointer BEFORE uploading, so an interruption leaves
    # a folder the other recovery tools can still find.  Only for a fresh
    # conversion — a RESUMABLE folder already has its state file, and
    # rewriting it would drop number_of_tries.
    if row["Verdict"] == CONVERTIBLE and not package.state_record_id:
        if write_recovered_state(
            package.staging_dir, cand.record_id, title=cand.title, tag=tag
        ):
            row["Record ID Written"] = "y"
            row["State File Record ID"] = cand.record_id

    logger.warning(
        "%s CONVERTING to file-by-file (%s) — this is a ONE-WAY DOOR.",
        tag, row["Verdict"],
    )
    ok = fbf.run_file_by_file(
        esid=cand.esid,
        staging_dir=package.staging_dir,
        raw_dir=package.raw_dir,
        record_id=cand.record_id,
        credentials=credentials,
        community_id=community_id,
        reserve_doi=reserve_doi,
        auto_publish=args.publish,
        upload_attempts=bounded_upload_attempts(args.upload_attempts),
    )
    row["Action Taken"] = _ACTION_CONVERTED if ok else _ACTION_FAILED
    return ok


# =====================================================================
#  Main
# =====================================================================

def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser.

    Returns:
        The configured :class:`argparse.ArgumentParser`.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Scan Zenodo for drafts that are ONLY missing a complete ZIP "
            "archive and finish them file-by-file. READ-ONLY by default: it "
            "writes two CSV reports and changes nothing. --execute converts "
            "the CONVERTIBLE/RESUMABLE drafts, leaving each one a complete "
            "DRAFT unless --publish is given."
        ),
    )
    parser.add_argument(
        "raw_data_dir", metavar="RAW_DATA_DIR",
        help="Folder holding the raw ESID subfolders (ESID#NNN / ESID_NNN).",
    )
    parser.add_argument(
        "--esid", nargs="+", default=None, metavar="ESID_OR_CSV",
        help=(
            "Consider only the specified ESID(s). Each value is either a "
            "literal ESID (1-3 digits, or a suffixed id like 120A / "
            "122_Part_1_of_2) or the path to a CSV whose FIRST column lists "
            "ESIDs (a header row is detected and skipped); numbers and CSV "
            "paths may be mixed. A previous run's summary report works "
            "directly, since ESID# is its first column. Requested ESIDs "
            "with no matching draft are reported and skipped. (Same "
            "semantics as prep_all_datasets.py.)"
        ),
    )
    parser.add_argument(
        "--execute", action="store_true",
        help=(
            "Actually convert. Without this the tool only reads and reports. "
            "Conversion is a ONE-WAY DOOR: the pending ZIP slot is deleted "
            "and the staging folder is marked file-by-file."
        ),
    )
    parser.add_argument(
        "--publish", action="store_true",
        help=(
            "Publish (or submit to the community queue) each record once it "
            "is confirmed complete. OFF by default: the normal outcome is an "
            "inspectable DRAFT, and publication is permanent. Requires "
            "--execute."
        ),
    )
    parser.add_argument(
        "--yes", action="store_true",
        help=(
            "Skip the interactive confirmation --execute otherwise requires. "
            "Needed for unattended runs (nohup, cron)."
        ),
    )
    parser.add_argument(
        "--max-files", type=int, default=_DEFAULT_MAX_FILES, metavar="N",
        help=(
            f"Refuse a draft whose file-by-file set (WAVs + CONFIG.TXT + "
            f"companions) exceeds N files (default: {_DEFAULT_MAX_FILES}). "
            "Zenodo caps a record at 100 files, so the default leaves a "
            "little headroom — with the usual companions that is roughly 85 "
            "WAVs. Checked before any network call for that draft."
        ),
    )
    parser.add_argument(
        "--upload-attempts", type=int, default=_DEFAULT_UPLOAD_ATTEMPTS,
        metavar="N",
        help=(
            f"PUT attempts per file before giving up on it (default: "
            f"{_DEFAULT_UPLOAD_ATTEMPTS}, maximum "
            f"{_MAX_UPLOAD_ATTEMPTS}). Applies to EVERY file: there is no "
            "ZIP on this path."
        ),
    )
    parser.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help=(
            "Convert at most N drafts, then stop (the scan still reports "
            "every one). Use --limit 1 to make the first production run a "
            "canary you can inspect in the Zenodo UI."
        ),
    )
    parser.add_argument(
        "--max-consecutive-failures", type=int,
        default=_DEFAULT_MAX_CONSECUTIVE_FAILURES, metavar="N",
        help=(
            f"Abort the batch after N conversions fail in a row (default: "
            f"{_DEFAULT_MAX_CONSECUTIVE_FAILURES}; 0 disables). An expired "
            "token, an unmounted volume or a Zenodo outage fails every ESID "
            "identically — this catches that after a few rather than after "
            "hundreds."
        ),
    )
    parser.add_argument(
        "--with-hashes", action="store_true",
        help=(
            "Fill the detail report's SHA-512 and MD5 columns from "
            "file_list.csv and the raw folder's wav_hashes.csv. Cache-only "
            "in both modes — this tool NEVER reads a file's bytes to hash "
            "it. Off by default only to keep the detail CSV small."
        ),
    )
    parser.add_argument(
        "--sleep-s", type=float, default=_DEFAULT_SLEEP_S, metavar="SECONDS",
        help=(
            f"Pause between per-draft file listings (default: "
            f"{_DEFAULT_SLEEP_S})."
        ),
    )
    parser.add_argument(
        "--config", default="Resources/config.json", metavar="PATH",
        help=(
            "Path to AZUS config.json (default: Resources/config.json). Read "
            "for community_id and reserve_doi only; auto_publish is ignored "
            "here in favour of the explicit --publish flag."
        ),
    )
    parser.add_argument(
        "--output", default=None, metavar="PATH",
        help=(
            "Where to write the summary CSV (default: "
            "Records/<stamp>_finish_zip_only_drafts.csv). The per-file "
            "detail report is written alongside it with a '_files' suffix."
        ),
    )
    parser.add_argument(
        "--log", default=None, metavar="PATH",
        help=(
            "Where to write this run's log (default: "
            "Records/<stamp>_finish_zip_only_drafts.log)."
        ),
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Log at DEBUG, naming every record considered.",
    )
    return parser


def _filter_by_esid(
    candidates: List[Candidate], requested: List[str]
) -> Tuple[List[Candidate], List[str]]:
    """Keep only requested ESIDs, in the order requested.

    Args:
        candidates: Every discovered candidate.
        requested: Canonical ESIDs from ``--esid``, in the given order.

    Returns:
        ``(kept, missing)`` — ``missing`` lists requested ESIDs with no
        matching draft.
    """
    wanted = set(requested)
    by_esid: Dict[str, List[Candidate]] = {}
    for cand in candidates:
        if cand.esid in wanted:
            by_esid.setdefault(cand.esid, []).append(cand)
    kept: List[Candidate] = []
    missing: List[str] = []
    for esid in requested:
        found = by_esid.get(esid)
        if not found:
            missing.append(esid)
            continue
        kept.extend(found)
    return kept, missing


def main() -> None:
    """Command-line entry point.  See the module docstring for details."""
    parser = _build_parser()
    args = parser.parse_args()
    if args.publish and not args.execute:
        parser.error("--publish requires --execute.")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1.")
    if args.max_consecutive_failures < 0:
        parser.error("--max-consecutive-failures cannot be negative.")

    requested: List[str] = []
    if args.esid:
        try:
            requested = azus_common.load_esid_args(args.esid)
        except ValueError as exc:
            parser.error(f"Invalid --esid value: {exc}")

    log_file = configure_logging(
        args.verbose, Path(args.log) if args.log else None
    )

    raw_root = Path(args.raw_data_dir)
    if not raw_root.is_dir():
        logger.error("Raw data folder not found: %s", raw_root)
        sys.exit(2)

    attempts = bounded_upload_attempts(args.upload_attempts)
    if attempts != args.upload_attempts:
        logger.warning(
            "--upload-attempts %d clamped to %d (the retry backoff defines "
            "waits for at most that many attempts).",
            args.upload_attempts, attempts,
        )

    try:
        credentials = get_credentials_from_env()
    except ValueError as exc:
        logger.error("%s", exc)
        logger.error(
            "Drafts are only visible on /api/user/records, which needs the "
            "API token. Run: source Resources/set_env.sh"
        )
        sys.exit(2)
    if not credentials.base_url.endswith("/"):
        logger.error(
            "INVENIO_RDM_BASE_URL is %r — it MUST end with '/', or every URL "
            "this tool builds is malformed. Fix the export and re-run.",
            credentials.base_url,
        )
        sys.exit(2)

    summary_path, detail_path = default_output_paths()
    if args.output:
        summary_path = Path(args.output)
        detail_path = summary_path.with_name(
            f"{summary_path.stem}_files{summary_path.suffix or '.csv'}"
        )
    is_sandbox = "sandbox" in credentials.base_url.lower()

    title_res = [
        err_mod.compile_title_pattern(p)
        for p in err_mod._DEFAULT_TITLE_PATTERNS
    ]

    logger.info("=" * 70)
    logger.info(
        "ZIP-ONLY DRAFTS — %s",
        "EXECUTE (drafts WILL be converted file-by-file)" if args.execute
        else "SCAN (read-only; nothing will be changed)",
    )
    logger.info("=" * 70)
    logger.info("Endpoint:   %s   <<< %s >>>", credentials.base_url,
                "SANDBOX" if is_sandbox else "PRODUCTION")
    logger.info("Raw data:   %s", raw_root.resolve())
    logger.info("Staging:    %s", _STAGING_AREA)
    logger.info("Max files:  %d per record", args.max_files)
    if args.execute:
        logger.info("Publish:    %s",
                    "YES (permanent)" if args.publish else "no (draft only)")
        logger.info("Attempts:   %d per file", attempts)
        if args.limit:
            logger.info("Limit:      %d draft(s) this run", args.limit)
    logger.info("Summary:    %s", summary_path)
    logger.info("Detail:     %s", detail_path)
    if log_file:
        logger.info("Log:        %s", log_file)
    logger.info("=" * 70)

    try:
        candidates, published, ignored = discover_drafts(
            credentials, title_res, []
        )
    except err_mod.ReportError as exc:
        logger.error("SCAN ABORTED — %s", exc)
        logger.error("No CSV was written; a truncated scan must not look "
                     "complete.")
        sys.exit(2)
    except Exception as exc:  # noqa: BLE001 — any listing failure is fatal
        logger.error("Listing failed (%s: %s).", type(exc).__name__, exc)
        logger.error("No CSV was written; a truncated scan must not look "
                     "complete.")
        sys.exit(2)

    logger.info(
        "Account listing: %d in-scope draft(s), %d published (out of scope), "
        "%d non-ESID title(s) ignored.", len(candidates), published, ignored,
    )
    if requested:
        candidates, missing = _filter_by_esid(candidates, requested)
        if missing:
            logger.warning(
                "%d requested ESID(s) have no in-scope draft and are skipped: "
                "%s", len(missing), ", ".join(missing),
            )
        logger.info("--esid filter: %d draft(s) remain.", len(candidates))
    if not candidates:
        logger.info("Nothing in scope — no report written.")
        sys.exit(0)

    if args.execute and not args.yes:
        if not sys.stdin.isatty():
            logger.error(
                "stdin is not a terminal, so the confirmation cannot be "
                "answered. Re-run with --yes for unattended use."
            )
            sys.exit(2)
        print(f"\n⚠️  This will convert up to {args.limit or len(candidates)} "
              f"draft(s) on {'SANDBOX' if is_sandbox else 'PRODUCTION'} "
              f"Zenodo to file-by-file mode.")
        print("   Each conversion deletes that record's pending ZIP slot. "
              "There is no way back.")
        if args.publish:
            print("   Completed records WILL be published. That is permanent.")
        if input("\nProceed? (yes/no): ").strip().lower() != "yes":
            logger.info("Cancelled by user.")
            sys.exit(0)

    staging_index = index_esid_folders(_STAGING_AREA)
    raw_index = index_esid_folders(raw_root)
    publish_config: Tuple[Optional[str], bool] = (None, False)
    if args.execute:
        community_id, reserve_doi, _auto = _load_publish_config(args.config)
        publish_config = (community_id, reserve_doi)
        if args.publish and community_id:
            logger.info(
                "Completed records will be SUBMITTED to community %s for "
                "review.", community_id,
            )

    install_stop_handlers()

    verdict_counts: Dict[str, int] = {}
    converted = 0
    classified = 0
    needs_attention = 0
    consecutive_failures = 0
    breaker_tripped = False
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    # Both CSVs are written row-by-row and flushed, so a kill loses at most
    # the in-flight ESID — whose state is still recoverable from Zenodo.
    with open(summary_path, "w", encoding="utf-8", newline="") as sfh, \
            open(detail_path, "w", encoding="utf-8", newline="") as dfh:
        swriter = csv.DictWriter(sfh, fieldnames=_SUMMARY_COLUMNS)
        dwriter = csv.DictWriter(dfh, fieldnames=_DETAIL_COLUMNS)
        swriter.writeheader()
        dwriter.writeheader()
        sfh.flush()
        dfh.flush()

        for index, cand in enumerate(candidates):
            row, detail_rows, package = classify_candidate(
                cand, credentials, staging_index, raw_index,
                args.max_files, with_hashes=args.with_hashes,
            )
            verdict = row["Verdict"]
            verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1

            actionable = verdict in _ACTIONABLE_VERDICTS
            if not args.execute:
                row["Action Taken"] = (
                    _ACTION_NONE_DRY_RUN if actionable else _ACTION_NONE
                )
            elif not actionable:
                row["Action Taken"] = _ACTION_NONE
            elif breaker_tripped:
                row["Action Taken"] = _ACTION_SKIPPED_BREAKER
            elif stop_requested():
                row["Action Taken"] = _ACTION_SKIPPED_STOP
            elif args.limit is not None and converted >= args.limit:
                row["Action Taken"] = _ACTION_SKIPPED_LIMIT
            else:
                if convert_candidate(
                    cand, package, credentials, row, args, publish_config,
                ):
                    converted += 1
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    if (args.max_consecutive_failures
                            and consecutive_failures
                            >= args.max_consecutive_failures):
                        breaker_tripped = True
                        logger.error(
                            "STOPPING — %d conversion(s) failed in a row. "
                            "That pattern means a systemic fault (expired "
                            "token, unmounted volume, Zenodo outage), not %d "
                            "unlucky ESIDs. Remaining drafts are reported but "
                            "not touched.",
                            consecutive_failures, consecutive_failures,
                        )

            logger.info(
                "[ESID %s] %s — %s", row["ESID#"] or "?", verdict,
                row["Action Taken"] if row["Action Taken"] != _ACTION_NONE
                else _RECOMMENDED_ACTION.get(verdict, "review"),
            )
            if row["Notes"]:
                logger.debug("[ESID %s] %s", row["ESID#"] or "?", row["Notes"])
            swriter.writerow(row)
            dwriter.writerows(detail_rows)
            sfh.flush()
            dfh.flush()
            classified += 1
            if row_needs_attention(row):
                needs_attention += 1

            if stop_requested() and not args.execute:
                logger.warning(
                    "Stopping the scan early as requested (%d of %d draft(s) "
                    "classified).", index + 1, len(candidates),
                )
                break
            if args.sleep_s > 0 and index + 1 < len(candidates):
                time.sleep(args.sleep_s)

    logger.info("=" * 70)
    logger.info("SUMMARY")
    logger.info("=" * 70)
    for verdict in sorted(verdict_counts):
        logger.info("  %-27s %d", verdict, verdict_counts[verdict])
    logger.info("Drafts classified:  %d", classified)
    if args.execute:
        logger.info("Converted:          %d", converted)
    logger.info("Need attention:     %d", needs_attention)
    logger.info("Summary report:     %s", summary_path)
    logger.info("Detail report:      %s", detail_path)
    if not args.execute:
        ready = verdict_counts.get(CONVERTIBLE, 0) + verdict_counts.get(
            RESUMABLE, 0
        )
        logger.warning(
            "READ-ONLY run — 0 writes performed. %d draft(s) are ready to "
            "convert; review the summary report, then re-run with --execute "
            "(start with --limit 1).", ready,
        )
    if breaker_tripped:
        logger.error(
            "The batch was cut short by --max-consecutive-failures. Fix the "
            "underlying fault, then re-run — re-running is always safe."
        )
    if stop_requested():
        logger.warning("Stopped on request; both reports are complete as far "
                       "as the run got.")
    logger.info("=" * 70)
    sys.exit(1 if needs_attention else 0)


if __name__ == "__main__":
    main()
