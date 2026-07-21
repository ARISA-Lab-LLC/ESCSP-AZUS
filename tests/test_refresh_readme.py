"""Unit tests for Resources/refresh_readme.py.

The tool finds prepared ``Staging_Area/ESID_NNN_Staging`` folders whose
``README.md`` predates the current template (it lacks the template's
sentinel sentence) and refreshes them: it regenerates README.html +
README.md, swaps the README.md sealed inside the dataset ZIP, and
rebuilds ``file_list.csv`` so the modified archive still passes the
upload integrity gate.

These tests prove, on hand-built miniature datasets:

  * the staleness scan classifies current / stale / anomalous folders;
  * a refresh makes the README current BOTH standalone and inside the
    ZIP;
  * the refreshed dataset passes the REAL upload gate
    (``standalone_tasks.verify_dataset_integrity``) — i.e. the ZIP row
    and README.md row in file_list.csv were rebuilt correctly;
  * the WAV audio entries and their manifest rows are left byte-for-byte
    untouched;
  * the rewrite is idempotent (a second run is a no-op) and leaves no
    temp files; and
  * the safety guards fire (template/README lacking the sentinel, a
    missing collector row, a stale README with no ZIP).

Run from the project root::

    python3 -m unittest discover -s tests -v
"""

import csv
import hashlib
import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "Resources"))

import azus_common  # noqa: E402
import refresh_readme  # noqa: E402
import standalone_tasks  # noqa: E402
from prepare_dataset import _FILE_LIST_HEADERS  # noqa: E402

_TEMPLATE = _PROJECT_ROOT / "Resources" / "README_template.html"
_SENTINEL = refresh_readme._SENTINEL

# A README the CURRENT template would never produce (no sentinel).
_OLD_README = "# ESID {esid}\n\nOld README predating the template change.\n"


def _sha(data: bytes) -> str:
    return hashlib.sha512(data).hexdigest()


def _rows_csv(rows):
    """Serialize rows exactly as prepare_dataset writes file_list.csv."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_FILE_LIST_HEADERS)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _wav_row(name: str, data: bytes) -> dict:
    return {
        "File Name": name,
        "File Type": "Waveform Audio File Format (.WAV)",
        "Description": "Audio recording file.",
        "File size (KB)": f"{len(data) / 1024:.2f}",
        "File size (Bytes)": str(len(data)),
        "Associated Data Dictionary": "WAV_data_dict.csv",
        "SHA-512 Hash": _sha(data),
        "Notes": "",
    }


def _readme_row(data: bytes) -> dict:
    return {
        "File Name": "README.md",
        "File Type": "Markdown (.md)",
        "Description": refresh_readme._README_DESCRIPTION,
        "File size (KB)": f"{len(data) / 1024:.2f}",
        "File size (Bytes)": str(len(data)),
        "Associated Data Dictionary": "N/A",
        "SHA-512 Hash": _sha(data),
        "Notes": "",
    }


def _zip_row(esid: str, data: bytes) -> dict:
    return {
        "File Name": f"ESID_{esid}.zip",
        "File Type": "ZIP Archive (.zip)",
        "Description": "Compressed archive.",
        "File size (KB)": f"{len(data) / 1024:.2f}",
        "File size (Bytes)": str(len(data)),
        "Associated Data Dictionary": "N/A",
        "SHA-512 Hash": _sha(data),
        "Notes": f"Extract to ESID_{esid}/ subfolder",
    }


def make_collector_csv(path: Path, esids) -> None:
    """Write a minimal collector CSV with one row per ESID."""
    headers = [
        "ESID", "Eclipse Date", "Latitude", "Longitude",
        "Eclipse Percent (%)", "Local Eclipse Type",
        "WAV Files Time & Date Settings",
    ]
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=headers)
        writer.writeheader()
        for esid in esids:
            writer.writerow({
                "ESID": esid,
                "Eclipse Date": "2024-04-08",
                "Latitude": "44.0",
                "Longitude": "-72.0",
                "Eclipse Percent (%)": "100",
                "Local Eclipse Type": "Total",
                "WAV Files Time & Date Settings": "UTC",
            })


class _DatasetTestCase(unittest.TestCase):
    """Fixture: a tmp Staging_Area plus a helper to build miniature
    prepared datasets whose starting state passes the integrity gate."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.staging = self.root / "Staging_Area"
        self.staging.mkdir()
        self.collector_csv = self.root / "collectors.csv"

    def build_dataset(self, esid, *, current=False, with_zip=True,
                      wavs=None, readme_row_in_file_list=True,
                      readme_entry_in_zip=True, filelist_entry_in_zip=True):
        """Create Staging_Area/ESID_<esid>_Staging with a valid ZIP +
        file_list.csv.

        ``current=True`` seeds a README that already carries the sentinel;
        ``with_zip=False`` omits the ZIP.  The remaining flags reproduce
        legacy dataset shapes that exercise the tool's less-travelled
        branches: ``readme_row_in_file_list=False`` builds a file_list.csv
        (external AND the in-ZIP copy derived from it) with NO README.md
        row, forcing the canonical-insert branch; ``readme_entry_in_zip``
        / ``filelist_entry_in_zip`` omit those entries from the archive,
        forcing the rewrite's add-when-absent fallback.  All variants keep
        the ZIP row + WAV rows, so the starting state still passes the
        integrity gate.
        """
        if wavs is None:
            wavs = {
                "20240408_120000.WAV": b"AAAA" * 64,
                "20240408_130000.WAV": b"BBBBBB" * 40,
            }
        folder = self.staging / f"ESID_{esid}_Staging"
        folder.mkdir()
        (folder / azus_common.PREP_SENTINEL).write_text("")

        if current:
            readme_text = (
                f"# ESID {esid}\n\n{_SENTINEL}\n"
            ).encode("utf-8")
        else:
            readme_text = _OLD_README.format(esid=esid).encode("utf-8")

        internal_rows = [_readme_row(readme_text)] if readme_row_in_file_list \
            else []
        internal_rows += [_wav_row(n, d) for n, d in wavs.items()]

        (folder / "README.md").write_bytes(readme_text)
        (folder / "README.html").write_text("<html>old</html>")

        if not with_zip:
            # Anomalous: stale README, no ZIP.  Still write a file_list so
            # the folder is otherwise plausible.
            _write_file_list(folder / "file_list.csv", internal_rows)
            return folder, wavs, readme_text

        internal_bytes = _rows_csv(internal_rows)
        zip_entries = dict(wavs)
        if readme_entry_in_zip:
            zip_entries["README.md"] = readme_text
        if filelist_entry_in_zip:
            zip_entries["file_list.csv"] = internal_bytes
        zip_path = folder / f"ESID_{esid}.zip"
        _write_zip(zip_path, esid, zip_entries)

        zip_bytes = zip_path.read_bytes()
        external_rows = [_zip_row(esid, zip_bytes)] + internal_rows
        _write_file_list(folder / "file_list.csv", external_rows)
        return folder, wavs, readme_text


def _write_zip(zip_path: Path, esid: str, entries: dict) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(f"ESID_{esid}/{name}", data)


def _write_file_list(path: Path, rows) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_FILE_LIST_HEADERS)
        writer.writeheader()
        writer.writerows(rows)


def _zip_member(zip_path: Path, basename: str) -> bytes:
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            if info.filename.rsplit("/", 1)[-1] == basename:
                return zf.read(info.filename)
    raise KeyError(basename)


def _external_row(folder: Path, file_name: str) -> dict:
    with open(folder / "file_list.csv", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row["File Name"] == file_name:
                return row
    raise KeyError(file_name)


def _internal_rows(zip_path: Path) -> list:
    """Parse the file_list.csv sealed INSIDE the ZIP into row dicts."""
    text = _zip_member(zip_path, "file_list.csv").decode("utf-8")
    return list(csv.DictReader(io.StringIO(text)))


# ===================================================================
#  Starting-state sanity: the fixtures themselves pass the gate
# ===================================================================

class TestFixtureIsValid(_DatasetTestCase):
    def test_built_dataset_passes_integrity_gate(self):
        folder, _, _ = self.build_dataset("007")
        problems = standalone_tasks.verify_dataset_integrity(
            str(folder / "ESID_007.zip")
        )
        self.assertEqual(problems, [])


# ===================================================================
#  Staleness scan
# ===================================================================

class TestScanStaging(_DatasetTestCase):
    def test_classifies_current_stale_and_anomalies(self):
        self.build_dataset("001")                       # stale
        self.build_dataset("002", current=True)         # current
        self.build_dataset("003", with_zip=False)       # stale, no ZIP
        # no-README anomaly
        bare = self.staging / "ESID_004_Staging"
        bare.mkdir()

        stale, skipped = refresh_readme.scan_staging(self.staging)
        self.assertEqual([e for e, _ in stale], ["001"])

        reasons = {e: r for e, _, r in skipped}
        self.assertEqual(reasons["002"], "already current")
        self.assertIn("no ZIP", reasons["003"])
        self.assertIn("no README.md", reasons["004"])


# ===================================================================
#  Refresh behaviour
# ===================================================================

class TestRefreshFolder(_DatasetTestCase):
    def _refresh(self, esid):
        make_collector_csv(self.collector_csv, [esid])
        folder = self.staging / f"ESID_{esid}_Staging"
        refresh_readme.refresh_folder(
            esid, folder, self.collector_csv, _TEMPLATE
        )
        return folder

    def test_readme_current_standalone_and_in_zip(self):
        self.build_dataset("007")
        folder = self._refresh("007")
        self.assertIn(
            _SENTINEL, (folder / "README.md").read_text(encoding="utf-8")
        )
        self.assertIn(
            _SENTINEL,
            _zip_member(folder / "ESID_007.zip", "README.md").decode("utf-8"),
        )
        # README.html regenerated too (was the old placeholder before).
        self.assertIn(
            _SENTINEL, (folder / "README.html").read_text(encoding="utf-8")
        )

    def test_refreshed_dataset_passes_integrity_gate(self):
        self.build_dataset("007")
        folder = self._refresh("007")
        problems = standalone_tasks.verify_dataset_integrity(
            str(folder / "ESID_007.zip")
        )
        self.assertEqual(problems, [])

    def test_wav_entries_and_rows_untouched(self):
        folder, wavs, _ = self.build_dataset("007")
        before = {n: _zip_member(folder / "ESID_007.zip", n) for n in wavs}
        before_rows = {n: _external_row(folder, n) for n in wavs}
        self._refresh("007")
        internal_after = {
            r["File Name"]: r
            for r in _internal_rows(folder / "ESID_007.zip")
        }
        for name, data in wavs.items():
            self.assertEqual(
                _zip_member(folder / "ESID_007.zip", name), data,
                f"WAV {name} bytes changed",
            )
            self.assertEqual(before[name], data)
            self.assertEqual(_external_row(folder, name), before_rows[name])
            # The in-ZIP manifest's WAV rows (hash + both sizes) must be
            # carried over verbatim too, not just the external copy.
            self.assertEqual(internal_after[name], before_rows[name])

    def test_file_list_zip_and_readme_rows_rebuilt(self):
        folder = None
        _ = self.build_dataset("007")
        folder = self._refresh("007")
        zip_path = folder / "ESID_007.zip"

        new_md = (folder / "README.md").read_bytes()
        readme_row = _external_row(folder, "README.md")
        self.assertEqual(readme_row["SHA-512 Hash"], _sha(new_md))
        self.assertEqual(readme_row["File size (Bytes)"], str(len(new_md)))
        self.assertEqual(
            readme_row["File size (KB)"], f"{len(new_md) / 1024:.2f}"
        )

        zip_row = _external_row(folder, "ESID_007.zip")
        self.assertEqual(
            zip_row["SHA-512 Hash"], azus_common.calculate_sha512(str(zip_path))
        )
        self.assertEqual(
            zip_row["File size (Bytes)"], str(zip_path.stat().st_size)
        )
        self.assertEqual(
            zip_row["File size (KB)"], f"{zip_path.stat().st_size / 1024:.2f}"
        )

    def test_internal_file_list_readme_row_matches(self):
        self.build_dataset("007")
        folder = self._refresh("007")
        new_md = _zip_member(folder / "ESID_007.zip", "README.md")
        rows = _internal_rows(folder / "ESID_007.zip")
        readme_rows = [r for r in rows if r["File Name"] == "README.md"]
        self.assertEqual(len(readme_rows), 1)
        self.assertEqual(readme_rows[0]["SHA-512 Hash"], _sha(new_md))
        self.assertEqual(readme_rows[0]["File size (Bytes)"], str(len(new_md)))
        self.assertEqual(
            readme_rows[0]["File size (KB)"], f"{len(new_md) / 1024:.2f}"
        )
        # Internal manifest must NOT list the ZIP (circular).
        self.assertFalse(
            any(r["File Name"] == "ESID_007.zip" for r in rows)
        )

    def test_second_run_is_noop(self):
        self.build_dataset("007")
        self._refresh("007")
        # Now current — scan no longer selects it.
        stale, skipped = refresh_readme.scan_staging(self.staging)
        self.assertEqual(stale, [])
        self.assertIn(
            "already current", {e: r for e, _, r in skipped}["007"]
        )

    def test_no_temp_files_left(self):
        self.build_dataset("007")
        folder = self._refresh("007")
        leftovers = [p.name for p in folder.iterdir()
                     if p.name.startswith(".") and p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])
        self.assertFalse(
            (folder / ".ESID_007.zip.refresh.tmp").exists()
        )

    def test_insert_branch_builds_readme_rows_both_manifests(self):
        """Legacy folder whose file_list.csv has NO README.md row: the
        canonical-insert branch must build a correct README row (hash +
        both sizes) in BOTH the external and in-ZIP manifests."""
        self.build_dataset("007", readme_row_in_file_list=False)
        folder = self._refresh("007")
        new_md = (folder / "README.md").read_bytes()
        expect_kb = f"{len(new_md) / 1024:.2f}"

        # External: row inserted right after the ZIP row (insert_at=1).
        with open(folder / "file_list.csv", encoding="utf-8") as fh:
            ext_rows = list(csv.DictReader(fh))
        self.assertEqual(ext_rows[0]["File Name"], "ESID_007.zip")
        self.assertEqual(ext_rows[1]["File Name"], "README.md")
        ext = ext_rows[1]
        self.assertEqual(ext["SHA-512 Hash"], _sha(new_md))
        self.assertEqual(ext["File size (Bytes)"], str(len(new_md)))
        self.assertEqual(ext["File size (KB)"], expect_kb)
        self.assertEqual(ext["File Type"], "Markdown (.md)")
        self.assertEqual(ext["Description"], refresh_readme._README_DESCRIPTION)

        # Internal (in-ZIP): row inserted at the front (insert_at=0).
        in_md = _zip_member(folder / "ESID_007.zip", "README.md")
        int_rows = _internal_rows(folder / "ESID_007.zip")
        self.assertEqual(int_rows[0]["File Name"], "README.md")
        self.assertEqual(int_rows[0]["SHA-512 Hash"], _sha(in_md))
        self.assertEqual(int_rows[0]["File size (Bytes)"], str(len(in_md)))
        self.assertEqual(int_rows[0]["File size (KB)"], expect_kb)

        # And the whole thing is still uploadable.
        self.assertEqual(
            standalone_tasks.verify_dataset_integrity(
                str(folder / "ESID_007.zip")
            ),
            [],
        )

    def test_zip_missing_readme_and_filelist_entries_are_added(self):
        """A ZIP that lacks README.md / file_list.csv entries: the rewrite
        must ADD them (the add-when-absent fallback) and stay uploadable."""
        self.build_dataset(
            "007", readme_entry_in_zip=False, filelist_entry_in_zip=False
        )
        folder = self._refresh("007")
        self.assertIn(
            _SENTINEL,
            _zip_member(folder / "ESID_007.zip", "README.md").decode("utf-8"),
        )
        # file_list.csv entry now present (no KeyError) and lists the README.
        int_rows = _internal_rows(folder / "ESID_007.zip")
        self.assertTrue(
            any(r["File Name"] == "README.md" for r in int_rows)
        )
        self.assertEqual(
            standalone_tasks.verify_dataset_integrity(
                str(folder / "ESID_007.zip")
            ),
            [],
        )


# ===================================================================
#  Safety guards
# ===================================================================

class TestGuards(_DatasetTestCase):
    def test_regenerated_readme_without_sentinel_raises_and_keeps_zip(self):
        folder, _, _ = self.build_dataset("007")
        make_collector_csv(self.collector_csv, ["007"])
        zip_before = (folder / "ESID_007.zip").read_bytes()
        # A template lacking the sentinel must abort BEFORE touching the ZIP.
        bad_template = self.root / "bad_template.html"
        bad_template.write_text("<html>$esid no sentinel here</html>")
        with self.assertRaises(RuntimeError):
            refresh_readme.refresh_folder(
                "007", folder, self.collector_csv, bad_template
            )
        self.assertEqual((folder / "ESID_007.zip").read_bytes(), zip_before)
        # Standalone README.md untouched (still stale).
        self.assertNotIn(
            _SENTINEL, (folder / "README.md").read_text(encoding="utf-8")
        )

    def test_missing_collector_row_raises(self):
        folder, _, _ = self.build_dataset("007")
        make_collector_csv(self.collector_csv, ["999"])  # no 007 row
        with self.assertRaises(RuntimeError):
            refresh_readme.refresh_folder(
                "007", folder, self.collector_csv, _TEMPLATE
            )

    def test_missing_zip_row_raises(self):
        folder, _, _ = self.build_dataset("007")
        make_collector_csv(self.collector_csv, ["007"])
        # Strip the ZIP row from file_list.csv.
        with open(folder / "file_list.csv", encoding="utf-8") as fh:
            rows = [r for r in csv.DictReader(fh)
                    if r["File Name"] != "ESID_007.zip"]
        _write_file_list(folder / "file_list.csv", rows)
        with self.assertRaises(RuntimeError):
            refresh_readme.refresh_folder(
                "007", folder, self.collector_csv, _TEMPLATE
            )


# ===================================================================
#  CLI
# ===================================================================

class TestMain(_DatasetTestCase):
    def _run(self, argv):
        with mock.patch.object(sys, "argv",
                               ["refresh_readme.py", str(self.staging), *argv]):
            with self.assertRaises(SystemExit) as ctx:
                refresh_readme.main()
        return ctx.exception.code

    def test_list_only_changes_nothing(self):
        folder, _, _ = self.build_dataset("007")
        zip_before = (folder / "ESID_007.zip").read_bytes()
        make_collector_csv(self.collector_csv, ["007"])
        code = self._run(["--collector-csv", str(self.collector_csv),
                          "--list-only"])
        self.assertEqual(code, 0)
        self.assertEqual((folder / "ESID_007.zip").read_bytes(), zip_before)

    def test_end_to_end_refresh(self):
        self.build_dataset("007")
        self.build_dataset("008", current=True)
        make_collector_csv(self.collector_csv, ["007", "008"])
        code = self._run(["--collector-csv", str(self.collector_csv)])
        self.assertEqual(code, 0)
        problems = standalone_tasks.verify_dataset_integrity(
            str(self.staging / "ESID_007_Staging" / "ESID_007.zip")
        )
        self.assertEqual(problems, [])

    def test_template_without_sentinel_exits_2(self):
        self.build_dataset("007")
        bad_template = self.root / "bad_template.html"
        bad_template.write_text("<html>no sentinel</html>")
        code = self._run(["--collector-csv", str(self.collector_csv),
                          "--readme-template", str(bad_template)])
        self.assertEqual(code, 2)

    def test_missing_staging_dir_exits_2(self):
        with mock.patch.object(sys, "argv",
                               ["refresh_readme.py",
                                str(self.root / "nope")]):
            with self.assertRaises(SystemExit) as ctx:
                refresh_readme.main()
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
