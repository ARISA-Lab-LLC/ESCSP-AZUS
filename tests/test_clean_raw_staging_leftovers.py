"""Unit tests for Resources/clean_raw_staging_leftovers.py.

The tool classifies raw-side ``ESID_*_Staging`` leftovers against
their Staging_Area/Uploaded_Data twins and, only with
``--delete-verified-duplicates``, deletes the SHA-verified duplicates.

These tests prove: every verdict (verified duplicate, no twin, twin
without sentinel, cannot-verify), the dry-run default, the deletion
gates (sentinel, SHA match, inside-raw-root, not-its-own-twin), the
Staging_Area self-scan refusal, suffixed-ESID names, and the exit-code
contract.

Run from the project root:

    python3 -m unittest discover -s tests -v
"""

import csv
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "Resources"))

import clean_raw_staging_leftovers as tool  # noqa: E402


def _fs_is_case_insensitive(root: Path) -> bool:
    """Probe whether ``root``'s filesystem treats paths case-insensitively."""
    probe = root / "CaseProbe.tmp"
    probe.write_text("")
    try:
        return (root / "caseprobe.tmp").exists()
    finally:
        probe.unlink()


class _LeftoverTestCase(unittest.TestCase):
    """Fixture: tmp Raw_Data + tmp Staging_Area/Uploaded_Data twins."""

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
            patcher = mock.patch.object(tool, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_leftover(self, esid, zip_bytes=b"payload"):
        folder = self.raw / f"ESID_{esid}_Staging"
        folder.mkdir()
        (folder / f"ESID_{esid}.zip").write_bytes(zip_bytes)
        return folder

    def make_twin(self, esid, zip_bytes=b"payload", sentinel=True,
                  uploaded=False):
        if uploaded:
            folder = self.uploaded / f"ESID_{esid}_Uploaded"
        else:
            folder = self.staging / f"ESID_{esid}_Staging"
        folder.mkdir()
        (folder / f"ESID_{esid}.zip").write_bytes(zip_bytes)
        if sentinel:
            (folder / tool._SENTINEL).write_text("")
        return folder

    def run_main(self, *extra):
        argv = ["clean_raw_staging_leftovers.py", str(self.raw),
                "--output", str(self.root / "report.csv"), *extra]
        with mock.patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit) as ctx:
                tool.main()
        rows = []
        report = self.root / "report.csv"
        if report.exists():
            with open(report, newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
        return ctx.exception.code, rows


class TestClassification(_LeftoverTestCase):
    def test_verified_duplicate(self):
        left = self.make_leftover("004")
        self.make_twin("004")
        row = tool.classify_leftover(left)
        self.assertEqual(row["Verdict"], tool.VERIFIED_DUPLICATE)
        self.assertEqual(row["Twin Sentinel"], "yes")

    def test_no_twin(self):
        left = self.make_leftover("004")
        self.assertEqual(
            tool.classify_leftover(left)["Verdict"], tool.NO_TWIN
        )

    def test_twin_without_sentinel(self):
        left = self.make_leftover("004")
        self.make_twin("004", sentinel=False)
        self.assertEqual(
            tool.classify_leftover(left)["Verdict"], tool.TWIN_NO_SENTINEL
        )

    def test_differing_zips_cannot_verify(self):
        left = self.make_leftover("004", zip_bytes=b"old build")
        self.make_twin("004", zip_bytes=b"new build")
        row = tool.classify_leftover(left)
        self.assertEqual(row["Verdict"], tool.CANNOT_VERIFY)
        self.assertIn("differ", row["ZIPs Match"])

    def test_missing_raw_zip_cannot_verify(self):
        folder = self.raw / "ESID_004_Staging"
        folder.mkdir()  # partial build — no ZIP
        self.make_twin("004")
        self.assertEqual(
            tool.classify_leftover(folder)["Verdict"], tool.CANNOT_VERIFY
        )

    def test_uploaded_twin_also_counts(self):
        left = self.make_leftover("004")
        self.make_twin("004", uploaded=True)
        row = tool.classify_leftover(left)
        self.assertEqual(row["Verdict"], tool.VERIFIED_DUPLICATE)
        self.assertIn("Uploaded", row["Twin Location"])

    def test_suffixed_esid_names_are_recognized(self):
        left = self.make_leftover("122_Part_1_of_2")
        self.make_twin("122_Part_1_of_2")
        row = tool.classify_leftover(left)
        self.assertEqual(row["ESID#"], "122_Part_1_of_2")
        self.assertEqual(row["Verdict"], tool.VERIFIED_DUPLICATE)
        self.assertEqual(
            [p.name for p in tool.find_leftovers(self.raw)],
            ["ESID_122_Part_1_of_2_Staging"],
        )


class TestDryRunDefault(_LeftoverTestCase):
    def test_nothing_deleted_and_exit_1(self):
        left = self.make_leftover("004")
        self.make_twin("004")
        code, rows = self.run_main()
        self.assertEqual(code, 1)  # findings remain (dry run)
        self.assertTrue(left.exists(), "dry run must not delete anything")
        self.assertEqual(rows[0]["Action Taken"], "none (dry run)")

    def test_empty_raw_folder_exits_0(self):
        code, rows = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(rows, [])


class TestDeletionMode(_LeftoverTestCase):
    def test_verified_duplicate_is_deleted(self):
        left = self.make_leftover("004")
        twin = self.make_twin("004")
        code, rows = self.run_main("--delete-verified-duplicates")
        self.assertEqual(code, 0)  # everything resolved
        self.assertFalse(left.exists())
        self.assertTrue(twin.exists(), "the twin must never be touched")
        self.assertEqual(rows[0]["Action Taken"],
                         "deleted raw-side duplicate")

    def test_unverified_leftovers_survive_deletion_mode(self):
        kept_no_twin = self.make_leftover("007")
        kept_differs = self.make_leftover("012", zip_bytes=b"old")
        self.make_twin("012", zip_bytes=b"new")
        deleted = self.make_leftover("004")
        self.make_twin("004")
        code, _ = self.run_main("--delete-verified-duplicates")
        self.assertEqual(code, 1)  # unresolved leftovers remain
        self.assertTrue(kept_no_twin.exists())
        self.assertTrue(kept_differs.exists())
        self.assertFalse(deleted.exists())

    def test_delete_regates_before_removing(self):
        """The deletion re-checks the gates itself: a twin that loses
        its sentinel between classify and delete is refused."""
        left = self.make_leftover("004")
        twin = self.make_twin("004")
        (twin / tool._SENTINEL).unlink()  # sentinel vanishes
        self.assertFalse(tool.delete_verified_duplicate(left, self.raw))
        self.assertTrue(left.exists())

    def test_delete_refuses_paths_outside_raw_root(self):
        left = self.make_leftover("004")
        self.make_twin("004")
        other_root = self.root / "elsewhere"
        other_root.mkdir()
        self.assertFalse(tool.delete_verified_duplicate(left, other_root))
        self.assertTrue(left.exists())

    def test_delete_refuses_folder_that_is_its_own_twin(self):
        twin = self.make_twin("004")
        self.assertFalse(tool.delete_verified_duplicate(twin, self.staging))
        self.assertTrue(twin.exists())


class TestAliasAndIndependence(_LeftoverTestCase):
    """The review-confirmed attacks: path aliases and non-independent
    ZIP copies must never classify as (or delete) a verified duplicate."""

    def test_case_alias_scan_of_staging_area_is_refused(self):
        """THE critical case: scanning 'staging_area' on a
        case-insensitive filesystem is the same physical directory as
        Staging_Area — it must exit 2 and delete nothing."""
        if not _fs_is_case_insensitive(self.root):
            self.skipTest("requires a case-insensitive filesystem")
        twin = self.make_twin("004")
        alias = Path(str(self.staging).replace("Staging_Area",
                                               "staging_area"))
        argv = ["clean_raw_staging_leftovers.py", str(alias),
                "--delete-verified-duplicates"]
        with mock.patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit) as ctx:
                tool.main()
        self.assertEqual(ctx.exception.code, 2)
        self.assertTrue(twin.exists(),
                        "a case-alias scan must never delete anything")

    def test_delete_refuses_case_alias_self_twin(self):
        """Even reached directly, deletion must recognize a case-alias
        of the twin as the same physical folder and refuse."""
        if not _fs_is_case_insensitive(self.root):
            self.skipTest("requires a case-insensitive filesystem")
        twin = self.make_twin("004")
        alias_folder = Path(
            str(twin).replace("Staging_Area", "staging_area")
        )
        alias_root = Path(str(self.staging).replace("Staging_Area",
                                                    "staging_area"))
        self.assertFalse(
            tool.delete_verified_duplicate(alias_folder, alias_root)
        )
        self.assertTrue(twin.exists())

    def test_symlinked_twin_zip_is_not_a_copy(self):
        left = self.make_leftover("004")
        twin = self.staging / "ESID_004_Staging"
        twin.mkdir()
        (twin / tool._SENTINEL).write_text("")
        os.symlink(left / "ESID_004.zip", twin / "ESID_004.zip")
        row = tool.classify_leftover(left)
        self.assertEqual(row["Verdict"], tool.CANNOT_VERIFY)
        self.assertIn("independent", row["ZIPs Match"])
        self.assertFalse(tool.delete_verified_duplicate(left, self.raw))
        self.assertTrue(left.exists())

    def test_hardlinked_twin_zip_is_not_a_copy(self):
        left = self.make_leftover("004")
        twin = self.staging / "ESID_004_Staging"
        twin.mkdir()
        (twin / tool._SENTINEL).write_text("")
        os.link(left / "ESID_004.zip", twin / "ESID_004.zip")
        row = tool.classify_leftover(left)
        self.assertEqual(row["Verdict"], tool.CANNOT_VERIFY)
        self.assertFalse(tool.delete_verified_duplicate(left, self.raw))
        self.assertTrue(left.exists())

    def test_diverging_upload_artifacts_block_deletion(self):
        """A raw-side upload_state.json the twin lacks could be the
        only link to a Zenodo draft — never delete it."""
        left = self.make_leftover("004")
        (left / "upload_state.json").write_text('{"record_id": "111"}')
        self.make_twin("004")
        row = tool.classify_leftover(left)
        self.assertEqual(row["Verdict"], tool.CANNOT_VERIFY)
        self.assertIn("upload artifacts", row["ZIPs Match"])
        self.assertFalse(tool.delete_verified_duplicate(left, self.raw))
        self.assertTrue(left.exists())

    def test_identical_upload_artifacts_do_not_block(self):
        left = self.make_leftover("004")
        (left / "upload_state.json").write_text('{"record_id": "111"}')
        twin = self.make_twin("004")
        (twin / "upload_state.json").write_text('{"record_id": "111"}')
        self.assertEqual(
            tool.classify_leftover(left)["Verdict"],
            tool.VERIFIED_DUPLICATE,
        )

    def test_unreadable_zip_is_cannot_verify_not_a_crash(self):
        left = self.make_leftover("004")
        twin = self.staging / "ESID_004_Staging"
        twin.mkdir()
        (twin / tool._SENTINEL).write_text("")
        (twin / "ESID_004.zip").mkdir()  # a DIRECTORY named like the zip
        row = tool.classify_leftover(left)
        self.assertEqual(row["Verdict"], tool.CANNOT_VERIFY)

    def test_twin_zip_swapped_during_hashing_is_refused(self):
        """TOCTOU: the twin ZIP changing between the pre-hash stat and
        the post-hash stat must refuse the deletion."""
        left = self.make_leftover("004")
        twin = self.make_twin("004")
        twin_zip = twin / "ESID_004.zip"
        calls = {"n": 0}

        def mutating_sha(path):
            calls["n"] += 1
            if calls["n"] == 2:  # mutate the twin as it is being hashed
                twin_zip.write_bytes(b"swapped underneath the tool!")
            return "same-hash"

        with mock.patch.object(tool.azus_common, "calculate_sha512",
                               side_effect=mutating_sha):
            self.assertFalse(
                tool.delete_verified_duplicate(left, self.raw)
            )
        self.assertTrue(left.exists())


class TestTwinLookup(_LeftoverTestCase):
    """find_twin: canonical-name fallback, sentineled preference, and
    the uploaded-twin-without-sentinel verdict."""

    def test_unpadded_leftover_finds_canonical_twin(self):
        left = self.make_leftover("4")  # ESID_4_Staging
        self.make_twin("004")
        row = tool.classify_leftover(left)
        self.assertEqual(row["Verdict"], tool.VERIFIED_DUPLICATE)
        self.assertIn("ESID_004_Staging", row["Twin Location"])

    def test_sentineled_uploaded_twin_beats_unsentineled_staging_twin(self):
        left = self.make_leftover("004")
        self.make_twin("004", sentinel=False)
        self.make_twin("004", uploaded=True)
        row = tool.classify_leftover(left)
        self.assertEqual(row["Verdict"], tool.VERIFIED_DUPLICATE)
        self.assertIn("Uploaded", row["Twin Location"])

    def test_uploaded_twin_without_sentinel_gets_its_own_verdict(self):
        left = self.make_leftover("004")
        self.make_twin("004", sentinel=False, uploaded=True)
        self.assertEqual(
            tool.classify_leftover(left)["Verdict"],
            tool.UPLOADED_TWIN_NO_SENTINEL,
        )

    def test_lowercase_staging_tail_is_still_a_leftover(self):
        folder = self.raw / "ESID_004_staging"
        folder.mkdir()
        (folder / "ESID_004.zip").write_bytes(b"payload")
        self.assertEqual(
            [p.name for p in tool.find_leftovers(self.raw)],
            ["ESID_004_staging"],
        )


class TestIncrementalReport(_LeftoverTestCase):
    def test_rows_are_flushed_before_later_failures(self):
        """A crash mid-run must not lose the audit record of leftovers
        already processed (deletions included)."""
        self.make_leftover("004")
        self.make_twin("004")
        self.make_leftover("007")
        real = tool.classify_leftover

        def explode_on_007(folder):
            if "007" in folder.name:
                raise RuntimeError("simulated crash")
            return real(folder)

        argv = ["clean_raw_staging_leftovers.py", str(self.raw),
                "--output", str(self.root / "report.csv")]
        with mock.patch.object(tool, "classify_leftover",
                               side_effect=explode_on_007), \
             mock.patch.object(sys, "argv", argv):
            with self.assertRaises(RuntimeError):
                tool.main()
        with open(self.root / "report.csv", newline="",
                  encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ESID#"], "004")


class TestSelfScanRefusal(_LeftoverTestCase):
    def test_scanning_staging_area_exits_2(self):
        self.make_twin("004")
        argv = ["clean_raw_staging_leftovers.py", str(self.staging),
                "--delete-verified-duplicates"]
        with mock.patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit) as ctx:
                tool.main()
        self.assertEqual(ctx.exception.code, 2)
        self.assertTrue((self.staging / "ESID_004_Staging").exists())

    def test_missing_raw_dir_exits_2(self):
        argv = ["clean_raw_staging_leftovers.py",
                str(self.root / "nope")]
        with mock.patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit) as ctx:
                tool.main()
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
