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
    with open(file_path, "rb") as fh:
        upload_response = requests.put(
            file_entry["links"]["content"], data=fh, headers=auth,
        )
        upload_response.raise_for_status()

    # Step 3: Commit the file
    commit_response = requests.post(
        file_entry["links"]["commit"], headers=auth,
    )
    commit_response.raise_for_status()

    return commit_response.json()


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

    Returns:
        Dictionary with keys:
            'successful' (bool), 'api_response' (dict|None), 'error' (dict|None).
    """
    credentials = get_credentials_from_env()
    record_id = None

    try:
        # --- Create draft record ---
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

        # Add community if specified
        if config.community_id:
            draft_metadata["parent"] = {
                "communities": {"ids": [config.community_id]}
            }

        # Add custom fields if specified
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
                logger.info("  Request log saved: %s", Path(request_log_path).name)
            except Exception as log_exc:
                logger.warning("Could not save request log: %s", log_exc)

        logger.info("Draft created with ID: %s", record_id)

        # --- Upload files ---
        logger.info("Uploading %d file(s)...", len(files))
        for i, file_path in enumerate(files, 1):
            file_name = Path(file_path).name
            file_size_mb = Path(file_path).stat().st_size / (1024 * 1024)

            logger.info(
                "  [%d/%d] Uploading %s (%.2f MB)...",
                i, len(files), file_name, file_size_mb,
            )

            start_time = time.time()
            upload_file_to_draft(credentials, record_id, file_path)
            elapsed = time.time() - start_time

            logger.info("  Uploaded in %.1fs", elapsed)

        logger.info("All files uploaded successfully")

        # --- Submit to community review queue (if a community is configured) ---
        # Including parent.communities.ids at draft creation only *associates*
        # the community; this second call moves the draft into the community's
        # review queue so a manager can accept it.  Skipped when community_id
        # is blank, so non-community uploads are completely unaffected.
        review_response = None
        if config.community_id:
            logger.info("Submitting draft to community review queue...")
            review_response = submit_to_community_review(
                credentials, record_id, config.community_id
            )
            logger.info(
                "  Submitted to community review — status: %s",
                review_response.get("status", "unknown"),
            )

        # --- Publish if requested ---
        if auto_publish:
            logger.info("Publishing record...")
            publish_response = publish_draft(credentials, record_id)
            logger.info("Record published")
            return {
                "successful": True,
                "api_response": publish_response,
                "error": None,
            }

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
