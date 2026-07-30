"""Unit tests for the ``--esid`` argument expansion.

Covers ``azus_common.load_esid_args`` (literal 1-3 digit ESIDs mixed
with CSV spreadsheet paths whose first column lists ESIDs, header row
optional), the fail-closed rules (junk tokens and empty files are hard
errors, never silent no-ops), and ``finish_stuck_uploads``'s new
``--esid`` filter over the discovered stuck list.

Run from the project root:

    python3 -m unittest discover -s tests -v
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "Resources"))

import azus_common  # noqa: E402
import finish_stuck_uploads as fsu  # noqa: E402


class _TmpTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _csv(self, name: str, text: str, encoding: str = "utf-8") -> str:
        path = self.root / name
        path.write_bytes(text.encode(encoding))
        return str(path)


class TestLiteralTokens(_TmpTestCase):
    def test_numbers_padded_and_order_kept(self):
        self.assertEqual(
            azus_common.load_esid_args(["4", "007", "12"]),
            ["004", "007", "012"],
        )

    def test_duplicates_removed_first_seen_order(self):
        self.assertEqual(
            azus_common.load_esid_args(["7", "007", "4"]), ["007", "004"]
        )

    def test_junk_token_is_hard_error(self):
        """'abc' used to be a silent match-nothing filter — now it fails."""
        with self.assertRaises(ValueError):
            azus_common.load_esid_args(["abc"])

    def test_four_digit_token_is_hard_error(self):
        with self.assertRaises(ValueError):
            azus_common.load_esid_args(["1234"])

    def test_numeric_token_never_treated_as_path(self):
        # Even if a file named '004' exists, '004' means ESID 004.
        (self.root / "004").write_text("999\n", encoding="utf-8")
        import os
        cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, cwd)
        self.assertEqual(azus_common.load_esid_args(["004"]), ["004"])

    def test_missing_file_with_nonnumeric_name_is_hard_error(self):
        with self.assertRaises(ValueError) as ctx:
            azus_common.load_esid_args([str(self.root / "nope.csv")])
        self.assertIn("neither", str(ctx.exception))


class TestCsvExpansion(_TmpTestCase):
    def test_report_csv_with_header(self):
        path = self._csv("report.csv", (
            "ESID#,Title,Zenodo URL,Draft (y/n),DOI\n"
            "004,t,u,n,d\n"
            "073,t,u,y,\n"
        ))
        self.assertEqual(azus_common.load_esid_args([path]), ["004", "073"])

    def test_headerless_single_column(self):
        path = self._csv("bare.csv", "4\n007\n12\n")
        self.assertEqual(
            azus_common.load_esid_args([path]), ["004", "007", "012"]
        )

    def test_first_row_that_parses_is_data_not_header(self):
        path = self._csv("nohdr.csv", "004,foo\n007,bar\n")
        self.assertEqual(azus_common.load_esid_args([path]), ["004", "007"])

    def test_excel_float_and_prefixed_cells(self):
        path = self._csv("excel.csv", "ESID#\n73.0\nESID 012\nESID#004\n")
        self.assertEqual(
            azus_common.load_esid_args([path]), ["073", "012", "004"]
        )

    def test_blank_rows_skipped_and_duplicates_deduped(self):
        path = self._csv("dups.csv", "ESID#\n004\n\n004\n007\n")
        self.assertEqual(azus_common.load_esid_args([path]), ["004", "007"])

    def test_unparseable_cells_warned_and_skipped(self):
        path = self._csv("messy.csv", "ESID#\n004\nnot-an-esid\n007\n")
        with self.assertLogs("azus.common", level="WARNING") as captured:
            result = azus_common.load_esid_args([path])
        self.assertEqual(result, ["004", "007"])
        self.assertTrue(any("not-an-esid" in m for m in captured.output))

    def test_all_invalid_file_is_hard_error(self):
        """A bad file must never silently mean 'no filter'."""
        path = self._csv("bad.csv", "ESID#\njunk\nmore junk\n")
        with self.assertRaises(ValueError) as ctx:
            azus_common.load_esid_args([path])
        self.assertIn("No valid ESIDs", str(ctx.exception))

    def test_semicolon_delimited(self):
        path = self._csv("semi.csv", "ESID#;Title\n004;x\n007;y\n")
        self.assertEqual(azus_common.load_esid_args([path]), ["004", "007"])

    def test_utf8_bom(self):
        path = self._csv("bom.csv", "﻿ESID#\n004\n")
        self.assertEqual(azus_common.load_esid_args([path]), ["004"])

    def test_cp1252_fallback(self):
        path = self.root / "cp1252.csv"
        path.write_bytes("ESID#,Städte\n004,a\n".encode("cp1252"))
        self.assertEqual(azus_common.load_esid_args([str(path)]), ["004"])

    def test_mixed_numbers_and_file(self):
        path = self._csv("mix.csv", "ESID#\n007\n012\n")
        self.assertEqual(
            azus_common.load_esid_args(["4", path, "12"]),
            ["004", "007", "012"],
        )


class TestFinishStuckEsidFilter(_TmpTestCase):
    """--esid on finish_stuck_uploads filters the discovered stuck list."""

    def _make_stuck(self, esid: str, record_id: str) -> None:
        folder = self.root / f"ESID_{esid}_Staging"
        folder.mkdir()
        (folder / "upload_state.json").write_text(
            json.dumps({"record_id": record_id}), encoding="utf-8"
        )

    def test_discovered_list_can_be_filtered(self):
        """The filter logic in main() uses set membership on the padded
        ESID; replicate its inputs to prove the intersection behavior."""
        self._make_stuck("004", "111")
        self._make_stuck("007", "222")
        self._make_stuck("012", "333")
        with mock.patch.object(fsu, "_STAGING_AREA", self.root):
            stuck, excluded = fsu.discover_stuck_esids()
        self.assertEqual([t[1] for t in stuck], ["004", "007", "012"])
        # Same expression main() uses:
        requested = azus_common.load_esid_args(["7", "999"])
        wanted = set(requested)
        filtered = [t for t in stuck if t[1] in wanted]
        self.assertEqual([t[1] for t in filtered], ["007"])

    def test_cli_list_only_with_csv_filter(self):
        """End-to-end: --list-only --esid <csv> prints only the
        intersection and exits 0."""
        self._make_stuck("004", "111")
        self._make_stuck("007", "222")
        filter_csv = self._csv("want.csv", "ESID#\n007\n999\n")
        env_script = (
            "import sys, unittest.mock as mock\n"
            f"sys.path.insert(0, {str(_PROJECT_ROOT / 'Resources')!r})\n"
            f"sys.path.insert(0, {str(_PROJECT_ROOT)!r})\n"
            "import finish_stuck_uploads as fsu\n"
            "from pathlib import Path\n"
            f"root = Path({str(self.root)!r})\n"
            "with mock.patch.object(fsu, '_STAGING_AREA', root):\n"
            # --log keeps this run's log inside the temp tree; without it
            # the tool writes into the real Records/ folder.
            f"    sys.argv = ['x', '--list-only', '--esid', {filter_csv!r},\n"
            f"                '--log', str(root / 'run.log')]\n"
            "    fsu.main()\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", env_script],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        out = result.stdout
        self.assertIn("1 of 2 requested", out)
        self.assertIn("ESID 007", out)
        self.assertNotIn("ESID 004  →", out)
        self.assertIn("not currently stuck", out)  # the 999 warning

    def test_cli_junk_esid_exits_2(self):
        result = subprocess.run(
            [sys.executable,
             str(_PROJECT_ROOT / "Resources" / "finish_stuck_uploads.py"),
             "--list-only", "--esid", "abc"],
            capture_output=True, text=True, cwd=_PROJECT_ROOT,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("neither", result.stdout + result.stderr)


class TestUploadOrderFollowsFilter(_TmpTestCase):
    """get_upload_data returns datasets in the ORDER the --esid values
    were given (CLI order / spreadsheet row order), not directory-scan
    or numeric order."""

    def _get_order(self, esid_filter):
        import standalone_tasks as tasks
        from types import SimpleNamespace

        staging = self.root / "Staging_Area"
        staging.mkdir(exist_ok=True)
        for esid in ("004", "007", "012"):
            folder = staging / f"ESID_{esid}_Staging"
            folder.mkdir(exist_ok=True)
            (folder / f"ESID_{esid}.zip").write_bytes(b"zip")

        def fake_create_upload_data(esid_folder_archives, **_kwargs):
            return ([SimpleNamespace(esid=e)
                     for e, _folder, _archives in esid_folder_archives],
                    [])

        tracker = mock.MagicMock()
        tracker.is_uploaded.return_value = False
        with mock.patch.object(tasks, "parse_collectors_csv",
                               return_value=[]), \
             mock.patch.object(tasks, "create_upload_data",
                               side_effect=fake_create_upload_data):
            result = tasks.get_upload_data(
                data_dir=str(staging),
                data_collectors_file="collectors.csv",
                dataset_category="Total",
                failure_results_file=str(self.root / "fail.csv"),
                tracker=tracker,
                esid_filter=esid_filter,
            )
        return [d.esid for d in result]

    def test_order_matches_the_given_list_not_the_directory(self):
        self.assertEqual(self._get_order(["12", "004", "7"]),
                         ["012", "004", "007"])

    def test_reversed_input_reverses_the_upload_order(self):
        self.assertEqual(self._get_order(["7", "12", "4"]),
                         ["007", "012", "004"])


class TestStandaloneTasksCli(_TmpTestCase):
    """standalone_tasks.py --esid expansion at the CLI boundary."""

    def _run(self, esid_args):
        config = self.root / "config.json"
        config.write_text(json.dumps({"uploads": {"datasets": []}}))
        env = {
            "PATH": "/usr/bin:/bin",
            "INVENIO_RDM_ACCESS_TOKEN": "dummy",
            "INVENIO_RDM_BASE_URL": "https://example.org/api/",
        }
        return subprocess.run(
            [sys.executable, str(_PROJECT_ROOT / "standalone_tasks.py"),
             "--config", str(config), "--dry-run", "--esid", *esid_args],
            capture_output=True, text=True, cwd=_PROJECT_ROOT, env=env,
        )

    def test_junk_token_exits_2(self):
        result = self._run(["abc"])
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("neither", result.stdout + result.stderr)

    def test_csv_expansion_shows_in_banner(self):
        report = self._csv("esid_records.csv", "ESID#\n004\n073\n")
        result = self._run([report, "12"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("004, 073, 012", result.stdout)


if __name__ == "__main__":
    unittest.main()
