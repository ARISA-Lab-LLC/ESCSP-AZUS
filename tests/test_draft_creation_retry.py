"""Unit tests for the guarded draft-creation retry and ZIP-scoped
``--upload-attempts``.

``POST /records`` is not idempotent, so its retry must never blindly
re-POST: a failed-looking attempt (transient 5xx — the failure mode a
July 2026 production run hit hundreds of times — or a dropped
connection) may still have created the draft.  These tests prove the
retry contract of ``_create_draft_with_guarded_retry``:

* transient 5xx → wait, guard-search the title, retry only when NO
  record with the title exists;
* a draft the failed POST actually created is ADOPTED, never duplicated;
* a published match or multiple drafts → ``DuplicateTitleError``;
* 4xx, an unguardable retry (empty title / guard-search failure), and
  exhausted attempts all fail closed with no extra POSTs.

They also pin the new ``--upload-attempts`` scope: the setting applies
ONLY to the data ZIP; companion files keep the default retry count.

Run from the project root:

    python3 -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from requests.exceptions import HTTPError, RequestException

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "Resources"))

import standalone_uploader as uploader  # noqa: E402
import standalone_tasks as tasks  # noqa: E402
from models.audiomoth import (  # noqa: E402
    DataCollector,
    DraftConfig,
    UploadData,
)

_FAKE_ENV = {
    "INVENIO_RDM_ACCESS_TOKEN": "dummy-token",
    "INVENIO_RDM_BASE_URL": "https://example.org/api/",
}
_TITLE = "2024-04-08 Total Solar Eclipse ESID#288"
_CREDS = uploader.Credentials(
    token="dummy-token", base_url="https://example.org/api/"
)
_METADATA = {"metadata": {"title": _TITLE}}


def _http_error(status):
    """A requests.HTTPError carrying a response with ``status``."""
    response = mock.MagicMock()
    response.status_code = status
    error = HTTPError(f"HTTP {status}")
    error.response = response
    return error


def _search_response(hits):
    """Duck-typed guard-search response wrapping the given hits."""
    response = mock.MagicMock()
    response.json.return_value = {"hits": {"hits": hits}}
    return response


def _hit(record_id, title=_TITLE, published=False):
    return {
        "id": record_id,
        "metadata": {"title": title},
        "is_published": published,
    }


class _RetryTestBase(unittest.TestCase):
    """Run _create_draft_with_guarded_retry with collaborators mocked."""

    def run_retry(self, create_effects, guard_effects=None,
                  intended_title=_TITLE, full_draft=None,
                  delete_effect=None):
        """Execute the helper; returns (outcome, mocks).

        ``outcome`` is the return value, or the exception raised.
        ``mocks["parent"]`` records the relative ordering of sleep and
        guard-search calls.
        """
        with ExitStack() as stack:
            mocks = {
                name: stack.enter_context(
                    mock.patch.object(uploader, name)
                )
                for name in ("create_draft_record", "_api_get_with_retry",
                             "get_draft_record", "delete_draft")
            }
            sleep = stack.enter_context(
                mock.patch.object(uploader.time, "sleep")
            )
            mocks["sleep"] = sleep
            parent = mock.Mock()
            parent.attach_mock(sleep, "sleep")
            parent.attach_mock(mocks["_api_get_with_retry"], "guard_search")
            mocks["parent"] = parent
            mocks["create_draft_record"].side_effect = create_effects
            if guard_effects is not None:
                mocks["_api_get_with_retry"].side_effect = guard_effects
            mocks["get_draft_record"].return_value = full_draft
            if delete_effect is not None:
                mocks["delete_draft"].side_effect = delete_effect
            try:
                outcome = uploader._create_draft_with_guarded_retry(
                    _CREDS, _METADATA, intended_title
                )
            except Exception as exc:  # returned for assertion, not hidden
                outcome = exc
        return outcome, mocks

    def assertPostCount(self, mocks, count):
        self.assertEqual(mocks["create_draft_record"].call_count, count)


class TestGuardedRetry(_RetryTestBase):
    def test_transient_500_then_success_retries_once(self):
        # Two guard searches: the between-attempts guard, then the
        # post-success sweep after the retry POST succeeds.
        outcome, mocks = self.run_retry(
            create_effects=[_http_error(500), {"id": "111"}],
            guard_effects=[_search_response([]), _search_response([])],
        )
        self.assertEqual(outcome, ({"id": "111"}, False))
        self.assertPostCount(mocks, 2)
        self.assertEqual(
            [c.args[0] for c in mocks["sleep"].call_args_list],
            [30, uploader._POST_SUCCESS_SWEEP_DELAY_S],
        )
        # Pin the guard search's endpoint and query, and that the
        # backoff sleep happens BEFORE the search.
        guard_call = mocks["_api_get_with_retry"].call_args_list[0]
        self.assertEqual(
            guard_call.kwargs["url"], "https://example.org/api/user/records"
        )
        self.assertEqual(
            guard_call.kwargs["params"]["q"], f'metadata.title:"{_TITLE}"'
        )
        call_names = [name for name, _, _ in mocks["parent"].mock_calls]
        self.assertLess(
            call_names.index("sleep"), call_names.index("guard_search")
        )
        mocks["delete_draft"].assert_not_called()

    def test_connection_error_is_also_retryable(self):
        outcome, mocks = self.run_retry(
            create_effects=[RequestException("connection reset"),
                            {"id": "111"}],
            guard_effects=[_search_response([]), _search_response([])],
        )
        self.assertEqual(outcome, ({"id": "111"}, False))
        self.assertPostCount(mocks, 2)

    def test_4xx_raises_immediately_without_retry(self):
        outcome, mocks = self.run_retry(
            create_effects=[_http_error(400)],
        )
        self.assertIsInstance(outcome, HTTPError)
        self.assertPostCount(mocks, 1)
        mocks["sleep"].assert_not_called()
        mocks["_api_get_with_retry"].assert_not_called()

    def test_draft_created_by_failed_post_is_adopted_not_duplicated(self):
        full = {"id": "222", "status": "draft", "parent": {}}
        outcome, mocks = self.run_retry(
            create_effects=[_http_error(500)],
            guard_effects=[_search_response([_hit("222")])],
            full_draft=full,
        )
        self.assertEqual(outcome, (full, True))
        self.assertPostCount(mocks, 1)  # adopted — NO second POST
        mocks["get_draft_record"].assert_called_once_with(_CREDS, "222")

    def test_published_match_raises_duplicate_title(self):
        outcome, mocks = self.run_retry(
            create_effects=[_http_error(500)],
            guard_effects=[
                _search_response([_hit("333", published=True)])
            ],
        )
        self.assertIsInstance(outcome, uploader.DuplicateTitleError)
        self.assertPostCount(mocks, 1)

    def test_multiple_drafts_raise_duplicate_title(self):
        outcome, mocks = self.run_retry(
            create_effects=[_http_error(500)],
            guard_effects=[
                _search_response([_hit("333"), _hit("444")])
            ],
        )
        self.assertIsInstance(outcome, uploader.DuplicateTitleError)
        self.assertPostCount(mocks, 1)

    def test_guard_search_failure_fails_closed(self):
        """If the guard cannot confirm no draft exists, do NOT re-POST."""
        original = _http_error(500)
        outcome, mocks = self.run_retry(
            create_effects=[original],
            guard_effects=[RequestException("search down")],
        )
        self.assertIs(outcome, original)
        self.assertPostCount(mocks, 1)

    def test_empty_title_means_single_shot(self):
        """--skip-title-guard leaves no safe way to retry — one shot."""
        outcome, mocks = self.run_retry(
            create_effects=[_http_error(500)],
            intended_title="",
        )
        self.assertIsInstance(outcome, HTTPError)
        self.assertPostCount(mocks, 1)
        mocks["sleep"].assert_not_called()

    def test_exhausted_attempts_raise_last_error(self):
        # The guard now runs after EVERY failed attempt, including the
        # final one (a phantom from the last POST must still be found),
        # so three failures mean three guard searches.
        outcome, mocks = self.run_retry(
            create_effects=[_http_error(500)] * 3,
            guard_effects=[_search_response([])] * 3,
        )
        self.assertIsInstance(outcome, HTTPError)
        self.assertPostCount(mocks, 3)
        self.assertEqual(
            [c.args[0] for c in mocks["sleep"].call_args_list], [30, 90, 90]
        )

    def test_final_attempt_phantom_is_adopted(self):
        """A phantom created by the LAST failed-looking POST is still
        found by the post-failure guard and adopted — the run succeeds."""
        full = {"id": "222", "status": "draft", "parent": {}}
        outcome, mocks = self.run_retry(
            create_effects=[_http_error(500)] * 3,
            guard_effects=[_search_response([]), _search_response([]),
                           _search_response([_hit("222")])],
            full_draft=full,
        )
        self.assertEqual(outcome, (full, True))
        self.assertPostCount(mocks, 3)

    def test_adopted_draft_vanishing_fails_closed(self):
        """Search sees a draft but /draft 404s — contradictory state."""
        original = _http_error(500)
        outcome, mocks = self.run_retry(
            create_effects=[original],
            guard_effects=[_search_response([_hit("222")])],
            full_draft=None,  # get_draft_record -> None (404)
        )
        self.assertIs(outcome, original)
        self.assertPostCount(mocks, 1)


class TestPostSuccessSweep(_RetryTestBase):
    """After a retry POST succeeds, one more search hunts for a phantom
    the earlier attempt created but the guard could not yet see."""

    def test_sweep_stray_deletes_fresh_draft_and_adopts_phantom(self):
        full = {"id": "999", "status": "draft", "parent": {}}
        outcome, mocks = self.run_retry(
            create_effects=[_http_error(500), {"id": "111"}],
            guard_effects=[_search_response([]),
                           _search_response([_hit("999")])],
            full_draft=full,
        )
        self.assertEqual(outcome, (full, True))
        # The fresh (empty) draft is deleted; the phantom survives.
        mocks["delete_draft"].assert_called_once_with(_CREDS, "111")

    def test_sweep_ignores_the_just_created_draft_itself(self):
        """The sweep finding ONLY the new draft (now indexed) is clean."""
        outcome, mocks = self.run_retry(
            create_effects=[_http_error(500), {"id": "111"}],
            guard_effects=[_search_response([]),
                           _search_response([_hit("111")])],
        )
        self.assertEqual(outcome, ({"id": "111"}, False))
        mocks["delete_draft"].assert_not_called()

    def test_sweep_search_failure_fails_closed_keeping_draft(self):
        """Cannot verify -> stop, but do NOT delete the created draft
        (the next run's duplicate guard adopts it)."""
        outcome, mocks = self.run_retry(
            create_effects=[_http_error(500), {"id": "111"}],
            guard_effects=[_search_response([]),
                           RequestException("sweep search down")],
        )
        self.assertIsInstance(outcome, uploader.DuplicateTitleError)
        self.assertIn("111", str(outcome))
        mocks["delete_draft"].assert_not_called()

    def test_sweep_published_match_deletes_fresh_and_raises(self):
        outcome, mocks = self.run_retry(
            create_effects=[_http_error(500), {"id": "111"}],
            guard_effects=[_search_response([]),
                           _search_response([_hit("333", published=True)])],
        )
        self.assertIsInstance(outcome, uploader.DuplicateTitleError)
        mocks["delete_draft"].assert_called_once_with(_CREDS, "111")

    def test_sweep_delete_failure_names_both_ids(self):
        outcome, mocks = self.run_retry(
            create_effects=[_http_error(500), {"id": "111"}],
            guard_effects=[_search_response([]),
                           _search_response([_hit("999")])],
            delete_effect=RequestException("delete refused"),
        )
        self.assertIsInstance(outcome, uploader.DuplicateTitleError)
        self.assertIn("111", str(outcome))
        self.assertIn("999", str(outcome))

    def test_first_attempt_success_never_sweeps(self):
        outcome, mocks = self.run_retry(
            create_effects=[{"id": "111"}],
        )
        self.assertEqual(outcome, ({"id": "111"}, False))
        mocks["_api_get_with_retry"].assert_not_called()
        mocks["sleep"].assert_not_called()


class TestGuardSearchHardening(_RetryTestBase):
    """The shared search helper fails closed on odd shapes and escapes
    the title in the Lucene query."""

    def test_lucene_phrase_escapes_quotes_and_backslashes(self):
        self.assertEqual(
            uploader._lucene_phrase('Recording "Alpha" site'),
            '"Recording \\"Alpha\\" site"',
        )
        self.assertEqual(uploader._lucene_phrase("plain"), '"plain"')

    def test_quoted_title_is_escaped_in_the_query(self):
        title = 'Recording "Alpha" ESID#073'
        outcome, mocks = self.run_retry(
            create_effects=[_http_error(500)] * 3,
            guard_effects=[_search_response([])] * 3,
            intended_title=title,
        )
        self.assertIsInstance(outcome, HTTPError)  # exhausted after guards
        q = mocks["_api_get_with_retry"].call_args.kwargs["params"]["q"]
        self.assertEqual(
            q, 'metadata.title:"Recording \\"Alpha\\" ESID#073"'
        )

    def test_missing_hits_envelope_fails_closed(self):
        """A 200 body without the search envelope must never be read as
        'no draft exists'."""
        odd = mock.MagicMock()
        odd.json.return_value = {"message": "gateway placeholder"}
        original = _http_error(500)
        outcome, mocks = self.run_retry(
            create_effects=[original],
            guard_effects=[odd],
        )
        self.assertIs(outcome, original)
        self.assertPostCount(mocks, 1)


class TestZipScopedUploadAttempts(unittest.TestCase):
    """--upload-attempts applies to the ZIP only (resume-mode harness)."""

    DRAFT_ID = "1234567"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        env = mock.patch.dict(os.environ, _FAKE_ENV)
        env.start()
        self.addCleanup(env.stop)

    def _run(self, upload_attempts, priority_files):
        zip_path = self.tmp / "ESID_005.zip"
        readme_path = self.tmp / "README.md"
        zip_path.write_bytes(b"z" * 32)
        readme_path.write_bytes(b"r" * 8)
        with ExitStack() as stack:
            mocks = {
                name: stack.enter_context(
                    mock.patch.object(uploader, name)
                )
                for name in (
                    "get_draft_record", "list_draft_files",
                    "upload_file_to_draft", "ensure_doi_reserved",
                    "submit_to_community_review", "publish_draft",
                )
            }
            mocks["get_draft_record"].return_value = {
                "id": self.DRAFT_ID, "status": "draft",
                "is_published": False, "parent": {},
            }
            mocks["list_draft_files"].return_value = []
            result = uploader.upload_to_zenodo(
                files=[str(zip_path), str(readme_path)],
                config=DraftConfig(),
                existing_draft_id=self.DRAFT_ID,
                upload_attempts=upload_attempts,
                priority_files=priority_files,
            )
        self.assertTrue(result["successful"], result)
        return {
            call.args[2] if len(call.args) > 2 else call.kwargs["file_path"]:
                call.kwargs["upload_attempts"]
            for call in mocks["upload_file_to_draft"].call_args_list
        }

    def test_attempts_apply_to_zip_only(self):
        attempts = self._run(upload_attempts=1, priority_files={"ESID_005.zip"})
        by_name = {Path(p).name: a for p, a in attempts.items()}
        self.assertEqual(by_name["ESID_005.zip"], 1)
        self.assertEqual(
            by_name["README.md"], uploader._PUT_RETRY_ATTEMPTS
        )

    def test_no_priority_files_keeps_apply_to_all_behavior(self):
        attempts = self._run(upload_attempts=1, priority_files=None)
        self.assertEqual(set(attempts.values()), {1})


class TestUploadDatasetWiring(unittest.TestCase):
    """upload_dataset passes every archive basename to upload_to_zenodo."""

    def test_archive_names_are_forwarded(self):
        import zipfile
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "ESID_005_Staging"
            folder.mkdir()
            zip_path = folder / "ESID_005.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("ESID_005/20240408_120000.WAV", b"\x00" * 64)
            collector = DataCollector.model_validate({
                "esid": "005",
                "affiliation": "Aff",
                "files_date_time_mode": "Automatic",
                "version": "2024.1.0",
                "latitude": "35.0",
                "longitude": "-106.0",
                "eclipse_date": "2024-04-08",
                "eclipse_type": "Total",
                "eclipse_coverage": "100",
                "eclipse_start_time_utc": "17:00:00",
                "eclipse_maximum_time_utc": "18:15:00",
                "subjects": "eclipse",
            })
            data = UploadData(
                esid="005", data_collector=collector,
                staging_folder=str(folder), archives=[str(zip_path)],
            )
            with mock.patch.object(tasks, "upload_to_zenodo") as up, \
                 mock.patch.object(tasks, "get_draft_config",
                                   return_value=mock.MagicMock()), \
                 mock.patch.object(tasks, "save_metadata_json"):
                up.return_value = {"successful": True, "api_response": {},
                                   "error": None}
                result = tasks.upload_dataset(
                    data=data,
                    project_config={"title_template": "ESID#$esid"},
                )
        self.assertTrue(result["successful"])
        self.assertEqual(
            up.call_args.kwargs["priority_files"], {"ESID_005.zip"}
        )


if __name__ == "__main__":
    unittest.main()
