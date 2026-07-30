"""Unit tests for Resources/new_version_upload.py.

This tool creates a PUBLISHED Zenodo version, which is permanent, so the
tests are organised by the safety property each class guarantees rather
than by function.  The load-bearing ones are TestPutPayloadShape (an
omitted key on a full-replace PUT silently strips a reserved DOI),
TestExecuteOrdering (the calls that must never happen), and TestStateFile
(the cross-tool pin that stops finish_stuck_uploads.py hijacking a
half-finished new-version draft).

Fully offline: every Zenodo call is mocked and the base URL is a .invalid
host that can never resolve.

Run from the project root:

    python3 -m unittest discover -s tests -v
"""

import csv
import json
import logging
import os
import sys
import tempfile
import pathlib
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from requests.exceptions import HTTPError, RequestException

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "Resources"))

import azus_common  # noqa: E402
import new_version_upload as nvu  # noqa: E402
import standalone_uploader as uploader  # noqa: E402

_ESID = "073"
_RECORD = "15234567"
_NEW_RECORD = "15311234"
_TITLE = "2024-04-08 Total Solar Eclipse ESID#073"
_OLD_LABEL = "2024.1.0"
_NEW_LABEL = "2024.1.0a"
_CONCEPT_DOI = "10.5281/zenodo.15234566"
_TODAY = datetime.now().strftime("%Y-%m-%d")
_BASE_URL = "https://zenodo.invalid/api/"  # .invalid TLD: can never resolve
_FAKE_ENV = {
    "INVENIO_RDM_ACCESS_TOKEN": "unit-test-token",
    "INVENIO_RDM_BASE_URL": _BASE_URL,
}
_PACKAGE = ("ESID_073.zip", "README.md", "file_list.csv", "License.txt")

# Fields Zenodo dumps but will not accept back on a load.  Echoing any of
# these into the PUT body would 400 the request.
_DUMP_ONLY = (
    "id", "created", "updated", "links", "revision_id", "versions",
    "status", "is_published", "is_draft", "expires_at", "stats", "ui",
    "expanded", "media_files", "errors",
)

_UNSET = object()


def setUpModule():
    """Silence the tool's logging for the whole module."""
    logging.disable(logging.CRITICAL)


def tearDownModule():
    """Restore logging."""
    logging.disable(logging.NOTSET)


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


def published_record(**over):
    """A published record body in the InvenioRDM shape."""
    body = {
        "id": _RECORD,
        "is_published": True,
        "versions": {"index": 1, "is_latest": True, "is_latest_draft": True},
        "metadata": {
            "title": _TITLE, "version": _OLD_LABEL,
            "publication_date": "2024-04-12",
            "description": "the old description" + "x" * 400,
        },
        "pids": {"doi": {"identifier": "10.5281/zenodo.15234567"}},
        "parent": {
            "pids": {"doi": {"identifier": _CONCEPT_DOI}},
            "communities": {"ids": ["comm-uuid"]},
        },
        "files": {"entries": {"ESID_073.zip": {}, "OLD_ONLY.csv": {}}},
    }
    for key, value in over.items():
        if key in ("metadata", "versions"):
            body[key] = {**body[key], **value}
        else:
            body[key] = value
    return body


def version_draft(**over):
    """The body POST /records/{id}/versions returns, dump-only fields included."""
    body = {
        "id": _NEW_RECORD,
        "access": {"record": "public", "files": "public"},
        "files": {"enabled": True},
        "custom_fields": {"code:developmentStatus": {"id": "wip"}},
        "pids": {"doi": {"identifier": "", "provider": "datacite"}},
        "metadata": {"title": _TITLE, "description": "inherited"},
        # Dump-only / not-ours fields that must NOT reach the PUT body:
        "links": {"self": "https://zenodo.invalid/api/records/1/draft"},
        "created": "2026-07-28T09:00:00", "updated": "2026-07-28T09:00:00",
        "revision_id": 3, "status": "draft", "is_published": False,
        "is_draft": True, "versions": {"index": 2, "is_latest": False},
        "parent": {"id": "parent-uuid", "communities": {"ids": ["comm-uuid"]}},
    }
    body.update(over)
    return body


def rebuilt_metadata(**over):
    """The metadata get_draft_config would produce from the new package."""
    meta = {
        "title": _TITLE,
        "version": _OLD_LABEL,          # from the collectors CSV
        "publication_date": _TODAY,     # recomputed at build time
        "description": "the CORRECTED description" + "y" * 400,
        "publisher": "Zenodo",
    }
    meta.update(over)
    return meta


def committed(name, size):
    """A completed draft file entry."""
    return {"key": name, "status": "completed", "size": size}


class _Case(unittest.TestCase):
    """Fixture: a temp Staging_Area / Uploaded_Data plus a minimal config."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.staging_area = self.root / "Staging_Area"
        self.uploaded = self.root / "Uploaded_Data"
        for folder in (self.staging_area, self.uploaded):
            folder.mkdir()
        for name, value in (("_STAGING_AREA", self.staging_area),
                            ("_UPLOADED_DATA", self.uploaded)):
            patcher = mock.patch.object(nvu, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.staging = self.staging_area / f"ESID_{_ESID}_Staging"
        self.report = self.root / "report.csv"
        self.config_json = self.root / "config.json"
        self.config_json.write_text(json.dumps({
            "project_config": None,
            "uploads": {"datasets": [{
                "collectors_csv": str(self.root / "collectors.csv"),
                "dataset_category": "Total",
            }]},
        }))

    def build_package(self, files=_PACKAGE, sentinel=True, state=None):
        """Create a plausible re-prepped staging folder."""
        self.staging.mkdir(exist_ok=True)
        for name in files:
            (self.staging / name).write_bytes(f"contents of {name}\n".encode())
        (self.staging / "README.html").write_text("<p>desc</p>")
        with open(self.staging / f"ESID_{_ESID}_to_upload.csv", "w",
                  newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=["File Name", "Notes"])
            writer.writeheader()
            for name in files:
                writer.writerow({"File Name": name, "Notes": ""})
        if sentinel:
            (self.staging / azus_common.PREP_SENTINEL).write_text("")
        if state is not None:
            (self.staging / azus_common.STATE_FILENAME).write_text(
                json.dumps(state)
            )
        return self.staging

    def committed_package(self):
        """Draft entries matching every file actually on disk."""
        return [
            committed(name, (self.staging / name).stat().st_size)
            for name in _PACKAGE if (self.staging / name).is_file()
        ]

    def patch_all(self, *, published=_UNSET, integrity=(), drafts=(),
                  published_hits=(), upload_ok=True, draft_files=_UNSET,
                  readback=_UNSET, new_draft=_UNSET, empty_check=None,
                  adopted=False):
        """Patch every collaborator the tool imported. Returns the mocks.

        Pass ``adopted=True`` for a resume run: the emptiness check is
        skipped on that path, so list_draft_files is called once (the
        completeness gate) rather than twice.
        """
        if published is _UNSET:
            published = published_record()
        if readback is _UNSET:
            readback = {"metadata": {
                "version": _NEW_LABEL, "title": _TITLE,
                "publication_date": _TODAY,
            }}
        if new_draft is _UNSET:
            new_draft = version_draft()
        if draft_files is _UNSET:
            draft_files = self.committed_package()

        def fake_integrity(staging_folder, esid, archives=None,
                           verify_zip_hash=True, digests_out=None):
            if digests_out is not None and verify_zip_hash:
                for archive in archives or []:
                    digests_out[pathlib.Path(archive).name] = {
                        "md5": "deadbeef", "sha512": "cafe",
                    }
            return list(integrity)

        # The fresh path calls list_draft_files twice — emptiness check,
        # then the completeness gate.  The adopted path skips the first.
        listings = [draft_files] if adopted else [
            empty_check if empty_check is not None else [], draft_files
        ]

        specs = {
            "verify_dataset_integrity": dict(side_effect=fake_integrity),
            "load_project_config": dict(return_value={}),
            "parse_collectors_csv": dict(
                return_value=[mock.Mock(esid=_ESID, version=_OLD_LABEL)]),
            "get_draft_config": dict(
                return_value=mock.Mock(metadata=rebuilt_metadata())),
            "get_published_record": dict(return_value=published),
            "_search_drafts_by_title": dict(
                return_value=(list(drafts), list(published_hits))),
            "create_new_version_draft": dict(return_value=new_draft),
            "update_draft_metadata": dict(return_value={}),
            "get_draft_record": dict(return_value=readback),
            "list_draft_files": dict(side_effect=listings),
            "upload_to_zenodo": dict(return_value={
                "successful": upload_ok, "api_response": {},
                "error": None if upload_ok else {"error_message": "timeout"},
            }),
            "ensure_doi_reserved": dict(return_value="10.5281/zenodo.999"),
            "publish_draft": dict(return_value={}),
        }
        started = {}
        for name, kwargs in specs.items():
            patcher = mock.patch.object(nvu, name, **kwargs)
            started[name] = patcher.start()
            self.addCleanup(patcher.stop)
        return started

    def tripwire(self, *names):
        """Make the named tool collaborators fail the test if called."""
        for name in names:
            patcher = mock.patch.object(
                nvu, name,
                side_effect=AssertionError(f"{name}() must not be called"),
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_main(self, *extra):
        """Run main() and return (exit_code, report_row)."""
        argv = ["new_version_upload.py", "--esid", _ESID,
                "--record-id", _RECORD, "--config", str(self.config_json),
                "--output", str(self.report), *extra]
        env = mock.patch.dict(os.environ, _FAKE_ENV)
        env.start()
        self.addCleanup(env.stop)
        with mock.patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit) as ctx:
                nvu.main()
        row = {}
        if self.report.exists():
            with open(self.report, newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
                row = rows[0] if rows else {}
        return ctx.exception.code, row


# --- pure: the version label ---------------------------------------------

class TestBumpVersionLabel(unittest.TestCase):
    """Advance a label or refuse — never invent a convention."""

    def test_appends_first_revision_letter(self):
        self.assertEqual(nvu.bump_version_label("2024.1.0"), "2024.1.0a")

    def test_advances_an_existing_letter(self):
        self.assertEqual(nvu.bump_version_label("2024.1.0a"), "2024.1.0b")
        self.assertEqual(nvu.bump_version_label("2024.1.0y"), "2024.1.0z")

    def test_non_semver_stems_still_bump(self):
        for value, expected in (("1", "1a"), ("2024-04-08", "2024-04-08a"),
                                ("2024.1.0-rc1", "2024.1.0-rc1a")):
            self.assertEqual(nvu.bump_version_label(value), expected)

    def test_surrounding_whitespace_is_stripped_not_bumped(self):
        self.assertEqual(nvu.bump_version_label("  2024.1.0  "), "2024.1.0a")

    def test_per_day_marker_starts_the_ladder_after_itself(self):
        """2024.1.0A is a per-day BASE version, not a revision letter.

        Until July 2026 an uppercase ending was refused outright; the
        per-day prep now marks its versions with a trailing "A"
        (prepare_dataset.DAY_ZIP_VERSION_SUFFIX), and bumping such a
        record must keep the marker and revise after it.
        """
        self.assertEqual(nvu.bump_version_label("2024.1.0A"), "2024.1.0Aa")
        self.assertEqual(nvu.bump_version_label("2024.1.0Aa"), "2024.1.0Ab")
        self.assertEqual(nvu.bump_version_label("2024.1.0Ay"), "2024.1.0Az")

    def test_the_marker_constant_is_the_producers(self):
        import prepare_dataset
        self.assertEqual(
            prepare_dataset.DAY_ZIP_VERSION_SUFFIX, "A"
        )

    def test_refusals(self):
        for value in ("2024.1.0z", "2024.1.0Az", "2024.1.0B", "2024.1.0ab",
                      "2024.1.0Aab", "1.0-beta", "", "   ", None, "v",
                      "draft", "abc"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    nvu.bump_version_label(value)

    def test_multi_letter_refusal_stops_beta_becoming_betb(self):
        with self.assertRaises(ValueError) as ctx:
            nvu.bump_version_label("1.0-beta")
        self.assertIn("more than one letter", str(ctx.exception))

    def test_every_refusal_names_the_escape_hatch(self):
        for value in ("2024.1.0z", "2024.1.0Az", "2024.1.0B", "2024.1.0ab",
                      "1.0-beta", "", None, "v"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as ctx:
                    nvu.bump_version_label(value)
                self.assertIn("--version-label", str(ctx.exception))


# --- pure: the PUT body ---------------------------------------------------

class TestPutPayloadShape(unittest.TestCase):
    """A full-replace PUT must carry every key that has to survive."""

    def setUp(self):
        self.draft = version_draft()
        self.meta = rebuilt_metadata(version=_NEW_LABEL)
        self.payload = nvu.build_put_payload(self.draft, self.meta)

    def test_echoes_pids_verbatim(self):
        self.assertEqual(self.payload["pids"], self.draft["pids"])

    def test_pids_key_present_even_when_the_draft_has_none(self):
        """Omitting pids on a full replace would strip a reserved DOI."""
        payload = nvu.build_put_payload(
            version_draft(pids={}), self.meta
        )
        self.assertIn("pids", payload)
        payload2 = nvu.build_put_payload(
            {k: v for k, v in version_draft().items() if k != "pids"}, self.meta
        )
        self.assertIn("pids", payload2)

    def test_files_enabled_is_carried(self):
        self.assertEqual(self.payload["files"], {"enabled": True})

    def test_access_and_custom_fields_echoed(self):
        self.assertEqual(self.payload["access"], self.draft["access"])
        self.assertEqual(self.payload["custom_fields"],
                         self.draft["custom_fields"])

    def test_metadata_is_the_rebuilt_one_with_the_bumped_version(self):
        self.assertEqual(self.payload["metadata"], self.meta)
        self.assertEqual(self.payload["metadata"]["version"], _NEW_LABEL)
        self.assertEqual(self.payload["metadata"]["publication_date"], _TODAY)

    def test_parent_is_never_sent(self):
        self.assertNotIn("parent", self.payload)

    def test_no_dump_only_fields(self):
        for key in _DUMP_ONLY:
            self.assertNotIn(key, self.payload, key)

    def test_only_the_allowed_keys_appear(self):
        self.assertEqual(set(self.payload), set(nvu._PUT_ALLOWED_KEYS))


# --- pure: the metadata diff ---------------------------------------------

class TestMetadataDiff(unittest.TestCase):
    """The diff is the operator's evidence that only the intended fix moved."""

    def test_classifies_every_key(self):
        rows = dict(
            (key, verdict) for key, verdict, _detail
            in nvu.metadata_diff(
                {"title": "T", "version": "1", "gone": "x"},
                {"title": "T", "version": "2", "fresh": "y"},
            )
        )
        self.assertEqual(rows["title"], "same")
        self.assertEqual(rows["version"], "CHANGED")
        self.assertEqual(rows["gone"], "removed")
        self.assertEqual(rows["fresh"], "added")

    def test_long_strings_report_length_and_first_difference(self):
        old = "a" * 300
        new = "a" * 100 + "b" + "a" * 199
        [(_key, verdict, detail)] = nvu.metadata_diff(
            {"description": old}, {"description": new}
        )
        self.assertEqual(verdict, "CHANGED")
        self.assertIn("first difference at offset 100", detail)


# --- the three new uploader primitives -----------------------------------

class TestNewVersionPrimitives(unittest.TestCase):
    """The REST layer: correct URLs, and no blind retry of a create."""

    def setUp(self):
        self.creds = uploader.Credentials(token="t", base_url=_BASE_URL)
        patcher = mock.patch.object(uploader.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_get_published_record_url_and_404(self):
        with mock.patch.object(
            uploader, "_api_get_with_retry", return_value=None
        ) as get:
            self.assertIsNone(
                uploader.get_published_record(self.creds, _RECORD)
            )
        self.assertEqual(get.call_args.kwargs["url"],
                         f"{_BASE_URL}records/{_RECORD}")
        self.assertTrue(get.call_args.kwargs["allow_404"])

    def test_get_published_record_returns_body(self):
        with mock.patch.object(
            uploader, "_api_get_with_retry",
            return_value=FakeResponse(published_record()),
        ):
            body = uploader.get_published_record(self.creds, _RECORD)
        self.assertEqual(body["id"], _RECORD)

    def test_create_new_version_posts_to_versions_with_no_body(self):
        with mock.patch.object(
            uploader.requests, "post",
            return_value=FakeResponse(version_draft()),
        ) as post:
            body = uploader.create_new_version_draft(self.creds, _RECORD)
        self.assertEqual(body["id"], _NEW_RECORD)
        self.assertEqual(post.call_args.args[0],
                         f"{_BASE_URL}records/{_RECORD}/versions")
        self.assertNotIn("json", post.call_args.kwargs)
        self.assertNotIn("data", post.call_args.kwargs)

    def test_create_new_version_is_never_retried(self):
        """A 5xx may still have created the draft; a retry could not tell."""
        with mock.patch.object(
            uploader.requests, "post",
            return_value=FakeResponse(status_code=500, text="boom"),
        ) as post:
            with self.assertRaises(HTTPError):
                uploader.create_new_version_draft(self.creds, _RECORD)
        self.assertEqual(post.call_count, 1)

    def test_update_draft_metadata_puts_the_payload_untouched(self):
        payload = {"metadata": {"version": _NEW_LABEL}, "pids": {}}
        with mock.patch.object(
            uploader.requests, "put", return_value=FakeResponse({"ok": True}),
        ) as put:
            uploader.update_draft_metadata(self.creds, _NEW_RECORD, payload)
        self.assertEqual(put.call_args.args[0],
                         f"{_BASE_URL}records/{_NEW_RECORD}/draft")
        self.assertEqual(put.call_args.kwargs["json"], payload)

    def test_update_draft_metadata_retries_5xx_then_succeeds(self):
        with mock.patch.object(
            uploader.requests, "put",
            side_effect=[FakeResponse(status_code=503, text="down"),
                         FakeResponse({"ok": True})],
        ) as put:
            uploader.update_draft_metadata(self.creds, _NEW_RECORD, {})
        self.assertEqual(put.call_count, 2)

    def test_update_draft_metadata_does_not_retry_4xx(self):
        with mock.patch.object(
            uploader.requests, "put",
            return_value=FakeResponse(status_code=400, text="bad field"),
        ) as put:
            with self.assertRaises(HTTPError):
                uploader.update_draft_metadata(self.creds, _NEW_RECORD, {})
        self.assertEqual(put.call_count, 1)

    def test_update_draft_metadata_raises_after_exhausting_retries(self):
        with mock.patch.object(
            uploader.requests, "put",
            return_value=FakeResponse(status_code=500, text="down"),
        ) as put:
            with self.assertRaises(RequestException):
                uploader.update_draft_metadata(self.creds, _NEW_RECORD, {})
        self.assertEqual(put.call_count, uploader._DRAFT_PUT_ATTEMPTS)


# --- pre-flight gates ----------------------------------------------------

class TestPreflightGates(_Case):
    """Every refusal happens before anything is created."""

    def _expect(self, verdict, *extra):
        self.tripwire("create_new_version_draft", "update_draft_metadata",
                      "upload_to_zenodo", "publish_draft")
        code, row = self.run_main("--execute", "--yes", *extra)
        self.assertEqual(row["Verdict"], verdict)
        self.assertEqual(code, 1)
        return row

    def test_missing_staging_folder(self):
        self.patch_all()
        self._expect(nvu.NO_STAGING_FOLDER)

    def test_missing_sentinel(self):
        self.build_package(sentinel=False)
        self.patch_all()
        self._expect(nvu.PREP_INCOMPLETE)

    def test_no_zip(self):
        self.build_package(files=("README.md",))
        self.patch_all()
        self._expect(nvu.ZIP_AMBIGUOUS)

    def test_staging_is_a_stuck_first_upload(self):
        self.build_package(state={"record_id": "999"})
        self.patch_all()
        self._expect(nvu.STAGING_IS_A_FIRST_UPLOAD)

    def test_staging_is_file_by_file(self):
        self.build_package(state={"mode": azus_common.FILE_BY_FILE_MODE})
        self.patch_all()
        self._expect(nvu.STAGING_IS_FILE_BY_FILE)

    def test_local_integrity_failure(self):
        self.build_package()
        self.patch_all(integrity=["ZIP is missing 3 WAV(s)"])
        row = self._expect(nvu.INTEGRITY_FAILED)
        self.assertIn("missing 3 WAV", row["Notes"])

    def test_no_collector_row(self):
        self.build_package()
        mocks = self.patch_all()
        mocks["parse_collectors_csv"].return_value = [
            mock.Mock(esid="999", version="1")
        ]
        self._expect(nvu.NO_COLLECTOR_ROW)

    def test_record_not_found(self):
        self.build_package()
        self.patch_all(published=None)
        self._expect(nvu.RECORD_NOT_FOUND)

    def test_record_id_mismatch_catches_a_concept_id(self):
        self.build_package()
        self.patch_all(published=published_record(id="99999999"))
        row = self._expect(nvu.RECORD_ID_MISMATCH)
        self.assertIn("99999999", row["Notes"])

    def test_record_not_published(self):
        self.build_package()
        self.patch_all(published=published_record(is_published=False))
        self._expect(nvu.RECORD_NOT_PUBLISHED)

    def test_record_not_latest(self):
        self.build_package()
        self.patch_all(published=published_record(versions={"is_latest": False}))
        self._expect(nvu.RECORD_NOT_LATEST)

    def test_draft_already_open_on_the_chain(self):
        self.build_package()
        self.patch_all(
            published=published_record(versions={"is_latest_draft": False})
        )
        self._expect(nvu.DRAFT_ALREADY_OPEN)

    def test_title_mismatch_is_the_wrong_record_gate(self):
        self.build_package()
        mocks = self.patch_all()
        mocks["get_draft_config"].return_value = mock.Mock(
            metadata=rebuilt_metadata(title="2024-04-08 Total Solar Eclipse ESID#074")
        )
        row = self._expect(nvu.TITLE_MISMATCH)
        self.assertIn("074", row["Notes"])

    def test_title_change_can_be_allowed_explicitly(self):
        self.build_package()
        mocks = self.patch_all()
        mocks["get_draft_config"].return_value = mock.Mock(
            metadata=rebuilt_metadata(title="A deliberately new title")
        )
        code, row = self.run_main("--allow-title-change")
        self.assertEqual(row["Verdict"], nvu.VERSION_PLANNED)
        self.assertEqual(code, 0)

    def test_stray_draft_sharing_the_title(self):
        self.build_package()
        self.patch_all(drafts=[{"id": "stray-1"}])
        row = self._expect(nvu.ACCOUNT_SWEEP_UNCLEAN)
        self.assertIn("stray-1", row["Notes"])

    def test_two_published_records_sharing_the_title(self):
        self.build_package()
        self.patch_all(published_hits=[{"id": "a"}, {"id": "b"}])
        self._expect(nvu.ACCOUNT_SWEEP_UNCLEAN)

    def test_title_search_failure_fails_closed(self):
        self.build_package()
        mocks = self.patch_all()
        mocks["_search_drafts_by_title"].side_effect = ValueError("odd body")
        self._expect(nvu.ACCOUNT_SWEEP_UNCLEAN)

    def test_unbumpable_version_is_refused(self):
        self.build_package()
        self.patch_all(published=published_record(metadata={"version": "2024.1.0z"}))
        row = self._expect(nvu.VERSION_BUMP_REFUSED)
        self.assertIn("--version-label", row["Notes"])

    def test_version_label_override_rescues_an_unbumpable_version(self):
        self.build_package()
        self.patch_all(published=published_record(metadata={"version": "1.0-beta"}))
        code, row = self.run_main("--version-label", "1.0-beta2")
        self.assertEqual(row["Verdict"], nvu.VERSION_PLANNED)
        self.assertEqual(row["New Version"], "1.0-beta2")
        self.assertEqual(code, 0)

    def test_existing_archive_destination_is_refused(self):
        self.build_package()
        (self.uploaded / f"ESID_{_ESID}_Uploaded_{_NEW_LABEL}").mkdir()
        self.patch_all()
        self._expect(nvu.ARCHIVE_EXISTS)


# --- dry run -------------------------------------------------------------

class TestDryRunWritesNothing(_Case):
    """Without --execute, nothing on Zenodo or disk changes."""

    def test_no_write_primitive_is_called(self):
        self.build_package()
        self.patch_all()
        self.tripwire("create_new_version_draft", "update_draft_metadata",
                      "upload_to_zenodo", "publish_draft",
                      "ensure_doi_reserved")
        before = sorted(p.name for p in self.staging.iterdir())
        code, row = self.run_main()
        self.assertEqual(row["Verdict"], nvu.VERSION_PLANNED)
        self.assertEqual(row["Action Taken"], "none (dry run)")
        self.assertEqual(sorted(p.name for p in self.staging.iterdir()), before)
        self.assertEqual(list(self.uploaded.iterdir()), [])
        self.assertEqual(code, 0)

    def test_reports_the_version_plan_and_concept_doi(self):
        self.build_package()
        self.patch_all()
        _code, row = self.run_main()
        self.assertEqual(row["Previous Version"], _OLD_LABEL)
        self.assertEqual(row["New Version"], _NEW_LABEL)
        self.assertEqual(row["Concept DOI"], _CONCEPT_DOI)
        self.assertEqual(row["Published"], "no")

    def test_counts_the_metadata_changes(self):
        self.build_package()
        self.patch_all()
        _code, row = self.run_main()
        self.assertGreater(int(row["Metadata Changes"]), 0)

    def test_publish_requires_execute(self):
        self.build_package()
        self.patch_all()
        with mock.patch.object(sys, "argv", [
            "new_version_upload.py", "--esid", _ESID, "--record-id", _RECORD,
            "--config", str(self.config_json), "--publish",
        ]):
            with self.assertRaises(SystemExit) as ctx:
                nvu.main()
        self.assertEqual(ctx.exception.code, 2)

    def test_bad_base_url_is_a_usage_error(self):
        self.build_package()
        self.patch_all()
        env = mock.patch.dict(os.environ, {
            "INVENIO_RDM_ACCESS_TOKEN": "t",
            "INVENIO_RDM_BASE_URL": "https://zenodo.invalid/api",  # no slash
        })
        env.start()
        self.addCleanup(env.stop)
        with mock.patch.object(sys, "argv", [
            "new_version_upload.py", "--esid", _ESID, "--record-id", _RECORD,
            "--config", str(self.config_json),
        ]):
            with self.assertRaises(SystemExit) as ctx:
                nvu.main()
        self.assertEqual(ctx.exception.code, 2)


# --- execute -------------------------------------------------------------

class TestExecuteOrdering(_Case):
    """The right calls, in the right order — and never the wrong ones."""

    def test_happy_path_creates_a_draft_but_does_not_publish(self):
        self.build_package()
        mocks = self.patch_all()
        self.tripwire("publish_draft")
        code, row = self.run_main("--execute", "--yes")
        self.assertEqual(row["Verdict"], nvu.VERSION_CREATED)
        self.assertEqual(row["New Record ID"], _NEW_RECORD)
        self.assertEqual(row["New Version"], _NEW_LABEL)
        self.assertEqual(int(row["Files Uploaded"]), len(_PACKAGE))
        self.assertEqual(row["Published"], "no")
        mocks["create_new_version_draft"].assert_called_once()
        mocks["update_draft_metadata"].assert_called_once()
        self.assertEqual(code, 0)

    def test_upload_to_zenodo_kwargs_are_exact(self):
        self.build_package()
        mocks = self.patch_all()
        self.run_main("--execute", "--yes")
        kwargs = mocks["upload_to_zenodo"].call_args.kwargs
        self.assertEqual(kwargs["existing_draft_id"], _NEW_RECORD)
        self.assertFalse(kwargs["title_guard"])
        self.assertFalse(kwargs["auto_publish"])
        self.assertFalse(kwargs["submit_review"])
        self.assertIsNone(kwargs["state_file_path"])
        self.assertIsNone(kwargs["request_log_path"])

    def test_metadata_put_happens_before_the_doi_reserve_and_upload(self):
        self.build_package()
        mocks = self.patch_all()
        parent = mock.Mock()
        parent.attach_mock(mocks["update_draft_metadata"], "put")
        parent.attach_mock(mocks["upload_to_zenodo"], "upload")
        parent.attach_mock(mocks["ensure_doi_reserved"], "doi")
        self.run_main("--execute", "--yes")
        order = [name for name, _a, _k in parent.mock_calls]
        self.assertEqual(order, ["put", "upload", "doi"])

    def test_the_put_carries_the_bumped_version(self):
        self.build_package()
        mocks = self.patch_all()
        self.run_main("--execute", "--yes")
        payload = mocks["update_draft_metadata"].call_args.args[2]
        self.assertEqual(payload["metadata"]["version"], _NEW_LABEL)
        self.assertIn("pids", payload)
        self.assertNotIn("parent", payload)

    def test_publish_only_with_the_flag(self):
        self.build_package()
        mocks = self.patch_all()
        code, row = self.run_main("--execute", "--yes", "--publish")
        mocks["publish_draft"].assert_called_once_with(mock.ANY, _NEW_RECORD)
        self.assertEqual(row["Verdict"], nvu.VERSION_PUBLISHED)
        self.assertEqual(row["Published"], "yes")
        self.assertEqual(code, 0)

    def test_community_review_is_never_submitted(self):
        """A manager's accept on a re-submitted version would publish it."""
        self.build_package()
        self.patch_all()
        patcher = mock.patch.object(
            uploader, "submit_to_community_review",
            side_effect=AssertionError("must never be called"),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        _code, row = self.run_main("--execute", "--yes")
        self.assertEqual(row["Verdict"], nvu.VERSION_CREATED)

    def test_dump_payload_writes_the_exact_body(self):
        self.build_package()
        self.patch_all()
        target = self.root / "payload.json"
        self.run_main("--execute", "--yes", "--dump-payload", str(target))
        payload = json.loads(target.read_text())
        self.assertEqual(set(payload), set(nvu._PUT_ALLOWED_KEYS))
        self.assertEqual(payload["metadata"]["version"], _NEW_LABEL)

    def test_non_tty_without_yes_is_a_usage_error(self):
        self.build_package()
        self.patch_all()
        self.tripwire("create_new_version_draft")
        with mock.patch.object(sys.stdin, "isatty", return_value=False):
            code, _row = self.run_main("--execute")
        self.assertEqual(code, 2)


class TestExecuteRefusals(_Case):
    """A half-finished run stops without publishing."""

    def test_new_draft_holding_files_aborts_before_the_put(self):
        self.build_package()
        mocks = self.patch_all(empty_check=[committed("stale.zip", 10)])
        self.tripwire("update_draft_metadata", "upload_to_zenodo",
                      "publish_draft")
        code, row = self.run_main("--execute", "--yes")
        self.assertEqual(row["Verdict"], nvu.NEW_DRAFT_NOT_EMPTY)
        mocks["create_new_version_draft"].assert_called_once()
        self.assertEqual(code, 1)

    def test_metadata_readback_mismatch_blocks_the_upload(self):
        self.build_package()
        self.patch_all(readback={"metadata": {
            "version": _OLD_LABEL, "title": _TITLE,
            "publication_date": _TODAY,
        }})
        self.tripwire("upload_to_zenodo", "publish_draft")
        code, row = self.run_main("--execute", "--yes")
        self.assertEqual(row["Verdict"], nvu.METADATA_PUT_UNVERIFIED)
        self.assertIn("version", row["Notes"])
        self.assertEqual(code, 1)

    def test_versions_post_failure_is_reported_as_ambiguous(self):
        self.build_package()
        mocks = self.patch_all()
        mocks["create_new_version_draft"].side_effect = RequestException("drop")
        self.tripwire("update_draft_metadata", "upload_to_zenodo")
        code, row = self.run_main("--execute", "--yes")
        self.assertEqual(row["Verdict"], nvu.VERSION_CREATE_AMBIGUOUS)
        self.assertEqual(code, 1)

    def test_upload_failure_blocks_publish(self):
        self.build_package()
        self.patch_all(upload_ok=False)
        self.tripwire("publish_draft")
        code, row = self.run_main("--execute", "--yes", "--publish")
        self.assertEqual(row["Verdict"], nvu.UPLOAD_FAILED)
        self.assertEqual(code, 1)


class TestCompletenessGate(_Case):
    """The record must hold exactly the new package — both directions."""

    def test_missing_file_blocks_publish(self):
        self.build_package()
        entries = self.committed_package()[1:]
        self.patch_all(draft_files=entries)
        self.tripwire("publish_draft")
        code, row = self.run_main("--execute", "--yes", "--publish")
        self.assertEqual(row["Verdict"], nvu.INCOMPLETE_ON_RECORD)
        self.assertIn("missing", row["Notes"])
        self.assertEqual(code, 1)

    def test_pending_status_blocks_publish(self):
        self.build_package()
        entries = self.committed_package()
        entries[0] = {"key": _PACKAGE[0], "status": "pending", "size": None}
        self.patch_all(draft_files=entries)
        self.tripwire("publish_draft")
        _code, row = self.run_main("--execute", "--yes", "--publish")
        self.assertEqual(row["Verdict"], nvu.INCOMPLETE_ON_RECORD)

    def test_unexpected_extra_entry_blocks_publish(self):
        """The direction the naive subset check misses."""
        self.build_package()
        entries = self.committed_package()
        entries.append(committed("OLD_BROKEN.zip", 999))
        self.patch_all(draft_files=entries)
        self.tripwire("publish_draft")
        _code, row = self.run_main("--execute", "--yes", "--publish")
        self.assertEqual(row["Verdict"], nvu.INCOMPLETE_ON_RECORD)
        self.assertIn("unexpected on record", row["Notes"])
        self.assertIn("OLD_BROKEN.zip", row["Notes"])

    def test_size_mismatch_blocks_publish(self):
        self.build_package()
        entries = self.committed_package()
        entries[0]["size"] = 1
        self.patch_all(draft_files=entries)
        self.tripwire("publish_draft")
        _code, row = self.run_main("--execute", "--yes", "--publish")
        self.assertEqual(row["Verdict"], nvu.INCOMPLETE_ON_RECORD)
        self.assertIn("wrong size", row["Notes"])


# --- resume, state, archive ---------------------------------------------

class TestResumeAdoption(_Case):
    """A half-finished run is finished, not duplicated."""

    def test_state_naming_a_draft_is_adopted_without_a_second_post(self):
        self.build_package(state={
            "mode": nvu.NEW_VERSION_MODE,
            "new_version_record_id": _NEW_RECORD,
            "new_version_label": _NEW_LABEL,
        })
        mocks = self.patch_all(
            published=published_record(versions={"is_latest_draft": False}),
            adopted=True,
        )
        self.tripwire("create_new_version_draft")
        code, row = self.run_main("--execute", "--yes")
        self.assertEqual(row["Verdict"], nvu.VERSION_CREATED)
        self.assertEqual(row["New Record ID"], _NEW_RECORD)
        mocks["update_draft_metadata"].assert_called_once()
        self.assertEqual(code, 0)

    def test_unreadable_draft_reissues_the_put_instead_of_refusing(self):
        """A lingering pending slot can make GET /draft 500."""
        self.build_package(state={
            "mode": nvu.NEW_VERSION_MODE,
            "new_version_record_id": _NEW_RECORD,
        })
        mocks = self.patch_all(
            published=published_record(versions={"is_latest_draft": False}),
            adopted=True,
        )
        readback = {"metadata": {"version": _NEW_LABEL, "title": _TITLE,
                                 "publication_date": _TODAY}}
        mocks["get_draft_record"].side_effect = [
            RequestException("HTTP 500"), readback,
        ]
        code, row = self.run_main("--execute", "--yes")
        self.assertEqual(row["Verdict"], nvu.VERSION_CREATED)
        mocks["update_draft_metadata"].assert_called_once()
        self.assertEqual(code, 0)


class TestStateFile(_Case):
    """The lineage is recorded, and the folder is invisible to other tools."""

    def test_lineage_written_before_the_upload(self):
        self.build_package()
        mocks = self.patch_all()
        seen = {}

        def capture(*args, **kwargs):
            seen.update(nvu.read_state(self.staging))
            return {"successful": True, "api_response": {}, "error": None}

        mocks["upload_to_zenodo"].side_effect = capture
        self.run_main("--execute", "--yes")
        self.assertEqual(seen["new_version_record_id"], _NEW_RECORD)
        self.assertEqual(seen["previous_record_id"], _RECORD)
        self.assertEqual(seen["previous_version_label"], _OLD_LABEL)
        self.assertEqual(seen["concept_doi"], _CONCEPT_DOI)
        self.assertEqual(seen["mode"], nvu.NEW_VERSION_MODE)

    def test_no_record_id_key_is_ever_written(self):
        self.build_package()
        self.patch_all()
        self.run_main("--execute", "--yes")
        self.assertNotIn("record_id", nvu.read_state(self.staging))

    def test_finish_stuck_uploads_does_not_discover_the_folder(self):
        """Cross-tool pin: a discovered folder would be resumed through the
        main pipeline with submit_review=True, hijacking the new version."""
        self.build_package()
        self.patch_all()
        self.run_main("--execute", "--yes")

        import finish_stuck_uploads as fsu
        with mock.patch.object(fsu, "_STAGING_AREA", self.staging_area):
            stuck, _excluded = fsu.discover_stuck_esids()
        self.assertEqual(
            [esid for _k, esid, _f, _r in stuck], [],
            "the new-version staging folder must not look like a stuck upload",
        )

    def test_unknown_keys_survive_a_rewrite(self):
        self.build_package(state={"mode": nvu.NEW_VERSION_MODE,
                                  "operator_note": "keep me"})
        nvu.write_state(self.staging, {"version_doi": "10.5281/x"})
        state = nvu.read_state(self.staging)
        self.assertEqual(state["operator_note"], "keep me")
        self.assertEqual(state["version_doi"], "10.5281/x")


class TestArchive(_Case):
    """The previous version's archive is never destroyed."""

    def test_refuses_when_the_destination_exists(self):
        self.build_package()
        destination = self.uploaded / f"ESID_{_ESID}_Uploaded_{_NEW_LABEL}"
        destination.mkdir()
        (destination / "keep.txt").write_text("v1 evidence\n")
        self.assertIsNone(
            nvu.archive_new_version_staging(self.staging, destination)
        )
        self.assertTrue(self.staging.is_dir(), "staging must stay put")
        self.assertEqual((destination / "keep.txt").read_text(), "v1 evidence\n")

    def test_moves_to_the_version_suffixed_destination(self):
        self.build_package()
        destination = self.uploaded / f"ESID_{_ESID}_Uploaded_{_NEW_LABEL}"
        self.assertEqual(
            nvu.archive_new_version_staging(self.staging, destination),
            destination,
        )
        self.assertFalse(self.staging.exists())
        self.assertTrue((destination / "ESID_073.zip").is_file())

    def test_the_previous_versions_archive_survives_a_full_publish(self):
        self.build_package()
        v1 = self.uploaded / f"ESID_{_ESID}_Uploaded"
        v1.mkdir()
        (v1 / "upload_state.json").write_text('{"record_id": "15234567"}')
        before = (v1 / "upload_state.json").read_text()
        self.patch_all()
        _code, row = self.run_main("--execute", "--yes", "--publish")
        self.assertEqual(row["Verdict"], nvu.VERSION_PUBLISHED)
        self.assertTrue(v1.is_dir(), "v1's archive must survive")
        self.assertEqual((v1 / "upload_state.json").read_text(), before)
        self.assertTrue(
            (self.uploaded / f"ESID_{_ESID}_Uploaded_{_NEW_LABEL}").is_dir()
        )


if __name__ == "__main__":
    unittest.main()
