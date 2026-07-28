#!/usr/bin/env python3
"""Finish a stuck ESID upload by sending its files individually.

WHY THIS EXISTS
===============
The preferred upload packs every WAV + CONFIG.TXT into one ZIP and sends
that single archive to Zenodo.  For the largest datasets the ZIP upload
consistently times out mid-transfer, losing hours each attempt.  This
module implements the FALLBACK: upload the individual WAV files (straight
from ``Raw_Data/ESID#NNN/``) plus ``CONFIG.TXT`` and the normal standalone
companion files directly to the SAME existing Zenodo draft, in place of
the ZIP.  Small files rarely time out, and a single failed file is cheap
to retry.

This is a LIBRARY module driven by ``finish_stuck_uploads.py
--enable-file-by-file``; it has no CLI of its own.  The heavy lifting
(draft resume, per-file transfer + md5 verification, publish/community
submit) is reused from ``standalone_uploader.upload_to_zenodo`` — this
module only swaps the file list and adds the file-by-file-specific
safety gates.

SAFETY INVARIANTS (see run_file_by_file)
========================================
* The ESID is marked ``mode == file_by_file`` in ``upload_state.json``
  BEFORE any upload, so the ZIP pipeline (standalone_tasks.py) skips it
  and the two never fight over the same record.
* The complete required set (every WAV + CONFIG.TXT + every standalone
  companion) is derived from the prep manifests and must be present
  locally, hash-verified, uploaded, AND confirmed committed on the record
  before the record is published / submitted for review.
* The ZIP is explicitly removed from the record — a file-by-file record
  must never carry the ZIP.
"""

import csv
import io
import json
import logging
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# --- Make sibling repo-root and Resources/ modules importable regardless
#     of how this module is imported (as a Resources/ script sibling, or
#     from the project root, or from the test suite). ---
_HERE = Path(__file__).resolve()
_RESOURCES_DIR = _HERE.parent
_PROJECT_ROOT = _RESOURCES_DIR.parent
for _p in (str(_PROJECT_ROOT), str(_RESOURCES_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import azus_common  # noqa: E402
from prepare_dataset import _FILE_LIST_HEADERS  # noqa: E402
from models.audiomoth import DraftConfig  # noqa: E402
from standalone_tasks import archive_staging_to_uploaded  # noqa: E402
from standalone_uploader import (  # noqa: E402
    Credentials,
    delete_draft_file,
    ensure_doi_reserved,
    get_draft_record,
    list_draft_files,
    publish_draft,
    submit_to_community_review,
    upload_to_zenodo,
)

logger = logging.getLogger("azus.file_by_file")

_MANIFEST_TEMPLATE = "ESID_{esid}_to_upload.csv"
_FILE_LIST_NAME = "file_list.csv"


# ===================================================================
#  Small readers / classifiers
# ===================================================================

def _zip_name(esid: str) -> str:
    """Return the archive file name for an ESID.

    Args:
        esid: Canonical ESID string (e.g. ``"064"``).

    Returns:
        The ZIP file name, e.g. ``"ESID_064.zip"``.
    """
    return f"ESID_{esid}.zip"


def is_raw_upload_name(name: str) -> bool:
    """Report whether a file name is uploaded from ``Raw_Data`` in this mode.

    That is exactly the set of files that live ONLY inside the ZIP in a
    normal upload: every ``.wav``/``.WAV`` audio file and ``CONFIG.TXT``.

    Args:
        name: A bare file name.

    Returns:
        True for a WAV file or ``CONFIG.TXT`` (case-insensitive).
    """
    return name.lower().endswith(".wav") or name.upper() == "CONFIG.TXT"


def _load_csv_rows(path: Path) -> List[Dict[str, str]]:
    """Read a CSV into a list of row dicts (empty list if absent).

    Args:
        path: CSV file to read.

    Returns:
        The rows as dicts; ``[]`` when the file does not exist.
    """
    if not path.is_file():
        return []
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def required_files(
    staging_dir: Path, esid: str
) -> Tuple[List[Tuple[str, str]], List[str]]:
    """Derive the authoritative file-by-file upload set for an ESID.

    The "required set" is what a ZIP-type upload would put on the record,
    minus the ZIP itself: every WAV + ``CONFIG.TXT`` (sourced from
    ``Raw_Data``, with the SHA-512 recorded by prep) plus every standalone
    companion (README.md, file_list.csv, total_eclipse_data.csv, data
    dictionaries, License, PDFs, related_identifiers.csv — sourced from the
    staging folder).  Raw files come from ``file_list.csv`` (it carries
    their hashes); companions come from the prep upload manifest (the
    authoritative list of what prep chose to upload standalone).

    Args:
        staging_dir: The ``ESID_NNN_Staging`` folder.
        esid: Canonical ESID string.

    Returns:
        A ``(raw_files, companion_names)`` tuple.  ``raw_files`` is a list
        of ``(name, sha512)`` for each WAV + CONFIG.TXT; ``companion_names``
        is the list of standalone companion file names (the ZIP excluded).
    """
    zip_name = _zip_name(esid)

    file_list_rows = _load_csv_rows(staging_dir / _FILE_LIST_NAME)
    raw_files: List[Tuple[str, str]] = []
    for row in file_list_rows:
        name = (row.get("File Name") or "").strip()
        if name and name != zip_name and is_raw_upload_name(name):
            raw_files.append((name, (row.get("SHA-512 Hash") or "").strip()))

    manifest_rows = _load_csv_rows(
        staging_dir / _MANIFEST_TEMPLATE.format(esid=esid)
    )
    companion_names = [
        (row.get("File Name") or "").strip()
        for row in manifest_rows
        if (row.get("File Name") or "").strip()
        and (row.get("File Name") or "").strip() != zip_name
    ]
    return raw_files, companion_names


def only_zip_missing(
    credentials: Credentials, record_id: str, staging_dir: Path, esid: str
) -> bool:
    """Report whether the ZIP is the SOLE thing still missing on the record.

    The safe trigger for switching to file-by-file: it must be the big ZIP
    timing out, not a systemic problem.  True iff every standalone
    companion is already committed on the Zenodo record AND the ZIP is not
    committed.  If any companion is also missing (a companion failure) or
    the ZIP is already committed (nothing to switch), returns False.

    Args:
        credentials: Zenodo API credentials.
        record_id: The draft/record id to inspect.
        staging_dir: The ESID's staging folder (for the companion list).
        esid: Canonical ESID string.

    Returns:
        True when only the ZIP is absent from an otherwise-complete set of
        committed companions.
    """
    _raw, companion_names = required_files(staging_dir, esid)
    entries = list_draft_files(credentials, record_id)
    committed = {
        e.get("key") for e in entries if e.get("status") == "completed"
    }
    if any(name not in committed for name in companion_names):
        return False  # a companion also failed — not a ZIP-only problem
    return _zip_name(esid) not in committed


# ===================================================================
#  Atomic manifest rewrites (requirement 6)
# ===================================================================

def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write bytes to ``path`` atomically (temp file + ``os.replace``).

    Args:
        path: Destination file.
        data: Bytes to write.
    """
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def rewrite_file_list_without_zip(staging_dir: Path, esid: str) -> None:
    """Rewrite ``file_list.csv`` with the ZIP row removed.

    The file-by-file record carries the individual files, not the ZIP, so
    the uploaded ``file_list.csv`` must not list a ZIP row.  Every other
    row (per-WAV, CONFIG.TXT, companions) is preserved verbatim.  A no-op
    when the file is absent or already has no ZIP row.

    Args:
        staging_dir: The staging folder.
        esid: Canonical ESID string.
    """
    path = staging_dir / _FILE_LIST_NAME
    rows = _load_csv_rows(path)
    if not rows:
        return
    zip_name = _zip_name(esid)
    kept = [r for r in rows if (r.get("File Name") or "").strip() != zip_name]
    if len(kept) == len(rows):
        return  # no ZIP row to drop
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf, fieldnames=_FILE_LIST_HEADERS, extrasaction="ignore"
    )
    writer.writeheader()
    writer.writerows(kept)
    _atomic_write_bytes(path, buf.getvalue().encode("utf-8"))
    logger.info("  Rewrote %s without the ZIP row.", path.name)


def rewrite_manifest_file_by_file(
    staging_dir: Path, esid: str, entries: List[Tuple[str, str]]
) -> None:
    """Rewrite the upload manifest to show the file-by-file set (req 6).

    A record of what this file-by-file record contains — the individual
    WAVs + CONFIG.TXT + companions, not the ZIP.  (The manifest is not
    re-consumed for the upload; the file-by-file tool builds its own list.)

    Two provenance copies are kept beside it so the history of what each
    strategy tried survives the rewrite:

    * ``ESID_NNN_zip_attempt_upload.csv`` — the manifest AS IT WAS for the
      ZIP attempt.  Written ONCE: on a re-run the live manifest is already
      the file-by-file version, and copying it again would overwrite the
      original ZIP-attempt history with a duplicate of the new set.
    * ``ESID_NNN_file_by_file_upload.csv`` — a mirror of the new manifest.

    Neither copy is uploaded: they are not in the upload list this module
    builds, and prepare_dataset.py excludes them from the manifest it
    generates on a re-prep.

    Args:
        staging_dir: The staging folder.
        esid: Canonical ESID string.
        entries: ``(file_name, note)`` pairs to list, in upload order.
    """
    path = staging_dir / _MANIFEST_TEMPLATE.format(esid=esid)

    # Snapshot the ZIP-attempt manifest BEFORE the rewrite destroys it.
    # Fail-closed by design: this runs ahead of the overwrite, so an I/O
    # error here aborts the ESID (run_file_by_file's handler) with the
    # original manifest still intact.
    zip_attempt = staging_dir / azus_common.MANIFEST_ARCHIVE_ZIP_ATTEMPT.format(
        esid=esid
    )
    if path.is_file() and not zip_attempt.is_file():
        _atomic_write_bytes(zip_attempt, path.read_bytes())
        logger.info("  Archived the ZIP-attempt manifest as %s.",
                    zip_attempt.name)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["File Name", "Notes"])
    writer.writeheader()
    for name, note in entries:
        writer.writerow({"File Name": name, "Notes": note})
    data = buf.getvalue().encode("utf-8")
    _atomic_write_bytes(path, data)
    logger.info("  Rewrote %s to the file-by-file set (%d files).",
                path.name, len(entries))

    # Mirror the new manifest, so the file-by-file set stays legible even
    # if a later run rewrites the live manifest again.
    fbf_copy = staging_dir / azus_common.MANIFEST_ARCHIVE_FILE_BY_FILE.format(
        esid=esid
    )
    _atomic_write_bytes(fbf_copy, data)
    logger.info("  Archived the file-by-file manifest as %s.", fbf_copy.name)


# ===================================================================
#  Mode marker
# ===================================================================

def mark_file_by_file_mode(staging_dir: Path) -> None:
    """Record ``mode == file_by_file`` in the folder's upload_state.json.

    Written BEFORE any upload so the ZIP pipeline's requirement-9 skip
    engages immediately and a mid-run failure resumes file-by-file rather
    than reverting to ZIP.  Read-merges the existing state so record_id /
    number_of_tries are preserved.  Best-effort: a write failure is logged,
    not raised (the mode is re-asserted on the next run).

    Args:
        staging_dir: The staging folder whose state file to mark.
    """
    state_file = staging_dir / azus_common.STATE_FILENAME
    try:
        state: Dict[str, object] = {}
        if state_file.is_file():
            loaded = json.loads(state_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state = loaded
        if state.get("mode") == azus_common.FILE_BY_FILE_MODE:
            return
        state["mode"] = azus_common.FILE_BY_FILE_MODE
        _atomic_write_bytes(
            state_file, json.dumps(state, indent=2).encode("utf-8")
        )
        logger.info("  Marked %s as file-by-file mode.", state_file.name)
    except (OSError, ValueError, TypeError) as exc:
        logger.warning(
            "  Could not mark file-by-file mode in %s (%s).", state_file, exc,
        )


# ===================================================================
#  The orchestrator
# ===================================================================

def run_file_by_file(
    esid: str,
    staging_dir: Path,
    raw_dir: Path,
    record_id: str,
    credentials: Credentials,
    *,
    community_id: Optional[str] = None,
    reserve_doi: bool = False,
    auto_publish: bool = False,
) -> bool:
    """Finish one ESID's upload file-by-file against its existing draft.

    Idempotent and re-runnable: already-committed files are skipped by the
    resume logic, an already-deleted ZIP is a no-op, and an already
    submitted/published record is left as-is.  The record is published or
    submitted for community review ONLY after every required file is
    confirmed committed on the record and the ZIP is confirmed absent.

    Args:
        esid: Canonical ESID string (e.g. ``"064"``).
        staging_dir: The ``ESID_NNN_Staging`` folder (companions + manifests).
        raw_dir: The ``Raw_Data/ESID#NNN`` folder (the WAVs + CONFIG.TXT).
        record_id: The existing Zenodo draft/record id to finish.
        credentials: Zenodo API credentials.
        community_id: Community to submit the finished record to (or None).
        reserve_doi: Reserve a DataCite DOI before submit/publish.
        auto_publish: Publish the record outright once complete.

    Returns:
        True when the record is complete (all files committed + verified)
        and published/submitted/left-as-draft as configured; False on any
        failure (nothing is published past an incomplete or unverified set).
    """
    tag = f"[ESID {esid}]"
    zip_name = _zip_name(esid)
    try:
        # 1. Derive the authoritative required set.
        raw_files, companion_names = required_files(staging_dir, esid)
        if not raw_files:
            logger.error(
                "%s No WAV/CONFIG rows in file_list.csv — cannot build the "
                "file-by-file set. Aborting (nothing uploaded).", tag,
            )
            return False

        # 2. Resolve local paths; a missing required file is fatal (an
        #    under-collected set must NEVER reach publish — req 8).
        resolved: List[str] = []
        missing: List[str] = []
        for name, base_dir in (
            [(n, staging_dir) for n in companion_names]
            + [(n, raw_dir) for n, _ in raw_files]
        ):
            p = base_dir / name
            if p.is_file():
                resolved.append(str(p))
            else:
                missing.append(name)
        if missing:
            logger.error(
                "%s %d required file(s) not found locally — aborting before "
                "any upload: %s", tag, len(missing), ", ".join(missing[:10]),
            )
            return False

        # 3. Pre-verify the raw WAV/CONFIG bytes against prep's SHA-512, so
        #    the uploaded audio is provably the content prep verified (a raw
        #    file modified since prep would otherwise pass md5-vs-Zenodo).
        hash_problems: List[str] = []
        for name, expected in raw_files:
            if not expected:
                hash_problems.append(f"{name} (no hash recorded in file_list.csv)")
                continue
            actual = azus_common.calculate_sha512(str(raw_dir / name))
            if actual != expected:
                hash_problems.append(f"{name} (SHA-512 mismatch vs file_list.csv)")
        if hash_problems:
            logger.error(
                "%s %d raw file(s) failed SHA-512 pre-verification — aborting "
                "(nothing uploaded): %s", tag, len(hash_problems),
                ", ".join(hash_problems[:10]),
            )
            return False

        # 4. Confirm the draft exists; NEVER let a 404 fall through to fresh
        #    creation (which would mint a blank duplicate record).
        if get_draft_record(credentials, record_id) is None:
            logger.error(
                "%s Draft record %s not found on Zenodo (404). Aborting — "
                "reset this folder's upload_state.json to re-create/re-upload.",
                tag, record_id,
            )
            return False

        # 5. ZIP-STATE GUARD (before any mutation). Inspect the record: if the
        #    ZIP is already COMMITTED, the ZIP upload SUCCEEDED — this ESID is
        #    NOT a file-by-file candidate. Refuse to delete the good ZIP or
        #    switch; file-by-file exists ONLY for a ZIP that keeps FAILING.
        #    Abort WITHOUT marking the mode or touching anything.
        entries = list_draft_files(credentials, record_id)
        zip_entry = next(
            (e for e in entries if e.get("key") == zip_name), None
        )
        if zip_entry is not None and zip_entry.get("status") == "completed":
            logger.error(
                "%s The ZIP is already COMMITTED on the record — the ZIP "
                "upload SUCCEEDED, so this ESID is NOT switched to "
                "file-by-file and the committed ZIP is left untouched. "
                "(File-by-file is only for a ZIP that keeps failing. If you "
                "genuinely intend file-by-file, delete the ZIP from the "
                "record manually first.)", tag,
            )
            return False

        # 6. POINT OF NO RETURN. Only an INCOMPLETE (never-committed) ZIP slot
        #    can reach here. Mark file-by-file mode (so the ZIP pipeline stands
        #    down — marking only NOW means a failed pre-check above left the
        #    ESID recoverable as ZIP), then clear that dead slot: a
        #    file-by-file record must not carry the ZIP, and a lingering
        #    pending slot would block publish.
        mark_file_by_file_mode(staging_dir)
        if zip_entry is not None:
            logger.warning(
                "%s Clearing INCOMPLETE ZIP slot from record: %s (status=%s)",
                tag, zip_name, zip_entry.get("status"),
            )
            delete_draft_file(credentials, record_id, zip_name)
            entries = list_draft_files(credentials, record_id)
            if any(e.get("key") == zip_name for e in entries):
                logger.error(
                    "%s ZIP entry %s still present after delete — aborting.",
                    tag, zip_name,
                )
                return False

        # 6. Update the on-disk manifests to reflect the file-by-file set.
        rewrite_file_list_without_zip(staging_dir, esid)
        manifest_entries = (
            [(n, "companion") for n in companion_names]
            + [(n, "raw (Raw_Data)") for n, _ in raw_files]
        )
        rewrite_manifest_file_by_file(staging_dir, esid, manifest_entries)

        # 7. Upload the missing files with publish OFF — this module owns the
        #    publish gate (step 8), so upload_to_zenodo must not publish or
        #    submit before completeness is confirmed.
        pids = (
            {"doi": {"provider": "datacite", "identifier": ""}}
            if reserve_doi else None
        )
        config = DraftConfig(community_id=community_id, pids=pids)
        logger.info(
            "%s Uploading file-by-file: %d companion(s) + %d raw file(s) to "
            "record %s.", tag, len(companion_names), len(raw_files), record_id,
        )
        result = upload_to_zenodo(
            files=resolved,
            config=config,
            existing_draft_id=record_id,
            state_file_path=str(staging_dir / azus_common.STATE_FILENAME),
            title_guard=False,
            auto_publish=False,
            submit_review=False,
        )
        if not result.get("successful"):
            err = (result.get("error") or {}).get("error_message", "unknown")
            logger.error("%s File-by-file upload failed: %s", tag, err)
            return False

        # 8. Completeness gate (req 8): every required file must now be
        #    COMMITTED on the record, and the ZIP must be absent, BEFORE any
        #    publish/submit.
        entries = list_draft_files(credentials, record_id)
        committed = {
            e.get("key") for e in entries if e.get("status") == "completed"
        }
        required_names = {Path(p).name for p in resolved}
        not_committed = sorted(required_names - committed)
        if not_committed:
            logger.error(
                "%s NOT publishing — %d required file(s) are not committed on "
                "the record: %s", tag, len(not_committed),
                ", ".join(not_committed[:10]),
            )
            return False
        if any(e.get("key") == zip_name for e in entries):
            logger.error(
                "%s NOT publishing — the ZIP is still on the record.", tag,
            )
            return False

        # 9. Reserve DOI (best-effort), then submit-to-community / publish.
        ensure_doi_reserved(credentials, record_id)
        if community_id:
            logger.info("%s Submitting to community review queue...", tag)
            submit_to_community_review(credentials, record_id, community_id)
        elif auto_publish:
            logger.info("%s Publishing record...", tag)
            publish_draft(credentials, record_id)

        # 10. Archive the completed staging folder out of Staging_Area/.
        archive_staging_to_uploaded(staging_dir, esid, tag)
        logger.info(
            "%s File-by-file upload COMPLETE (%d files on record).",
            tag, len(required_names),
        )
        return True

    except Exception as exc:  # one bad ESID must not abort the batch
        logger.error(
            "%s File-by-file upload FAILED (%s: %s).",
            tag, type(exc).__name__, exc,
        )
        return False
