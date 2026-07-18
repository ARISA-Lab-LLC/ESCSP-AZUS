"""Unit tests for Resources/esid_record_report.py.

No network: the Zenodo API is simulated with fabricated payloads in both
serializations Zenodo serves (InvenioRDM shape from /api/user/records,
legacy shape from the community listing).  The tests prove the accuracy
guarantees: title-pattern gating BEFORE any validation, deterministic
short-page pagination (never trusting a full page as the end), count
cross-checks, fail-closed serialization handling, merge-conflict
detection, and the exact CSV contract.

Run from the project root:

    python3 -m unittest discover -s tests -v
"""

import csv
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "Resources"))

import esid_record_report as m  # noqa: E402

_WEB = "https://zenodo.org/"
# Canonical matching title prefix (space form; the no-space form is
# covered by the second default pattern).
_T = "2024-04-08 Total Solar Eclipse ESID #"
_DEFAULT_RES = [m.compile_title_pattern(p) for p in m._DEFAULT_TITLE_PATTERNS]


# --- fabricated API payloads ----------------------------------------------

def invenio_hit(record_id, title, is_published, doi=None, self_html=None):
    """A hit in the InvenioRDM shape (/api/user/records)."""
    return {
        "id": record_id,
        "is_published": is_published,
        "status": "published" if is_published else "draft",
        "metadata": {"title": title},
        "pids": {"doi": {"identifier": doi}} if doi else {},
        "links": {"self_html": self_html} if self_html else {},
    }


def legacy_hit(record_id, title, doi=None, html=None):
    """A hit in the legacy shape (community listing) — always published."""
    return {
        "id": record_id,
        "status": "published",
        "title": title,
        "doi": doi,
        "links": {"html": html} if html else {},
    }


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def page(hits, total, next_url=None):
    payload = {"hits": {"hits": hits, "total": total}}
    if next_url:
        payload["links"] = {"next": next_url}
    return FakeResponse(payload)


# --- title pattern ----------------------------------------------------------

class TestCompileTitlePattern(unittest.TestCase):
    def test_default_matches_space_form_any_prefix(self):
        self.assertEqual(m.match_title_any(_DEFAULT_RES, f"{_T}073"), "073")
        self.assertEqual(
            m.match_title_any(
                _DEFAULT_RES, "2023-10-14 Annular Solar Eclipse ESID #009"
            ),
            "009",
        )

    def test_default_matches_nospace_form_too(self):
        # Production published titles use "ESID#NNN" without the space;
        # the second default pattern covers them.
        self.assertEqual(
            m.match_title_any(
                _DEFAULT_RES, "2024-04-08 Total Solar Eclipse ESID#073"
            ),
            "073",
        )

    def test_document_record_is_ignored(self):
        # The real-world false alarm: an Observer Role document record.
        title = ("Eclipse Soundscapes 2023 Observer Role Data: Qualitative "
                 "Observation Data Collected During the Annular Solar "
                 "Eclipse on October 14, 2023")
        self.assertIsNone(m.match_title_any(_DEFAULT_RES, title))

    def test_esid_must_be_exactly_three_digits(self):
        self.assertIsNone(m.match_title_any(_DEFAULT_RES, f"{_T}73"))
        self.assertIsNone(m.match_title_any(_DEFAULT_RES, f"{_T}0733"))

    def test_trailing_text_allowed(self):
        self.assertEqual(
            m.match_title_any(_DEFAULT_RES, f"{_T}073 (v2)"), "073"
        )

    def test_case_insensitive(self):
        self.assertEqual(
            m.match_title_any(_DEFAULT_RES, f"{_T.upper()}073"), "073"
        )

    def test_single_star_pattern_still_works(self):
        anchored = m.compile_title_pattern(
            "2024-04-08 Total Solar Eclipse ESID#*"
        )
        self.assertEqual(
            m.match_title(anchored, "2024-04-08 Total Solar Eclipse ESID#073"),
            "073",
        )
        # Anchored at the start: a prefixed copy does not match.
        self.assertIsNone(
            m.match_title(anchored, "Copy of 2024-04-08 Total Solar Eclipse ESID#073")
        )

    def test_literal_tail_after_final_star(self):
        tailed = m.compile_title_pattern("*ESID #* data")
        self.assertEqual(m.match_title(tailed, f"{_T}073 data"), "073")
        self.assertIsNone(m.match_title(tailed, f"{_T}073 audio"))

    def test_pattern_without_star_raises(self):
        with self.assertRaises(m.ReportError):
            m.compile_title_pattern("no wildcard here")


class TestAndFilters(unittest.TestCase):
    """--and-title-pattern: AND-based filters composing with the OR list."""

    _OR = [
        m.compile_title_pattern("*ESID #*"),
        m.compile_title_pattern("*ESID#*"),
        m.compile_title_pattern("*ESID No. *"),
    ]

    def test_user_example_composition(self):
        """The exact scenario from the requirements (3-digit ESIDs)."""
        filters = [m.compile_filter_pattern("*2024*")]
        self.assertEqual(
            m.title_in_scope(
                self._OR, filters, "2024-04-04 Partial Solar Eclipse ESID #406"
            ),
            "406",
        )
        self.assertEqual(
            m.title_in_scope(
                self._OR, filters, "2024-04-04 Total Solar Eclipse ESID#046"
            ),
            "046",
        )
        self.assertIsNone(
            m.title_in_scope(
                self._OR, filters, "2023-08-04 Annular Solar Eclipse ESID#046"
            )
        )

    def test_esid_no_dot_pattern(self):
        filters = [m.compile_filter_pattern("*2024*")]
        self.assertEqual(
            m.title_in_scope(
                self._OR, filters, "2024 recordings ESID No. 073"
            ),
            "073",
        )

    def test_all_and_filters_must_pass(self):
        filters = [
            m.compile_filter_pattern("*2024*"),
            m.compile_filter_pattern("*Total*"),
        ]
        self.assertEqual(
            m.title_in_scope(
                self._OR, filters, "2024-04-08 Total Solar Eclipse ESID #073"
            ),
            "073",
        )
        self.assertIsNone(
            m.title_in_scope(
                self._OR, filters, "2024-04-08 Partial Solar Eclipse ESID #073"
            )
        )

    def test_and_filter_alone_is_not_enough(self):
        # Matching every AND filter but no OR pattern → out of scope.
        filters = [m.compile_filter_pattern("*2024*")]
        self.assertIsNone(
            m.title_in_scope(self._OR, filters, "2024 site report, no esid")
        )

    def test_no_filters_means_or_only(self):
        self.assertEqual(
            m.title_in_scope(
                self._OR, [], "2023-10-14 Annular Solar Eclipse ESID #009"
            ),
            "009",
        )

    def test_filter_is_case_insensitive_and_literal(self):
        filters = [m.compile_filter_pattern("*total solar*")]
        self.assertEqual(
            m.title_in_scope(
                self._OR, filters, "2024-04-08 Total Solar Eclipse ESID #073"
            ),
            "073",
        )

    def test_records_from_hits_applies_filters(self):
        hits = [
            invenio_hit("1", "2024-04-08 Total Solar Eclipse ESID #001", True),
            invenio_hit("2", "2023-10-14 Annular Solar Eclipse ESID #002", True),
        ]
        records, excluded = m.records_from_hits(
            hits, "account", _WEB, self._OR,
            [m.compile_filter_pattern("*2024*")],
        )
        self.assertEqual([r.esid for r in records], ["001"])
        self.assertEqual(excluded, 1)


# --- pattern gating before validation ---------------------------------------

class TestRecordsFromHits(unittest.TestCase):
    def test_malformed_nonmatching_hit_is_ignored_not_validated(self):
        """The user requirement: no error checking on out-of-scope records.
        This hit has no id and no status fields — it would fail every
        strict check — but its title misses the pattern, so it must be
        silently excluded, never reported."""
        malformed_document = {"metadata": {"title": "Observer Role manual"}}
        records, excluded = m.records_from_hits(
            [malformed_document], "account", _WEB, _DEFAULT_RES
        )
        self.assertEqual(records, [])
        self.assertEqual(excluded, 1)

    def test_malformed_matching_hit_gets_error_row(self):
        """An in-scope hit that can't be trusted is still reported —
        with the anomaly in its error field instead of aborting."""
        no_status = {"id": "9", "metadata": {"title": f"{_T}009"}}
        records, excluded = m.records_from_hits(
            [no_status], "account", _WEB, _DEFAULT_RES
        )
        self.assertEqual(excluded, 0)
        self.assertEqual(len(records), 1)
        self.assertIsNone(records[0].is_draft)
        self.assertIn("cannot determine draft state", records[0].error)

    def test_mixed_hits_filter_and_parse(self):
        hits = [
            invenio_hit("1", f"{_T}001", True, doi="10.5281/zenodo.1"),
            legacy_hit("2", "A community document, no pattern"),
            invenio_hit("3", f"{_T}003", False),
        ]
        records, excluded = m.records_from_hits(
            hits, "account", _WEB, _DEFAULT_RES
        )
        self.assertEqual([r.esid for r in records], ["001", "003"])
        self.assertEqual(excluded, 1)


# --- record_from_hit / _draft_flag_from_hit --------------------------------

class TestRecordFromHit(unittest.TestCase):
    def test_invenio_draft_no_doi(self):
        rec = m.record_from_hit(
            invenio_hit("101", f"{_T}073", False), "account", _WEB, "073"
        )
        self.assertTrue(rec.is_draft)
        self.assertEqual(rec.doi, "")
        self.assertEqual(rec.esid, "073")
        self.assertEqual(rec.url, "https://zenodo.org/uploads/101")

    def test_invenio_published_with_doi_and_link(self):
        rec = m.record_from_hit(
            invenio_hit(
                "102", f"{_T}004", True,
                doi="10.5281/zenodo.102",
                self_html="https://zenodo.org/records/102",
            ),
            "account", _WEB, "004",
        )
        self.assertFalse(rec.is_draft)
        self.assertEqual(rec.doi, "10.5281/zenodo.102")
        self.assertEqual(rec.url, "https://zenodo.org/records/102")

    def test_legacy_shape(self):
        rec = m.record_from_hit(
            legacy_hit("103", f"{_T}007", doi="10.5281/zenodo.103"),
            "community", _WEB, "007",
        )
        self.assertFalse(rec.is_draft)
        self.assertEqual(rec.doi, "10.5281/zenodo.103")
        self.assertEqual(rec.url, "https://zenodo.org/records/103")

    def test_published_url_fallback(self):
        rec = m.record_from_hit(
            invenio_hit("104", f"{_T}010", True), "account", _WEB, "010"
        )
        self.assertEqual(rec.url, "https://zenodo.org/records/104")

    def test_clean_record_has_empty_error(self):
        rec = m.record_from_hit(
            invenio_hit("105", f"{_T}005", True), "account", _WEB, "005"
        )
        self.assertEqual(rec.error, "")

    def test_missing_both_status_fields_gets_error(self):
        rec = m.record_from_hit(
            {"id": "106", "metadata": {"title": f"{_T}011"}},
            "account", _WEB, "011",
        )
        self.assertIsNone(rec.is_draft)
        self.assertIn("cannot determine draft state", rec.error)
        # Draft-ness unknown → /records/ vs /uploads/ would be a guess.
        self.assertEqual(rec.url, "")

    def test_status_only_fallback(self):
        hit = {"id": "107", "status": "draft",
               "metadata": {"title": f"{_T}012"}}
        rec = m.record_from_hit(hit, "account", _WEB, "012")
        self.assertTrue(rec.is_draft)
        self.assertEqual(rec.error, "")

    def test_draft_in_community_listing_gets_error(self):
        rec = m.record_from_hit(
            invenio_hit("108", f"{_T}013", False), "community", _WEB, "013"
        )
        self.assertIsNone(rec.is_draft)
        self.assertIn("community listing", rec.error)

    def test_missing_record_id_gets_error(self):
        rec = m.record_from_hit(
            {"status": "draft", "title": f"{_T}014"},
            "account", _WEB, "014",
        )
        self.assertIn("no record id", rec.error)
        self.assertEqual(rec.url, "")


# --- pagination: deterministic short-page termination -----------------------

class TestFetchAllHitsVerified(unittest.TestCase):
    _PAGE_SIZE = 2

    def _fetch(self, responses, first_url="http://api/user/records?size=2"):
        """Run fetch_all_hits_verified against scripted responses,
        recording every requested URL."""
        it = iter(responses)
        urls = []

        def fake_get(**kwargs):
            urls.append(kwargs["url"])
            return next(it)

        with mock.patch.object(m, "_api_get_with_retry", side_effect=fake_get):
            hits = m.fetch_all_hits_verified(
                first_url, {}, "test", self._PAGE_SIZE
            )
        return hits, urls

    def test_short_first_page_is_the_end(self):
        hits, urls = self._fetch([
            page([invenio_hit("1", f"{_T}001", True)], total=1),
        ])
        self.assertEqual(len(hits), 1)
        self.assertEqual(len(urls), 1)  # no phantom second request

    def test_multipage_follows_next_link(self):
        hits, urls = self._fetch([
            page([invenio_hit("1", f"{_T}001", True),
                  invenio_hit("2", f"{_T}002", True)],
                 total=3, next_url="http://p2"),
            page([invenio_hit("3", f"{_T}003", True)], total=3),
        ])
        self.assertEqual([h["id"] for h in hits], ["1", "2", "3"])
        self.assertEqual(urls[1], "http://p2")

    def test_full_page_without_next_link_keeps_going(self):
        """The production bug: /user/records served exactly `size` hits
        with a matching total and NO next link while more records
        existed.  A full page must trigger an explicit page=2 request;
        only a short page ends the walk."""
        hits, urls = self._fetch([
            page([invenio_hit("1", f"{_T}001", True),
                  invenio_hit("2", f"{_T}002", True)], total=2),   # full, no next
            page([invenio_hit("3", f"{_T}003", True),
                  invenio_hit("4", f"{_T}004", True)], total=2),   # full, no next
            page([], total=2),                                     # empty: the end
        ])
        self.assertEqual([h["id"] for h in hits], ["1", "2", "3", "4"])
        self.assertEqual(len(urls), 3)
        self.assertIn("page=2", urls[1])
        self.assertIn("page=3", urls[2])
        self.assertIn("size=2", urls[1])  # size param preserved

    def test_fetched_more_than_reported_total_is_not_an_error(self):
        """Zenodo capping/under-reporting hits.total must not abort —
        the deterministic page walk is the completeness proof."""
        hits, _ = self._fetch([
            page([invenio_hit("1", f"{_T}001", True),
                  invenio_hit("2", f"{_T}002", True)], total=2),
            page([invenio_hit("3", f"{_T}003", True)], total=2),  # short: end
        ])
        self.assertEqual(len(hits), 3)

    def test_fetched_fewer_than_reported_total_raises(self):
        with self.assertRaises(m.ReportError):
            self._fetch([
                page([invenio_hit("1", f"{_T}001", True)], total=5),  # short
            ])

    def test_missing_total_is_tolerated(self):
        hits, _ = self._fetch([
            FakeResponse({"hits": {"hits": [invenio_hit("1", f"{_T}001", True)]}}),
        ])
        self.assertEqual(len(hits), 1)

    def test_elasticsearch_style_total(self):
        payload = {"hits": {"hits": [invenio_hit("1", f"{_T}001", True)],
                            "total": {"value": 1}}}
        hits, _ = self._fetch([FakeResponse(payload)])
        self.assertEqual(len(hits), 1)

    def test_duplicate_ids_across_pages_are_dropped(self):
        # Page 2 is raw-full (2 hits) even though one is a duplicate, so
        # the walk correctly continues; the empty page 3 ends it.
        hits, _ = self._fetch([
            page([invenio_hit("1", f"{_T}001", True),
                  invenio_hit("2", f"{_T}002", True)],
                 total=3, next_url="http://p2"),
            page([invenio_hit("2", f"{_T}002", True),
                  invenio_hit("3", f"{_T}003", True)],
                 total=3),
            page([], total=3),
        ])
        self.assertEqual([h["id"] for h in hits], ["1", "2", "3"])

    def test_runaway_pagination_is_hard_error(self):
        def full(n):
            return page(
                [invenio_hit(str(2 * n), f"{_T}001", True),
                 invenio_hit(str(2 * n + 1), f"{_T}002", True)],
                total=999, next_url="http://again",
            )
        with mock.patch.object(m, "_MAX_PAGES_PER_SOURCE", 2):
            with self.assertRaises(m.ReportError):
                self._fetch([full(1), full(2), full(3)])


# --- merge ------------------------------------------------------------------

class TestMergeRecords(unittest.TestCase):
    def _rec(self, record_id, source, is_draft=False):
        return m.EsidRecord(
            record_id=record_id, title=f"{_T}00{record_id}",
            esid=f"00{record_id}", doi="", is_draft=is_draft,
            url="u", source=source,
        )

    def test_account_wins_on_overlap(self):
        merged = m.merge_records(
            [self._rec("1", "community")],
            [self._rec("1", "account"), self._rec("2", "account", is_draft=True)],
        )
        by_id = {r.record_id: r for r in merged}
        self.assertEqual(by_id["1"].source, "community+account")
        self.assertTrue(by_id["2"].is_draft)
        self.assertEqual(len(merged), 2)

    def test_community_only_preserved(self):
        merged = m.merge_records([self._rec("9", "community")], [])
        self.assertEqual(merged[0].source, "community")
        self.assertFalse(merged[0].is_draft)

    def test_conflict_becomes_error_row(self):
        """The published-with-open-edit-draft case (record in the
        community listing AND draft-flagged in the account listing) is
        reported with an unknown draft state, not aborted on."""
        merged = m.merge_records(
            [self._rec("1", "community")],
            [self._rec("1", "account", is_draft=True)],
        )
        self.assertEqual(len(merged), 1)
        rec = merged[0]
        self.assertIsNone(rec.is_draft)
        self.assertEqual(rec.source, "community+account")
        self.assertIn("open (unpublished) edit draft", rec.error)


# --- rows + CSV --------------------------------------------------------------

class TestBuildRowsAndCsv(unittest.TestCase):
    def _records(self):
        return [
            m.EsidRecord("300", f"{_T}010", "010", "", True,
                         "https://zenodo.org/uploads/300", "account"),
            m.EsidRecord("100", f"{_T}002", "002", "10.5281/zenodo.100", False,
                         "https://zenodo.org/records/100", "community+account"),
            m.EsidRecord("200", f"{_T}002", "002", "", True,
                         "https://zenodo.org/uploads/200", "account"),
        ]

    def test_rows_sorted_and_flagged(self):
        rows = m.build_rows(self._records())
        self.assertEqual(
            [
                (r["ESID#"], r["Title"], r["Zenodo URL"], r["Draft (y/n)"],
                 r["DOI"], r["ERROR?"])
                for r in rows
            ],
            [
                ("002", f"{_T}002", "https://zenodo.org/records/100", "n",
                 "10.5281/zenodo.100", ""),
                ("002", f"{_T}002", "https://zenodo.org/uploads/200", "y",
                 "", ""),
                ("010", f"{_T}010", "https://zenodo.org/uploads/300", "y",
                 "", ""),
            ],
        )

    def test_csv_contract(self):
        rows = m.build_rows(self._records())
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.csv"
            m.write_report(rows, out)
            with out.open(newline="", encoding="utf-8") as fh:
                content = list(csv.reader(fh))
        self.assertEqual(
            content[0],
            ["ESID#", "Title", "Zenodo URL", "Draft (y/n)", "DOI", "ERROR?"],
        )
        self.assertEqual(len(content), 1 + 3)

    def test_anomalous_record_keeps_its_row(self):
        rec = m.EsidRecord(
            "1", f"{_T}001", "001", "", None, "", "account",
            error="cannot determine draft state",
        )
        rows = m.build_rows([rec])
        self.assertEqual(rows[0]["Draft (y/n)"], "?")
        self.assertEqual(rows[0]["Zenodo URL"], "")
        self.assertIn("cannot determine draft state", rows[0]["ERROR?"])

    def test_empty_url_without_error_gets_error_cell(self):
        rec = m.EsidRecord("1", f"{_T}001", "001", "", True, "", "account")
        rows = m.build_rows([rec])
        self.assertEqual(rows[0]["ERROR?"], "No URL available for this record.")

    def test_unknown_draft_without_error_gets_error_cell(self):
        rec = m.EsidRecord(
            "1", f"{_T}001", "001", "", None, "u", "account"
        )
        rows = m.build_rows([rec])
        self.assertEqual(
            rows[0]["ERROR?"], "Draft state could not be determined."
        )


if __name__ == "__main__":
    unittest.main()
