"""Pre-flight test for InvenioRDM (Zenodo) multipart file upload.

Verifies that Zenodo's multipart upload API is enabled on the target
instance and that our planned init/PUT/commit payload format is accepted,
BEFORE we change the production uploader.  Performs an end-to-end round
trip:

    1. Create a throwaway draft (minimal metadata, never published).
    2. Generate a 12 MB deterministic test file.
    3. Multipart-init the file (2 parts x 6 MB).
    4. PUT each part to the URL returned by init.
    5. Commit.
    6. Verify the committed file's size matches what we sent.
    7. Delete the draft.

On any failure, prints the offending HTTP response body and leaves the
draft in place so you can inspect it.  Reads credentials from the same
INVENIO_RDM_* env vars as the production uploader.

Environment variables:
    INVENIO_RDM_ACCESS_TOKEN: Zenodo API bearer token. Read indirectly
        via ``get_credentials_from_env()`` (imported from
        ``standalone_uploader``); a missing or placeholder value aborts
        the test with exit code 2.
    INVENIO_RDM_BASE_URL: Zenodo API base URL (e.g.,
        https://zenodo.org/api/). Also read via
        ``get_credentials_from_env()``.

Usage:
    source Resources/set_env.sh
    python multipart_preflight.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from standalone_uploader import (
    Credentials,
    _auth_headers,
    create_draft_record,
    delete_draft,
    get_credentials_from_env,
)


# S3-style multipart minimum is 5 MB per non-final part; 6 MB stays comfortably
# above that while keeping the test fast.
PART_SIZE = 6 * 1024 * 1024
NUM_PARTS = 2
TOTAL_SIZE = PART_SIZE * NUM_PARTS
TEST_FILE_NAME = "azus_multipart_preflight.bin"
TEST_FILE_PATH = Path(tempfile.gettempdir()) / TEST_FILE_NAME


def _minimal_draft_metadata() -> Dict[str, Any]:
    """Smallest metadata Zenodo will accept for a draft record.

    Never published — this draft is deleted at the end of the test.

    Returns:
        A metadata payload dict (access/files/metadata blocks) suitable
        for passing to ``create_draft_record``.
    """
    return {
        "access": {"record": "public", "files": "public"},
        "files": {"enabled": True},
        "metadata": {
            "resource_type": {"id": "dataset"},
            "title": "AZUS multipart upload pre-flight (DELETE ME)",
            "publication_date": time.strftime("%Y-%m-%d"),
            "creators": [
                {
                    "person_or_org": {
                        "type": "personal",
                        "given_name": "Pre-flight",
                        "family_name": "Test",
                    }
                }
            ],
            "description": (
                "Throwaway draft created by multipart_preflight.py to verify "
                "InvenioRDM multipart upload support. Safe to delete."
            ),
        },
    }


def _generate_test_file(path: Path, size: int) -> None:
    """Write a deterministic test file of the requested size.

    Fills ``path`` with a repeating 8-byte pattern (``AZUSMPRT``) so the
    content is reproducible and easy to eyeball, writing in 1 MB blocks
    until ``size`` bytes have been emitted.

    Args:
        path: Destination path for the generated file (overwritten if it
            already exists).
        size: Total number of bytes to write.
    """
    pattern = b"AZUSMPRT"  # 8 bytes, deterministic, easy to eyeball
    with open(path, "wb") as fh:
        remaining = size
        block = pattern * (1024 * 1024 // len(pattern))  # 1 MB block
        while remaining > 0:
            chunk = block if remaining >= len(block) else block[:remaining]
            fh.write(chunk)
            remaining -= len(chunk)


def _init_multipart_upload(
    credentials: Credentials, record_id: str, key: str
) -> Dict[str, Any]:
    """Initialize a multipart file upload on a draft record.

    POSTs a single file entry declaring a multipart transfer
    (``type`` "M", ``NUM_PARTS`` parts of ``PART_SIZE`` bytes) to the
    draft's files endpoint.

    Args:
        credentials: Zenodo credentials.
        record_id: ID of the draft record to attach the file to.
        key: File name (key) to register for the upload.

    Returns:
        The parsed JSON init response, including the per-part upload
        links when multipart is enabled.

    Raises:
        RuntimeError: If the API returns an HTTP status of 400 or above;
            the message includes the status code and response body.
    """
    url = f"{credentials.base_url}records/{record_id}/draft/files"
    payload = [
        {
            "key": key,
            "size": TOTAL_SIZE,
            "transfer": {
                "type": "M",
                "parts": NUM_PARTS,
                "part_size": PART_SIZE,
            },
        }
    ]
    response = requests.post(
        url,
        json=payload,
        headers=_auth_headers(credentials, content_type="application/json"),
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Multipart init failed: HTTP {response.status_code}\n"
            f"Response body:\n{response.text}"
        )
    return response.json()


def _put_part(credentials: Credentials, url: str, part_bytes: bytes) -> None:
    """Upload a single part to its pre-signed part URL.

    Args:
        credentials: Zenodo credentials (used for the auth header).
        url: The part upload URL returned by the init response.
        part_bytes: Raw bytes of this part to PUT.

    Raises:
        RuntimeError: If the PUT returns an HTTP status of 400 or above;
            the message includes the URL, status code, and response body.
    """
    response = requests.put(url, data=part_bytes, headers=_auth_headers(credentials))
    if response.status_code >= 400:
        raise RuntimeError(
            f"Part PUT failed at {url}: HTTP {response.status_code}\n"
            f"Response body:\n{response.text}"
        )


def _commit_file(credentials: Credentials, record_id: str, key: str) -> Dict[str, Any]:
    """Commit a multipart upload, finalizing the file on the draft.

    Args:
        credentials: Zenodo credentials.
        record_id: ID of the draft record holding the file.
        key: File name (key) whose upload should be committed.

    Returns:
        The parsed JSON commit response, including the server-reported
        file ``size``.

    Raises:
        RuntimeError: If the API returns an HTTP status of 400 or above;
            the message includes the status code and response body.
    """
    url = f"{credentials.base_url}records/{record_id}/draft/files/{key}/commit"
    response = requests.post(url, headers=_auth_headers(credentials))
    if response.status_code >= 400:
        raise RuntimeError(
            f"Commit failed: HTTP {response.status_code}\n"
            f"Response body:\n{response.text}"
        )
    return response.json()


def _extract_part_urls(init_response: Dict[str, Any], key: str) -> List[str]:
    """Pull the ordered per-part upload URLs out of an init response.

    Locates the file entry matching ``key``, reads its
    ``links.parts`` list, and returns the part URLs sorted by part
    number. A missing ``links.parts`` is treated as a signal that
    multipart upload is not enabled on the instance.

    Args:
        init_response: The parsed JSON returned by
            ``_init_multipart_upload``.
        key: File name (key) whose part URLs to extract.

    Returns:
        The part upload URLs ordered by ascending part number.

    Raises:
        RuntimeError: If no entry matches ``key``, if the entry has no
            ``links.parts`` (multipart likely disabled), or if the
            number of parts does not equal ``NUM_PARTS``.
    """
    entries = init_response.get("entries", [])
    entry = next((e for e in entries if e.get("key") == key), None)
    if entry is None:
        raise RuntimeError(
            f"No entry for '{key}' in init response. Keys: "
            f"{[e.get('key') for e in entries]}"
        )
    parts = entry.get("links", {}).get("parts")
    if not parts:
        raise RuntimeError(
            "Init response did not include 'links.parts' for the file entry. "
            "Multipart upload may not be enabled on this instance.\n"
            f"Entry:\n{json.dumps(entry, indent=2)}"
        )
    if len(parts) != NUM_PARTS:
        raise RuntimeError(
            f"Expected {NUM_PARTS} part URLs, got {len(parts)}. Entry:\n"
            f"{json.dumps(entry, indent=2)}"
        )
    return [p["url"] for p in sorted(parts, key=lambda p: p["part"])]


def _print_header(text: str) -> None:
    """Print a banner line, framed above and below by a rule of '='.

    Args:
        text: The header text to display between the two rules.
    """
    print("=" * 60)
    print(text)
    print("=" * 60)


def _print_step(step: int, total: int, label: str) -> None:
    """Print a numbered step marker for the console progress output.

    Args:
        step: The 1-based index of the current step.
        total: The total number of steps in the run.
        label: A short description of what this step does.
    """
    print(f"\n[Step {step}/{total}] {label}")


def main() -> int:
    """Run the end-to-end multipart upload pre-flight and report.

    Loads credentials from the environment, creates a throwaway draft,
    generates and multipart-uploads a test file, commits it, verifies the
    server-reported size, and deletes the draft. On failure the draft is
    left in place (with cleanup instructions printed) so it can be
    inspected; the local test file is always removed.

    Returns:
        A process exit code: 0 if multipart upload works end to end, 1 if
        any step fails, and 2 if credentials are missing or invalid.

    Raises:
        RuntimeError: Raised internally on a local or server-side
            file-size mismatch. It is caught by this function's own
            handler and reported as a FAIL result (exit code 1) rather
            than propagated to the caller.
    """
    # 1 draft + 2 generate + 3 init + NUM_PARTS puts + commit + cleanup
    total_steps = 5 + NUM_PARTS
    record_id: Optional[str] = None

    try:
        credentials = get_credentials_from_env()
    except ValueError as exc:
        print(f"Credentials error: {exc}", file=sys.stderr)
        return 2

    masked = credentials.token[-4:] if len(credentials.token) >= 4 else "****"
    _print_header("AZUS Multipart Upload Pre-flight Test")
    print(f"Endpoint:  {credentials.base_url}")
    print(f"Token:     ****{masked}")
    print(f"Test file: {TOTAL_SIZE / (1024 * 1024):.2f} MB"
          f" in {NUM_PARTS} parts of {PART_SIZE / (1024 * 1024):.2f} MB each")

    try:
        _print_step(1, total_steps, "Creating throwaway test draft...")
        draft = create_draft_record(credentials, _minimal_draft_metadata())
        record_id = str(draft["id"])
        print(f"  -> Draft created: ID {record_id}")

        _print_step(2, total_steps, f"Generating test file at {TEST_FILE_PATH}...")
        _generate_test_file(TEST_FILE_PATH, TOTAL_SIZE)
        actual = TEST_FILE_PATH.stat().st_size
        print(f"  -> Wrote {actual:,} bytes")
        if actual != TOTAL_SIZE:
            raise RuntimeError(f"File size mismatch: expected {TOTAL_SIZE}, got {actual}")

        _print_step(
            3,
            total_steps,
            f"Initializing multipart upload ({NUM_PARTS} parts x "
            f"{PART_SIZE // (1024 * 1024)} MB)...",
        )
        init_response = _init_multipart_upload(credentials, record_id, TEST_FILE_NAME)
        print("  -> Init response (full):")
        print(json.dumps(init_response, indent=2))
        part_urls = _extract_part_urls(init_response, TEST_FILE_NAME)
        print(f"  -> Got {len(part_urls)} part URL(s).")

        with open(TEST_FILE_PATH, "rb") as fh:
            for i, url in enumerate(part_urls, start=1):
                _print_step(3 + i, total_steps, f"PUT part {i}/{NUM_PARTS} ({PART_SIZE / (1024*1024):.2f} MB)...")
                chunk = fh.read(PART_SIZE)
                started = time.time()
                _put_part(credentials, url, chunk)
                print(f"  -> OK in {time.time() - started:.2f}s")

        _print_step(4 + NUM_PARTS, total_steps, "Committing multipart upload...")
        commit_response = _commit_file(credentials, record_id, TEST_FILE_NAME)
        server_size = commit_response.get("size")
        print(f"  -> Commit succeeded. Server reports file size: {server_size}")
        if server_size != TOTAL_SIZE:
            raise RuntimeError(
                f"Server-side size mismatch: expected {TOTAL_SIZE}, got {server_size}"
            )
        print("  -> File integrity: MATCH")

        _print_step(5 + NUM_PARTS, total_steps,
                    f"Cleaning up test draft {record_id}...")
        delete_draft(credentials, record_id)
        print("  -> Draft deleted.")
        record_id = None

        print()
        _print_header("RESULT: PASS - multipart upload is working on this instance.")
        print("You can proceed with the production implementation.")
        return 0

    except Exception as exc:
        print()
        _print_header("RESULT: FAIL")
        print(f"Error: {exc}")
        if record_id is not None:
            print(
                f"\nTest draft {record_id} was NOT deleted (left in place for "
                f"inspection). Delete it manually when ready:\n"
                f"  curl -X DELETE -H 'Authorization: Bearer <token>' \\\n"
                f"    {credentials.base_url}records/{record_id}/draft"
            )
        return 1

    finally:
        if TEST_FILE_PATH.exists():
            try:
                TEST_FILE_PATH.unlink()
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
