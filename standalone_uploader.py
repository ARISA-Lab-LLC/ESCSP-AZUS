"""Zenodo API client for creating records and uploading files.

This module handles direct HTTP communication with the Zenodo/InvenioRDM
API.  It has no dependency on Prefect or any orchestration framework.

All functions are synchronous.  For future Prefect integration, wrap calls
in ``@task``-decorated functions.

Environment variables required:
    INVENIO_RDM_ACCESS_TOKEN: Zenodo API bearer token.
    INVENIO_RDM_BASE_URL: Zenodo API base URL (e.g., https://zenodo.org/api/).
"""

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

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

    if not token or token == "ZENODO_ACESS_TOKEN":
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
    )
    response.raise_for_status()
    return response.json()


def upload_file_to_draft(
    credentials: Credentials,
    record_id: str,
    file_path: str,
) -> Dict[str, Any]:
    """Upload a single file to a draft record (three-step process).

    1. Initialize the file upload slot.
    2. PUT the file content.
    3. Commit the upload.

    Args:
        credentials: Zenodo credentials.
        record_id: Draft record ID.
        file_path: Local path to the file.

    Returns:
        API response with committed file details.

    Raises:
        FileNotFoundError: If the local file does not exist.
        HTTPError: If any API step fails.
    """
    file_path_obj = Path(file_path)
    if not file_path_obj.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    url = f"{credentials.base_url}records/{record_id}/draft/files"
    auth = _auth_headers(credentials)

    # Step 1: Initialize file upload
    init_data = [{"key": file_path_obj.name}]
    response = requests.post(url, json=init_data, headers=auth)
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
    )
    commit_response.raise_for_status()

    return commit_response.json()


def _put_file_content_with_retry(
    url: str,
    file_path: str,
    auth_headers: Dict[str, str],
) -> None:
    """PUT file bytes to the draft content URL with retry on transport errors.

    Retries on `RequestException` (covers SSLError, ConnectionError, Timeout,
    ChunkedEncodingError) and on HTTP 5xx responses.  4xx responses fail
    immediately — they indicate a real client/auth/payload problem that won't
    fix itself on retry.

    Each attempt re-opens the file and uploads from byte 0.  Single-PUT
    semantics in InvenioRDM mean the server-side content is overwritten on
    every successful PUT, so retrying is safe.

    Raises:
        RequestException or HTTPError: the last error if all attempts fail.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(1, _PUT_RETRY_ATTEMPTS + 1):
        try:
            with open(file_path, "rb") as fh:
                response = requests.put(url, data=fh, headers=auth_headers)
            # Treat 5xx as a transient error worth retrying; 4xx is fatal.
            if 500 <= response.status_code < 600:
                raise RequestException(
                    f"Server error HTTP {response.status_code}: "
                    f"{response.text[:200]}"
                )
            response.raise_for_status()
            return
        except HTTPError as exc:
            # 4xx — do not retry.
            raise
        except RequestException as exc:
            last_exc = exc
            file_name = Path(file_path).name
            if attempt < _PUT_RETRY_ATTEMPTS:
                backoff = _PUT_RETRY_BACKOFF_S[attempt - 1]
                logger.warning(
                    "  PUT failed for %s (attempt %d/%d): %s: %s. "
                    "Retrying in %ds...",
                    file_name, attempt, _PUT_RETRY_ATTEMPTS,
                    exc.__class__.__name__, exc, backoff,
                )
                time.sleep(backoff)
            else:
                logger.error(
                    "  PUT failed for %s after %d attempts. Last error: %s: %s",
                    file_name, _PUT_RETRY_ATTEMPTS,
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
    response = requests.post(url, headers=_auth_headers(credentials))
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
    response = requests.delete(url, headers=_auth_headers(credentials))
    response.raise_for_status()


def _api_get_with_retry(
    url: str,
    auth_headers: Dict[str, str],
    label: str,
    allow_404: bool = False,
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
            response = requests.get(url, headers=auth_headers)
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
    """Delete a single file entry from a draft (used to clear a pending slot)."""
    url = f"{credentials.base_url}records/{record_id}/draft/files/{key}"
    response = requests.delete(url, headers=_auth_headers(credentials))
    # 404 is fine — the slot is already gone.
    if response.status_code != 404:
        response.raise_for_status()


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

    Returns:
        Dictionary with keys:
            'successful' (bool), 'api_response' (dict|None), 'error' (dict|None).
    """
    credentials = get_credentials_from_env()
    record_id: Optional[str] = None
    draft_response: Optional[Dict[str, Any]] = None
    is_resume = False

    try:
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

            draft_response = create_draft_record(credentials, draft_metadata)
            record_id = draft_response.get("id")

            if not record_id:
                raise ValueError("No record ID returned from draft creation")

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

            logger.info("Draft created with ID: %s", record_id)

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
                }
                Path(state_file_path).write_text(
                    _json.dumps(state, indent=2), encoding="utf-8"
                )
                logger.info("  Wrote upload state file: %s", state_file_path)
            except Exception as state_exc:
                logger.warning(
                    "Could not write upload state file %s: %s",
                    state_file_path, state_exc,
                )

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
            target_names = {Path(p).name for p in files}
            for key, entry in existing_by_key.items():
                if key not in target_names:
                    logger.info(
                        "    (not in upload list — leaving as-is: %s)", key
                    )
                    continue
                status = (entry.get("status") or "").lower()
                # Treat any status that looks "finalized" as already uploaded.
                # InvenioRDM's canonical value is "completed"; we log the
                # raw value so a future status change is easy to spot.
                if status == "completed":
                    skip_keys.add(key)
                    logger.info(
                        "  Already uploaded, skipping: %s (status=%s)", key, status
                    )
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
            file_name = Path(file_path).name
            file_size_mb = Path(file_path).stat().st_size / (1024 * 1024)

            logger.info(
                "  [%d/%d] Uploading %s (%.2f MB)...",
                i, len(to_upload), file_name, file_size_mb,
            )

            start_time = time.time()
            upload_file_to_draft(credentials, record_id, file_path)
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

    except (HTTPError, RequestException, Exception) as exc:
        # Unified error handling — extract details for HTTP errors
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
