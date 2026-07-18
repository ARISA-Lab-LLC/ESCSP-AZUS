"""Unit tests for the duplicate-title guard in standalone_uploader.py.

No network: every Zenodo API touchpoint (``_api_get_with_retry``,
``create_draft_record``, ``get_draft_record``, ``list_draft_files``,
``upload_file_to_draft``, and the ``requests`` module itself) is mocked
with fabricated payloads in both serializations Zenodo serves
(InvenioRDM ``is_published`` flag and legacy ``status`` field).

Proves the guard's safety guarantees:
  * ``_normalize_title`` — whitespace collapse + casefold equivalence.
  * ``_find_title_matches`` — exact-title gate over fuzzy search hits,
    dual-shape published/draft classification.
  * ``upload_to_zenodo`` guard block — an existing unpublished draft is
    adopted (resumed) instead of duplicated; a published record raises
    ``DuplicateTitleError`` (dataset fails, nothing created); a failed
    search fails CLOSED (no draft minted); ``title_guard=False`` skips
    the search entirely.
  * ``ensure_doi_reserved`` — idempotent: no reserve POST when a DOI
    exists; POST to the pids/doi endpoint when missing; HTTP 400
    ("already exists") is success; real HTTP failures propagate.

Run from the project root:

    python3 -m unittest discover -s tests -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "Resources"))

from requests.exceptions import HTTPError, RequestException  # noqa: E402

import standalone_uploader as uploader  # noqa: E402


_TITLE = "2024-04-08 Total Solar Eclipse ESID #073"
_BASE_URL = "https://zenodo.invalid/api/"  # .invalid TLD: can never resolve


# --- fabricated API payloads ----------------------------------------------

def invenio_hit(record_id, title, is_published, doi=None):
    """A search hit in the InvenioRDM shape (/api/user/records)."""
    return {
        "id": record_id,
        "is_published": is_published,
        "status": "published" if is_published else "draft",
        "metadata": {"title": title},
        "pids": {"doi": {"identifier": doi}} if doi else {},
    }


def legacy_hit(record_id, title, status="published", doi=None):
    """A search hit in the legacy shape: top-level title, status only."""
    return {"id": record_id, "status": status, "title": title, "doi": doi}


class FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, payload=None, status_code=200, text=""):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise HTTPError(f"HTTP {self.status_code}", response=self)


def make_config(title=_TITLE):
    """A minimal DraftConfig-shaped object for upload_to_zenodo."""
    return SimpleNamespace(
        metadata={"title": title},
        record_access="public",
        files_access="public",
        files_enabled=True,
        community_id=None,
        custom_fields=None,
        pids=None,
    )


class _PatchingTestCase(unittest.TestCase):
    """Shared helper: patch an attribute on the uploader module for the
    duration of one test, auto-reverted via addCleanup."""

    def _patch(self, name, **kwargs):
        patcher = mock.patch.object(uploader, name, **kwargs)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked


# --- _normalize_title -------------------------------------------------------

class TestNormalizeTitle(unittest.TestCase):
    def test_whitespace_collapse(self):
        self.assertEqual(
            uploader._normalize_title("  2024-04-08 \t Total\nSolar   Eclipse "),
            "2024-04-08 total solar eclipse",
        )

    def test_casefold_equivalence_classes(self):
        """casefold(), not lower(): 'ß' and 'SS' land in the same class,
        so a re-typed title survives the comparison either way."""
        self.assertEqual(
            uploader._normalize_title("Große Straße"),
            uploader._normalize_title("GROSSE STRASSE"),
        )

    def test_equivalent_variants_normalize_identically(self):
        variants = [
            _TITLE,
            _TITLE.upper(),
            f"  {_TITLE}  ",
            _TITLE.replace(" ", "   "),
        ]
        normalized = {uploader._normalize_title(v) for v in variants}
        self.assertEqual(len(normalized), 1)


# --- _find_title_matches -----------------------------------------------------

class TestFindTitleMatches(unittest.TestCase):
    def test_invenio_serialization_split(self):
        hits = [
            invenio_hit("p1", _TITLE, is_published=True),
            invenio_hit("d1", _TITLE, is_published=False),
        ]
        drafts, published = uploader._find_title_matches(hits, _TITLE)
        self.assertEqual([h["id"] for h in drafts], ["d1"])
        self.assertEqual([h["id"] for h in published], ["p1"])

    def test_legacy_serialization_uses_status_field(self):
        hits = [
            legacy_hit("p1", _TITLE, status="published"),
            legacy_hit("d1", _TITLE, status="draft"),
        ]
        drafts, published = uploader._find_title_matches(hits, _TITLE)
        self.assertEqual([h["id"] for h in drafts], ["d1"])
        self.assertEqual([h["id"] for h in published], ["p1"])

    def test_is_published_flag_wins_over_status(self):
        """When both fields are present, ``is_published`` is authoritative
        (the dual-shape mapping from Resources/find_duplicate_records.py)."""
        hit = {
            "id": "x",
            "is_published": False,
            "status": "published",
            "metadata": {"title": _TITLE},
        }
        drafts, published = uploader._find_title_matches([hit], _TITLE)
        self.assertEqual([h["id"] for h in drafts], ["x"])
        self.assertEqual(published, [])

    def test_near_miss_titles_do_not_match(self):
        """The search API is a fuzzy candidate fetch — only an EXACT
        normalized title counts as a duplicate."""
        hits = [
            invenio_hit("a", _TITLE + " (v2)", is_published=True),
            invenio_hit("b", _TITLE[:-1], is_published=True),   # ...#07
            invenio_hit("c", _TITLE + "0", is_published=False),  # ...#0730
            {"id": "d", "status": "published"},                  # no title
        ]
        drafts, published = uploader._find_title_matches(hits, _TITLE)
        self.assertEqual(drafts, [])
        self.assertEqual(published, [])

    def test_whitespace_and_case_variants_match(self):
        hit = invenio_hit(
            "p1", "  2024-04-08  TOTAL  Solar eclipse ESID #073\n",
            is_published=True,
        )
        drafts, published = uploader._find_title_matches([hit], _TITLE)
        self.assertEqual(drafts, [])
        self.assertEqual([h["id"] for h in published], ["p1"])


# --- upload_to_zenodo guard behavior -----------------------------------------

class TestUploadTitleGuard(_PatchingTestCase):
    """Behavior of upload_to_zenodo's duplicate guard, fully offline.

    Every network function is replaced; ``uploader.requests`` itself is
    swapped for a tripwire that fails the test on any direct HTTP call.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.data_file = self.root / "ESID_073.zip"
        self.data_file.write_bytes(b"zip-bytes")

        self.creds = uploader.Credentials(token="fake", base_url=_BASE_URL)
        self._patch("get_credentials_from_env", return_value=self.creds)
        self.search = self._patch("_api_get_with_retry")
        self.create = self._patch("create_draft_record")
        self.get_draft = self._patch("get_draft_record")
        self.list_files = self._patch("list_draft_files")
        self.list_files.return_value = []
        self.put_file = self._patch("upload_file_to_draft")
        self._patch("logger")  # keep test output clean

        # Hermetic tripwire: nothing in these paths may touch requests.
        tripwire = mock.MagicMock()
        for verb in ("get", "post", "put", "delete"):
            getattr(tripwire, verb).side_effect = AssertionError(
                f"unexpected direct requests.{verb} call"
            )
        self._patch("requests", new=tripwire)

    def _search_returns(self, hits):
        self.search.return_value = FakeResponse({"hits": {"hits": hits}})

    def _run(self, **kwargs):
        return uploader.upload_to_zenodo(
            files=[str(self.data_file)], config=make_config(), **kwargs
        )

    def test_matching_unpublished_draft_is_adopted(self):
        """One same-title draft on the account -> resume it: no new draft,
        files land on the existing one, state file records the adoption."""
        self._search_returns([invenio_hit("d-77", _TITLE, is_published=False)])
        self.get_draft.return_value = {
            "id": "d-77", "is_published": False, "parent": {},
        }
        state_path = self.root / "upload_state.json"

        result = self._run(state_file_path=str(state_path))

        self.assertTrue(result["successful"])
        self.create.assert_not_called()
        self.get_draft.assert_called_once_with(self.creds, "d-77")
        self.put_file.assert_called_once()
        self.assertEqual(self.put_file.call_args.args[1], "d-77")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["record_id"], "d-77")
        self.assertTrue(state["resumed"])

    def test_matching_published_record_fails_without_creating(self):
        self._search_returns(
            [invenio_hit("pub-1", _TITLE, is_published=True,
                         doi="10.5281/zenodo.101")]
        )
        result = self._run()
        self.assertFalse(result["successful"])
        self.assertEqual(result["error"]["type"], "DuplicateTitleError")
        self.assertIn("pub-1", result["error"]["error_message"])
        self.create.assert_not_called()
        self.put_file.assert_not_called()
        self.get_draft.assert_not_called()

    def test_multiple_matching_drafts_fail_without_creating(self):
        """Two same-title drafts is already a mess — refusing to add a
        third is the only safe move."""
        self._search_returns([
            invenio_hit("d-1", _TITLE, is_published=False),
            invenio_hit("d-2", _TITLE, is_published=False),
        ])
        result = self._run()
        self.assertFalse(result["successful"])
        self.assertEqual(result["error"]["type"], "DuplicateTitleError")
        self.create.assert_not_called()
        self.put_file.assert_not_called()

    def test_search_failure_fails_closed(self):
        """A broken duplicate search must NOT fall through to draft
        creation — a missed guard mints a permanent duplicate, while a
        failed run is retryable."""
        self.search.side_effect = RequestException("Zenodo search down")
        result = self._run()
        self.assertFalse(result["successful"])
        self.assertEqual(result["error"]["type"], "RequestException")
        self.create.assert_not_called()
        self.put_file.assert_not_called()

    def test_title_guard_false_skips_search_entirely(self):
        self.create.return_value = {"id": "new-9"}
        result = self._run(title_guard=False)
        self.assertTrue(result["successful"])
        self.search.assert_not_called()
        self.create.assert_called_once()
        self.assertEqual(self.put_file.call_args.args[1], "new-9")

    def test_no_matches_proceeds_to_create(self):
        self._search_returns([])
        self.create.return_value = {"id": "new-1"}
        result = self._run()
        self.assertTrue(result["successful"])
        self.search.assert_called_once()
        self.assertEqual(
            self.search.call_args.kwargs["url"], f"{_BASE_URL}user/records"
        )
        self.create.assert_called_once()

    def test_near_miss_search_hits_do_not_block_creation(self):
        """Fuzzy search may return similar titles; only an exact match
        engages the guard."""
        self._search_returns(
            [invenio_hit("x", _TITLE + " (Copy)", is_published=True)]
        )
        self.create.return_value = {"id": "new-2"}
        result = self._run()
        self.assertTrue(result["successful"])
        self.create.assert_called_once()

    def test_explicit_existing_draft_id_skips_guard(self):
        """A local resume pointer means there is nothing to guard against."""
        self.get_draft.return_value = {
            "id": "d-5", "is_published": False, "parent": {},
        }
        result = self._run(existing_draft_id="d-5")
        self.assertTrue(result["successful"])
        self.search.assert_not_called()
        self.create.assert_not_called()


# --- ensure_doi_reserved ------------------------------------------------------

class TestEnsureDoiReserved(_PatchingTestCase):
    def setUp(self):
        self.creds = uploader.Credentials(token="fake", base_url=_BASE_URL)
        self.requests = self._patch("requests")
        self.get_draft = self._patch("get_draft_record")
        self._patch("logger")

    def test_doi_in_supplied_metadata_no_post(self):
        doi = uploader.ensure_doi_reserved(
            self.creds, "42",
            {"pids": {"doi": {"identifier": "10.5281/zenodo.42"}}},
        )
        self.assertEqual(doi, "10.5281/zenodo.42")
        self.requests.post.assert_not_called()
        self.get_draft.assert_not_called()

    def test_doi_found_on_refetch_no_post(self):
        """Caller passed no metadata: the draft is re-fetched, and the
        DOI found there suppresses the reserve call (idempotency)."""
        self.get_draft.return_value = {
            "pids": {"doi": {"identifier": "10.5281/zenodo.7"}}
        }
        doi = uploader.ensure_doi_reserved(self.creds, "7", None)
        self.assertEqual(doi, "10.5281/zenodo.7")
        self.requests.post.assert_not_called()

    def test_missing_doi_posts_to_reserve_endpoint(self):
        self.get_draft.return_value = {"pids": {}}
        self.requests.post.return_value = FakeResponse(
            {"pids": {"doi": {"identifier": "10.5281/zenodo.999"}}},
            status_code=201,
        )
        doi = uploader.ensure_doi_reserved(self.creds, "55", {"pids": {}})
        self.assertEqual(doi, "10.5281/zenodo.999")
        self.requests.post.assert_called_once()
        self.assertEqual(
            self.requests.post.call_args.args[0],
            f"{_BASE_URL}records/55/draft/pids/doi",
        )

    def test_http_400_already_exists_is_success(self):
        """InvenioRDM answers 400 when a DOI already exists — the goal
        state ('draft has a DOI') is met, so no exception."""
        self.get_draft.return_value = {"pids": {}}
        self.requests.post.return_value = FakeResponse(
            status_code=400, text="A DOI already exists."
        )
        doi = uploader.ensure_doi_reserved(self.creds, "55", None)
        self.assertIsNone(doi)

    def test_real_http_failure_raises(self):
        self.get_draft.return_value = {"pids": {}}
        self.requests.post.return_value = FakeResponse(
            status_code=403, text="forbidden"
        )
        with self.assertRaises(HTTPError):
            uploader.ensure_doi_reserved(self.creds, "55", None)

    def test_broken_draft_endpoint_still_reserves(self):
        """A broken GET /draft must not block reservation: the reserve
        POST is attempted anyway and its 400 is tolerated."""
        self.get_draft.side_effect = RequestException("GET /draft is 500ing")
        self.requests.post.return_value = FakeResponse(
            status_code=400, text="A DOI already exists."
        )
        doi = uploader.ensure_doi_reserved(self.creds, "66", None)
        self.assertIsNone(doi)
        self.requests.post.assert_called_once()


if __name__ == "__main__":
    unittest.main()


# --- number_of_tries attempt counter -----------------------------------------

class TestReadNumberOfTries(unittest.TestCase):
    """Unit behavior of the upload_state.json attempt-counter reader."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name) / "upload_state.json"

    def test_missing_file_counts_from_zero(self):
        self.assertEqual(uploader._read_number_of_tries(self.state), 0)

    def test_legacy_file_without_field_counts_from_zero(self):
        self.state.write_text(json.dumps({"record_id": "77"}))
        self.assertEqual(uploader._read_number_of_tries(self.state), 0)

    def test_existing_count_returned(self):
        self.state.write_text(json.dumps({"number_of_tries": 3}))
        self.assertEqual(uploader._read_number_of_tries(self.state), 3)

    def test_corrupt_json_counts_from_zero(self):
        self.state.write_text("{not json")
        self.assertEqual(uploader._read_number_of_tries(self.state), 0)

    def test_bad_value_counts_from_zero(self):
        self.state.write_text(json.dumps({"number_of_tries": "many"}))
        self.assertEqual(uploader._read_number_of_tries(self.state), 0)
        self.state.write_text(json.dumps({"number_of_tries": -5}))
        self.assertEqual(uploader._read_number_of_tries(self.state), 0)


class TestNumberOfTriesCounter(_PatchingTestCase):
    """Every upload attempt advances number_of_tries in upload_state.json."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.data_file = self.root / "ESID_073.zip"
        self.data_file.write_bytes(b"zip-bytes")
        self.state_path = self.root / "upload_state.json"

        self.creds = uploader.Credentials(token="fake", base_url=_BASE_URL)
        self._patch("get_credentials_from_env", return_value=self.creds)
        self.search = self._patch("_api_get_with_retry")
        self.search.return_value = FakeResponse({"hits": {"hits": []}})
        self.create = self._patch("create_draft_record")
        self.create.return_value = {"id": "new-1"}
        self.get_draft = self._patch("get_draft_record")
        self.list_files = self._patch("list_draft_files")
        self.list_files.return_value = []
        self._patch("upload_file_to_draft")
        self._patch("logger")
        tripwire = mock.MagicMock()
        for verb in ("get", "post", "put", "delete"):
            getattr(tripwire, verb).side_effect = AssertionError(
                f"unexpected direct requests.{verb} call"
            )
        self._patch("requests", new=tripwire)

    def _state(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def _run(self, **kwargs):
        return uploader.upload_to_zenodo(
            files=[str(self.data_file)], config=make_config(),
            state_file_path=str(self.state_path), **kwargs,
        )

    def test_first_attempt_writes_one(self):
        """Initial value is 0; the first attempt advances it to 1."""
        self.assertTrue(self._run()["successful"])
        self.assertEqual(self._state()["number_of_tries"], 1)

    def test_each_resume_advances_the_counter(self):
        self.get_draft.return_value = {
            "id": "new-1", "is_published": False, "parent": {},
        }
        self._run()
        self.assertEqual(self._state()["number_of_tries"], 1)
        self._run(existing_draft_id="new-1")
        self.assertEqual(self._state()["number_of_tries"], 2)
        self._run(existing_draft_id="new-1")
        self.assertEqual(self._state()["number_of_tries"], 3)

    def test_legacy_state_file_gains_the_field_at_one(self):
        """A pre-field state file (older AZUS) is treated as 0 and the
        field is created on the next attempt."""
        self.state_path.write_text(json.dumps({
            "record_id": "new-1",
            "created_at": "2026-01-01T00:00:00",
            "zenodo_url": "https://zenodo.org/uploads/new-1",
            "resumed": False,
        }))
        self.get_draft.return_value = {
            "id": "new-1", "is_published": False, "parent": {},
        }
        self._run(existing_draft_id="new-1")
        state = self._state()
        self.assertEqual(state["number_of_tries"], 1)
        self.assertEqual(state["record_id"], "new-1")
