"""Unit tests for audit_prep_completeness.py's per-day layout support.

The auditor certifies folders as complete against the prep contract, and
the contract now has two layouts.  The properties under test: layout
detection routes correctly (single / per-day / mixed / none); a complete
per-day folder audits Yes; every per-day defect — a missing day ZIP, a
WAV missing from its day's archive, an unexpected entry (per-day ZIPs
carry NO metadata), a mixed layout — audits No with a message naming it;
and single-zip folders are still audited by the untouched legacy rules.

Run from the project root:

    python3 -m unittest discover -s tests -v
"""

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "Resources"))

import audit_prep_completeness as m  # noqa: E402
import azus_common  # noqa: E402
import prepare_dataset as prep  # noqa: E402

_ESID = "005"
_WAVS = {
    "20240408_120000.WAV": b"A" * 400,
    "20240408_130000.WAV": b"B" * 600,
    "20240409_090000.WAV": b"C" * 500,
}
_DAYS = ("2024_04_08", "2024_04_09")
_COMPANIONS = ["License.txt", "WAV_data_dict.csv"]


class _Case(unittest.TestCase):
    """Fixture: a raw folder and a staging folder, built per test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.raw = self.root / f"ESID_{_ESID}"
        self.staging = self.root / f"ESID_{_ESID}_Staging"
        self.raw.mkdir()
        self.staging.mkdir()
        for name, data in _WAVS.items():
            (self.raw / name).write_bytes(data)
        (self.raw / "CONFIG.TXT").write_text("gain: medium\n")

    def make_day_zip(self, day, wav_names, *, with_config=True,
                     extra_entries=()):
        """Write one per-day archive shaped like the prep produces."""
        zip_name = azus_common.day_zip_name(_ESID, day)
        zip_path = self.staging / zip_name
        stem = zip_path.stem
        with zipfile.ZipFile(zip_path, "w") as zf:
            if with_config:
                zf.writestr(f"{stem}/CONFIG.TXT", "gain: medium\n")
            for name in wav_names:
                zf.write(self.raw / name, f"{stem}/{name}")
            for name in extra_entries:
                zf.writestr(f"{stem}/{name}", "x")
        return zip_path

    def make_complete_per_day_staging(self):
        """Every folder file + both day ZIPs, exactly as prep lays out."""
        self.make_day_zip(
            "2024_04_08", ["20240408_120000.WAV", "20240408_130000.WAV"]
        )
        self.make_day_zip("2024_04_09", ["20240409_090000.WAV"])
        for name in (
            [t.format(esid=_ESID) for t in prep.STAGING_OUTPUT_FILES_COMMON]
            + _COMPANIONS
            + list(prep.CONDITIONAL_FILES)
        ):
            (self.staging / name).write_text("x")

    def audit(self):
        return m.audit_one_esid(
            _ESID, self.raw, self.staging, list(_COMPANIONS),
            audit_all=True,
        )


class TestLayoutRouting(_Case):
    def test_no_zips_at_all_is_no(self):
        status, details = self.audit()
        self.assertEqual(status, "No")
        self.assertTrue(any("ZIP archive missing" in d for d in details))

    def test_mixed_layout_is_no(self):
        self.make_complete_per_day_staging()
        (self.staging / f"ESID_{_ESID}.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
        status, details = self.audit()
        self.assertEqual(status, "No")
        self.assertTrue(any("mixed layout" in d for d in details))

    def test_unloadable_companion_list_is_ambiguous_in_both_layouts(self):
        self.make_complete_per_day_staging()
        status, _ = m.audit_one_esid(
            _ESID, self.raw, self.staging, None, audit_all=True
        )
        self.assertEqual(status, "Ambiguous")

    def test_sentinel_fast_path_still_wins(self):
        (self.staging / azus_common.PREP_SENTINEL).write_text("")
        status, _ = m.audit_one_esid(
            _ESID, self.raw, self.staging, list(_COMPANIONS),
            audit_all=False,
        )
        self.assertEqual(status, "Yes")


class TestPerDayAudit(_Case):
    def test_complete_folder_is_yes(self):
        self.make_complete_per_day_staging()
        status, details = self.audit()
        self.assertEqual(status, "Yes", details)

    def test_missing_day_zip_is_no_and_named(self):
        self.make_complete_per_day_staging()
        (self.staging / azus_common.day_zip_name(_ESID, "2024_04_09")).unlink()
        status, details = self.audit()
        self.assertEqual(status, "No")
        self.assertTrue(
            any("ESID_005_2024_04_09.zip" in d for d in details), details
        )

    def test_wav_missing_from_its_day_zip_is_no(self):
        self.make_complete_per_day_staging()
        victim = self.staging / azus_common.day_zip_name(_ESID, "2024_04_08")
        victim.unlink()
        self.make_day_zip("2024_04_08", ["20240408_120000.WAV"])  # one short
        status, details = self.audit()
        self.assertEqual(status, "No")
        self.assertTrue(
            any("20240408_130000.WAV" in d and "missing" in d
                for d in details), details,
        )

    def test_metadata_inside_a_day_zip_is_no(self):
        """Per-day ZIPs carry no metadata — an entry there is a defect."""
        self.make_complete_per_day_staging()
        victim = self.staging / azus_common.day_zip_name(_ESID, "2024_04_09")
        victim.unlink()
        self.make_day_zip(
            "2024_04_09", ["20240409_090000.WAV"],
            extra_entries=("README.md",),
        )
        status, details = self.audit()
        self.assertEqual(status, "No")
        self.assertTrue(
            any("unexpected entry: README.md" in d for d in details), details
        )

    def test_day_zip_for_a_day_with_no_audio_is_no(self):
        self.make_complete_per_day_staging()
        self.make_day_zip("2024_05_01", [])
        status, details = self.audit()
        self.assertEqual(status, "No")
        self.assertTrue(
            any("no raw audio" in d and "2024_05_01" in d for d in details),
            details,
        )

    def test_config_missing_from_one_day_zip_is_no(self):
        self.make_complete_per_day_staging()
        victim = self.staging / azus_common.day_zip_name(_ESID, "2024_04_09")
        victim.unlink()
        self.make_day_zip(
            "2024_04_09", ["20240409_090000.WAV"], with_config=False
        )
        status, details = self.audit()
        self.assertEqual(status, "No")
        self.assertTrue(
            any("CONFIG.TXT" in d and "2024_04_09" in d for d in details),
            details,
        )

    def test_raw_wav_without_a_date_prefix_is_ambiguous(self):
        """The truth set is underivable — not provably incomplete."""
        self.make_complete_per_day_staging()
        (self.raw / "Recording_1.WAV").write_bytes(b"x" * 100)
        status, details = self.audit()
        self.assertEqual(status, "Ambiguous")
        self.assertTrue(
            any("cannot derive" in d for d in details), details
        )

    def test_missing_folder_companion_is_no(self):
        self.make_complete_per_day_staging()
        (self.staging / "README.md").unlink()
        status, details = self.audit()
        self.assertEqual(status, "No")
        self.assertTrue(
            any("folder missing: README.md" in d for d in details), details
        )

    def test_missing_conditional_file_is_ambiguous(self):
        self.make_complete_per_day_staging()
        for cond in prep.CONDITIONAL_FILES:
            (self.staging / cond).unlink()
        status, details = self.audit()
        self.assertEqual(status, "Ambiguous", details)

    def test_corrupt_day_zip_is_ambiguous(self):
        self.make_complete_per_day_staging()
        victim = self.staging / azus_common.day_zip_name(_ESID, "2024_04_09")
        victim.write_bytes(b"this is not a zip")
        status, details = self.audit()
        self.assertEqual(status, "Ambiguous")
        self.assertTrue(any("corrupt" in d for d in details), details)


class TestAuditAgreesWithPrepOnSidecars(_Case):
    """The auditor's expectation must match what prep actually archives.

    prep excludes AppleDouble sidecars from the per-day archives; if the
    auditor still expected them, every archive would read as incomplete
    and a correct folder would audit No.
    """

    def test_complete_folder_with_a_sidecar_still_audits_yes(self):
        self.make_complete_per_day_staging()
        (self.raw / "._20240408_120000.WAV").write_bytes(b"x" * 80)
        status, details = self.audit()
        self.assertEqual(status, "Yes", details)

    def test_a_dated_non_zip_does_not_flip_the_layout_verdict(self):
        """staging_zip_mode keys on parse_day_zip_name, which needs .zip."""
        (self.staging / f"ESID_{_ESID}_2024_04_08.log").write_text("noise")
        self.assertIsNone(
            prep.staging_zip_mode(self.staging, _ESID),
            "a dated .log is not a data archive",
        )
        status, details = self.audit()
        self.assertEqual(status, "No")
        self.assertTrue(any("ZIP archive missing" in d for d in details))


class TestSingleZipStillAuditedByLegacyRules(_Case):
    def _make_single_zip_staging(self):
        zip_path = self.staging / f"ESID_{_ESID}.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(f"ESID_{_ESID}/CONFIG.TXT", "gain: medium\n")
            for name in _WAVS:
                zf.write(self.raw / name, f"ESID_{_ESID}/{name}")
            for entry in (
                list(prep.ZIP_METADATA_ENTRIES)
                + _COMPANIONS
                + list(prep.CONDITIONAL_FILES)
            ):
                zf.writestr(f"ESID_{_ESID}/{entry}", "x")
        for name in (
            [t.format(esid=_ESID) for t in prep.STAGING_OUTPUT_FILES]
            + _COMPANIONS
            + list(prep.CONDITIONAL_FILES)
        ):
            target = self.staging / name
            if not target.exists():
                target.write_text("x")

    def test_complete_single_zip_folder_is_yes(self):
        self._make_single_zip_staging()
        status, details = self.audit()
        self.assertEqual(status, "Yes", details)

    def test_wav_missing_from_the_single_zip_is_no(self):
        self._make_single_zip_staging()
        zip_path = self.staging / f"ESID_{_ESID}.zip"
        (self.raw / "20240410_000000.WAV").write_bytes(b"D" * 300)
        status, details = self.audit()
        self.assertEqual(status, "No")
        self.assertTrue(
            any("20240410_000000.WAV" in d for d in details), details
        )


if __name__ == "__main__":
    unittest.main()
