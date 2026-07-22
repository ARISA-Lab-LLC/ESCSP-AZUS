"""Unit tests for prep_all_datasets.py's --esid and --force flags.

``--esid`` restricts a batch prep to the given ESID(s) and enforces the
GIVEN order (list or CSV first-column), exactly like standalone_tasks.py.
``--force`` bypasses the skip checks that normally leave an ESID alone
when it already has a folder in Staging_Area/ or Uploaded_Data/, so the
listed ESIDs are re-prepared regardless (prepare_dataset.py then replaces
any existing staging folder).

These tests prove the filter/order helper in isolation and drive
``main()`` end-to-end with the prepare_dataset.py subprocess mocked out,
so no real preparation runs.

Run from the project root:

    python3 -m unittest discover -s tests -v
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "Resources"))

import azus_common  # noqa: E402
import prep_all_datasets as prep  # noqa: E402


class TestFilterAndOrder(unittest.TestCase):
    """The pure filter/order helper."""

    def _discovered(self, *esids):
        return [
            (azus_common.esid_sort_key(e), e, Path(f"/raw/ESID_{e}"))
            for e in esids
        ]

    def test_restricts_and_reorders(self):
        disc = self._discovered("004", "007", "073")
        selected, missing = prep.filter_and_order_discovered(
            disc, ["073", "004"]
        )
        self.assertEqual([e for _, e, _ in selected], ["073", "004"])
        self.assertEqual(missing, [])

    def test_missing_requested_reported(self):
        disc = self._discovered("004")
        selected, missing = prep.filter_and_order_discovered(
            disc, ["004", "999"]
        )
        self.assertEqual([e for _, e, _ in selected], ["004"])
        self.assertEqual(missing, ["999"])

    def test_suffix_match_is_case_insensitive(self):
        disc = self._discovered("120A")
        selected, missing = prep.filter_and_order_discovered(disc, ["120a"])
        self.assertEqual(len(selected), 1)
        self.assertEqual(missing, [])


class _RawTreeCase(unittest.TestCase):
    """Fixture: a tmp raw tree plus patched Staging/Uploaded areas."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.raw = self.root / "Raw_Data"
        self.staging = self.root / "Staging_Area"
        self.uploaded = self.root / "Uploaded_Data"
        for d in (self.raw, self.staging, self.uploaded):
            d.mkdir()
        for name, value in (("_STAGING_AREA", self.staging),
                            ("_UPLOADED_DATA", self.uploaded)):
            patcher = mock.patch.object(prep, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_raw(self, *esids):
        for esid in esids:
            (self.raw / f"ESID_{esid}").mkdir()

    def mark_staged(self, esid):
        folder = self.staging / f"ESID_{esid}_Staging"
        folder.mkdir()
        (folder / prep._PREP_SENTINEL).write_text("")

    def mark_uploaded(self, esid):
        (self.uploaded / f"ESID_{esid}_Uploaded").mkdir()

    def run_main(self, argv):
        """Run main() with prepare_dataset.py mocked; return (exit_code,
        prepped_folder_names_in_call_order)."""
        with mock.patch.object(prep, "run_prepare_dataset",
                               return_value=0) as run, \
             mock.patch.object(sys, "argv",
                               ["prep_all_datasets.py", str(self.raw), *argv]):
            try:
                prep.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
        prepped = [Path(c.kwargs["esid_folder"]).name
                   for c in run.call_args_list]
        return code, prepped


class TestEsidFilterOrder(_RawTreeCase):
    def test_no_esid_processes_all_numerically(self):
        self.make_raw("003", "001", "002")
        code, prepped = self.run_main([])
        self.assertEqual(code, 0)
        self.assertEqual(prepped, ["ESID_001", "ESID_002", "ESID_003"])

    def test_esid_restricts_and_orders(self):
        self.make_raw("001", "002", "003")
        code, prepped = self.run_main(["--esid", "003", "001"])
        self.assertEqual(code, 0)
        self.assertEqual(prepped, ["ESID_003", "ESID_001"])

    def test_missing_requested_skipped_others_run(self):
        self.make_raw("001")
        code, prepped = self.run_main(["--esid", "001", "999"])
        self.assertEqual(code, 0)
        self.assertEqual(prepped, ["ESID_001"])

    def test_all_requested_missing_exits_1(self):
        self.make_raw("001")
        code, prepped = self.run_main(["--esid", "999"])
        self.assertEqual(code, 1)
        self.assertEqual(prepped, [])

    def test_esid_from_csv_first_column_order(self):
        self.make_raw("001", "002", "003")
        csv_path = self.root / "list.csv"
        csv_path.write_text("ESID\n002\n003\n", encoding="utf-8")
        code, prepped = self.run_main(["--esid", str(csv_path)])
        self.assertEqual(prepped, ["ESID_002", "ESID_003"])


class TestForce(_RawTreeCase):
    def test_without_force_skips_already_staged(self):
        self.make_raw("001", "002")
        self.mark_staged("001")
        code, prepped = self.run_main([])
        self.assertEqual(prepped, ["ESID_002"])

    def test_without_force_skips_uploaded(self):
        self.make_raw("001")
        self.mark_uploaded("001")
        code, prepped = self.run_main([])
        self.assertEqual(prepped, [])

    def test_force_reprep_staged(self):
        self.make_raw("001", "002")
        self.mark_staged("001")
        code, prepped = self.run_main(["--force"])
        self.assertEqual(sorted(prepped), ["ESID_001", "ESID_002"])

    def test_force_reprep_uploaded(self):
        self.make_raw("001")
        self.mark_uploaded("001")
        code, prepped = self.run_main(["--force"])
        self.assertEqual(prepped, ["ESID_001"])

    def test_esid_and_force_combined_orders_and_reprep(self):
        self.make_raw("001", "002", "003")
        self.mark_staged("002")
        code, prepped = self.run_main(["--esid", "002", "001", "--force"])
        self.assertEqual(prepped, ["ESID_002", "ESID_001"])


if __name__ == "__main__":
    unittest.main()
