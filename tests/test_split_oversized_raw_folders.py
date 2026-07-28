"""Unit tests for Resources/split_oversized_raw_folders.py.

The tool moves real WAV audio between folders, so these tests are
organised by the safety property each class guarantees rather than by
function.  The load-bearing ones are TestMoveIsLossless (no byte is ever
lost or altered) and TestResumeIsIdentical (an interrupted run re-plans to
the same boundary and finishes).

Run from the project root:

    python3 -m unittest discover -s tests -v
"""

import csv
import errno
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "Resources"))

import azus_common  # noqa: E402
import split_oversized_raw_folders as tool  # noqa: E402


# --- helpers (same WAV builders as test_audit_wav_integrity) --------------

def riff_header(declared_total: int) -> bytes:
    """First 12 bytes of a WAV whose header declares `declared_total`."""
    chunk = declared_total - 8
    return b"RIFF" + chunk.to_bytes(4, "little") + b"WAVE"


def write_wav(path: Path, total_size: int) -> None:
    """A well-formed WAV whose actual and declared lengths both equal
    `total_size` (stat and header agree).  A size of 0 writes a genuinely
    empty file — the zero-byte case the tool reports on."""
    if total_size == 0:
        path.write_bytes(b"")
        return
    path.write_bytes(riff_header(total_size) + b"\x00" * (total_size - 12))


def write_truncated_wav(path: Path, declared: int, actual: int) -> None:
    """A WAV whose header declares `declared` bytes but whose file is only
    `actual` bytes."""
    path.write_bytes(riff_header(declared) + b"\x00" * (actual - 12))


def snapshot(folder: Path):
    """{name: (inode, size)} for every file in a folder — proves a dry run
    changed nothing, and proves a same-device move kept the same inode."""
    return {
        p.name: (p.stat().st_ino, p.stat().st_size)
        for p in sorted(folder.iterdir()) if p.is_file()
    }


def wav_set(*names):
    """Build a {name: size} mapping with distinct, realistic sizes."""
    return {name: 2048 + 16 * index for index, name in enumerate(names)}


_FIVE = {
    "20240408_120000.WAV": 4096,
    "20240408_121000.WAV": 4096,
    "20240408_122000.WAV": 4096,
    "20240408_123000.WAV": 4096,
    "20240408_124000.WAV": 4096,
}


class _SplitTestCase(unittest.TestCase):
    """Fixture: a temp Raw_Data plus temp Staging_Area / Uploaded_Data, with
    the module's layout constants patched so the real project tree is never
    touched."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.raw = self.root / "Raw_Data"
        self.staging = self.root / "Staging_Area"
        self.uploaded = self.root / "Uploaded_Data"
        for folder in (self.raw, self.staging, self.uploaded):
            folder.mkdir()
        for name, value in (("_STAGING_AREA", self.staging),
                            ("_UPLOADED_DATA", self.uploaded)):
            patcher = mock.patch.object(tool, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.collectors = self.root / "collectors.csv"
        self.write_collectors(["445_Part_1_of_2", "445_Part_2_of_2"])

    def write_collectors(self, esids):
        """Write a minimal collectors spreadsheet with the given ESID rows."""
        with open(self.collectors, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=["ESID", "Latitude"])
            writer.writeheader()
            for esid in esids:
                writer.writerow({"ESID": esid, "Latitude": "31.39"})

    def make_pair(self, esid="445", wavs=None, extras=("CONFIG.TXT",),
                  part2_wavs=None, make_part2=True):
        """Create a Part 1 / Part 2 pair. Returns (part1, part2)."""
        part1 = self.raw / f"ESID#{esid}_Part_1_of_2"
        part2 = self.raw / f"ESID#{esid}_Part_2_of_2"
        part1.mkdir()
        if make_part2:
            part2.mkdir()
        for name, size in (wavs if wavs is not None else _FIVE).items():
            write_wav(part1 / name, size)
        for name, size in (part2_wavs or {}).items():
            write_wav(part2 / name, size)
        for name in extras:
            (part1 / name).write_text(f"contents of {name}\n")
        return part1, part2

    def run_main(self, *extra):
        """Run main() and return (exit_code, report_rows)."""
        argv = ["split_oversized_raw_folders.py", str(self.raw),
                "--output", str(self.root / "report.csv"),
                "--collectors-csv", str(self.collectors), *extra]
        with mock.patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit) as ctx:
                tool.main()
        rows = []
        report = self.root / "report.csv"
        if report.exists():
            with open(report, newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
        return ctx.exception.code, rows


# --- pure: the balancing algorithm ---------------------------------------

class TestBalancing(unittest.TestCase):
    """The cut is the best available and the plan self-check proves it."""

    def test_equal_sizes_even_count_splits_exactly(self):
        self.assertEqual(tool.choose_cut_index([100] * 6), 3)

    def test_equal_sizes_odd_count_puts_extra_in_part_1(self):
        # Two cuts tie; maximising k leaves the odd file in Part 1.
        self.assertEqual(tool.choose_cut_index([100] * 5), 3)

    def test_tie_breaks_to_the_largest_index(self):
        # A genuine tie: k=1 -> |1-3|=2 and k=2 -> |3-1|=2. Take the later,
        # which moves fewer files.
        self.assertEqual(tool.choose_cut_index([1, 2, 1]), 2)

    def test_chosen_cut_is_globally_minimal(self):
        for sizes in ([1, 1, 1, 97], [50, 40, 30, 20, 10], [7], [7, 7],
                      [1, 2, 4, 8, 16, 32, 64], [100, 1, 1, 1, 1]):
            cut = tool.choose_cut_index(sizes)
            if cut is None:
                self.assertLess(len(sizes), 2)
                continue
            best = min(
                abs(sum(sizes[:k]) - sum(sizes[k:]))
                for k in range(1, len(sizes))
            )
            self.assertEqual(
                abs(sum(sizes[:cut]) - sum(sizes[cut:])), best, sizes
            )

    def test_one_dominant_file_still_leaves_both_sides_non_empty(self):
        plan = tool.plan_split(wav_set(
            "20240408_120000.WAV", "20240408_121000.WAV"
        ) | {"20240408_122000.WAV": 10 ** 9})
        self.assertTrue(plan.part1_names)
        self.assertTrue(plan.part2_names)

    def test_too_few_wavs_has_no_cut(self):
        self.assertIsNone(tool.choose_cut_index([]))
        self.assertIsNone(tool.choose_cut_index([5]))
        self.assertIsNone(tool.plan_split({}).cut_index)
        self.assertIsNone(tool.plan_split({"20240408_120000.WAV": 5}).cut_index)

    def test_plan_verify_is_clean_and_halves_reconstruct_the_total(self):
        plan = tool.plan_split(dict(_FIVE))
        self.assertEqual(plan.verify(), [])
        self.assertEqual(plan.part1_bytes + plan.part2_bytes, plan.total_bytes)
        self.assertEqual(
            set(plan.part1_names) | set(plan.part2_names), set(_FIVE)
        )
        self.assertFalse(set(plan.part1_names) & set(plan.part2_names))

    def test_verify_catches_a_suboptimal_cut(self):
        good = tool.plan_split(dict(_FIVE))
        bad = tool.SplitPlan(
            ordered_names=good.ordered_names, sizes=good.sizes, cut_index=1
        )
        self.assertTrue(any("not the minimum" in e for e in bad.verify()))


# --- pure: ordering -------------------------------------------------------

class TestOrdering(unittest.TestCase):
    """Chronological where timestamps exist, deterministic where they don't."""

    def test_parses_the_project_convention(self):
        self.assertEqual(
            tool.parse_wav_timestamp("20240408_120000.WAV"), "20240408120000"
        )

    def test_rejects_non_conforming_names(self):
        for name in ("5D8F3A2B.WAV", "recording.WAV", "20241301_000000.WAV",
                     "20240408_996060.WAV", "20240408.WAV", "2024048_12000.WAV"):
            self.assertIsNone(tool.parse_wav_timestamp(name), name)

    def test_tolerates_a_trailing_part(self):
        self.assertEqual(
            tool.parse_wav_timestamp("20240408_120000_1.WAV"), "20240408120000"
        )

    def test_orders_chronologically_across_a_month_boundary(self):
        names = ["20240401_000000.WAV", "20240331_235959.WAV"]
        self.assertEqual(
            sorted(names, key=tool.wav_sort_key),
            ["20240331_235959.WAV", "20240401_000000.WAV"],
        )

    def test_unparseable_names_sort_last_and_reset_clock_sorts_first(self):
        names = ["5D8F3A2B.WAV", "20240408_120000.WAV", "19700101_000000.WAV"]
        self.assertEqual(
            sorted(names, key=tool.wav_sort_key),
            ["19700101_000000.WAV", "20240408_120000.WAV", "5D8F3A2B.WAV"],
        )

    def test_key_is_a_total_order_on_case_variants(self):
        keys = {tool.wav_sort_key(n) for n in ("abc.WAV", "ABC.WAV", "AbC.wav")}
        self.assertEqual(len(keys), 3)

    def test_planning_is_reproducible(self):
        sizes = dict(_FIVE)
        self.assertEqual(
            tool.plan_split(sizes).ordered_names,
            tool.plan_split(dict(reversed(list(sizes.items())))).ordered_names,
        )

    def test_unparseable_names_are_counted_and_reported(self):
        plan = tool.plan_split({**_FIVE, "5D8F3A2B.WAV": 4096})
        self.assertEqual(plan.unparseable_names, ("5D8F3A2B.WAV",))

    def test_part2_name_preserves_spelling_and_case(self):
        self.assertEqual(
            tool.part2_folder_name("ESID#445_Part_1_of_2"),
            "ESID#445_Part_2_of_2",
        )
        self.assertEqual(
            tool.part2_folder_name("ESID_445_part_1_of_2"),
            "ESID_445_part_2_of_2",
        )
        self.assertIsNone(tool.part2_folder_name("ESID#445"))

    def test_parse_part_esid(self):
        self.assertEqual(tool.parse_part_esid("445_Part_1_of_2"), ("445", 1, 2))
        self.assertEqual(tool.parse_part_esid("445_Part_2_of_3"), ("445", 2, 3))
        self.assertIsNone(tool.parse_part_esid("445"))
        self.assertIsNone(tool.parse_part_esid("120A"))


# --- dry run --------------------------------------------------------------

class TestDryRunMutatesNothing(_SplitTestCase):
    """Without --perform-split not one byte moves."""

    def test_nothing_changes_and_exit_is_1(self):
        part1, part2 = self.make_pair()
        before1, before2 = snapshot(part1), snapshot(part2)
        code, rows = self.run_main()
        self.assertEqual(snapshot(part1), before1, "dry run must not move anything")
        self.assertEqual(snapshot(part2), before2, "dry run must not create anything")
        self.assertEqual(rows[0]["Verdict"], tool.SPLIT_PLANNED)
        self.assertEqual(rows[0]["Action Taken"], "none (dry run)")
        self.assertEqual(rows[0]["WAVs Moved"], "0")
        self.assertEqual(code, 1)

    def test_report_shows_the_cut_boundary(self):
        self.make_pair()
        _code, rows = self.run_main()
        self.assertIn("->", rows[0]["Cut Boundary"])
        self.assertEqual(rows[0]["Part 1 WAVs"], "3")
        self.assertEqual(rows[0]["Part 2 WAVs"], "2")


# --- what must never be copied -------------------------------------------

class TestExcludedFilesNeverCopied(_SplitTestCase):
    """A companion copy must never duplicate a Zenodo draft binding."""

    def test_upload_artifacts_and_hidden_files_are_not_copied(self):
        part1, part2 = self.make_pair(extras=(
            "CONFIG.TXT", "upload_state.json", "ESID_445_request_log.json",
            "ESID_445_zip_attempt_upload.csv", "ESID_445.zip",
            ".DS_Store", ".prep_complete", "._20240408_120000.WAV",
        ))
        (part1 / "ESID_445_Staging").mkdir()
        (part1 / "ESID_445_Staging" / "inner.txt").write_text("x\n")
        code, rows = self.run_main("--perform-split")
        self.assertEqual(rows[0]["Verdict"], tool.SPLIT_DONE)
        for forbidden in ("upload_state.json", "ESID_445_request_log.json",
                          "ESID_445_zip_attempt_upload.csv", "ESID_445.zip",
                          ".DS_Store", ".prep_complete",
                          "._20240408_120000.WAV", "ESID_445_Staging"):
            self.assertFalse(
                (part2 / forbidden).exists(),
                f"{forbidden} must never be copied into Part 2",
            )
        self.assertTrue((part2 / "CONFIG.TXT").is_file())
        self.assertEqual(
            azus_common.calculate_sha512(str(part1 / "CONFIG.TXT")),
            azus_common.calculate_sha512(str(part2 / "CONFIG.TXT")),
        )
        # Skips are recorded for review, not treated as problems: making
        # routine macOS junk force a nonzero exit would train the operator
        # to ignore exit codes.
        self.assertIn("upload_state.json", rows[0]["Non-WAV Skipped"])
        self.assertIn("ESID_445_Staging", rows[0]["Non-WAV Skipped"])
        self.assertEqual(code, 0)

    def test_hidden_wav_predicate_is_stricter_than_the_audit_one(self):
        """A dot-prefixed .wav must not count as content, even though
        audit_wav_integrity's predicate (which only excludes AppleDouble
        sidecars) accepts it."""
        self.assertTrue(tool.is_split_wav_name("20240408_120000.WAV"))
        for hidden in (".hidden.wav", ".20240408_120000.WAV",
                       "._20240408_120000.WAV"):
            self.assertFalse(tool.is_split_wav_name(hidden), hidden)
        self.assertFalse(tool.is_split_wav_name(".DS_Store"))

    def test_hidden_wav_is_neither_planned_nor_moved(self):
        part1, part2 = self.make_pair()
        write_wav(part1 / ".hidden.wav", 4096)
        write_wav(part1 / "._20240408_120000.WAV", 4096)
        code, rows = self.run_main("--perform-split")
        self.assertEqual(rows[0]["Verdict"], tool.SPLIT_DONE)
        # Counted as the five real recordings only.
        self.assertEqual(rows[0]["WAV Count"], str(len(_FIVE)))
        self.assertIn("hidden", rows[0]["Notes"])
        # Left where it was, in both cases.
        self.assertTrue((part1 / ".hidden.wav").is_file())
        self.assertFalse((part2 / ".hidden.wav").exists())
        self.assertTrue((part1 / "._20240408_120000.WAV").is_file())
        self.assertFalse((part2 / "._20240408_120000.WAV").exists())
        self.assertEqual(code, 1)

    def test_hidden_wav_does_not_shift_the_cut(self):
        """Excluding it must not change where the real recordings split."""
        part1, _part2 = self.make_pair()
        before, _u, _w = tool.wav_sizes(part1)
        write_wav(part1 / ".hidden.wav", 999_999)
        after, _u, warnings = tool.wav_sizes(part1)
        self.assertEqual(set(after), set(before))
        self.assertEqual(
            tool.plan_split(after).cut_index, tool.plan_split(before).cut_index
        )
        self.assertTrue(any("hidden" in w for w in warnings))

    def test_symlinked_companion_is_skipped(self):
        part1, part2 = self.make_pair()
        target = self.root / "outside.txt"
        target.write_text("outside\n")
        os.symlink(target, part1 / "linked.txt")
        self.run_main("--perform-split")
        self.assertFalse((part2 / "linked.txt").exists())

    def test_part2_bound_to_a_draft_is_refused_even_with_overrides(self):
        part1, part2 = self.make_pair()
        (part2 / "upload_state.json").write_text('{"record_id": "1"}')
        before = snapshot(part1)
        code, rows = self.run_main(
            "--perform-split", "--resume", "--allow-still-oversized"
        )
        self.assertEqual(rows[0]["Verdict"], tool.PART2_BOUND_TO_DRAFT)
        self.assertEqual(snapshot(part1), before)
        self.assertEqual(code, 1)

    def test_missing_config_txt_is_reported(self):
        self.make_pair(extras=())
        _code, rows = self.run_main()
        self.assertIn("CONFIG.TXT", rows[0]["Notes"])

    def test_differing_companion_is_a_collision_and_no_wav_moves(self):
        part1, part2 = self.make_pair()
        (part2 / "CONFIG.TXT").write_text("DIFFERENT\n")
        before = snapshot(part1)
        code, rows = self.run_main("--perform-split")
        self.assertEqual(rows[0]["Verdict"], tool.NAME_COLLISION)
        self.assertEqual(snapshot(part1), before, "no WAV may move after a copy failure")
        self.assertEqual(code, 1)


# --- the move itself ------------------------------------------------------

class TestMoveIsLossless(_SplitTestCase):
    """Every byte that existed before the split still exists after it."""

    def test_same_device_move_preserves_inode_and_content(self):
        part1, part2 = self.make_pair()
        before = snapshot(part1)
        digests = {
            name: azus_common.calculate_sha512(str(part1 / name)) for name in _FIVE
        }
        code, rows = self.run_main("--perform-split")
        self.assertEqual(rows[0]["Verdict"], tool.SPLIT_DONE)
        self.assertEqual(code, 0)

        for name in _FIVE:
            in1, in2 = (part1 / name).exists(), (part2 / name).exists()
            self.assertNotEqual(in1, in2, f"{name} must be in exactly one folder")
            landed = part1 if in1 else part2
            self.assertEqual(
                azus_common.calculate_sha512(str(landed / name)), digests[name]
            )
            # Same filesystem -> rename, not copy: the inode is unchanged.
            self.assertEqual(
                (landed / name).stat().st_ino, before[name][0], name
            )

    def test_union_is_identical_before_and_after(self):
        part1, part2 = self.make_pair()
        before, _u, _w = tool.wav_sizes(part1)
        self.run_main("--perform-split")
        after = {}
        for folder in (part1, part2):
            sizes, _u, _w = tool.wav_sizes(folder)
            after.update(sizes)
        matched, notes = tool.compare_file_maps(before, after)
        self.assertTrue(matched, notes)

    def test_cross_device_path_copies_verifies_and_unlinks(self):
        part1, part2 = self.make_pair()
        digests = {
            name: azus_common.calculate_sha512(str(part1 / name)) for name in _FIVE
        }
        with mock.patch.object(
            tool.os, "rename",
            side_effect=OSError(errno.EXDEV, "cross-device link"),
        ):
            code, rows = self.run_main("--perform-split")
        self.assertEqual(rows[0]["Verdict"], tool.SPLIT_DONE)
        self.assertEqual(code, 0)
        for name in _FIVE:
            in1, in2 = (part1 / name).exists(), (part2 / name).exists()
            self.assertNotEqual(in1, in2, name)
            landed = part1 if in1 else part2
            self.assertEqual(
                azus_common.calculate_sha512(str(landed / name)), digests[name]
            )
        self.assertFalse(list(part2.glob(".*.partial")), "no partial may survive")

    def test_cross_device_hash_mismatch_keeps_the_source(self):
        part1, part2 = self.make_pair()
        real_sha = azus_common.calculate_sha512

        def fake_sha(path):
            """Make every .partial copy hash differently from its source."""
            return "MISMATCH" if ".partial" in str(path) else real_sha(path)

        with mock.patch.object(
            tool.os, "rename", side_effect=OSError(errno.EXDEV, "cross-device")
        ), mock.patch.object(
            tool.azus_common, "calculate_sha512", side_effect=fake_sha
        ):
            code, rows = self.run_main("--perform-split")
        self.assertEqual(rows[0]["Verdict"], tool.INCOMPLETE)
        self.assertEqual(rows[0]["WAVs Moved"], "0")
        for name in _FIVE:
            self.assertTrue((part1 / name).is_file(), f"{name} source must survive")
        self.assertFalse(list(part2.glob(".*.partial")))
        self.assertEqual(code, 1)

    def test_stale_partial_is_removed_before_moving(self):
        part1, part2 = self.make_pair()
        stale = part2 / ".20240408_124000.WAV.partial"
        stale.write_bytes(b"junk")
        self.run_main("--perform-split")
        self.assertFalse(stale.exists())

    def test_a_truncating_rename_aborts_the_pair(self):
        part1, part2 = self.make_pair()

        def truncating_rename(src, dst):
            """Simulate a filesystem that reports one device but copies badly."""
            Path(dst).write_bytes(b"\x00" * 10)
            Path(src).unlink()

        with mock.patch.object(tool.os, "rename", side_effect=truncating_rename):
            code, rows = self.run_main("--perform-split")
        self.assertEqual(rows[0]["Verdict"], tool.INCOMPLETE)
        self.assertIn("atomically", rows[0]["Notes"])
        self.assertEqual(code, 1)

    def test_a_file_that_changed_since_planning_is_not_moved(self):
        part1, part2 = self.make_pair()
        real_assess = tool.assess_pair

        def grow_then_assess(pair, args, raw_root, esid_column):
            """Let the reviewed plan be built, then enlarge one file."""
            row, plan = real_assess(pair, args, raw_root, esid_column)
            if plan is not None and (pair.part1 / "20240408_124000.WAV").exists():
                write_wav(pair.part1 / "20240408_124000.WAV", 9000)
            return row, plan

        with mock.patch.object(tool, "assess_pair", side_effect=grow_then_assess):
            code, rows = self.run_main("--perform-split")
        self.assertEqual(rows[0]["Verdict"], tool.PLAN_CHANGED)
        self.assertEqual(code, 1)


# --- resume ---------------------------------------------------------------

class TestResumeIsIdentical(_SplitTestCase):
    """The union-derived plan makes an interrupted run finish correctly."""

    def test_plan_is_unchanged_by_a_partial_move(self):
        part1, part2 = self.make_pair()
        full = tool.plan_split(dict(_FIVE))
        # Move the Part-2-bound files by hand, as an interrupted run would.
        for name in full.part2_names:
            (part1 / name).rename(part2 / name)
        union = {}
        for folder in (part1, part2):
            sizes, _u, _w = tool.wav_sizes(folder)
            union.update(sizes)
        self.assertEqual(tool.plan_split(union).part2_names, full.part2_names)
        self.assertEqual(tool.plan_split(union).cut_index, full.cut_index)

    def test_resume_moves_only_the_remainder(self):
        part1, part2 = self.make_pair()
        plan = tool.plan_split(dict(_FIVE))
        moved_early = plan.part2_names[0]
        (part1 / moved_early).rename(part2 / moved_early)
        code, rows = self.run_main("--perform-split", "--resume")
        self.assertEqual(rows[0]["Verdict"], tool.SPLIT_DONE)
        self.assertEqual(int(rows[0]["WAVs Moved"]), len(plan.part2_names) - 1)
        self.assertEqual(code, 0)

    def test_second_run_is_a_no_op(self):
        part1, part2 = self.make_pair()
        first_code, _rows = self.run_main("--perform-split")
        self.assertEqual(first_code, 0)
        before1, before2 = snapshot(part1), snapshot(part2)
        code, rows = self.run_main("--perform-split", "--resume")
        self.assertEqual(rows[0]["Verdict"], tool.ALREADY_SPLIT)
        self.assertEqual(rows[0]["Action Taken"], "none (already split)")
        self.assertEqual(snapshot(part1), before1)
        self.assertEqual(snapshot(part2), before2)
        self.assertEqual(code, 0)

    def test_crash_between_rename_and_unlink_is_healed(self):
        part1, part2 = self.make_pair()
        plan = tool.plan_split(dict(_FIVE))
        duplicated = plan.part2_names[0]
        # Same name in BOTH folders, byte-identical: the one duplicate state
        # the cross-device path can crash into.
        import shutil as _shutil
        _shutil.copy2(part1 / duplicated, part2 / duplicated)
        code, rows = self.run_main("--perform-split", "--resume")
        self.assertEqual(rows[0]["Verdict"], tool.SPLIT_DONE)
        self.assertFalse((part1 / duplicated).exists(), "source must be dropped")
        self.assertTrue((part2 / duplicated).is_file())
        self.assertEqual(code, 0)

    def test_same_name_at_different_sizes_is_refused(self):
        part1, part2 = self.make_pair()
        plan = tool.plan_split(dict(_FIVE))
        name = plan.part2_names[0]
        write_wav(part2 / name, 9000)  # different size — an interrupted copy
        before = snapshot(part1)
        code, rows = self.run_main("--perform-split", "--resume")
        self.assertEqual(rows[0]["Verdict"], tool.NAME_COLLISION)
        self.assertEqual(snapshot(part1), before)
        self.assertEqual(code, 1)

    def test_report_rows_survive_a_later_pair_failing(self):
        self.make_pair("445")
        self.make_pair("446")
        self.write_collectors([
            "445_Part_1_of_2", "445_Part_2_of_2",
            "446_Part_1_of_2", "446_Part_2_of_2",
        ])
        calls = {"n": 0}
        real_execute = tool.execute_split

        def failing_second(*a, **kw):
            """Succeed for the first pair, raise for the second."""
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("boom")
            return real_execute(*a, **kw)

        with mock.patch.object(tool, "execute_split", side_effect=failing_second):
            with mock.patch.object(sys, "argv", [
                "split_oversized_raw_folders.py", str(self.raw),
                "--output", str(self.root / "report.csv"),
                "--collectors-csv", str(self.collectors), "--perform-split",
            ]):
                with self.assertRaises(RuntimeError):
                    tool.main()
        with open(self.root / "report.csv", newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 1, "the first pair's row must be flushed")
        self.assertEqual(rows[0]["ESID#"], "445")


# --- refusals -------------------------------------------------------------

class TestRefusalsFailClosed(_SplitTestCase):
    """Every refusal leaves the data exactly as it was."""

    def test_missing_part2_is_reported_and_never_created(self):
        part1, part2 = self.make_pair(make_part2=False)
        code, rows = self.run_main("--perform-split")
        self.assertEqual(rows[0]["Verdict"], tool.PART2_MISSING)
        self.assertFalse(part2.exists(), "the tool must not create Part 2")
        self.assertEqual(len(snapshot(part1)), len(_FIVE) + 1)
        self.assertEqual(code, 1)

    def test_too_few_wavs_is_refused(self):
        for wavs in ({}, {"20240408_120000.WAV": 4096}):
            with self.subTest(count=len(wavs)):
                self.setUp()
                self.make_pair(wavs=wavs)
                code, rows = self.run_main("--perform-split")
                self.assertEqual(rows[0]["Verdict"], tool.TOO_FEW_WAVS)
                self.assertEqual(code, 1)

    def test_part2_with_wavs_needs_resume(self):
        part1, part2 = self.make_pair()
        plan = tool.plan_split(dict(_FIVE))
        name = plan.part2_names[0]
        (part1 / name).rename(part2 / name)
        before = snapshot(part1)
        code, rows = self.run_main("--perform-split")
        self.assertEqual(rows[0]["Verdict"], tool.PART2_HAS_WAVS)
        self.assertEqual(snapshot(part1), before)
        self.assertEqual(code, 1)

    def test_part1_bound_wav_in_part2_is_refused_even_with_resume(self):
        part1, part2 = self.make_pair()
        plan = tool.plan_split(dict(_FIVE))
        wrong = plan.part1_names[0]  # belongs in Part 1
        (part1 / wrong).rename(part2 / wrong)
        code, rows = self.run_main("--perform-split", "--resume")
        self.assertEqual(rows[0]["Verdict"], tool.PART2_UNEXPECTED_WAVS)
        self.assertEqual(code, 1)

    def test_cloud_placeholder_size_is_refused(self):
        part1, _part2 = self.make_pair()
        real_scan = tool.scan_disk_wavs

        def lying_stat(folder, threshold):
            """Simulate a Dropbox placeholder: stat says 0, bytes readable."""
            stats = real_scan(folder, threshold)
            if stats.sizes and folder.name.endswith("_Part_1_of_2"):
                stats.discrepancies.append((
                    sorted(stats.sizes)[0],
                    "stat reports 0 bytes but 12 byte(s) are readable",
                ))
            return stats

        before = snapshot(part1)
        with mock.patch.object(tool, "scan_disk_wavs", side_effect=lying_stat):
            code, rows = self.run_main("--perform-split")
        self.assertEqual(rows[0]["Verdict"], tool.UNTRUSTWORTHY_SIZES)
        self.assertEqual(snapshot(part1), before)
        self.assertEqual(code, 1)

    def test_a_wav_whose_size_cannot_be_read_is_refused_not_stranded(self):
        part1, _part2 = self.make_pair()
        real_scan = tool.scan_disk_wavs

        def drop_one(folder, threshold):
            """Simulate scan_disk_wavs omitting an unstattable file."""
            stats = real_scan(folder, threshold)
            if stats.sizes and folder.name.endswith("_Part_1_of_2"):
                stats.sizes.pop(sorted(stats.sizes)[0])
            return stats

        with mock.patch.object(tool, "scan_disk_wavs", side_effect=drop_one):
            code, rows = self.run_main("--perform-split")
        self.assertEqual(rows[0]["Verdict"], tool.UNTRUSTWORTHY_SIZES)
        self.assertEqual(code, 1)

    def test_truncated_wav_is_warned_about_but_does_not_block(self):
        """A truncated recording has a real, measurable size — the split math
        is still correct, so it is reported rather than refused."""
        part1, part2 = self.make_pair()
        write_truncated_wav(part1 / "20240408_124000.WAV", 9000, 4096)
        code, rows = self.run_main("--perform-split")
        self.assertEqual(rows[0]["Verdict"], tool.SPLIT_DONE)
        self.assertIn("truncated", rows[0]["Notes"])
        self.assertEqual(code, 1)  # reported, so the run still flags it

    def test_still_oversized_is_refused_unless_allowed(self):
        part1, part2 = self.make_pair()
        code, rows = self.run_main("--perform-split", "--limit-bytes", "100")
        self.assertEqual(rows[0]["Verdict"], tool.STILL_OVERSIZED)
        self.assertEqual(rows[0]["Over Limit"], "Part 1 and Part 2")
        self.assertEqual(code, 1)

        code, rows = self.run_main(
            "--perform-split", "--limit-bytes", "100", "--allow-still-oversized"
        )
        self.assertEqual(rows[0]["Verdict"], tool.SPLIT_DONE)
        self.assertEqual(code, 1)  # Over Limit still flags it

    def test_zero_byte_wav_is_reported(self):
        self.make_pair(wavs={**_FIVE, "20240408_125000.WAV": 0})
        _code, rows = self.run_main()
        self.assertEqual(rows[0]["Zero-Byte WAVs"], "1")

    def test_scanning_staging_area_is_a_usage_error(self):
        code, _rows = self.run_main()  # sanity: raw dir is fine
        with mock.patch.object(sys, "argv", [
            "split_oversized_raw_folders.py", str(self.staging),
            "--output", str(self.root / "report2.csv"),
        ]):
            with self.assertRaises(SystemExit) as ctx:
                tool.main()
        self.assertEqual(ctx.exception.code, 2)


# --- collectors spreadsheet ----------------------------------------------

class TestCollectorsCheckNeverEdits(_SplitTestCase):
    """The spreadsheet is read, reported on, and never written."""

    def test_missing_rows_are_reported_but_do_not_block(self):
        self.make_pair()
        self.write_collectors(["445"])  # the un-split row only
        code, rows = self.run_main("--perform-split")
        self.assertEqual(rows[0]["Verdict"], tool.SPLIT_DONE)
        self.assertIn("MISSING", rows[0]["Collectors CSV"])
        self.assertIn("445_Part_1_of_2", rows[0]["Collectors CSV"])
        self.assertEqual(code, 1)

    def test_spreadsheet_is_byte_identical_after_a_real_split(self):
        self.make_pair()
        before = azus_common.calculate_sha512(str(self.collectors))
        self.run_main("--perform-split")
        self.assertEqual(
            azus_common.calculate_sha512(str(self.collectors)), before,
            "the collectors spreadsheet must never be modified",
        )

    def test_stale_bare_row_is_reported(self):
        self.make_pair()
        self.write_collectors(["445", "445_Part_1_of_2", "445_Part_2_of_2"])
        _code, rows = self.run_main()
        self.assertIn("stale bare 445", rows[0]["Collectors CSV"])

    def test_both_rows_present_is_clean(self):
        self.make_pair()
        _code, rows = self.run_main()
        self.assertEqual(rows[0]["Collectors CSV"], "both rows present")

    def test_skip_flag_omits_the_check(self):
        self.make_pair()
        self.write_collectors(["445"])
        code, rows = self.run_main("--perform-split", "--skip-collectors-check")
        self.assertEqual(rows[0]["Collectors CSV"], "not checked")
        self.assertEqual(code, 0)


# --- discovery, filtering, exit codes ------------------------------------

class TestDiscoveryAndExitCodes(_SplitTestCase):
    """Pairs are found in ESID order and the 0/1/2 contract holds."""

    def test_part2_folders_are_not_treated_as_candidates(self):
        self.make_pair()
        pairs = tool.find_part_pairs(self.raw)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].base_esid, "445")
        self.assertTrue(pairs[0].part1.name.endswith("_Part_1_of_2"))

    def test_plain_esid_folders_are_ignored(self):
        (self.raw / "ESID#100").mkdir()
        self.make_pair()
        self.assertEqual(len(tool.find_part_pairs(self.raw)), 1)

    def test_of_3_pair_is_reported_not_split(self):
        part1 = self.raw / "ESID#500_Part_1_of_3"
        part1.mkdir()
        (self.raw / "ESID#500_Part_2_of_3").mkdir()
        for name, size in _FIVE.items():
            write_wav(part1 / name, size)
        code, rows = self.run_main("--perform-split")
        self.assertEqual(rows[0]["Verdict"], tool.NOT_A_2_PART_ESID)
        self.assertEqual(code, 1)

    def test_esid_filter_accepts_either_part_name(self):
        self.make_pair("445")
        self.make_pair("446")
        for token in ("445", "445_Part_1_of_2", "445_Part_2_of_2"):
            with self.subTest(token=token):
                _code, rows = self.run_main("--esid", token)
                self.assertEqual([r["ESID#"] for r in rows], ["445"])

    def test_esid_matching_no_pair_is_a_usage_error(self):
        self.make_pair()
        code, _rows = self.run_main("--esid", "999")
        self.assertEqual(code, 2)

    def test_no_pairs_found_is_clean(self):
        code, rows = self.run_main()
        self.assertEqual(rows, [])
        self.assertEqual(code, 0)

    def test_bad_limit_is_a_usage_error(self):
        self.make_pair()
        with mock.patch.object(sys, "argv", [
            "split_oversized_raw_folders.py", str(self.raw),
            "--limit-bytes", "0",
        ]):
            with self.assertRaises(SystemExit) as ctx:
                tool.main()
        self.assertEqual(ctx.exception.code, 2)

    def test_missing_raw_dir_is_a_usage_error(self):
        with mock.patch.object(sys, "argv", [
            "split_oversized_raw_folders.py", str(self.root / "nope"),
        ]):
            with self.assertRaises(SystemExit) as ctx:
                tool.main()
        self.assertEqual(ctx.exception.code, 2)

    def test_pre_split_staging_folder_is_flagged(self):
        self.make_pair()
        leftover = self.staging / "ESID_445_Staging"
        leftover.mkdir()
        (leftover / azus_common.STATE_FILENAME).write_text('{"record_id": "9"}')
        code, rows = self.run_main("--perform-split")
        self.assertIn("ESID_445_Staging", rows[0]["Pre-Split Record"])
        self.assertIn("bound to a Zenodo draft", rows[0]["Pre-Split Record"])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
