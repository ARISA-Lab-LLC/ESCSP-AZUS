"""Unit tests for prepare_dataset.py's per-day ZIP layout (the default).

One ``ESID_NNN_YYYY_MM_DD.zip`` per recording day (day = the LITERAL
8-digit prefix of the WAV filename), each holding that day's WAVs plus
a copy of CONFIG.TXT and NOTHING else; one ZIP row per archive in
``file_list.csv``; the dataset version marked with a trailing ``A``;
``--single-zip`` preserving the legacy layout untouched.

Run from the project root:

    python3 -m unittest discover -s tests -v
"""

import csv
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "Resources"))

import azus_common  # noqa: E402
import prepare_dataset as prep  # noqa: E402

_TOOL = _PROJECT_ROOT / "Resources" / "prepare_dataset.py"


def riff_header(declared_total: int) -> bytes:
    chunk = declared_total - 8
    return b"RIFF" + chunk.to_bytes(4, "little") + b"WAVE"


def write_wav(path: Path, total_size: int) -> None:
    """A well-formed WAV whose stat size and RIFF header agree."""
    path.write_bytes(riff_header(total_size) + b"\x00" * (total_size - 12))


# Three recording days, the middle one from an unset clock — the
# grouping must take the 1970 date literally rather than block.
_WAVS = {
    "20240408_120000.WAV": 4000,
    "20240408_130000.WAV": 6000,
    "20240409_090000.WAV": 5000,
    "19700101_000000.WAV": 3000,
}
_DAYS = ("1970_01_01", "2024_04_08", "2024_04_09")


class _Case(unittest.TestCase):
    """Fixture: one raw ESID folder spanning three literal days."""

    ESID = "005"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.source = self.root / "ESID_005"
        self.out = self.root / "staging"
        self.source.mkdir()
        self.out.mkdir()
        for name, size in _WAVS.items():
            write_wav(self.source / name, size)
        (self.source / "CONFIG.TXT").write_text("gain: medium\n")

    def wav_paths(self):
        return sorted(self.source.glob("*.WAV"))

    def build_zips(self):
        return prep.create_day_zip_files(self.source, self.out, self.ESID)


class TestGroupWavsByDay(_Case):
    def test_groups_by_literal_date_including_1970(self):
        by_day = prep.group_wavs_by_day(self.wav_paths())
        self.assertEqual(list(by_day), list(_DAYS))  # ascending
        self.assertEqual(
            [p.name for p in by_day["2024_04_08"]],
            ["20240408_120000.WAV", "20240408_130000.WAV"],
        )
        self.assertEqual(
            [p.name for p in by_day["1970_01_01"]], ["19700101_000000.WAV"]
        )

    def test_missing_prefix_is_fatal_and_names_the_offenders(self):
        write_wav(self.source / "Recording_1.WAV", 2000)
        write_wav(self.source / "noise.wav", 2000)
        with self.assertRaises(ValueError) as ctx:
            prep.group_wavs_by_day(self.wav_paths() +
                                   sorted(self.source.glob("*.wav")))
        message = str(ctx.exception)
        self.assertIn("REFUSING", message)
        self.assertIn("Recording_1.WAV", message)
        self.assertIn("noise.wav", message)
        self.assertIn("--single-zip", message)

    def test_empty_input_groups_to_nothing(self):
        self.assertEqual(prep.group_wavs_by_day([]), {})


class TestCreateDayZipFiles(_Case):
    def test_one_zip_per_day_named_correctly(self):
        zip_paths, per_zip, _hashes = self.build_zips()
        self.assertEqual(
            [p.name for p in zip_paths],
            [f"ESID_005_{day}.zip" for day in _DAYS],
        )
        self.assertEqual(
            per_zip["ESID_005_2024_04_08.zip"],
            ["20240408_120000.WAV", "20240408_130000.WAV"],
        )

    def test_config_txt_is_the_first_entry_of_every_zip(self):
        zip_paths, _per_zip, _hashes = self.build_zips()
        for zip_path in zip_paths:
            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
            self.assertTrue(
                names[0].endswith("/CONFIG.TXT"), f"{zip_path.name}: {names}"
            )

    def test_entries_live_under_the_zip_stem_subfolder(self):
        """Extracting several day ZIPs side by side must never collide."""
        zip_paths, _per_zip, _hashes = self.build_zips()
        for zip_path in zip_paths:
            with zipfile.ZipFile(zip_path) as zf:
                for name in zf.namelist():
                    self.assertTrue(
                        name.startswith(f"{zip_path.stem}/"),
                        f"{zip_path.name}: {name}",
                    )

    def test_day_zips_hold_only_that_days_wavs_plus_config(self):
        zip_paths, per_zip, _hashes = self.build_zips()
        for zip_path in zip_paths:
            with zipfile.ZipFile(zip_path) as zf:
                basenames = {Path(n).name for n in zf.namelist()}
            expected = set(per_zip[zip_path.name]) | {"CONFIG.TXT"}
            self.assertEqual(basenames, expected, zip_path.name)

    def test_content_hashes_cover_every_wav_and_config(self):
        import hashlib
        _zip_paths, _per_zip, hashes = self.build_zips()
        self.assertEqual(set(hashes), set(_WAVS) | {"CONFIG.TXT"})
        truth = hashlib.sha512(
            (self.source / "20240409_090000.WAV").read_bytes()
        ).hexdigest()
        self.assertEqual(hashes["20240409_090000.WAV"], truth)


class TestVersionSuffix(unittest.TestCase):
    def test_appends_the_marker(self):
        row = {"Version": "2024.1.0"}
        prep._apply_day_zip_version_suffix(row)
        self.assertEqual(row["Version"], "2024.1.0A")

    def test_idempotent(self):
        row = {"Version": "2024.1.0A"}
        prep._apply_day_zip_version_suffix(row)
        self.assertEqual(row["Version"], "2024.1.0A")

    def test_tolerant_key_match(self):
        row = {"version": "2024.1.0"}
        prep._apply_day_zip_version_suffix(row)
        self.assertEqual(row["version"], "2024.1.0A")

    def test_empty_and_absent_versions_only_warn(self):
        for row in ({"Version": ""}, {"Latitude": "35.0"}):
            before = dict(row)
            prep._apply_day_zip_version_suffix(row)
            self.assertEqual(row, before)


class TestZenodoFileCapGuard(_Case):
    def test_at_cap_passes(self):
        companions = (
            azus_common.ZENODO_MAX_FILES_PER_RECORD
            - 1
            - len(prep._ALWAYS_GENERATED_COMPANIONS)
            - len(prep.CONDITIONAL_FILES)
        )
        prep.enforce_zenodo_file_cap(1, companions, self.ESID)  # no exit

    def test_one_over_the_cap_refuses(self):
        companions = (
            azus_common.ZENODO_MAX_FILES_PER_RECORD
            - 1
            - len(prep._ALWAYS_GENERATED_COMPANIONS)
            - len(prep.CONDITIONAL_FILES)
        )
        with self.assertRaises(SystemExit) as ctx:
            prep.enforce_zenodo_file_cap(2, companions, self.ESID)
        self.assertEqual(ctx.exception.code, 1)

    def test_refusal_happens_before_any_zip_is_written(self):
        """An over-long deployment costs a scan, not hours of zipping."""
        # 120 distinct literal days — far past the ~85-day ceiling.
        for i in range(120):
            write_wav(self.source / f"2024{i:04d}_000000.WAV", 100)
        wav_files = sorted(self.source.glob("*.WAV"))
        by_day = prep.group_wavs_by_day(wav_files)
        self.assertGreater(len(by_day), 100)
        with self.assertRaises(SystemExit) as ctx:
            prep.enforce_zenodo_file_cap(len(by_day), 11, self.ESID)
        self.assertEqual(ctx.exception.code, 1)
        # The guard sits ahead of create_day_zip_files in the runner, so
        # a refusal leaves the output directory without a single archive.
        self.assertEqual(list(self.out.glob("*.zip")), [])

    def test_cap_message_cites_the_source(self):
        with self.assertRaises(SystemExit):
            with self.assertLogs("azus.prepare", level="ERROR") as logs:
                prep.enforce_zenodo_file_cap(200, 11, self.ESID)
        text = "\n".join(logs.output)
        self.assertIn("help.zenodo.org", text)
        self.assertIn("--single-zip", text)
        self.assertIn("split_oversized_raw_folders.py", text)


class TestDayZipFileList(_Case):
    def _rows(self):
        zip_paths, per_zip, hashes = self.build_zips()
        # Minimal standing-in internal rows: CONFIG + the WAVs.
        internal = [{
            "File Name": "CONFIG.TXT", "File Type": "Plain Text (.txt)",
            "Description": "x", "File size (KB)": "0.01",
            "File size (Bytes)": "14",
            "Associated Data Dictionary": "CONFIG_data_dict.csv",
            "SHA-512 Hash": hashes["CONFIG.TXT"], "Notes": "",
        }]
        for name in _WAVS:
            internal.append({
                "File Name": name, "File Type": "WAV", "Description": "x",
                "File size (KB)": "1", "File size (Bytes)": "1",
                "Associated Data Dictionary": "WAV_data_dict.csv",
                "SHA-512 Hash": hashes[name], "Notes": "",
            })
        prep.create_day_zip_file_list(
            self.out, self.ESID, zip_paths, per_zip, internal
        )
        with open(self.out / "file_list.csv", newline="",
                  encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def test_one_zip_row_per_day_first_in_day_order(self):
        rows = self._rows()
        self.assertEqual(
            [r["File Name"] for r in rows[:3]],
            [f"ESID_005_{day}.zip" for day in _DAYS],
        )
        for row in rows[:3]:
            self.assertEqual(row["File Type"], "ZIP Archive (.zip)")
            self.assertTrue(row["SHA-512 Hash"])
            self.assertIn("Extract to", row["Notes"])

    def test_zip_rows_hash_matches_the_archive(self):
        import hashlib
        rows = self._rows()
        for row in rows[:3]:
            truth = hashlib.sha512(
                (self.out / row["File Name"]).read_bytes()
            ).hexdigest()
            self.assertEqual(row["SHA-512 Hash"], truth)

    def test_wav_rows_name_their_archive(self):
        rows = {r["File Name"]: r for r in self._rows()}
        self.assertEqual(
            rows["20240409_090000.WAV"]["Notes"],
            "Archived in ESID_005_2024_04_09.zip",
        )
        self.assertEqual(
            rows["19700101_000000.WAV"]["Notes"],
            "Archived in ESID_005_1970_01_01.zip",
        )
        self.assertEqual(
            rows["CONFIG.TXT"]["Notes"],
            "A copy is included in every day ZIP",
        )

    def test_headers_are_unchanged(self):
        self._rows()
        with open(self.out / "file_list.csv", encoding="utf-8") as fh:
            header = fh.readline().strip()
        self.assertEqual(header.split(","), prep._FILE_LIST_HEADERS)


class TestVerifyDayZips(_Case):
    def _verify(self, zip_paths):
        return prep.verify_day_zips_against_source(
            zip_paths, self.source, self.ESID
        )

    def test_clean_round_trip_verifies(self):
        zip_paths, _per_zip, _hashes = self.build_zips()
        self.assertEqual(self._verify(zip_paths), [])

    def test_wav_added_after_zipping_fails(self):
        zip_paths, _per_zip, _hashes = self.build_zips()
        write_wav(self.source / "20240408_235959.WAV", 2000)
        problems = self._verify(zip_paths)
        self.assertTrue(
            any("20240408_235959.WAV" in p for p in problems), problems
        )

    def test_new_day_after_zipping_fails(self):
        zip_paths, _per_zip, _hashes = self.build_zips()
        write_wav(self.source / "20240410_000000.WAV", 2000)
        problems = self._verify(zip_paths)
        self.assertTrue(
            any("ESID_005_2024_04_10.zip" in p for p in problems), problems
        )

    def test_missing_day_zip_fails(self):
        zip_paths, _per_zip, _hashes = self.build_zips()
        problems = self._verify(zip_paths[:-1])  # drop 2024_04_09
        self.assertTrue(
            any("ESID_005_2024_04_09.zip" in p and "missing" in p
                for p in problems), problems,
        )

    def test_size_drift_fails(self):
        zip_paths, _per_zip, _hashes = self.build_zips()
        write_wav(self.source / "20240408_120000.WAV", 4444)  # was 4000
        problems = self._verify(zip_paths)
        self.assertTrue(
            any("20240408_120000.WAV" in p for p in problems), problems
        )

    def test_missing_config_in_a_zip_fails(self):
        zip_paths, _per_zip, _hashes = self.build_zips()
        # Rebuild one archive without CONFIG.TXT.
        victim = zip_paths[1]
        with zipfile.ZipFile(victim) as zf:
            keep = [n for n in zf.namelist()
                    if not n.endswith("CONFIG.TXT")]
            payload = {n: zf.read(n) for n in keep}
        victim.unlink()
        with zipfile.ZipFile(victim, "w") as zf:
            for name, data in payload.items():
                zf.writestr(name, data)
        problems = self._verify(zip_paths)
        self.assertTrue(
            any("CONFIG.TXT" in p and victim.name in p for p in problems),
            problems,
        )

    def test_zero_byte_wav_warns_but_verifies(self):
        (self.source / "20240409_235000.WAV").write_bytes(b"")
        zip_paths, _per_zip, _hashes = self.build_zips()
        self.assertEqual(self._verify(zip_paths), [])


class TestEndToEndModes(unittest.TestCase):
    """The real CLI, both layouts, in a hermetic fake project tree."""

    def _fake_project(self, root: Path) -> Path:
        import shutil
        fake_root = root / "fake_project"
        fake_resources = fake_root / "Resources"
        fake_resources.mkdir(parents=True)
        for module in ("prepare_dataset.py", "audit_wav_integrity.py",
                       "azus_common.py"):
            shutil.copy2(_PROJECT_ROOT / "Resources" / module,
                         fake_resources / module)
        for data_file in ("README_template.html", "resource_files_list.csv",
                          "License.txt"):
            shutil.copy2(_PROJECT_ROOT / "Resources" / data_file,
                         fake_resources / data_file)
        (fake_root / "Staging_Area").mkdir()
        return fake_root

    def _raw_and_collectors(self, root: Path):
        source = root / "Raw_Data" / "ESID_005"
        source.mkdir(parents=True)
        for name, size in _WAVS.items():
            write_wav(source / name, size)
        (source / "CONFIG.TXT").write_text("gain: medium\n")
        collectors = root / "collectors.csv"
        collectors.write_text(
            "ESID,Eclipse Date,Local Eclipse Type,Latitude,Longitude,Version\n"
            "005,2024-04-08,Total,35.08,-106.65,2024.1.0\n",
        )
        return source, collectors

    def _run(self, fake_root, source, collectors, *extra):
        return subprocess.run(
            [sys.executable,
             str(fake_root / "Resources" / "prepare_dataset.py"),
             str(source), "--collector-csv", str(collectors), *extra],
            capture_output=True, text=True, cwd=fake_root,
        )

    def _staged_version(self, staged: Path) -> str:
        with open(staged / "total_eclipse_data.csv", newline="",
                  encoding="utf-8") as fh:
            return list(csv.DictReader(fh))[0]["Version"]

    def test_per_day_default_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_root = self._fake_project(root)
            source, collectors = self._raw_and_collectors(root)
            result = self._run(fake_root, source, collectors)
            self.assertEqual(
                result.returncode, 0, result.stdout + result.stderr
            )
            staged = fake_root / "Staging_Area" / "ESID_005_Staging"
            self.assertTrue((staged / ".prep_complete").exists())

            # The three day ZIPs, and no legacy ZIP.
            for day in _DAYS:
                self.assertTrue(
                    (staged / f"ESID_005_{day}.zip").exists(), day
                )
            self.assertFalse((staged / "ESID_005.zip").exists())

            # No metadata inside any day ZIP.
            for day in _DAYS:
                with zipfile.ZipFile(staged / f"ESID_005_{day}.zip") as zf:
                    basenames = {Path(n).name for n in zf.namelist()}
                self.assertNotIn("README.md", basenames)
                self.assertNotIn("file_list.csv", basenames)
                self.assertIn("CONFIG.TXT", basenames)

            # file_list.csv: three ZIP rows first.
            with open(staged / "file_list.csv", newline="",
                      encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(
                [r["File Name"] for r in rows[:3]],
                [f"ESID_005_{day}.zip" for day in _DAYS],
            )

            # The manifest lists every day ZIP.
            manifest = (staged / "ESID_005_to_upload.csv").read_text()
            for day in _DAYS:
                self.assertIn(f"ESID_005_{day}.zip", manifest)

            # The version marker landed.
            self.assertEqual(self._staged_version(staged), "2024.1.0A")

    def test_single_zip_flag_preserves_the_legacy_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_root = self._fake_project(root)
            source, collectors = self._raw_and_collectors(root)
            result = self._run(fake_root, source, collectors, "--single-zip")
            self.assertEqual(
                result.returncode, 0, result.stdout + result.stderr
            )
            staged = fake_root / "Staging_Area" / "ESID_005_Staging"
            self.assertTrue((staged / "ESID_005.zip").exists())
            self.assertFalse(
                list(staged.glob("ESID_005_*_*_*.zip")),
                "no day ZIPs in single-zip mode",
            )
            # Metadata IS inside the legacy ZIP, as always.
            with zipfile.ZipFile(staged / "ESID_005.zip") as zf:
                basenames = {Path(n).name for n in zf.namelist()}
            self.assertIn("README.md", basenames)
            # And the version is NOT marked.
            self.assertEqual(self._staged_version(staged), "2024.1.0")


if __name__ == "__main__":
    unittest.main()
