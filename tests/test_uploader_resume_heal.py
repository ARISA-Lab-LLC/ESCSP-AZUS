"""Unit tests for standalone_uploader.py's resume mismatch-heal logic.

Covers two safety layers that keep a Zenodo draft's files identical to
the local dataset, with no network access (every HTTP-touching
collaborator is mocked):

1. The resume orchestration inside ``upload_to_zenodo()`` — on a resume,
   a "completed" remote entry is only skipped after it verifies against
   the local file (size first, then md5); a mismatched entry is deleted
   from the draft and re-uploaded; an unverifiable local file fails the
   dataset WITHOUT deleting the remote copy (which may be the only good
   copy); and a set ``abort_event`` stops the run at the file boundary.

2. The post-commit verification inside ``upload_file_to_draft()`` — a
   commit response whose size/checksum disagrees with the local file
   deletes the corrupt slot and raises ``FileIntegrityError``.

Run from the project root:

    python3 -m unittest tests.test_uploader_resume_heal -v
"""

import hashlib
import logging
import os
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "Resources"))

import standalone_uploader as uploader  # noqa: E402
from models.audiomoth import DraftConfig  # noqa: E402


def setUpModule():
    # The heal paths log warnings/errors by design; keep test output clean.
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


# --- fabricated payloads / helpers ------------------------------------------

_CONTENT = b"eclipse soundscapes audio bytes"
_SIZE = len(_CONTENT)
_MD5 = hashlib.md5(_CONTENT).hexdigest()

_FAKE_ENV = {
    "INVENIO_RDM_ACCESS_TOKEN": "unit-test-token",
    "INVENIO_RDM_BASE_URL": "https://zenodo.invalid/api/",
}


def committed_entry(key, size, md5=None):
    """A Zenodo draft file entry in 'completed' state."""
    entry = {"key": key, "status": "completed", "size": size}
    if md5 is not None:
        entry["checksum"] = f"md5:{md5}"
    return entry


class FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, payload=None, status_code=200):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            from requests.exceptions import HTTPError
            raise HTTPError(f"HTTP {self.status_code}", response=self)


# --- upload_to_zenodo() resume orchestration --------------------------------

class _ResumeTestBase(unittest.TestCase):
    """Hermetic harness: run upload_to_zenodo in resume mode with every
    Zenodo-touching collaborator mocked out on the module."""

    DRAFT_ID = "1234567"

    # Collaborators replaced with plain MagicMocks (behavior configured
    # per-test via the returned mocks dict).
    _COLLABORATORS = (
        "get_draft_record",
        "list_draft_files",
        "delete_draft_file",
        "upload_file_to_draft",
        "ensure_doi_reserved",
        "submit_to_community_review",
        "publish_draft",
    )
    # A resume run must never create a fresh draft or issue a raw API GET
    # (the title guard is bypassed when existing_draft_id is supplied);
    # these tripwires make any such call fail the test loudly.
    _TRIPWIRES = ("create_draft_record", "_api_get_with_retry")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        env = mock.patch.dict(os.environ, _FAKE_ENV)
        env.start()
        self.addCleanup(env.stop)

    def make_local_file(self, name="ESID_005.zip", content=_CONTENT):
        path = self.tmp / name
        path.write_bytes(content)
        return path

    def run_resume(self, files, remote_entries, abort_event=None):
        """Run upload_to_zenodo(existing_draft_id=...) and return
        (result, mocks) where mocks maps collaborator name -> MagicMock."""
        draft_response = {
            "id": self.DRAFT_ID,
            "status": "draft",
            "is_published": False,
            "parent": {},
        }
        with ExitStack() as stack:
            mocks = {
                name: stack.enter_context(mock.patch.object(uploader, name))
                for name in self._COLLABORATORS
            }
            mocks["get_draft_record"].return_value = draft_response
            mocks["list_draft_files"].return_value = remote_entries
            for name in self._TRIPWIRES:
                stack.enter_context(mock.patch.object(
                    uploader, name,
                    side_effect=AssertionError(
                        f"{name}() must not be called on this resume run"
                    ),
                ))
            result = uploader.upload_to_zenodo(
                files=[str(f) for f in files],
                config=DraftConfig(),
                existing_draft_id=self.DRAFT_ID,
                abort_event=abort_event,
            )
        return result, mocks


class TestResumeSkipsVerifiedFile(_ResumeTestBase):
    def test_matching_committed_file_is_skipped(self):
        """A committed remote entry whose size AND md5 match the local
        file is skipped: no delete, no re-upload, run succeeds."""
        local = self.make_local_file()
        result, mocks = self.run_resume(
            [local], [committed_entry(local.name, _SIZE, _MD5)]
        )
        self.assertTrue(result["successful"])
        self.assertIsNone(result["error"])
        mocks["upload_file_to_draft"].assert_not_called()
        mocks["delete_draft_file"].assert_not_called()

    def test_only_mismatched_file_is_reuploaded_in_mixed_set(self):
        """With one verified file and one size-mismatched file, only the
        mismatched one is healed (deleted + re-uploaded)."""
        good = self.make_local_file("ESID_005.zip")
        bad = self.make_local_file("file_list.csv", b"a,b,c\n")
        result, mocks = self.run_resume(
            [good, bad],
            [
                committed_entry(good.name, _SIZE, _MD5),
                committed_entry(bad.name, 999_999),
            ],
        )
        self.assertTrue(result["successful"])
        uploaded = [
            call.args[2] for call in mocks["upload_file_to_draft"].call_args_list
        ]
        self.assertEqual(uploaded, [str(bad)])
        deleted = [
            call.args[1:] for call in mocks["delete_draft_file"].call_args_list
        ]
        self.assertEqual(deleted, [(self.DRAFT_ID, bad.name)])


class TestResumeHealsMismatchedFile(_ResumeTestBase):
    def _assert_healed(self, result, mocks, local):
        self.assertTrue(result["successful"])
        delete_call = mocks["delete_draft_file"].call_args
        self.assertEqual(delete_call.args[1:], (self.DRAFT_ID, local.name))
        mocks["delete_draft_file"].assert_called_once()
        upload_call = mocks["upload_file_to_draft"].call_args
        self.assertEqual(upload_call.args[1:], (self.DRAFT_ID, str(local)))
        mocks["upload_file_to_draft"].assert_called_once()

    def test_size_mismatch_is_deleted_and_reuploaded(self):
        """The interrupted-run scar: Zenodo holds a short copy of the
        file — it must be deleted and the local file re-uploaded."""
        local = self.make_local_file()
        result, mocks = self.run_resume(
            [local], [committed_entry(local.name, _SIZE - 5, _MD5)]
        )
        self._assert_healed(result, mocks, local)

    def test_md5_mismatch_with_matching_size_is_deleted_and_reuploaded(self):
        """Same length, different bytes: the cheap size check passes but
        the md5 comparison must still catch it and take the heal path."""
        local = self.make_local_file()
        result, mocks = self.run_resume(
            [local], [committed_entry(local.name, _SIZE, "0" * 32)]
        )
        self._assert_healed(result, mocks, local)

    def test_pending_slot_is_cleared_and_reuploaded(self):
        """A non-completed ('pending') slot from a failed prior run is
        deleted and the file re-uploaded from scratch."""
        local = self.make_local_file()
        result, mocks = self.run_resume(
            [local],
            [{"key": local.name, "status": "pending", "size": 0}],
        )
        self._assert_healed(result, mocks, local)

    def test_remote_entry_not_in_upload_list_is_left_alone(self):
        """A remote file that is not part of this upload (e.g. the
        deferred ZIP's siblings) is never verified, deleted, or touched."""
        local = self.make_local_file()
        result, mocks = self.run_resume(
            [local],
            [committed_entry("SOMETHING_ELSE.zip", 42)],  # would mismatch
        )
        self.assertTrue(result["successful"])
        mocks["delete_draft_file"].assert_not_called()
        # The local file itself has no remote entry, so it is uploaded.
        upload_call = mocks["upload_file_to_draft"].call_args
        self.assertEqual(upload_call.args[2], str(local))


class TestResumeUnverifiableLocalFile(_ResumeTestBase):
    """When the LOCAL copy cannot be read during verification the dataset
    must fail — and the remote copy must NOT be deleted, because it may
    be the only good copy left."""

    def _assert_failed_closed(self, result, mocks):
        self.assertFalse(result["successful"])
        self.assertEqual(result["error"]["type"], "FileIntegrityError")
        self.assertIn(
            "Cannot verify committed file", result["error"]["error_message"]
        )
        mocks["delete_draft_file"].assert_not_called()
        mocks["upload_file_to_draft"].assert_not_called()

    def test_missing_local_file_fails_without_deleting_remote(self):
        missing = self.tmp / "ESID_005.zip"  # never created
        result, mocks = self.run_resume(
            [missing], [committed_entry(missing.name, _SIZE, _MD5)]
        )
        self._assert_failed_closed(result, mocks)

    @unittest.skipIf(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        "chmod 000 does not block reads for root",
    )
    def test_unreadable_local_file_fails_without_deleting_remote(self):
        """stat() succeeds (size matches) but the md5 hashing read is
        denied — still the fail-closed FileIntegrityError path."""
        local = self.make_local_file()
        local.chmod(0o000)
        self.addCleanup(local.chmod, 0o644)
        result, mocks = self.run_resume(
            [local], [committed_entry(local.name, _SIZE, _MD5)]
        )
        self._assert_failed_closed(result, mocks)


class TestAbortBeforeFileLoop(_ResumeTestBase):
    def test_abort_event_set_stops_run_without_uploading(self):
        """A pre-set abort event yields the unsuccessful 'AbortedByUser'
        result at the first file boundary; nothing is uploaded and the
        draft is left resumable (no delete, no review submission)."""
        local = self.make_local_file()
        abort = threading.Event()
        abort.set()
        result, mocks = self.run_resume([local], [], abort_event=abort)
        self.assertFalse(result["successful"])
        self.assertIsNone(result["api_response"])
        self.assertEqual(result["error"]["type"], "AbortedByUser")
        mocks["upload_file_to_draft"].assert_not_called()
        mocks["delete_draft_file"].assert_not_called()
        mocks["submit_to_community_review"].assert_not_called()


# --- upload_file_to_draft() post-commit verification -------------------------

class TestUploadFileToDraftPostCommitVerification(unittest.TestCase):
    """Drive upload_file_to_draft with mocked requests: init, PUT, and
    commit all succeed at the HTTP level, but the commit response's
    reported size/checksum decides whether the upload is trusted."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.local = Path(self._tmp.name) / "ESID_005.zip"
        self.local.write_bytes(_CONTENT)
        self.credentials = uploader.Credentials(
            token="unit-test-token", base_url="https://zenodo.invalid/api/"
        )

    def _scripted_http(self, stack, commit_payload):
        """Patch the HTTP layer so init, PUT, and commit all succeed.

        requests.post serves the init response first, then the commit
        response; requests.put (the content upload) succeeds.  Returns
        the delete_draft_file mock for slot-cleanup assertions.
        """
        init_payload = {
            "entries": [{
                "key": self.local.name,
                "links": {
                    "content": "https://zenodo.invalid/content",
                    "commit": "https://zenodo.invalid/commit",
                },
            }]
        }
        stack.enter_context(mock.patch.object(
            uploader.requests, "post",
            side_effect=[
                FakeResponse(init_payload),
                FakeResponse(commit_payload),
            ],
        ))
        stack.enter_context(mock.patch.object(
            uploader.requests, "put", return_value=FakeResponse()
        ))
        return stack.enter_context(
            mock.patch.object(uploader, "delete_draft_file")
        )

    def _assert_slot_deleted_and_raised(self, commit_payload):
        with ExitStack() as stack:
            delete_mock = self._scripted_http(stack, commit_payload)
            with self.assertRaises(uploader.FileIntegrityError) as ctx:
                uploader.upload_file_to_draft(
                    self.credentials, "1234567", str(self.local)
                )
        self.assertIn("failed verification", str(ctx.exception))
        delete_mock.assert_called_once()
        self.assertEqual(
            delete_mock.call_args.args[1:], ("1234567", self.local.name)
        )

    def test_commit_reporting_wrong_size_deletes_slot_and_raises(self):
        self._assert_slot_deleted_and_raised({
            "key": self.local.name,
            "size": _SIZE + 7,
            "checksum": f"md5:{_MD5}",
        })

    def test_commit_reporting_wrong_md5_deletes_slot_and_raises(self):
        self._assert_slot_deleted_and_raised({
            "key": self.local.name,
            "size": _SIZE,
            "checksum": "md5:" + "0" * 32,
        })

    def test_commit_matching_local_file_returns_committed_entry(self):
        commit_payload = {
            "key": self.local.name,
            "status": "completed",
            "size": _SIZE,
            "checksum": f"md5:{_MD5}",
        }
        with ExitStack() as stack:
            delete_mock = self._scripted_http(stack, commit_payload)
            outcome = uploader.upload_file_to_draft(
                self.credentials, "1234567", str(self.local)
            )
        self.assertEqual(outcome, commit_payload)
        delete_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
