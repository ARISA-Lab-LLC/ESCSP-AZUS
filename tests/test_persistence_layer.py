"""Unit tests for AZUS's file-based persistence layer.

Covers the small on-disk records the pipeline leaves behind and later
relies on to avoid duplicate work:

  * ``standalone_tasks.UploadTracker`` — the one-path-per-line
    ``uploaded_files.txt`` dedupe record (append, reload, count).
  * ``standalone_tasks.save_result`` / ``save_result_csv`` — the
    success/failure result CSVs (routing, header-once, append-only).
  * ``standalone_tasks._recover_draft_id_from_request_log`` — case-7
    recovery of a draft's record_id from ``ESID_XXX_request_log.json``.
  * ``finish_stuck_uploads.discover_stuck_esids`` — Staging_Area/ scan
    for resumable drafts (``upload_state.json`` marker).
  * ``diagnose_missing_states.restore_state`` — the opt-in healer that
    rewrites ``upload_state.json`` from a surviving request log.

All tests are hermetic: every path lives inside a TemporaryDirectory,
and module-level path constants are monkeypatched where a function
reads them.  No network, no real Staging_Area/, Records/ or Zenodo.

Run from the project root:

    python3 -m unittest tests.test_persistence_layer -v
"""

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "Resources"))

import standalone_tasks as tasks  # noqa: E402
import finish_stuck_uploads as fsu  # noqa: E402
import diagnose_missing_states as diag  # noqa: E402
import list_upload_states as lus  # noqa: E402


class _TmpDirTestCase(unittest.TestCase):
    """Shared per-test TemporaryDirectory as ``self.root`` (a Path)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


# ===================================================================
#  UploadTracker
# ===================================================================

class TestUploadTracker(_TmpDirTestCase):
    def setUp(self):
        super().setUp()
        self.tracker_file = self.root / "Records" / "uploaded_files.txt"

    def _tracker(self):
        return tasks.UploadTracker(tracker_file=str(self.tracker_file))

    def test_fresh_tracker_is_empty(self):
        tracker = self._tracker()
        self.assertEqual(tracker.get_count(), 0)
        self.assertFalse(tracker.is_uploaded("/data/ESID_005.zip"))

    def test_constructor_creates_missing_parent_directory(self):
        # Records/ may not exist on a fresh install — the tracker must
        # not require it to be pre-created.
        self.assertFalse(self.tracker_file.parent.exists())
        self._tracker()
        self.assertTrue(self.tracker_file.parent.is_dir())

    def test_mark_uploaded_appends_path_to_tracker_file(self):
        tracker = self._tracker()
        tracker.mark_uploaded("/data/ESID_005.zip")
        tracker.mark_uploaded("/data/ESID_007.zip")
        lines = self.tracker_file.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines, ["/data/ESID_005.zip", "/data/ESID_007.zip"])

    def test_is_uploaded_true_after_mark(self):
        tracker = self._tracker()
        tracker.mark_uploaded("/data/ESID_005.zip")
        self.assertTrue(tracker.is_uploaded("/data/ESID_005.zip"))
        self.assertFalse(tracker.is_uploaded("/data/ESID_006.zip"))

    def test_marks_persist_across_new_instances(self):
        self._tracker().mark_uploaded("/data/ESID_005.zip")
        fresh = self._tracker()
        self.assertTrue(fresh.is_uploaded("/data/ESID_005.zip"))
        self.assertEqual(fresh.get_count(), 1)

    def test_get_count_counts_unique_paths(self):
        tracker = self._tracker()
        tracker.mark_uploaded("/data/ESID_005.zip")
        tracker.mark_uploaded("/data/ESID_007.zip")
        tracker.mark_uploaded("/data/ESID_012.zip")
        self.assertEqual(tracker.get_count(), 3)

    def test_duplicate_marks_do_not_double_count(self):
        """Marking the same path twice must not inflate the count — in
        the live instance NOR after a reload from disk (the file may
        carry duplicate lines; the set semantics must dedupe them)."""
        tracker = self._tracker()
        tracker.mark_uploaded("/data/ESID_005.zip")
        tracker.mark_uploaded("/data/ESID_005.zip")
        self.assertEqual(tracker.get_count(), 1)
        self.assertEqual(self._tracker().get_count(), 1)

    def test_blank_lines_in_tracker_file_ignored(self):
        # Hand-edited tracker files (the documented way to force a
        # re-upload) can easily end up with stray blank lines.
        self.tracker_file.parent.mkdir(parents=True)
        self.tracker_file.write_text(
            "/data/ESID_005.zip\n\n   \n/data/ESID_007.zip\n",
            encoding="utf-8",
        )
        tracker = self._tracker()
        self.assertEqual(tracker.get_count(), 2)
        self.assertTrue(tracker.is_uploaded("/data/ESID_007.zip"))


# ===================================================================
#  save_result / save_result_csv
# ===================================================================

class TestSaveResultCsv(_TmpDirTestCase):
    def setUp(self):
        super().setUp()
        self.success_file = str(self.root / "successful_results.csv")
        self.failure_file = str(self.root / "failed_results.csv")

    def _rows(self, path):
        with open(path, newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def test_success_routes_row_to_success_csv_only(self):
        tasks.save_result(
            esid="005",
            success=True,
            success_file=self.success_file,
            failure_file=self.failure_file,
            api_response={
                "id": 424242,
                "doi": "10.5281/zenodo.424242",
                "links": {"self_html": "https://zenodo.org/records/424242"},
            },
        )
        rows = self._rows(self.success_file)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["esid"], "005")
        self.assertEqual(rows[0]["id"], "424242")
        self.assertEqual(rows[0]["doi"], "10.5281/zenodo.424242")
        self.assertEqual(rows[0]["link"], "https://zenodo.org/records/424242")
        # A success must not leave anything in the failure ledger.
        self.assertFalse(Path(self.failure_file).exists())

    def test_failure_routes_row_to_failure_csv_with_error_fields(self):
        tasks.save_result(
            esid="007",
            success=False,
            success_file=self.success_file,
            failure_file=self.failure_file,
            error_type="ConnectionError",
            error_message="ZIP PUT exhausted retries",
        )
        rows = self._rows(self.failure_file)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["esid"], "007")
        self.assertEqual(rows[0]["error_type"], "ConnectionError")
        self.assertEqual(rows[0]["error_message"], "ZIP PUT exhausted retries")
        self.assertFalse(Path(self.success_file).exists())

    def test_failure_without_details_gets_default_error_fields(self):
        # Even a detail-free failure must never produce an empty error
        # column — the CSVs are the operator's audit trail.
        tasks.save_result(
            esid="008",
            success=False,
            success_file=self.success_file,
            failure_file=self.failure_file,
        )
        row = self._rows(self.failure_file)[0]
        self.assertEqual(row["error_type"], "Unknown")
        self.assertEqual(row["error_message"], "Upload failed")

    def test_header_written_once_and_appends_accumulate(self):
        from models.audiomoth import PersistedResult

        target = str(self.root / "results.csv")
        tasks.save_result_csv(file=target, result=PersistedResult(esid="005"))
        tasks.save_result_csv(file=target, result=PersistedResult(esid="007"))
        tasks.save_result_csv(file=target, result=PersistedResult(esid="012"))

        with open(target, newline="", encoding="utf-8") as fh:
            raw = list(csv.reader(fh))
        # Exactly one header line, then one line per persisted result.
        self.assertEqual(len(raw), 4)
        self.assertIn("esid", raw[0])
        self.assertIn("error_message", raw[0])
        self.assertEqual(
            [row["esid"] for row in self._rows(target)],
            ["005", "007", "012"],
        )

    def test_creates_missing_parent_directories(self):
        from models.audiomoth import PersistedResult

        target = self.root / "Records" / "deep" / "results.csv"
        tasks.save_result_csv(
            file=str(target), result=PersistedResult(esid="005")
        )
        self.assertEqual(self._rows(target)[0]["esid"], "005")

    def test_empty_file_path_rejected(self):
        from models.audiomoth import PersistedResult

        with self.assertRaises(ValueError):
            tasks.save_result_csv(file="", result=PersistedResult(esid="005"))


# ===================================================================
#  _recover_draft_id_from_request_log
# ===================================================================

class TestRecoverDraftIdFromRequestLog(_TmpDirTestCase):
    """Case-7 recovery: upload_state.json lost, request log survives."""

    def _write_log(self, payload, esid="073"):
        log = self.root / f"ESID_{esid}_request_log.json"
        log.write_text(payload, encoding="utf-8")
        return log

    def test_record_id_recovered_from_request_log(self):
        self._write_log(json.dumps({"record_id": "1234567", "other": "x"}))
        self.assertEqual(
            tasks._recover_draft_id_from_request_log(self.root, "073"),
            "1234567",
        )

    def test_numeric_record_id_returned_as_string(self):
        # Older logs stored the id as a JSON number; callers expect str.
        self._write_log(json.dumps({"record_id": 1234567}))
        self.assertEqual(
            tasks._recover_draft_id_from_request_log(self.root, "073"),
            "1234567",
        )

    def test_missing_request_log_returns_none(self):
        self.assertIsNone(
            tasks._recover_draft_id_from_request_log(self.root, "073")
        )

    def test_wrong_esid_request_log_not_used(self):
        # The log is looked up per-ESID; ESID 074's log must never
        # resume ESID 073's draft.
        self._write_log(json.dumps({"record_id": "1234567"}), esid="074")
        self.assertIsNone(
            tasks._recover_draft_id_from_request_log(self.root, "073")
        )

    def test_malformed_json_returns_none_with_warning(self):
        self._write_log("{not json at all")
        with self.assertLogs("azus", level="WARNING") as captured:
            result = tasks._recover_draft_id_from_request_log(
                self.root, "073"
            )
        self.assertIsNone(result)
        self.assertTrue(
            any("unreadable" in message for message in captured.output)
        )

    def test_empty_record_id_returns_none(self):
        self._write_log(json.dumps({"record_id": ""}))
        self.assertIsNone(
            tasks._recover_draft_id_from_request_log(self.root, "073")
        )

    def test_absent_record_id_key_returns_none(self):
        self._write_log(json.dumps({"created_at": "2026-01-01T00:00:00"}))
        self.assertIsNone(
            tasks._recover_draft_id_from_request_log(self.root, "073")
        )


# ===================================================================
#  finish_stuck_uploads.discover_stuck_esids
# ===================================================================

class TestDiscoverStuckEsids(_TmpDirTestCase):
    """Fake Staging_Area/ scan — _STAGING_AREA is monkeypatched."""

    def setUp(self):
        super().setUp()
        self.staging = self.root / "Staging_Area"
        self.staging.mkdir()
        patcher = mock.patch.object(fsu, "_STAGING_AREA", self.staging)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _make_folder(self, name, state=None, state_text=None):
        """Create a staging folder; write upload_state.json if given."""
        folder = self.staging / name
        folder.mkdir()
        if state is not None:
            state_text = json.dumps(state)
        if state_text is not None:
            (folder / "upload_state.json").write_text(
                state_text, encoding="utf-8"
            )
        return folder

    def test_missing_staging_area_returns_empty(self):
        with mock.patch.object(
            fsu, "_STAGING_AREA", self.root / "does_not_exist"
        ):
            self.assertEqual(fsu.discover_stuck_esids(), ([], []))

    def test_valid_states_discovered_sorted_numerically(self):
        # Insertion order is deliberately non-numeric, and ESID_4 is
        # unpadded — string sorting would put "ESID_4" after "ESID_073".
        f73 = self._make_folder("ESID_073", state={"record_id": "111"})
        f4 = self._make_folder("ESID_4", state={"record_id": "222"})
        f105 = self._make_folder(
            "ESID_105_Staging", state={"record_id": "333"}
        )

        stuck, excluded = fsu.discover_stuck_esids()

        self.assertEqual(excluded, [])
        self.assertEqual(
            stuck,
            [
                ((4, ""), "004", f4, "222"),
                ((73, ""), "073", f73, "111"),
                ((105, ""), "105", f105, "333"),
            ],
        )

    def test_folders_without_state_file_are_reported_excluded(self):
        self._make_folder("ESID_073", state={"record_id": "111"})
        self._make_folder("ESID_009")
        self._make_folder("ESID_002")

        stuck, excluded = fsu.discover_stuck_esids()

        self.assertEqual([t[1] for t in stuck], ["073"])
        # Excluded folders must be surfaced (sorted), never hidden —
        # hiding them once let missing-state datasets sit out recovery.
        self.assertEqual(excluded, ["ESID_002", "ESID_009"])

    def test_malformed_state_json_skipped_with_warning(self):
        self._make_folder("ESID_050", state_text="{broken json")
        with self.assertLogs("azus.finish_stuck", level="WARNING") as captured:
            stuck, excluded = fsu.discover_stuck_esids()
        # Unresumable, but not "excluded" either — the folder HAS a
        # state file; it is skipped with an explicit warning instead.
        self.assertEqual(stuck, [])
        self.assertEqual(excluded, [])
        self.assertTrue(
            any("Could not parse" in message for message in captured.output)
        )

    def test_state_without_record_id_skipped_with_warning(self):
        self._make_folder("ESID_051", state={"created_at": "2026-01-01"})
        self._make_folder("ESID_052", state={"record_id": "   "})
        with self.assertLogs("azus.finish_stuck", level="WARNING") as captured:
            stuck, excluded = fsu.discover_stuck_esids()
        self.assertEqual(stuck, [])
        self.assertEqual(excluded, [])
        self.assertTrue(
            any("no record_id" in message for message in captured.output)
        )

    def test_non_esid_entries_ignored(self):
        self._make_folder("backup_stuff", state={"record_id": "999"})
        (self.staging / ".DS_Store").write_bytes(b"\x00")
        (self.staging / "ESID_060.zip").write_bytes(b"not a dir")

        stuck, excluded = fsu.discover_stuck_esids()

        self.assertEqual(stuck, [])
        self.assertEqual(excluded, [])


# ===================================================================
#  diagnose_missing_states.restore_state
# ===================================================================

class TestRestoreState(_TmpDirTestCase):
    """The opt-in healer: rewrite upload_state.json from a request log."""

    def setUp(self):
        super().setUp()
        self.folder = self.root / "ESID_073"
        self.folder.mkdir()
        self.request_log = self.folder / "ESID_073_request_log.json"
        self.request_log.write_text(
            json.dumps({"record_id": "1234567"}), encoding="utf-8"
        )
        self.state_path = self.folder / "upload_state.json"

    def _evidence(self, record_id="1234567"):
        ev = diag.Evidence(esid="073", folder=self.folder)
        ev.request_log_record_id = record_id
        ev.request_log_path = self.request_log
        return ev

    def test_writes_state_with_record_id_and_restored_from_marker(self):
        self.assertTrue(diag.restore_state(self._evidence()))

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["record_id"], "1234567")
        self.assertEqual(state["restored_from"], "ESID_073_request_log.json")
        self.assertIs(state["resumed"], False)
        # The restored state must point at the EXISTING draft so that
        # finish_stuck_uploads resumes it instead of minting a duplicate.
        self.assertIn("1234567", state["zenodo_url"])
        # Restoring the link is NOT an upload attempt: the counter is
        # written at its initial value; the next actual run makes it 1.
        self.assertEqual(state["number_of_tries"], 0)

    def test_restored_state_is_discoverable_by_finish_stuck_uploads(self):
        """End-to-end handoff: the healer's output is exactly what the
        recovery tool scans for."""
        diag.restore_state(self._evidence())
        with mock.patch.object(fsu, "_STAGING_AREA", self.root):
            stuck, excluded = fsu.discover_stuck_esids()
        self.assertEqual(excluded, [])
        self.assertEqual(
            [(t[1], t[3]) for t in stuck], [("073", "1234567")]
        )

    def test_never_overwrites_existing_state_file(self):
        original = json.dumps({"record_id": "ORIGINAL"})
        self.state_path.write_text(original, encoding="utf-8")

        with self.assertLogs("azus.state_diag", level="WARNING") as captured:
            result = diag.restore_state(self._evidence())

        self.assertFalse(result)
        self.assertEqual(
            self.state_path.read_text(encoding="utf-8"), original
        )
        self.assertTrue(
            any("not overwriting" in message for message in captured.output)
        )

    def test_no_record_id_writes_nothing(self):
        self.assertFalse(diag.restore_state(self._evidence(record_id="")))
        self.assertFalse(self.state_path.exists())


if __name__ == "__main__":
    unittest.main()


class TestListUploadStatesTriesColumn(unittest.TestCase):
    """list_upload_states surfaces the number_of_tries attempt counter."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _folder_with_state(self, esid: str, state: dict) -> None:
        folder = self.root / f"ESID_{esid}_Staging"
        folder.mkdir()
        (folder / "upload_state.json").write_text(
            json.dumps(state), encoding="utf-8"
        )

    def test_counter_shown_and_legacy_marked(self):
        self._folder_with_state("001", {
            "record_id": "111", "zenodo_url": "u", "created_at": "t",
            "resumed": True, "number_of_tries": 4,
        })
        # Legacy state file: predates the counter field entirely.
        self._folder_with_state("002", {
            "record_id": "222", "zenodo_url": "u", "created_at": "t",
            "resumed": False,
        })
        rows = lus.scan_directory("Staging", self.root)
        by_esid = {r["ESID#"]: r for r in rows}
        self.assertEqual(by_esid["001"]["Number of Tries"], "4")
        self.assertEqual(by_esid["002"]["Number of Tries"], "0 (legacy)")
        # The column exists in the CSV header contract.
        self.assertIn("Number of Tries", lus._CSV_COLUMNS)

    def test_unreadable_state_leaves_tries_blank_with_note(self):
        folder = self.root / "ESID_003_Staging"
        folder.mkdir()
        (folder / "upload_state.json").write_text("{not json")
        rows = lus.scan_directory("Staging", self.root)
        self.assertEqual(rows[0]["Number of Tries"], "")
        self.assertIn("unreadable state file", rows[0]["Notes"])
