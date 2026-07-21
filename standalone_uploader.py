"""Zenodo API client for creating records and uploading files.

This module handles direct HTTP communication with the Zenodo/InvenioRDM
API.  It has no dependency on Prefect or any orchestration framework.

All functions are synchronous.  For future Prefect integration, wrap calls
in ``@task``-decorated functions.

Environment variables required:
    INVENIO_RDM_ACCESS_TOKEN: Zenodo API bearer token.
    INVENIO_RDM_BASE_URL: Zenodo API base URL (e.g., https://zenodo.org/api/).
"""

import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.exceptions import HTTPError, RequestException

logger = logging.getLogger("azus.uploader")


# ===================================================================
#  Upload retry tuning
# ===================================================================
# Single-PUT uploads of multi-GB files can take hours and ride on one
# long-lived TLS connection.  Transient drops (SSLEOFError, ConnectionError,
# socket reset by an intermediate proxy) should not fail the whole job.
# Each retry restarts the PUT from byte 0 — Zenodo's draft file PUT
# replaces server-side content on each call, so retrying is safe but
# expensive.  Three attempts is a sensible ceiling.
_PUT_RETRY_ATTEMPTS = 3
_PUT_RETRY_BACKOFF_S = (30, 90, 270)  # before attempts 2, 3, (4 not used)

# Metadata API calls (GET /draft, GET /draft/files) are sub-second under
# normal conditions but Zenodo occasionally returns transient 5xx — usually
# during deploys or backend GC.  These calls are cheap so a shorter backoff
# is appropriate.  Same 3-attempt ceiling for consistency with PUT retries.
_API_RETRY_ATTEMPTS = 3
_API_RETRY_BACKOFF_S = (5, 15, 45)

# Draft-creation POST retry tuning.  Unlike a file PUT, POST /records is
# NOT idempotent: an attempt that LOOKS failed (5xx, dropped connection)
# may still have created the draft server-side, and a blind retry would
# mint a duplicate record.  Every retry is therefore preceded by a
# title-guard search that ADOPTS any draft the earlier attempt actually
# created (see _create_draft_with_guarded_retry).  A July 2026
# production run hit hundreds of transient HTTP 500s on exactly this
# POST, each failing its whole dataset in one shot — hence the retry.
_POST_RETRY_ATTEMPTS = 3
# Backoffs before the guard search that follows each failed attempt;
# the last value repeats when there are more attempts than entries.
_POST_RETRY_BACKOFF_S = (30, 90)
# Zenodo's record search is eventually consistent (records are indexed
# AFTER the database commit), so a draft created by a failed-looking
# POST may not be searchable yet when the guard runs.  After a retry
# POST succeeds, wait this long and search once more — if the earlier
# attempt's phantom draft has surfaced, the fresh (still empty) draft
# is deleted and the run fails closed or adopts, never keeps both.
_POST_SUCCESS_SWEEP_DELAY_S = 15

# Small settle before the first draft-creation POST (belt-and-suspenders,
# operator-requested).  Not the cause of the observed 500s — see the note
# at the POST call site — just a brief pause after the duplicate-guard
# search.
_PRE_CREATE_PAUSE_S = 2

# DOI reservation on the dedicated create-then-reserve endpoint is
# best-effort: retry a transient 5xx a few times, then warn and continue
# (Zenodo mints the DOI at publish if it could not be reserved early), so
# a DataCite outage never fails the record or its upload.
_DOI_RESERVE_ATTEMPTS = 3
_DOI_RESERVE_BACKOFF_S = (5, 15)  # before attempts 2 and 3

# Read-buffer size for local file hashing.  Mirrors
# Resources/azus_common.py HASH_BUFFER_SIZE — kept local (not imported)
# so this lowest-level module stays importable without the Resources/
# directory on sys.path.
_HASH_BUFFER_SIZE = 65_536

# (connect, read) timeout applied to EVERY Zenodo HTTP call.  Without a
# timeout, a half-open connection (proxy drop, load-balancer black hole)
# blocks forever — and never raises, so the retry/backoff machinery never
# fires and an unattended multi-hour batch wedges on one dead socket.
# The read timeout is per-socket-operation (time between bytes moving),
# not total transfer time, so a healthy multi-hour PUT of a 43 GB ZIP is
# unaffected; only a stalled connection trips it and becomes retryable.
_CONNECT_TIMEOUT_S = 10
_READ_TIMEOUT_S = 300
_REQUEST_TIMEOUT = (_CONNECT_TIMEOUT_S, _READ_TIMEOUT_S)


# ===================================================================
#  File integrity verification
# ===================================================================

class FileIntegrityError(Exception):
    """A file on Zenodo does not match the local file it should mirror.

    Raised when a committed upload's size/checksum disagrees with the
    local file, or when a resume run cannot verify an already-committed
    file.  Always fails the dataset — a mismatched file must never be
    left on a record that could be published.
    """


def _calculate_md5(file_path: str) -> str:
    """Stream a file through md5 (Zenodo's checksum algorithm).

    Args:
        file_path: Local path to the file to hash.

    Returns:
        The file's md5 digest as a lowercase hex string.
    """
    md5 = hashlib.md5()
    with open(file_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_HASH_BUFFER_SIZE), b""):
            md5.update(chunk)
    return md5.hexdigest()


def _read_number_of_tries(state_file: Path) -> int:
    """Read the attempt counter from an existing ``upload_state.json``.

    Returns the stored ``number_of_tries``, or 0 when the file does not
    exist yet (first attempt), predates the field (older AZUS versions),
    or is unreadable/malformed — in every one of those cases the caller
    increments from 0, so the field is created at 1 on this attempt.

    Args:
        state_file: Path to the ESID's ``upload_state.json``.

    Returns:
        The prior attempt count (never negative).
    """
    if not state_file.is_file():
        return 0
    try:
        import json as _json
        state = _json.loads(state_file.read_text(encoding="utf-8"))
        return max(0, int(state.get("number_of_tries", 0)))
    except (OSError, ValueError, TypeError) as exc:
        logger.warning(
            "Could not read number_of_tries from %s (%s) — counting "
            "from 0.", state_file, exc,
        )
        return 0


def _remote_entry_mismatch(
    entry: Dict[str, Any],
    local_size: int,
    local_md5: Optional[str] = None,
) -> Optional[str]:
    """Compare a Zenodo draft file entry against the local file's facts.

    Args:
        entry: A file entry dict from Zenodo (``size``, ``checksum`` keys).
        local_size: The local file's size in bytes.
        local_md5: The local file's md5 hex digest, or None to skip the
            checksum comparison (size-only check).

    Returns:
        A human-readable mismatch description, or None when the remote
        entry matches everything it was given to compare against.
    """
    remote_size = entry.get("size")
    if remote_size is not None:
        try:
            remote_size_int = int(remote_size)
        except (TypeError, ValueError):
            return f"Zenodo reported an unparseable size: {remote_size!r}"
        if remote_size_int != local_size:
            return (
                f"size mismatch — Zenodo holds {remote_size_int} bytes, "
                f"local file is {local_size} bytes"
            )
    checksum = entry.get("checksum") or ""
    if local_md5 is not None and checksum.startswith("md5:"):
        if checksum[len("md5:"):] != local_md5:
            return (
                f"checksum mismatch — Zenodo holds {checksum}, "
                f"local file is md5:{local_md5}"
            )
    return None


# ===================================================================
#  Credentials
# ===================================================================

@dataclass
class Credentials:
    """Zenodo API credentials loaded from environment variables.

    Attributes:
        token: Bearer token for API authentication.
        base_url: Zenodo API base URL (must end with '/').
    """

    token: str
    base_url: str


def get_credentials_from_env() -> Credentials:
    """Load Zenodo credentials from environment variables.

    Returns:
        Credentials dataclass.

    Raises:
        ValueError: If credentials are not set or still contain placeholders.
    """
    token = os.getenv("INVENIO_RDM_ACCESS_TOKEN", "")
    base_url = os.getenv("INVENIO_RDM_BASE_URL", "")

    # Historical placeholder values from set_env.sh templates — the
    # misspelled "ACESS" variant shipped in early copies, so both
    # spellings must be treated as "not configured".
    if not token or token in ("ZENODO_ACESS_TOKEN", "ZENODO_ACCESS_TOKEN"):
        raise ValueError(
            "INVENIO_RDM_ACCESS_TOKEN not set or still using placeholder. "
            "Update Resources/set_env.sh and run: source Resources/set_env.sh"
        )

    if not base_url:
        raise ValueError(
            "INVENIO_RDM_BASE_URL not set. "
            "Update Resources/set_env.sh and run: source Resources/set_env.sh"
        )

    return Credentials(token=token, base_url=base_url)


# ===================================================================
#  Low-level Zenodo API operations
# ===================================================================

def _auth_headers(credentials: Credentials, content_type: Optional[str] = None) -> Dict[str, str]:
    """Build authorization headers for Zenodo API requests.

    Args:
        credentials: API credentials.
        content_type: Optional Content-Type header value.

    Returns:
        Headers dictionary.
    """
    headers = {"Authorization": f"Bearer {credentials.token}"}
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def create_draft_record(
    credentials: Credentials,
    metadata: Dict[str, Any],
) -> Dict[str, Any]:
    """Create a draft record on Zenodo.

    Args:
        credentials: Zenodo credentials.
        metadata: Record metadata payload.

    Returns:
        API response with draft record details.

    Raises:
        HTTPError: If the API request fails.
    """
    url = f"{credentials.base_url}records"
    response = requests.post(
        url,
        json=metadata,
        headers=_auth_headers(credentials, content_type="application/json"),
        timeout=_REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def _lucene_phrase(title: str) -> str:
    """Quote a title as a Lucene phrase, escaping backslashes and quotes.

    A title containing ``"`` embedded raw into ``metadata.title:"..."``
    breaks the query — Zenodo would either reject it or, worse, match
    nothing, which the guards would read as "no duplicate exists".

    Args:
        title: The record title to embed in a search query.

    Returns:
        The title wrapped in double quotes, with backslashes and double
        quotes escaped for Lucene phrase syntax.
    """
    escaped = title.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _search_drafts_by_title(
    credentials: Credentials,
    intended_title: str,
    label: str,
) -> "Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]":
    """Search the account's records for a title; fail closed on anything odd.

    Used by both duplicate guards (the pre-creation guard and the
    retry/sweep guard).  Unlike a plain search call, an unrecognized
    response body — a 200 without the ``hits`` envelope from a proxy or
    an API shape change — RAISES instead of being read as "no matches":
    for a duplicate guard, "cannot verify" must never become "verified
    absent".

    Args:
        credentials: Zenodo credentials.
        intended_title: The exact record title to search for.
        label: Human-readable call-site label for retry log lines.

    Returns:
        The ``(matching_drafts, matching_published)`` tuple from
        :func:`_find_title_matches`.

    Raises:
        HTTPError: 4xx from the search endpoint.
        RequestException: Transport errors after retries.
        ValueError: If the response body lacks the search envelope.
    """
    guard_response = _api_get_with_retry(
        url=f"{credentials.base_url}user/records",
        auth_headers=_auth_headers(credentials),
        label=label,
        params={
            "q": f"metadata.title:{_lucene_phrase(intended_title)}",
            "size": 10,
        },
    )
    guard_body = guard_response.json() if guard_response is not None else None
    hits_envelope = (
        guard_body.get("hits") if isinstance(guard_body, dict) else None
    )
    if not isinstance(hits_envelope, dict) or not isinstance(
        hits_envelope.get("hits"), list
    ):
        raise ValueError(
            f"{label} returned an unrecognized body shape (no hits "
            f"envelope): {str(guard_body)[:200]}"
        )
    return _find_title_matches(hits_envelope["hits"], intended_title)


def _create_draft_with_guarded_retry(
    credentials: Credentials,
    draft_metadata: Dict[str, Any],
    intended_title: str,
) -> Tuple[Dict[str, Any], bool]:
    """Create a draft record, retrying transient failures without
    knowingly creating a duplicate.

    ``POST /records`` is not idempotent: Zenodo can return a 5xx (or the
    connection can drop) AFTER the draft was actually created, so a
    blind retry could mint duplicate records.  Each retry is therefore
    guarded:

    1. Attempt the POST.  A 4xx response is a real client error and is
       raised immediately — retrying cannot help.
    2. On a 5xx or transport error, wait (30 s, then 90 s) and search
       the account's records for ``intended_title`` — after EVERY
       failed attempt, including the last one, so a phantom created by
       the final POST is still adopted rather than lost:

       * exactly one matching DRAFT → the failed-looking POST actually
         created it — fetch and ADOPT it instead of re-creating;
       * a matching PUBLISHED record, or several drafts → raise
         :class:`DuplicateTitleError` (never create another);
       * no match → the POST truly failed; try again while attempts
         remain, else re-raise the creation error.

    3. When a RETRY's POST succeeds, run one more search after a short
       delay (the post-success sweep).  Zenodo's search index is
       eventually consistent, so an earlier attempt's phantom draft may
       have been invisible to the guard in step 2 and only surface now.
       If the sweep reveals any same-title record besides the one just
       created, the just-created draft (still empty — no files, no
       state file) is deleted and the single-stray case is adopted;
       anything else raises :class:`DuplicateTitleError`.  If the sweep
       search itself fails, the run fails closed WITHOUT deleting the
       created draft — the next run's duplicate guard adopts it.

    4. Fail closed everywhere else: if a guard search fails, or
       ``intended_title`` is empty (``--skip-title-guard``), the
       creation error is re-raised instead of retrying blind — a
       duplicate record is permanent while a failed dataset is
       retryable, and the next run's duplicate guard adopts any stray
       draft this run may have left behind.

    Residual risk (inherent to Zenodo's API): a phantom that is still
    unindexed when the post-success sweep runs cannot be detected — the
    public API offers no database-backed draft listing.  The outcome
    degrades safely: the phantom is an empty draft, and the NEXT run's
    duplicate guard sees two same-title drafts and fails closed until
    they are cleaned up (Resources/find_duplicate_records.py).  A
    duplicate can never be published by this code path.

    Args:
        credentials: Zenodo credentials.
        draft_metadata: The full creation payload (access, files,
            metadata, optional parent/custom_fields/pids).
        intended_title: Record title used for the guard searches.  An
            empty string disables retrying entirely (single shot — the
            historical behavior).

    Returns:
        A ``(draft_response, adopted)`` tuple: the created — or
        adopted — draft's API representation, and whether it was
        adopted from an earlier failed-looking attempt rather than
        created by the returning call.

    Raises:
        HTTPError: On a 4xx response (immediately), or when the final
            allowed attempt fails with no phantom found, or when a
            retry cannot be guarded.
        RequestException: Same conditions, for transport-level errors.
        DuplicateTitleError: If a guard search finds a published record
            or multiple drafts carrying ``intended_title``, or the
            post-success sweep cannot rule out a duplicate.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            created = create_draft_record(credentials, draft_metadata)
        except (HTTPError, RequestException) as exc:
            status = getattr(
                getattr(exc, "response", None), "status_code", None
            )
            if status is not None and 400 <= status < 500:
                raise  # client error — a retry would fail identically
            if not intended_title:
                logger.warning(
                    "Draft creation failed and no intended title is "
                    "available to guard a retry (--skip-title-guard?) — "
                    "not retrying, because a blind retry could create a "
                    "duplicate record."
                )
                raise
            backoff = _POST_RETRY_BACKOFF_S[
                min(attempt, len(_POST_RETRY_BACKOFF_S)) - 1
            ]
            logger.warning(
                "Draft creation failed (attempt %d/%d): %s — waiting %ds, "
                "then checking whether the draft was created anyway...",
                attempt, _POST_RETRY_ATTEMPTS, exc, backoff,
            )
            time.sleep(backoff)

            try:
                drafts, published = _search_drafts_by_title(
                    credentials, intended_title,
                    "draft-creation retry guard",
                )
            except Exception as guard_exc:
                logger.error(
                    "Retry guard search failed (%s) — cannot verify that "
                    "the draft was not already created, so NOT retrying. "
                    "Re-run later; the duplicate guard will adopt any "
                    "stray draft.", guard_exc,
                )
                raise exc from guard_exc

            if published:
                ids = ", ".join(str(h.get("id")) for h in published)
                raise DuplicateTitleError(
                    f"A published record titled '{intended_title}' was "
                    f"found while retrying draft creation (ids: {ids}) — "
                    "refusing to create a duplicate."
                ) from exc
            if len(drafts) > 1:
                ids = ", ".join(str(h.get("id")) for h in drafts)
                raise DuplicateTitleError(
                    f"Multiple drafts titled '{intended_title}' were "
                    f"found while retrying draft creation (ids: {ids}) — "
                    "refusing to create another. Clean up the strays "
                    "first (Resources/find_duplicate_records.py)."
                ) from exc
            if drafts:
                adopted_id = str(drafts[0].get("id"))
                logger.warning(
                    "The failed-looking creation attempt DID create "
                    "draft %s — adopting it instead of creating a "
                    "duplicate.", adopted_id,
                )
                full_draft = get_draft_record(credentials, adopted_id)
                if full_draft is None:
                    # The search saw it but /draft 404s — contradictory
                    # state; fail closed rather than re-POST.
                    raise exc
                return full_draft, True
            if attempt >= _POST_RETRY_ATTEMPTS:
                # The guard confirmed no phantom exists for the final
                # attempt either — the failure is real.
                raise
            logger.info(
                "  No record with this title exists — the creation "
                "attempt truly failed; retrying (attempt %d/%d)...",
                attempt + 1, _POST_RETRY_ATTEMPTS,
            )
            continue

        # --- POST succeeded ---
        if attempt == 1:
            return created, False

        # A RETRY succeeded, meaning an earlier attempt looked failed —
        # its phantom draft may exist but have been unindexed when the
        # guard searched.  Give the index a moment, then sweep.
        created_id = str(created.get("id"))
        logger.info(
            "  Draft created on retry — sweeping for a stray duplicate "
            "the earlier failed-looking attempt may have left..."
        )
        time.sleep(_POST_SUCCESS_SWEEP_DELAY_S)
        try:
            drafts, published = _search_drafts_by_title(
                credentials, intended_title, "post-success duplicate sweep",
            )
        except Exception as sweep_exc:
            # Cannot verify: keep the created draft (the next run's
            # duplicate guard will adopt it) but do NOT proceed on a
            # possibly-duplicated title.
            raise DuplicateTitleError(
                f"Draft {created_id} was created, but the duplicate "
                f"sweep for '{intended_title}' failed ({sweep_exc}) — "
                "stopping without uploading. Re-run later; the "
                "duplicate guard will adopt the existing draft."
            ) from sweep_exc

        stray_drafts = [
            h for h in drafts if str(h.get("id")) != created_id
        ]
        if not stray_drafts and not published:
            return created, False

        # A same-title record exists besides the one just created.  The
        # just-created draft is empty (created moments ago, no files,
        # no state file) — delete it so this run never leaves TWO new
        # artifacts, then adopt or fail closed.
        stray_ids = ", ".join(
            str(h.get("id")) for h in stray_drafts + published
        )
        logger.warning(
            "Post-success sweep found same-title record(s) %s besides "
            "the just-created draft %s — deleting the fresh draft.",
            stray_ids, created_id,
        )
        try:
            delete_draft(credentials, created_id)
        except Exception as del_exc:
            raise DuplicateTitleError(
                f"TWO same-title records exist for '{intended_title}' "
                f"(just-created draft {created_id}; pre-existing "
                f"{stray_ids}) and the fresh draft could not be "
                f"deleted ({del_exc}). Clean up manually "
                "(Resources/find_duplicate_records.py) before re-running."
            ) from del_exc
        if published or len(stray_drafts) > 1:
            raise DuplicateTitleError(
                f"Same-title record(s) already exist for "
                f"'{intended_title}' (ids: {stray_ids}); the fresh "
                f"draft {created_id} was deleted. Investigate with "
                "Resources/find_duplicate_records.py."
            )
        adopted_id = str(stray_drafts[0].get("id"))
        logger.warning(
            "Adopting phantom draft %s created by the earlier "
            "failed-looking attempt.", adopted_id,
        )
        full_draft = get_draft_record(credentials, adopted_id)
        if full_draft is None:
            raise DuplicateTitleError(
                f"The sweep saw draft {adopted_id} but it could not be "
                f"fetched; the fresh draft {created_id} was already "
                "deleted. Re-run later — the duplicate guard will "
                "locate the surviving draft."
            )
        return full_draft, True


def upload_file_to_draft(
    credentials: Credentials,
    record_id: str,
    file_path: str,
    upload_attempts: int = _PUT_RETRY_ATTEMPTS,
    known_md5: Optional[str] = None,
) -> Dict[str, Any]:
    """Upload a single file to a draft record (three-step process).

    1. Initialize the file upload slot.
    2. PUT the file content.
    3. Commit the upload.

    Args:
        credentials: Zenodo credentials.
        record_id: Draft record ID.
        file_path: Local path to the file.
        upload_attempts: Total number of PUT attempts allowed for the
            file content (step 2).  Defaults to the module constant
            ``_PUT_RETRY_ATTEMPTS`` (3), preserving the historical
            behavior for any direct importer.  ``1`` means one shot with
            no retry after failure.
        known_md5: Precomputed md5 hex digest of ``file_path`` from an
            earlier integrity pass.  When supplied it is reused for the
            post-commit verification, sparing a second full read of a
            multi-GB file; when None the digest is computed here.

    Returns:
        API response with committed file details.

    Raises:
        FileNotFoundError: If the local file does not exist.
        HTTPError: If any API step fails.
        FileIntegrityError: If the committed file's size or checksum on
            Zenodo does not match the local file.  The broken slot is
            deleted before raising, so the draft stays clean.
    """
    file_path_obj = Path(file_path)
    if not file_path_obj.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    # Local facts captured up front — what Zenodo holds after the commit
    # must match these exactly (post-commit verification below).
    # known_md5 (from the integrity gate's combined digest pass) saves a
    # second full read of a multi-GB ZIP; if the file changed since that
    # hash was taken, the post-commit comparison against Zenodo's actual
    # checksum fails the dataset — fail-closed either way.
    local_size = file_path_obj.stat().st_size
    if known_md5:
        local_md5 = known_md5
    else:
        logger.debug("  Hashing %s (md5) for post-upload verification...",
                     file_path_obj.name)
        local_md5 = _calculate_md5(file_path)

    url = f"{credentials.base_url}records/{record_id}/draft/files"
    auth = _auth_headers(credentials)

    # Step 1: Initialize file upload
    init_data = [{"key": file_path_obj.name}]
    response = requests.post(
        url, json=init_data, headers=auth, timeout=_REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    entries = response.json().get("entries", [])
    if not entries:
        raise ValueError(f"Failed to initialize upload for {file_path_obj.name}")

    # Step 2: Upload file content
    # InvenioRDM returns ALL draft file entries (not just the newly initialized one),
    # so we must find the entry matching our filename by key — never assume entries[0].
    file_entry = next(
        (e for e in entries if e.get("key") == file_path_obj.name), None
    )
    if file_entry is None:
        raise ValueError(
            f"No matching entry for '{file_path_obj.name}' in init response. "
            f"Keys returned: {[e.get('key') for e in entries]}"
        )
    # Wrap the PUT in a try/except so that if all retries are exhausted,
    # we DELETE the pending file slot before propagating the error.  If
    # we left the slot in "pending" state, Zenodo's GET /draft endpoint
    # has been observed to return HTTP 500 for that record afterwards —
    # the partial state breaks the server-side serializer.  Cleaning up
    # the slot keeps the draft in a clean state so the next resume run
    # can re-initialize this file from scratch and the /draft endpoint
    # keeps working.  A cleanup failure must NOT mask the original
    # upload error — log it as a warning and re-raise the PUT error.
    try:
        _put_file_content_with_retry(
            url=file_entry["links"]["content"],
            file_path=file_path,
            auth_headers=auth,
            attempts=upload_attempts,
        )
    except (HTTPError, RequestException) as put_exc:
        logger.info(
            "  Cleaning up pending file slot for %s after exhausted retries...",
            file_path_obj.name,
        )
        try:
            delete_draft_file(credentials, record_id, file_path_obj.name)
            logger.info(
                "  Pending slot cleaned: %s. The draft remains in clean "
                "draft state; upload_state.json still points to it and a "
                "re-run will re-initialize this file fresh.",
                file_path_obj.name,
            )
        except Exception as cleanup_exc:
            logger.warning(
                "  Could not clean up pending file slot %s: %s. "
                "If the next resume's GET /draft returns 500, the "
                "graceful-degradation path will still allow resume "
                "via the file-list endpoint.",
                file_path_obj.name, cleanup_exc,
            )
        raise put_exc

    # Step 3: Commit the file
    commit_response = requests.post(
        file_entry["links"]["commit"], headers=auth,
        timeout=_REQUEST_TIMEOUT,
    )
    commit_response.raise_for_status()
    committed = commit_response.json()

    # Step 4: Post-commit verification — double-check what Zenodo now
    # holds against the local file's size and md5.  A silent transfer
    # corruption or truncation must fail the dataset here, not surface
    # later as a broken download for a researcher.
    mismatch = _remote_entry_mismatch(committed, local_size, local_md5)
    if mismatch is not None:
        logger.error(
            "  Post-upload verification FAILED for %s: %s. "
            "Deleting the corrupt remote copy...",
            file_path_obj.name, mismatch,
        )
        try:
            delete_draft_file(credentials, record_id, file_path_obj.name)
            logger.info("  Corrupt remote copy deleted: %s", file_path_obj.name)
        except Exception as cleanup_exc:
            logger.warning(
                "  Could not delete corrupt remote copy %s: %s — the next "
                "resume run's size/checksum verification will clear it.",
                file_path_obj.name, cleanup_exc,
            )
        raise FileIntegrityError(
            f"Uploaded file {file_path_obj.name} failed verification "
            f"({mismatch})"
        )
    logger.info(
        "  Verified on Zenodo: %s (size and md5 match local file)",
        file_path_obj.name,
    )
    return committed


def _put_file_content_with_retry(
    url: str,
    file_path: str,
    auth_headers: Dict[str, str],
    attempts: int = _PUT_RETRY_ATTEMPTS,
) -> None:
    """PUT file bytes to the draft content URL with retry on transport errors.

    Retries on `RequestException` (covers SSLError, ConnectionError, Timeout,
    ChunkedEncodingError) and on HTTP 5xx responses.  4xx responses fail
    immediately — they indicate a real client/auth/payload problem that won't
    fix itself on retry.

    Each attempt re-opens the file and uploads from byte 0.  Single-PUT
    semantics in InvenioRDM mean the server-side content is overwritten on
    every successful PUT, so retrying is safe.

    Args:
        url: Zenodo draft content URL.
        file_path: Local file to upload.
        auth_headers: Bearer-token headers.
        attempts: Total number of PUT attempts to make.  ``1`` means one
            shot with no retry.  Defaults to the module constant
            ``_PUT_RETRY_ATTEMPTS`` (3), preserving historical behavior
            for direct importers.  Backoffs come from
            ``_PUT_RETRY_BACKOFF_S`` and are consumed only between
            attempts; the last attempt is followed by no wait.

    Raises:
        RequestException or HTTPError: the last error if all attempts fail.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        try:
            with open(file_path, "rb") as fh:
                response = requests.put(
                    url, data=fh, headers=auth_headers,
                    timeout=_REQUEST_TIMEOUT,
                )
            # Treat 5xx as a transient error worth retrying; 4xx is fatal.
            if 500 <= response.status_code < 600:
                raise RequestException(
                    f"Server error HTTP {response.status_code}: "
                    f"{response.text[:200]}"
                )
            response.raise_for_status()
            return
        except HTTPError:
            # 4xx — do not retry.
            raise
        except RequestException as exc:
            last_exc = exc
            file_name = Path(file_path).name
            if attempt < attempts:
                backoff = _PUT_RETRY_BACKOFF_S[attempt - 1]
                logger.warning(
                    "  PUT failed for %s (attempt %d/%d): %s: %s. "
                    "Retrying in %ds...",
                    file_name, attempt, attempts,
                    exc.__class__.__name__, exc, backoff,
                )
                time.sleep(backoff)
            else:
                logger.error(
                    "  PUT failed for %s after %d attempt(s). Last error: %s: %s",
                    file_name, attempts,
                    exc.__class__.__name__, exc,
                )
    assert last_exc is not None
    raise last_exc


def publish_draft(
    credentials: Credentials,
    record_id: str,
) -> Dict[str, Any]:
    """Publish a draft record on Zenodo.

    Args:
        credentials: Zenodo credentials.
        record_id: Draft record ID.

    Returns:
        API response with published record details.

    Raises:
        HTTPError: If the publish request fails.
    """
    url = f"{credentials.base_url}records/{record_id}/draft/actions/publish"
    response = requests.post(
        url, headers=_auth_headers(credentials), timeout=_REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def delete_draft(credentials: Credentials, record_id: str) -> None:
    """Delete a draft record from Zenodo.

    Args:
        credentials: Zenodo credentials.
        record_id: Draft record ID.

    Raises:
        HTTPError: If the delete request fails.
    """
    url = f"{credentials.base_url}records/{record_id}/draft"
    response = requests.delete(
        url, headers=_auth_headers(credentials), timeout=_REQUEST_TIMEOUT,
    )
    response.raise_for_status()


def _api_get_with_retry(
    url: str,
    auth_headers: Dict[str, str],
    label: str,
    allow_404: bool = False,
    params: Optional[Dict[str, Any]] = None,
) -> Optional[requests.Response]:
    """Issue a GET with retry on transient errors.

    Retries on `RequestException` (SSLError, ConnectionError, Timeout) and
    HTTP 5xx.  HTTP 4xx fails immediately — those are real client errors
    that won't fix themselves on retry.

    Args:
        url: full URL to fetch.
        auth_headers: headers dict (bearer token, etc.).
        label: short human-readable label for the call site, used in retry
            warnings so the user can see which API call is flaky.
        allow_404: if True, a 404 returns None instead of raising.  Used
            by `get_draft_record` to distinguish "draft truly gone" from
            other failures.
        params: optional query parameters (requests handles the URL
            encoding — important for search queries containing quotes,
            spaces, or '#').

    Returns:
        The `requests.Response` on 2xx, or None if `allow_404` and the
        server returned 404.

    Raises:
        HTTPError: HTTP 4xx (other than allowed 404).
        RequestException: after all retries are exhausted.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(1, _API_RETRY_ATTEMPTS + 1):
        try:
            response = requests.get(
                url, headers=auth_headers, params=params,
                timeout=_REQUEST_TIMEOUT,
            )
            if allow_404 and response.status_code == 404:
                return None
            if 500 <= response.status_code < 600:
                raise RequestException(
                    f"Server error HTTP {response.status_code}: "
                    f"{response.text[:200]}"
                )
            response.raise_for_status()
            return response
        except HTTPError:
            # 4xx — real client error, retrying won't help.
            raise
        except RequestException as exc:
            last_exc = exc
            if attempt < _API_RETRY_ATTEMPTS:
                backoff = _API_RETRY_BACKOFF_S[attempt - 1]
                logger.warning(
                    "  %s failed (attempt %d/%d): %s: %s. Retrying in %ds...",
                    label, attempt, _API_RETRY_ATTEMPTS,
                    exc.__class__.__name__, exc, backoff,
                )
                time.sleep(backoff)
            else:
                logger.error(
                    "  %s failed after %d attempts. Last error: %s: %s",
                    label, _API_RETRY_ATTEMPTS,
                    exc.__class__.__name__, exc,
                )
    assert last_exc is not None
    raise last_exc


def get_draft_record(
    credentials: Credentials, record_id: str
) -> Optional[Dict[str, Any]]:
    """Fetch an existing draft record by ID.

    Args:
        credentials: Zenodo credentials.
        record_id: Draft record ID.

    Returns:
        The draft record dict on 200, or None on 404 (draft truly gone).

    Raises:
        HTTPError: 4xx other than 404.
        RequestException: 5xx or transport error after all API retries exhausted.
            Caller can catch this and choose to proceed without metadata
            (e.g., if `/draft` is broken but the draft itself is still
            usable via `/draft/files`).
    """
    url = f"{credentials.base_url}records/{record_id}/draft"
    response = _api_get_with_retry(
        url=url,
        auth_headers=_auth_headers(credentials),
        label=f"GET draft {record_id}",
        allow_404=True,
    )
    if response is None:
        return None
    return response.json()


def list_draft_files(
    credentials: Credentials, record_id: str
) -> List[Dict[str, Any]]:
    """List file entries on an existing draft record, with retry on 5xx.

    Each entry contains at least ``key`` (filename), ``status`` ("pending"
    or "completed"), ``size``, and ``links``.

    Args:
        credentials: Zenodo credentials.
        record_id: Draft record ID.

    Returns:
        List of file entry dicts (an empty list when the draft has no
        files).

    Raises:
        HTTPError: 4xx.
        RequestException: after all API retries exhausted.
    """
    url = f"{credentials.base_url}records/{record_id}/draft/files"
    response = _api_get_with_retry(
        url=url,
        auth_headers=_auth_headers(credentials),
        label=f"GET draft files {record_id}",
    )
    assert response is not None  # allow_404=False by default → never None
    return response.json().get("entries", []) or []


def delete_draft_file(
    credentials: Credentials, record_id: str, key: str
) -> None:
    """Delete a single file entry from a draft (used to clear a pending slot).

    A 404 is treated as success — the slot is already gone.

    Args:
        credentials: Zenodo credentials.
        record_id: Draft record ID.
        key: Filename (entry key) of the file to delete.

    Raises:
        HTTPError: If the delete request fails with a status other than 404.
    """
    url = f"{credentials.base_url}records/{record_id}/draft/files/{key}"
    response = requests.delete(
        url, headers=_auth_headers(credentials), timeout=_REQUEST_TIMEOUT,
    )
    # 404 is fine — the slot is already gone.
    if response.status_code != 404:
        response.raise_for_status()


class DuplicateTitleError(Exception):
    """Raised by the duplicate guard when creating a draft would duplicate
    an existing record with the same title.

    The unified error handler in ``upload_to_zenodo`` converts this into a
    normal failure result, so the dataset is marked failed (and stays in
    Staging_Area/ for human review) instead of minting a duplicate record.
    """


def _normalize_title(title: str) -> str:
    """Whitespace-collapsed, case-folded title for exact comparison.

    Args:
        title: Raw title string (any surrounding or inner whitespace, any
            case).

    Returns:
        The title with runs of whitespace collapsed to single spaces and
        case-folded, suitable for exact-equality comparison.
    """
    return " ".join(str(title).split()).casefold()


def _find_title_matches(
    hits: List[Dict[str, Any]], title: str
) -> "tuple[List[Dict[str, Any]], List[Dict[str, Any]]]":
    """Split search hits into (matching_drafts, matching_published).

    A hit "matches" when its normalized title equals the normalized target
    title — the search API is only a candidate fetch (its matching is
    fuzzy); this exact comparison is the real gate.

    Handles both Zenodo serializations (same dual-shape mapping proven in
    Resources/find_duplicate_records.py): published-ness comes from
    ``is_published`` when present, else ``status == "published"``.

    Args:
        hits: Candidate record dicts returned by the Zenodo search API.
        title: The intended title to match against (normalized internally).

    Returns:
        A ``(matching_drafts, matching_published)`` tuple: the hits whose
        title matches exactly, partitioned into unpublished drafts and
        already-published records.
    """
    target = _normalize_title(title)
    drafts: List[Dict[str, Any]] = []
    published: List[Dict[str, Any]] = []
    for hit in hits:
        hit_title = (
            (hit.get("metadata") or {}).get("title", "")
            or hit.get("title", "")
        )
        if _normalize_title(hit_title) != target:
            continue
        status = str(hit.get("status", ""))
        if bool(hit.get("is_published", status == "published")):
            published.append(hit)
        else:
            drafts.append(hit)
    return drafts, published


def _draft_doi(draft_response: Optional[Dict[str, Any]]) -> Optional[str]:
    """Extract the DOI identifier from a draft record dict, if one exists.

    Args:
        draft_response: A draft record dict (as returned by Zenodo), or
            None.

    Returns:
        The DOI string (e.g. ``"10.5281/zenodo.1234567"``), or None if the
        draft is missing or has no DOI reserved/assigned yet.
    """
    if not draft_response:
        return None
    doi = (draft_response.get("pids") or {}).get("doi") or {}
    identifier = (doi.get("identifier") or "").strip()
    return identifier or None


def ensure_doi_reserved(
    credentials: Credentials,
    record_id: str,
    draft_response: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Make sure a draft has a DOI, reserving one if it does not.

    Idempotent: safe to call at any point in a draft's life, any number
    of times.  Checks the draft's ``pids.doi`` first (re-fetching the
    draft metadata if the caller didn't supply it or it looks stale) and
    only calls the reserve endpoint when no DOI exists yet.

    The reserve call is the official InvenioRDM endpoint — the same one
    Zenodo's "Get a DOI now" button uses:

        POST /api/records/{id}/draft/pids/doi

    Args:
        credentials: Zenodo API credentials.
        record_id: The draft record ID.
        draft_response: The draft metadata dict if the caller already has
            it (saves one GET).  Pass None to force a fresh fetch.

    Reservation is BEST-EFFORT: a transient 5xx (e.g. DataCite degraded)
    is retried a few times, and a persistent failure is logged as a
    WARNING and swallowed — the function returns None rather than
    raising.  Reserving a DOI early is a convenience; Zenodo mints one
    automatically at publish, so a DataCite outage must not fail the
    record or block its upload.

    Args:
        credentials: Zenodo API credentials.
        record_id: The draft record ID.
        draft_response: The draft metadata dict if the caller already has
            it (saves one GET).  Pass None to force a fresh fetch.

    Returns:
        The DOI string (existing or newly reserved), or None when no DOI
        could be confirmed — either because the reserve call reported
        "already exists" (HTTP 400, a DOI is present but not read back)
        or because reservation could not be completed (persistent error;
        the draft keeps going and gets its DOI at publish).

    Raises:
        Nothing for reservation failures — this is best-effort.  The
        ``RequestException`` raised internally on a 5xx is caught by the
        retry loop; a persistent 5xx or a non-400 client error is logged
        and swallowed (returns None), so a DataCite outage never fails
        the record or its upload.
    """
    doi = _draft_doi(draft_response)
    if doi is None:
        # No DOI in the supplied metadata (or none supplied) — re-fetch to
        # be sure before issuing a reserve call.  A broken /draft endpoint
        # (seen with corrupted pending-slot drafts) raises here; in that
        # case fall through and attempt the reservation anyway, tolerating
        # the "already exists" response below.
        try:
            fresh = get_draft_record(credentials, record_id)
            doi = _draft_doi(fresh)
        except (HTTPError, RequestException) as exc:
            logger.warning(
                "Could not fetch draft %s to check DOI state (%s) — "
                "attempting reservation anyway.",
                record_id, exc,
            )

    if doi:
        logger.info("  DOI already assigned: %s", doi)
        return doi

    logger.info("  No DOI on draft %s — reserving one...", record_id)
    url = f"{credentials.base_url}records/{record_id}/draft/pids/doi"
    last_exc: Optional[BaseException] = None
    for attempt in range(1, _DOI_RESERVE_ATTEMPTS + 1):
        try:
            response = requests.post(
                url, headers=_auth_headers(credentials),
                timeout=_REQUEST_TIMEOUT,
            )
            if response.status_code == 400:
                # InvenioRDM answers 400 when a DOI is already present —
                # treat as success (the goal state is "draft has a DOI").
                # Log Zenodo's message so a genuinely different 400 shows.
                logger.info(
                    "  Reserve endpoint returned 400 for draft %s — a DOI "
                    "most likely already exists. Zenodo said: %s",
                    record_id, response.text[:300],
                )
                return None
            if 500 <= response.status_code < 600:
                # Transient server-side failure (DataCite degraded, etc.).
                raise RequestException(
                    f"Server error HTTP {response.status_code}: "
                    f"{response.text[:200]}"
                )
            response.raise_for_status()
            reserved = _draft_doi(response.json())
            logger.info(
                "  DOI reserved: %s",
                reserved or "(reserved, id not returned)",
            )
            return reserved
        except HTTPError:
            # 4xx other than the 400 handled above — a real client error
            # that a retry cannot fix.  Still best-effort: warn, no raise.
            logger.warning(
                "  DOI reservation for draft %s failed with a client "
                "error — continuing without an early DOI (Zenodo will "
                "mint one at publish).", record_id, exc_info=True,
            )
            return None
        except RequestException as exc:
            last_exc = exc
            if attempt < _DOI_RESERVE_ATTEMPTS:
                backoff = _DOI_RESERVE_BACKOFF_S[attempt - 1]
                logger.warning(
                    "  DOI reservation for draft %s failed (attempt "
                    "%d/%d): %s. Retrying in %ds...",
                    record_id, attempt, _DOI_RESERVE_ATTEMPTS, exc, backoff,
                )
                time.sleep(backoff)
    logger.warning(
        "  DOI reservation for draft %s did not complete after %d "
        "attempt(s) (last error: %s) — continuing WITHOUT an early DOI. "
        "The record and its upload are unaffected; Zenodo mints a DOI at "
        "publish. (A DataCite outage is the usual cause.)",
        record_id, _DOI_RESERVE_ATTEMPTS, last_exc,
    )
    return None


def _create_community_review_request(
    credentials: Credentials,
    record_id: str,
    community_id: str,
) -> Dict[str, Any]:
    """Create a community review request on a draft record.

    InvenioRDM requires an explicit review request object to be created
    before ``submit-review`` can be called.  Including
    ``parent.communities.ids`` in the draft creation POST only *associates*
    the community — it does NOT create the review request object.

    This is step 2 of the 3-step community submission flow:
        1. Create draft with ``parent.communities.ids``
        2. **POST /draft/review** ← this function
        3. POST /draft/actions/submit-review

    Args:
        credentials: Zenodo credentials.
        record_id: Draft record ID.
        community_id: Zenodo community UUID or slug.

    Returns:
        API response from the review creation endpoint.

    Raises:
        HTTPError: If the review request creation fails.
    """
    url = f"{credentials.base_url}records/{record_id}/draft/review"
    payload = {
        "receiver": {"community": community_id},
        "type": "community-submission",
    }
    response = requests.put(
        url,
        json=payload,
        headers=_auth_headers(credentials, content_type="application/json"),
        timeout=_REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def submit_to_community_review(
    credentials: Credentials,
    record_id: str,
    community_id: str,
) -> Dict[str, Any]:
    """Create and submit a draft record to the community review queue.

    InvenioRDM community submission requires two steps after all files
    are uploaded:

    1. **Create review request** — ``PUT /draft/review`` with the community
       ID establishes the review object that links this draft to the
       community's queue.
    2. **Submit review** — ``POST /draft/actions/submit-review`` moves the
       draft into the queue so a community manager can accept or decline it.

    This function performs both steps in sequence.  It is only called when
    ``community_id`` is set in ``project_config.json``; non-community
    uploads skip it entirely.

    Args:
        credentials: Zenodo credentials.
        record_id: Draft record ID (returned by ``create_draft_record``).
        community_id: Zenodo community UUID or slug from project config.

    Returns:
        API response dictionary from the final submit-review step.

    Raises:
        HTTPError: If either API step fails.
    """
    # Step 1: Create the review request object linking draft → community
    logger.info("  Creating community review request...")
    _create_community_review_request(credentials, record_id, community_id)

    # Step 2: Submit the draft into the community review queue
    logger.info("  Submitting to community review queue...")
    url = f"{credentials.base_url}records/{record_id}/draft/actions/submit-review"
    response = requests.post(
        url,
        headers=_auth_headers(credentials, content_type="application/json"),
        timeout=_REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


# ===================================================================
#  High-level upload orchestration
# ===================================================================

def _cleanup_failed_draft(
    credentials: Credentials,
    record_id: Optional[str],
    delete_on_failure: bool,
) -> None:
    """Attempt to delete a failed draft record.

    Args:
        credentials: Zenodo credentials.
        record_id: Draft record ID (may be None if creation failed).
        delete_on_failure: Whether cleanup was requested.
    """
    if not delete_on_failure or not record_id:
        return

    try:
        logger.info("Deleting failed draft %s...", record_id)
        delete_draft(credentials, record_id)
        logger.info("Draft deleted")
    except Exception as del_exc:
        logger.warning("Failed to delete draft: %s", del_exc)


def upload_to_zenodo(
    files: List[str],
    config: Any,
    delete_on_failure: bool = False,
    auto_publish: bool = False,
    request_log_path: Optional[str] = None,
    existing_draft_id: Optional[str] = None,
    state_file_path: Optional[str] = None,
    submit_review: bool = True,
    upload_attempts: int = _PUT_RETRY_ATTEMPTS,
    title_guard: bool = True,
    abort_event: Optional["threading.Event"] = None,
    known_md5s: Optional[Dict[str, str]] = None,
    zip_filename: Optional[str] = None,
) -> Dict[str, Any]:
    """Upload files to Zenodo and optionally publish the record.

    This is the main entry point for uploading a dataset.  It creates
    a draft record, uploads all files, and optionally publishes.

    Args:
        files: List of local file paths to upload.
        config: DraftConfig model with record metadata.
        delete_on_failure: Delete the draft if upload fails.
        auto_publish: Publish the record after successful upload.
        request_log_path: Optional path to write the draft metadata payload as
            JSON for debugging.  If None, no log is written.
        existing_draft_id: If provided, resume uploading to this existing draft
            instead of creating a new one.  Files already committed on the draft
            are skipped; "pending" slots are deleted and re-uploaded.  If the
            given draft no longer exists on Zenodo (404), falls back to creating
            a new draft.
        state_file_path: If provided, a small JSON file is written here (after
            the draft is created or located) with the record id and Zenodo URL.
            Used by the orchestrator to enable automatic resume on re-run.
        submit_review: If False, skip the community-review submission even
            when ``config.community_id`` is set.  Used by the ``--defer-zip``
            workflow: the record must NOT enter the community review queue
            until every file (including the deferred ZIP) is uploaded,
            because a community manager accepting the record publishes it —
            and published records cannot accept new files.
        upload_attempts: Total number of PUT attempts for the data ZIP
            (step 2 of the per-file upload).  Defaults to
            ``_PUT_RETRY_ATTEMPTS`` (3); ``1`` means one shot.  The
            ``--upload-attempts`` CLI flag on ``standalone_tasks.py``
            surfaces this to end users.  When ``zip_filename`` is given,
            this setting applies ONLY to that file — every other
            (small) file always gets the default ``_PUT_RETRY_ATTEMPTS``
            attempts.  When ``zip_filename`` is None, it applies to all
            files (the historical behavior, kept for direct callers).
        title_guard: When True (default) and no ``existing_draft_id`` was
            supplied, search the account's records for the intended title
            BEFORE creating a fresh draft.  A matching unpublished draft is
            adopted (resumed) instead of duplicated; a matching published
            record raises :class:`DuplicateTitleError` (dataset marked
            failed, nothing created).  This is the last line of defense
            against duplicate records when a folder's ``upload_state.json``
            link has been lost.  Disable per-run with ``--skip-title-guard``
            on ``standalone_tasks.py``.
        abort_event: Optional cooperative-cancellation flag checked at each
            file boundary.  When set, the upload stops before the next file
            and returns an ``AbortedByUser`` failure result; the draft and
            its ``upload_state.json`` stay resumable.  None disables the
            check.
        known_md5s: Optional mapping of filename to precomputed md5 hex
            digest.  Digests found here are reused for per-file upload
            verification and for resume-time checks of already-committed
            files, sparing a second full read of large files.  Missing
            entries are hashed on demand.
        zip_filename: Basename of the dataset's data ZIP within
            ``files``.  Scopes ``upload_attempts`` to that one file (see
            above).  None applies ``upload_attempts`` to every file.

    Returns:
        Dictionary with keys:
            'successful' (bool), 'api_response' (dict|None), 'error' (dict|None).

    Raises:
        ValueError: If Zenodo credentials are missing or still contain
            placeholders — propagated from ``get_credentials_from_env``,
            which runs before the internal error handling begins.

    Note:
        The known failure families — HTTP/transport errors, the
        duplicate-title guard (:class:`DuplicateTitleError`) and the
        integrity guard (:class:`FileIntegrityError`), missing local
        files, and malformed API responses — are caught internally and
        surfaced in the ``error`` field of the returned dict rather than
        raised.  Only unexpected programming errors propagate.
    """
    credentials = get_credentials_from_env()
    record_id: Optional[str] = None
    draft_response: Optional[Dict[str, Any]] = None
    is_resume = False

    try:
        # --- Duplicate guard -------------------------------------------
        # Runs ONLY when there is no local resume pointer — exactly the
        # dangerous situation: if a record with this title already exists
        # on Zenodo (its upload_state.json link was lost), creating a
        # fresh draft would mint a duplicate.  Search the account's own
        # records (drafts included) for the intended title first.
        #
        # Fail-closed on search errors: this search hits the same API as
        # draft creation, and a missed guard creates a PERMANENT duplicate
        # while a failed run is retryable.
        if title_guard and not existing_draft_id:
            intended_title = (config.metadata or {}).get("title", "")
            if intended_title:
                logger.info(
                    "Duplicate guard: checking account for existing "
                    "records titled %r...", intended_title,
                )
                # Shared hardened search: Lucene-escapes the title and
                # raises on an unrecognized response body instead of
                # reading it as "no matches" (fail closed).
                matching_drafts, matching_published = (
                    _search_drafts_by_title(
                        credentials, intended_title,
                        "duplicate-guard title search",
                    )
                )
                if matching_published:
                    ids = ", ".join(
                        f"id {h.get('id')} (doi {((h.get('pids') or {}).get('doi') or {}).get('identifier') or h.get('doi') or '?'})"
                        for h in matching_published
                    )
                    raise DuplicateTitleError(
                        f"A record titled '{intended_title}' already "
                        f"exists on Zenodo: {ids} — refusing to create a "
                        "duplicate. Investigate with "
                        "Resources/find_duplicate_records.py; use "
                        "--skip-title-guard only if a same-title record "
                        "is truly intended."
                    )
                if len(matching_drafts) > 1:
                    ids = ", ".join(str(h.get("id")) for h in matching_drafts)
                    raise DuplicateTitleError(
                        f"Multiple existing drafts titled "
                        f"'{intended_title}' found (ids: {ids}) — refusing "
                        "to create another. Clean up the strays first "
                        "(see Resources/find_duplicate_records.py)."
                    )
                if matching_drafts:
                    adopted_id = str(matching_drafts[0].get("id"))
                    logger.warning(
                        "DUPLICATE GUARD: found existing draft %s with "
                        "this title — resuming it instead of creating a "
                        "new record.", adopted_id,
                    )
                    existing_draft_id = adopted_id
                else:
                    logger.info("  No existing record with this title — OK.")

        # --- Locate or create the draft record ---
        if existing_draft_id:
            logger.info("Resume requested for draft %s", existing_draft_id)

            # Try to fetch the full draft metadata.  Three possible outcomes:
            #   1. Returns dict       → draft exists, normal resume path.
            #   2. Returns None       → HTTP 404, draft is truly gone.
            #                           Fall back to creating a fresh draft.
            #   3. Raises (4xx / 5xx after retries) → /draft endpoint is
            #                           broken for this draft but the draft
            #                           itself may still be intact (we've
            #                           seen this happen when a draft has
            #                           a "pending" file slot from a prior
            #                           failed upload — partial state crashes
            #                           the server-side serializer).  Skip
            #                           the metadata and proceed via the
            #                           file-list endpoint, which uses a
            #                           different code path on Zenodo's
            #                           side and is usually still healthy.
            draft_response = None
            metadata_unavailable = False
            try:
                draft_response = get_draft_record(credentials, existing_draft_id)
            except (HTTPError, RequestException) as exc:
                logger.warning(
                    "Could not fetch metadata for draft %s (%s: %s). "
                    "Proceeding with resume — the file-list endpoint and "
                    "uploads use different Zenodo handlers that may still work.",
                    existing_draft_id, exc.__class__.__name__, exc,
                )
                metadata_unavailable = True

            if draft_response is None and not metadata_unavailable:
                # Case 2 — true 404, draft is gone.  Fall through to fresh
                # draft creation (is_resume stays False).
                logger.warning(
                    "Draft %s no longer exists on Zenodo. "
                    "Falling back to fresh draft creation.",
                    existing_draft_id,
                )
            else:
                # Case 1 or Case 3 — proceed with resume against the
                # existing draft.  The downstream code that touches
                # `draft_response` already uses defensive None-checks
                # (`draft_response and draft_response.get(...)`), so the
                # "no metadata" path is naturally handled: the already-
                # submitted / already-published guards default to False,
                # which for a stuck upload is the correct assumption
                # (those steps come AFTER file uploads).
                record_id = str(existing_draft_id)
                is_resume = True
                if draft_response:
                    logger.info(
                        "  Resuming draft %s "
                        "(status=%s state=%s is_published=%s has_review=%s)",
                        record_id,
                        draft_response.get("status"),
                        draft_response.get("state"),
                        draft_response.get("is_published"),
                        bool(draft_response.get("parent", {}).get("review")),
                    )
                else:
                    logger.info(
                        "  Resuming draft %s (metadata unavailable; "
                        "relying on /draft/files for resume decisions)",
                        record_id,
                    )

        if not is_resume:
            logger.info("Creating draft record...")

            # Handle access values — may be enums or plain strings
            record_access = (
                config.record_access.value
                if hasattr(config.record_access, "value")
                else config.record_access
            )
            files_access = (
                config.files_access.value
                if hasattr(config.files_access, "value")
                else config.files_access
            )

            draft_metadata: Dict[str, Any] = {
                "access": {"record": record_access, "files": files_access},
                "files": {"enabled": config.files_enabled},
                "metadata": config.metadata,
            }

            if config.community_id:
                draft_metadata["parent"] = {
                    "communities": {"ids": [config.community_id]}
                }
            if config.custom_fields:
                draft_metadata["custom_fields"] = config.custom_fields
            # NOTE: DOI reservation (pids) is deliberately NOT sent in the
            # creation body.  Asking Zenodo to reserve a DataCite DOI
            # inside POST /records makes record creation depend on the
            # external DataCite service — when DataCite is degraded, the
            # whole creation returns HTTP 500 and no draft is created.
            # (An earlier "always send pids at creation" change caused
            # exactly that: repeatable, all-day 500s.)  Reservation now
            # happens only on the dedicated create-then-reserve endpoint
            # via ensure_doi_reserved() below, which is best-effort — a
            # DataCite outage no longer blocks the draft or its upload;
            # Zenodo mints the DOI at publish if it was not reserved
            # early.  config.pids stays the "reserve requested" signal
            # that gates that dedicated call.

            # Guarded retry: transient 5xx/transport failures are retried,
            # but never blindly — each retry first checks whether the
            # failed-looking POST actually created the draft and adopts
            # it if so.  Without a title to guard with (--skip-title-
            # guard), creation stays single-shot.
            intended_title = (
                (config.metadata or {}).get("title", "") if title_guard
                else ""
            )
            # Small pause before the first creation POST (belt-and-
            # suspenders, requested by the operator).  The evidence shows
            # timing between the duplicate-guard search and this POST is
            # NOT the cause of the 500s — the guarded retry's 30/90s
            # backoffs space out later attempts — but a brief settle here
            # is harmless.
            time.sleep(_PRE_CREATE_PAUSE_S)
            draft_response, adopted_draft = _create_draft_with_guarded_retry(
                credentials, draft_metadata, intended_title
            )
            if adopted_draft:
                # An adopted draft may pre-date this run and carry file
                # entries — route it through the resume-time file
                # listing/verification pass (same as an adoption by the
                # duplicate guard above).
                is_resume = True
            record_id = draft_response.get("id")

            if not record_id:
                raise ValueError("No record ID returned from draft creation")
            # Normalize to str — the resume path stores str(existing_id),
            # and record_id is interpolated into URLs and state files.
            record_id = str(record_id)

            # Persist the outgoing request payload for debugging / audit
            if request_log_path:
                try:
                    import json as _json
                    log_entry = {
                        "record_id": record_id,
                        "request": {"body": draft_metadata},
                        "response": draft_response,
                    }
                    Path(request_log_path).write_text(
                        _json.dumps(log_entry, indent=2), encoding="utf-8"
                    )
                    logger.info(
                        "  Request log saved: %s", Path(request_log_path).name
                    )
                except Exception as log_exc:
                    logger.warning("Could not save request log: %s", log_exc)

            logger.info(
                "Draft %s with ID: %s",
                "adopted" if adopted_draft else "created", record_id,
            )

        # --- Write resume-state file (idempotent — safe to overwrite) ---
        if state_file_path and record_id:
            try:
                import json as _json
                from datetime import datetime as _dt
                state = {
                    "record_id": str(record_id),
                    "created_at": _dt.now().isoformat(timespec="seconds"),
                    "zenodo_url": f"https://zenodo.org/uploads/{record_id}",
                    "resumed": is_resume,
                    # Attempt counter: starts at 0 conceptually; every run
                    # that gets far enough to upload against this record
                    # (fresh create, resume, defer-zip phase 1, recovery
                    # via finish_stuck_uploads) counts as one attempt.
                    # A pre-existing state file WITHOUT the field (written
                    # by an older AZUS) is treated as 0 and advanced —
                    # the field is created on the next attempt.
                    "number_of_tries": _read_number_of_tries(
                        Path(state_file_path)
                    ) + 1,
                }
                Path(state_file_path).write_text(
                    _json.dumps(state, indent=2), encoding="utf-8"
                )
                logger.info(
                    "  Wrote upload state file (attempt #%d): %s",
                    state["number_of_tries"], state_file_path,
                )
            except Exception as state_exc:
                logger.warning(
                    "Could not write upload state file %s: %s",
                    state_file_path, state_exc,
                )

        # --- Reserve the DOI early when requested (reserve_doi config) ---
        # Runs for fresh drafts AND resumes, so a --defer-zip phase-1 run
        # yields its DOI immediately.  Idempotent: no-op when the draft
        # already has one.  A second unconditional check runs right before
        # community-review submission below.
        if getattr(config, "pids", None) and record_id:
            ensure_doi_reserved(credentials, record_id, draft_response)

        # --- Determine which files to skip / clear on resume ---
        skip_keys: set = set()
        if is_resume:
            existing_entries = list_draft_files(credentials, record_id)
            logger.info(
                "  Draft has %d existing file entr%s:",
                len(existing_entries),
                "y" if len(existing_entries) == 1 else "ies",
            )
            for entry in existing_entries:
                logger.info(
                    "    - %s (status=%s, size=%s, checksum=%s)",
                    entry.get("key"),
                    entry.get("status"),
                    entry.get("size"),
                    entry.get("checksum"),
                )

            existing_by_key = {e.get("key"): e for e in existing_entries}
            local_by_name = {Path(p).name: p for p in files}
            for key, entry in existing_by_key.items():
                if key not in local_by_name:
                    logger.info(
                        "    (not in upload list — leaving as-is: %s)", key
                    )
                    continue
                status = (entry.get("status") or "").lower()
                # Treat any status that looks "finalized" as already uploaded.
                # InvenioRDM's canonical value is "completed"; we log the
                # raw value so a future status change is easy to spot.
                if status == "completed":
                    # A committed file is only skipped after it VERIFIES
                    # against the local file — size first (cheap), then
                    # md5 when Zenodo provides one.  Skipping by name
                    # alone let short ZIPs from interrupted runs stay on
                    # records forever, even after the local ZIP was fixed.
                    local_path = local_by_name[key]
                    try:
                        local_size = Path(local_path).stat().st_size
                        mismatch = _remote_entry_mismatch(entry, local_size)
                        if (
                            mismatch is None
                            and (entry.get("checksum") or "").startswith("md5:")
                        ):
                            logger.info(
                                "  Verifying committed file %s (md5)...", key
                            )
                            local_md5 = (
                                (known_md5s or {}).get(key)
                                or _calculate_md5(local_path)
                            )
                            mismatch = _remote_entry_mismatch(
                                entry, local_size, local_md5
                            )
                    except OSError as exc:
                        # Cannot read the local file — fail the dataset
                        # rather than guess.  Deleting the remote copy
                        # here could destroy the only good copy.
                        raise FileIntegrityError(
                            f"Cannot verify committed file {key} against "
                            f"local copy ({exc}) — refusing to continue."
                        )
                    if mismatch is None:
                        skip_keys.add(key)
                        logger.info(
                            "  Already uploaded and VERIFIED, skipping: %s "
                            "(status=%s)", key, status,
                        )
                    else:
                        logger.warning(
                            "  Committed file on Zenodo does NOT match the "
                            "local file (%s) — deleting the remote copy of "
                            "%s and re-uploading.", mismatch, key,
                        )
                        delete_draft_file(credentials, record_id, key)
                else:
                    logger.info(
                        "  Clearing existing slot for re-upload: %s "
                        "(status=%s, size=%s)",
                        key, status or "?", entry.get("size"),
                    )
                    delete_draft_file(credentials, record_id, key)

        # --- Upload files ---
        to_upload = [p for p in files if Path(p).name not in skip_keys]
        logger.info(
            "Uploading %d file(s)%s...",
            len(to_upload),
            f" ({len(skip_keys)} already committed)" if skip_keys else "",
        )
        for i, file_path in enumerate(to_upload, 1):
            # File-boundary abort check: a Ctrl+C on a concurrent run
            # must not wait for hours of remaining files.  The draft and
            # its upload_state.json already exist, so stopping here
            # leaves a normal resumable "stuck" upload.
            if abort_event is not None and abort_event.is_set():
                logger.warning(
                    "Run aborted by user — stopping before %s. Draft %s "
                    "remains resumable (finish_stuck_uploads.py).",
                    Path(file_path).name, record_id,
                )
                return {
                    "successful": False,
                    "api_response": None,
                    "error": {
                        "type": "AbortedByUser",
                        "error_message": (
                            "Run interrupted (Ctrl+C) — upload stopped at "
                            "a file boundary; re-run to resume this draft."
                        ),
                    },
                }
            file_name = Path(file_path).name
            file_size_mb = Path(file_path).stat().st_size / (1024 * 1024)

            logger.info(
                "  [%d/%d] Uploading %s (%.2f MB)...",
                i, len(to_upload), file_name, file_size_mb,
            )

            # --upload-attempts tunes ONLY the data ZIP (the multi-GB
            # transfer where retry cost matters); companion files keep
            # the default.  A None zip_filename (direct caller) keeps
            # the historical apply-to-all behavior.
            attempts_for_file = (
                upload_attempts
                if zip_filename is None or file_name == zip_filename
                else _PUT_RETRY_ATTEMPTS
            )

            start_time = time.time()
            upload_file_to_draft(
                credentials, record_id, file_path,
                upload_attempts=attempts_for_file,
                known_md5=(known_md5s or {}).get(file_name),
            )
            elapsed = time.time() - start_time

            logger.info("  Uploaded in %.1fs", elapsed)

        logger.info("All files uploaded successfully")

        # --- Submit to community review queue (skip if already submitted) ---
        # Including parent.communities.ids at draft creation only *associates*
        # the community; this second call moves the draft into the community's
        # review queue so a manager can accept it.  On resume, a review request
        # may already exist — calling submit-review twice 4xx's, so detect and
        # skip when parent.review is already populated.
        review_response = None
        already_in_review = bool(
            draft_response and draft_response.get("parent", {}).get("review")
        )
        if config.community_id and not already_in_review and submit_review:
            # Best-effort DOI reservation before the review queue — this
            # is the last moment to reserve one early (acceptance from the
            # queue publishes the record).  Unconditional (not gated on
            # reserve_doi) and idempotent — a no-op when the DOI already
            # exists.  If reservation cannot complete (e.g. DataCite is
            # down) it warns and returns None rather than raising: the
            # record still enters review and Zenodo mints the DOI at
            # publish, so a DOI-service outage never blocks the dataset.
            ensure_doi_reserved(credentials, record_id, draft_response)

            logger.info("Submitting draft to community review queue...")
            review_response = submit_to_community_review(
                credentials, record_id, config.community_id
            )
            logger.info(
                "  Submitted to community review — status: %s",
                review_response.get("status", "unknown"),
            )
        elif config.community_id and already_in_review:
            logger.info("Community review already submitted — skipping resubmit")
        elif config.community_id and not submit_review:
            logger.info(
                "Community review submission DEFERRED — will be submitted "
                "on the run that uploads the remaining file(s)."
            )

        # --- Publish if requested (skip if already published) ---
        already_published = bool(
            draft_response and draft_response.get("is_published", False)
        )
        if auto_publish and not already_published:
            logger.info("Publishing record...")
            publish_response = publish_draft(credentials, record_id)
            logger.info("Record published")
            return {
                "successful": True,
                "api_response": publish_response,
                "error": None,
            }
        if auto_publish and already_published:
            logger.info("Record already published — skipping publish step")

        logger.info("Record created as draft (not published)")
        return {
            "successful": True,
            # Return the review response when available — it contains richer
            # community state info than the original draft creation response.
            "api_response": review_response if review_response else draft_response,
            "error": None,
        }

    except (
        HTTPError,
        RequestException,
        DuplicateTitleError,
        FileIntegrityError,
        FileNotFoundError,
        ValueError,
    ) as exc:
        # Unified error handling for the KNOWN failure families: HTTP /
        # transport errors, the duplicate-title and integrity guards, and
        # bad local files or API response shapes.  Anything OUTSIDE these
        # is a programming error and must propagate loudly (with a
        # traceback) instead of masquerading as "upload failed" in the
        # failure CSV — that masking hid real defects in the past.
        if isinstance(exc, HTTPError):
            try:
                error_details = exc.response.json()
                error_msg = f"HTTP {exc.response.status_code}: {error_details}"
            except Exception:
                error_msg = f"HTTP error: {exc}"
            error_type = "HTTPError"
        elif isinstance(exc, RequestException):
            error_msg = f"Connection error: {exc}"
            error_type = "RequestException"
        else:
            error_msg = str(exc)
            error_type = type(exc).__name__

        logger.error("Upload failed: %s", error_msg)

        _cleanup_failed_draft(credentials, record_id, delete_on_failure)

        return {
            "successful": False,
            "api_response": None,
            "error": {"type": error_type, "error_message": error_msg},
        }
