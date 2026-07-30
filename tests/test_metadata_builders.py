"""Unit tests for the metadata builders in standalone_tasks.py.

Covers the config-driven Zenodo metadata pipeline: load_project_config,
build_creators / build_contributors / build_fundings, get_draft_config,
get_recording_dates, read_upload_manifest, and create_upload_data.

Hermetic: everything runs inside tempfile.TemporaryDirectory.  The
project config fixture is templates/project_config.json.example copied
into the temp dir, so no test depends on the real
Resources/project_config.json, Staging_Area/, or any network call.

Run from the project root:

    python3 -m unittest tests.test_metadata_builders -v
"""

import csv
import json
import logging
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "Resources"))

import standalone_tasks as tasks  # noqa: E402
from models.audiomoth import DataCollector, UploadData  # noqa: E402

_EXAMPLE_CONFIG = _PROJECT_ROOT / "templates" / "project_config.json.example"


def setUpModule():
    # The tests deliberately exercise warning/error paths (unmatched
    # ESIDs, missing manifest files); keep the test output clean.
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


# --- fixtures ---------------------------------------------------------------

def copy_example_config(dst_dir: Path) -> Path:
    """Copy the shipped config template into a temp dir and return its path."""
    dst = dst_dir / "project_config.json"
    shutil.copy(_EXAMPLE_CONFIG, dst)
    return dst


def load_example_config(dst_dir: Path) -> dict:
    """Copy + load the example template through the real loader."""
    return tasks.load_project_config(str(copy_example_config(dst_dir)))


def make_collector(esid: str = "005", **overrides) -> DataCollector:
    """Build a minimal valid DataCollector (field names, not CSV aliases)."""
    data = {
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
    }
    data.update(overrides)
    return DataCollector.model_validate(data)


def make_zip(zip_path: Path, entry_names, esid: str = "005") -> Path:
    """Create a real ZIP whose entries live under an ESID_XXX/ subfolder."""
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name in entry_names:
            zf.writestr(f"ESID_{esid}/{name}", b"\x01" * 64)
    return zip_path


class TempDirTestCase(unittest.TestCase):
    """Base: every test gets a throwaway directory in self.root."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)


# --- load_project_config -----------------------------------------------------

class TestLoadProjectConfig(TempDirTestCase):
    def test_loads_example_template(self):
        config = load_example_config(self.root)
        self.assertEqual(
            config["title_template"], "$eclipse_date $eclipse_label ESID#$esid"
        )
        self.assertEqual(len(config["creators"]), 2)
        self.assertEqual(config["minimum_recording_year"], 2000)
        self.assertEqual(config["license"], "cc-by-4.0")

    def test_missing_file_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError) as cm:
            tasks.load_project_config(str(self.root / "nope.json"))
        self.assertIn("Project config not found", str(cm.exception))

    def test_invalid_json_raises_decode_error(self):
        bad = self.root / "broken.json"
        bad.write_text("{ this is not json", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            tasks.load_project_config(str(bad))


# --- build_creators ----------------------------------------------------------

class TestBuildCreators(TempDirTestCase):
    def test_example_template_produces_two_creators(self):
        """The shipped template has one organizational + one personal creator.

        Its orcid/affiliation values are empty strings, so no identifiers
        are built and the empty affiliations are filtered to None.
        """
        config = load_example_config(self.root)
        creators = tasks.build_creators(config)
        self.assertEqual(len(creators), 2)

        org, person = creators
        self.assertEqual(org.person_or_org.type, "organizational")
        self.assertEqual(org.role.id, "hostinginstitution")
        self.assertIsNone(org.person_or_org.identifiers)  # empty orcid
        self.assertIsNone(org.affiliations)  # [] filtered to None

        self.assertEqual(person.person_or_org.type, "personal")
        self.assertEqual(person.role.id, "datamanager")
        self.assertIsNone(person.person_or_org.identifiers)

    def test_full_personal_entry_serialized_shape(self):
        """A filled-in entry: ORCID becomes an identifier, affiliations kept,
        and exclude_none drops every unset optional field from the payload."""
        config = {"creators": [{
            "type": "personal",
            "given_name": "Ada",
            "family_name": "Lovelace",
            "orcid": "0000-0001-2345-6789",
            "role": "datamanager",
            "affiliations": ["ARISA Lab"],
        }]}
        (creator,) = tasks.build_creators(config)
        self.assertEqual(
            creator.model_dump(exclude_none=True),
            {
                "person_or_org": {
                    "type": "personal",
                    "given_name": "Ada",
                    "family_name": "Lovelace",
                    "identifiers": [
                        {"scheme": "orcid",
                         "identifier": "0000-0001-2345-6789"},
                    ],
                },
                "role": {"id": "datamanager"},
                "affiliations": [{"name": "ARISA Lab"}],
            },
        )

    def test_empty_and_whitespace_affiliations_filtered(self):
        # Zenodo rejects {"name": ""} affiliations — they must be dropped.
        config = {"creators": [{
            "type": "organizational",
            "name": "ARISA Lab",
            "affiliations": ["", "   ", "Real Org"],
        }]}
        (creator,) = tasks.build_creators(config)
        self.assertEqual(
            [a.name for a in creator.affiliations], ["Real Org"]
        )

    def test_missing_role_defaults_to_other(self):
        config = {"creators": [{"type": "organizational", "name": "X"}]}
        (creator,) = tasks.build_creators(config)
        self.assertEqual(creator.role.id, "other")

    def test_no_creators_key_returns_empty_list(self):
        self.assertEqual(tasks.build_creators({}), [])


# --- build_contributors --------------------------------------------------------

class TestBuildContributors(TempDirTestCase):
    def test_example_template_contributor(self):
        config = load_example_config(self.root)
        contributors = tasks.build_contributors(config)
        self.assertEqual(len(contributors), 1)
        self.assertEqual(contributors[0].person_or_org.type, "personal")
        self.assertEqual(contributors[0].role.id, "projectmember")

    def test_full_entry_serialized_shape(self):
        config = {"contributors": [{
            "type": "personal",
            "given_name": "Grace",
            "family_name": "Hopper",
            "orcid": "0000-0002-9999-0000",
            "role": "researcher",
            "affiliations": ["Navy"],
        }]}
        (contributor,) = tasks.build_contributors(config)
        self.assertEqual(
            contributor.model_dump(exclude_none=True),
            {
                "person_or_org": {
                    "type": "personal",
                    "given_name": "Grace",
                    "family_name": "Hopper",
                    "identifiers": [
                        {"scheme": "orcid",
                         "identifier": "0000-0002-9999-0000"},
                    ],
                },
                "role": {"id": "researcher"},
                "affiliations": [{"name": "Navy"}],
            },
        )

    def test_no_contributors_key_returns_empty_list(self):
        self.assertEqual(tasks.build_contributors({}), [])


# --- build_fundings ------------------------------------------------------------

class TestBuildFundings(TempDirTestCase):
    def test_full_entry_award_url_becomes_url_identifier(self):
        config = {"funding": [{
            "funder_id": "021nxhr62",
            "award_title": "Eclipse Soundscapes",
            "award_number": "80NSSC21M0008",
            "award_url": "https://example.org/award/80NSSC21M0008",
        }]}
        (funding,) = tasks.build_fundings(config)
        self.assertEqual(
            funding.model_dump(exclude_none=True),
            {
                "funder": {"id": "021nxhr62"},
                "award": {
                    "title": {"en": "Eclipse Soundscapes"},
                    "number": "80NSSC21M0008",
                    "identifiers": [
                        {"scheme": "url",
                         "identifier":
                             "https://example.org/award/80NSSC21M0008"},
                    ],
                },
            },
        )

    def test_empty_award_url_yields_no_identifiers(self):
        # The example template ships empty award_url — no identifier built.
        config = load_example_config(self.root)
        (funding,) = tasks.build_fundings(config)
        self.assertIsNone(funding.award.identifiers)

    def test_no_funding_key_returns_empty_list(self):
        self.assertEqual(tasks.build_fundings({}), [])


# --- get_draft_config ----------------------------------------------------------

class TestGetDraftConfig(TempDirTestCase):
    """Draft assembly from the example template + a minimal DataCollector."""

    README_HTML = "<html><body><p>Dataset description.</p></body></html>"

    def setUp(self):
        super().setUp()
        self.config = load_example_config(self.root)
        # The template ships community_id: "" — give it a value so the
        # pass-through into DraftConfig is observable.
        self.config["community_id"] = "escsp-test-community"
        self.readme = self.root / "README.html"
        self.readme.write_text(self.README_HTML, encoding="utf-8")
        self.collector = make_collector()

    def _draft(self, **kwargs):
        return tasks.get_draft_config(
            data_collector=self.collector,
            readme_html_path=str(self.readme),
            project_config=self.config,
            **kwargs,
        )

    def test_title_is_templated_from_collector(self):
        # title_template = "$eclipse_date $eclipse_label ESID#$esid"
        draft = self._draft()
        self.assertEqual(
            draft.metadata["title"],
            "2024-04-08 Total Solar Eclipse ESID#005",
        )

    def test_description_community_and_license(self):
        draft = self._draft()
        self.assertEqual(draft.metadata["description"], self.README_HTML)
        self.assertEqual(draft.community_id, "escsp-test-community")
        self.assertEqual(draft.metadata["rights"], [{"id": "cc-by-4.0"}])
        self.assertEqual(draft.metadata["languages"], [{"id": "eng"}])

    def test_reserve_doi_true_adds_datacite_pids(self):
        draft = self._draft(reserve_doi=True)
        self.assertEqual(
            draft.pids,
            {"doi": {"provider": "datacite", "identifier": ""}},
        )

    def test_reserve_doi_false_has_no_pids_payload(self):
        draft = self._draft(reserve_doi=False)
        self.assertEqual(draft.pids, {})

    def test_missing_readme_path_raises_value_error(self):
        with self.assertRaises(ValueError):
            tasks.get_draft_config(
                data_collector=self.collector,
                readme_html_path=None,
                project_config=self.config,
            )

    def test_nonexistent_readme_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            tasks.get_draft_config(
                data_collector=self.collector,
                readme_html_path=str(self.root / "missing" / "README.html"),
                project_config=self.config,
            )

    def test_recording_period_becomes_edtf_interval(self):
        self.collector = make_collector(
            first_recording_day="2024-04-08",
            last_recording_day="2024-04-09",
        )
        draft = self._draft()
        self.assertEqual(
            draft.metadata["dates"],
            [{
                "date": "2024-04-08/2024-04-09",
                "type": {"id": "collected"},
                "description": "Recording period",
            }],
        )

    def test_single_day_recording_is_a_plain_date(self):
        self.collector = make_collector(
            first_recording_day="2024-04-08",
            last_recording_day="2024-04-08",
        )
        draft = self._draft()
        self.assertEqual(
            draft.metadata["dates"],
            [{
                "date": "2024-04-08",
                "type": {"id": "collected"},
                "description": "Day of recording",
            }],
        )

    def test_volunteer_creator_appended_with_parsed_affiliations(self):
        """The template's volunteer_creator_label appends one organizational
        creator per dataset, with the collector's colon-delimited
        affiliation string split into individual affiliations."""
        draft = self._draft()
        creators = draft.metadata["creators"]
        # 2 from the example config + 1 volunteer
        self.assertEqual(len(creators), 3)
        self.assertEqual(
            creators[-1],
            {
                "person_or_org": {
                    "type": "organizational",
                    "name": "Volunteer Scientist",
                },
                "role": {"id": "datacollector"},
                "affiliations": [
                    {"name": "Eclipse Soundscapes"},
                    {"name": "ARISA Lab"},
                ],
            },
        )

    def test_subjects_parsed_from_collector_keywords(self):
        draft = self._draft()
        self.assertEqual(
            draft.metadata["subjects"],
            [{"subject": "eclipse"}, {"subject": "audiomoth"}],
        )


# --- get_recording_dates --------------------------------------------------------

class TestGetRecordingDates(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.config = load_example_config(self.root)  # minimum year 2000

    def test_min_and_max_dates_from_wav_names(self):
        zip_path = make_zip(
            self.root / "ESID_005.zip",
            ["20240409_130000.WAV", "20240408_120000.WAV"],
        )
        # Expected strings follow tasks.UPLOAD_DATE_FORMAT ("%Y-%m-%d").
        self.assertEqual(
            tasks.get_recording_dates([str(zip_path)], self.config),
            ("2024-04-08", "2024-04-09"),
        )

    def test_dates_below_minimum_recording_year_filtered(self):
        # 1999 predates minimum_recording_year (2000) — an AudioMoth with an
        # unset clock must not stretch the recording interval.
        zip_path = make_zip(
            self.root / "ESID_005.zip",
            ["19990101_000000.WAV", "20240408_120000.WAV",
             "20240409_130000.WAV"],
        )
        self.assertEqual(
            tasks.get_recording_dates([str(zip_path)], self.config),
            ("2024-04-08", "2024-04-09"),
        )

    def test_non_wav_entries_ignored(self):
        # A later-dated non-WAV entry must not extend the interval.
        zip_path = make_zip(
            self.root / "ESID_005.zip",
            ["20240408_120000.WAV", "20240501_000000.txt"],
        )
        self.assertEqual(
            tasks.get_recording_dates([str(zip_path)], self.config),
            ("2024-04-08", "2024-04-08"),
        )

    def test_no_valid_dates_raises_value_error(self):
        zip_path = make_zip(
            self.root / "ESID_005.zip",
            ["not-a-date.WAV", "19990101_000000.WAV"],
        )
        with self.assertRaises(ValueError):
            tasks.get_recording_dates([str(zip_path)], self.config)

    def test_missing_zip_raises_value_error(self):
        with self.assertRaises(ValueError):
            tasks.get_recording_dates(
                str(self.root / "ESID_404.zip"), self.config
            )


# --- read_upload_manifest --------------------------------------------------------

def write_manifest(path: Path, names) -> Path:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["File Name", "Description"])
        for name in names:
            writer.writerow([name, "x"])
    return path


class TestReadUploadManifest(TempDirTestCase):
    def test_all_listed_files_found(self):
        for name in ("ESID_005.zip", "Data_Dictionary.csv"):
            (self.root / name).write_bytes(b"data")
        manifest = write_manifest(
            self.root / "ESID_005_to_upload.csv",
            ["ESID_005.zip", "Data_Dictionary.csv"],
        )
        found = tasks.read_upload_manifest(manifest, self.root)
        self.assertEqual(
            found,
            {
                "ESID_005.zip": str(self.root / "ESID_005.zip"),
                "Data_Dictionary.csv": str(self.root / "Data_Dictionary.csv"),
            },
        )

    def test_missing_file_raises_and_names_it(self):
        (self.root / "ESID_005.zip").write_bytes(b"data")
        manifest = write_manifest(
            self.root / "ESID_005_to_upload.csv",
            ["ESID_005.zip", "GHOST_FILE.csv"],
        )
        with self.assertRaises(FileNotFoundError) as cm:
            tasks.read_upload_manifest(manifest, self.root)
        self.assertIn("GHOST_FILE.csv", str(cm.exception))

    def test_manifest_without_file_name_column_raises(self):
        manifest = self.root / "ESID_005_to_upload.csv"
        with open(manifest, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["Wrong Column"])
            writer.writerow(["ESID_005.zip"])
        with self.assertRaises(ValueError):
            tasks.read_upload_manifest(manifest, self.root)

    def test_blank_filename_rows_are_skipped(self):
        (self.root / "ESID_005.zip").write_bytes(b"data")
        manifest = write_manifest(
            self.root / "ESID_005_to_upload.csv",
            ["ESID_005.zip", "", "   "],
        )
        found = tasks.read_upload_manifest(manifest, self.root)
        self.assertEqual(list(found), ["ESID_005.zip"])


# --- create_upload_data ------------------------------------------------------------

class TestCreateUploadData(TempDirTestCase):
    """Assembly of UploadData bundles from ESID/file pairs + collectors.

    Each staging folder mimics prepare_dataset.py output: the dataset ZIP,
    a data dictionary, an ESID_XXX_to_upload.csv manifest, and READMEs.
    """

    def setUp(self):
        super().setUp()
        self.config = load_example_config(self.root)

    def _make_staging(self, esid="005", manifest_names=None, readmes=True):
        staging = self.root / f"ESID_{esid}_Staging"
        staging.mkdir()
        zip_path = make_zip(
            staging / f"ESID_{esid}.zip", ["20240408_120000.WAV"], esid=esid
        )
        dd_path = staging / f"ESID_{esid}_DataDictionary.csv"
        dd_path.write_text("Field,Description\n", encoding="utf-8")
        if manifest_names is None:
            manifest_names = [zip_path.name, dd_path.name]
        write_manifest(
            staging / f"ESID_{esid}_to_upload.csv", manifest_names
        )
        if readmes:
            (staging / "README.html").write_text("<p>d</p>", encoding="utf-8")
            (staging / "README.md").write_text("# d", encoding="utf-8")
        return staging, zip_path, dd_path

    def test_matched_pair_builds_upload_data(self):
        staging, zip_path, dd_path = self._make_staging()
        upload_data, unmatched = tasks.create_upload_data(
            [("005", str(staging), [str(zip_path)])],
            [make_collector("005")],
            project_config=self.config,
        )
        self.assertEqual(unmatched, [])
        self.assertEqual(len(upload_data), 1)
        (data,) = upload_data
        self.assertIsInstance(data, UploadData)
        self.assertEqual(data.esid, "005")
        self.assertEqual(data.archives, [str(zip_path)])
        self.assertEqual(data.staging_folder, str(staging))
        # The archives are carried by the dedicated archives field — they
        # must NOT also appear in additional_files (double upload = 400).
        self.assertEqual(data.additional_files, [str(dd_path)])
        # READMEs resolve from the staging dir even though the manifest
        # never lists them.
        self.assertEqual(data.readme_html, str(staging / "README.html"))
        self.assertEqual(data.readme_md, str(staging / "README.md"))
        # all_files: README.md first, metadata next, ZIP last.
        self.assertEqual(
            data.all_files,
            [str(staging / "README.md"), str(dd_path), str(zip_path)],
        )

    def test_missing_readmes_stay_none(self):
        staging, zip_path, _ = self._make_staging(readmes=False)
        upload_data, _ = tasks.create_upload_data(
            [("005", str(staging), [str(zip_path)])],
            [make_collector("005")],
            project_config=self.config,
        )
        self.assertIsNone(upload_data[0].readme_html)
        self.assertIsNone(upload_data[0].readme_md)

    def test_missing_collector_reports_unmatched_esid(self):
        staging, zip_path, _ = self._make_staging(esid="007")
        upload_data, unmatched = tasks.create_upload_data(
            [("007", str(staging), [str(zip_path)])],
            [make_collector("005")],  # no collector for 007
            project_config=self.config,
        )
        self.assertEqual(upload_data, [])
        self.assertEqual(unmatched, ["007"])

    def test_discovery_failure_writes_failure_row_and_continues(self):
        """A dataset whose manifest lists a missing file must not abort the
        batch: it gets a failure-CSV row and the good dataset still uploads."""
        bad_staging, bad_zip, _ = self._make_staging(
            esid="005",
            manifest_names=["ESID_005.zip", "GHOST_FILE.csv"],
        )
        good_staging, good_zip, _ = self._make_staging(esid="006")
        failure_csv = self.root / "failed_results.csv"

        upload_data, unmatched = tasks.create_upload_data(
            [("005", str(bad_staging), [str(bad_zip)]),
             ("006", str(good_staging), [str(good_zip)])],
            [make_collector("005"), make_collector("006")],
            project_config=self.config,
            failure_results_file=str(failure_csv),
        )

        self.assertEqual(unmatched, [])
        self.assertEqual([d.esid for d in upload_data], ["006"])
        with open(failure_csv, "r", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["esid"], "005")
        self.assertIn("Manifest/file discovery failed",
                      rows[0]["error_message"])

    def test_discovery_failure_without_results_file_skips_dataset(self):
        bad_staging, bad_zip, _ = self._make_staging(
            manifest_names=["ESID_005.zip", "GHOST_FILE.csv"],
        )
        upload_data, unmatched = tasks.create_upload_data(
            [("005", str(bad_staging), [str(bad_zip)])],
            [make_collector("005")],
            project_config=self.config,
        )
        self.assertEqual(upload_data, [])
        self.assertEqual(unmatched, [])


if __name__ == "__main__":
    unittest.main()
