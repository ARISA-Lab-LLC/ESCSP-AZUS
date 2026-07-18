#!/usr/bin/env python3
"""Find Zenodo records with duplicate titles (Eclipse Soundscapes).

WHAT THIS TOOL DOES
===================
Duplicate records have been observed on Zenodo.  The known way AZUS can
create them: if ``upload_state.json`` is lost from a staging folder
(for example, a re-prep wipes and rebuilds the folder), the next upload
run cannot resume the existing draft and creates a FRESH one — same
title, two records.  This tool makes those duplicates visible.

It fetches record titles from up to two places:

  * the Eclipse Soundscapes COMMUNITY listing (public API — records
    that have been accepted into the community), and
  * the uploading ACCOUNT's own records (``/api/user/records`` —
    drafts, in-review, and published records; requires the API token).

…then reports two kinds of duplicate groups:

  * ``exact-title`` — two or more records whose titles are identical
    (after trimming whitespace and ignoring case) but that are NOT
    versions of one another (different ``parent.id``).  Versions of
    the same record legitimately share a title and are never flagged.
  * ``same-esid``  — two or more non-version records whose titles
    contain the same ESID number but whose titles differ.  This
    catches duplicates hidden by a title-template change between runs.

THE TOOL IS READ-ONLY.  It deletes nothing.  Each reported record row
includes its URL so you can review and remove strays by hand — draft
duplicates are the safely deletable ones; published duplicates need a
curation decision.

USAGE
=====
From the project root:

    # Full check (community + your account).  Requires the API token:
    source Resources/set_env.sh
    python Resources/find_duplicate_records.py

    # Community-only check — public API, no token needed:
    python Resources/find_duplicate_records.py --scope community

    # Log every fetched title, not just the duplicates:
    python Resources/find_duplicate_records.py --verbose

EXIT CODES
==========
    0  no duplicates found
    1  at least one duplicate group found (see CSV + summary)
    2  usage / auth / API error
"""

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

logger = logging.getLogger("azus.dup_check")

_DEFAULT_PROJECT_CONFIG = _PROJECT_ROOT / "Resources" / "project_config.json"
_DEFAULT_BASE_URL = "https://zenodo.org/api/"

# ESID number as it appears in generated titles, e.g. "... ESID 073" /
# "ESID_073" / "ESID#73".  Case-insensitive.
_ESID_IN_TITLE_RE = re.compile(r"ESID[\s_#]*(\d+)", re.IGNORECASE)

# Runaway guard: 200 pages x 100 hits = 20,000 records per source, far
# beyond anything this project will produce.
_MAX_PAGES_PER_SOURCE = 200

# Zenodo rejects size > 25 on unauthenticated requests ("Please use
# authenticated requests to increase the limit to 100"), so the page
# size depends on whether we hold a token.
_PAGE_SIZE_AUTHENTICATED = 100
_PAGE_SIZE_ANONYMOUS = 25

_CSV_COLUMNS = [
    "Group #",
    "Group Type",
    "Group Key",
    "Record ID",
    "Parent ID",
    "Status",
    "Published",
    "DOI",
    "Created",
    "Updated",
    "Source",
    "Title",
    "URL",
]


@dataclass
class RecordInfo:
    """The fields of one Zenodo record hit that this tool cares about."""

    record_id: str
    parent_id: str
    title: str
    doi: str
    status: str
    is_published: bool
    created: str
    updated: str
    url: str
    source: str  # "community", "account", or "community+account"

    @property
    def normalized_title(self) -> str:
        """Whitespace-collapsed, case-folded title for exact matching."""
        return " ".join(self.title.split()).casefold()

    @property
    def esid(self) -> Optional[str]:
        """Zero-padded ESID number extracted from the title, if present."""
        m = _ESID_IN_TITLE_RE.search(self.title)
        return f"{int(m.group(1)):03d}" if m else None


def _record_from_hit(hit: Dict, source: str) -> RecordInfo:
    """Map one API hit to a RecordInfo (defensive .get() everywhere —
    drafts may lack a DOI, and field shapes vary slightly by endpoint).

    Zenodo serves TWO serializations and this tool must read both
    (verified against production July 2026):

      * InvenioRDM shape (e.g. /api/user/records): ``parent.id``,
        ``pids.doi.identifier``, ``is_published``.
      * Legacy shape (e.g. the community records listing):
        ``parent``/``pids`` are None; the version-group id is
        ``conceptrecid`` and the DOI is the top-level ``doi`` field.

    Version-group id matters for correctness: two VERSIONS of one
    record legitimately share a title and must not be flagged as
    duplicates.  Published-ness falls back to ``status == "published"``
    when ``is_published`` is absent — a published record mislabeled as
    a draft would be listed as a "safely deletable stray", which it
    is not.
    """
    status = str(hit.get("status", ""))
    parent_id = (
        (hit.get("parent") or {}).get("id")
        or hit.get("conceptrecid")
        or hit.get("id", "")
    )
    doi = (
        ((hit.get("pids") or {}).get("doi") or {}).get("identifier")
        or hit.get("doi")
        or ""
    )
    links = hit.get("links") or {}
    return RecordInfo(
        record_id=str(hit.get("id", "")),
        parent_id=str(parent_id),
        title=(hit.get("metadata") or {}).get("title", "") or hit.get("title", "") or "",
        doi=str(doi),
        status=status,
        is_published=bool(hit.get("is_published", status == "published")),
        created=str(hit.get("created", "")),
        updated=str(hit.get("updated", "")),
        url=str(links.get("self_html") or links.get("html") or ""),
        source=source,
    )


def fetch_all_hits(
    first_url: str, headers: Dict[str, str], label: str
) -> List[Dict]:
    """Fetch every hit from a paginated InvenioRDM listing endpoint.

    Follows ``links.next`` until it disappears.  All requests go through
    the uploader's retry helper, so transient 5xx responses are retried
    with backoff instead of aborting the audit.
    """
    hits: List[Dict] = []
    url: Optional[str] = first_url
    page = 0
    while url:
        page += 1
        if page > _MAX_PAGES_PER_SOURCE:
            logger.warning(
                "%s: stopped after %d pages (%d records) — runaway guard. "
                "Raise _MAX_PAGES_PER_SOURCE if the collection is really "
                "this large.",
                label, _MAX_PAGES_PER_SOURCE, len(hits),
            )
            break
        response = _api_get_with_retry(
            url=url, auth_headers=headers, label=f"{label} page {page}",
        )
        payload = response.json()
        page_hits = (payload.get("hits") or {}).get("hits", []) or []
        hits.extend(page_hits)
        url = (payload.get("links") or {}).get("next")
    logger.info("%s: fetched %d record(s) across %d page(s)", label, len(hits), page)
    return hits


def find_duplicate_groups(
    records: List[RecordInfo],
) -> List[Tuple[str, str, List[RecordInfo]]]:
    """Group records into duplicate groups.

    Returns a list of (group_type, group_key, members) tuples:

      * ("exact-title", <normalized title>, members) — ≥2 records share
        the normalized title AND span ≥2 distinct parent ids.  Records
        sharing a parent are versions of one record — never duplicates.
      * ("same-esid", <ESID>, members) — ≥2 records with the same ESID
        in the title, ≥2 distinct parent ids, AND ≥2 distinct
        normalized titles (identical-title cases are already covered
        by exact-title groups).

    A record can legitimately appear in one group of each type (e.g.,
    two identical-title strays plus a third with a renamed title).
    """
    groups: List[Tuple[str, str, List[RecordInfo]]] = []

    by_title: Dict[str, List[RecordInfo]] = {}
    for rec in records:
        if not rec.normalized_title:
            logger.warning(
                "Record %s has an empty title — excluded from grouping "
                "(%s)", rec.record_id, rec.url,
            )
            continue
        by_title.setdefault(rec.normalized_title, []).append(rec)

    for title_key in sorted(by_title):
        members = by_title[title_key]
        if len(members) >= 2 and len({m.parent_id for m in members}) >= 2:
            groups.append(("exact-title", title_key, members))

    by_esid: Dict[str, List[RecordInfo]] = {}
    for rec in records:
        if rec.esid is not None and rec.normalized_title:
            by_esid.setdefault(rec.esid, []).append(rec)

    for esid_key in sorted(by_esid):
        members = by_esid[esid_key]
        distinct_parents = {m.parent_id for m in members}
        distinct_titles = {m.normalized_title for m in members}
        if (len(members) >= 2
                and len(distinct_parents) >= 2
                and len(distinct_titles) >= 2):
            groups.append(("same-esid", esid_key, members))

    return groups


def write_report(
    groups: List[Tuple[str, str, List[RecordInfo]]], output_path: Path
) -> None:
    """One CSV row per record in each duplicate group."""
    with open(output_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS)
        writer.writeheader()
        for group_num, (group_type, key, members) in enumerate(groups, 1):
            for rec in sorted(members, key=lambda r: r.created):
                writer.writerow({
                    "Group #": group_num,
                    "Group Type": group_type,
                    "Group Key": key,
                    "Record ID": rec.record_id,
                    "Parent ID": rec.parent_id,
                    "Status": rec.status,
                    "Published": "yes" if rec.is_published else "no",
                    "DOI": rec.doi,
                    "Created": rec.created,
                    "Updated": rec.updated,
                    "Source": rec.source,
                    "Title": rec.title,
                    "URL": rec.url,
                })
    logger.info("Report written: %s", output_path)


def _load_community_id(project_config_path: Path) -> str:
    """Read community_id from project_config.json."""
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
    """Command-line entry point."""
    parser = argparse.ArgumentParser(
        description=(
            "Fetch record titles from the Eclipse Soundscapes community "
            "and/or the uploading account's own records (drafts included) "
            "and report duplicate-title groups to a CSV. Read-only: "
            "deletes nothing."
        ),
    )
    parser.add_argument(
        "--scope", choices=("both", "community", "account"), default="both",
        help=(
            "Where to look (default: both). 'community' = records accepted "
            "into the community (public API, no token needed). 'account' = "
            "everything owned by your Zenodo account, INCLUDING drafts "
            "(needs the API token from Resources/set_env.sh). 'both' = "
            "union of the two, deduplicated."
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
            "duplicate_records_report_YYYYMMDD_HHMMSS.csv in the current "
            "directory)."
        ),
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Log every fetched record title, not just the duplicates.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    # --- Credentials.  Hard-required for any scope that reads the account
    # listing: silently skipping drafts would report "no duplicates" while
    # the likeliest hiding place went unscanned.
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
                "for the public, tokenless check.",
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

    output_path = (
        Path(args.output)
        if args.output
        else azus_common.timestamped_output_path("duplicate_records_report")
    )

    logger.info("=" * 70)
    logger.info("AZUS DUPLICATE-RECORD CHECK (read-only)")
    logger.info("=" * 70)
    logger.info("Endpoint: %s", base_url)
    logger.info("Scope:    %s", args.scope)
    logger.info("Output:   %s", output_path)
    logger.info("=" * 70)

    # --- Fetch ---
    records_by_id: Dict[str, RecordInfo] = {}
    try:
        if args.scope in ("both", "community"):
            community_id = args.community_id or _load_community_id(
                Path(args.project_config)
            )
            logger.info("Community: %s", community_id)
            url = (f"{base_url}communities/{community_id}/records"
                   f"?size={page_size}")
            for hit in fetch_all_hits(url, headers, "community records"):
                rec = _record_from_hit(hit, "community")
                records_by_id[rec.record_id] = rec

        if args.scope in ("both", "account"):
            url = f"{base_url}user/records?size={page_size}"
            for hit in fetch_all_hits(url, headers, "account records"):
                rec = _record_from_hit(hit, "account")
                existing = records_by_id.get(rec.record_id)
                if existing is not None:
                    existing.source = "community+account"
                else:
                    records_by_id[rec.record_id] = rec
    except Exception as exc:
        logger.error("API fetch failed: %s", exc)
        sys.exit(2)

    records = list(records_by_id.values())
    logger.info("Total distinct records: %d", len(records))
    if args.verbose:
        for rec in sorted(records, key=lambda r: r.normalized_title):
            logger.info(
                "  [%s] %s (%s, published=%s) %s",
                rec.record_id, rec.title, rec.status,
                rec.is_published, rec.url,
            )

    # --- Detect + report ---
    groups = find_duplicate_groups(records)
    write_report(groups, output_path)

    involved: Dict[str, RecordInfo] = {}
    for group_num, (group_type, key, members) in enumerate(groups, 1):
        logger.info(
            "Group %d [%s] key=%r — %d record(s):",
            group_num, group_type, key, len(members),
        )
        for rec in sorted(members, key=lambda r: r.created):
            involved[rec.record_id] = rec
            logger.info(
                "    %s  %-9s published=%-5s created=%s  %s",
                rec.record_id, rec.status, rec.is_published,
                rec.created[:10], rec.url,
            )

    draft_count = sum(1 for r in involved.values() if not r.is_published)
    logger.info("=" * 70)
    logger.info("SUMMARY")
    logger.info("=" * 70)
    logger.info("Records checked:       %d", len(records))
    logger.info("Duplicate groups:      %d (exact-title: %d, same-esid: %d)",
                len(groups),
                sum(1 for g in groups if g[0] == "exact-title"),
                sum(1 for g in groups if g[0] == "same-esid"))
    logger.info("Records in groups:     %d", len(involved))
    logger.info("  of which UNPUBLISHED (draft/in-review — the safely "
                "deletable strays): %d", draft_count)
    logger.info("Report: %s", output_path)
    logger.info("This tool deletes nothing — review each URL and remove "
                "strays by hand.")
    logger.info("=" * 70)

    sys.exit(1 if groups else 0)


if __name__ == "__main__":
    main()
