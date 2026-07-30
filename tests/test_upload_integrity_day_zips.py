"""Unit tests for the upload integrity gate on the PER-DAY layout.

Companion to tests/test_upload_integrity.py, which is the permanent
``--single-zip`` regression pin.  This file covers what changes when prep
writes one archive per recording day: the gate must scope the
``file_list.csv`` cross-check to the archive that OWNS each WAV, and it
must check both directions —

  * a WAV missing from ITS day archive (not from "the ZIP"), and
  * a day the manifest describes but whose archive is absent.

The second direction is the one the old whole-folder comparison could not
see: it passed any folder where exactly one day happened to be present,
which is precisely what an interrupted per-day prep leaves behind.

Ownership is re-derived with ``azus_common.wav_day_key`` ->
``azus_common.day_zip_name``, the same rule prep grouped by.  The
``Notes`` column records the same mapping in prose, and nothing here
parses it.

Run from the project root:

    python3 -m unittest discover -s tests -v
"""

import csv
import hashlib
import sys
import tempfile
import unittest
import zipfile
from unittest import mock
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "Resources"))

import azus_common  # noqa: E402
import prepare_dataset as prep  # noqa: E402
import standalone_tasks as tasks  # noqa: E402

_ESID = "005"
_FILE_LIST_HEADERS = [
    "File Name", "File Type", "Description", "File size (KB)",
    "File size (Bytes)", "Associated Data Dictionary", "SHA-512 Hash",
    "Notes",
]

# Two full days plus a third, so "one day is present" is never accidentally
# the whole truth.
_DAY_WAVS = {
    "2024_04_08": {"20240408_120000.WAV": 4000, "20240408_130000.WAV": 6000},
    "2024_04_09": {"20240409_090000.WAV": 5000},
    "2024_04_10": {"20240410_080000.WAV": 3000},
}


def riff_header(declared_total: int) -> bytes:
    chunk = declared_total - 8
    return b"RIFF" + chunk.to_bytes(4, "little") + b"WAVE"


def wav_bytes(total_size: int) -> bytes:
    """A well-formed WAV payload whose length and RIFF header agree."""
    return riff_header(total_size) + b"\x00" * (total_size - 12)


def _sha512(path: Path) -> str:
    return hashlib.sha512(path.read_bytes()).hexdigest()


class _Case(unittest.TestCase):
    """Fixture: a per-day staging folder shaped exactly as prep lays it out."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.staging = self.root / f"ESID_{_ESID}_Staging"
        self.staging.mkdir()

    # --- builders -------------------------------------------------------

    def make_day_zip(self, day, wavs, *, with_config=True, extra=()):
        """Write one day archive: that day's WAVs + a copy of CONFIG.TXT."""
        zip_path = self.staging / azus_common.day_zip_name(_ESID, day)
        stem = zip_path.stem
        with zipfile.ZipFile(zip_path, "w") as zf:
            if with_config:
                zf.writestr(f"{stem}/CONFIG.TXT", "gain: medium\n")
            for name, size in wavs.items():
                zf.writestr(f"{stem}/{name}", wav_bytes(size))
            for name, payload in extra:
                zf.writestr(f"{stem}/{name}", payload)
        return zip_path

    def write_file_list(self, rows):
        with open(self.staging / "file_list.csv", "w", encoding="utf-8",
                  newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=_FILE_LIST_HEADERS)
            writer.writeheader()
            for row in rows:
                writer.writerow({h: row.get(h, "") for h in _FILE_LIST_HEADERS})

    def build(self, days=None, *, sentinel=True):
        """Build a clean per-day folder over ``days`` and return the archives.

        file_list.csv is written prep's way: one row per archive first, in
        ascending day order, then every WAV row.
        """
        days = days or list(_DAY_WAVS)
        archives, zip_rows, wav_rows = [], [], []
        for day in days:
            wavs = _DAY_WAVS[day]
            zip_path = self.make_day_zip(day, wavs)
            archives.append(zip_path)
            zip_rows.append({
                "File Name": zip_path.name,
                "File Type": "ZIP Archive (.zip)",
                "File size (KB)": f"{zip_path.stat().st_size / 1024:.2f}",
                "File size (Bytes)": str(zip_path.stat().st_size),
                "SHA-512 Hash": _sha512(zip_path),
            })
            for name, size in wavs.items():
                wav_rows.append({
                    "File Name": name,
                    "File Type": "WAV",
                    "File size (KB)": f"{size / 1024:.2f}",
                    "File size (Bytes)": str(size),
                    "SHA-512 Hash": "unused-wav-hash",
                    "Notes": f"Archived in {zip_path.name}",
                })
        self.write_file_list(zip_rows + wav_rows)
        if sentinel:
            (self.staging / azus_common.PREP_SENTINEL).touch()
        return archives

    # --- helpers --------------------------------------------------------

    def verify(self, **kw):
        return tasks.verify_dataset_integrity(str(self.staging), _ESID, **kw)

    def rewrite_zip(self, zip_path, drop=(), add=()):
        """Rebuild an archive without ``drop`` and with ``add`` entries."""
        with zipfile.ZipFile(zip_path) as zf:
            keep = [(i.filename, zf.read(i.filename)) for i in zf.infolist()
                    if Path(i.filename).name not in drop]
        with zipfile.ZipFile(zip_path, "w") as zf:
            for name, payload in keep:
                zf.writestr(name, payload)
            for name, payload in add:
                zf.writestr(f"{zip_path.stem}/{name}", payload)

    def tamper_hash(self, archive_name):
        with open(self.staging / "file_list.csv", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        for row in rows:
            if row["File Name"] == archive_name:
                row["SHA-512 Hash"] = "0" * 128
        self.write_file_list(rows)


class TestLayoutRouting(_Case):
    """The gate must agree with the prep contract on what layout this is."""

    def test_clean_per_day_folder_verifies(self):
        self.build()
        self.assertEqual(self.verify(), [])

    def test_no_archive_at_all_is_refused(self):
        (self.staging / azus_common.PREP_SENTINEL).touch()
        self.write_file_list([])
        problems = self.verify()
        self.assertTrue(any("No data archive" in p for p in problems))

    def test_mixed_layout_is_refused_without_reading_any_archive(self):
        self.build()
        # A legacy archive alongside the day archives: a state no prep
        # produces.  It must be refused before anything is opened, so a
        # mixed 43 GB folder never costs a full read.
        (self.staging / f"ESID_{_ESID}.zip").write_bytes(b"not even a zip")
        with mock.patch.object(
            azus_common, "calculate_digests",
            side_effect=AssertionError("must not hash a mixed folder"),
        ):
            problems = self.verify()
        self.assertTrue(any("mixed layout" in p for p in problems))

    def test_missing_sentinel_still_fails_in_per_day_mode(self):
        self.build(sentinel=False)
        problems = self.verify()
        self.assertTrue(any(azus_common.PREP_SENTINEL in p for p in problems))

    def test_single_day_folder_is_per_day_not_legacy(self):
        """N=1 must classify by NAME, not by archive count.

        A one-day site produces exactly one archive, so a count-based
        check would read it as the legacy layout.
        """
        archives = self.build(days=["2024_04_08"])
        self.assertEqual(self.verify(), [])
        _, mode, problems = tasks.resolve_dataset_archives(self.staging, _ESID)
        self.assertEqual(problems, [])
        self.assertEqual(mode, prep.ZIP_MODE_PER_DAY)
        self.assertEqual(len(archives), 1)


class TestPerDayGate(_Case):
    """One test per defect, each asserting the message names the offender."""

    def test_wav_missing_from_its_own_day_archive(self):
        archives = self.build()
        day8 = archives[0]
        self.rewrite_zip(day8, drop={"20240408_130000.WAV"})
        problems = self.verify(verify_zip_hash=False)
        self.assertTrue(any("MISSING from the ZIP" in p for p in problems))
        # The message must name the archive at fault and no other.
        offending = [p for p in problems if "MISSING from the ZIP" in p]
        self.assertEqual(len(offending), 1)
        self.assertIn(day8.name, offending[0])
        self.assertNotIn(archives[1].name, offending[0])

    def test_wav_in_the_wrong_day_archive_is_caught_both_ways(self):
        """A day-8 WAV filed into day 9 is invisible to a whole-folder
        comparison: the set of WAVs across the folder is unchanged."""
        archives = self.build()
        day8, day9 = archives[0], archives[1]
        moved = "20240408_130000.WAV"
        self.rewrite_zip(day8, drop={moved})
        self.rewrite_zip(day9, add=[(moved, wav_bytes(6000))])
        problems = self.verify(verify_zip_hash=False)
        self.assertTrue(any(
            "MISSING from the ZIP" in p and day8.name in p for p in problems))
        self.assertTrue(any(
            "not listed in file_list.csv" in p and day9.name in p
            for p in problems))

    def test_manifest_day_with_no_archive_on_disk(self):
        """The interrupted-prep case the old gate passed silently."""
        archives = self.build()
        for archive in archives[1:]:
            archive.unlink()
        problems = self.verify(verify_zip_hash=False)
        for archive in archives[1:]:
            self.assertTrue(
                any(archive.name in p and "not present" in p
                    for p in problems),
                f"{archive.name} not reported: {problems}",
            )

    def test_orphan_archive_absent_from_the_manifest(self):
        """A stale archive from an earlier failed prep must not ride along."""
        self.build(days=["2024_04_08", "2024_04_09"])
        orphan = self.make_day_zip("2024_04_10", _DAY_WAVS["2024_04_10"])
        problems = self.verify(verify_zip_hash=False)
        self.assertTrue(any(
            "no row for" in p and orphan.name in p for p in problems))

    def test_config_missing_from_one_day_archive_is_not_a_wav_problem(self):
        """CONFIG.TXT is not a WAV, so its absence must not be reported as
        a missing or unexpected WAV — the WAV checks stay clean."""
        self.build(days=["2024_04_08"])
        archives, _, _ = tasks.resolve_dataset_archives(self.staging, _ESID)
        self.rewrite_zip(archives[0], drop={"CONFIG.TXT"})
        problems = self.verify(verify_zip_hash=False)
        self.assertFalse(
            any("WAV" in p for p in problems),
            f"CONFIG.TXT absence leaked into the WAV checks: {problems}",
        )

    def test_one_corrupt_archive_names_itself_and_spares_the_others(self):
        archives = self.build()
        archives[1].write_bytes(b"this is not a zip archive")
        problems = self.verify()
        unreadable = [p for p in problems if "not a readable archive" in p]
        self.assertEqual(len(unreadable), 1)
        self.assertIn(archives[1].name, unreadable[0])

    def test_per_archive_hash_mismatch_names_the_archive(self):
        archives = self.build()
        self.tamper_hash(archives[1].name)
        problems = self.verify()
        mismatched = [p for p in problems if "SHA-512 does not match" in p]
        self.assertEqual(len(mismatched), 1)
        self.assertIn(archives[1].name, mismatched[0])

    def test_undatable_manifest_wav_is_reported(self):
        """A WAV with no 8-digit prefix belongs to no day archive.  Without
        this check the ownership filter would skip it silently, hiding a
        WAV that is in the manifest but in none of the archives."""
        self.build(days=["2024_04_08"])
        with open(self.staging / "file_list.csv", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        rows.append({
            "File Name": "5D8F3A2B.WAV",  # old firmware, no date prefix
            "File Type": "WAV",
            "File size (KB)": "1.00", "File size (Bytes)": "1024",
        })
        self.write_file_list(rows)
        problems = self.verify(verify_zip_hash=False)
        self.assertTrue(any(
            "no 8-digit date prefix" in p and "5D8F3A2B.WAV" in p
            for p in problems))


class TestPerDayDigests(_Case):
    """digests_out is the uploader's known_md5s — it must never be
    populated from a dataset that failed verification."""

    def test_digests_are_keyed_by_archive_and_complete(self):
        archives = self.build()
        digests = {}
        self.assertEqual(self.verify(digests_out=digests), [])
        self.assertEqual(sorted(digests), sorted(a.name for a in archives))
        for archive in archives:
            entry = digests[archive.name]
            self.assertEqual(entry["sha512"], _sha512(archive))
            self.assertEqual(
                entry["md5"],
                hashlib.md5(archive.read_bytes()).hexdigest(),
            )

    def test_one_bad_archive_withholds_every_digest(self):
        """Not just the bad archive's: the dataset failed, so none of its
        digests may reach the uploader."""
        archives = self.build()
        self.tamper_hash(archives[1].name)
        digests = {}
        problems = self.verify(digests_out=digests)
        self.assertTrue(problems)
        self.assertEqual(digests, {})

    def test_skipping_the_hash_step_yields_no_digests(self):
        self.build()
        digests = {}
        self.assertEqual(self.verify(verify_zip_hash=False,
                                     digests_out=digests), [])
        self.assertEqual(digests, {})

    def test_structural_checks_run_even_when_the_hash_is_skipped(self):
        """--skip-integrity-hash bypasses ONLY the hash step."""
        archives = self.build()
        self.rewrite_zip(archives[0], drop={"20240408_130000.WAV"})
        problems = self.verify(verify_zip_hash=False)
        self.assertTrue(any("MISSING from the ZIP" in p for p in problems))


class TestPerDaySizeDrift(_Case):
    """The byte-exact size column must work per archive, exactly as it
    does for the legacy layout (see test_upload_integrity.py)."""

    def test_sub_kb_truncation_caught_without_the_hash_step(self):
        archives = self.build(days=["2024_04_08"])
        # One byte short of what the manifest records: rounds to the same
        # 2-decimal KB, so only the byte-exact column catches it.
        self.rewrite_zip(
            archives[0], drop={"20240408_120000.WAV"},
            add=[("20240408_120000.WAV", wav_bytes(3999))],
        )
        problems = self.verify(verify_zip_hash=False)
        self.assertTrue(any("differ in size" in p for p in problems))
        self.assertTrue(any(archives[0].name in p for p in problems))


class TestResolveDatasetArchives(_Case):
    """The single layout seam every consumer goes through."""

    def test_archives_come_back_in_ascending_day_order(self):
        self.build()
        archives, mode, problems = tasks.resolve_dataset_archives(
            self.staging, _ESID)
        self.assertEqual(problems, [])
        self.assertEqual(mode, prep.ZIP_MODE_PER_DAY)
        self.assertEqual(
            [a.name for a in archives],
            [azus_common.day_zip_name(_ESID, d) for d in sorted(_DAY_WAVS)],
        )

    def test_it_agrees_with_the_prep_contract_on_what_should_exist(self):
        """Cross-module pin: the upload side's enumerator and prep's own
        expectation must name the same archives, or a folder could be
        grouped one way and uploaded another."""
        raw = self.root / f"ESID_{_ESID}"
        raw.mkdir()
        for wavs in _DAY_WAVS.values():
            for name, size in wavs.items():
                (raw / name).write_bytes(wav_bytes(size))
        self.build()
        archives, _, _ = tasks.resolve_dataset_archives(self.staging, _ESID)
        self.assertEqual(
            [a.name for a in archives],
            prep.expected_day_zip_names(_ESID, raw),
        )

    def test_non_archive_lookalikes_are_ignored(self):
        """Routing through parse_day_zip_name, not glob('ESID_*.zip'):
        a hand-made backup must never be treated as a data archive."""
        self.build()
        (self.staging / f"ESID_{_ESID}_2024_04_08.txt").write_text("log")
        (self.staging / f"ESID_{_ESID}_backup.zip").write_bytes(b"PK")
        archives, mode, problems = tasks.resolve_dataset_archives(
            self.staging, _ESID)
        self.assertEqual(problems, [])
        self.assertEqual(mode, prep.ZIP_MODE_PER_DAY)
        self.assertEqual(len(archives), len(_DAY_WAVS))
        self.assertNotIn(
            f"ESID_{_ESID}_backup.zip", [a.name for a in archives])

    def test_another_esids_day_archive_is_not_adopted(self):
        self.build()
        (self.staging / "ESID_073_2024_04_08.zip").write_bytes(b"PK")
        archives, _, problems = tasks.resolve_dataset_archives(
            self.staging, _ESID)
        self.assertEqual(problems, [])
        self.assertEqual(len(archives), len(_DAY_WAVS))


class TestGateRejectsAnArchivePath(_Case):
    """The gate's subject is the folder.  A caller passing an archive
    would otherwise get a confusing 'no sentinel' problem and look like a
    legitimately failing dataset."""

    def test_passing_an_archive_path_raises(self):
        archives = self.build()
        with self.assertRaises(ValueError) as ctx:
            tasks.verify_dataset_integrity(str(archives[0]), _ESID)
        self.assertIn("staging FOLDER", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
