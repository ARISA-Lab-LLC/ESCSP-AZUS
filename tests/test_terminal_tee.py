"""Unit tests for the ``--save-terminal-output`` terminal tee.

The tee copies everything the run prints to the screen into ``.txt``
files in the Records/ folder without changing the screen output:
one ``..._Standalone_terminal_output_pre.txt`` (everything before the
first upload attempt), one ``..._ESID_<esid>_upload_attempt_NNN.txt``
per dataset attempt (counter increments, never overwrites), and one
``..._Standalone_terminal_output_post.txt`` (everything after).

These tests prove: buffering before the Records dir is known, the
three-way routing (including per-thread routing with concurrent
workers and un-prefixed ``azus.uploader`` lines), the never-overwrite
counters, the prompt mirror, fail-open behavior on an unwritable
Records dir, and — at the CLI level — that no files appear without the
flag and that the screen output is unchanged by it.

Run from the project root:

    python3 -m unittest discover -s tests -v
"""

import json
import logging
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "Resources"))

import standalone_tasks as tasks  # noqa: E402
from models.audiomoth import DataCollector, UploadData  # noqa: E402

_STAMP = "2026-07-21_1435"


class _TeeTestCase(unittest.TestCase):
    """Shared fixture: a tmp Records dir + a tee handler on root."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.records = Path(self._tmp.name) / "Records"
        self.logger = logging.getLogger("azus")

    def make_handler(self, activate=True):
        handler = tasks._TerminalTeeHandler()
        handler.setFormatter(logging.Formatter(tasks._LOG_FORMAT))
        root = logging.getLogger()
        old_level = root.level
        root.setLevel(logging.INFO)
        root.addHandler(handler)

        def _teardown():
            root.removeHandler(handler)
            handler.close()
            root.setLevel(old_level)
            tasks._TEE_HANDLER = None

        self.addCleanup(_teardown)
        if activate:
            handler.activate(self.records, _STAMP)
        return handler

    def read(self, name):
        return (self.records / name).read_text(encoding="utf-8")

    def txt_files(self):
        if not self.records.is_dir():
            return []
        return sorted(p.name for p in self.records.glob("*.txt"))


class TestTeeHandlerRouting(_TeeTestCase):
    """Three-way routing, buffering, counters, mirror, fail-open."""

    def test_buffered_lines_flush_into_pre_file(self):
        handler = self.make_handler(activate=False)
        self.logger.info("before records dir is known 1")
        self.logger.info("before records dir is known 2")
        handler.activate(self.records, _STAMP)
        self.logger.info("after activation")
        pre = self.read(f"{_STAMP}_Standalone_terminal_output_pre.txt")
        self.assertIn("before records dir is known 1", pre)
        self.assertIn("before records dir is known 2", pre)
        self.assertIn("after activation", pre)
        self.assertLess(
            pre.index("known 1"), pre.index("known 2"),
            "buffered lines must keep their order",
        )

    def test_pre_post_and_attempt_files_split_correctly(self):
        handler = self.make_handler()
        self.logger.info("PRE marker")
        handler.begin_esid("004")
        self.logger.info("[ESID 004] Starting")
        handler.end_esid()
        self.logger.info("POST summary marker")
        handler.close()
        pre = self.read(f"{_STAMP}_Standalone_terminal_output_pre.txt")
        attempt = self.read(f"{_STAMP}_ESID_004_upload_attempt_001.txt")
        post = self.read(f"{_STAMP}_Standalone_terminal_output_post.txt")
        self.assertIn("PRE marker", pre)
        self.assertNotIn("Starting", pre)
        self.assertIn("[ESID 004] Starting", attempt)
        self.assertIn("POST summary marker", post)
        self.assertNotIn("POST summary marker", attempt)

    def test_attempt_counter_never_overwrites(self):
        self.records.mkdir(parents=True)
        first = self.records / f"{_STAMP}_ESID_004_upload_attempt_001.txt"
        second = self.records / f"{_STAMP}_ESID_004_upload_attempt_002.txt"
        first.write_text("earlier attempt 1\n", encoding="utf-8")
        second.write_text("earlier attempt 2\n", encoding="utf-8")
        handler = self.make_handler()
        handler.begin_esid("004")
        self.logger.info("third attempt line")
        handler.end_esid()
        self.assertEqual(first.read_text(encoding="utf-8"), "earlier attempt 1\n")
        self.assertEqual(second.read_text(encoding="utf-8"), "earlier attempt 2\n")
        third = self.read(f"{_STAMP}_ESID_004_upload_attempt_003.txt")
        self.assertIn("third attempt line", third)

    def test_pre_file_collision_gets_counter(self):
        self.records.mkdir(parents=True)
        existing = self.records / f"{_STAMP}_Standalone_terminal_output_pre.txt"
        existing.write_text("from an earlier run\n", encoding="utf-8")
        self.make_handler()
        self.logger.info("second run pre line")
        self.assertEqual(
            existing.read_text(encoding="utf-8"), "from an earlier run\n"
        )
        pre2 = self.read(f"{_STAMP}_Standalone_terminal_output_pre_002.txt")
        self.assertIn("second run pre line", pre2)

    def test_worker_threads_route_to_their_own_files(self):
        handler = self.make_handler()
        lines_per_thread = 25

        def worker(esid):
            handler.begin_esid(esid)
            try:
                for i in range(lines_per_thread):
                    self.logger.info("marker-%s line %d", esid, i)
            finally:
                handler.end_esid()

        threads = [
            threading.Thread(target=worker, args=(esid,))
            for esid in ("004", "007")
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for esid, other in (("004", "007"), ("007", "004")):
            content = self.read(
                f"{_STAMP}_ESID_{esid}_upload_attempt_001.txt"
            )
            self.assertEqual(
                content.count(f"marker-{esid} "), lines_per_thread
            )
            self.assertNotIn(f"marker-{other} ", content)

    def test_mirror_writes_prompt_lines_verbatim(self):
        handler = self.make_handler()
        handler.mirror("⚠️  You are about to upload datasets to Zenodo.")
        handler.mirror("Proceed? (yes/no): yes")
        pre = self.read(f"{_STAMP}_Standalone_terminal_output_pre.txt")
        self.assertIn(
            "⚠️  You are about to upload datasets to Zenodo.\n", pre
        )
        self.assertIn("Proceed? (yes/no): yes\n", pre)

    def test_unwritable_records_dir_disables_tee_not_the_run(self):
        blocker = Path(self._tmp.name) / "not_a_dir"
        blocker.write_text("a file where the dir should go", encoding="utf-8")
        handler = self.make_handler(activate=False)
        with self.assertLogs("azus", level="WARNING") as captured:
            handler.activate(blocker / "Records", _STAMP)
        self.assertTrue(
            any("disabled" in m for m in captured.output),
            "the user must be told the tee is off",
        )
        # Logging must keep working without raising.
        self.logger.info("run continues fine")
        self.assertEqual(self.txt_files(), [])


class TestTeeThroughProcessOneDataset(_TeeTestCase):
    """The wrapper brackets an attempt and captures uploader lines."""

    def _run_one(self, esid="004"):
        collector = DataCollector.model_validate({
            "esid": esid,
            "affiliation": "Eclipse Soundscapes : ARISA Lab",
            "files_date_time_mode": "Automatic",
            "version": "2024.1.0",
            "latitude": "35.0000",
            "longitude": "-106.0000",
            "eclipse_date": "2024-04-08",
            "eclipse_type": "Total",
            "eclipse_coverage": "100",
            "eclipse_start_time_utc": "17:00:00",
            "eclipse_maximum_time_utc": "18:15:00",
            "subjects": "eclipse : audiomoth",
        })
        data = UploadData(
            esid=esid,
            data_collector=collector,
            zip_file=str(Path(self._tmp.name) / f"ESID_{esid}.zip"),
        )

        def fake_upload(**_kwargs):
            # Real uploads log through the azus.uploader logger with NO
            # [ESID ...] prefix — those lines must still land in the
            # attempt file via the per-thread routing.
            logging.getLogger("azus.uploader").info("Creating draft record...")
            return {"successful": False, "api_response": None,
                    "error": {"type": "Boom", "error_message": "synthetic"}}

        with mock.patch.object(tasks, "verify_dataset_integrity",
                               return_value=[]), \
             mock.patch.object(tasks, "upload_dataset",
                               side_effect=fake_upload):
            tasks._process_one_dataset(
                index=1, total=1, data=data,
                delete_failures=False, auto_publish=False, reserve_doi=False,
                related_identifiers_csv=None, references_csv=None,
                project_config={},
                successful_results_file=str(self.records / "ok.csv"),
                failure_results_file=str(self.records / "bad.csv"),
                tracker=mock.MagicMock(),
                tracker_lock=threading.Lock(),
                results_lock=threading.Lock(),
                stats={"total_processed": 0, "successful": 0,
                       "failed": 0, "skipped": 0},
                stats_lock=threading.Lock(),
            )

    def test_attempt_file_captures_the_whole_attempt(self):
        handler = self.make_handler()
        tasks._TEE_HANDLER = handler
        self.logger.info("banner line before uploads")
        self._run_one()
        self.logger.info("summary line after uploads")
        handler.close()
        pre = self.read(f"{_STAMP}_Standalone_terminal_output_pre.txt")
        attempt = self.read(f"{_STAMP}_ESID_004_upload_attempt_001.txt")
        post = self.read(f"{_STAMP}_Standalone_terminal_output_post.txt")
        self.assertIn("banner line before uploads", pre)
        self.assertIn("[ESID 004] Starting", attempt)
        self.assertIn("Creating draft record...", attempt)
        self.assertIn("[ESID 004] DONE (failed)", attempt)
        self.assertIn("summary line after uploads", post)
        self.assertNotIn("Starting", pre)
        self.assertNotIn("Creating draft record", post)

    def test_tee_off_means_no_files_and_no_errors(self):
        # No handler installed at all — the wrapper must be a clean no-op.
        self.assertIsNone(tasks._TEE_HANDLER)
        self._run_one()
        self.assertEqual(self.txt_files(), [])


class TestCliDryRun(unittest.TestCase):
    """CLI-level behavior of the flag (subprocess, hermetic tmp dirs)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.records = self.root / "Records"

    def _run(self, extra_args):
        config = self.root / "config.json"
        config.write_text(json.dumps({
            "uploads": {
                "datasets": [],
                "successful_results_file":
                    str(self.records / "successful_results.csv"),
            }
        }), encoding="utf-8")
        env = {
            "PATH": "/usr/bin:/bin",
            "INVENIO_RDM_ACCESS_TOKEN": "dummy",
            "INVENIO_RDM_BASE_URL": "https://example.org/api/",
        }
        return subprocess.run(
            [sys.executable, str(_PROJECT_ROOT / "standalone_tasks.py"),
             "--config", str(config), "--dry-run", *extra_args],
            capture_output=True, text=True, cwd=self.root, env=env,
        )

    @staticmethod
    def _strip_timestamps(text):
        """Drop the '%(asctime)s - ' prefix so runs can be compared."""
        return [line.split(" - ", 1)[-1]
                for line in text.splitlines() if line.strip()]

    def test_without_flag_no_txt_files(self):
        result = self._run([])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(list(self.records.glob("*.txt"))
                         if self.records.is_dir() else [])

    def test_dry_run_writes_pre_file_equal_to_screen(self):
        result = self._run(["--save-terminal-output"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        txts = list(self.records.glob("*.txt"))
        self.assertEqual(len(txts), 1, [t.name for t in txts])
        self.assertRegex(
            txts[0].name,
            r"^\d{4}-\d{2}-\d{2}_\d{4}_Standalone_terminal_output_pre\.txt$",
        )
        pre_lines = self._strip_timestamps(
            txts[0].read_text(encoding="utf-8")
        )
        screen_lines = self._strip_timestamps(result.stdout)
        self.assertEqual(pre_lines, screen_lines)

    def test_screen_output_unchanged_by_flag(self):
        plain = self._strip_timestamps(self._run([]).stdout)
        teed = self._strip_timestamps(
            self._run(["--save-terminal-output"]).stdout
        )
        extra = [line for line in teed if line not in plain]
        self.assertEqual(
            len(extra), 1, f"unexpected extra screen lines: {extra}"
        )
        self.assertIn("Saving a copy of terminal output to", extra[0])


if __name__ == "__main__":
    unittest.main()
