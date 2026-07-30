#!/usr/bin/env python3
"""Publish a re-prepped staging package as a NEW VERSION of a published record.

PURPOSE
=======
Published Zenodo files are immutable.  When an already-published ESID
record turns out to carry wrong metadata AND a broken or short ZIP, the
only fix is a new version — a second record in the same version chain,
sharing the concept DOI, with its own version DOI.  Until now this repo
has always deferred that decision to a human (see the note in
``Resources/reprep_incomplete_staging.py``).  This tool performs it.

Input is a freshly re-prepped ``Staging_Area/ESID_NNN_Staging/`` package
plus the record id of the published version being superseded.  Both are
required and nothing is inferred: one record id pairs with one ESID.

THE FILE SET IS EXACTLY THE NEW PACKAGE
=======================================
``POST /versions`` returns a draft with NO files, and this tool
deliberately does NOT call ``POST /draft/actions/files-import``.  Linking
the previous version's files would be free, but the uploader's
reconciliation leaves remote entries that are absent from the upload list
untouched — so any imported v1 file whose name the new package does not
use would ride forward onto the published new version, permanently.  For
the case this tool exists to fix, that file is the corrupt ZIP.

Starting from an empty draft makes the invariant checkable in both
directions, which is what the completeness gate enforces: the file set on
the new version equals the new package, exactly.

THE NEW VERSION IS NEVER SUBMITTED FOR COMMUNITY REVIEW
=======================================================
A new version inherits community membership through the shared parent.
Re-submitting it would put it in a queue where a manager's "accept"
PUBLISHES it — the same race ``--defer-zip`` exists to avoid.  This tool
passes ``submit_review=False`` and never calls
``submit_to_community_review``.

SAFETY MODEL (execution is opt-in, publication doubly so)
=========================================================
Dry-run by default: without ``--execute`` the tool makes exactly two
read-only GETs and writes nothing but its CSV report.  Classification and
mutation are separate, and the mutating path re-derives every gate from
scratch before touching anything.  Refusals are logged as
``REFUSING to ...`` and never raise.

``--publish`` is OFF by default, and that default IS the rollback
strategy: every state up to publication is undone by discarding one
draft, and nothing before publication can alter the published record
being superseded.  Once published, a version and its DOI are permanent.

WITHOUT A SANDBOX, THE DRY RUN IS THE COMPENSATING CONTROL
==========================================================
There is no sandbox account for this project, so the dry run is designed
to be the thing you trust instead: it prints the fully constructed URLs,
a per-key diff of the published metadata against the rebuilt metadata,
the exact file plan, and the precise call sequence tagged READ/WRITE —
including a list of the calls deliberately NOT made.  Read it before
every execute.

VERSION LABELS
==============
``POST /versions`` clears ``version``, so it must be re-supplied.  The
label advances by a trailing letter: ``2024.1.0`` -> ``2024.1.0a`` ->
``2024.1.0b``.  Anything the rule cannot advance unambiguously is refused
with a message naming ``--version-label``; see :func:`bump_version_label`.

USAGE
=====
::

    # 1. Review the plan (two read-only GETs; writes nothing)
    python Resources/new_version_upload.py --esid 073 --record-id 15234567

    # 2. Create the new version, leaving it a DRAFT for inspection
    python Resources/new_version_upload.py --esid 073 --record-id 15234567 \\
        --execute

    # 3. Publish it, once the draft looks right in the Zenodo UI
    python Resources/new_version_upload.py --esid 073 --record-id 15234567 \\
        --execute --publish

OUTPUT
======
A one-row CSV recording the record ids, both version labels, the concept
DOI, the file counts and the verdict — the audit trail for an operation
that cannot be undone once published.

EXIT CODES
==========
* ``0`` — dry run all-clear, or ``--execute`` completed
* ``1`` — a gate refused, or a step failed
* ``2`` — usage error (bad flags, missing staging folder, credentials)
"""

import argparse
import csv
import json
import logging
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import azus_common
# The per-day prep marks dataset versions with a trailing suffix
# ("2024.1.0A"); bump_version_label must recognize it, and importing the
# constant from its owner means the two modules cannot drift.
from prepare_dataset import DAY_ZIP_VERSION_SUFFIX

# The pipeline modules live at the project root, one level up.
_PROJECT_ROOT = azus_common.PROJECT_ROOT
sys.path.insert(0, str(_PROJECT_ROOT))

from standalone_tasks import (  # noqa: E402
    get_draft_config,
    load_project_config,
    parse_collectors_csv,
    verify_dataset_integrity,
)
from standalone_uploader import (  # noqa: E402
    _normalize_title,
    _search_drafts_by_title,
    create_new_version_draft,
    ensure_doi_reserved,
    get_credentials_from_env,
    get_draft_record,
    get_published_record,
    list_draft_files,
    publish_draft,
    update_draft_metadata,
    upload_to_zenodo,
)

logger = logging.getLogger("azus.new_version")

_STAGING_AREA = azus_common.STAGING_AREA
_UPLOADED_DATA = azus_common.UPLOADED_DATA

# This tool's marker in the staging folder's state file.  Deliberately NOT
# accompanied by a "record_id" key: that key is what
# finish_stuck_uploads.discover_stuck_esids() looks for, and a folder it
# discovered would be resumed through the main pipeline with
# submit_review=True — hijacking the new-version draft into the community
# review queue.
NEW_VERSION_MODE = "new_version"

# The ONLY keys sent in the PUT body.  Everything except "metadata" is
# echoed verbatim from the new-version draft.  Two hazards drive this:
# PUT /draft replaces the whole representation (so an omitted "pids"
# strips a reserved DOI and an omitted "files" can disable the bucket),
# and InvenioRDM's schema rejects dump-only fields (id, links, versions,
# status, ...), so echoing the whole body back would 400.  "parent" is
# excluded on purpose — community membership lives there and is changed
# through its own endpoint, never as a side effect of a metadata edit.
_PUT_ALLOWED_KEYS = ("access", "files", "metadata", "custom_fields", "pids")

_CSV_COLUMNS = [
    "ESID#",
    "Previous Record ID",
    "Previous Version",
    "New Record ID",
    "New Version",
    "Concept DOI",
    "Version DOI",
    "Title",
    "Metadata Changes",
    "Files Uploaded",
    "Files Not Carried Forward",
    "Published",
    "Verdict",
    "Action Taken",
    "Notes",
]

# Verdicts — see the module docstring for the safety model.
VERSION_PLANNED = "VERSION_PLANNED"
VERSION_CREATED = "VERSION_CREATED"
VERSION_PUBLISHED = "VERSION_PUBLISHED"
NO_CREDENTIALS = "NO_CREDENTIALS"
BAD_BASE_URL = "BAD_BASE_URL"
NO_STAGING_FOLDER = "NO_STAGING_FOLDER"
PREP_INCOMPLETE = "PREP_INCOMPLETE"
ZIP_AMBIGUOUS = "ZIP_AMBIGUOUS"
STAGING_IS_A_FIRST_UPLOAD = "STAGING_IS_A_FIRST_UPLOAD"
STAGING_IS_FILE_BY_FILE = "STAGING_IS_FILE_BY_FILE"
INTEGRITY_FAILED = "INTEGRITY_FAILED"
NO_COLLECTOR_ROW = "NO_COLLECTOR_ROW"
METADATA_BUILD_FAILED = "METADATA_BUILD_FAILED"
RECORD_NOT_FOUND = "RECORD_NOT_FOUND"
RECORD_ID_MISMATCH = "RECORD_ID_MISMATCH"
RECORD_NOT_PUBLISHED = "RECORD_NOT_PUBLISHED"
RECORD_NOT_LATEST = "RECORD_NOT_LATEST"
DRAFT_ALREADY_OPEN = "DRAFT_ALREADY_OPEN"
TITLE_MISMATCH = "TITLE_MISMATCH"
ACCOUNT_SWEEP_UNCLEAN = "ACCOUNT_SWEEP_UNCLEAN"
VERSION_BUMP_REFUSED = "VERSION_BUMP_REFUSED"
ARCHIVE_EXISTS = "ARCHIVE_EXISTS"
NEW_DRAFT_NOT_EMPTY = "NEW_DRAFT_NOT_EMPTY"
METADATA_PUT_UNVERIFIED = "METADATA_PUT_UNVERIFIED"
UPLOAD_FAILED = "UPLOAD_FAILED"
INCOMPLETE_ON_RECORD = "INCOMPLETE_ON_RECORD"
VERSION_CREATE_AMBIGUOUS = "VERSION_CREATE_AMBIGUOUS"

_RECOMMENDED_ACTION = {
    VERSION_PLANNED: "review the metadata diff above, then re-run with --execute",
    VERSION_CREATED: (
        "inspect the draft in the Zenodo UI, then publish it with "
        "--execute --publish (or from the UI)"
    ),
    VERSION_PUBLISHED: "done — the new version is live and permanent",
    NO_STAGING_FOLDER: "re-prep this ESID first (Resources/prepare_dataset.py)",
    PREP_INCOMPLETE: "re-prep — the .prep_complete sentinel is missing",
    ZIP_AMBIGUOUS: "the staging folder must hold exactly one ESID_*.zip",
    STAGING_IS_A_FIRST_UPLOAD: (
        "this folder is a stuck FIRST upload, not a new version — finish it "
        "with Resources/finish_stuck_uploads.py"
    ),
    STAGING_IS_FILE_BY_FILE: (
        "this folder is in file-by-file mode — finish it with "
        "Resources/finish_stuck_uploads.py --enable-file-by-file"
    ),
    INTEGRITY_FAILED: (
        "the local package is not trustworthy — fix it before versioning "
        "(that is the whole point of this operation)"
    ),
    NO_COLLECTOR_ROW: "add this ESID's row to the collectors spreadsheet",
    METADATA_BUILD_FAILED: "fix the staging folder or the collectors row",
    RECORD_NOT_FOUND: "check --record-id against Resources/esid_record_report.py",
    RECORD_ID_MISMATCH: (
        "--record-id resolved to a different record; you most likely passed "
        "the CONCEPT id instead of a version id"
    ),
    RECORD_NOT_PUBLISHED: (
        "that record is still a draft — finish it, do not version it"
    ),
    RECORD_NOT_LATEST: (
        "version the LATEST version of the chain, not an older one"
    ),
    DRAFT_ALREADY_OPEN: (
        "a draft is already open on this chain — publish or discard it "
        "(DELETE /api/records/{id}/draft) before creating another"
    ),
    TITLE_MISMATCH: (
        "the rebuilt title differs from the published one — you may have "
        "paired the wrong --record-id with this ESID. Use "
        "--allow-title-change only if the retitle is intended"
    ),
    ACCOUNT_SWEEP_UNCLEAN: (
        "resolve the stray draft or duplicate records first "
        "(Resources/find_duplicate_records.py)"
    ),
    VERSION_BUMP_REFUSED: "pass --version-label explicitly",
    ARCHIVE_EXISTS: "move or rename the existing archive folder first",
    NEW_DRAFT_NOT_EMPTY: (
        "the new draft already holds files — a previous run got further "
        "than expected; inspect it in the Zenodo UI before continuing"
    ),
    METADATA_PUT_UNVERIFIED: (
        "the metadata update did not read back as sent — do NOT upload; "
        "inspect the draft and report this"
    ),
    UPLOAD_FAILED: "re-run with --execute; the upload resumes where it stopped",
    INCOMPLETE_ON_RECORD: (
        "the record does not hold exactly the new package — re-run with "
        "--execute; nothing was published"
    ),
    VERSION_CREATE_AMBIGUOUS: (
        "the versions POST failed in a way that MAY still have created a "
        "draft — re-run the DRY RUN to see whether one appeared, then adopt "
        "or discard it by hand. The published record is unaffected"
    ),
}


# ===================================================================
#  Pure helpers (no I/O — unit-tested directly)
# ===================================================================

def bump_version_label(current: Optional[str]) -> str:
    """Advance a version label by its trailing revision letter.

    ``2024.1.0`` -> ``2024.1.0a`` -> ``2024.1.0b``.  The per-day prep's
    version marker (:data:`DAY_ZIP_VERSION_SUFFIX`, ``"A"``) is treated
    as part of the BASE version, and the lowercase revision ladder
    continues after it: ``2024.1.0A`` -> ``2024.1.0Aa`` ->
    ``2024.1.0Ab``.  Beyond that, the rule refuses anything it cannot
    advance unambiguously rather than inventing a convention: silently
    turning ``1.0-beta`` into ``1.0-betb`` would put a nonsense version
    on a permanent public record.

    Args:
        current: The published record's ``metadata.version``.

    Returns:
        The next label.

    Raises:
        ValueError: When the label cannot be advanced.  Every message
            names ``--version-label`` as the way forward.
    """
    escape = " — pass --version-label to set it explicitly."
    text = (current or "").strip()
    if not text:
        raise ValueError(
            "The published record has no version to advance from" + escape
        )

    stem = text.rstrip("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
    suffix = text[len(stem):]

    if not stem:
        raise ValueError(
            f"Version {text!r} is all letters, so there is no stem to "
            "advance" + escape
        )
    if not suffix:
        return stem + "a"

    # The per-day marker is base version, not a revision letter: start
    # (or continue) the lowercase ladder after it.
    if suffix == DAY_ZIP_VERSION_SUFFIX:
        return text + "a"
    if (
        len(suffix) == 2
        and suffix[0] == DAY_ZIP_VERSION_SUFFIX
        and suffix[1].islower()
    ):
        if suffix[1] == "z":
            raise ValueError(
                f"Version {text!r} has reached 'z' — 26 letter revisions "
                "means something else is wrong" + escape
            )
        return text[:-1] + chr(ord(suffix[1]) + 1)

    if len(suffix) > 1:
        raise ValueError(
            f"Version {text!r} ends in more than one letter ({suffix!r}), so "
            "advancing it would be a guess" + escape
        )
    if suffix.isupper():
        raise ValueError(
            f"Version {text!r} ends in an uppercase letter; advancing it "
            "would either mix cases or invent a second convention" + escape
        )
    if suffix == "z":
        raise ValueError(
            f"Version {text!r} has reached 'z' — 26 letter revisions means "
            "something else is wrong" + escape
        )
    return stem + chr(ord(suffix) + 1)


def build_put_payload(
    version_draft: Dict[str, Any], metadata: Dict[str, Any]
) -> Dict[str, Any]:
    """Compose the ``PUT /draft`` body: echo the draft, replace metadata.

    ``PUT /records/{id}/draft`` replaces the draft's whole representation,
    so every key that must survive has to be sent back.  Only the five
    keys in :data:`_PUT_ALLOWED_KEYS` are included — notably ``pids``,
    whose omission would strip a reserved DOI, and ``files``, whose
    omission could disable the bucket.  Dump-only fields (``id``,
    ``links``, ``versions``, ``status``, ...) are excluded because the
    schema rejects them, and ``parent`` because community membership is
    not this call's business.

    Args:
        version_draft: The body returned by ``POST /records/{id}/versions``.
        metadata: The rebuilt metadata dict to install.

    Returns:
        The exact payload to hand to
        :func:`standalone_uploader.update_draft_metadata`.
    """
    payload: Dict[str, Any] = {}
    for key in _PUT_ALLOWED_KEYS:
        if key in version_draft:
            payload[key] = version_draft[key]
    # "pids" must be present even when empty: sending the key with {} is
    # explicit, whereas omitting it lets a full-replace drop an existing DOI.
    payload.setdefault("pids", {})
    payload["metadata"] = metadata
    return payload


def metadata_diff(
    published: Dict[str, Any], rebuilt: Dict[str, Any]
) -> List[Tuple[str, str, str]]:
    """Compare the published metadata against the rebuilt metadata, per key.

    The driving case for this tool is "the published metadata is wrong",
    so this diff is the operator's main evidence: it shows the intended
    fix is present AND that nothing else moved (a changed
    ``README_template.html`` silently rewriting the description, say).

    Args:
        published: ``metadata`` from the published record.
        rebuilt: ``metadata`` freshly built from the staging package.

    Returns:
        ``(key, verdict, detail)`` triples sorted by key, where verdict is
        ``"same"``, ``"CHANGED"``, ``"added"`` or ``"removed"``.
    """
    rows: List[Tuple[str, str, str]] = []
    for key in sorted(set(published) | set(rebuilt)):
        old, new = published.get(key), rebuilt.get(key)
        if key not in rebuilt:
            rows.append((key, "removed", _short(old)))
        elif key not in published:
            rows.append((key, "added", _short(new)))
        elif old == new:
            rows.append((key, "same", ""))
        elif isinstance(old, str) and isinstance(new, str) and len(old) > 200:
            offset = next(
                (i for i, (a, b) in enumerate(zip(old, new)) if a != b),
                min(len(old), len(new)),
            )
            rows.append((
                key, "CHANGED",
                f"{len(old):,} -> {len(new):,} chars, "
                f"first difference at offset {offset:,}",
            ))
        else:
            rows.append((key, "CHANGED", f"{_short(old)}  ->  {_short(new)}"))
    return rows


def _short(value: Any, limit: int = 90) -> str:
    """Render a metadata value compactly for the diff table.

    Args:
        value: Any JSON-ish metadata value.
        limit: Maximum characters before truncation.

    Returns:
        A single-line string, truncated with an ellipsis when long.
    """
    text = json.dumps(value, ensure_ascii=False) if not isinstance(
        value, str
    ) else value
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _versions_flags(record: Dict[str, Any]) -> Tuple[bool, bool]:
    """Read the ``versions.is_latest`` / ``is_latest_draft`` flags.

    Args:
        record: A published record body.

    Returns:
        ``(is_latest, is_latest_draft)``; a missing flag reads as False so
        an unfamiliar serialization fails closed.
    """
    versions = record.get("versions") or {}
    return bool(versions.get("is_latest")), bool(versions.get("is_latest_draft"))


def _concept_doi(record: Dict[str, Any]) -> str:
    """Extract the all-versions (concept) DOI from a published record.

    Args:
        record: A published record body.

    Returns:
        The concept DOI, or ``""`` when the serialization does not carry
        one (informational only — nothing decides anything from it).
    """
    parent = record.get("parent") or {}
    doi = ((parent.get("pids") or {}).get("doi") or {})
    return str(doi.get("identifier") or "")


def _version_doi(record: Dict[str, Any]) -> str:
    """Extract a record's own version-specific DOI.

    Args:
        record: A published record or draft body.

    Returns:
        The version DOI, or ``""`` when none is assigned.
    """
    doi = ((record.get("pids") or {}).get("doi") or {})
    return str(doi.get("identifier") or "")


# ===================================================================
#  The plan
# ===================================================================

@dataclass
class VersionPlan:
    """Everything the execute phase needs, assembled read-only.

    Attributes:
        esid: Canonical ESID.
        staging: The re-prepped staging folder.
        record_id: The published record being superseded.
        published: That record's body.
        old_label: Its ``metadata.version``.
        new_label: The label the new version will carry.
        config: The rebuilt ``DraftConfig``.
        files: Absolute paths of every file to upload.
        zip_path: The package's single data ZIP.
        known_md5s: ``{basename: md5}`` captured by the integrity gate.
        concept_doi: The chain's all-versions DOI (may be ``""``).
        archive_destination: Where the staging folder will be moved.
    """

    esid: str
    staging: Path
    record_id: str
    published: Dict[str, Any]
    old_label: str
    new_label: str
    config: Any
    files: List[str]
    zip_path: Path
    known_md5s: Dict[str, str] = field(default_factory=dict)
    concept_doi: str = ""
    archive_destination: Optional[Path] = None

    @property
    def title(self) -> str:
        """The title the new version will carry.

        Returns:
            The rebuilt metadata's title.
        """
        return str((self.config.metadata or {}).get("title", ""))

    @property
    def published_title(self) -> str:
        """The published record's current title.

        Returns:
            The title as Zenodo holds it.
        """
        return str((self.published.get("metadata") or {}).get("title", ""))


def _blank_row(esid: str, record_id: str) -> Dict[str, object]:
    """Start a report row with the identity columns filled.

    Args:
        esid: Canonical ESID.
        record_id: The published record id.

    Returns:
        A row dict with every column present.
    """
    row: Dict[str, object] = {column: "" for column in _CSV_COLUMNS}
    row.update({
        "ESID#": esid, "Previous Record ID": record_id,
        "Files Uploaded": 0, "Published": "no",
    })
    return row


def _load_tool_config(config_path: Path) -> Dict[str, Any]:
    """Read the collectors CSV path and citation paths from config.json.

    Args:
        config_path: Path to AZUS config.json.

    Returns:
        A dict with ``collectors_csv``, ``dataset_category``,
        ``project_config``, ``related_identifiers_csv`` and
        ``references_csv`` (missing values become ``None``/``""``).

    Raises:
        ValueError: When the file cannot be read or names no dataset.
    """
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read {config_path}: {exc}") from exc
    uploads = cfg.get("uploads") or {}
    datasets = uploads.get("datasets") or []
    if not datasets:
        raise ValueError(f"{config_path} lists no uploads.datasets entries.")
    first = datasets[0]
    return {
        "collectors_csv": first.get("collectors_csv", ""),
        "dataset_category": first.get("dataset_category", "Total"),
        "project_config": cfg.get("project_config"),
        "related_identifiers_csv": uploads.get("related_identifiers_csv") or None,
        "references_csv": uploads.get("references_csv") or None,
    }


def _package_files(staging: Path) -> Tuple[List[str], Optional[Path], List[str]]:
    """List the files this package will upload, from its prep manifest.

    Args:
        staging: The staging folder.

    Returns:
        ``(paths, zip_path, problems)`` — absolute paths in manifest
        order, the single data ZIP, and any problems found.
    """
    problems: List[str] = []
    zips = sorted(staging.glob("ESID_*.zip"))
    zip_path = zips[0] if len(zips) == 1 else None
    if zip_path is None:
        problems.append(
            f"expected exactly one ESID_*.zip in {staging.name}, found {len(zips)}"
        )

    manifest = next(iter(sorted(staging.glob("ESID_*_to_upload.csv"))), None)
    paths: List[str] = []
    if manifest is None:
        problems.append(f"no ESID_*_to_upload.csv manifest in {staging.name}")
        return paths, zip_path, problems
    with open(manifest, "r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("File Name") or "").strip()
            if not name:
                continue
            candidate = staging / name
            if candidate.is_file():
                paths.append(str(candidate))
            else:
                problems.append(f"manifest lists {name}, which is not on disk")
    if not paths:
        problems.append(f"{manifest.name} lists no existing files")
    return paths, zip_path, problems


def read_state(staging: Path) -> Dict[str, Any]:
    """Read this tool's state file, tolerating absence and corruption.

    Args:
        staging: The staging folder.

    Returns:
        The parsed state dict, or ``{}``.
    """
    path = staging / azus_common.STATE_FILENAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(staging: Path, updates: Dict[str, Any]) -> None:
    """Read-merge ``updates`` into the staging folder's state file.

    Merged, not rebuilt, so keys written by other tools survive — the same
    contract ``_write_upload_state`` honours.  Never writes a ``record_id``
    key: that is what makes ``finish_stuck_uploads.py`` treat a folder as a
    stuck first upload and resume it through the main pipeline.

    Args:
        staging: The staging folder.
        updates: Keys to set.
    """
    state = read_state(staging)
    state.update(updates)
    state.pop("record_id", None)
    path = staging / azus_common.STATE_FILENAME
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


# ===================================================================
#  Read-only classification
# ===================================================================

def preflight(
    args: argparse.Namespace, credentials: Any
) -> Tuple[Dict[str, object], Optional[VersionPlan]]:
    """Run every gate and build the plan, mutating nothing.

    Makes exactly two read-only API calls: ``GET /records/{id}`` and the
    account title search.  A ``VERSION_PLANNED`` verdict is only ever
    advertised for a pair the execute path would actually accept, and
    :func:`execute` re-runs this whole function before touching anything.

    Args:
        args: Parsed CLI arguments.
        credentials: Zenodo API credentials.

    Returns:
        ``(row, plan)``; ``plan`` is None for every refusal.
    """
    esid = args.esid
    row = _blank_row(esid, args.record_id)

    def refuse(verdict: str, note: str = "") -> Tuple[Dict[str, object], None]:
        """Record a refusal on the row and return no plan."""
        row["Verdict"] = verdict
        if note:
            row["Notes"] = note
        return row, None

    # --- Local package -------------------------------------------------
    staging = _STAGING_AREA / f"ESID_{esid}_Staging"
    if not staging.is_dir():
        return refuse(NO_STAGING_FOLDER, f"{staging} does not exist")
    if not (staging / azus_common.PREP_SENTINEL).is_file():
        return refuse(PREP_INCOMPLETE, f"no {azus_common.PREP_SENTINEL}")

    state = read_state(staging)
    if state and state.get("mode") != NEW_VERSION_MODE:
        if azus_common.read_upload_mode(staging) == azus_common.FILE_BY_FILE_MODE:
            return refuse(STAGING_IS_FILE_BY_FILE, "mode=file_by_file")
        if state.get("record_id"):
            return refuse(
                STAGING_IS_A_FIRST_UPLOAD,
                f"state file points at record {state['record_id']}",
            )

    files, zip_path, problems = _package_files(staging)
    if problems:
        return refuse(ZIP_AMBIGUOUS, "; ".join(problems[:4]))

    digests: Dict[str, str] = {}
    integrity = verify_dataset_integrity(
        str(zip_path),
        verify_zip_hash=not args.skip_integrity_hash,
        digests_out=digests,
    )
    if integrity:
        return refuse(INTEGRITY_FAILED, "; ".join(integrity[:4]))
    known_md5s = {zip_path.name: digests["md5"]} if digests.get("md5") else {}

    # --- Rebuild the metadata BEFORE any mutation ----------------------
    try:
        tool_cfg = _load_tool_config(Path(args.config))
        project_config = load_project_config(tool_cfg["project_config"])
        collectors = parse_collectors_csv(
            tool_cfg["collectors_csv"], tool_cfg["dataset_category"],
            project_config,
        )
    except (ValueError, OSError, KeyError) as exc:
        return refuse(METADATA_BUILD_FAILED, str(exc))

    matches = [c for c in collectors if c.esid.casefold() == esid.casefold()]
    if not matches:
        return refuse(
            NO_COLLECTOR_ROW, f"no row for ESID {esid} in the collectors CSV"
        )

    readme_html = staging / "README.html"
    try:
        config = get_draft_config(
            data_collector=matches[0],
            readme_html_path=str(readme_html) if readme_html.is_file() else None,
            related_identifiers_csv=(
                str(staging / "related_identifiers.csv")
                if (staging / "related_identifiers.csv").is_file()
                else tool_cfg["related_identifiers_csv"]
            ),
            references_csv=(
                str(staging / "references.csv")
                if (staging / "references.csv").is_file()
                else tool_cfg["references_csv"]
            ),
            project_config=project_config,
            # False so config.pids stays falsy and upload_to_zenodo never
            # reserves: this tool owns reservation, after the metadata PUT.
            reserve_doi=False,
        )
    except Exception as exc:  # noqa: BLE001 — any build failure is a refusal
        return refuse(METADATA_BUILD_FAILED, f"{type(exc).__name__}: {exc}")

    # --- The published record (API call 1) -----------------------------
    published = get_published_record(credentials, args.record_id)
    if published is None:
        return refuse(RECORD_NOT_FOUND, f"record {args.record_id} returned 404")
    resolved = str(published.get("id") or "")
    if resolved != str(args.record_id):
        return refuse(
            RECORD_ID_MISMATCH,
            f"--record-id {args.record_id} resolved to record {resolved}",
        )
    if not published.get("is_published", False):
        return refuse(RECORD_NOT_PUBLISHED, f"record {resolved} is not published")
    is_latest, is_latest_draft = _versions_flags(published)
    if not is_latest:
        return refuse(RECORD_NOT_LATEST, f"record {resolved} is not the latest")

    old_label = str((published.get("metadata") or {}).get("version") or "")
    row.update({
        "Previous Version": old_label,
        "Concept DOI": _concept_doi(published),
        "Title": str((published.get("metadata") or {}).get("title", "")),
    })

    if not is_latest_draft:
        # A draft is already open on this chain.  Adopting ours is fine;
        # anyone else's is not.
        adopted = str(state.get("new_version_record_id") or "")
        if not adopted:
            return refuse(
                DRAFT_ALREADY_OPEN,
                "an unpublished draft already exists on this version chain",
            )

    # --- Title equality: the wrong-record gate -------------------------
    rebuilt_title = str((config.metadata or {}).get("title", ""))
    published_title = str((published.get("metadata") or {}).get("title", ""))
    if _normalize_title(rebuilt_title) != _normalize_title(published_title):
        if not args.allow_title_change:
            return refuse(
                TITLE_MISMATCH,
                f"rebuilt {rebuilt_title!r} != published {published_title!r}",
            )
        logger.warning(
            "  Title change accepted via --allow-title-change: %r -> %r",
            published_title, rebuilt_title,
        )

    # --- Account sweep (API call 2) ------------------------------------
    try:
        drafts, published_hits = _search_drafts_by_title(
            credentials, published_title, "new-version account sweep",
        )
    except Exception as exc:  # noqa: BLE001 — fails closed by design
        return refuse(ACCOUNT_SWEEP_UNCLEAN, f"title search failed: {exc}")
    stray = [d for d in drafts if str(d.get("id")) != str(
        state.get("new_version_record_id") or ""
    )]
    if stray:
        return refuse(
            ACCOUNT_SWEEP_UNCLEAN,
            f"{len(stray)} stray draft(s) share this title "
            f"(e.g. {stray[0].get('id')})",
        )
    if len(published_hits) > 1:
        return refuse(
            ACCOUNT_SWEEP_UNCLEAN,
            f"{len(published_hits)} published records share this title",
        )

    # --- Version label -------------------------------------------------
    if args.version_label:
        new_label = args.version_label.strip()
        if not new_label or new_label == old_label:
            return refuse(
                VERSION_BUMP_REFUSED,
                "--version-label must be non-empty and differ from the current",
            )
    else:
        try:
            new_label = bump_version_label(old_label)
        except ValueError as exc:
            return refuse(VERSION_BUMP_REFUSED, str(exc))

    destination = _UPLOADED_DATA / f"ESID_{esid}_Uploaded_{new_label}"
    if destination.exists():
        return refuse(ARCHIVE_EXISTS, f"{destination} already exists")

    plan = VersionPlan(
        esid=esid, staging=staging, record_id=str(args.record_id),
        published=published, old_label=old_label, new_label=new_label,
        config=config, files=files, zip_path=zip_path,
        known_md5s=known_md5s, concept_doi=_concept_doi(published),
        archive_destination=destination,
    )
    diff = metadata_diff(published.get("metadata") or {}, config.metadata or {})
    row.update({
        "New Version": new_label,
        "Metadata Changes": sum(1 for _k, v, _d in diff if v != "same"),
        "Verdict": VERSION_PLANNED,
    })
    return row, plan


# ===================================================================
#  Reporting
# ===================================================================

def log_plan(plan: VersionPlan, credentials: Any, args: argparse.Namespace) -> None:
    """Print everything an operator needs to approve the run.

    With no sandbox available this output IS the safety review, so it
    shows the constructed URLs, the metadata diff, the file plan, and the
    exact call sequence — including the calls deliberately not made.

    Args:
        plan: The assembled plan.
        credentials: Zenodo API credentials (for the base URL).
        args: Parsed CLI arguments.
    """
    base = credentials.base_url
    published_files = (plan.published.get("files") or {}).get("entries") or {}
    remote_names = set(published_files) if isinstance(published_files, dict) else set()
    local_names = {Path(p).name for p in plan.files}

    logger.info("-" * 70)
    logger.info("Record:     %s  (%s)", plan.record_id, plan.published_title)
    logger.info("Version:    %s  ->  %s", plan.old_label or "(none)", plan.new_label)
    logger.info("Concept DOI: %s", plan.concept_doi or "(not present — informational)")
    logger.info("Version DOI (previous): %s", _version_doi(plan.published) or "(none)")
    published_date = (plan.published.get("metadata") or {}).get("publication_date", "?")
    logger.info(
        "publication_date: %s (v1)  ->  %s (recomputed today)",
        published_date, (plan.config.metadata or {}).get("publication_date", "?"),
    )

    logger.info("")
    logger.info("METADATA DIFF (published -> rebuilt)")
    for key, verdict, detail in metadata_diff(
        plan.published.get("metadata") or {}, plan.config.metadata or {}
    ):
        if verdict == "same" and not args.verbose:
            continue
        logger.info("  %-22s %-8s %s", key, verdict, detail)

    logger.info("")
    logger.info("FILES TO UPLOAD (%d)", len(plan.files))
    for path in plan.files:
        p = Path(path)
        logger.info("  %-46s %12d bytes", p.name, p.stat().st_size)
    not_carried = sorted(remote_names - local_names)
    if not_carried:
        logger.warning(
            "  Present on the published version, NOT carried forward (%d): %s",
            len(not_carried), ", ".join(not_carried),
        )
    brand_new = sorted(local_names - remote_names)
    if brand_new:
        logger.info("  New in this package: %s", ", ".join(brand_new))

    logger.info("")
    logger.info("PLANNED CALL SEQUENCE")
    logger.info("  1. [READ ] GET  %srecords/%s", base, plan.record_id)
    logger.info("  2. [READ ] GET  %suser/records?q=metadata.title:...", base)
    logger.info("  3. [WRITE] POST %srecords/%s/versions   (no body)",
                base, plan.record_id)
    logger.info("  4. [READ ] GET  %srecords/{new_id}/draft/files  (expect 0)", base)
    logger.info("  5. [WRITE] PUT  %srecords/{new_id}/draft   keys=%s",
                base, ",".join(_PUT_ALLOWED_KEYS))
    logger.info("  6. [READ ] GET  %srecords/{new_id}/draft   (verify the PUT)", base)
    logger.info("  7. [WRITE] POST/PUT/POST  x%d files (init, content, commit)",
                len(plan.files))
    logger.info("  8. [READ ] GET  %srecords/{new_id}/draft/files  (completeness)", base)
    logger.info("  9. [WRITE] POST %srecords/{new_id}/draft/pids/doi  (best-effort)",
                base)
    logger.info(" 10. [WRITE] POST %srecords/{new_id}/draft/actions/publish   %s",
                base, "" if args.publish else "SKIPPED — no --publish")
    logger.info("")
    logger.info("  NOT CALLED BY DESIGN:")
    logger.info("    POST .../draft/actions/files-import   "
                "(the new version's file set is exactly the new package)")
    logger.info("    POST .../draft/actions/submit-review  "
                "(a manager's accept would publish it)")
    logger.info("")
    logger.info("Archive on success: %s", plan.archive_destination)


def row_needs_attention(row: Dict[str, object]) -> bool:
    """Tell whether the run should exit nonzero.

    Args:
        row: The completed report row.

    Returns:
        True unless the operation reached a clean terminal state.
    """
    return row["Verdict"] not in (
        VERSION_PLANNED, VERSION_CREATED, VERSION_PUBLISHED
    )


# ===================================================================
#  Mutation
# ===================================================================

def archive_new_version_staging(
    staging: Path, destination: Path
) -> Optional[Path]:
    """Move the staging folder to a version-suffixed archive folder.

    Deliberately NOT ``standalone_tasks.archive_staging_to_uploaded``:
    that one ``rmtree``s its destination, which for a versioned ESID would
    destroy the previous version's archive — the only local record of what
    that version contained.  This one refuses instead.

    Args:
        staging: The staging folder to move.
        destination: ``Uploaded_Data/ESID_NNN_Uploaded_<label>/``.

    Returns:
        The destination on success, or None when it was refused or failed
        (never fatal — Zenodo is already correct by this point).
    """
    try:
        if destination.exists():
            logger.error(
                "REFUSING to archive — %s already exists. The staging folder "
                "is left in place; move it by hand.", destination,
            )
            return None
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging), str(destination))
        logger.info("  Archived staging folder to: %s", destination)
        return destination
    except OSError as exc:
        logger.warning("  Could not archive the staging folder: %s", exc)
        return None


def execute(
    args: argparse.Namespace, credentials: Any
) -> Dict[str, object]:
    """Create (and optionally publish) the new version.

    Re-runs :func:`preflight` from scratch first — nothing from the
    reviewed dry run is trusted — then mutates in a fixed order, verifying
    after each step.

    Args:
        args: Parsed CLI arguments.
        credentials: Zenodo API credentials.

    Returns:
        The completed report row.
    """
    row, plan = preflight(args, credentials)
    if plan is None:
        logger.error(
            "REFUSING to create a new version of record %s — %s.",
            args.record_id, row["Verdict"],
        )
        return row

    state = read_state(plan.staging)
    adopted_id = str(state.get("new_version_record_id") or "")

    # --- 1. The new-version draft (single-shot, never retried) ---------
    if adopted_id:
        logger.info("  Adopting the new-version draft %s from a prior run.",
                    adopted_id)
        new_id = adopted_id
        try:
            version_draft = get_draft_record(credentials, new_id)
        except Exception as exc:  # noqa: BLE001
            # A lingering pending slot can make GET /draft 500.  The PUT
            # below is a full replace, so re-issuing it is safe and is the
            # repair — do not refuse.
            logger.warning(
                "  Could not read draft %s (%s) — re-issuing the metadata "
                "PUT anyway.", new_id, exc,
            )
            version_draft = None
        if version_draft is None:
            version_draft = {"pids": {}, "files": {"enabled": True}}
    else:
        try:
            version_draft = create_new_version_draft(credentials, plan.record_id)
        except Exception as exc:  # noqa: BLE001
            row["Verdict"] = VERSION_CREATE_AMBIGUOUS
            row["Notes"] = f"{type(exc).__name__}: {exc}"
            logger.error(
                "REFUSING to continue — the versions POST failed (%s). It MAY "
                "still have created a draft; re-run the dry run to find out.",
                exc,
            )
            return row
        new_id = str(version_draft.get("id") or "")
        if not new_id:
            row["Verdict"] = VERSION_CREATE_AMBIGUOUS
            row["Notes"] = "the versions POST returned no record id"
            return row
        # Persist the lineage BEFORE uploading, so a mid-upload death still
        # leaves the link between this folder and the new draft on disk.
        write_state(plan.staging, {
            "mode": NEW_VERSION_MODE,
            "new_version_record_id": new_id,
            "new_version_label": plan.new_label,
            "previous_record_id": plan.record_id,
            "previous_version_label": plan.old_label,
            "concept_doi": plan.concept_doi,
            "version_doi": "",
            "created_at": datetime.now().isoformat(timespec="seconds"),
        })
        logger.info("  New version draft created: %s", new_id)

    row["New Record ID"] = new_id
    row["New Version"] = plan.new_label

    # --- 2. The draft must start empty ---------------------------------
    if not adopted_id and list_draft_files(credentials, new_id):
        row["Verdict"] = NEW_DRAFT_NOT_EMPTY
        logger.error(
            "REFUSING to continue — draft %s already holds files.", new_id
        )
        return row

    # --- 3. Metadata PUT, then read it back ----------------------------
    metadata = dict(plan.config.metadata or {})
    metadata["version"] = plan.new_label
    payload = build_put_payload(version_draft, metadata)
    if args.dump_payload:
        Path(args.dump_payload).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        logger.info("  Wrote the PUT payload to %s", args.dump_payload)
    try:
        update_draft_metadata(credentials, new_id, payload)
    except Exception as exc:  # noqa: BLE001
        row["Verdict"] = METADATA_PUT_UNVERIFIED
        row["Notes"] = f"{type(exc).__name__}: {exc}"
        logger.error("REFUSING to upload — the metadata PUT failed: %s", exc)
        return row

    readback = get_draft_record(credentials, new_id) or {}
    rb_meta = readback.get("metadata") or {}
    mismatches = [
        f"{k}: sent {metadata.get(k)!r}, read back {rb_meta.get(k)!r}"
        for k in ("version", "title", "publication_date")
        if rb_meta.get(k) != metadata.get(k)
    ]
    if mismatches:
        row["Verdict"] = METADATA_PUT_UNVERIFIED
        row["Notes"] = "; ".join(mismatches)
        logger.error(
            "REFUSING to upload — the metadata did not read back as sent: %s",
            "; ".join(mismatches),
        )
        return row
    logger.info("  Metadata verified on the draft (version=%s).", plan.new_label)

    # --- 4. Upload the package -----------------------------------------
    result = upload_to_zenodo(
        files=plan.files,
        config=plan.config,
        existing_draft_id=new_id,
        title_guard=False,
        auto_publish=False,
        submit_review=False,
        state_file_path=None,
        request_log_path=None,
        upload_attempts=args.upload_attempts,
        known_md5s=plan.known_md5s,
        zip_filename=plan.zip_path.name,
    )
    if not result.get("successful"):
        row["Verdict"] = UPLOAD_FAILED
        row["Notes"] = (result.get("error") or {}).get("error_message", "unknown")
        logger.error("Upload failed: %s", row["Notes"])
        return row

    # --- 5. Completeness, both directions ------------------------------
    entries = list_draft_files(credentials, new_id)
    committed = {
        e.get("key"): e for e in entries if e.get("status") == "completed"
    }
    expected = {Path(p).name: Path(p).stat().st_size for p in plan.files}
    missing = sorted(set(expected) - set(committed))
    extra = sorted({e.get("key") for e in entries} - set(expected))
    wrong_size = sorted(
        name for name, entry in committed.items()
        if name in expected and entry.get("size") not in (None, expected[name])
    )
    if missing or extra or wrong_size:
        row["Verdict"] = INCOMPLETE_ON_RECORD
        row["Notes"] = "; ".join(filter(None, [
            f"missing: {', '.join(missing[:5])}" if missing else "",
            f"unexpected on record: {', '.join(extra[:5])}" if extra else "",
            f"wrong size: {', '.join(wrong_size[:5])}" if wrong_size else "",
        ]))
        logger.error("REFUSING to publish — %s", row["Notes"])
        return row

    row["Files Uploaded"] = len(expected)
    row["Verdict"] = VERSION_CREATED
    row["Action Taken"] = f"created draft {new_id}, uploaded {len(expected)} file(s)"

    # --- 6. DOI, then publish (only when asked) ------------------------
    ensure_doi_reserved(credentials, new_id)

    if not args.publish:
        logger.info(
            "  Draft %s is complete and NOT published (no --publish). "
            "Inspect it, then publish.", new_id,
        )
        return row

    publish_draft(credentials, new_id)
    confirmed = get_published_record(credentials, new_id)
    if confirmed is None:
        row["Verdict"] = VERSION_CREATED
        row["Notes"] = (
            "publish was sent but the record does not read back as published "
            "— check the Zenodo UI before re-running"
        )
        logger.error("  %s", row["Notes"])
        return row
    row.update({
        "Published": "yes",
        "Version DOI": _version_doi(confirmed),
        "Concept DOI": _concept_doi(confirmed) or plan.concept_doi,
        "Verdict": VERSION_PUBLISHED,
        "Action Taken": f"published {new_id} as {plan.new_label}",
    })
    write_state(plan.staging, {"version_doi": _version_doi(confirmed)})
    logger.info(
        "  PUBLISHED %s as version %s (DOI %s)",
        new_id, plan.new_label, _version_doi(confirmed) or "?",
    )

    archived = archive_new_version_staging(
        plan.staging, plan.archive_destination
    )
    if archived is None:
        row["Notes"] = "; ".join(filter(None, [
            str(row["Notes"]), "staging folder not archived — move it by hand",
        ]))
    return row


# ===================================================================
#  Main
# ===================================================================

def main() -> None:
    """Command-line entry point.  See the module docstring for details."""
    parser = argparse.ArgumentParser(
        description=(
            "Publish a re-prepped staging package as a NEW VERSION of an "
            "already-published Zenodo record. Dry-run by default; nothing "
            "is created without --execute, and nothing is published "
            "without --publish."
        ),
    )
    parser.add_argument("--esid", required=True, metavar="ESID",
                        help="The ESID to version (e.g. 073, 122_Part_1_of_2).")
    parser.add_argument("--record-id", required=True, metavar="ID",
                        help="Record id of the PUBLISHED version being superseded.")
    parser.add_argument("--execute", action="store_true",
                        help="Create the new version. Without this, the tool "
                             "only reports (two read-only GETs).")
    parser.add_argument("--publish", action="store_true",
                        help="Publish the new version after the completeness "
                             "gate. OFF by default: an unpublished draft can "
                             "be discarded, a published version cannot.")
    parser.add_argument("--yes", action="store_true",
                        help="Skip the interactive confirmation that "
                             "--execute otherwise requires. Needed for "
                             "unattended runs.")
    parser.add_argument("--version-label", default=None, metavar="LABEL",
                        help="Set the new version label explicitly instead of "
                             "advancing the trailing letter.")
    parser.add_argument("--allow-title-change", action="store_true",
                        help="Permit a rebuilt title that differs from the "
                             "published one. Normally a mismatch means the "
                             "wrong --record-id was paired with this ESID.")
    parser.add_argument("--skip-integrity-hash", action="store_true",
                        help="Skip the full ZIP re-hash in the local "
                             "integrity gate (structural checks still run).")
    parser.add_argument("--upload-attempts", type=int, default=3, metavar="N",
                        help="PUT attempts for the data ZIP (default: 3).")
    parser.add_argument("--config", default=str(
        _PROJECT_ROOT / "Resources" / "config.json"), metavar="PATH",
        help="AZUS config.json (collectors CSV, project config, citations).")
    parser.add_argument("--dump-payload", default=None, metavar="PATH",
                        help="Write the exact PUT body to PATH for inspection.")
    parser.add_argument("--output", default=None, metavar="PATH",
                        help="CSV report path (default: timestamped, in cwd).")
    parser.add_argument("--verbose", action="store_true",
                        help="Include unchanged metadata keys in the diff.")
    args = parser.parse_args()

    if args.upload_attempts < 1:
        parser.error(f"--upload-attempts must be >= 1 (got {args.upload_attempts}).")
    if args.publish and not args.execute:
        parser.error("--publish requires --execute.")
    try:
        args.esid = azus_common.normalize_esid(args.esid)
    except ValueError as exc:
        parser.error(str(exc))

    azus_common.configure_logging(args.verbose)

    try:
        credentials = get_credentials_from_env()
    except ValueError as exc:
        logger.error("%s", exc)
        logger.error("Run: source Resources/set_env.sh")
        sys.exit(2)
    if not credentials.base_url.endswith("/"):
        logger.error(
            "INVENIO_RDM_BASE_URL is %r — it MUST end with '/', or every URL "
            "this tool builds is malformed (e.g. '%srecords/%s'). Fix the "
            "export and re-run.",
            credentials.base_url, credentials.base_url, args.record_id,
        )
        sys.exit(2)

    output_path = (
        Path(args.output) if args.output
        else azus_common.timestamped_output_path("new_version_upload")
    )
    is_sandbox = "sandbox" in credentials.base_url.lower()

    logger.info("=" * 70)
    logger.info(
        "NEW VERSION UPLOAD — %s",
        "EXECUTE (a new Zenodo version WILL be created)" if args.execute
        else "DRY RUN (nothing will be created)",
    )
    logger.info("=" * 70)
    logger.info("Endpoint:   %s   <<< %s >>>",
                credentials.base_url, "SANDBOX" if is_sandbox else "PRODUCTION")
    logger.info("ESID:       %s", args.esid)
    logger.info("Record:     %s", args.record_id)
    logger.info("Publish:    %s", "YES" if args.publish else "no (draft only)")
    logger.info("Output:     %s", output_path)
    logger.info("=" * 70)

    row, plan = preflight(args, credentials)
    if plan is not None:
        log_plan(plan, credentials, args)

    if plan is not None and args.execute:
        if not args.yes:
            if not sys.stdin.isatty():
                logger.error(
                    "stdin is not a terminal, so the confirmation cannot be "
                    "answered. Re-run with --yes for unattended use."
                )
                sys.exit(2)
            print(f"\n⚠️  This will create a NEW VERSION of record "
                  f"{args.record_id} on "
                  f"{'SANDBOX' if is_sandbox else 'PRODUCTION'} Zenodo.")
            if args.publish:
                print("   It WILL be published. Publication is permanent.")
            if input("\nProceed? (yes/no): ").strip().lower() != "yes":
                logger.info("Cancelled by user.")
                sys.exit(0)
        row = execute(args, credentials)
    elif plan is not None:
        row["Action Taken"] = "none (dry run)"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS)
        writer.writeheader()
        writer.writerow(row)

    logger.info("=" * 70)
    logger.info("Verdict: %s — %s", row["Verdict"],
                _RECOMMENDED_ACTION.get(str(row["Verdict"]), "review"))
    if row["Notes"]:
        logger.warning("Notes:   %s", row["Notes"])
    logger.info("Report:  %s", output_path)
    if not args.execute and row["Verdict"] == VERSION_PLANNED:
        logger.warning(
            "DRY RUN — 0 writes performed. Review the metadata diff and the "
            "file plan above, then re-run with --execute."
        )
    logger.info("=" * 70)
    sys.exit(1 if row_needs_attention(row) else 0)


if __name__ == "__main__":
    main()
