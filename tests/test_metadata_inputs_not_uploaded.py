"""Metadata inputs live in the staging folder but are never uploaded.

Three files in a prepared folder are INPUTS used to build the Zenodo
record's metadata rather than payload files of the record:

  * ``README.html``             -> the record's description field
  * ``related_identifiers.csv`` -> the record's related identifiers
  * ``references.csv``          -> the record's references

``README.html`` was always excluded for that reason.  The two CSVs were
not, and `related_identifiers.csv` was reaching Zenodo: prep's
``create_upload_manifest`` is a directory SCAN, so anything sitting in the
folder was listed, and the upload side faithfully uploaded it.  The record
therefore shipped a file that the record's own ``file_list.csv`` did not
document — `file_list.csv` never listed it, so it was undocumented payload.

The exclusion is applied on BOTH sides from one shared definition,
``azus_common.METADATA_INPUT_FILES``.  The second layer is not redundant:
the manifest is written at prep time, so folders prepped before this rule
existed still list these files, and re-prepping a multi-GB site to drop a
2 KB input file would be absurd.  The back-compat case below is the one
that protects those folders.

Excluding them cannot affect the metadata they produce: the upload side
reads both CSVs straight from the staging folder, not via the manifest
(``standalone_tasks`` per-record citation override, and
``new_version_upload``).

Run from the project root:

    python3 -m unittest discover -s tests -v
"""

import csv
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
import prepare_dataset as prep  # noqa: E402
import standalone_tasks as tasks  # noqa: E402
from models.audiomoth import DataCollector  # noqa: E402

_ESID = "777"


def make_collector(esid: str = _ESID) -> DataCollector:
    """Minimal valid DataCollector (same shape as the other test modules)."""
    return DataCollector.model_validate({
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


class TestTheSharedContract(unittest.TestCase):

    def test_all_three_metadata_inputs_are_named(self):
        self.assertEqual(
            set(azus_common.METADATA_INPUT_FILES),
            {"README.html", "related_identifiers.csv", "references.csv"},
        )

    def test_readme_md_is_NOT_a_metadata_input(self):
        """README.md is genuine payload — it is uploaded, via the dedicated
        UploadData.readme_md field.  Over-excluding it would silently drop a
        file the record is supposed to carry."""
        self.assertNotIn("README.md", azus_common.METADATA_INPUT_FILES)

    def test_prep_excludes_every_metadata_input_from_the_manifest(self):
        for name in azus_common.METADATA_INPUT_FILES:
            self.assertIn(name, prep._MANIFEST_EXCLUDES, name)


class TestPrepManifestOmitsThem(unittest.TestCase):
    """The root fix: the manifest is a directory scan, so it must filter."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name) / f"ESID_{_ESID}_Staging"
        self.folder.mkdir()

    def test_metadata_inputs_on_disk_are_not_listed(self):
        # A realistic prepared folder: payload, companions, and the inputs.
        for name in ("ESID_777_2024_04_08.zip", "README.md", "file_list.csv",
                     "total_eclipse_data.csv", "License.txt",
                     "README.html", "related_identifiers.csv",
                     "references.csv"):
            (self.folder / name).write_text("x", encoding="utf-8")

        manifest = prep.create_upload_manifest(self.folder, _ESID)
        with open(manifest, newline="", encoding="utf-8") as fh:
            listed = {row["File Name"] for row in csv.DictReader(fh)}

        for name in azus_common.METADATA_INPUT_FILES:
            self.assertNotIn(name, listed, f"{name} must not be uploaded")
        # ...and the real payload is still there.
        self.assertIn("ESID_777_2024_04_08.zip", listed)
        self.assertIn("README.md", listed)
        self.assertIn("file_list.csv", listed)
        self.assertIn("License.txt", listed)

    def test_the_inputs_are_left_on_disk(self):
        """They must stay in the folder — the upload step reads them from
        there to build the record's metadata."""
        for name in ("README.html", "related_identifiers.csv",
                     "references.csv", "README.md"):
            (self.folder / name).write_text("x", encoding="utf-8")
        prep.create_upload_manifest(self.folder, _ESID)
        for name in azus_common.METADATA_INPUT_FILES:
            self.assertTrue((self.folder / name).is_file(), name)


class TestUploadExcludesThemToo(unittest.TestCase):
    """The back-compat layer: a folder prepped BEFORE the fix still has the
    old manifest listing related_identifiers.csv.  Those folders must not
    need re-prepping."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.folder = self.root / f"ESID_{_ESID}_Staging"
        self.folder.mkdir()
        self.archive = self.folder / f"ESID_{_ESID}_2024_04_08.zip"
        with zipfile.ZipFile(self.archive, "w") as zf:
            zf.writestr(f"{self.archive.stem}/20240408_120000.WAV", b"\0" * 32)
        for name in ("README.html", "README.md", "License.txt",
                     "related_identifiers.csv", "references.csv",
                     "total_eclipse_data.csv"):
            (self.folder / name).write_text("x", encoding="utf-8")
        # A STALE manifest, exactly as prep used to write it.
        with open(self.folder / f"ESID_{_ESID}_to_upload.csv", "w",
                  newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["File Name", "Description"])
            for name in (self.archive.name, "README.md", "License.txt",
                         "related_identifiers.csv", "references.csv",
                         "total_eclipse_data.csv"):
                w.writerow([name, "x"])

    def _build(self):
        data, unmatched = tasks.create_upload_data(
            esid_folder_archives=[
                (_ESID, str(self.folder), [str(self.archive)])
            ],
            data_collectors=[make_collector()],
            project_config={"default_required_files": []},
        )
        self.assertEqual(unmatched, [])
        return data[0]

    def test_a_stale_manifest_still_does_not_upload_them(self):
        item = self._build()
        names = {Path(f).name for f in item.all_files}
        for excluded in azus_common.METADATA_INPUT_FILES:
            self.assertNotIn(
                excluded, names,
                f"{excluded} reached the upload set from a stale manifest",
            )

    def test_the_real_payload_is_untouched(self):
        item = self._build()
        names = [Path(f).name for f in item.all_files]
        self.assertIn(self.archive.name, names)
        self.assertIn("README.md", names)
        self.assertIn("License.txt", names)
        self.assertIn("total_eclipse_data.csv", names)
        # README.md exactly once — it comes via readme_md, not additional_files.
        self.assertEqual(names.count("README.md"), 1)

    def test_the_metadata_path_still_finds_the_csvs(self):
        """Excluding them from the upload must not stop them being READ.
        upload_dataset resolves both from the staging folder directly."""
        item = self._build()
        with mock.patch.object(tasks, "upload_to_zenodo") as up, \
             mock.patch.object(tasks, "get_draft_config") as cfg, \
             mock.patch.object(tasks, "save_metadata_json"):
            up.return_value = {"successful": True, "api_response": {},
                               "error": None}
            cfg.return_value = mock.MagicMock()
            tasks.upload_dataset(
                data=item,
                project_config={"title_template": "ESID#$esid",
                                "minimum_recording_year": 2000},
            )
        kwargs = cfg.call_args.kwargs
        self.assertEqual(
            kwargs["related_identifiers_csv"],
            str(self.folder / "related_identifiers.csv"),
            "the per-record related_identifiers.csv must still be read",
        )
        self.assertEqual(
            kwargs["references_csv"], str(self.folder / "references.csv"),
            "the per-record references.csv must still be read",
        )


if __name__ == "__main__":
    unittest.main()
