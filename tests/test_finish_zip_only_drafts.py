"""Unit tests for Resources/finish_zip_only_drafts.py.

The tool discovers repairable drafts from ZENODO rather than from local
state files, and a CONVERTIBLE verdict authorises a ONE-WAY DOOR: the
pending ZIP slot is deleted and the staging folder is marked file-by-file.
So the properties under test are mostly refusals — the scan must be
provably read-only, an ambiguous record must never be touched, and the
fail-open hole in ``only_zip_missing_from_entries`` must be unreachable.

No network: the account listing is scripted through
``esid_record_report._api_get_with_retry`` and the per-draft file listing
through a ``list_draft_files`` patched on this module with a side effect
keyed by record id, so each draft can answer differently.

Run from the project root:

    python3 -m unittest discover -s tests -v
"""

import csv
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "Resources"))

import azus_common  # noqa: E402
import esid_record_report as err_mod  # noqa: E402
import file_by_file_upload as fbf  # noqa: E402
import finish_zip_only_drafts as m  # noqa: E402
from prepare_dataset import _FILE_LIST_HEADERS  # noqa: E402

_TITLE = "2024-04-08 Total Solar Eclipse ESID #"
_RES = [m.err_mod.compile_title_pattern(p)
        for p in m.err_mod._DEFAULT_TITLE_PATTERNS]

_WAVS = {
    "20240408_120000.WAV": b"AUDIO-ONE-" * 50,
    "20240408_130000.WAV": b"AUDIO-TWO-" * 40,
}
_CONFIG = b"GAIN=medium\n"
_COMPANIONS = ["README.md", "total_eclipse_data.csv", "file_list.csv"]


# --- fabricated payloads --------------------------------------------------

class FakeResponse:
    """Minimal stand-in for a requests.Response carrying JSON."""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        """Return the canned payload.

        Returns:
            The payload this response was built with.
        """
        return self._payload


def hit(record_id, esid, *, is_published=False, title=None):
    """One /api/user/records hit in the InvenioRDM shape."""
    return {
        "id": str(record_id),
        "is_published": is_published,
        "status": "published" if is_published else "draft",
        "metadata": {"title": title or f"{_TITLE}{esid}"},
    }


def page(hits, total=None):
    """One listing page; a short page terminates the walk."""
    return FakeResponse(
        {"hits": {"hits": hits, "total": total if total is not None
                  else len(hits)}}
    )


def entry(key, status="completed", size=10, checksum="md5:abc"):
    """One draft file entry."""
    return {"key": key, "status": status, "size": size, "checksum": checksum}


def committed(names):
    """Draft entries with every name committed."""
    return [entry(n) for n in names]


def _sha(data):
    return hashlib.sha512(data).hexdigest()


def _flrow(name, data):
    return {
        "File Name": name, "File Type": "x", "Description": "x",
        "File size (KB)": "1", "File size (Bytes)": str(len(data)),
        "Associated Data Dictionary": "N/A", "SHA-512 Hash": _sha(data),
        "Notes": "",
    }


class _Fixture(unittest.TestCase):
    """One prepped ESID 007 draft: staging folder, raw folder, manifests."""

    ESID = "007"
    RECORD = "555"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.staging_area = self.root / "Staging_Area"
        self.uploaded = self.root / "Uploaded_Data"
        self.raw_root = self.root / "Raw_Data"
        self.records = self.root / "Records"
        for d in (self.staging_area, self.uploaded, self.raw_root, self.records):
            d.mkdir()
        self.staging = self.staging_area / f"ESID_{self.ESID}_Staging"
        self.raw = self.raw_root / f"ESID#{self.ESID}"
        self.staging.mkdir()
        self.raw.mkdir()

        # Point the module's staging area and the shared Uploaded_Data at
        # the fixture, so nothing touches the real repo.
        for patcher in (
            mock.patch.object(m, "_STAGING_AREA", self.staging_area),
            mock.patch.object(m.azus_common, "UPLOADED_DATA", self.uploaded),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        self.summary = self.records / "summary.csv"
        self.detail = self.records / "summary_files.csv"
        self.log = self.records / "run.log"

    # --- fixture builders -------------------------------------------------

    def build(self, *, wav_count=None, prep_sentinel=True, manifests=True,
              file_list=True, drop_wav=None, mode=None, state_record=None):
        """Populate the staging + raw folders.  Returns the WAV name list."""
        names = list(_WAVS)
        data = dict(_WAVS)
        if wav_count is not None:
            data = {
                f"20240408_{i:06d}.WAV": b"AUDIO" * (i + 1)
                for i in range(wav_count)
            }
            names = list(data)
        for name, blob in data.items():
            if name != drop_wav:
                (self.raw / name).write_bytes(blob)
        (self.raw / "CONFIG.TXT").write_bytes(_CONFIG)

        (self.staging / "README.md").write_text("# x\n")
        (self.staging / "total_eclipse_data.csv").write_text("ESID\n007\n")
        if prep_sentinel:
            (self.staging / azus_common.PREP_SENTINEL).write_text("")

        if file_list:
            rows = [_flrow(f"ESID_{self.ESID}.zip", b"zipbytes")]
            rows += [_flrow(n, b"x") for n in _COMPANIONS]
            rows.append(_flrow("CONFIG.TXT", _CONFIG))
            rows += [_flrow(n, blob) for n, blob in data.items()]
            with open(self.staging / "file_list.csv", "w", encoding="utf-8",
                      newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=_FILE_LIST_HEADERS)
                w.writeheader()
                w.writerows(rows)

        if manifests:
            with open(self.staging / f"ESID_{self.ESID}_to_upload.csv", "w",
                      encoding="utf-8", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=["File Name", "Notes"])
                w.writeheader()
                for name in [f"ESID_{self.ESID}.zip"] + _COMPANIONS:
                    w.writerow({"File Name": name, "Notes": ""})

        if mode or state_record:
            state = {"number_of_tries": 3}
            if mode:
                state["mode"] = mode
            if state_record:
                state["record_id"] = state_record
            (self.staging / azus_common.STATE_FILENAME).write_text(
                json.dumps(state)
            )
        return names + ["CONFIG.TXT"]

    def prep_extra(self, esid):
        """Prep a second (third, ...) ESID identically to the fixture one."""
        staging = self.staging_area / f"ESID_{esid}_Staging"
        raw = self.raw_root / f"ESID#{esid}"
        staging.mkdir()
        raw.mkdir()
        for name, blob in _WAVS.items():
            (raw / name).write_bytes(blob)
        (raw / "CONFIG.TXT").write_bytes(_CONFIG)
        (staging / "README.md").write_text("# x\n")
        (staging / "total_eclipse_data.csv").write_text("ESID\n")
        (staging / azus_common.PREP_SENTINEL).write_text("")
        rows = [_flrow(f"ESID_{esid}.zip", b"z")]
        rows += [_flrow(n, b"x") for n in _COMPANIONS]
        rows.append(_flrow("CONFIG.TXT", _CONFIG))
        rows += [_flrow(n, b) for n, b in _WAVS.items()]
        with open(staging / "file_list.csv", "w", encoding="utf-8",
                  newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=_FILE_LIST_HEADERS)
            w.writeheader()
            w.writerows(rows)
        with open(staging / f"ESID_{esid}_to_upload.csv", "w",
                  encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["File Name", "Notes"])
            w.writeheader()
            for name in [f"ESID_{esid}.zip"] + _COMPANIONS:
                w.writerow({"File Name": name, "Notes": ""})
        return staging

    def all_committed(self):
        """Every name a complete file-by-file record would hold."""
        return _COMPANIONS + list(_WAVS) + ["CONFIG.TXT"]

    def package(self):
        """The LocalPackage the tool would derive for the fixture."""
        return m.derive_local_package(
            self.ESID,
            {self.ESID: self.staging},
            {self.ESID: self.raw},
        )

    # --- CLI driver -------------------------------------------------------

    def run_cli(self, *extra, pages=None, listings=None, convert=True,
                list_raises=None, fbf_side_effect=None,
                publish_config=(None, False, False)):
        """Run main() with a scripted listing; returns (exit_code, mocks).

        ``fbf_side_effect`` and ``publish_config`` are parameters rather
        than something a test patches around this call: run_cli patches the
        same attributes, and an inner patch started later wins.
        """
        if pages is None:
            pages = [page([hit(self.RECORD, self.ESID)])]
        page_iter = iter(pages)

        def fake_get(**_kwargs):
            return next(page_iter)

        def fake_list(_creds, record_id):
            if list_raises is not None:
                raise list_raises
            if listings is None:
                return []
            value = listings[str(record_id)]
            return value() if callable(value) else value

        creds = mock.Mock()
        creds.base_url = "https://zenodo.invalid/api/"
        creds.token = "t"

        argv = [
            "finish_zip_only_drafts.py", str(self.raw_root),
            "--output", str(self.summary), "--log", str(self.log),
            "--sleep-s", "0", *extra,
        ]
        patchers = {
            "get": mock.patch.object(
                err_mod, "_api_get_with_retry", side_effect=fake_get),
            "creds": mock.patch.object(
                m, "get_credentials_from_env", return_value=creds),
            "list_draft_files": mock.patch.object(
                m, "list_draft_files", side_effect=fake_list),
            "run_file_by_file": (
                mock.patch.object(
                    m.fbf, "run_file_by_file", side_effect=fbf_side_effect)
                if fbf_side_effect is not None else
                mock.patch.object(
                    m.fbf, "run_file_by_file", return_value=convert)
            ),
            "publish_config": mock.patch.object(
                m, "_load_publish_config", return_value=publish_config),
            "auth": mock.patch.object(
                m, "_auth_headers", return_value={}),
            "argv": mock.patch.object(sys, "argv", argv),
        }
        started = {k: p.start() for k, p in patchers.items()}
        for p in patchers.values():
            self.addCleanup(p.stop)
        with self.assertRaises(SystemExit) as ctx:
            m.main()
        return ctx.exception.code, started

    def summary_rows(self):
        """Read the summary report back."""
        with open(self.summary, newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def detail_rows(self):
        """Read the detail report back."""
        with open(self.detail, newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))


# =====================================================================
#  Listing -> ESID
# =====================================================================

class TestListingToEsidMap(unittest.TestCase):
    """Titles become ESIDs only when the capture is a REAL ESID."""

    def test_plain_and_suffixed_titles(self):
        for title_esid, expected in (
            ("073", "073"), ("007", "007"), ("120A", "120A"),
            ("122 Part 1 of 2", "122_Part_1_of_2"),
        ):
            esid, verdict, _note = m.esid_from_hit(
                _RES, [], hit("1", title_esid)
            )
            self.assertEqual((esid, verdict), (expected, ""), title_esid)

    def test_non_esid_title_is_silently_out_of_scope(self):
        esid, verdict, note = m.esid_from_hit(
            _RES, [], {"metadata": {"title": "Some other dataset"}}
        )
        self.assertEqual((esid, verdict, note), (None, "", ""))

    def test_four_digit_titles_do_not_match_at_all(self):
        """The pattern's (?!\\d) already stops 1234 passing as 123 + junk."""
        self.assertIsNone(err_mod.match_title_any(_RES, f"{_TITLE}9999"))
        esid, verdict, _note = m.esid_from_hit(
            _RES, [], {"metadata": {"title": f"{_TITLE}9999"}}
        )
        self.assertEqual((esid, verdict), (None, ""))

    def test_a_capture_outside_the_esid_grammar_is_refused(self):
        """The guard that keeps title-matching and ESID-validity in lockstep.

        ``match_title`` falls back to ``capture[:3]`` rather than aborting a
        whole report — right for a report, wrong for a tool that deletes
        files.  With the default patterns the two grammars agree, so this
        cannot fire in production; it fires here to prove the refusal exists
        and is not merely implied by the regex.
        """
        with mock.patch.object(
            m.err_mod, "title_in_scope", return_value="12A"
        ):
            esid, verdict, note = m.esid_from_hit(
                _RES, [], {"metadata": {"title": f"{_TITLE}012"}}
            )
        self.assertIsNone(esid)
        self.assertEqual(verdict, m.TITLE_UNPARSEABLE)
        self.assertIn("12A", note)

    def test_published_records_are_out_of_scope(self):
        cands, published, ignored = m.candidates_from_hits(
            [hit("1", "007", is_published=True),
             hit("2", "008"),
             {"metadata": {"title": "unrelated"}}],
            _RES, [], "https://zenodo.invalid/",
        )
        self.assertEqual([c.esid for c in cands], ["008"])
        self.assertEqual((published, ignored), (1, 1))

    def test_indeterminate_draft_state_is_reported_not_guessed(self):
        cands, _p, _i = m.candidates_from_hits(
            [{"id": "9", "metadata": {"title": f"{_TITLE}011"}}],
            _RES, [], "https://zenodo.invalid/",
        )
        self.assertEqual(cands[0].verdict, m.DRAFT_STATE_UNKNOWN)

    def test_missing_record_id_is_reported(self):
        cands, _p, _i = m.candidates_from_hits(
            [{"status": "draft", "metadata": {"title": f"{_TITLE}011"}}],
            _RES, [], "https://zenodo.invalid/",
        )
        self.assertEqual(cands[0].verdict, m.NO_RECORD_ID)

    def test_duplicate_drafts_flag_every_member_with_no_network(self):
        """Two drafts for one ESID: converting either orphans the other."""
        cands, _p, _i = m.candidates_from_hits(
            [hit("1", "007"), hit("2", "007"), hit("3", "008")],
            _RES, [], "https://zenodo.invalid/",
        )
        flagged = m.flag_duplicate_esids(cands)
        self.assertEqual(flagged, 2)
        by_id = {c.record_id: c for c in cands}
        self.assertEqual(by_id["1"].verdict, m.DUPLICATE_DRAFTS_FOR_ESID)
        self.assertEqual(by_id["2"].verdict, m.DUPLICATE_DRAFTS_FOR_ESID)
        self.assertEqual(by_id["3"].verdict, "")
        # Each row names the other draft, so the operator can compare them.
        self.assertIn("2", by_id["1"].note)
        self.assertIn("1", by_id["2"].note)


# =====================================================================
#  The verdict decision (pure)
# =====================================================================

class TestZipOnlyDecision(unittest.TestCase):
    """Every verdict, from entries alone, with no network."""

    ZIP = "ESID_007.zip"
    RAW = [("a.WAV", "h1"), ("CONFIG.TXT", "h2")]
    COMP = ["README.md", "file_list.csv"]

    def decide(self, entries, *, fbf_mode=False, raw=None, comp=None):
        return m.classify_from_entries(
            entries,
            self.RAW if raw is None else raw,
            self.COMP if comp is None else comp,
            self.ZIP, already_file_by_file=fbf_mode,
        )

    def test_committed_zip_means_nothing_to_do(self):
        verdict, _ = self.decide(committed(self.COMP + [self.ZIP]))
        self.assertEqual(verdict, m.ZIP_ALREADY_COMMITTED)

    def test_pending_zip_still_qualifies(self):
        """A pending slot is the NORMAL residue of a timed-out ZIP."""
        entries = committed(self.COMP) + [entry(self.ZIP, status="pending")]
        verdict, _ = self.decide(entries)
        self.assertEqual(verdict, m.CONVERTIBLE)

    def test_no_zip_entry_at_all_qualifies(self):
        verdict, _ = self.decide(committed(self.COMP))
        self.assertEqual(verdict, m.CONVERTIBLE)

    def test_missing_companion_is_not_a_zip_size_problem(self):
        verdict, note = self.decide(committed(["README.md"]))
        self.assertEqual(verdict, m.COMPANIONS_MISSING)
        self.assertIn("file_list.csv", note)

    def test_already_file_by_file_resumes_even_mid_upload(self):
        """The property that makes a weeks-long run survivable.

        Treating "some WAVs already committed" as a skip would strand every
        ESID a restart interrupted.
        """
        entries = committed(self.COMP + ["a.WAV"])
        verdict, note = self.decide(entries, fbf_mode=True)
        self.assertEqual(verdict, m.RESUMABLE)
        self.assertIn("1/2", note)

    def test_already_file_by_file_resumes_before_any_upload(self):
        """Marked, then killed before the first PUT: still a resume."""
        verdict, _ = self.decide(committed(self.COMP), fbf_mode=True)
        self.assertEqual(verdict, m.RESUMABLE)

    def test_file_by_file_resume_is_not_blocked_by_a_companion(self):
        """Once switched, an uncommitted companion is just work remaining."""
        verdict, _ = self.decide(committed(["README.md"]), fbf_mode=True)
        self.assertEqual(verdict, m.RESUMABLE)

    def test_raw_files_without_the_mode_marker_are_refused(self):
        verdict, note = self.decide(committed(self.COMP + ["a.WAV"]))
        self.assertEqual(verdict, m.PARTIALLY_CONVERTED)
        self.assertIn("a.WAV", note)

    def test_config_txt_alone_counts_as_partially_converted(self):
        """CONFIG.TXT is a raw upload name — a ZIP record must not hold it."""
        verdict, _ = self.decide(committed(self.COMP + ["CONFIG.TXT"]))
        self.assertEqual(verdict, m.PARTIALLY_CONVERTED)


class TestFailOpenClosed(unittest.TestCase):
    """The regression that matters most.

    ``only_zip_missing_from_entries`` fails OPEN: with no companions the
    "every companion is committed" test is vacuously true, so a record
    missing EVERYTHING reads as "only the ZIP is missing" — and that answer
    authorises a one-way door.  The new decision must refuse instead, and
    must do so structurally rather than relying on a caller's discipline.
    """

    def test_the_underlying_predicate_really_does_fail_open(self):
        self.assertTrue(
            fbf.only_zip_missing_from_entries([], [], "ESID_007.zip")
        )

    def test_empty_companion_list_is_refused(self):
        verdict, note = m.classify_from_entries(
            [], [("a.WAV", "h")], [], "ESID_007.zip",
            already_file_by_file=False,
        )
        self.assertEqual(verdict, m.MANIFESTS_MISSING)
        self.assertIn("0 companion", note)

    def test_empty_raw_list_is_refused(self):
        verdict, _ = m.classify_from_entries(
            [], [], ["README.md"], "ESID_007.zip",
            already_file_by_file=False,
        )
        self.assertEqual(verdict, m.MANIFESTS_MISSING)

    def test_refusal_holds_even_in_file_by_file_mode(self):
        """A resume must not bypass the check either."""
        verdict, _ = m.classify_from_entries(
            [], [], [], "ESID_007.zip", already_file_by_file=True,
        )
        self.assertEqual(verdict, m.MANIFESTS_MISSING)

    def test_a_committed_zip_still_wins_over_the_refusal(self):
        """Order matters: a good ZIP is reported as such, not as a manifest
        problem, because the recommended actions differ completely."""
        verdict, _ = m.classify_from_entries(
            committed(["ESID_007.zip"]), [], [], "ESID_007.zip",
            already_file_by_file=False,
        )
        self.assertEqual(verdict, m.ZIP_ALREADY_COMMITTED)


# =====================================================================
#  Local gates
# =====================================================================

class TestLocalGates(_Fixture):
    """Every local refusal, all reached without a network call."""

    def verdict(self, max_files=96, record="555"):
        return m.local_verdict(self.package(), record, self.ESID, max_files)

    def test_a_complete_package_passes(self):
        self.build()
        self.assertEqual(self.verdict(), ("", ""))

    def test_missing_staging_folder(self):
        self.build()
        for child in self.staging.iterdir():
            child.unlink()
        self.staging.rmdir()
        self.assertEqual(self.verdict()[0], m.NO_STAGING_FOLDER)

    def test_missing_prep_sentinel(self):
        self.build(prep_sentinel=False)
        self.assertEqual(self.verdict()[0], m.PREP_INCOMPLETE)

    def test_missing_file_list(self):
        self.build(file_list=False)
        self.assertEqual(self.verdict()[0], m.MANIFESTS_MISSING)

    def test_missing_upload_manifest(self):
        self.build(manifests=False)
        self.assertEqual(self.verdict()[0], m.MANIFESTS_MISSING)

    def test_missing_raw_wav(self):
        self.build(drop_wav="20240408_120000.WAV")
        verdict, note = self.verdict()
        self.assertEqual(verdict, m.RAW_FILES_MISSING)
        self.assertIn("20240408_120000.WAV", note)

    def test_no_raw_folder(self):
        self.build()
        package = m.derive_local_package(
            self.ESID, {self.ESID: self.staging}, {}
        )
        verdict, _ = m.local_verdict(package, "555", self.ESID, 96)
        self.assertEqual(verdict, m.NO_RAW_FOLDER)

    def test_uploaded_twin_is_refused(self):
        self.build()
        (self.uploaded / f"ESID_{self.ESID}_Uploaded").mkdir()
        self.assertEqual(self.verdict()[0], m.UPLOADED_TWIN_EXISTS)

    def test_state_file_naming_another_record_is_refused(self):
        self.build(state_record="999")
        verdict, note = self.verdict(record="555")
        self.assertEqual(verdict, m.STATE_RECORD_MISMATCH)
        self.assertIn("999", note)

    def test_state_file_naming_this_record_passes(self):
        self.build(state_record="555")
        self.assertEqual(self.verdict(record="555"), ("", ""))


class TestFileCountGuard(_Fixture):
    """Requirement 2, decided before any network call for that draft."""

    def test_exactly_at_the_ceiling_is_allowed(self):
        names = self.build(wav_count=10)
        package = self.package()
        total = len(package.raw_files) + len(package.companion_names)
        self.assertEqual(total, len(names) + len(_COMPANIONS))
        self.assertEqual(
            m.local_verdict(package, "555", self.ESID, total), ("", "")
        )

    def test_one_over_the_ceiling_is_refused(self):
        self.build(wav_count=10)
        package = self.package()
        total = len(package.raw_files) + len(package.companion_names)
        verdict, note = m.local_verdict(
            package, "555", self.ESID, total - 1
        )
        self.assertEqual(verdict, m.TOO_MANY_FILES)
        self.assertIn(str(total), note)

    def test_oversized_draft_costs_zero_network_calls(self):
        """ESID 797 had 6270 WAVs and burned a hash pass plus three 500s
        before failing.  The refusal must precede every request."""
        self.build(wav_count=12)
        code, mocks = self.run_cli("--max-files", "5")
        self.assertEqual(code, 1)
        mocks["list_draft_files"].assert_not_called()
        self.assertEqual(
            [r["Verdict"] for r in self.summary_rows()], [m.TOO_MANY_FILES]
        )

    def test_the_engine_keeps_its_own_hard_zenodo_ceiling(self):
        """--max-files is operator policy; 100 is the API's own limit."""
        self.assertEqual(fbf._ZENODO_MAX_FILES_PER_RECORD, 100)
        self.assertLess(m._DEFAULT_MAX_FILES, 100)


# =====================================================================
#  The scan writes nothing
# =====================================================================

class TestScanIsReadOnly(_Fixture):
    """A read-only run must be provably read-only."""

    def test_no_state_file_appears_and_no_mutating_call_is_made(self):
        self.build()
        state = self.staging / azus_common.STATE_FILENAME
        self.assertFalse(state.exists())
        code, mocks = self.run_cli(
            listings={self.RECORD: committed(_COMPANIONS)}
        )
        self.assertEqual(code, 1)  # CONVERTIBLE is outstanding work
        self.assertFalse(state.exists())
        mocks["run_file_by_file"].assert_not_called()
        rows = self.summary_rows()
        self.assertEqual(rows[0]["Verdict"], m.CONVERTIBLE)
        self.assertEqual(rows[0]["Action Taken"], m._ACTION_NONE_DRY_RUN)
        self.assertEqual(rows[0]["Record ID Written"], "n")

    def test_manifests_are_not_rewritten(self):
        self.build()
        manifest = self.staging / f"ESID_{self.ESID}_to_upload.csv"
        before = manifest.read_bytes(), (self.staging / "file_list.csv").read_bytes()
        self.run_cli(listings={self.RECORD: committed(_COMPANIONS)})
        after = manifest.read_bytes(), (self.staging / "file_list.csv").read_bytes()
        self.assertEqual(before, after)

    def test_benign_only_run_exits_zero(self):
        self.build()
        code, _ = self.run_cli(
            listings={self.RECORD: committed(
                _COMPANIONS + [f"ESID_{self.ESID}.zip"]
            )}
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            self.summary_rows()[0]["Verdict"], m.ZIP_ALREADY_COMMITTED
        )

    def test_rows_are_flushed_as_the_scan_proceeds(self):
        """A kill must lose at most the in-flight ESID, never earlier rows."""
        self.build()
        self.prep_extra("008")
        seen = {}

        def second_listing():
            # Called while classifying ESID 008 — ESID 007's row must
            # already be readable on disk, not sitting in a buffer.
            seen["rows"] = self.summary_rows()
            seen["details"] = self.detail_rows()
            return committed(_COMPANIONS)

        self.run_cli(
            pages=[page([hit("555", "007"), hit("556", "008")])],
            listings={"555": committed(_COMPANIONS), "556": second_listing},
        )
        self.assertEqual([r["ESID#"] for r in seen["rows"]], ["007"])
        self.assertTrue(seen["details"])
        self.assertEqual(len(self.summary_rows()), 2)

    def test_detail_report_reconciles_local_and_remote(self):
        self.build()
        entries = committed(_COMPANIONS) + [
            entry(f"ESID_{self.ESID}.zip", status="pending"),
            entry("stray.txt"),
        ]
        self.run_cli("--with-hashes", listings={self.RECORD: entries})
        rows = {r["File Name"]: r for r in self.detail_rows()}
        self.assertEqual(rows["README.md"]["Source"], m._SOURCE_COMPANION)
        self.assertEqual(
            rows["README.md"]["Status"], m._STATUS_ALREADY_COMMITTED
        )
        wav = rows["20240408_120000.WAV"]
        self.assertEqual(wav["Source"], m._SOURCE_RAW)
        self.assertEqual(wav["Status"], m._STATUS_TO_UPLOAD)
        self.assertEqual(wav["SHA-512"], _sha(_WAVS["20240408_120000.WAV"]))
        self.assertEqual(wav["Size (Bytes)"], str(len(
            _WAVS["20240408_120000.WAV"]
        )))
        self.assertEqual(
            rows[f"ESID_{self.ESID}.zip"]["Status"], m._STATUS_TO_DELETE
        )
        self.assertEqual(rows["stray.txt"]["Source"], m._SOURCE_UNEXPECTED)
        self.assertEqual(rows["stray.txt"]["Status"], m._STATUS_ON_RECORD_ONLY)

    def test_hash_columns_are_empty_without_the_flag(self):
        self.build()
        self.run_cli(listings={self.RECORD: committed(_COMPANIONS)})
        for row in self.detail_rows():
            self.assertEqual(row["SHA-512"], "")
            self.assertEqual(row["MD5"], "")

    def test_hash_columns_never_read_a_file(self):
        """Cache-only in both modes: a scan must not hash terabytes."""
        self.build()
        with mock.patch.object(
            m.hash_raw_wavs.azus_common, "calculate_digests",
            side_effect=AssertionError("the scan hashed a file"),
        ):
            self.run_cli(
                "--with-hashes", listings={self.RECORD: committed(_COMPANIONS)}
            )
        # md5 comes only from a warm cache; there is none here.
        wavs = [r for r in self.detail_rows() if r["Source"] == m._SOURCE_RAW]
        self.assertTrue(wavs)
        self.assertTrue(all(r["MD5"] == "" for r in wavs))

    def test_md5_column_comes_from_the_warm_cache(self):
        self.build()
        m.hash_raw_wavs.ensure_hashes(
            self.raw, sorted(p.name for p in self.raw.iterdir()), need_md5=True,
        )
        self.run_cli(
            "--with-hashes", listings={self.RECORD: committed(_COMPANIONS)}
        )
        rows = {r["File Name"]: r for r in self.detail_rows()}
        expected = azus_common.calculate_digests(
            str(self.raw / "20240408_120000.WAV"), ("md5",)
        )["md5"]
        self.assertEqual(rows["20240408_120000.WAV"]["MD5"], expected)


# =====================================================================
#  Listing-level failures
# =====================================================================

class TestListingFailuresWriteNoCsv(_Fixture):
    """A truncated scan must never look complete."""

    def test_report_error_aborts_with_no_csv(self):
        self.build()
        with mock.patch.object(
            m, "discover_drafts",
            side_effect=err_mod.ReportError("fetched 3 of 9"),
        ):
            code, _ = self.run_cli()
        self.assertEqual(code, 2)
        self.assertFalse(self.summary.exists())

    def test_unexpected_listing_failure_also_writes_no_csv(self):
        self.build()
        with mock.patch.object(
            m, "discover_drafts", side_effect=RuntimeError("socket closed"),
        ):
            code, _ = self.run_cli()
        self.assertEqual(code, 2)
        self.assertFalse(self.summary.exists())

    def test_one_draft_listing_failure_does_not_stop_the_scan(self):
        self.build()
        code, _ = self.run_cli(list_raises=RuntimeError("HTTP 500"))
        self.assertEqual(code, 1)
        rows = self.summary_rows()
        self.assertEqual(rows[0]["Verdict"], m.DRAFT_LIST_FAILED)
        self.assertIn("HTTP 500", rows[0]["Notes"])


# =====================================================================
#  State recovery
# =====================================================================

class TestStateRecovery(_Fixture):
    """The point of scanning Zenodo: recovering a lost draft pointer."""

    def test_fresh_state_file_is_written_with_zero_tries(self):
        self.build()
        ok = m.write_recovered_state(
            self.staging, "555", title="T", tag="[ESID 007]"
        )
        self.assertTrue(ok)
        state = json.loads(
            (self.staging / azus_common.STATE_FILENAME).read_text()
        )
        self.assertEqual(state["record_id"], "555")
        # Recovering a pointer is NOT an upload attempt: starting at 1 would
        # push ESIDs toward finish_stuck_uploads' --tries-threshold for work
        # nobody performed.
        self.assertEqual(state["number_of_tries"], 0)
        self.assertIn("finish_zip_only_drafts", state["restored_from"])

    def test_mismatched_state_file_is_never_overwritten(self):
        self.build(state_record="999")
        path = self.staging / azus_common.STATE_FILENAME
        before = path.read_bytes()
        ok = m.write_recovered_state(
            self.staging, "555", title="T", tag="[ESID 007]"
        )
        self.assertFalse(ok)
        self.assertEqual(path.read_bytes(), before)

    def test_matching_state_file_is_left_byte_identical(self):
        """A restart must not reset number_of_tries or drop other keys."""
        self.build(state_record="555")
        path = self.staging / azus_common.STATE_FILENAME
        before = path.read_bytes()
        self.assertFalse(m.write_recovered_state(
            self.staging, "555", title="T", tag="[ESID 007]"
        ))
        self.assertEqual(path.read_bytes(), before)

    def test_execute_writes_the_pointer_and_reports_it(self):
        self.build()
        state = self.staging / azus_common.STATE_FILENAME
        code, _mocks = self.run_cli(
            "--execute", "--yes",
            listings={self.RECORD: committed(_COMPANIONS)},
        )
        self.assertEqual(code, 0)
        self.assertTrue(state.exists())
        rows = self.summary_rows()
        self.assertEqual(rows[0]["Record ID Written"], "y")
        self.assertEqual(rows[0]["State File Record ID"], self.RECORD)

    def test_pointer_is_written_before_the_upload_starts(self):
        """An interruption must leave a folder the recovery tools can find."""
        self.build()
        state = self.staging / azus_common.STATE_FILENAME
        observed = {}

        def spy(**_kwargs):
            observed["state_existed"] = state.exists()
            return True

        self.run_cli(
            "--execute", "--yes",
            listings={self.RECORD: committed(_COMPANIONS)},
            fbf_side_effect=spy,
        )
        self.assertTrue(observed["state_existed"])

    def test_resume_does_not_rewrite_the_state_file(self):
        self.build(mode=azus_common.FILE_BY_FILE_MODE, state_record="555")
        path = self.staging / azus_common.STATE_FILENAME
        before = path.read_bytes()
        code, _ = self.run_cli(
            "--execute", "--yes",
            listings={self.RECORD: committed(_COMPANIONS)},
        )
        self.assertEqual(code, 0)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.summary_rows()[0]["Verdict"], m.RESUMABLE)


# =====================================================================
#  Publish gating
# =====================================================================

class TestPublishGating(_Fixture):
    """auto_publish is the master gate, and it is OFF by default."""

    def test_publish_without_execute_is_a_usage_error(self):
        self.build()
        with mock.patch.object(sys, "argv", [
            "finish_zip_only_drafts.py", str(self.raw_root), "--publish",
        ]):
            with self.assertRaises(SystemExit) as ctx:
                m.main()
        self.assertEqual(ctx.exception.code, 2)

    def test_execute_defaults_to_leaving_a_draft(self):
        self.build()
        code, mocks = self.run_cli(
            "--execute", "--yes",
            listings={self.RECORD: committed(_COMPANIONS)},
        )
        self.assertEqual(code, 0)
        kwargs = mocks["run_file_by_file"].call_args.kwargs
        self.assertFalse(kwargs["auto_publish"])

    def test_publish_forwards_the_master_gate(self):
        self.build()
        _code, mocks = self.run_cli(
            "--execute", "--publish", "--yes",
            listings={self.RECORD: committed(_COMPANIONS)},
        )
        self.assertTrue(
            mocks["run_file_by_file"].call_args.kwargs["auto_publish"]
        )

    def test_community_id_comes_from_the_config_not_the_flag(self):
        self.build()
        _code, mocks = self.run_cli(
            "--execute", "--yes",
            listings={self.RECORD: committed(_COMPANIONS)},
            publish_config=("COMM-UUID", True, True),
        )
        kwargs = mocks["run_file_by_file"].call_args.kwargs
        self.assertEqual(kwargs["community_id"], "COMM-UUID")
        self.assertTrue(kwargs["reserve_doi"])
        # config's auto_publish is deliberately IGNORED: --publish is the
        # only way to leave draft state from this tool.
        self.assertFalse(kwargs["auto_publish"])


# =====================================================================
#  upload_attempts bounds
# =====================================================================

class TestUploadAttemptsBounds(_Fixture):
    """The cap is derived from the backoff tuple, never a literal."""

    def test_cap_matches_the_backoff_tuple(self):
        from standalone_uploader import _PUT_RETRY_BACKOFF_S
        self.assertEqual(
            m._MAX_UPLOAD_ATTEMPTS, len(_PUT_RETRY_BACKOFF_S) + 1
        )

    def test_over_the_cap_is_clamped_not_an_indexerror(self):
        self.assertEqual(
            m.bounded_upload_attempts(m._MAX_UPLOAD_ATTEMPTS + 5),
            m._MAX_UPLOAD_ATTEMPTS,
        )

    def test_below_one_is_clamped_up(self):
        self.assertEqual(m.bounded_upload_attempts(0), 1)

    def test_the_clamped_value_is_what_reaches_the_engine(self):
        self.build()
        _code, mocks = self.run_cli(
            "--execute", "--yes", "--upload-attempts", "99",
            listings={self.RECORD: committed(_COMPANIONS)},
        )
        self.assertEqual(
            mocks["run_file_by_file"].call_args.kwargs["upload_attempts"],
            m._MAX_UPLOAD_ATTEMPTS,
        )


# =====================================================================
#  Surviving a weeks-long run
# =====================================================================

class TestSurvivesRestart(_Fixture):
    """Restart, canary and circuit-breaker behaviour."""

    def _three_drafts(self):
        """Prep ESIDs 007, 008 and 009 identically."""
        self.build()
        extra = ["008", "009"]
        for esid in extra:
            self.prep_extra(esid)
        return extra

    def _pages_for(self, esids):
        return [page([hit(f"{500 + i}", e)
                      for i, e in enumerate(["007"] + esids)])]

    def _listings_for(self, esids):
        return {
            str(500 + i): committed(_COMPANIONS)
            for i in range(len(esids) + 1)
        }

    def test_limit_makes_the_first_run_a_canary(self):
        extra = self._three_drafts()
        code, mocks = self.run_cli(
            "--execute", "--yes", "--limit", "1",
            pages=self._pages_for(extra), listings=self._listings_for(extra),
        )
        self.assertEqual(mocks["run_file_by_file"].call_count, 1)
        # Every draft is still classified and reported.
        rows = self.summary_rows()
        self.assertEqual(len(rows), 3)
        actions = [r["Action Taken"] for r in rows]
        self.assertEqual(actions[0], m._ACTION_CONVERTED)
        self.assertEqual(
            actions[1:], [m._ACTION_SKIPPED_LIMIT] * 2
        )
        self.assertEqual(code, 1)  # the two skipped rows are outstanding

    def test_circuit_breaker_stops_after_consecutive_failures(self):
        extra = self._three_drafts()
        code, mocks = self.run_cli(
            "--execute", "--yes", "--max-consecutive-failures", "2",
            pages=self._pages_for(extra), listings=self._listings_for(extra),
            convert=False,
        )
        # Two attempts, then the breaker; the third is reported, not tried.
        self.assertEqual(mocks["run_file_by_file"].call_count, 2)
        actions = [r["Action Taken"] for r in self.summary_rows()]
        self.assertEqual(
            actions, [m._ACTION_FAILED, m._ACTION_FAILED,
                      m._ACTION_SKIPPED_BREAKER],
        )
        self.assertEqual(code, 1)

    def test_a_success_resets_the_failure_streak(self):
        extra = self._three_drafts()
        results = iter([False, True, False])
        code, mocks = self.run_cli(
            "--execute", "--yes", "--max-consecutive-failures", "2",
            pages=self._pages_for(extra), listings=self._listings_for(extra),
            fbf_side_effect=lambda **_k: next(results),
        )
        self.assertEqual(mocks["run_file_by_file"].call_count, 3)
        self.assertEqual(code, 1)

    def test_breaker_can_be_disabled(self):
        extra = self._three_drafts()
        _code, mocks = self.run_cli(
            "--execute", "--yes", "--max-consecutive-failures", "0",
            pages=self._pages_for(extra), listings=self._listings_for(extra),
            convert=False,
        )
        self.assertEqual(mocks["run_file_by_file"].call_count, 3)

    def test_a_stop_request_finishes_the_current_esid_then_stops(self):
        extra = self._three_drafts()

        def convert_then_ask_to_stop(**_kwargs):
            m._stop_requested = True
            return True

        self.addCleanup(setattr, m, "_stop_requested", False)
        code, mocks = self.run_cli(
            "--execute", "--yes",
            pages=self._pages_for(extra), listings=self._listings_for(extra),
            fbf_side_effect=convert_then_ask_to_stop,
        )
        # The in-flight ESID completed; the rest were not attempted.
        self.assertEqual(mocks["run_file_by_file"].call_count, 1)
        rows = self.summary_rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["Action Taken"], m._ACTION_CONVERTED)
        self.assertEqual(rows[1]["Action Taken"], m._ACTION_SKIPPED_STOP)
        self.assertEqual(code, 1)

    def test_a_second_clean_pass_is_a_no_op(self):
        """Re-running is always safe: a finished record falls out benign."""
        self.build()
        code, mocks = self.run_cli(
            "--execute", "--yes",
            listings={self.RECORD: committed(
                _COMPANIONS + [f"ESID_{self.ESID}.zip"]
            )},
        )
        self.assertEqual(code, 0)
        mocks["run_file_by_file"].assert_not_called()

    def test_an_interrupted_conversion_re_scans_as_resumable(self):
        """Not a skip — a skip would strand every restart-interrupted ESID."""
        self.build(mode=azus_common.FILE_BY_FILE_MODE, state_record="555")
        code, mocks = self.run_cli(
            "--execute", "--yes",
            listings={self.RECORD: committed(
                _COMPANIONS + ["20240408_120000.WAV"]
            )},
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.summary_rows()[0]["Verdict"], m.RESUMABLE)
        mocks["run_file_by_file"].assert_called_once()

    def test_reports_written_so_far_survive_a_later_raise(self):
        extra = self._three_drafts()
        calls = {"n": 0}

        def boom(**_kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("volume unmounted")
            return True

        with self.assertRaises(RuntimeError):
            self.run_cli(
                "--execute", "--yes",
                pages=self._pages_for(extra),
                listings=self._listings_for(extra),
                fbf_side_effect=boom,
            )
        # The first ESID's row is on disk even though the run died later.
        rows = self.summary_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Action Taken"], m._ACTION_CONVERTED)


# =====================================================================
#  --esid
# =====================================================================

class TestEsidFilter(_Fixture):
    """--esid behaves as it does everywhere else in AZUS."""

    def test_filters_and_preserves_the_requested_order(self):
        cands = [
            m.Candidate(esid="007", record_id="1", title="a"),
            m.Candidate(esid="008", record_id="2", title="b"),
            m.Candidate(esid="009", record_id="3", title="c"),
        ]
        kept, missing = m._filter_by_esid(cands, ["009", "007"])
        self.assertEqual([c.esid for c in kept], ["009", "007"])
        self.assertEqual(missing, [])

    def test_requested_esid_with_no_draft_is_reported(self):
        cands = [m.Candidate(esid="007", record_id="1", title="a")]
        kept, missing = m._filter_by_esid(cands, ["007", "042"])
        self.assertEqual([c.esid for c in kept], ["007"])
        self.assertEqual(missing, ["042"])

    def test_accepts_a_csv_of_esids(self):
        self.build()
        csv_path = self.records / "wanted.csv"
        with open(csv_path, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["ESID#"])
            w.writerow(["007"])
        code, _ = self.run_cli(
            "--esid", str(csv_path),
            listings={self.RECORD: committed(_COMPANIONS)},
        )
        self.assertEqual(code, 1)
        self.assertEqual([r["ESID#"] for r in self.summary_rows()], ["007"])

    def test_a_previous_summary_report_can_be_fed_straight_back(self):
        """ESID# is the first column precisely so this works."""
        self.build()
        self.run_cli(listings={self.RECORD: committed(_COMPANIONS)})
        first = self.summary_rows()
        self.assertEqual(list(first[0])[0], "ESID#")
        self.assertEqual(
            azus_common.load_esid_args([str(self.summary)]), ["007"]
        )


# =====================================================================
#  Report + verdict schemas
# =====================================================================

class TestReportSchemas(unittest.TestCase):
    """The CSV contract and the verdict/action bookkeeping."""

    def _verdicts(self):
        return {
            value for name, value in vars(m).items()
            if name.isupper() and isinstance(value, str)
            and value == name and name not in {"CACHE_FILENAME"}
        }

    def test_every_verdict_has_a_recommended_action(self):
        self.assertEqual(self._verdicts(), set(m._RECOMMENDED_ACTION))

    def test_benign_and_actionable_verdicts_are_real_verdicts(self):
        known = set(m._RECOMMENDED_ACTION)
        self.assertTrue(m._BENIGN_VERDICTS <= known)
        self.assertTrue(m._ACTIONABLE_VERDICTS <= known)
        self.assertFalse(m._BENIGN_VERDICTS & m._ACTIONABLE_VERDICTS)

    def test_esid_is_the_first_summary_column(self):
        self.assertEqual(m._SUMMARY_COLUMNS[0], "ESID#")
        self.assertEqual(m._DETAIL_COLUMNS[0], "ESID#")

    def test_needs_attention_ignores_benign_rows(self):
        self.assertFalse(m.row_needs_attention({
            "Verdict": m.ZIP_ALREADY_COMMITTED,
            "Action Taken": m._ACTION_NONE,
        }))

    def test_needs_attention_counts_an_unconverted_candidate(self):
        self.assertTrue(m.row_needs_attention({
            "Verdict": m.CONVERTIBLE, "Action Taken": m._ACTION_NONE_DRY_RUN,
        }))

    def test_needs_attention_clears_after_a_conversion(self):
        self.assertFalse(m.row_needs_attention({
            "Verdict": m.CONVERTIBLE, "Action Taken": m._ACTION_CONVERTED,
        }))


if __name__ == "__main__":
    unittest.main()
