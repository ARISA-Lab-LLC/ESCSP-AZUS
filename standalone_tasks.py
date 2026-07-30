#!/usr/bin/env python3
"""AZUS Standalone Upload Pipeline.

This module contains the complete upload pipeline for AZUS: dataset discovery,
metadata construction, Zenodo upload orchestration, and result tracking.

All project-specific identity (creators, contributors, funding, etc.) is read
from ``Resources/project_config.json``.  No Eclipse Soundscapes–specific data
is hardcoded in this file.

Usage:
    python standalone_tasks.py [--config Resources/config.json] [--dry-run]
    python standalone_tasks.py --config Resources/config.json --esid 004
    python standalone_tasks.py --config Resources/config.json --esid 004 007 012
    python standalone_tasks.py --save-terminal-output   # tee screen to Records/*.txt

Design notes for future Prefect integration:
    Every public function in this module is a plain synchronous function.
    To convert to Prefect tasks, simply decorate them with ``@task`` and
    call them from a ``@flow``-decorated orchestrator.  The ``upload_datasets``
    function is the natural entry point for a Prefect flow.
"""

# ---------------------------------------------------------------------------
# Standard library
# ---------------------------------------------------------------------------
import argparse
import concurrent.futures
import csv
import glob
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import zipfile
from pathlib import Path
from string import Template
from typing import Any, Dict, List, Optional, TextIO, Tuple

# ---------------------------------------------------------------------------
# Project models (no external dependencies beyond Pydantic)
# ---------------------------------------------------------------------------
from models.invenio import (
    Metadata,
    Identifier,
    PersonOrganization,
    Affiliation,
    Role,
    Creator,
    Contributor,
    ResourceType,
    License,
    Language,
    Date,
    DateType,
    Funder,
    Award,
    AwardTitle,
    Funding,
    Subject,
    RelationType,
    RelatedIdentifier,
    Reference,
)
from models.audiomoth import (
    DatasetCategory,
    EclipseType,        # backward-compatible alias
    DataCollector,
    UploadData,
    PersistedResult,
    DraftConfig,
    Access,
)
# Shared helpers live in Resources/ (see azus_common.py).
sys.path.insert(0, str(Path(__file__).resolve().parent / "Resources"))
import azus_common  # noqa: E402
# The ZIP-layout verdict is imported FROM THE PRODUCER: prepare_dataset.py
# owns the canonical answer to "which layout did prep write?" right next to
# the code that writes it.  Re-deriving it here is what
# prepare_dataset.expected_day_zip_names' docstring forbids — a folder must
# never be grouped one way and read another.  Same convention as
# Resources/audit_prep_completeness.py.
import prepare_dataset as _prep_contract  # noqa: E402

from standalone_uploader import (
    upload_to_zenodo,
    get_credentials_from_env,
    # The same hardened title search the uploader's duplicate guard uses,
    # so --skip-existing-records and the guard agree about what exists.
    _search_drafts_by_title,
    _PUT_RETRY_ATTEMPTS as _DEFAULT_UPLOAD_ATTEMPTS,
)

# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------
logger = logging.getLogger("azus")

# Date format used in Zenodo metadata
UPLOAD_DATE_FORMAT = "%Y-%m-%d"

# SHA-512 read buffer — 64 KB gives good throughput on large files
_HASH_BUFFER_SIZE = 65_536


# ===================================================================
#  Project configuration loader
# ===================================================================

def load_project_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """Load the project identity configuration from a JSON file.

    The config file contains all project-specific metadata: creators,
    contributors, funding, community ID, custom fields, CSV header
    expectations, and default file lists.

    Args:
        config_path: Path to the project config JSON.  Defaults to
            ``Resources/project_config.json`` relative to this script.

    Returns:
        Parsed JSON dictionary.

    Raises:
        FileNotFoundError: If the config file does not exist.
        json.JSONDecodeError: If the file is not valid JSON.
    """
    if config_path is None:
        config_path = str(
            Path(__file__).parent / "Resources" / "project_config.json"
        )

    config_file = Path(config_path)
    if not config_file.exists():
        raise FileNotFoundError(
            f"Project config not found: {config_file}\n"
            f"Copy templates/project_config.json.example to "
            f"Resources/project_config.json and fill in your project details."
        )

    with open(config_file, "r", encoding="utf-8") as fh:
        config = json.load(fh)

    logger.info("Loaded project config: %s", config_file.name)
    return config


# ===================================================================
#  Metadata builders — driven by project_config.json
# ===================================================================

def _build_person_or_org(entry: Dict[str, Any]) -> PersonOrganization:
    """Build a PersonOrganization model from a config entry.

    Args:
        entry: Dictionary with keys: type, name/given_name/family_name, orcid.

    Returns:
        PersonOrganization Pydantic model.
    """
    identifiers = None
    if entry.get("orcid"):
        identifiers = [Identifier(scheme="orcid", identifier=entry["orcid"])]

    return PersonOrganization(
        type=entry["type"],
        given_name=entry.get("given_name"),
        family_name=entry.get("family_name"),
        name=entry.get("name"),
        identifiers=identifiers,
    )


def build_creators(project_config: Dict[str, Any]) -> List[Creator]:
    """Build the list of Zenodo creators from project configuration.

    Args:
        project_config: Parsed project_config.json.

    Returns:
        List of Creator models.
    """
    creators = []
    for entry in project_config.get("creators", []):
        # Filter out empty strings — Zenodo rejects {"name": ""} affiliations
        affiliations = [
            Affiliation(name=aff)
            for aff in entry.get("affiliations", [])
            if aff and aff.strip()
        ]
        creators.append(
            Creator(
                person_or_org=_build_person_or_org(entry),
                role=Role(id=entry.get("role", "other")),
                affiliations=affiliations if affiliations else None,
            )
        )
    return creators


def build_contributors(project_config: Dict[str, Any]) -> List[Contributor]:
    """Build the list of Zenodo contributors from project configuration.

    Args:
        project_config: Parsed project_config.json.

    Returns:
        List of Contributor models.
    """
    contributors = []
    for entry in project_config.get("contributors", []):
        # Filter out empty strings — Zenodo rejects {"name": ""} affiliations
        affiliations = [
            Affiliation(name=aff)
            for aff in entry.get("affiliations", [])
            if aff and aff.strip()
        ]
        contributors.append(
            Contributor(
                person_or_org=_build_person_or_org(entry),
                role=Role(id=entry.get("role", "other")),
                affiliations=affiliations if affiliations else None,
            )
        )
    return contributors


def build_fundings(project_config: Dict[str, Any]) -> List[Funding]:
    """Build the list of funding entries from project configuration.

    Args:
        project_config: Parsed project_config.json.

    Returns:
        List of Funding models.
    """
    fundings = []
    for entry in project_config.get("funding", []):
        # Build award identifiers (typically a URL)
        award_identifiers = None
        if entry.get("award_url"):
            award_identifiers = [
                Identifier(scheme="url", identifier=entry["award_url"])
            ]

        fundings.append(
            Funding(
                funder=Funder(id=entry.get("funder_id")),
                award=Award(
                    title=AwardTitle(en=entry.get("award_title", "")),
                    number=entry.get("award_number"),
                    identifiers=award_identifiers,
                ),
            )
        )
    return fundings


# ===================================================================
#  Utility functions
# ===================================================================

# Shared streaming SHA-512 (one definition for the whole suite).
calculate_sha512 = azus_common.calculate_sha512


def parse_values_from_str(string: str, delimiter: str = ":") -> List[str]:
    """Split a delimited string and strip whitespace from each value.

    Args:
        string: Input string (e.g., "value1 : value2 : value3").
        delimiter: Separator character.

    Returns:
        List of stripped strings.
    """
    return [value.strip() for value in string.split(sep=delimiter)]


# ===================================================================
#  Pre-upload integrity verification
# ===================================================================

# Written by Resources/prepare_dataset.py as its very last action; its
# absence means preparation never finished (or predates the sentinel).
_PREP_SENTINEL_NAME = azus_common.PREP_SENTINEL

# Set when the user interrupts a concurrent run (Ctrl+C).  Workers check
# it before starting a dataset and the uploader checks it between files,
# so an interrupt takes effect at the next file boundary instead of
# after hours of queued work.  Interrupted drafts stay resumable.
_ABORT_EVENT = threading.Event()


# ===================================================================
#  Terminal-output tee (--save-terminal-output)
# ===================================================================

# Log format shared by the screen/log handlers AND the tee files, so the
# saved .txt files read exactly like the terminal did.
_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

# The active tee handler for this run, or None unless the
# --save-terminal-output flag was given.  Module-level (like
# _ABORT_EVENT) so _process_one_dataset can route its own dataset's
# output without threading the handler through every caller.
_TEE_HANDLER: Optional["_TerminalTeeHandler"] = None


class _TerminalTeeHandler(logging.Handler):
    """Tee the run's screen output into per-phase ``.txt`` review files.

    Attached to the ROOT logger alongside the normal screen and
    ``azus_upload.log`` handlers (which are untouched — the screen
    output never changes), this handler writes a copy of every log
    line into one of three kinds of file in the ``Records/`` folder,
    all sharing one run-start timestamp (``YYYY-MM-DD_HHMM``):

    * ``<stamp>_Standalone_terminal_output_pre.txt`` — everything
      logged before the first dataset upload attempt begins.
    * ``<stamp>_ESID_<esid>_upload_attempt_<NNN>.txt`` — everything one
      dataset's attempt logs (integrity gate, upload, archive move,
      result write), including the un-prefixed ``standalone_uploader``
      lines.  ``NNN`` starts at ``001`` and increments until the name
      does not collide with an existing file, so nothing is ever
      overwritten.
    * ``<stamp>_Standalone_terminal_output_post.txt`` — everything
      logged after upload attempts have started that belongs to no
      specific dataset (normally the final summary block).

    Lifecycle: the handler starts in a buffering state because the
    Records/ folder is only known once the configuration file has been
    read; :meth:`activate` then opens the pre file and flushes the
    buffer.  :meth:`begin_esid` / :meth:`end_esid` bracket one
    dataset's attempt — each attempt runs entirely within one thread
    (the main thread for ``--workers 1``, one pool thread otherwise),
    so a ``threading.local`` slot is enough to route every log record
    the attempt emits, whichever module logged it.  The first
    :meth:`begin_esid` closes the pre file.  :meth:`close` (invoked by
    ``logging.shutdown()`` at exit) closes whatever is still open.

    A tee failure (unwritable Records/ folder, disk full) must never
    crash or stop an upload run: the handler disables itself, reports
    the problem once through the normal logging path, and the run
    continues with screen output only.
    """

    def __init__(self) -> None:
        """Create the handler in its pre-activation buffering state.

        Args:
            None.

        Returns:
            None.
        """
        super().__init__()
        self._records_dir: Optional[Path] = None
        self._stamp: str = ""
        self._buffer: List[str] = []
        self._pre_file: Optional[TextIO] = None
        self._post_file: Optional[TextIO] = None
        self._uploads_started = False
        self._disabled = False
        # Attempt files still open, so close() can sweep up files of
        # worker threads that never reached end_esid (e.g. Ctrl+C).
        self._open_esid_files: set = set()
        self._local = threading.local()

    def activate(self, records_dir: Path, stamp: str) -> None:
        """Open the pre file in ``records_dir`` and flush buffered lines.

        Called from :func:`main` as soon as the configuration file has
        been read (the Records/ folder is derived from it).  Everything
        logged between handler attachment and this call was buffered in
        memory and is written to the pre file first, so the pre file
        starts at the very first log line of the run.

        Args:
            records_dir: Directory that receives all tee files (the
                ``Records/`` folder; created if missing).
            stamp: Run-start timestamp, ``YYYY-MM-DD_HHMM`` — shared by
                every file this run writes.

        Returns:
            None.  On failure (directory not creatable, pre file not
            writable) the tee disables itself and logs one warning;
            the run itself is never interrupted.
        """
        self.acquire()
        warn: Optional[BaseException] = None
        try:
            if self._disabled or self._records_dir is not None:
                return
            try:
                records_dir = Path(records_dir)
                records_dir.mkdir(parents=True, exist_ok=True)
                self._stamp = stamp
                self._records_dir = records_dir
                self._pre_file = self._open_phase_file("pre")
                for line in self._buffer:
                    self._pre_file.write(line + "\n")
                self._buffer.clear()
            except OSError as exc:
                self._disabled = True
                self._records_dir = None
                self._pre_file = None
                self._buffer.clear()
                warn = exc
        finally:
            self.release()
        if warn is not None:
            logger.warning(
                "--save-terminal-output disabled — could not open the "
                "terminal-output file in %s: %s", records_dir, warn,
            )

    def begin_esid(self, esid: str) -> None:
        """Start routing this thread's log output to an ESID attempt file.

        The first call also flips the run from the "pre" phase to the
        "uploads started" phase and closes the pre file (under the
        handler lock, so no thread can be caught mid-write to it).

        Args:
            esid: The dataset's ESID — used in the attempt filename
                ``<stamp>_ESID_<esid>_upload_attempt_<NNN>.txt``.

        Returns:
            None.  If the attempt file cannot be opened, this thread's
            output falls through to the post file instead — the upload
            itself is never interrupted.
        """
        self.acquire()
        try:
            if self._disabled or self._records_dir is None:
                return
            if not self._uploads_started:
                self._uploads_started = True
                if self._pre_file is not None:
                    self._pre_file.close()
                    self._pre_file = None
            try:
                handle: Optional[TextIO] = self._open_attempt_file(esid)
            except OSError:
                handle = None
            if handle is not None:
                self._open_esid_files.add(handle)
            self._local.esid_file = handle
        finally:
            self.release()

    def end_esid(self) -> None:
        """Stop per-ESID routing for this thread and close its file.

        Safe to call when :meth:`begin_esid` never ran on this thread
        (it is a no-op then), so callers can put it in a ``finally``.

        Args:
            None.

        Returns:
            None.
        """
        self.acquire()
        try:
            handle = getattr(self._local, "esid_file", None)
            self._local.esid_file = None
            if handle is not None:
                self._open_esid_files.discard(handle)
                try:
                    handle.close()
                except OSError:
                    pass
        finally:
            self.release()

    def mirror(self, text: str) -> None:
        """Copy one non-logging screen line (a ``print``) into the tee.

        The interactive confirmation prompt is the only screen output
        that bypasses the logging module; :func:`main` mirrors those
        lines here so the pre file is a complete record of the screen.

        Args:
            text: The line exactly as it appeared on screen (without
                the trailing newline).

        Returns:
            None.  A write failure disables the tee (reported once via
            a normal log warning) rather than interrupting the run.
        """
        if self._disabled:
            return
        self.acquire()
        warn: Optional[BaseException] = None
        try:
            try:
                self._write_phase_line(text)
            except OSError as exc:
                self._disabled = True
                warn = exc
        finally:
            self.release()
        if warn is not None:
            logger.warning(
                "--save-terminal-output disabled — could not write to "
                "the terminal-output file: %s", warn,
            )

    def emit(self, record: logging.LogRecord) -> None:
        """Write one formatted log record to the appropriate tee file.

        Called by the logging framework (under the handler lock) for
        every record that reaches the root logger.  Routing: a thread
        inside :meth:`begin_esid`/:meth:`end_esid` writes to its ESID
        attempt file; any other thread writes to the pre file (or the
        in-memory buffer before :meth:`activate`) until uploads start,
        and to the post file afterwards.

        Args:
            record: The log record to tee.

        Returns:
            None.  Failures are reported through the standard
            :meth:`logging.Handler.handleError` path and never
            propagate into the upload run.
        """
        if self._disabled:
            return
        try:
            message = self.format(record)
            esid_file = getattr(self._local, "esid_file", None)
            if esid_file is not None:
                esid_file.write(message + "\n")
            else:
                self._write_phase_line(message)
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        """Close every tee file that is still open.

        Invoked by ``logging.shutdown()`` at interpreter exit, which
        covers every exit path of :func:`main` — including the log
        lines written after the summary block and straggler worker
        threads after a Ctrl+C.

        Args:
            None.

        Returns:
            None.
        """
        self.acquire()
        try:
            for handle in list(self._open_esid_files):
                try:
                    handle.close()
                except OSError:
                    pass
            self._open_esid_files.clear()
            for handle in (self._pre_file, self._post_file):
                if handle is not None:
                    try:
                        handle.close()
                    except OSError:
                        pass
            self._pre_file = None
            self._post_file = None
            self._buffer.clear()
        finally:
            self.release()
        super().close()

    def _write_phase_line(self, text: str) -> None:
        """Write one line to the current non-ESID phase destination.

        The caller must hold the handler lock (:meth:`emit` is called
        under it by the framework; :meth:`mirror` acquires it).

        Args:
            text: The formatted line to write (no trailing newline).

        Returns:
            None.

        Raises:
            OSError: If the phase file cannot be opened or written —
                callers convert this into a disable-and-warn, never a
                crash.
        """
        if self._records_dir is None:
            self._buffer.append(text)
        elif not self._uploads_started:
            if self._pre_file is not None:
                self._pre_file.write(text + "\n")
        else:
            if self._post_file is None:
                self._post_file = self._open_phase_file("post")
            self._post_file.write(text + "\n")

    def _open_phase_file(self, kind: str) -> TextIO:
        """Open the pre or post file, never overwriting an existing one.

        The first choice is ``<stamp>_Standalone_terminal_output_<kind>.txt``;
        if a file of that name already exists (two runs started in the
        same minute writing to the same Records/ folder), ``_002``,
        ``_003``, … is appended to the stem until the exclusive create
        succeeds.

        Args:
            kind: ``"pre"`` or ``"post"``.

        Returns:
            The opened, line-buffered text file.

        Raises:
            OSError: If no candidate file can be created.
        """
        base = f"{self._stamp}_Standalone_terminal_output_{kind}"
        counter = 1
        while True:
            name = f"{base}.txt" if counter == 1 else f"{base}_{counter:03d}.txt"
            try:
                return open(
                    self._records_dir / name, "x",
                    encoding="utf-8", buffering=1,
                )
            except FileExistsError:
                counter += 1

    def _open_attempt_file(self, esid: str) -> TextIO:
        """Open a per-ESID attempt file with the next free counter.

        Tries ``<stamp>_ESID_<esid>_upload_attempt_001.txt`` first and
        increments the counter until a name does not collide with an
        existing file, so an earlier attempt's record is never
        overwritten.

        Args:
            esid: The dataset's ESID (used verbatim in the filename).

        Returns:
            The opened, line-buffered text file.

        Raises:
            OSError: If no candidate file can be created.
        """
        counter = 1
        while True:
            name = (
                f"{self._stamp}_ESID_{esid}_upload_attempt_{counter:03d}.txt"
            )
            try:
                return open(
                    self._records_dir / name, "x",
                    encoding="utf-8", buffering=1,
                )
            except FileExistsError:
                counter += 1

# How many offending filenames to name in an integrity problem message
# before collapsing the rest into a count.
_INTEGRITY_EXAMPLE_LIMIT = 5


def _summarize_names(names: List[str]) -> str:
    """Format a filename list for an error message, capping the examples.

    Args:
        names: Filenames to render.  Only the first
            ``_INTEGRITY_EXAMPLE_LIMIT`` are named individually.

    Returns:
        A comma-joined string of the first few names; when the list is
        longer, the remainder is collapsed into a ``... (N total)`` tail.
    """
    shown = ", ".join(names[:_INTEGRITY_EXAMPLE_LIMIT])
    if len(names) > _INTEGRITY_EXAMPLE_LIMIT:
        shown += f", ... ({len(names)} total)"
    return shown


def resolve_dataset_archives(
    staging_folder: Path, esid: str
) -> Tuple[List[Path], Optional[str], List[str]]:
    """Enumerate a prepared folder's data archives and report its layout.

    The single seam every upload-side consumer goes through to answer
    "what are this dataset's archives?".  Before this existed the answer
    was re-derived by ``glob("ESID_*.zip")``, which admits hand-made
    names like ``ESID_005_backup.zip`` as data archives; routing through
    ``azus_common.parse_day_zip_name`` applies the same strict grammar
    prep used to write the names.

    Enumeration follows ``audit_wav_integrity.locate_zips``: the legacy
    single archive if present, then every per-day archive whose parsed
    ESID matches exactly.  Name order is day order — the ``YYYY_MM_DD``
    tail is fixed-width and the ESID prefix is constant.

    Args:
        staging_folder: The prepared staging (or uploaded) folder.
        esid: Canonical padded ESID string, e.g. ``"005"``.  A
            non-canonical value makes every per-day archive invisible,
            which is the same precondition ``staging_zip_mode`` carries.

    Returns:
        A ``(archives, mode, problems)`` tuple.  ``mode`` is one of
        ``prepare_dataset.ZIP_MODE_SINGLE`` / ``ZIP_MODE_PER_DAY`` /
        ``ZIP_MODE_MIXED``, or None when the folder holds no data
        archive.  ``problems`` is empty when the folder is coherent and
        usable; a non-empty list means the caller must refuse the
        dataset without opening anything.
    """
    problems: List[str] = []
    mode = _prep_contract.staging_zip_mode(staging_folder, esid)

    archives: List[Path] = []
    single = staging_folder / _prep_contract.SINGLE_ZIP_NAME_TEMPLATE.format(
        esid=esid
    )
    if single.is_file():
        archives.append(single)
    try:
        for entry in sorted(staging_folder.iterdir()):
            if not entry.is_file():
                continue
            parsed = azus_common.parse_day_zip_name(entry.name)
            if parsed is not None and parsed[0] == esid:
                archives.append(entry)
    except OSError as exc:
        return [], None, [
            f"Staging folder is unreadable ({type(exc).__name__}: {exc}): "
            f"{staging_folder.name}"
        ]

    if mode == _prep_contract.ZIP_MODE_MIXED:
        problems.append(
            f"{staging_folder.name} holds BOTH the legacy "
            f"{single.name} and per-day archives — a mixed layout no prep "
            "produces. Re-prep the folder, or verify it with "
            "Resources/audit_prep_completeness.py."
        )
    elif mode is None:
        problems.append(
            f"No data archive in {staging_folder.name} — neither "
            f"{single.name} nor any ESID_{esid}_YYYY_MM_DD.zip."
        )

    return archives, mode, problems


def read_staged_version(staging_folder: Path) -> Optional[str]:
    """Read the dataset version prep recorded in the staging folder.

    Prep writes the site's collector row to the staging folder's own
    ``total_eclipse_data.csv``, and for the per-day layout it appends
    ``prepare_dataset.DAY_ZIP_VERSION_SUFFIX`` to the version first, so
    a per-day dataset is unambiguously marked (``2024.1.0`` ->
    ``2024.1.0A``).  Reading it here is what carries that marker onto the
    Zenodo record: ``get_draft_config`` otherwise sources the version
    from the MASTER collectors spreadsheet, which prep never writes back
    to.  Prep stays the single authority for the marker; this only
    consumes what prep already wrote.

    The column name is matched case-tolerantly, the same way prep writes
    it.

    Args:
        staging_folder: The prepared folder to read.

    Returns:
        The recorded version string, or None when the file is absent,
        unreadable, empty, or has no version column — in which case the
        caller keeps the collectors-CSV value.
    """
    staged_csv = staging_folder / "total_eclipse_data.csv"
    if not staged_csv.is_file():
        return None
    try:
        with open(staged_csv, "r", encoding="utf-8") as fh:
            row = next(csv.DictReader(fh), None)
    except (OSError, csv.Error) as exc:
        logger.warning(
            "Could not read %s (%s) — falling back to the collectors CSV "
            "for the record version.", staged_csv.name, exc,
        )
        return None
    if not row:
        return None
    for key, value in row.items():
        if key and key.strip().casefold() == "version":
            return (value or "").strip() or None
    return None


def verify_dataset_integrity(
    staging_folder: str,
    esid: str,
    archives: Optional[List[str]] = None,
    verify_zip_hash: bool = True,
    digests_out: Optional[Dict[str, Dict[str, str]]] = None,
) -> List[str]:
    """Verify a prepared dataset's integrity BEFORE any upload work.

    Broken ZIPs (missing WAV files) have been uploaded to Zenodo because
    nothing in the upload path ever checked what prepare_dataset.py
    produced.  This gate cross-checks the staging folder against the
    integrity records prep already writes, cheapest check first:

    1. The ``.prep_complete`` sentinel must exist in the staging folder
       (prepare_dataset.py touches it as its very last action).
    2. The layout must be coherent — a folder holding both the legacy
       archive and per-day archives is refused before anything is opened.
    3. Every archive must be a readable ZIP.
    4. Every WAV listed in the staging folder's ``file_list.csv`` (the
       external manifest, which carries per-file sizes and SHA-512
       hashes) must be present in the archive that OWNS it, with a
       matching uncompressed size — and no archive may hold a WAV the
       manifest does not list.  In the per-day layout each archive owns
       one day, so ownership is re-derived per WAV with
       ``azus_common.wav_day_key`` -> ``azus_common.day_zip_name``.  The
       ``Notes`` column also records this mapping, but it is free text
       for humans and is never parsed.
    5. Each archive's own SHA-512 must match the hash recorded in its
       ``file_list.csv`` row (skippable via ``verify_zip_hash=False`` /
       the ``--skip-integrity-hash`` CLI flag; the structural checks
       above always run).

    Both directions are checked, because each catches a different real
    failure.  A manifest listing WAVs for a day whose archive is absent
    is how an interrupted prep looks: the old single-archive comparison
    passed such a folder whenever exactly one day had been written.  An
    archive present with no manifest row is how a stale archive from an
    earlier failed prep looks.

    Args:
        staging_folder: The prepared folder holding the archives and
            ``file_list.csv``.  NOT an archive path — the subject of this
            check has always been the folder.
        esid: Canonical padded ESID string, needed to derive which
            archive owns a given WAV.
        archives: The dataset's archives, normally supplied by
            ``resolve_dataset_archives`` so the set verified is exactly
            the set uploaded.  When None they are resolved here.
        verify_zip_hash: When True (default), re-hash each archive and
            compare against the manifest.  Costs one full read per
            archive — small next to the hours-long upload it protects.
        digests_out: Optional dict the caller supplies to receive the
            digests computed during the hash step, keyed by archive
            basename with ``{"sha512": ..., "md5": ...}`` values.  Filled
            only when the hash step runs and EVERY archive passes; both
            digests come from one read, and the md5s become the
            uploader's ``known_md5s`` so it need not re-read the
            archives.

    Returns:
        List of human-readable problem strings.  Empty list = verified.
        Any problem must fail the dataset — never upload past one.  A bad
        dataset is REPORTED, not raised: the caller decides, and it fails
        closed.

    Raises:
        ValueError: If ``staging_folder`` is a file rather than a
            directory — i.e. a caller passed an archive path.  This is a
            programming error, not a bad dataset, so it is raised rather
            than returned as a problem.
    """
    problems: List[str] = []
    staging_dir = Path(staging_folder)

    # A caller that still passes an archive path would otherwise get a
    # confusing "no sentinel" problem and look like a legitimately failing
    # dataset.  Fail loudly instead of silently mis-verifying.
    if staging_dir.is_file():
        raise ValueError(
            "verify_dataset_integrity takes the staging FOLDER, not an "
            f"archive path (got {staging_dir.name})"
        )

    # --- 1. Prep-completion sentinel ---
    if not (staging_dir / _PREP_SENTINEL_NAME).is_file():
        problems.append(
            f"No {_PREP_SENTINEL_NAME} sentinel in {staging_dir.name} — "
            "preparation never completed (or the folder predates the "
            "sentinel). Re-run Resources/prepare_dataset.py, or verify "
            "and back-fill with Resources/audit_prep_completeness.py."
        )

    # --- 2. Layout must be coherent before anything is opened ---
    if archives is None:
        resolved, mode, layout_problems = resolve_dataset_archives(
            staging_dir, esid
        )
        archive_paths = resolved
    else:
        archive_paths = [Path(a) for a in archives]
        _, mode, layout_problems = resolve_dataset_archives(staging_dir, esid)
    if layout_problems:
        # A mixed or archive-less folder cannot be verified at all, and a
        # mixed one must never be read: refuse before opening a 43 GB file.
        return problems + layout_problems

    per_day = mode == _prep_contract.ZIP_MODE_PER_DAY

    # --- 3. Read each archive's WAV entries (basename -> uncompressed size) ---
    # Entries live under a subfolder inside the archive, so key on basename.
    archive_wav_sizes: Dict[str, Dict[str, int]] = {}
    for archive in archive_paths:
        try:
            with zipfile.ZipFile(archive, "r") as zf:
                zip_infos = zf.infolist()
        except (zipfile.BadZipFile, OSError) as exc:
            problems.append(
                f"ZIP is not a readable archive "
                f"({type(exc).__name__}: {exc}): {archive.name}"
            )
            continue  # other archives are still worth checking
        sizes: Dict[str, int] = {}
        for info in zip_infos:
            basename = info.filename.rsplit("/", 1)[-1]
            if basename.lower().endswith(".wav"):
                sizes[basename] = info.file_size
        archive_wav_sizes[archive.name] = sizes

    if not archive_wav_sizes:
        return problems  # nothing readable — nothing further can be checked

    # --- 4. Cross-check against prep's manifest (file_list.csv) ---
    file_list_path = staging_dir / "file_list.csv"
    # Archive basename -> its recorded SHA-512.  Per-day prep writes one
    # ZIP row per archive, so this is a plain read rather than a guess.
    expected_zip_hashes: Dict[str, str] = {}
    archive_names = {a.name for a in archive_paths}
    # Day archives the manifest records but that are absent from disk.
    recorded_missing: set = set()
    if not file_list_path.is_file():
        problems.append(
            f"No file_list.csv in {staging_dir.name} — cannot verify ZIP "
            "contents. Re-run Resources/prepare_dataset.py."
        )
    else:
        listed_wav_sizes: Dict[str, str] = {}
        listed_wav_bytes: Dict[str, str] = {}
        try:
            with open(file_list_path, "r", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    name = (row.get("File Name") or "").strip()
                    if name in archive_names:
                        recorded = (row.get("SHA-512 Hash") or "").strip()
                        if recorded:
                            expected_zip_hashes[name] = recorded
                    elif name.lower().endswith(".zip"):
                        # A ZIP row naming an archive that is not on disk:
                        # prep recorded it, then the file went missing.
                        parsed = azus_common.parse_day_zip_name(name)
                        if parsed is not None and parsed[0] == esid:
                            recorded_missing.add(name)
                    elif name.lower().endswith(".wav"):
                        listed_wav_sizes[name] = (
                            (row.get("File size (KB)") or "").strip()
                        )
                        listed_wav_bytes[name] = (
                            (row.get("File size (Bytes)") or "").strip()
                        )
        except (OSError, csv.Error) as exc:
            problems.append(
                f"file_list.csv is unreadable ({exc}) — cannot verify "
                "ZIP contents."
            )
            listed_wav_sizes = {}
            listed_wav_bytes = {}

        if listed_wav_sizes or expected_zip_hashes:
            # Which archive OWNS each listed WAV.  Re-derived from the
            # filename with the same rule prep grouped by — never from the
            # free-text Notes column that records the same mapping.
            owner_of: Dict[str, Optional[str]] = {}
            for name in listed_wav_sizes:
                if not per_day:
                    owner_of[name] = archive_paths[0].name
                    continue
                day = azus_common.wav_day_key(name)
                owner_of[name] = (
                    azus_common.day_zip_name(esid, day) if day else None
                )

            undatable = sorted(n for n, o in owner_of.items() if o is None)
            if undatable:
                problems.append(
                    f"{len(undatable)} WAV(s) in file_list.csv have no "
                    f"8-digit date prefix, so no day archive can own them: "
                    f"{_summarize_names(undatable)}"
                )

            # Forward direction: the manifest expects an archive we do not
            # have.  This is what an interrupted per-day prep looks like,
            # and the old whole-folder comparison passed it whenever
            # exactly one day happened to be present.
            orphaned_days: Dict[str, int] = {}
            for name, owner in owner_of.items():
                if owner is not None and owner not in archive_wav_sizes:
                    orphaned_days[owner] = orphaned_days.get(owner, 0) + 1
            for owner in sorted(set(orphaned_days) | recorded_missing):
                count = orphaned_days.get(owner, 0)
                recorded = (
                    "file_list.csv records day archive"
                    if owner in recorded_missing
                    else "file_list.csv lists WAVs for day archive"
                )
                problems.append(
                    f"{recorded} {owner} ({count} WAV(s)), which is not "
                    f"present in {staging_dir.name} — preparation did not "
                    "complete."
                )

            def _size_differs(name: str, zip_wav_sizes: Dict[str, int]) -> bool:
                """True when the WAV's ZIP size disagrees with the manifest."""
                # Prefer the byte-exact column (written by current prep);
                # two different sizes can round to the same 2-decimal KB,
                # so the KB comparison alone leaves a small blind spot
                # when --skip-integrity-hash disables the hash backstop.
                exact = listed_wav_bytes.get(name, "")
                if exact:
                    return str(zip_wav_sizes[name]) != exact
                # Legacy manifests (pre-Bytes-column): rounded-KB compare
                # using the same formatting prep used to write it.
                return (
                    f"{zip_wav_sizes[name] / 1024:.2f}"
                    != listed_wav_sizes[name]
                )

            # Reverse direction, per archive: each archive must hold exactly
            # the WAVs the manifest assigns to it.  Scoping per archive is
            # what catches a day-1 WAV written into day 2's archive, which a
            # whole-folder comparison cannot see.
            for archive_name in sorted(archive_wav_sizes):
                zip_wav_sizes = archive_wav_sizes[archive_name]
                owned = {
                    n for n, o in owner_of.items() if o == archive_name
                }
                missing = sorted(owned - set(zip_wav_sizes))
                extra = sorted(set(zip_wav_sizes) - owned)
                scope = f" ({archive_name})" if per_day else ""
                if missing:
                    problems.append(
                        f"{len(missing)} WAV(s) listed in file_list.csv are "
                        f"MISSING from the ZIP{scope}: "
                        f"{_summarize_names(missing)}"
                    )
                if extra:
                    problems.append(
                        f"{len(extra)} WAV(s) in the ZIP{scope} are not "
                        f"listed in file_list.csv: {_summarize_names(extra)}"
                    )
                size_mismatches = sorted(
                    name
                    for name in owned & set(zip_wav_sizes)
                    if _size_differs(name, zip_wav_sizes)
                )
                if size_mismatches:
                    problems.append(
                        f"{len(size_mismatches)} WAV(s) differ in size "
                        f"between file_list.csv and the ZIP{scope}: "
                        f"{_summarize_names(size_mismatches)}"
                    )
                if archive_name not in expected_zip_hashes:
                    problems.append(
                        f"file_list.csv has no row for {archive_name} — the "
                        "external file list was never finalized; preparation "
                        "did not complete."
                    )
        else:
            problems.append(
                "file_list.csv lists no WAV files and no ZIP row — "
                "preparation did not complete."
            )

    # --- 5. Archive hashes vs the manifest's recorded hashes ---
    # Skipped when structural problems already failed the dataset (no
    # point reading a 43 GB archive we already know is bad).
    hashed = verify_zip_hash and expected_zip_hashes and not problems
    if hashed:
        computed: Dict[str, Dict[str, str]] = {}
        for archive in archive_paths:
            expected = expected_zip_hashes.get(archive.name)
            if not expected:
                continue
            logger.info(
                "Verifying SHA-512 of %s against file_list.csv (md5 computed "
                "in the same read for the upload step)...",
                archive.name,
            )
            digests = azus_common.calculate_digests(
                str(archive), ("sha512", "md5")
            )
            actual_hash = digests["sha512"]
            if actual_hash != expected:
                problems.append(
                    f"ZIP SHA-512 does not match file_list.csv — the archive "
                    f"changed after preparation. Expected "
                    f"{expected[:16]}..., got {actual_hash[:16]}...: "
                    f"{archive.name}"
                )
            else:
                computed[archive.name] = digests
        # Only hand the digests back when EVERY archive VERIFIED — the
        # uploader must never trust hashes from a dataset that failed, and
        # a dataset with one bad archive never uploads at all.
        if digests_out is not None and not problems:
            digests_out.update(computed)

    if not problems:
        total_wavs = sum(len(s) for s in archive_wav_sizes.values())
        logger.info(
            "Integrity verified for %s: %d WAV file(s) across %d archive(s) "
            "match file_list.csv; archive sha512 %s.",
            staging_dir.name,
            total_wavs,
            len(archive_wav_sizes),
            "OK" if hashed else "not checked",
        )
    return problems


# ===================================================================
#  CSV parsing and validation
# ===================================================================

def parse_collectors_csv(
    csv_file_path: str,
    dataset_category: str,
    project_config: Optional[Dict[str, Any]] = None,
) -> List[DataCollector]:
    """Parse a collectors CSV file into DataCollector models.

    Validates that all required headers (from project config) are present
    before parsing rows.

    Args:
        csv_file_path: Path to the collectors CSV.
        dataset_category: Category string (e.g., 'Total', 'Annular') used
            to determine which conditional headers are required.
        project_config: Parsed project_config.json.  If None, loads default.

    Returns:
        List of DataCollector models.

    Raises:
        ValueError: If required headers are missing or CSV is empty.
    """
    if project_config is None:
        project_config = load_project_config()

    with open(csv_file_path, mode="r", encoding="utf-8") as csv_file:
        csv_reader = csv.DictReader(csv_file)
        csv_headers = csv_reader.fieldnames

        if not csv_headers:
            raise ValueError("No headers found in the CSV file.")

        # --- Validate required headers from config ---
        expected_headers = list(project_config.get("csv_required_headers", []))

        # Add conditional headers for this dataset category
        conditional = project_config.get("csv_conditional_headers", {})
        if dataset_category in conditional:
            expected_headers.extend(conditional[dataset_category])

        missing_headers = set(expected_headers) - set(csv_headers)
        if missing_headers:
            raise ValueError(
                f"Expected CSV headers not found: {missing_headers}"
            )

        data = [DataCollector.model_validate(row) for row in csv_reader]

    logger.info("Parsed %d rows from %s", len(data), Path(csv_file_path).name)
    return data


# ===================================================================
#  Related-identifier helpers
# ===================================================================

# Map human-readable resource_type labels (as entered in Zenodo's UI or
# exported from older spreadsheets) to InvenioRDM vocabulary IDs.
# Keys are lowercased + stripped versions of whatever might appear in CSVs.
# See: https://github.com/inveniosoftware/invenio-rdm-records/blob/master/
#      invenio_rdm_records/fixtures/data/vocabularies/resource_types.yaml
_RESOURCE_TYPE_MAP: Dict[str, str] = {
    # Audio / Video
    "video/audio":              "video",
    "audiovisual":              "video",
    "audio":                    "audio",
    "video":                    "video",
    # Publications
    "publication-article":      "publication-article",
    "journal article":          "publication-article",
    "article":                  "publication-article",
    "publication-report":       "publication-report",
    "publication / report":     "publication-report",
    "publication/report":       "publication-report",
    "report":                   "publication-report",
    "publication-preprint":     "publication-preprint",
    "preprint":                 "publication-preprint",
    "publication-book":         "publication-book",
    "book":                     "publication-book",
    "publication-section":      "publication-section",
    "book chapter":             "publication-section",
    "publication-thesis":       "publication-thesis",
    "thesis":                   "publication-thesis",
    "publication":              "publication",
    # Data / Software
    "dataset":                  "dataset",
    "software":                 "software",
    "image":                    "image",
    "other":                    "other",
}


def _normalize_resource_type(raw: str) -> str:
    """Map a human-readable resource_type label to an InvenioRDM vocabulary ID.

    Strips, lowercases, and looks up ``raw`` in :data:`_RESOURCE_TYPE_MAP`.
    Falls back to ``"other"`` if the value is not recognised.

    Args:
        raw: Resource type string from a CSV cell (any capitalisation).

    Returns:
        InvenioRDM vocabulary ID string.
    """
    normalised = raw.strip().lower()
    vocab_id = _RESOURCE_TYPE_MAP.get(normalised)
    if vocab_id is None:
        logger.warning(
            "Unknown resource_type %r — defaulting to 'other'.  "
            "Add it to _RESOURCE_TYPE_MAP if needed.",
            raw,
        )
        vocab_id = "other"
    return vocab_id


def read_related_identifiers_from_csv(
    csv_path: Optional[str],
) -> List[RelatedIdentifier]:
    """Read related identifiers (citations, related works) from a CSV.

    Expected CSV columns: identifier, scheme, relation_type, resource_type

    All values are normalised before being passed to the InvenioRDM API:

    * ``scheme`` — lowercased (e.g., ``"DOI"`` → ``"doi"``).
    * ``relation_type`` — stripped, lowercased, spaces removed to produce the
      InvenioRDM vocabulary ID (e.g., ``"Is supplemented by"`` →
      ``"issupplementedby"``).  Human-readable labels from Zenodo's upload form
      are accepted and translated automatically.
    * ``resource_type`` — mapped from human-readable labels to InvenioRDM
      vocabulary IDs via :data:`_RESOURCE_TYPE_MAP`.  Unknown values fall back
      to ``"other"``.

    Args:
        csv_path: Path to the CSV file.  If None/empty, returns [].

    Returns:
        List of RelatedIdentifier models.
    """
    if not csv_path or not csv_path.strip():
        return []

    csv_file = Path(csv_path)
    if not csv_file.exists():
        logger.info("Related identifiers CSV not found: %s", csv_path)
        return []

    related_identifiers: List[RelatedIdentifier] = []

    try:
        with open(csv_file, mode="r", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            required_cols = {"identifier", "scheme", "relation_type"}

            if not required_cols.issubset(set(reader.fieldnames or [])):
                logger.warning(
                    "Related identifiers CSV missing required columns: %s "
                    "(found: %s)",
                    required_cols, reader.fieldnames,
                )
                return []

            for row_num, row in enumerate(reader, start=2):
                raw_identifier = row.get("identifier", "").strip()
                if not raw_identifier:
                    continue

                # --- Normalize scheme: must be lowercase ---
                scheme = row.get("scheme", "").strip().lower()

                # --- Normalize relation_type: strip → lowercase → remove spaces
                #     This converts both already-correct IDs ("cites") and
                #     human-readable labels ("Is supplemented by") to the
                #     InvenioRDM vocabulary ID format ("issupplementedby"). ---
                raw_rt = row.get("relation_type", "").strip()
                relation_type_id = raw_rt.lower().replace(" ", "")

                # --- Normalize resource_type to InvenioRDM vocabulary ID ---
                resource_type = None
                raw_rt_type = row.get("resource_type", "").strip()
                if raw_rt_type:
                    resource_type_id = _normalize_resource_type(raw_rt_type)
                    resource_type = ResourceType(id=resource_type_id)

                try:
                    related_identifiers.append(
                        RelatedIdentifier(
                            identifier=raw_identifier,
                            scheme=scheme,
                            # InvenioRDM requires relation_type as {"id": "..."}
                            relation_type=RelationType(id=relation_type_id),
                            resource_type=resource_type,
                        )
                    )
                except Exception as exc:
                    logger.warning(
                        "Error parsing related identifier on row %d: %s "
                        "(row data: %s)",
                        row_num, exc, row,
                    )

        logger.info(
            "Loaded %d related identifier(s) from %s",
            len(related_identifiers), csv_file.name,
        )

    except Exception as exc:
        logger.warning(
            "Error reading related identifiers CSV %s: %s", csv_path, exc
        )

    return related_identifiers


def read_references_from_csv(csv_path: Optional[str]) -> List[Reference]:
    """Read bibliographic reference strings from a CSV.

    Expected CSV column: reference

    InvenioRDM requires references as ``{"reference": "..."}`` objects, NOT
    plain strings.  This function wraps each citation string in a
    :class:`~models.invenio.Reference` model automatically.

    Args:
        csv_path: Path to the CSV file.  If None/empty, returns [].

    Returns:
        List of Reference models, each containing a ``reference`` string.
    """
    if not csv_path or not csv_path.strip():
        return []

    csv_file = Path(csv_path)
    if not csv_file.exists():
        logger.info("References CSV not found: %s", csv_path)
        return []

    references: List[Reference] = []

    try:
        with open(csv_file, mode="r", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            if "reference" not in (reader.fieldnames or []):
                logger.warning("References CSV missing 'reference' column")
                return []

            for row in reader:
                ref_str = row.get("reference", "").strip()
                if ref_str:
                    references.append(Reference(reference=ref_str))

        logger.info(
            "Loaded %d reference(s) from %s", len(references), csv_file.name
        )

    except Exception as exc:
        logger.warning("Error reading references CSV %s: %s", csv_path, exc)

    return references


# ===================================================================
#  File discovery
# ===================================================================

def read_upload_manifest(
    manifest_path: Path,
    dataset_dir: Path,
) -> Dict[str, Optional[str]]:
    """Read an upload manifest CSV and locate all listed files.

    Args:
        manifest_path: Path to the ESID_XXX_to_upload.csv manifest.
        dataset_dir: Directory to search for files.

    Returns:
        Dictionary mapping filenames to their full paths.

    Raises:
        FileNotFoundError: If any files listed in the manifest are missing.
    """
    logger.info("Reading upload manifest: %s", manifest_path.name)

    files_to_upload: List[str] = []

    with open(manifest_path, "r", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if "File Name" not in (reader.fieldnames or []):
            raise ValueError(
                f"Manifest CSV missing 'File Name' column. "
                f"Found columns: {reader.fieldnames}"
            )
        for row in reader:
            filename = row.get("File Name", "").strip()
            if filename:
                files_to_upload.append(filename)

    logger.info("Manifest lists %d files to upload", len(files_to_upload))

    # Locate each file on disk
    found_files: Dict[str, Optional[str]] = {}
    missing_files: List[str] = []

    for filename in files_to_upload:
        file_path = dataset_dir / filename
        if file_path.exists() and file_path.is_file():
            found_files[filename] = str(file_path)
        else:
            found_files[filename] = None
            missing_files.append(filename)

    found_count = sum(1 for v in found_files.values() if v is not None)
    logger.info("Found %d/%d files", found_count, len(files_to_upload))

    if missing_files:
        logger.error("Missing %d files: %s", len(missing_files), missing_files[:5])
        raise FileNotFoundError(
            f"Missing {len(missing_files)} files listed in manifest. "
            f"First missing: {missing_files[0]}"
        )

    return found_files


def find_dataset_files(
    staging_folder: str,
    esid: str,
    required_files: Optional[List[str]] = None,
    project_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Optional[str]]:
    """Discover all files associated with a dataset.

    Checks for an ``ESID_XXX_to_upload.csv`` manifest first.  If found,
    uses that to determine files.  Otherwise falls back to the default
    required file list from project configuration.

    Note that the manifest is a directory scan (see
    ``prepare_dataset.create_upload_manifest``), so in the per-day layout
    it lists every day archive.  Callers must exclude the archives they
    handle explicitly rather than assuming the manifest holds only
    companions.

    Args:
        staging_folder: The prepared folder to discover files in.
        esid: Canonical padded ESID string, used to name the manifest.
        required_files: Override list of filenames to look for.
        project_config: Parsed project_config.json.

    Returns:
        Dictionary mapping filenames to their full paths (None if missing).

    Raises:
        FileNotFoundError: If ``staging_folder`` does not exist.
        ValueError: If ``staging_folder`` exists but is not a directory.
    """
    dataset_dir = Path(staging_folder)
    if not dataset_dir.exists():
        raise FileNotFoundError(f"Staging folder not found: {staging_folder}")
    if not dataset_dir.is_dir():
        raise ValueError(f"Path is not a directory: {staging_folder}")

    # --- Try upload manifest first ---
    if esid:
        manifest_path = dataset_dir / f"ESID_{esid}_to_upload.csv"
        if manifest_path.exists():
            logger.info("Found upload manifest: %s", manifest_path.name)
            return read_upload_manifest(manifest_path, dataset_dir)

    # --- Fall back to default file list ---
    logger.info("No upload manifest found, using default file discovery")

    if required_files is None:
        if project_config is None:
            project_config = load_project_config()
        required_files = project_config.get("default_required_files", [])

    found_files: Dict[str, Optional[str]] = {}
    missing_files: List[str] = []

    for filename in required_files:
        file_path = dataset_dir / filename
        if file_path.exists() and file_path.is_file():
            found_files[filename] = str(file_path)
        else:
            found_files[filename] = None
            missing_files.append(filename)

    found_count = sum(1 for v in found_files.values() if v is not None)
    logger.info(
        "Found %d/%d files for %s", found_count, len(required_files), zip_path.name
    )
    if missing_files:
        logger.warning("Missing %d files: %s", len(missing_files), ", ".join(missing_files[:5]))

    return found_files


# ===================================================================
#  Directory and file operations
# ===================================================================

def rename_dir_files(directory: str) -> None:
    """Rename files in a directory, replacing '#' with '_'.

    This normalizes ESID#XXX filenames to ESID_XXX format.

    Args:
        directory: Path to directory to scan.

    Raises:
        ValueError: If the directory is invalid.
    """
    if not directory or not os.path.isdir(directory):
        raise ValueError(f"Invalid directory: {directory}")

    for root, _, files in os.walk(directory):
        for filename in files:
            if filename.startswith("ESID#") and filename.endswith("zip"):
                old_path = os.path.join(root, filename)
                new_path = os.path.join(root, filename.replace("#", "_"))
                os.rename(old_path, new_path)
                logger.debug("Renamed %s → %s", filename, Path(new_path).name)


def list_dir_files(
    directory: str,
    file_pattern: str = "*",
) -> List[str]:
    """List files in a directory matching a glob pattern.

    Args:
        directory: Directory path.
        file_pattern: Glob pattern (default: all files).

    Returns:
        List of matching file paths.

    Raises:
        ValueError: If directory is invalid.
    """
    if not directory or not os.path.isdir(directory):
        raise ValueError(f"Invalid directory: {directory}")

    search_pattern = os.path.join(directory, file_pattern)
    return [f for f in glob.glob(search_pattern) if os.path.isfile(f)]


def get_esid_file_pairs(files: List[str]) -> List[Tuple[str, str]]:
    """Extract ESID numbers from filenames and pair with file paths.

    Args:
        files: List of file paths (e.g., ``['.../ESID_005.zip']``).

    Returns:
        List of (esid, file_path) tuples.
    """
    pairs: List[Tuple[str, str]] = []
    for f in files:
        esid = azus_common.parse_esid(Path(f).name)
        if esid is None:
            # The old last-underscore-segment split would have produced
            # garbage here (e.g. "v2" from ESID_005_v2.zip) and silently
            # attached the wrong collector metadata downstream.
            logger.warning(
                "Cannot parse an ESID from ZIP name %s — skipping it.",
                Path(f).name,
            )
            continue
        pairs.append((esid, f))
    return pairs


# ===================================================================
#  Recording date extraction
# ===================================================================

def get_recording_dates(
    archives: List[str],
    project_config: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    """Extract the earliest and latest recording dates from the archives.

    Reads WAV filenames inside every archive (without extracting) and
    parses the day from the ``YYYYMMDD_HHMMSS`` naming convention via
    ``azus_common.wav_day_key``, the same rule prep grouped by.  Spanning
    all archives matters in the per-day layout: each archive holds one
    day, so reading a single one would date the record to that day
    instead of the whole campaign.

    Args:
        archives: Data archive paths for one dataset.
        project_config: Project config (for minimum_recording_year).

    Returns:
        Tuple of (start_date, end_date) in YYYY-MM-DD format.

    Raises:
        ValueError: If ``archives`` is a single path rather than a list
            (a bare string would otherwise be iterated character by
            character), if it is empty, if any archive is missing, or if
            no valid dates are found.
    """
    # A bare string is iterable, so it would silently be read as a list of
    # single characters and fail with a baffling "missing ZIP file: v".
    if isinstance(archives, (str, Path)):
        raise ValueError(
            "get_recording_dates takes a LIST of archive paths, not a "
            f"single path (got {archives!r})"
        )
    if not archives:
        raise ValueError("No archives supplied — cannot derive recording dates.")
    for archive in archives:
        if not archive or not os.path.exists(archive):
            raise ValueError(f"Invalid or missing ZIP file: {archive}")

    if project_config is None:
        project_config = load_project_config()

    minimum_year = project_config.get("minimum_recording_year", 2000)

    wav_names = set()
    for archive in archives:
        with zipfile.ZipFile(archive, "r") as zf:
            wav_names.update(
                Path(name).name
                for name in zf.namelist()
                if name.lower().endswith(".wav")
            )

    from datetime import datetime as _dt

    dates = []
    for name in wav_names:
        # wav_day_key does no calendar validation by design (prep must not
        # reject an odd filename), so strptime is what rejects an
        # impossible date like 20241332 — keep the guard.
        try:
            day = azus_common.wav_day_key(name)
            if day is None:
                continue
            parsed = _dt.strptime(day, "%Y_%m_%d").date()
            # Unset-AudioMoth-clock files (1970) are deliberately kept by
            # prep but must not date the record; --skip-date-check covers
            # the case where they are all a site has.
            if parsed.year >= minimum_year:
                dates.append(parsed)
        except (ValueError, IndexError):
            continue

    if not dates:
        raise ValueError("No valid dates found in WAV file names.")

    return (
        min(dates).strftime(UPLOAD_DATE_FORMAT),
        max(dates).strftime(UPLOAD_DATE_FORMAT),
    )


# ===================================================================
#  Upload data assembly
# ===================================================================

def create_upload_data(
    esid_folder_archives: List[Tuple[str, str, List[str]]],
    data_collectors: List[DataCollector],
    project_config: Optional[Dict[str, Any]] = None,
    failure_results_file: Optional[str] = None,
) -> Tuple[List[UploadData], List[str]]:
    """Combine dataset folders with collector metadata into UploadData objects.

    Args:
        esid_folder_archives: List of (ESID, staging_folder, archives)
            triples — one per prepared folder, so one per Zenodo record.
            The archives are the set resolved by
            :func:`resolve_dataset_archives`, so the set verified and
            uploaded is the same set discovery found.
        data_collectors: List of DataCollector models.
        project_config: Project config (for file discovery).
        failure_results_file: Optional CSV path; when given, an ESID whose
            manifest/file discovery fails gets a failure row instead of its
            exception aborting the whole batch.

    Returns:
        Tuple of (upload_data_list, unmatched_esid_list).  Each dataset's
        collector is a COPY whose version may be overridden from the
        staging folder's ``total_eclipse_data.csv`` (see
        :func:`read_staged_version`), so the per-day version marker reaches
        the record and the shared collector object is never mutated.
    """
    # Both sides of this join are canonical ESIDs (the CSV side via the
    # DataCollector validator, the folder side via parse_esid); the join
    # itself is case-insensitive so a suffix case mismatch between a
    # folder name and the CSV (ESID_120a vs 120A) still matches.
    collector_dict = {dc.esid.casefold(): dc for dc in data_collectors}
    upload_data: List[UploadData] = []
    unmatched_ids: List[str] = []

    for esid, staging_folder, archives in esid_folder_archives:
        if esid.casefold() not in collector_dict:
            logger.warning("No collector info found for ESID: %s", esid)
            unmatched_ids.append(esid)
            continue

        # A broken manifest (listed files missing on disk, malformed CSV)
        # used to raise straight out of this loop, aborting EVERY dataset
        # in the batch.  Isolate it: record the failure, keep going.
        try:
            dataset_files = find_dataset_files(
                staging_folder, esid, project_config=project_config
            )
        except (FileNotFoundError, ValueError) as exc:
            logger.error(
                "ESID %s: manifest/file discovery failed — skipping this "
                "dataset: %s", esid, exc,
            )
            if failure_results_file:
                save_result_csv(
                    file=failure_results_file,
                    result=PersistedResult(
                        esid=esid,
                        error_message=f"Manifest/file discovery failed: {exc}",
                    ),
                )
            continue

        # find_dataset_files drives the upload manifest, which intentionally
        # excludes README.html (its content becomes the Zenodo description
        # field, it is not uploaded as a file).  Resolve README.html and
        # README.md directly from the ESID staging directory so they are
        # always found regardless of what the manifest contains.
        esid_staging_dir = Path(staging_folder)
        readme_html_path = esid_staging_dir / "README.html"
        readme_md_path   = esid_staging_dir / "README.md"

        # Exclude files that must not reach additional_files:
        #   README.md    — added explicitly via UploadData.readme_md
        #   the archives — added explicitly via UploadData.archives
        #   the metadata inputs (azus_common.METADATA_INPUT_FILES) —
        #     README.html becomes the description, and
        #     related_identifiers.csv / references.csv become record
        #     metadata; all three are read from the folder to BUILD the
        #     record and are not files OF it.  Excluded HERE as well as in
        #     prep's manifest, because the manifest is a directory scan
        #     written at prep time: folders prepped before that rule
        #     existed still list them, and re-prepping a multi-GB site to
        #     drop a 2 KB input file would be absurd.
        # Without this exclusion an archive would appear twice in all_files
        # (once here, once from archives), causing a 400 "already exists"
        # error on the second upload attempt.  EVERY archive must be
        # excluded, not just one: the manifest is a directory scan, so in
        # the per-day layout it lists them all, and any archive left in
        # additional_files would upload as a "companion" — with the default
        # retry budget instead of --upload-attempts.
        excluded = (
            {"README.md"}
            | set(azus_common.METADATA_INPUT_FILES)
            | {Path(a).name for a in archives}
        )
        additional_files = [
            path for filename, path in dataset_files.items()
            if path and filename not in excluded
        ]

        # Prep marks the per-day layout by appending DAY_ZIP_VERSION_SUFFIX
        # to the version in the staging folder's own total_eclipse_data.csv,
        # but get_draft_config sources version from the MASTER collectors
        # spreadsheet, which prep never writes back to.  Read the staged
        # value so the record carries the marker prep assigned it.  Copy
        # the collector rather than mutating the shared instance.
        collector = collector_dict[esid.casefold()]
        staged_version = read_staged_version(esid_staging_dir)
        if staged_version and staged_version != collector.version:
            logger.info(
                "ESID %s: version from staged total_eclipse_data.csv: "
                "%s (collectors CSV says %s)",
                esid, staged_version, collector.version or "(empty)",
            )
            collector = collector.model_copy(
                update={"version": staged_version}
            )

        data = UploadData(
            esid=esid,
            data_collector=collector,
            staging_folder=str(esid_staging_dir),
            archives=list(archives),
            readme_html=str(readme_html_path) if readme_html_path.exists() else None,
            readme_md=str(readme_md_path) if readme_md_path.exists() else None,
            additional_files=additional_files,
        )

        logger.info(
            "Prepared ESID %s: %d archive(s) + %d additional files = "
            "%d total", esid, len(data.archives), len(additional_files),
            len(data.all_files),
        )

        if not data.readme_html:
            logger.warning("ESID %s — README.html not found", esid)

        upload_data.append(data)

    return upload_data, unmatched_ids


# ===================================================================
#  Draft record configuration builder
# ===================================================================

def build_record_title(
    data_collector: DataCollector,
    project_config: Optional[Dict[str, Any]] = None,
) -> str:
    """Render the Zenodo record title for one site from the config template.

    Extracted so that everything asking "what is this dataset's title?"
    asks ONE function.  The title is the key the duplicate guard and
    ``--skip-existing-records`` search Zenodo by, so a second copy of this
    rule would mean searching for a title the record does not actually
    carry — the search would find nothing and both protections would
    silently pass.

    Args:
        data_collector: Collector metadata for this site.
        project_config: Parsed project_config.json; ``title_template``
            defaults to ``"$esid"`` when absent.

    Returns:
        The rendered title.  ``safe_substitute`` is used deliberately, so
        an unknown placeholder in the template survives verbatim rather
        than raising mid-run.
    """
    if project_config is None:
        project_config = load_project_config()
    title_template = Template(
        project_config.get("title_template", "$esid")
    )
    # The ESID renders in its display form: underscores become spaces
    # ("122_Part_1_of_2" -> "ESID#122 Part 1 of 2").  Plain 3-digit
    # ESIDs are unaffected, so existing record titles do not change.
    return title_template.safe_substitute(
        esid=azus_common.esid_display(data_collector.esid),
        eclipse_date=data_collector.eclipse_date,
        eclipse_label=data_collector.eclipse_label(),
    )


def find_existing_zenodo_record(
    data: UploadData,
    project_config: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Ask Zenodo whether this dataset's record already exists.

    The authoritative check behind ``--skip-existing-records``: it queries
    the account by the dataset's intended title rather than trusting the
    staging folder's ``upload_state.json``, so a folder whose state file
    was lost or hand-deleted is still recognised.  That is the case where
    proceeding would create a duplicate.

    Both drafts and published records count as "already exists".  A
    published record means the site is finished; an unfinished draft is
    finished with ``Resources/finish_stuck_uploads.py``, not by this run.

    Costs one search per dataset.  It does not replace the duplicate guard
    inside :func:`upload_to_zenodo`, which searches again immediately
    before creating a draft — that second search is what closes the window
    between this check and draft creation.

    Args:
        data: The dataset being considered.
        project_config: Parsed project_config.json (for the title template).

    Returns:
        A human-readable description of what was found, suitable for a log
        line and a skip reason, or None when Zenodo holds no record with
        this title.

    Raises:
        Exception: Whatever the search raises (network, auth, or an
            unrecognized response body — the search itself fails closed on
            those).  The caller must treat an undeterminable answer as a
            failure rather than uploading blindly.
    """
    title = build_record_title(data.data_collector, project_config)
    if not title:
        return None
    credentials = get_credentials_from_env()
    drafts, published = _search_drafts_by_title(
        credentials, title, "--skip-existing-records pre-check",
    )
    if published:
        ids = ", ".join(str(hit.get("id")) for hit in published)
        return f"already PUBLISHED on Zenodo as record {ids}"
    if drafts:
        ids = ", ".join(str(hit.get("id")) for hit in drafts)
        return (
            f"already has a draft on Zenodo (record {ids}) — finish it with "
            "Resources/finish_stuck_uploads.py"
        )
    return None


def get_draft_config(
    data_collector: DataCollector,
    readme_html_path: Optional[str] = None,
    related_identifiers_csv: Optional[str] = None,
    references_csv: Optional[str] = None,
    project_config: Optional[Dict[str, Any]] = None,
    reserve_doi: bool = False,
) -> DraftConfig:
    """Build a complete Zenodo draft record configuration.

    All project-specific metadata (creators, contributors, funding,
    community, custom fields) is read from ``project_config``.

    Args:
        data_collector: Collector metadata for this dataset.
        readme_html_path: Path to README.html (content = Zenodo description).
        related_identifiers_csv: Path to related identifiers CSV.
        references_csv: Path to references CSV.
        project_config: Parsed project_config.json.  Loads default if None.
        reserve_doi: Reserve a DataCite DOI at draft creation time.
            Only meaningful for production Zenodo — Sandbox DOIs are not
            registered with DataCite.  Defaults to False.

    Returns:
        DraftConfig model ready for the Zenodo API.

    Raises:
        ValueError: If readme_html_path is not provided.
        FileNotFoundError: If README.html does not exist.
    """
    if project_config is None:
        project_config = load_project_config()

    # --- Read description from README.html ---
    if not readme_html_path:
        raise ValueError(
            "README.html path is required. "
            "Run prepare_dataset.py first to generate README.html."
        )

    readme_path = Path(readme_html_path)
    if not readme_path.exists():
        raise FileNotFoundError(
            f"README.html not found at: {readme_html_path}\n"
            f"Run prepare_dataset.py to generate it before uploading."
        )

    logger.info("Using description from README.html: %s", readme_html_path)
    description = readme_path.read_text(encoding="utf-8")

    # --- Build recording date metadata ---
    # Use a single EDTF date interval ("start/end") instead of two separate
    # "collected" entries.  InvenioRDM supports this natively and it produces
    # cleaner metadata than two disconnected date entries.
    from datetime import datetime as _dt

    dates: List[Date] = []
    first_day = data_collector.first_recording_day
    last_day  = data_collector.last_recording_day

    if first_day and last_day:
        if first_day == last_day:
            # Recording happened on a single day — no interval needed
            dates.append(Date(
                date=first_day,
                type=DateType(id="collected"),
                description="Day of recording",
            ))
        else:
            # EDTF interval: "YYYY-MM-DD/YYYY-MM-DD"
            dates.append(Date(
                date=f"{first_day}/{last_day}",
                type=DateType(id="collected"),
                description="Recording period",
            ))
    elif first_day:
        dates.append(Date(
            date=first_day,
            type=DateType(id="collected"),
            description="Day of recording",
        ))

    # --- Build creators (from config + volunteer) ---
    creators = build_creators(project_config)

    volunteer_label = project_config.get("volunteer_creator_label", "")
    if volunteer_label:
        # Volunteers are anonymous — use organizational type with just a label.
        # personal type requires a non-empty given_name, which we do not have.
        volunteer_affiliations = [
            Affiliation(name=aff)
            for aff in parse_values_from_str(data_collector.affiliation)
            if aff and aff.strip()   # filter empty affiliation strings
        ]
        creators.append(Creator(
            person_or_org=PersonOrganization(
                type="organizational",
                name=volunteer_label,
            ),
            role=Role(id=project_config.get("volunteer_creator_role", "datacollector")),
            affiliations=volunteer_affiliations if volunteer_affiliations else None,
        ))

    # --- Build subjects from CSV keywords ---
    # subjects is Optional[str]: a site with no Keywords cell must yield
    # no Subject entries, not an AttributeError (None) or a Zenodo-
    # rejected empty Subject ("").
    subjects = [
        Subject(subject=s)
        for s in parse_values_from_str(data_collector.subjects or "")
        if s
    ]

    # --- Load related identifiers and references from CSV ---
    related_identifiers = read_related_identifiers_from_csv(related_identifiers_csv)
    references = read_references_from_csv(references_csv)

    # --- Build title from template ---
    title = build_record_title(data_collector, project_config)

    # --- Assemble Metadata ---
    metadata = Metadata(
        resource_type=ResourceType(
            id=project_config.get("resource_type", "dataset")
        ),
        title=title,
        publication_date=_dt.now().strftime(UPLOAD_DATE_FORMAT),
        creators=creators,
        description=description,
        funding=build_fundings(project_config),
        rights=[License(id=project_config.get("license", "cc-by-4.0"))],
        languages=[
            Language(id=lang)
            for lang in project_config.get("languages", ["eng"])
        ],
        dates=dates if dates else None,
        version=data_collector.version,
        publisher=project_config.get("publisher", "Zenodo"),
        subjects=subjects,
        related_identifiers=related_identifiers if related_identifiers else None,
        references=references if references else None,
        contributors=build_contributors(project_config) or None,
    )

    # Reserve a DOI at draft creation if requested.
    # Zenodo/InvenioRDM requires provider="datacite" and an empty identifier
    # string to trigger DOI reservation.
    pids = (
        {"doi": {"provider": "datacite", "identifier": ""}}
        if reserve_doi
        else {}
    )

    return DraftConfig(
        record_access=Access.PUBLIC,
        files_access=Access.PUBLIC,
        files_enabled=True,
        metadata=metadata.to_dict(),
        community_id=project_config.get("community_id", ""),
        custom_fields=project_config.get("custom_fields"),
        pids=pids,
    )


# ===================================================================
#  Result persistence
# ===================================================================

def save_result_csv(file: str, result: PersistedResult) -> None:
    """Append an upload result to a local CSV file.

    Creates the file and writes a header row if it does not yet exist.

    Args:
        file: CSV file path.
        result: Upload result to persist.

    Raises:
        ValueError: If file path is empty.
    """
    if not file:
        raise ValueError("Invalid file path for result CSV")

    output_file = Path(file)
    new_file = not output_file.exists()

    if new_file:
        logger.info("Creating results CSV: %s", file)
        output_file.parent.mkdir(exist_ok=True, parents=True)

    result_dict = result.model_dump()

    with open(file, mode="a", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=result_dict.keys())
        if new_file:
            writer.writeheader()
        writer.writerow(result_dict)


# ===================================================================
#  Upload tracker — prevents duplicate uploads across runs
# ===================================================================

class UploadTracker:
    """Track which files have already been uploaded to avoid duplicates.

    Persists the list of uploaded file paths to a plain-text file
    (``uploaded_files.txt``) inside the Records directory, alongside
    ``successful_results.csv`` and ``failed_results.csv``.  One absolute
    ZIP file path per line — easy to inspect and edit in any text editor.

    To force a re-upload of a specific record, open ``uploaded_files.txt``
    in the Records directory, delete the line containing that record's ZIP
    path, and re-run the uploader.

    Attributes:
        tracker_file: Path to the tracker persistence file.
        uploaded_files: Set of previously uploaded file paths.
    """

    #: Default filename — no leading dot, visible in any file browser.
    DEFAULT_FILENAME = "uploaded_files.txt"

    def __init__(self, tracker_file: str = DEFAULT_FILENAME):
        """Open (or create) the tracker file and load prior uploads.

        Args:
            tracker_file: Path to the persistence file.  Its parent
                directory is created if missing (Records/ may not yet
                exist on a fresh install).
        """
        self.tracker_file = Path(tracker_file)
        # Ensure the parent directory exists (Records/ may not exist yet
        # on a fresh install).
        self.tracker_file.parent.mkdir(parents=True, exist_ok=True)
        self.uploaded_files = self._load()

    def _load(self) -> set:
        """Load previously uploaded file paths from disk.

        Returns:
            Set of file paths, one per non-blank line of the tracker
            file; an empty set when the file does not exist yet.
        """
        if self.tracker_file.exists():
            with open(self.tracker_file, "r", encoding="utf-8") as fh:
                return {line.strip() for line in fh if line.strip()}
        return set()

    def is_uploaded(self, file_path: str) -> bool:
        """Check if a file has already been uploaded.

        Args:
            file_path: The file path to look up (compared verbatim
                against the recorded paths).

        Returns:
            True if the path is already recorded as uploaded.
        """
        return file_path in self.uploaded_files

    def mark_uploaded(self, file_path: str) -> None:
        """Mark a file as successfully uploaded.

        Adds the path to the in-memory set and appends it to the tracker
        file so the record survives across runs.

        Args:
            file_path: The file path to record as uploaded.
        """
        self.uploaded_files.add(file_path)
        with open(self.tracker_file, "a", encoding="utf-8") as fh:
            fh.write(f"{file_path}\n")

    def get_count(self) -> int:
        """Return the number of previously uploaded files.

        Returns:
            Count of distinct file paths currently tracked.
        """
        return len(self.uploaded_files)


# ===================================================================
#  Single-dataset upload
# ===================================================================

def save_result(
    esid: str,
    success: bool,
    success_file: str,
    failure_file: str,
    api_response: Optional[Dict[str, Any]] = None,
    error_type: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    """Save the result of an upload attempt.

    Routes the result to either the success or failure CSV file.

    Args:
        esid: Dataset identifier.
        success: Whether the upload succeeded.
        success_file: CSV path for successful results.
        failure_file: CSV path for failed results.
        api_response: Zenodo API response dictionary.
        error_type: Error class name if failed.
        error_message: Error description if failed.
    """
    results_file = success_file if success else failure_file
    persisted_result = PersistedResult(esid=esid)

    if not success:
        persisted_result.error_type = error_type or "Unknown"
        persisted_result.error_message = error_message or "Upload failed"

    if api_response:
        persisted_result.update(api_response)

    save_result_csv(file=results_file, result=persisted_result)

    if success:
        logger.info("ESID %s: Upload successful", esid)
    else:
        logger.error("ESID %s: Upload failed — %s", esid, error_message)


# ===================================================================
#  Metadata JSON persistence
# ===================================================================

def save_metadata_json(
    config: "DraftConfig",
    esid: str,
    output_dir: Path,
) -> Optional[Path]:
    """Write the Zenodo API payload for this record to a local JSON file.

    Saves the exact JSON structure that will be sent to the Zenodo
    ``POST /records`` endpoint, plus a ``_generated_at`` timestamp and
    a ``_azus_note`` header for provenance.  The file is named
    ``ESID_XXX_metadata.json`` and sits alongside the other staging files.

    Failures are logged as warnings — a write error here must never
    abort an upload.

    Args:
        config: ``DraftConfig`` produced by ``get_draft_config()``.
        esid: ESID number string (e.g., ``'005'``).
        output_dir: Staging directory where the file will be written.

    Returns:
        Path to the written JSON file, or ``None`` if writing failed.
    """
    from datetime import datetime as _dt

    json_path = output_dir / f"ESID_{esid}_metadata.json"

    # Reconstruct the exact payload upload_to_zenodo() will POST,
    # with provenance headers prepended.
    record_access = (
        config.record_access.value
        if hasattr(config.record_access, "value")
        else config.record_access
    )
    files_access = (
        config.files_access.value
        if hasattr(config.files_access, "value")
        else config.files_access
    )

    payload: Dict[str, Any] = {
        "_azus_note": (
            "This file records the metadata submitted to Zenodo for this "
            "upload.  It is generated by AZUS immediately before upload and "
            "is for local review and auditing only — it is not uploaded to "
            "Zenodo."
        ),
        "_generated_at": _dt.now().isoformat(timespec="seconds"),
        "access": {"record": record_access, "files": files_access},
        "files": {"enabled": config.files_enabled},
        "metadata": config.metadata,
    }

    if config.pids:
        payload["pids"] = config.pids
    if config.community_id:
        payload["parent"] = {
            "communities": {"ids": [config.community_id]}
        }
    if config.custom_fields:
        payload["custom_fields"] = config.custom_fields

    try:
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        logger.info("  Metadata JSON saved: %s", json_path.name)
        return json_path
    except Exception as exc:
        logger.warning(
            "Could not write metadata JSON for ESID %s: %s", esid, exc
        )
        return None


def _recover_draft_id_from_request_log(
    esid_dir: Path, esid: str
) -> Optional[str]:
    """Case-7 recovery: pull the draft's record_id from the request log.

    ``upload_state.json`` can be lost while its sibling
    ``ESID_XXX_request_log.json`` (written at draft creation) survives —
    e.g. when the state-file write failed with a warning.  Recovering the
    record_id from the request log lets the run RESUME the existing draft
    instead of creating a duplicate record.  The uploader rewrites
    ``upload_state.json`` on resume, so the folder self-heals.

    Args:
        esid_dir: The dataset's staging folder to search.
        esid: Zero-padded 3-digit ESID, used to build the request-log
            filename (``ESID_XXX_request_log.json``).

    Returns:
        The record_id string, or None when no readable request log with a
        record_id exists.
    """
    request_log = esid_dir / f"ESID_{esid}_request_log.json"
    if not request_log.is_file():
        return None
    try:
        payload = json.loads(request_log.read_text(encoding="utf-8"))
        record_id = str(payload.get("record_id") or "") or None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "Request log for ESID %s is unreadable (%s) — cannot recover "
            "a draft id from it.", esid, exc,
        )
        return None
    if record_id:
        logger.warning(
            "No usable upload_state.json for ESID %s — recovered draft %s "
            "from %s. Resuming that draft (the state file will be "
            "rewritten automatically).",
            esid, record_id, request_log.name,
        )
    return record_id


def upload_dataset(
    data: UploadData,
    delete_failures: bool = False,
    auto_publish: bool = False,
    reserve_doi: bool = False,
    related_identifiers_csv: Optional[str] = None,
    references_csv: Optional[str] = None,
    project_config: Optional[Dict[str, Any]] = None,
    defer_zip: bool = False,
    draft_only: bool = False,
    upload_attempts: int = _DEFAULT_UPLOAD_ATTEMPTS,
    title_guard: bool = True,
    archive_md5s: Optional[Dict[str, str]] = None,
    skip_date_check: bool = False,
) -> Dict[str, Any]:
    """Upload a single dataset to Zenodo.

    Extracts recording dates, builds the draft configuration from the
    project config, then calls the Zenodo uploader.

    Args:
        data: UploadData bundle for this dataset.
        delete_failures: Delete the draft if upload fails.
        auto_publish: Publish the record after successful upload.
        reserve_doi: Reserve a DataCite DOI at draft creation time.
        related_identifiers_csv: Global path to related identifiers CSV.
            Overridden by a per-record file in the ESID staging directory
            if one exists.
        references_csv: Global path to references CSV.
            Overridden by a per-record file in the ESID staging directory
            if one exists.
        project_config: Parsed project_config.json.
        defer_zip: If True, upload everything EXCEPT the data archives
            and skip the community-review submission.  In the per-day
            layout that defers every day archive, not just one — a record
            must never enter community review holding a fraction of its
            recording days.  The record (and its reserved DOI) is created
            on Zenodo, and upload_state.json is left in the staging
            folder — exactly the state a "stuck" upload leaves behind —
            so Resources/finish_stuck_uploads.py can upload the archives
            and submit the record for review later.
        draft_only: If True, upload everything INCLUDING the data archives but
            leave the record as a plain, editable draft — skip BOTH the
            community-review submission and publishing (auto_publish is
            forced off).  For reviewing a fully uploaded record on Zenodo
            before submitting or publishing it yourself.  Mutually
            exclusive with ``defer_zip``.
        upload_attempts: Total number of PUT attempts for each data
            archive (companion files always keep the default).  Defaults
            to the historical value (3).  Forwarded to
            :func:`upload_to_zenodo`.
        title_guard: When True (default), the uploader searches the
            account for an existing record with the same title BEFORE
            creating a fresh draft — the last line of defense against
            duplicate records when a folder's upload_state.json link was
            lost.  Forwarded to :func:`upload_to_zenodo`.
        archive_md5s: Pre-computed MD5s of the data archives, keyed by
            basename (produced during integrity verification), passed
            through as the uploader's ``known_md5s`` so it can skip
            re-reading the files to hash them.  None when no digests were
            carried over.
        skip_date_check: When True, a dataset whose WAV names carry no
            valid recording dates is uploaded anyway with its recording
            dates recorded as not available — the record's
            Collected-dates metadata entry is omitted rather than
            invented (``--skip-date-check`` CLI flag).

    Raises:
        Nothing propagates to the caller: internal ``ValueError`` raises
        (unusable recording dates, failed Eclipse-Date fallback) are
        caught by this function's own phase handlers and converted into
        the returned failure dict.

    Returns:
        Dictionary with keys: 'successful' (bool), 'api_response', 'error'.
    """
    logger.info("Starting upload for ESID %s", data.esid)
    logger.info(
        "  Data archive(s): %d — %s",
        len(data.archives),
        ", ".join(Path(a).name for a in data.archives),
    )
    logger.info("  Total files: %d", len(data.all_files))

    import traceback

    esid_dir = Path(data.staging_folder)

    # ------------------------------------------------------------------
    # Phase 1: Build the Zenodo draft configuration.
    # Kept separate from the upload phase so that if config construction
    # fails, we can report the error cleanly without entering the upload
    # path.  The metadata JSON is saved immediately after this phase so
    # it exists on disk regardless of what happens during the upload.
    # ------------------------------------------------------------------
    try:
        # Extract recording dates from the ZIP archive.  When every WAV
        # name fails the date parse (unset AudioMoth clock -> 19700101_*
        # names below minimum_recording_year, or hex names from old
        # firmware), --skip-date-check uploads the dataset anyway with
        # its recording dates recorded as not available (the record's
        # Collected-dates metadata entry is omitted).
        try:
            start_date, end_date = get_recording_dates(
                archives=data.archives, project_config=project_config
            )
        except ValueError as date_exc:
            if not skip_date_check:
                raise ValueError(
                    f"{date_exc} Re-run with --skip-date-check to "
                    "upload anyway with the recording dates recorded "
                    "as not available."
                ) from date_exc
            # "Not available": Zenodo's schema requires a valid EDTF
            # value in any dates entry, so the honest representation is
            # to OMIT the Collected-dates entry from the record rather
            # than invent a date.  get_draft_config leaves metadata
            # dates out entirely when both fields are None.
            logger.warning(
                "  --skip-date-check: no valid WAV filename dates for "
                "ESID %s — the record will carry NO recording-date "
                "metadata (dates not available).",
                data.esid,
            )
            start_date = end_date = None
        logger.debug("  Recording period: %s to %s", start_date, end_date)

        data.data_collector.first_recording_day = start_date
        data.data_collector.last_recording_day = end_date

        # Per-record citation override with global fallback.
        # If related_identifiers.csv or references.csv exists inside the
        # ESID staging directory, use it instead of the global config path.
        effective_related_csv = (
            str(esid_dir / "related_identifiers.csv")
            if (esid_dir / "related_identifiers.csv").exists()
            else related_identifiers_csv
        )
        effective_references_csv = (
            str(esid_dir / "references.csv")
            if (esid_dir / "references.csv").exists()
            else references_csv
        )
        if effective_related_csv != related_identifiers_csv:
            logger.info("  Using per-record related_identifiers.csv")
        if effective_references_csv != references_csv:
            logger.info("  Using per-record references.csv")

        config = get_draft_config(
            data_collector=data.data_collector,
            readme_html_path=data.readme_html,
            related_identifiers_csv=effective_related_csv,
            references_csv=effective_references_csv,
            project_config=project_config,
            reserve_doi=reserve_doi,
        )

    except Exception as exc:
        logger.error("Failed to build draft config for ESID %s: %s", data.esid, exc)
        logger.debug("Full traceback:\n%s", traceback.format_exc())
        return {
            "successful": False,
            "error": {
                "type": type(exc).__name__,
                "error_message": str(exc),
            },
            "api_response": None,
        }

    # ------------------------------------------------------------------
    # Phase 2: Persist the metadata payload to disk.
    # Called unconditionally after a successful config build — before the
    # upload attempt — so the JSON file exists whether the upload succeeds,
    # fails, or is interrupted.  save_metadata_json() has its own internal
    # try/except and will never raise here.
    # ------------------------------------------------------------------
    save_metadata_json(
        config=config,
        esid=data.esid,
        output_dir=esid_dir,
    )

    # ------------------------------------------------------------------
    # Phase 3: Upload to Zenodo (with resume support).
    # If a prior run created a draft but failed before publication,
    # upload_state.json in the staging folder records the draft ID so
    # the upload can resume against that same draft on re-run.
    # ------------------------------------------------------------------
    state_file = esid_dir / "upload_state.json"
    existing_draft_id: Optional[str] = None
    if state_file.exists():
        try:
            saved_state = json.loads(state_file.read_text(encoding="utf-8"))
            existing_draft_id = str(saved_state.get("record_id") or "") or None
            if existing_draft_id:
                logger.info(
                    "Found upload_state.json for ESID %s — will resume draft %s",
                    data.esid, existing_draft_id,
                )
        except Exception as exc:
            logger.warning(
                "Could not parse upload_state.json for ESID %s (%s) — "
                "proceeding without resume.",
                data.esid, exc,
            )

    # Case-7 fallback: no usable state file, but the request log written
    # at draft creation may still hold the record_id.  Without this, the
    # run would create a fresh draft — a duplicate of the existing one.
    if existing_draft_id is None:
        existing_draft_id = _recover_draft_id_from_request_log(
            esid_dir, data.esid
        )

    # When deferring the ZIP: drop it from the upload list and hold back
    # the community-review submission.  Review must wait until the ZIP is
    # on the record — a community manager accepting the record publishes
    # it, and published records cannot accept new files.
    files_to_upload = data.all_files
    if defer_zip:
        # Defer EVERY archive, not one: a per-day record holding a fraction
        # of its recording days must not proceed to community review.
        deferred = set(data.archives)
        files_to_upload = [f for f in data.all_files if f not in deferred]
        logger.info(
            "  --defer-zip: skipping %d archive(s) this run — %s "
            "(%d of %d files will upload)",
            len(deferred),
            ", ".join(sorted(Path(a).name for a in deferred)),
            len(files_to_upload), len(data.all_files),
        )

    try:
        logger.info("Uploading to Zenodo...")
        result = upload_to_zenodo(
            files=files_to_upload,
            config=config,
            delete_on_failure=delete_failures,
            # --draft-only forces publishing off even if config enables it.
            auto_publish=auto_publish and not draft_only,
            request_log_path=str(
                esid_dir / f"ESID_{data.esid}_request_log.json"
            ),
            existing_draft_id=existing_draft_id,
            state_file_path=str(state_file),
            # Skip the community-review submission when deferring the ZIP
            # (review must wait for the ZIP) OR when --draft-only keeps the
            # record a plain editable draft.
            submit_review=not defer_zip and not draft_only,
            upload_attempts=upload_attempts,
            title_guard=title_guard,
            abort_event=_ABORT_EVENT,
            known_md5s=archive_md5s or None,
            # Scope --upload-attempts to the data archives; companion files
            # always keep the uploader's default retry behavior.  EVERY
            # archive is named, so no day silently gets the smaller budget.
            priority_files={Path(a).name for a in data.archives},
        )
        return result

    except Exception as exc:
        # upload_to_zenodo converts every KNOWN failure family (HTTP,
        # transport, duplicate-title, integrity, bad file) into a result
        # dict internally, so an exception arriving here is unexpected —
        # very likely a code bug.  Log the full traceback at ERROR so it
        # cannot hide among routine upload failures, but still convert to
        # a failure result so one ESID never poisons the batch.
        logger.error(
            "UNEXPECTED exception during upload for ESID %s (%s: %s) — "
            "likely a code bug, full traceback follows.",
            data.esid, type(exc).__name__, exc, exc_info=True,
        )
        return {
            "successful": False,
            "error": {
                "type": type(exc).__name__,
                "error_message": str(exc),
            },
            "api_response": None,
        }


# ===================================================================
#  Multi-dataset upload orchestrator
# ===================================================================

def get_upload_data(
    data_dir: str,
    data_collectors_file: str,
    dataset_category: str,
    failure_results_file: str,
    tracker: UploadTracker,
    project_config: Optional[Dict[str, Any]] = None,
    esid_filter: Optional[List[str]] = None,
    stats: Optional[Dict[str, int]] = None,
) -> List[UploadData]:
    """Discover and prepare datasets in a directory for upload.

    When ``esid_filter`` is provided, only ESIDs in that list are processed;
    all others are silently skipped.  ESID numbers are compared as
    zero-padded three-digit strings so ``'4'``, ``'04'``, and ``'004'`` all
    match a folder named ``ESID_004``.  The returned datasets are ordered
    by the FILTER's order (the ``--esid`` command-line order, or the
    spreadsheet's row order) — not numerically and not by directory-scan
    order — so uploads happen in the order the user listed them.

    Args:
        data_dir: Directory containing ESID subdirectories with ZIP files.
        data_collectors_file: Path to the collectors CSV.
        dataset_category: Category string (e.g., 'Total', 'Annular').
        failure_results_file: CSV path for logging failures.
        tracker: UploadTracker to skip already-uploaded files.
        project_config: Parsed project_config.json.
        esid_filter: Optional list of ESID number strings to upload.
            If ``None`` or empty, all discovered ESIDs are processed.
            When given, the list's order becomes the upload order.
            Example: ``['004', '007', '012']``.
        stats: Optional shared statistics dict.  When provided,
            ``stats["skipped"]`` is incremented by the number of datasets
            dropped because their ZIP is already in the upload tracker —
            so the end-of-run summary reflects reality.

    Returns:
        List of UploadData objects ready for upload — in filter order
        when ``esid_filter`` was given, else in discovery order.

    Raises:
        ValueError: If ``data_dir`` or ``data_collectors_file`` is empty.
    """
    if not data_dir:
        raise ValueError("Missing data directory")
    if not data_collectors_file:
        raise ValueError("Missing data collectors file")

    # Normalize filter values to canonical ESIDs ('4', '04', '004' all
    # resolve to '004'; suffixed ids like 120A pass through) and compare
    # casefolded so a suffix case mismatch cannot hide a dataset.  The
    # order map remembers each ESID's position in the GIVEN list (first
    # occurrence wins) — the final dataset list is sorted by it so
    # uploads run in the user's order, not directory-scan order.
    normalized_filter: Optional[set] = None
    filter_order: Dict[str, int] = {}
    if esid_filter:
        # First occurrence wins for both position and display case.
        ordered: Dict[str, str] = {}
        for value in esid_filter:
            canonical = azus_common.normalize_esid(value)
            ordered.setdefault(canonical.casefold(), canonical)
        normalized_filter = set(ordered)
        filter_order = {key: i for i, key in enumerate(ordered)}
        logger.info(
            "ESID filter active — restricting upload to (in this "
            "order): %s",
            ", ".join(ordered.values()),
        )

    logger.info("Loading data collectors from: %s", data_collectors_file)
    data_collectors = parse_collectors_csv(
        csv_file_path=data_collectors_file,
        dataset_category=dataset_category,
        project_config=project_config,
    )
    logger.info("Loaded %d data collector records", len(data_collectors))

    # Discover prepared dataset folders.  One folder is one dataset is one
    # Zenodo record, whatever ZIP layout it holds — the archives inside are
    # resolved per folder rather than each becoming its own work item.
    logger.info("Scanning directory: %s", data_dir)
    data_path = Path(data_dir)
    folder_items: List[Tuple[str, str, List[str]]] = []

    for subdir in data_path.iterdir():
        if subdir.is_dir() and (
            subdir.name.startswith("ESID_") or subdir.name.startswith("ESID#")
        ):
            # Shared parser — tolerant of folder names like "ESID_073",
            # "ESID_073_Staging" (prepare_dataset.py's name), "ESID#73".
            # Resolved once here: it is both the filter key and the ESID
            # the archives are resolved against below.
            folder_esid = azus_common.parse_esid(subdir.name)
            if folder_esid is None:
                logger.debug(
                    "  Skipping %s (no ESID number in folder name)",
                    subdir.name,
                )
                continue

            # Apply ESID filter before adding to the work list.
            if normalized_filter is not None:
                if folder_esid.casefold() not in normalized_filter:
                    logger.debug(
                        "  Skipping %s (not in --esid filter)", subdir.name
                    )
                    continue

            # Requirement 9: never let the ZIP pipeline touch an ESID that
            # has been switched to file-by-file mode — only the file-by-file
            # tool (finish_stuck_uploads.py --enable-file-by-file) finishes
            # it, so the two never fight over the same Zenodo record. Skip
            # CLEANLY here (before the no-ZIP failure-row path below), so a
            # file-by-file folder is not logged as a failure every run.
            # A per-day folder's marker is STALE — file-by-file cannot apply
            # to it, so nothing else is contending for its record and
            # skipping would leave it finishable by no path at all.
            if azus_common.file_by_file_mode_blocks_zip_path(
                subdir, folder_esid
            ):
                logger.info(
                    "  Skipping %s — upload_state.json marks it file-by-file "
                    "mode (finish with finish_stuck_uploads.py "
                    "--enable-file-by-file).", subdir.name,
                )
                continue

            # A staging folder with no usable archive cannot be uploaded.
            # This used to be skipped with NO logging at all — a mis-staged
            # dataset simply vanished from the run.  Now it is loud and
            # recorded.  A mixed-layout folder is refused here too, with
            # the resolver's own explanation.
            archives, _mode, layout_problems = resolve_dataset_archives(
                subdir, folder_esid
            )
            if layout_problems:
                for problem in layout_problems:
                    logger.warning("ESID folder unusable — skipping: %s", problem)
                save_result_csv(
                    file=failure_results_file,
                    result=PersistedResult(
                        esid=folder_esid,
                        error_message="; ".join(layout_problems),
                    ),
                )
                continue
            folder_items.append(
                (folder_esid, str(subdir), [str(a) for a in archives])
            )

    logger.info(
        "Found %d dataset folder(s) matching criteria (%d archive(s) total)",
        len(folder_items), sum(len(a) for _, _, a in folder_items),
    )

    # Skip already-uploaded datasets.  The tracker records archive paths, so
    # a dataset counts as done only when EVERY archive is recorded — a
    # partially recorded folder re-enters the pipeline, where the uploader's
    # name+size+md5 check skips the archives already committed remotely.
    original_count = len(folder_items)
    remaining: List[Tuple[str, str, List[str]]] = []
    for esid, folder, archives in folder_items:
        if archives and all(tracker.is_uploaded(a) for a in archives):
            logger.info(
                "Tracker skip (already uploaded): %s (%d archive(s))",
                Path(folder).name, len(archives),
            )
            continue
        remaining.append((esid, folder, archives))
    folder_items = remaining
    skipped = original_count - len(folder_items)
    if skipped:
        logger.info("Skipped %d already-uploaded dataset(s)", skipped)
        if stats is not None:
            stats["skipped"] += skipped

    if not folder_items:
        logger.warning("No new datasets to upload")
        return []

    upload_data, unmatched_ids = create_upload_data(
        esid_folder_archives=folder_items,
        data_collectors=data_collectors,
        project_config=project_config,
        failure_results_file=failure_results_file,
    )

    for esid in unmatched_ids:
        logger.warning("No collector data found for ESID: %s", esid)
        save_result_csv(
            file=failure_results_file,
            result=PersistedResult(
                esid=esid,
                error_message="Unable to find data collector info",
            ),
        )

    if filter_order:
        # Upload in the order the ESIDs were given (--esid order /
        # spreadsheet row order).  Stable sort: anything unexpectedly
        # absent from the map keeps its relative position at the end.
        upload_data.sort(
            key=lambda d: filter_order.get(
                d.esid.casefold(), len(filter_order)
            )
        )

    logger.info("Prepared %d dataset(s) for upload", len(upload_data))
    return upload_data


def archive_staging_to_uploaded(
    staging_folder: Path, esid: str, tag: str = ""
) -> Optional[Path]:
    """Move a completed staging folder into ``Uploaded_Data/ESID_XXX_Uploaded/``.

    Per-ESID file I/O on a unique path (no two workers ever touch the same
    folder), so no lock is needed.  An existing destination is replaced.
    Any failure is logged and swallowed (returns ``None``) so a local move
    problem never undoes an already-successful Zenodo upload.

    Args:
        staging_folder: The ESID's staging folder to archive.
        esid: Canonical ESID string (names the destination folder).
        tag: Optional log prefix (e.g. ``"[ESID 073]"``).

    Returns:
        The destination path on a successful move, or ``None`` if the move
        was skipped (folder absent) or failed.
    """
    try:
        if not staging_folder.is_dir():
            return None
        uploaded_dir = azus_common.UPLOADED_DATA
        destination = uploaded_dir / f"ESID_{esid}_Uploaded"
        uploaded_dir.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            logger.warning(
                "%s Replacing existing uploaded folder: %s", tag, destination,
            )
            shutil.rmtree(destination)
        shutil.move(str(staging_folder), str(destination))
        logger.info("%s Archived staging folder to: %s", tag, destination)
        return destination
    except Exception as exc:
        logger.warning("%s Could not move staging folder: %s", tag, exc)
        return None


def _process_one_dataset_inner(
    index: int,
    total: int,
    data: UploadData,
    *,
    delete_failures: bool,
    auto_publish: bool,
    reserve_doi: bool,
    related_identifiers_csv: Optional[str],
    references_csv: Optional[str],
    project_config: Optional[Dict[str, Any]],
    successful_results_file: str,
    failure_results_file: str,
    tracker: "UploadTracker",
    tracker_lock: threading.Lock,
    results_lock: threading.Lock,
    stats: Dict[str, int],
    stats_lock: threading.Lock,
    defer_zip: bool = False,
    draft_only: bool = False,
    upload_attempts: int = _DEFAULT_UPLOAD_ATTEMPTS,
    title_guard: bool = True,
    verify_zip_hash: bool = True,
    skip_date_check: bool = False,
    skip_existing_records: bool = False,
) -> None:
    """Upload one ESID dataset end-to-end and record the result.

    This is the per-ESID unit of work.  It performs (in order):
      0. Verify the dataset's integrity (:func:`verify_dataset_integrity`)
         — the ``.prep_complete`` sentinel, ZIP readability, ZIP contents
         vs ``file_list.csv``, and the ZIP's SHA-512.  Any problem marks
         the dataset FAILED before a single byte reaches Zenodo.
      1. Upload all files for the dataset to a Zenodo draft (via
         :func:`upload_dataset`, which internally handles per-file retries,
         draft resume via ``upload_state.json``, community submission,
         and optional publishing).
      2. On success, append the ZIP path to the upload tracker so future
         runs skip this dataset.
      3. On success, move the prepared staging folder into
         ``Uploaded_Data/ESID_XXX_Uploaded/`` (best-effort — a move
         failure is logged but does not undo the successful upload).
      4. Append a row to the appropriate result CSV (success or failure).
      5. Update the shared statistics dictionary.

    Thread safety
    -------------
    This function is designed to be safe to call **concurrently** from
    multiple worker threads (one ESID per thread).  It coordinates access
    to the three pieces of shared state via locks supplied by the caller:

    * ``tracker_lock`` — guards :meth:`UploadTracker.mark_uploaded`, which
      appends one line to ``Records/uploaded_files.txt``.  Without this
      lock, two threads writing at the same time could corrupt that file.
    * ``results_lock`` — guards :func:`save_result`, which appends one row
      to ``successful_results.csv`` or ``failed_results.csv``.  Same
      reason: prevents two threads writing simultaneously and producing
      a garbled CSV.
    * ``stats_lock`` — guards increments to the shared ``stats`` counter
      dictionary (``stats["total_processed"] += 1`` is **not** atomic
      under Python's GIL for compound updates).

    Everything else this function touches is naturally per-ESID and
    requires no coordination: the Zenodo draft (different record_id per
    ESID), the staging folder (different path per ESID), and
    ``upload_state.json`` (lives inside the per-ESID staging folder).
    What makes those per-ESID claims hold is that
    :func:`get_upload_data` emits at most ONE :class:`UploadData` per
    staging folder — a dataset is a folder, not an archive.  Were one
    folder to produce several work items, two threads could share a
    staging folder, a draft and an ``upload_state.json``, and the first
    to finish would move the folder out from under the others.
    The Python ``logging`` module is already thread-safe by design, so
    log lines from different ESIDs may interleave but never garble — use
    the ``[ESID XXX]`` prefix on the key log messages below to follow
    one dataset's progress through interleaved output.

    Args:
        index: 1-based position of this dataset in the queue (for logs).
        total: Total number of datasets queued (for logs).
        data: The :class:`UploadData` bundle for this ESID.
        delete_failures: Whether to delete the Zenodo draft on failure
            (forwarded to :func:`upload_dataset`).
        auto_publish: Whether to publish the record after upload
            (forwarded to :func:`upload_dataset`).
        reserve_doi: Whether to reserve a DOI at draft creation
            (forwarded to :func:`upload_dataset`).
        related_identifiers_csv: Global related-identifiers CSV path.
        references_csv: Global references CSV path.
        project_config: Parsed ``project_config.json``.
        successful_results_file: CSV path for successful results.
        failure_results_file: CSV path for failed results.
        tracker: The shared :class:`UploadTracker` instance.
        tracker_lock: Lock that guards the tracker's append-to-file.
        results_lock: Lock that guards both result CSV writes.
        stats: Shared statistics dict (modified in place).
        stats_lock: Lock that guards stat increments.
        defer_zip: If True (the ``--defer-zip`` flag), the data archives
            are NOT uploaded and the record is NOT submitted for community
            review.  Every archive is held back, not just one: a record
            must never enter review holding a fraction of its recording
            days.  On success the run is counted as "deferred" — the
            staging folder stays in ``Staging_Area/`` with its
            ``upload_state.json`` so ``Resources/finish_stuck_uploads.py``
            can upload the archives and finish the record later.  Nothing is written to the success CSV
            or the upload tracker, because the record is not complete yet.
        draft_only: If True (the ``--draft-only`` flag), upload everything
            INCLUDING the ZIP but leave the record a plain editable draft —
            skip the community-review submission and publishing.  Forwarded
            to :func:`upload_dataset`.
        upload_attempts: Total number of PUT attempts for the data
            ZIP (``--upload-attempts`` CLI flag; companion files always
            keep the default).  Forwarded to :func:`upload_dataset`.
        title_guard: Duplicate-record guard (default True; disabled by the
            ``--skip-title-guard`` CLI flag).  Forwarded to
            :func:`upload_dataset`.
        skip_existing_records: When True (the ``--skip-existing-records``
            flag), ask Zenodo whether a record with this dataset's title
            already exists BEFORE the integrity gate, and skip the dataset
            if one does — counted as skipped, not failed, with the staging
            folder left exactly where it is.  Both drafts and published
            records count.  A search that cannot be completed fails the
            dataset rather than falling through to an upload.
        verify_zip_hash: When True (default), the pre-upload integrity
            gate re-hashes the ZIP and compares against ``file_list.csv``
            (``--skip-integrity-hash`` disables just this step; the
            sentinel and ZIP-contents checks always run).
        skip_date_check: Upload datasets whose WAV names carry no
            valid recording dates with the dates recorded as not
            available (``--skip-date-check``); forwarded to
            :func:`upload_dataset`.

    Returns:
        None.  All results are recorded via the result CSVs and the
        shared stats dict — there is nothing for the caller to inspect
        afterwards.  A failed upload does NOT raise; it is logged and
        written to the failure CSV so that one bad ESID never poisons
        the worker pool or stops other ESIDs from finishing.
    """
    # Per-ESID prefix lets the user follow one dataset through interleaved
    # multi-worker output.  Example: `grep '[ESID 012]' azus_upload.log`.
    tag = f"[ESID {data.esid}]"

    if _ABORT_EVENT.is_set():
        logger.warning("%s Skipped — run aborted by user (Ctrl+C).", tag)
        with stats_lock:
            stats["aborted"] = stats.get("aborted", 0) + 1
        return

    # Requirement 9 (second guard): never run the ZIP pipeline on an ESID in
    # file-by-file mode. get_upload_data already skips these at discovery,
    # but a switch that flipped the mode AFTER discovery (TOCTOU) or any
    # direct caller of this function could still arrive here — and this must
    # precede the ZIP integrity gate below, which would otherwise open a
    # stale or absent ZIP.
    staging_folder = Path(data.staging_folder).resolve()
    if azus_common.file_by_file_mode_blocks_zip_path(
        staging_folder, data.esid
    ):
        logger.info(
            "%s Skipped — file-by-file mode (finish with "
            "finish_stuck_uploads.py --enable-file-by-file).", tag,
        )
        with stats_lock:
            stats["skipped"] = stats.get("skipped", 0) + 1
        return

    logger.info("%s Starting (dataset %d of %d)", tag, index, total)

    # --- Optional pre-check: is this record already on Zenodo? ---
    # Deliberately BEFORE the integrity gate, which re-hashes every archive:
    # skipping here costs one search instead of a multi-GB read.  A search
    # that cannot be completed fails the dataset rather than falling through
    # to an upload — the same fail-closed stance the integrity gate takes,
    # because the whole point of the flag is not to touch what already
    # exists.
    if skip_existing_records:
        try:
            existing = find_existing_zenodo_record(data, project_config)
        except Exception as exc:
            logger.error(
                "%s Could not determine whether this record already exists "
                "(%s: %s) — failing the dataset rather than risking a "
                "duplicate. Re-run without --skip-existing-records to upload "
                "anyway (the duplicate guard still protects the record).",
                tag, type(exc).__name__, exc,
            )
            with stats_lock:
                stats["total_processed"] += 1
                stats["failed"] += 1
            with results_lock:
                save_result(
                    esid=data.esid,
                    success=False,
                    success_file=successful_results_file,
                    failure_file=failure_results_file,
                    error_type="ExistingRecordCheckFailed",
                    error_message=(
                        f"--skip-existing-records search failed "
                        f"({type(exc).__name__}: {exc})"
                    ),
                )
            logger.error("%s DONE (existing-record check failed)", tag)
            return
        if existing:
            logger.info(
                "%s Skipped — %s (--skip-existing-records).", tag, existing
            )
            with stats_lock:
                stats["skipped"] = stats.get("skipped", 0) + 1
            return

    # --- Step 0: integrity gate — nothing uploads past a problem ---
    # Runs entirely locally (no network).  A broken or unverifiable
    # dataset is marked FAILED here so an incomplete ZIP can never
    # reach Zenodo, no matter how it ended up in the staging area.
    integrity_digests: Dict[str, Dict[str, str]] = {}
    try:
        integrity_problems = verify_dataset_integrity(
            staging_folder=str(staging_folder),
            esid=data.esid,
            archives=data.archives,
            verify_zip_hash=verify_zip_hash,
            digests_out=integrity_digests,
        )
    except Exception as exc:  # a gate crash must fail closed, not open
        integrity_problems = [
            f"Integrity check itself failed ({type(exc).__name__}: {exc})"
        ]

    # Zenodo caps a record's file count.  Prep budgets for it before
    # writing archives, but the upload manifest is a directory scan, so an
    # extra file dropped into the folder afterwards can push the real set
    # over.  Refuse here, before any network work, rather than uploading
    # for hours and taking a 400 on the last file.
    if len(data.all_files) > azus_common.ZENODO_MAX_FILES_PER_RECORD:
        companions = len(data.all_files) - len(data.archives)
        integrity_problems.append(
            f"{len(data.all_files)} file(s) ({len(data.archives)} archive(s) "
            f"+ {companions} companion(s)) exceeds Zenodo's "
            f"{azus_common.ZENODO_MAX_FILES_PER_RECORD}-file limit per "
            "record — split the site with "
            "Resources/split_oversized_raw_folders.py, or re-prep with "
            "--single-zip."
        )

    if integrity_problems:
        for problem in integrity_problems:
            logger.error("%s INTEGRITY CHECK FAILED: %s", tag, problem)
        logger.error(
            "%s Dataset will NOT be uploaded — fix the staging folder "
            "(re-run Resources/prepare_dataset.py) and try again.", tag,
        )
        with stats_lock:
            stats["total_processed"] += 1
            stats["failed"] += 1
        with results_lock:
            save_result(
                esid=data.esid,
                success=False,
                success_file=successful_results_file,
                failure_file=failure_results_file,
                error_type="DatasetIntegrityError",
                error_message=" | ".join(integrity_problems),
            )
        logger.error("%s DONE (failed integrity check)", tag)
        return

    # Catch-all guard around the entire per-ESID workflow.  ``upload_dataset``
    # already wraps its own work in try/except and returns a failure dict,
    # so a real exception here is unexpected — but we never want one ESID's
    # crash to take down the whole batch, so we convert any leaked exception
    # into a synthesized failure result and continue.
    try:
        result = upload_dataset(
            data=data,
            delete_failures=delete_failures,
            auto_publish=auto_publish,
            reserve_doi=reserve_doi,
            related_identifiers_csv=related_identifiers_csv,
            references_csv=references_csv,
            project_config=project_config,
            defer_zip=defer_zip,
            draft_only=draft_only,
            upload_attempts=upload_attempts,
            title_guard=title_guard,
            # md5s from the integrity gate's combined digest pass — saves
            # the uploader a second full read of each (verified) archive.
            archive_md5s={
                name: digests["md5"]
                for name, digests in integrity_digests.items()
                if digests.get("md5")
            },
            skip_date_check=skip_date_check,
        )
    except Exception as exc:
        logger.error("%s Unexpected error during upload: %s", tag, exc)
        result = {
            "successful": False,
            "api_response": None,
            "error": {
                "type": type(exc).__name__,
                "error_message": str(exc),
            },
        }

    # --- Update shared counters under the stats lock ---
    # Compound increments like ``stats["x"] += 1`` are NOT atomic in Python;
    # two threads incrementing at once could lose updates.  Locking is cheap.
    with stats_lock:
        stats["total_processed"] += 1

    if result["successful"] and defer_zip:
        # Deferred success: the record and its reserved DOI exist on Zenodo
        # with every file EXCEPT the data archives, and community review has NOT
        # been submitted.  Deliberately skip the tracker append, the move to
        # Uploaded_Data/, and the success-CSV row — the record is not
        # complete.  The staging folder (with upload_state.json inside) is
        # left in Staging_Area/, which is exactly the state
        # finish_stuck_uploads.py looks for.
        with stats_lock:
            stats["deferred"] = stats.get("deferred", 0) + 1
        logger.info(
            "%s DONE (deferred) — record + DOI created, ZIP not uploaded. "
            "Run 'python Resources/finish_stuck_uploads.py' to upload the "
            "ZIP and submit the record for community review.",
            tag,
        )
        return

    if result["successful"]:
        with stats_lock:
            stats["successful"] += 1

        # Mark the ZIP as uploaded so future runs skip it.  The tracker
        # appends one line to ``Records/uploaded_files.txt``; the lock
        # ensures two threads don't write to that file at the same time.
        # Record every archive: a dataset counts as uploaded only when all
        # of them are, so a partial folder re-enters the pipeline next run.
        with tracker_lock:
            for archive in data.archives:
                tracker.mark_uploaded(archive)

        # Archive the staging folder into Uploaded_Data/ESID_XXX_Uploaded/.
        # Per-ESID file I/O on a unique path (no two threads touch the same
        # folder), so no lock is needed. Failures are logged, not fatal —
        # a local move problem must not undo the successful Zenodo upload.
        archive_staging_to_uploaded(staging_folder, data.esid, tag)

        # Append the success row to successful_results.csv (lock-guarded).
        with results_lock:
            save_result(
                esid=data.esid,
                success=True,
                success_file=successful_results_file,
                failure_file=failure_results_file,
                api_response=result.get("api_response"),
            )
        logger.info("%s DONE (success)", tag)
    else:
        with stats_lock:
            stats["failed"] += 1
        error = result.get("error", {}) or {}
        with results_lock:
            save_result(
                esid=data.esid,
                success=False,
                success_file=successful_results_file,
                failure_file=failure_results_file,
                error_type=error.get("type"),
                error_message=error.get("error_message"),
            )
        logger.error("%s DONE (failed)", tag)


def _process_one_dataset(
    index: int,
    total: int,
    data: UploadData,
    **kwargs: Any,
) -> None:
    """Run one ESID dataset job, teeing its output when enabled.

    A thin wrapper around :func:`_process_one_dataset_inner`, which does
    all of the real work — see its docstring for the full contract and
    thread-safety notes.  When ``--save-terminal-output`` is active,
    everything the attempt logs between entry and return — including
    the un-prefixed ``standalone_uploader`` lines — is routed to this
    ESID's ``..._upload_attempt_NNN.txt`` file for the duration of the
    call (each attempt runs entirely within one thread, so the routing
    is per-thread).

    Args:
        index: 1-based position of this dataset in the queue (for logs).
        total: Total number of datasets queued (for logs).
        data: The :class:`UploadData` bundle for this ESID.
        kwargs: Forwarded verbatim to
            :func:`_process_one_dataset_inner` (the shared locks,
            result-file paths, tracker, stats, and per-run flags).

    Returns:
        None.  Results are recorded exactly as described in
        :func:`_process_one_dataset_inner`; a failed upload never
        raises.
    """
    tee = _TEE_HANDLER
    if tee is not None and not _ABORT_EVENT.is_set():
        tee.begin_esid(data.esid)
    try:
        _process_one_dataset_inner(index=index, total=total, data=data, **kwargs)
    finally:
        if tee is not None:
            tee.end_esid()


def upload_datasets(
    datasets: List[Dict[str, str]],
    successful_results_file: str,
    failure_results_file: str,
    related_identifiers_csv: Optional[str] = None,
    references_csv: Optional[str] = None,
    auto_publish: bool = False,
    delete_failures: bool = False,
    reserve_doi: bool = False,
    project_config: Optional[Dict[str, Any]] = None,
    esid_filter: Optional[List[str]] = None,
    workers: int = 1,
    defer_zip: bool = False,
    draft_only: bool = False,
    upload_attempts: int = _DEFAULT_UPLOAD_ATTEMPTS,
    title_guard: bool = True,
    verify_zip_hash: bool = True,
    skip_date_check: bool = False,
    skip_existing_records: bool = False,
) -> Dict[str, int]:
    """Upload configured datasets to Zenodo.

    Iterates over the ``datasets`` list from config.json.  Each entry
    specifies a directory, collectors CSV, and dataset category.  This
    single loop replaces the former duplicate annular/total processing.

    Concurrency
    -----------
    When ``workers == 1`` (the default), datasets are uploaded one at a
    time in scan order — exactly the original behavior, no threads
    involved.  When ``workers > 1``, that many ESID datasets are
    uploaded **at the same time** using a thread pool.  Files within a
    single dataset are still uploaded sequentially; only the **outer
    loop over ESIDs** is parallelized.

    With concurrency, log lines from different ESIDs will interleave in
    ``azus_upload.log``.  Every key per-ESID log line is prefixed with
    ``[ESID XXX]`` so you can follow one dataset's progress by grepping:

        grep '\\[ESID 012\\]' azus_upload.log

    The three shared resources (``Records/uploaded_files.txt``, the
    result CSVs, and the in-memory ``stats`` counters) are guarded by
    locks created here and passed into the per-ESID worker.

    Args:
        datasets: List of dataset config dicts, each with keys:
            'name', 'dataset_dir', 'collectors_csv', 'dataset_category'.
        successful_results_file: CSV path for successful uploads.
        failure_results_file: CSV path for failed uploads.
        related_identifiers_csv: Global path to related identifiers CSV.
        references_csv: Global path to references CSV.
        auto_publish: Publish records after successful upload.
        delete_failures: Delete draft records on failure.
        reserve_doi: Reserve a DataCite DOI at draft creation time.
        project_config: Parsed project_config.json.
        esid_filter: Optional list of ESID number strings to upload.
            If ``None`` or empty, all discovered ESIDs are processed.
            The list's order becomes the upload order.  Passed through
            to :func:`get_upload_data`.
        workers: Number of ESID datasets to upload concurrently.
            ``1`` (default) means sequential — identical to the original
            behavior.  Values greater than 1 enable parallel uploads via
            a thread pool.  Must be >= 1.
        defer_zip: If True, upload everything EXCEPT each dataset's data
            ZIP and skip community-review submission.  Deferred datasets
            stay in ``Staging_Area/`` and are counted under the
            ``'deferred'`` stat.  Finish them later with
            ``Resources/finish_stuck_uploads.py``.
        draft_only: If True (the ``--draft-only`` flag), upload everything
            INCLUDING each ZIP but leave every record a plain editable
            draft — skip community-review submission and publishing.
            Mutually exclusive with ``defer_zip``.
        upload_attempts: Total number of PUT attempts for the data
            ZIP (``--upload-attempts`` CLI flag; companion files always
            keep the default).  Defaults to ``_DEFAULT_UPLOAD_ATTEMPTS``
            (3) so unmodified callers get identical behavior.
        title_guard: Duplicate-record guard (default True).  Before
            creating any fresh draft, the uploader searches the account
            for an existing record with the same title; an existing draft
            is resumed instead, an existing published record makes the
            dataset fail rather than duplicate.  Disable with
            ``--skip-title-guard``.
        skip_existing_records: When True (the ``--skip-existing-records``
            flag), each dataset is checked against Zenodo by title before
            any local hashing, and one that already exists there — as a
            draft or as a published record — is skipped and counted as
            skipped rather than uploaded.
        verify_zip_hash: When True (default), each dataset's pre-upload
            integrity gate re-hashes its ZIP against ``file_list.csv``.
            ``--skip-integrity-hash`` disables just the hash step; the
            sentinel and ZIP-contents checks always run.
        skip_date_check: Upload datasets whose WAV names carry no
            valid recording dates with the dates recorded as not
            available (``--skip-date-check``); forwarded per dataset.

    Returns:
        Dictionary with upload statistics:
        {'total_processed', 'successful', 'failed', 'skipped', 'deferred'}.

    Raises:
        ValueError: If ``datasets`` is empty (nothing configured to
            upload).
    """
    if not datasets:
        raise ValueError("No datasets configured for upload")

    # Defensive validation — ``main()`` already enforces this, but a direct
    # caller (e.g. a future automation script) might not.  Clamp to 1.
    if workers < 1:
        logger.warning(
            "workers=%d is invalid; clamping to 1 (sequential upload).",
            workers,
        )
        workers = 1

    # Derive the Records directory from the results file path so the tracker
    # sits alongside the other output CSVs (successful_results.csv, etc.).
    # This keeps all upload bookkeeping in one visible, well-known location
    # instead of a hidden dotfile scattered wherever the script was run from.
    records_dir = Path(successful_results_file).parent
    tracker_path = str(records_dir / UploadTracker.DEFAULT_FILENAME)
    tracker = UploadTracker(tracker_file=tracker_path)
    logger.info(
        "Upload tracker: %s — %d file(s) previously uploaded",
        tracker_path, tracker.get_count(),
    )

    stats = {
        "total_processed": 0,
        "successful": 0,
        "failed": 0,
        "skipped": 0,
        "deferred": 0,
    }

    # --- Locks for shared state (only consulted in the workers>1 path) ---
    # Created once and shared with every worker.  See the module-level
    # docstring on `_process_one_dataset` for what each lock protects.
    # In the workers==1 path, these are acquired by the (only) caller
    # thread — uncontended locks in Python are extremely cheap (a few
    # hundred nanoseconds), so we use the same code path for both modes
    # rather than maintaining two parallel implementations.
    tracker_lock = threading.Lock()
    results_lock = threading.Lock()
    stats_lock = threading.Lock()

    # --- Process each dataset category in a single unified loop ---
    for dataset_entry in datasets:
        dataset_name = dataset_entry.get("name", "Unnamed")
        dataset_dir = dataset_entry.get("dataset_dir", "")
        collectors_csv = dataset_entry.get("collectors_csv", "")
        dataset_category = dataset_entry.get("dataset_category", "")

        if not dataset_dir or not collectors_csv:
            logger.warning(
                "Skipping dataset '%s': missing dataset_dir or collectors_csv",
                dataset_name,
            )
            continue

        logger.info("=" * 70)
        logger.info("PROCESSING: %s", dataset_name)
        logger.info("=" * 70)

        rename_dir_files(directory=dataset_dir)

        category_upload_data = get_upload_data(
            data_dir=dataset_dir,
            data_collectors_file=collectors_csv,
            dataset_category=dataset_category,
            failure_results_file=failure_results_file,
            tracker=tracker,
            project_config=project_config,
            esid_filter=esid_filter,
            stats=stats,
        )

        total_in_category = len(category_upload_data)

        # ---------------------------------------------------------------
        # Build the keyword arguments common to every per-ESID call.
        # We bundle these once and reuse them in both the sequential
        # branch and the thread-pool branch below — this guarantees both
        # paths invoke `_process_one_dataset` with the exact same args.
        # ---------------------------------------------------------------
        common_kwargs: Dict[str, Any] = dict(
            delete_failures=delete_failures,
            auto_publish=auto_publish,
            reserve_doi=reserve_doi,
            related_identifiers_csv=related_identifiers_csv,
            references_csv=references_csv,
            project_config=project_config,
            successful_results_file=successful_results_file,
            failure_results_file=failure_results_file,
            tracker=tracker,
            tracker_lock=tracker_lock,
            results_lock=results_lock,
            stats=stats,
            stats_lock=stats_lock,
            defer_zip=defer_zip,
            draft_only=draft_only,
            upload_attempts=upload_attempts,
            title_guard=title_guard,
            verify_zip_hash=verify_zip_hash,
            skip_date_check=skip_date_check,
            skip_existing_records=skip_existing_records,
        )

        if workers == 1:
            # ===========================================================
            # SEQUENTIAL PATH (default — workers == 1).
            # No thread pool, no overhead.  This is exactly the original
            # behavior and the safest, most reliable mode of operation.
            # Use this unless you have a clear reason to upload datasets
            # concurrently.
            # ===========================================================
            for i, data in enumerate(category_upload_data, 1):
                _process_one_dataset(
                    index=i, total=total_in_category, data=data,
                    **common_kwargs,
                )
        else:
            # ===========================================================
            # CONCURRENT PATH (workers > 1).
            #
            # Uses Python's standard ThreadPoolExecutor — a pool of
            # worker threads, where each worker pulls one ESID dataset
            # off the queue, runs `_process_one_dataset` end-to-end on
            # it, then picks up the next one.
            #
            # Why threads (not processes)?
            #   Uploading files is **I/O-bound**: most of the time the
            #   thread is just waiting for the network.  During that
            #   wait, Python releases the GIL and lets other threads
            #   run.  This is the textbook use case for threads.
            #   Processes (multiprocessing) would add complexity
            #   (pickling, inter-process communication) without speed
            #   benefit for network I/O.
            #
            # Why `as_completed` instead of `executor.map`?
            #   `as_completed` lets us iterate over futures in the order
            #   they finish.  If we use `.result()` on each, any
            #   exception that escaped `_process_one_dataset` would be
            #   re-raised here.  In practice, `_process_one_dataset` has
            #   a top-level try/except and never lets an exception out —
            #   but calling `.result()` is still good hygiene: if an
            #   unexpected exception ever does escape, it surfaces
            #   loudly instead of being swallowed.
            #
            # Lifecycle:
            #   The `with` block guarantees `executor.shutdown(wait=True)`
            #   runs on exit, which waits for in-flight uploads to finish
            #   before returning — so the function does not return until
            #   every ESID has been processed (success or failure).
            # ===========================================================
            logger.info(
                "Concurrent upload enabled: %d ESID dataset(s) at a time. "
                "Look for [ESID XXX] prefixes in the log to follow each one.",
                workers,
            )
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="azus-upload",
            ) as executor:
                futures = [
                    executor.submit(
                        _process_one_dataset,
                        index=i, total=total_in_category, data=data,
                        **common_kwargs,
                    )
                    for i, data in enumerate(category_upload_data, 1)
                ]
                try:
                    for future in concurrent.futures.as_completed(futures):
                        # Re-raise any exception that escaped the worker.
                        # `_process_one_dataset` is designed to never raise,
                        # so this is a safety net for unexpected failures.
                        future.result()
                except KeyboardInterrupt:
                    # Without this, the `with` block's shutdown(wait=True)
                    # would silently run every queued dataset and wait for
                    # in-flight multi-GB uploads — Ctrl+C could take hours
                    # to act.  Set the abort event (workers check it
                    # between files), drop queued futures, and don't wait.
                    logger.warning(
                        "Interrupt received — cancelling queued datasets; "
                        "in-flight workers stop at their next file "
                        "boundary. Interrupted drafts stay resumable via "
                        "upload_state.json."
                    )
                    _ABORT_EVENT.set()
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise

    return stats


# ===================================================================
#  CLI entry point
# ===================================================================

def main() -> None:
    """Command-line entry point for AZUS standalone upload."""
    parser = argparse.ArgumentParser(
        description="AZUS Standalone Upload — Upload datasets to Zenodo"
    )
    parser.add_argument(
        "--config", type=str, default="Resources/config.json",
        help="Path to configuration file (default: Resources/config.json)",
    )
    parser.add_argument(
        "--skip-existing-records", action="store_true",
        help=(
            "Before uploading anything, ask Zenodo whether a record with "
            "this ESID's title already exists; if one does, skip the folder "
            "and move on to the next ESID. Both an unfinished DRAFT and an "
            "already PUBLISHED record count as existing. Skipped folders are "
            "counted as skipped (not failed) and are left untouched in the "
            "staging area. The check runs BEFORE the integrity gate, so a "
            "skipped folder costs one API search instead of re-hashing every "
            "archive. It queries Zenodo rather than reading "
            "upload_state.json, so a folder whose state file was lost is "
            "still recognised. Finish an existing draft with "
            "Resources/finish_stuck_uploads.py."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Validate configuration without uploading",
    )
    parser.add_argument(
        "--esid", nargs="+", metavar="ESID_OR_CSV",
        help=(
            "Upload only the specified ESID(s). Each value is either a "
            "literal ESID — a 1-3 digit number (leading zeros optional: "
            "'4', '04', and '004' all match ESID_004) or a suffixed id "
            "like 120A or 122_Part_1_of_2 — OR the path to a CSV whose "
            "FIRST column lists ESIDs — e.g. the output of "
            "esid_record_report.py / list_upload_states.py / "
            "esid_wav_inventory.py (a header row is detected and "
            "skipped automatically). The two forms can be mixed. "
            "Datasets are uploaded in the order given — the command-"
            "line order and/or the spreadsheet's row order. "
            "Examples: --esid 004 007  or  --esid esid_records.csv  "
            "or  --esid 004 esid_records.csv"
        ),
    )
    parser.add_argument(
        "--workers", type=int, default=1, metavar="N",
        help=(
            "Number of ESID datasets to upload AT THE SAME TIME (default: 1, "
            "sequential). Each worker uploads one complete dataset (all its "
            "files end-to-end) before picking up the next one. Files within "
            "a single dataset are still uploaded one at a time — only the "
            "outer 'one ESID after another' loop is parallelized. "
            "Recommended range: 1 to 4. Higher values send more parallel "
            "traffic to Zenodo and split your upload bandwidth across them. "
            "When N > 1, log lines from different ESIDs will interleave; "
            "use 'grep \"[ESID XXX]\" azus_upload.log' to follow one dataset."
        ),
    )
    parser.add_argument(
        "--upload-attempts", type=int, default=_DEFAULT_UPLOAD_ATTEMPTS,
        metavar="N",
        help=(
            "Total number of PUT attempts for the DATA ZIP before the "
            "dataset is marked failed and the run moves on (default: "
            f"{_DEFAULT_UPLOAD_ATTEMPTS}). N=1 means one shot with no "
            "retry; N=3 is the historical behavior with 30s / 90s "
            "backoffs between attempts. Valid range: 1 to 3. Applies "
            "ONLY to the multi-GB data ZIP — companion files (README, "
            "CSVs) always keep the default 3 attempts, and the "
            "draft-creation call has its own duplicate-guarded retry "
            "(3 attempts, 30s/90s backoffs) that never re-creates a "
            "draft the failed attempt actually made. If a run finishes "
            "with failures, 'python Resources/finish_stuck_uploads.py' "
            "remains the ESID-level retry loop."
        ),
    )
    parser.add_argument(
        "--skip-title-guard", action="store_true",
        help=(
            "DISABLE the duplicate-record guard. By default, before "
            "creating any new Zenodo record, AZUS searches your account "
            "for an existing record with the same title: an existing "
            "DRAFT is resumed instead of duplicated, and an existing "
            "PUBLISHED record makes the dataset fail rather than create "
            "a duplicate. Only skip this if you truly intend to create a "
            "second record with an identical title."
        ),
    )
    parser.add_argument(
        "--yes", action="store_true",
        help=(
            "Skip the interactive 'Proceed? (yes/no)' confirmation. "
            "REQUIRED for unattended runs (cron, CI, scripts): without "
            "it, a run whose stdin is not a terminal exits with an error "
            "instead of hanging or crashing on the prompt."
        ),
    )
    parser.add_argument(
        "--skip-integrity-hash", action="store_true",
        help=(
            "Skip ONLY the SHA-512 re-hash of each data ZIP during the "
            "pre-upload integrity check (saves one full read of every "
            "archive). The rest of the gate always runs: the "
            ".prep_complete sentinel, ZIP readability, and the "
            "ZIP-contents-vs-file_list.csv cross-check. Use only when "
            "re-running a batch whose ZIPs were already hash-verified."
        ),
    )
    parser.add_argument(
        "--skip-date-check", action="store_true",
        help=(
            "When a dataset's WAV filenames carry NO valid recording "
            "dates (unset AudioMoth clock -> 19700101_* names, or hex "
            "names from old firmware), upload it anyway with the "
            "recording dates recorded as not available — the record's "
            "Collected-dates metadata entry is omitted rather than "
            "invented. Datasets whose filenames DO parse keep their "
            "real dates — this is a per-dataset fallback, not a "
            "blanket override."
        ),
    )
    parser.add_argument(
        "--save-terminal-output", action="store_true",
        help=(
            "Also save everything this run prints to the screen as .txt "
            "files in the Records/ folder (the screen output itself is "
            "unchanged). Three kinds of file are written, all sharing "
            "one run-start timestamp: "
            "<date>_<time>_Standalone_terminal_output_pre.txt "
            "(everything before the first upload attempt), one "
            "<date>_<time>_ESID_<esid>_upload_attempt_001.txt per "
            "dataset attempt (the 001 counter increments so an existing "
            "file is never overwritten), and "
            "<date>_<time>_Standalone_terminal_output_post.txt "
            "(everything after — normally the summary). Output printed "
            "before the configuration file has been read is captured "
            "too, unless the run dies that early."
        ),
    )
    parser.add_argument(
        "--defer-zip", action="store_true",
        help=(
            "Two-phase upload, phase 1: create each Zenodo record, upload "
            "every file EXCEPT the big data ZIP, and reserve the DOI — but "
            "do NOT submit the record for community review yet. The dataset "
            "folder stays in Staging_Area/ with its upload_state.json, "
            "exactly like a 'stuck' upload. Phase 2: run "
            "'python Resources/finish_stuck_uploads.py --workers 1' to "
            "upload the ZIPs one at a time (full bandwidth each, fewest "
            "failures) and submit each record for review. Review is held "
            "back on purpose: if a community manager accepted a record "
            "before its ZIP arrived, the record would be published and "
            "could no longer accept files."
        ),
    )
    parser.add_argument(
        "--draft-only", action="store_true",
        help=(
            "Upload everything INCLUDING the data archives, but leave each "
            "record a plain, editable Zenodo draft: do NOT submit it to the "
            "community review queue and do NOT publish it (this overrides "
            "auto_publish in the config). Use it to review a fully uploaded "
            "record on Zenodo, then submit or publish it yourself. Cannot be "
            "combined with --defer-zip."
        ),
    )
    args = parser.parse_args()

    # --- --draft-only and --defer-zip are contradictory (ZIP in vs out) ---
    if args.draft_only and args.defer_zip:
        parser.error(
            "--draft-only and --defer-zip cannot be combined: --defer-zip "
            "skips the ZIP for a two-phase upload, while --draft-only uploads "
            "the ZIP and stops at a draft. Pick one."
        )

    # --- Validate --workers BEFORE any other work so errors come early ---
    if args.workers < 1:
        parser.error(
            f"--workers must be at least 1 (got {args.workers}). "
            "Use --workers 1 for sequential, or --workers 3 (etc.) to upload "
            "multiple datasets concurrently."
        )

    # --- Validate --upload-attempts (must fit inside the backoff tuple) ---
    if not (1 <= args.upload_attempts <= 3):
        parser.error(
            f"--upload-attempts must be between 1 and 3 (got "
            f"{args.upload_attempts}). N=1 means one shot per file with no "
            "retry; N=3 is the historical behavior with 30s / 90s backoffs."
        )

    # --- Configure logging ---
    logging.basicConfig(
        level=logging.INFO,
        format=_LOG_FORMAT,
        handlers=[
            logging.FileHandler("azus_upload.log"),
            logging.StreamHandler(sys.stdout),
        ],
    )

    # --- Attach the terminal-output tee (--save-terminal-output) ---
    # Attached before the first log line so nothing is missed; it
    # buffers in memory until the Records/ folder is known (activate()
    # below).  The screen and azus_upload.log handlers are untouched.
    global _TEE_HANDLER
    tee_handler: Optional[_TerminalTeeHandler] = None
    if args.save_terminal_output:
        tee_handler = _TerminalTeeHandler()
        tee_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        logging.getLogger().addHandler(tee_handler)
        _TEE_HANDLER = tee_handler

    # --- Expand --esid values (numbers and/or spreadsheet paths) ---
    # Done early so a bad value or unreadable file aborts before any
    # other work.  A junk token used to be a SILENT no-op filter that
    # matched nothing; now it is a hard usage error.
    esid_filter: Optional[List[str]] = None
    if args.esid:
        try:
            esid_filter = azus_common.load_esid_args(args.esid)
        except ValueError as exc:
            logger.error("%s", exc)
            sys.exit(2)

    # --- Load configuration ---
    config_path = Path(args.config)
    if not config_path.exists():
        logger.error("Configuration file not found: %s", config_path)
        sys.exit(1)

    logger.info("Loading configuration from: %s", config_path)
    with open(config_path, "r", encoding="utf-8") as fh:
        config_data = json.load(fh)

    if "uploads" not in config_data:
        logger.error("Invalid configuration: missing 'uploads' section")
        sys.exit(1)

    uploads_config = config_data["uploads"]

    # --- Activate the terminal-output tee now that Records/ is known ---
    # The Records dir is derived exactly like the tracker's: the parent
    # of the successful-results CSV.  The run-start timestamp is shared
    # by every .txt file this run writes.
    if tee_handler is not None:
        from datetime import datetime as _dt
        records_dir = Path(
            uploads_config.get(
                "successful_results_file", "Records/successful_results.csv"
            )
        ).parent
        tee_handler.activate(records_dir, _dt.now().strftime("%Y-%m-%d_%H%M"))
        logger.info("Saving a copy of terminal output to: %s", records_dir)

    # --- Load project identity ---
    project_config_path = config_data.get("project_config", None)
    project_config = load_project_config(project_config_path)

    # --- Validate credentials ---
    try:
        get_credentials_from_env()
        logger.info("Zenodo credentials loaded from environment")
    except ValueError as exc:
        logger.error("%s", exc)
        logger.error("Run: source Resources/set_env.sh")
        sys.exit(1)

    # --- Extract dataset list ---
    datasets = uploads_config.get("datasets", [])
    related_identifiers_csv = uploads_config.get("related_identifiers_csv", "")
    references_csv = uploads_config.get("references_csv", "")

    # --- Display configuration ---
    logger.info("=" * 70)
    logger.info("AZUS STANDALONE UPLOAD")
    logger.info("=" * 70)
    logger.info("Project: %s", project_config.get("project_name", "Unknown"))
    logger.info("Datasets configured: %d", len(datasets))
    for ds in datasets:
        logger.info("  • %s → %s", ds.get("name", "?"), ds.get("dataset_dir", "?"))
    logger.info(
        "Auto-publish: %s",
        uploads_config.get("auto_publish", False) and not args.draft_only,
    )
    logger.info("Delete failures: %s", uploads_config.get("delete_failures", False))
    logger.info("Reserve DOI: %s", uploads_config.get("reserve_doi", False))
    if esid_filter:
        logger.info(
            "ESID filter:  %s (all others will be skipped)",
            ", ".join(esid_filter),
        )
    else:
        logger.info("ESID filter:  none (all discovered ESIDs will be uploaded)")
    if args.workers > 1:
        logger.info(
            "Workers:      %d (uploading %d ESID datasets at a time)",
            args.workers, args.workers,
        )
    else:
        logger.info("Workers:      1 (sequential — one ESID dataset at a time)")
    if args.defer_zip:
        logger.info(
            "Defer ZIP:    ON — data ZIPs will NOT be uploaded and records "
            "will NOT be submitted for review this run. Finish later with "
            "Resources/finish_stuck_uploads.py."
        )
    if args.draft_only:
        logger.info(
            "Draft only:   ON — records are uploaded in full (ZIP included) "
            "but left as plain drafts: NO community-review submission and NO "
            "publish. Submit or publish them yourself when ready."
        )
    if args.upload_attempts != _DEFAULT_UPLOAD_ATTEMPTS:
        logger.info(
            "Upload attempts: %d per file (overridden from default %d)",
            args.upload_attempts, _DEFAULT_UPLOAD_ATTEMPTS,
        )
    if args.skip_title_guard:
        logger.warning(
            "Duplicate guard: DISABLED (--skip-title-guard) — records "
            "with titles that already exist on Zenodo WILL be created."
        )
    if args.skip_date_check:
        logger.warning(
            "Date fallback: ON (--skip-date-check) — datasets without "
            "valid WAV filename dates will be uploaded with NO "
            "recording-date metadata (dates not available)."
        )
    if args.skip_integrity_hash:
        logger.warning(
            "Integrity gate: sentinel + ZIP-contents checks ON; ZIP "
            "SHA-512 re-hash SKIPPED (--skip-integrity-hash)."
        )
    else:
        logger.info(
            "Integrity gate: ON — every dataset is verified (sentinel, "
            "ZIP contents vs file_list.csv, ZIP SHA-512) before upload."
        )
    logger.info("=" * 70)

    # --- CSV pre-validation ---
    if not args.dry_run:
        logger.info("VALIDATING CSV FILES")
        for ds in datasets:
            csv_file = ds.get("collectors_csv", "")
            category = ds.get("dataset_category", "")
            if csv_file:
                logger.info("Checking %s CSV: %s", ds.get("name", "?"), Path(csv_file).name)
                try:
                    collectors = parse_collectors_csv(
                        csv_file, category, project_config
                    )
                    logger.info("  Valid — %d records", len(collectors))
                except Exception as exc:
                    logger.error("  CSV validation failed: %s", exc)
                    logger.error(
                        "Fix with: python validate_csv.py %s --eclipse-type %s",
                        csv_file, category.lower(),
                    )
                    sys.exit(1)

    if args.dry_run:
        logger.info("Dry run complete — configuration is valid")
        sys.exit(0)

    # --- Confirmation prompt (bypass with --yes for unattended runs) ---
    if args.yes:
        logger.info("Confirmation skipped (--yes).")
    elif not sys.stdin.isatty():
        logger.error(
            "stdin is not a terminal, so the 'Proceed?' confirmation "
            "cannot be answered. Re-run with --yes for unattended use."
        )
        sys.exit(2)
    else:
        print("\n⚠️  You are about to upload datasets to Zenodo.")
        print("   This will create REAL records on Zenodo.")
        response = input("\nProceed? (yes/no): ")
        # The prompt is the only screen output that bypasses logging;
        # mirror it (and the answer) so the pre file stays a complete
        # record of the screen.
        if tee_handler is not None:
            tee_handler.mirror("⚠️  You are about to upload datasets to Zenodo.")
            tee_handler.mirror("   This will create REAL records on Zenodo.")
            tee_handler.mirror(f"Proceed? (yes/no): {response}")
        if response.lower() != "yes":
            logger.info("Upload cancelled by user")
            sys.exit(0)

    # --- Run upload ---
    try:
        stats = upload_datasets(
            datasets=datasets,
            successful_results_file=uploads_config.get(
                "successful_results_file", "Records/successful_results.csv"
            ),
            failure_results_file=uploads_config.get(
                "failure_results_file", "Records/failed_results.csv"
            ),
            related_identifiers_csv=related_identifiers_csv,
            references_csv=references_csv,
            auto_publish=uploads_config.get("auto_publish", False),
            delete_failures=uploads_config.get("delete_failures", False),
            reserve_doi=uploads_config.get("reserve_doi", False),
            project_config=project_config,
            esid_filter=esid_filter,
            workers=args.workers,
            defer_zip=args.defer_zip,
            draft_only=args.draft_only,
            upload_attempts=args.upload_attempts,
            title_guard=not args.skip_title_guard,
            verify_zip_hash=not args.skip_integrity_hash,
            skip_existing_records=args.skip_existing_records,
            skip_date_check=args.skip_date_check,
        )

        # --- Display summary ---
        logger.info("=" * 70)
        logger.info("UPLOAD SUMMARY")
        logger.info("=" * 70)
        logger.info("Total processed: %d", stats["total_processed"])
        logger.info("Successful:      %d", stats["successful"])
        logger.info("Failed:          %d", stats["failed"])
        logger.info("Skipped:         %d", stats["skipped"])
        if stats.get("deferred"):
            logger.info("Deferred:        %d (ZIP not uploaded yet)", stats["deferred"])
        if stats.get("aborted"):
            logger.info(
                "Aborted:         %d (Ctrl+C before start — re-run to upload)",
                stats["aborted"],
            )
        logger.info("=" * 70)

        if stats["failed"]:
            logger.warning(
                "%d upload(s) failed — check %s for details",
                stats["failed"],
                uploads_config.get("failure_results_file"),
            )
        if stats.get("deferred"):
            logger.info(
                "%d record(s) created without their data ZIP. Finish them with: "
                "python Resources/finish_stuck_uploads.py --workers 1",
                stats["deferred"],
            )

        sys.exit(0 if stats["failed"] == 0 else 1)

    except KeyboardInterrupt:
        logger.warning("Upload interrupted by user")
        sys.exit(130)
    except Exception as exc:
        logger.error("Fatal error: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
