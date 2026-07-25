#!/usr/bin/env python3
"""Inventory Zenodo ESID data records matching a title pattern — CSV report.

PURPOSE
=======
Scan the project's Zenodo presence via the API and write one CSV row per
record whose title matches ANY of the ESID data-record patterns
(``--title-pattern``, repeatable; defaults to ``*ESID #*`` OR ``*ESID#*``
— covering both title forms observed on production Zenodo.  In each
pattern the LAST ``*`` stands for the ESID (3-digit number plus any
suffix), any earlier
``*`` matches arbitrary text such as the date/label prefix):

    ESID#, Title, Zenodo URL, Draft (y/n), DOI, ERROR?

Records whose titles do NOT match the pattern (documents, manuals, other
collections) are excluded before any validation — no error checking is
performed on them, so an oddly-serialized non-data record can never
abort a run it has no business being in.

Sources scanned (``--scope``, default ``both``):

  * **account** — ``/api/user/records``: everything owned by the
    uploading Zenodo account, INCLUDING unpublished drafts.  This is the
    only source that can see drafts, so it requires the API token from
    ``Resources/set_env.sh``.
  * **community** — the public community listing: records ACCEPTED into
    the project community, including ones owned by other accounts.
    Contains no drafts by definition.

PATTERN RULES
=============
Two kinds of pattern work together; a record is reported when its title
matches **at least one** ``--title-pattern`` (OR) **and every**
``--and-title-pattern`` (AND).

``--title-pattern`` (OR, repeatable)
    The LAST ``*`` stands for the ESID — **exactly three digits** plus
    any suffix (suffixed ESIDs render with spaces in titles, e.g.
    ``ESID#122 Part 1 of 2``, and are reported in canonical underscored
    form ``122_Part_1_of_2``); any earlier ``*`` matches arbitrary
    text; everything else is literal.

``--and-title-pattern`` (AND, repeatable, optional)
    A pure filter: every ``*`` matches arbitrary text, nothing is
    captured.  ``*2024*`` means "title contains 2024".

All patterns are matched case-insensitively against the START of each
title, and text after the pattern is always allowed.

ACCURACY GUARANTEES (this report must never be silently wrong)
================================================================
The whole point of this tool is a spreadsheet someone can trust, so it
fails loudly rather than guess:

  * **Deterministic pagination** — a listing is only considered complete
    when a page returns fewer hits than the page size.  A full page is
    never trusted as the end, even when Zenodo omits the next-page link
    and reports a "matching" total (observed on ``/api/user/records``,
    where trusting those capped a scan at 100 records).
  * **Count cross-check** — fetching fewer records than the API's own
    reported total (``hits.total``) aborts the run; duplicate hits
    across pages are dropped with a warning.
  * **Pagination runaway is a hard error**, not a warning — a truncated
    scan must never masquerade as a complete one.
  * **Per-record anomalies never guess and never hide** — a hit whose
    draft state cannot be determined (unknown serialization, or the
    contradictory published-in-community-but-draft-in-account case) is
    still reported: its Draft cell shows ``?``, the untrusted cells stay
    empty, and the anomaly message appears both on screen and in the
    row's ``ERROR?`` column.  The run exits ``1`` so scripts notice.
  * **Listing-level failures still abort with NO CSV** (exit ``2``) —
    pagination runaway, a fetched count below the API's reported total,
    or auth/API errors mean the report as a whole cannot be trusted,
    and a partial file must never be mistaken for a complete inventory.
  * DOIs are read from the record (``pids.doi.identifier`` or the legacy
    top-level ``doi``), never derived or invented; a DOI-less draft gets
    an empty cell.

Reuses the retry and dual-serialization patterns proven against
production Zenodo in ``find_duplicate_records.py`` (July 2026).

NOTE: the default listings return the latest version per concept plus
all drafts — historical published versions are not enumerated.  That is
the operationally relevant view (one row per live record).

EXAMPLES
========
Full inventory, both sources, default patterns (needs the API token)::

    source Resources/set_env.sh
    python Resources/esid_record_report.py --output esid_records.csv

Every 2024 record across all known "ESID#" title variations — matches
"2024-04-04 Partial Solar Eclipse ESID #406" and "2024-04-08 Total
Solar Eclipse ESID#046", but not "2023-08-04 Annular ... ESID#046"::

    python Resources/esid_record_report.py \\
        --title-pattern "*Solar Eclipse ESID #*" \\
        --title-pattern "*Solar Eclipse ESID#*" \\
        --title-pattern "*Solar Eclipse ESID No. *" \\
        --and-title-pattern "*2024*" \\
        --output esid_2024_records.csv

Published-only quick look, no token needed::

    python Resources/esid_record_report.py --scope community

Narrow to one eclipse dataset with an anchored single pattern::

    python Resources/esid_record_report.py \\
        --title-pattern "2024-04-08 Total Solar Eclipse ESID#*"

USAGE
=====
::

    source Resources/set_env.sh   # token — required for account/both
    python Resources/esid_record_report.py
        [--title-pattern PATTERN ...]      # OR;  default: "*ESID #*", "*ESID#*"
        [--and-title-pattern PATTERN ...]  # AND; default: none
        [--scope both|account|community]   # default: both
        [--community-id ID]
        [--project-config Resources/project_config.json]
        [--output PATH]     # default: Records/YYYYMMDD_HHMMSS_esid_record_report.csv
        [--verbose]

ENVIRONMENT VARIABLES
=====================
    INVENIO_RDM_ACCESS_TOKEN: Zenodo API bearer token for authentication;
        required for the account scope (which reads your account's own
        records, drafts included).
    INVENIO_RDM_BASE_URL: Zenodo API base URL; defaults to
        https://zenodo.org/api/ when unset.

EXIT CODES
==========
* ``0`` — complete, verified report written; every ``ERROR?`` cell empty
* ``1`` — complete report written, but at least one row carries an
  ``ERROR?`` message that needs review
* ``2`` — listing-level failure (auth, API, pagination) — no CSV written
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import azus_common

# The uploader module lives at the project root one level up.  Make it
# importable before the import below.
_PROJECT_ROOT = azus_common.PROJECT_ROOT
sys.path.insert(0, str(_PROJECT_ROOT))

from standalone_uploader import (  # noqa: E402
    _api_get_with_retry,
    _auth_headers,
    get_credentials_from_env,
)

logger = logging.getLogger("azus.esid_report")

_DEFAULT_PROJECT_CONFIG = _PROJECT_ROOT / "Resources" / "project_config.json"
_DEFAULT_BASE_URL = "https://zenodo.org/api/"

# The shapes of an ESID data-record title.  Wildcard semantics: the LAST
# "*" stands for the ESID (3-digit number + optional suffix); any
# earlier "*" matches
# arbitrary text.  A record is in scope when its title matches ANY of
# the patterns — the defaults cover both title forms observed on
# production Zenodo: "ESID #NNN" (with space) and "ESID#NNN" (without).
# Only matching records are inventoried (or even validated) — everything
# else in the account/community (documents, manuals) is out of scope.
_DEFAULT_TITLE_PATTERNS = ("*ESID #*", "*ESID#*")

# Runaway guard: 200 pages x 100 hits = 20,000 records per source, far
# beyond anything this project will produce.  Unlike the duplicate
# checker, hitting this guard is a HARD ERROR here — a truncated scan
# must never look like a complete inventory.
_MAX_PAGES_PER_SOURCE = 200

# Zenodo rejects size > 25 on unauthenticated requests.
_PAGE_SIZE_AUTHENTICATED = 100
_PAGE_SIZE_ANONYMOUS = 25

_CSV_COLUMNS = [
    "ESID#", "Title", "Zenodo URL", "Draft (y/n)", "DOI", "Upload Date",
    "Last Updated", "ERROR?",
]


class ReportError(Exception):
    """Any condition that makes the report untrustworthy — aborts the run."""


@dataclass
class EsidRecord:
    """The fields of one Zenodo record hit that this report cares about.

    Attributes:
        record_id: Zenodo record identifier (e.g. ``"14888071"``).
        title: The record title exactly as it appears on Zenodo.
        esid: Canonical ESID captured from the title (zero-padded
            3-digit number plus any suffix, e.g. ``122_Part_1_of_2``).
        doi: Assigned DOI, or ``""`` when none has been reserved/minted.
        is_draft: True for an unpublished draft, False for published,
            None when the state could not be determined (see ``error``).
        url: Web page for the record (``/records/`` or ``/uploads/``),
            or ``""`` when it could not be determined.
        source: ``"community"``, ``"account"``, or ``"community+account"``.
        error: ``""`` for a clean record; otherwise the anomaly
            message(s) that go into the CSV's ``ERROR?`` column (and
            would previously have aborted the whole report).
        upload_date: The record's Zenodo ``created`` date (``YYYY-MM-DD``,
            when the draft/record was first created on Zenodo — i.e. when
            it was uploaded), or ``""`` when the API hit carries no
            ``created`` field.
        last_updated: The record's Zenodo ``updated`` date (``YYYY-MM-DD``,
            last modification), or ``""`` when the hit carries no
            ``updated`` field.
    """

    record_id: str
    title: str
    esid: str
    doi: str
    is_draft: Optional[bool]
    url: str
    source: str
    error: str = ""
    upload_date: str = ""
    last_updated: str = ""

    def add_error(self, message: str) -> None:
        """Append an anomaly message to this record's ``error`` field.

        Args:
            message: The anomaly text to record; joined to any existing
                message with `` | ``.
        """
        self.error = f"{self.error} | {message}" if self.error else message


def compile_title_pattern(pattern: str) -> "re.Pattern[str]":
    """Compile an OR pattern (``--title-pattern``) into a regex.

    Wildcard semantics: the LAST ``*`` stands for the ESID — the
    3-digit number plus, in a title, any display-form suffix (suffixed
    ESIDs like ``122_Part_1_of_2`` render with spaces in titles:
    ``ESID#122 Part 1 of 2``, so everything word-like after the digits
    is treated as the suffix).  Any earlier ``*`` matches arbitrary
    text (e.g. the leading ``*`` in the default ``*ESID #*`` lets any
    date/label prefix through).  Every other character is matched
    literally.  Matching is case-insensitive, anchored at the START of
    the title, and tolerant of trailing text after the pattern.
    ``(?!\\d)`` stops a 4+-digit number from passing as a 3-digit ESID
    plus trailing digits.

    Args:
        pattern: User-facing pattern, e.g. ``"*ESID #*"``.

    Returns:
        Compiled case-insensitive regex whose group 1 is the ESID in
        its title (display) form — :func:`match_title` converts it to
        the canonical underscored form.

    Raises:
        ReportError: If the pattern contains no ``*``.
    """
    if "*" not in pattern:
        raise ReportError(
            f"--title-pattern must contain a '*' (the ESID "
            f"placeholder); got {pattern!r}."
        )
    head, _, tail = pattern.rpartition("*")
    literal_parts = head.split("*")
    regex = (
        r".*?".join(re.escape(part) for part in literal_parts)
        + r"(\d{3}(?!\d)[A-Za-z0-9_ ]*)"
        + re.escape(tail)
    )
    return re.compile(regex, re.IGNORECASE)


def compile_filter_pattern(pattern: str) -> "re.Pattern[str]":
    """Compile an AND filter (``--and-title-pattern``) into a regex.

    Unlike the OR patterns, filters capture nothing: every ``*`` simply
    matches arbitrary text and everything else is literal.  Matching is
    case-insensitive, anchored at the START of the title, and tolerant
    of trailing text — so ``*2024*`` means "title contains 2024".

    Args:
        pattern: User-facing filter, e.g. ``"*2024*"``.

    Returns:
        Compiled case-insensitive regex (no capture groups).
    """
    regex = r".*?".join(re.escape(part) for part in pattern.split("*"))
    return re.compile(regex, re.IGNORECASE)


def _title_from_hit(hit: Dict) -> str:
    """Title in either serialization: ``metadata.title`` or top-level.

    Args:
        hit: One raw hit dict from a Zenodo listing.

    Returns:
        The record title, or ``""`` when neither field is present.
    """
    return (hit.get("metadata") or {}).get("title", "") or hit.get("title", "") or ""


def match_title(title_re: "re.Pattern[str]", title: str) -> Optional[str]:
    """Return the canonical ESID if the title matches the pattern.

    The capture is the ESID's display form (suffix words separated by
    spaces); it is converted to the canonical underscored form for the
    report (``ESID#122 Part 1 of 2`` → ``122_Part_1_of_2``).

    Args:
        title_re: A regex from :func:`compile_title_pattern`.
        title: Record title to test.

    Returns:
        The canonical ESID string (zero-padded 3 digits plus any
        suffix), or None on no match.
    """
    m = title_re.match(title)
    if m is None:
        return None
    capture = m.group(1).strip()
    try:
        return azus_common.normalize_esid(capture)
    except ValueError:
        # Defensive: a capture the grammar rejects falls back to the
        # bare 3-digit number rather than aborting the whole report.
        return capture[:3]


def match_title_any(
    title_res: List["re.Pattern[str]"], title: str
) -> Optional[str]:
    """Return the canonical ESID from the first OR pattern that matches.

    Args:
        title_res: Regexes from :func:`compile_title_pattern`.
        title: Record title to test.

    Returns:
        The canonical ESID string, or None if no pattern matches.
    """
    for title_re in title_res:
        esid = match_title(title_re, title)
        if esid is not None:
            return esid
    return None


def title_in_scope(
    title_res: List["re.Pattern[str]"],
    filter_res: List["re.Pattern[str]"],
    title: str,
) -> Optional[str]:
    """Combine the OR patterns with the AND filters.

    A title is in scope when it matches AT LEAST ONE of ``title_res``
    (which also yields the ESID) AND EVERY filter in ``filter_res``.

    Args:
        title_res: OR regexes from :func:`compile_title_pattern`.
        filter_res: AND regexes from :func:`compile_filter_pattern`
            (empty list = no AND constraints).
        title: Record title to test.

    Returns:
        The canonical ESID, or None when the title is out of scope.
    """
    esid = match_title_any(title_res, title)
    if esid is None:
        return None
    for filter_re in filter_res:
        if filter_re.match(title) is None:
            return None
    return esid


def _draft_flag_from_hit(
    hit: Dict, source: str
) -> "Tuple[Optional[bool], str]":
    """Determine draft-ness of a hit, never guessing on ambiguity.

    InvenioRDM shape (``/api/user/records``) carries ``is_published``;
    the legacy community-listing shape carries ``status``.  A hit with
    NEITHER cannot be classified, and guessing would put a wrong y/n in
    a report people rely on — so the state comes back as unknown
    (``None``) with an explanatory message for the ``ERROR?`` column.

    Community hits are additionally required to be published: that
    listing only contains records accepted into the community, so a
    draft appearing there is contradictory and also reports as unknown.

    Args:
        hit: One raw hit dict from a Zenodo listing.
        source: ``"community"`` or ``"account"`` (for error messages
            and the community-must-be-published rule).

    Returns:
        Tuple of ``(is_draft, error_message)``: ``(True/False, "")``
        when the state is trustworthy, ``(None, <message>)`` when not.
    """
    status = str(hit.get("status", ""))
    if "is_published" in hit:
        is_draft = not bool(hit["is_published"])
    elif status:
        is_draft = status != "published"
    else:
        return None, (
            f"Record {hit.get('id')!r} from {source} listing has neither "
            "'is_published' nor 'status' — cannot determine draft state. "
            "Zenodo's serialization may have changed; refusing to guess."
        )
    if source == "community" and is_draft:
        return None, (
            f"Record {hit.get('id')!r} appears in the community listing "
            "but classifies as a draft — the community listing must only "
            "contain accepted (published) records."
        )
    return is_draft, ""


def record_from_hit(
    hit: Dict, source: str, web_base: str, esid: str
) -> EsidRecord:
    """Map one pattern-matched API hit to an EsidRecord (both serializations).

    Only called for hits whose title already matched the ESID data-record
    pattern — the strict fail-closed checks in here apply exclusively to
    records this report actually cares about.

    Field locations verified against production Zenodo (July 2026) in
    find_duplicate_records.py: DOI from ``pids.doi.identifier`` or legacy
    top-level ``doi``; URL from ``links.self_html``/``links.html`` with a
    constructed fallback matching the ``upload_state.json`` convention
    (drafts live under /uploads/, published under /records/).

    Anomalies that would previously have aborted the whole report (no
    record id, undeterminable draft state) are recorded in the returned
    record's ``error`` field instead — they surface in the CSV's
    ``ERROR?`` column, with ``?``/empty cells where a value cannot be
    trusted.

    Args:
        hit: One raw hit dict from a Zenodo listing.
        source: ``"community"`` or ``"account"``.
        web_base: Web root for constructed URLs (e.g.
            ``"https://zenodo.org/"``).
        esid: The canonical ESID already captured from the title.

    Returns:
        The populated :class:`EsidRecord` (``error`` non-empty when the
        hit is anomalous).
    """
    title = _title_from_hit(hit)
    record = EsidRecord(
        record_id=str(hit.get("id", "")),
        title=title,
        esid=esid,
        doi="",
        is_draft=None,
        url="",
        source=source,
    )
    if not record.record_id:
        record.add_error(
            f"A hit from the {source} listing has no record id — "
            "row values cannot be fully trusted."
        )
    is_draft, draft_error = _draft_flag_from_hit(hit, source)
    record.is_draft = is_draft
    if draft_error:
        record.add_error(draft_error)
    doi = (
        ((hit.get("pids") or {}).get("doi") or {}).get("identifier")
        or hit.get("doi")
        or ""
    )
    record.doi = str(doi)
    # Upload date = the record's Zenodo "created" timestamp (draft/record
    # creation ≈ when it was uploaded), reduced to its date portion.
    # Present on both the account and community serializations; absent →
    # left blank rather than guessed.
    record.upload_date = str(hit.get("created") or "").split("T", 1)[0]
    # Last updated = the record's "updated" timestamp (date portion).
    record.last_updated = str(hit.get("updated") or "").split("T", 1)[0]
    links = hit.get("links") or {}
    record.url = str(links.get("self_html") or links.get("html") or "")
    if not record.url and record.record_id:
        if is_draft is True:
            record.url = f"{web_base}uploads/{record.record_id}"
        elif is_draft is False:
            record.url = f"{web_base}records/{record.record_id}"
        # is_draft None: /records/ vs /uploads/ would be a guess — leave
        # the URL empty; the ERROR? cell already explains why.
    if not record.url and not record.error:
        record.add_error("No URL available for this record.")
    return record


def records_from_hits(
    hits: List[Dict],
    source: str,
    web_base: str,
    title_res: List["re.Pattern[str]"],
    filter_res: Optional[List["re.Pattern[str]"]] = None,
) -> "Tuple[List[EsidRecord], int]":
    """Filter hits by the title patterns, then strictly parse the matches.

    A hit is in scope when its title matches ANY of the OR patterns
    (``title_res``) AND every AND filter (``filter_res``).  Out-of-scope
    hits (documents, manuals, other eclipses) are counted and skipped
    WITHOUT any validation — an oddly-serialized out-of-scope record
    must never abort the report.

    An in-scope hit that cannot be parsed trustworthily is still
    reported: its anomaly message is logged to the screen and carried
    into the row's ``ERROR?`` column (it no longer aborts the run).

    Args:
        hits: Raw hit dicts from :func:`fetch_all_hits_verified`.
        source: ``"community"`` or ``"account"``.
        web_base: Web root for constructed URLs.
        title_res: OR regexes from :func:`compile_title_pattern`.
        filter_res: Optional AND regexes from
            :func:`compile_filter_pattern`.

    Returns:
        Tuple of ``(records, excluded_count)``.
    """
    records: List[EsidRecord] = []
    excluded = 0
    for hit in hits:
        title = _title_from_hit(hit)
        esid = title_in_scope(title_res, filter_res or [], title)
        if esid is None:
            excluded += 1
            logger.debug(
                "  (pattern miss, ignored) %s: %r", source, title
            )
            continue
        record = record_from_hit(hit, source, web_base, esid)
        if record.error:
            logger.error(
                "ESID %s (record %s): %s — reported in the ERROR? column.",
                record.esid, record.record_id or "?", record.error,
            )
        records.append(record)
    if excluded:
        logger.info(
            "%s: %d record(s) did not match any title pattern and were "
            "ignored (use --verbose to list them).",
            source, excluded,
        )
    return records, excluded


def _reported_total(payload: Dict) -> Optional[int]:
    """Extract the listing's self-reported total hit count.

    InvenioRDM serves ``hits.total`` as an int; some Elasticsearch-styled
    responses use ``{"value": N}``.  The total is used as a cross-check
    only — completeness is proven by paging until a short page (see
    :func:`fetch_all_hits_verified`).

    Args:
        payload: One decoded JSON page from a listing response.

    Returns:
        The reported total as an int, or None when the response carries
        no ``hits.total``.
    """
    total = (payload.get("hits") or {}).get("total")
    if isinstance(total, dict):
        total = total.get("value")
    return int(total) if isinstance(total, int) else None


def _url_with_page(url: str, page: int) -> str:
    """Return ``url`` with its ``page`` query parameter set to ``page``.

    Args:
        url: The listing URL to modify.
        page: The 1-based page number to request.

    Returns:
        The URL with its ``page`` query parameter set (all other query
        parameters preserved).
    """
    parts = urlsplit(url)
    params = dict(parse_qsl(parts.query))
    params["page"] = str(page)
    return urlunsplit(parts._replace(query=urlencode(params)))


def fetch_all_hits_verified(
    first_url: str, headers: Dict[str, str], label: str, page_size: int
) -> List[Dict]:
    """Fetch every hit from a paginated listing, verifying completeness.

    Completeness is proven by DETERMINISTIC TERMINATION: the fetch only
    stops when a page comes back with fewer than ``page_size`` hits.  A
    full page is never trusted as the end — even when the response has
    no ``links.next`` and even when ``hits.total`` claims we have
    everything.  This matters on production Zenodo: ``/api/user/records``
    has been observed to serve exactly ``size`` hits with a matching
    "total" and no next link while MORE records exist, which silently
    capped the report at 100 rows.  When that happens, the next page is
    requested explicitly via the ``page`` query parameter.

    Cross-checks on top of the deterministic walk:

    * fetched fewer records than the API's reported total → abort
      (records were dropped);
    * duplicate record ids across pages (a listing shifting mid-scan)
      are dropped with a warning, so one record can never appear twice;
    * more records than the reported total is only a warning — the walk
      itself proves completeness; the API's total was capped or stale.

    Args:
        first_url: Page-1 listing URL (must already carry ``size=``).
        headers: Auth headers (empty dict for anonymous requests).
        label: Human-readable source name for log/error messages.
        page_size: The ``size`` value in ``first_url`` — a page with
            fewer hits than this is the end of the listing.

    Returns:
        Every hit dict from the listing, in page order, deduplicated.

    Raises:
        ReportError: On pagination runaway or a fetched count below the
            API's reported total.
    """
    hits: List[Dict] = []
    seen_ids: set = set()
    duplicates = 0
    expected_total: Optional[int] = None
    url: str = first_url
    page = 0
    while True:
        page += 1
        if page > _MAX_PAGES_PER_SOURCE:
            raise ReportError(
                f"{label}: exceeded {_MAX_PAGES_PER_SOURCE} pages "
                f"({len(hits)} records fetched so far) — refusing to emit "
                "a possibly truncated report. Raise _MAX_PAGES_PER_SOURCE "
                "if the collection is genuinely this large."
            )
        response = _api_get_with_retry(
            url=url, auth_headers=headers, label=f"{label} page {page}",
        )
        payload = response.json()
        if expected_total is None:
            expected_total = _reported_total(payload)
        page_hits = (payload.get("hits") or {}).get("hits", []) or []
        for hit in page_hits:
            hit_id = str(hit.get("id", ""))
            if hit_id and hit_id in seen_ids:
                duplicates += 1
                continue
            if hit_id:
                seen_ids.add(hit_id)
            hits.append(hit)

        if len(page_hits) < page_size:
            break  # short (or empty) page — the definitive end
        # Full page: NOT proof of another page, but never assume the end.
        next_url = (payload.get("links") or {}).get("next")
        url = next_url if next_url else _url_with_page(first_url, page + 1)

    if duplicates:
        logger.warning(
            "%s: %d duplicate hit(s) across pages were dropped — the "
            "listing shifted while being scanned. Re-run to be safe.",
            label, duplicates,
        )
    if expected_total is None:
        logger.warning(
            "%s: the API response carried no hits.total — completeness "
            "rests on the page walk alone (terminated on a short page).",
            label,
        )
    elif len(hits) < expected_total:
        raise ReportError(
            f"{label}: fetched {len(hits)} record(s) but the API reported "
            f"a total of {expected_total} — records were dropped. "
            "Re-run the report."
        )
    elif len(hits) > expected_total:
        logger.warning(
            "%s: fetched %d record(s) but the API reported a total of "
            "only %d — Zenodo capped or under-reported the total. The "
            "page walk terminated deterministically, so the fetched set "
            "is complete.",
            label, len(hits), expected_total,
        )
    logger.info(
        "%s: fetched %d record(s) across %d page(s), ending on a short "
        "page — listing exhausted.",
        label, len(hits), page,
    )
    return hits


def merge_records(
    community: List[EsidRecord], account: List[EsidRecord]
) -> List[EsidRecord]:
    """Union the two sources by record id; account data wins.

    The account listing carries the authoritative draft flag, so on
    overlap its record is kept (tagged ``community+account``).  An
    overlap where the account calls the record a draft is contradictory
    (the community listing has no drafts — typically a published record
    with an open edit draft): the record is reported with an unknown
    draft state (``?``) and the explanation in its ``ERROR?`` cell.

    Args:
        community: Records parsed from the community listing.
        account: Records parsed from the account listing.

    Returns:
        The merged record list (one entry per record id).
    """
    merged: Dict[str, EsidRecord] = {r.record_id: r for r in community}
    for rec in account:
        if rec.record_id in merged and rec.is_draft:
            message = (
                f"Record {rec.record_id} is in the community listing but "
                "the account listing calls it a draft — likely a "
                "published record with an open (unpublished) edit draft; "
                "verify on Zenodo."
            )
            logger.error(
                "ESID %s (record %s): %s — reported in the ERROR? column.",
                rec.esid, rec.record_id, message,
            )
            rec.is_draft = None
            rec.add_error(message)
        if rec.record_id in merged:
            rec.source = "community+account"
        merged[rec.record_id] = rec
    return list(merged.values())


def build_rows(records: List[EsidRecord]) -> List[Dict[str, str]]:
    """Shape the pattern-matched records into CSV rows, sorted.

    One row per record — two records sharing an ESID both appear, which
    is exactly how stray duplicates stay visible.  An anomalous record
    keeps its row: unknown draft state shows as ``?`` and the anomaly
    message fills the ``ERROR?`` column.  Cells are still self-checked;
    a cell that fails without an explaining ERROR? entry gets one
    (internal invariants — a malformed ESID or empty title cannot come
    from API data — still raise, since they mean a code bug).

    Args:
        records: Merged, in-scope records.

    Returns:
        Row dicts keyed by ``_CSV_COLUMNS``, sorted by ESID then
        record id.

    Raises:
        ReportError: If an internal invariant is broken (code bug),
            never for API-data anomalies.
    """

    def sort_key(rec: EsidRecord):
        """Order by ESID, then numeric record id, then raw id string."""
        rid = rec.record_id
        return (azus_common.esid_sort_key(rec.esid),
                int(rid) if rid.isdigit() else 0, rid)

    draft_cell = {True: "y", False: "n", None: "?"}
    rows = [
        {
            "ESID#": rec.esid,
            "Title": rec.title,
            "Zenodo URL": rec.url,
            "Draft (y/n)": draft_cell[rec.is_draft],
            "DOI": rec.doi,
            "Upload Date": rec.upload_date,
            "Last Updated": rec.last_updated,
            "ERROR?": rec.error,
        }
        for rec in sorted(records, key=sort_key)
    ]

    # Runtime self-check on the exact cells being shipped — belt and
    # suspenders for a report whose whole purpose is being correct.
    # ESID and title are produced by the pattern match itself, so a bad
    # one is a code bug (raise); a missing URL or unknown draft state is
    # an API-data anomaly and must be explained in the ERROR? cell.
    for row in rows:
        if not re.fullmatch(r"\d{3}(?:[A-Za-z_][A-Za-z0-9_]*)?", row["ESID#"]):
            raise ReportError(f"Self-check failed: malformed ESID {row!r}")
        if not row["Title"]:
            raise ReportError(f"Self-check failed: empty title {row!r}")
        if row["Draft (y/n)"] == "?" and not row["ERROR?"]:
            row["ERROR?"] = "Draft state could not be determined."
        if not row["Zenodo URL"] and not row["ERROR?"]:
            row["ERROR?"] = "No URL available for this record."
    return rows


def write_report(rows: List[Dict[str, str]], output_path: Path) -> None:
    """Write the verified rows to the CSV.

    Args:
        rows: Self-checked row dicts from :func:`build_rows`.
        output_path: Destination CSV path (overwritten if present).  Its
            parent directory is created if missing (e.g. the default
            ``Records/`` folder on a fresh checkout).
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    logger.info("Report written: %s (%d row(s))", output_path, len(rows))


def _load_community_id(project_config_path: Path) -> str:
    """Read ``community_id`` from project_config.json, or exit 2.

    Args:
        project_config_path: Path to the project_config.json file.

    Returns:
        The non-empty ``community_id`` string from the config.
    """
    try:
        config = json.loads(project_config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Could not read %s: %s", project_config_path, exc)
        sys.exit(2)
    community_id = str(config.get("community_id") or "").strip()
    if not community_id:
        logger.error(
            "No community_id in %s — pass --community-id explicitly.",
            project_config_path,
        )
        sys.exit(2)
    return community_id


def main() -> None:
    """Command-line entry point.  See the module docstring for details."""
    parser = argparse.ArgumentParser(
        description=(
            "Inventory Zenodo ESID data records matching title patterns — "
            "one CSV row per record: ESID#, Title, Zenodo URL, "
            "Draft (y/n), DOI, ERROR?. Read-only. Per-record anomalies "
            "are reported in the ERROR? column (exit 1); listing-level "
            "failures abort with no CSV (exit 2)."
        ),
    )
    parser.add_argument(
        "--title-pattern", action="append", default=None, metavar="PATTERN",
        help=(
            "Only records whose title matches a pattern are reported "
            "(or even validated). May be given multiple times — a title "
            "matching ANY pattern is in scope. The LAST '*' of each "
            "pattern stands for the ESID (3-digit number + optional suffix); "
            "any earlier '*' "
            "matches arbitrary text; everything else is literal. Matched "
            "case-insensitively against the start of the title. "
            f"Default: {' OR '.join(repr(p) for p in _DEFAULT_TITLE_PATTERNS)}"
        ),
    )
    parser.add_argument(
        "--and-title-pattern", action="append", default=None,
        metavar="PATTERN",
        help=(
            "Additional AND-based filter(s). May be given multiple times "
            "— a title must match EVERY one of these (on top of matching "
            "at least one --title-pattern) to be reported. Here '*' just "
            "matches arbitrary text (no ESID capture); everything else "
            "is literal, case-insensitive, matched from the start of the "
            "title with trailing text allowed. Example: --and-title-"
            "pattern '*2024*' keeps only titles containing 2024. "
            "Default: no AND filters."
        ),
    )
    parser.add_argument(
        "--scope", choices=("both", "community", "account"), default="both",
        help=(
            "Where to look (default: both). 'account' = everything owned "
            "by your Zenodo account INCLUDING drafts (needs the API token "
            "from Resources/set_env.sh). 'community' = records accepted "
            "into the project community (public, tokenless, no drafts, "
            "may include other owners). 'both' = union."
        ),
    )
    parser.add_argument(
        "--community-id", default=None, metavar="ID",
        help=(
            "Zenodo community UUID or slug (default: community_id from "
            "the project config)."
        ),
    )
    parser.add_argument(
        "--project-config", default=str(_DEFAULT_PROJECT_CONFIG),
        metavar="PATH",
        help=f"Path to project_config.json (default: {_DEFAULT_PROJECT_CONFIG})",
    )
    parser.add_argument(
        "--output", default=None, metavar="PATH",
        help=(
            "Where to write the CSV report (default: "
            "Records/YYYYMMDD_HHMMSS_esid_record_report.csv under the "
            "project root)."
        ),
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help=(
            "Log every fetched record, including the titles excluded as "
            "pattern misses."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    # --- Title patterns: validated before any network work ---
    title_patterns = args.title_pattern or list(_DEFAULT_TITLE_PATTERNS)
    and_patterns = args.and_title_pattern or []
    try:
        title_res = [compile_title_pattern(p) for p in title_patterns]
        filter_res = [compile_filter_pattern(p) for p in and_patterns]
    except ReportError as exc:
        logger.error("%s", exc)
        sys.exit(2)

    # --- Credentials.  Hard-required for any scope that reads the account
    # listing: drafts are only visible there, and a tokenless run that
    # silently skipped them would report every draft as absent.
    credentials = None
    try:
        credentials = get_credentials_from_env()
    except ValueError as exc:
        if args.scope in ("both", "account"):
            logger.error("%s", exc)
            logger.error(
                "The '%s' scope reads your account's records (drafts "
                "included) and needs the API token. Run: "
                "source Resources/set_env.sh — or use --scope community "
                "for the public, tokenless (published-only) view.",
                args.scope,
            )
            sys.exit(2)

    if credentials is not None:
        base_url = credentials.base_url
        headers = _auth_headers(credentials)
        page_size = _PAGE_SIZE_AUTHENTICATED
    else:
        base_url = os.environ.get("INVENIO_RDM_BASE_URL", _DEFAULT_BASE_URL)
        if not base_url.endswith("/"):
            base_url += "/"
        headers = {}
        page_size = _PAGE_SIZE_ANONYMOUS

    # Web URLs (records/uploads pages) live one level above the API base:
    # https://zenodo.org/api/ -> https://zenodo.org/
    web_base = base_url[:-4] if base_url.endswith("api/") else "https://zenodo.org/"

    if args.output:
        output_path = Path(args.output)
    else:
        # Default: Records/<timestamp>_esid_record_report.csv (timestamp
        # first so runs sort chronologically in a file listing).
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = (
            azus_common.PROJECT_ROOT / "Records"
            / f"{stamp}_esid_record_report.csv"
        )

    logger.info("=" * 70)
    logger.info("AZUS ESID RECORD REPORT (read-only)")
    logger.info("=" * 70)
    logger.info("Endpoint: %s", base_url)
    logger.info("Scope:    %s", args.scope)
    logger.info("Patterns: %s", "  OR  ".join(title_patterns))
    if and_patterns:
        logger.info("AND also: %s", "  AND  ".join(and_patterns))
    logger.info("Output:   %s", output_path)
    logger.info("=" * 70)

    # --- Fetch (everything verified before anything is written) ---
    community_records: List[EsidRecord] = []
    account_records: List[EsidRecord] = []
    excluded_total = 0
    try:
        if args.scope in ("both", "community"):
            community_id = args.community_id or _load_community_id(
                Path(args.project_config)
            )
            logger.info("Community: %s", community_id)
            url = (f"{base_url}communities/{community_id}/records"
                   f"?size={page_size}")
            community_records, excluded = records_from_hits(
                fetch_all_hits_verified(
                    url, headers, "community records", page_size
                ),
                "community", web_base, title_res, filter_res,
            )
            excluded_total += excluded

        if args.scope in ("both", "account"):
            url = f"{base_url}user/records?size={page_size}"
            account_records, excluded = records_from_hits(
                fetch_all_hits_verified(
                    url, headers, "account records", page_size
                ),
                "account", web_base, title_res, filter_res,
            )
            excluded_total += excluded

        records = merge_records(community_records, account_records)
        rows = build_rows(records)
    except ReportError as exc:
        logger.error("REPORT ABORTED — %s", exc)
        logger.error("No CSV was written; nothing partial to mistrust.")
        sys.exit(2)
    except Exception as exc:
        logger.error("API fetch failed: %s", exc)
        logger.error("No CSV was written; nothing partial to mistrust.")
        sys.exit(2)

    write_report(rows, output_path)

    # --- Summary ---
    drafts = sum(1 for r in rows if r["Draft (y/n)"] == "y")
    published = sum(1 for r in rows if r["Draft (y/n)"] == "n")
    unknown = sum(1 for r in rows if r["Draft (y/n)"] == "?")
    no_doi = sum(1 for r in rows if not r["DOI"])
    error_rows = sum(1 for r in rows if r["ERROR?"])
    logger.info("=" * 70)
    logger.info("SUMMARY")
    logger.info("=" * 70)
    logger.info("Pattern matches (community): %d", len(community_records))
    logger.info("Pattern matches (account):   %d", len(account_records))
    logger.info("Ignored (pattern miss):      %d", excluded_total)
    logger.info("Distinct records merged:     %d", len(records))
    logger.info("ESID rows written:           %d", len(rows))
    logger.info("  Drafts (y):              %d", drafts)
    logger.info("  Published (n):           %d", published)
    if unknown:
        logger.info("  Unknown draft state (?): %d", unknown)
    logger.info("  Rows without a DOI:      %d", no_doi)
    logger.info("Report: %s", output_path)
    if error_rows:
        logger.warning(
            "%d row(s) carry an ERROR? message — filter the ERROR? "
            "column in the CSV and review each one.", error_rows,
        )
    logger.info("=" * 70)
    sys.exit(1 if error_rows else 0)


if __name__ == "__main__":
    main()
