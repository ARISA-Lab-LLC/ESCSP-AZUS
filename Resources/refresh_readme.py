#!/usr/bin/env python3
"""Refresh stale README files in prepared Staging_Area/ datasets.

WHAT THIS TOOL DOES
===================
When the README template changes, datasets already prepared in
``Staging_Area/`` keep their OLD ``README.md`` — both the standalone copy
and the copy sealed inside the dataset ZIP.  This tool finds every
staging folder whose ``README.md`` predates the current template (it
lacks the template's sentinel sentence) and brings it up to date.

For each stale folder it:

  1. Regenerates ``README.html`` and ``README.md`` from the current
     template (``Resources/README_template.html`` or ``--readme-template``)
     using the site's row from the collector CSV.
  2. Removes the old ``README.md`` from the dataset ZIP and adds the new
     one in its place.
  3. Rebuilds ``file_list.csv`` so the dataset still passes the upload
     integrity gate.

Step 3 is NOT optional.  ``standalone_tasks.verify_dataset_integrity``
refuses to upload a dataset whose ZIP SHA-512 does not match the ZIP-row
hash recorded in ``file_list.csv``.  Swapping ``README.md`` inside the
archive changes that hash, so this tool recomputes:

  * the **ZIP row** (new SHA-512 + size) in the standalone
    ``file_list.csv``, and
  * the **README.md row** (new SHA-512 + size) in BOTH the standalone
    ``file_list.csv`` and the copy sealed inside the ZIP —

so the refreshed dataset is internally consistent and uploadable.  No
WAV file is re-hashed: only the two rows that actually changed are
touched.

The dataset's gigabytes of audio are copied entry-by-entry into a new
archive (ZIP has no in-place entry replacement), so refreshing a large
site takes time proportional to its size.  Use ``--list-only`` first to
see the scope.

The rewrite is crash-safe: the ZIP and ``file_list.csv`` are written to
temporary files and swapped in atomically, and the standalone
``README.md`` (this tool's own up-to-date marker) is written LAST, so an
interrupted run is simply redone on the next invocation.

USAGE
=====
From the project root::

    python Resources/refresh_readme.py /path/to/Staging_Area \\
        --config Resources/config.json
    python Resources/refresh_readme.py /path/to/Staging_Area \\
        --collector-csv Resources/collectors.csv --list-only
    python Resources/refresh_readme.py /path/to/Staging_Area \\
        --config Resources/config.json \\
        --readme-template Resources/README_template.html

EXIT CODES
==========
* ``0`` — every stale folder refreshed (or nothing stale, or
  ``--list-only``)
* ``1`` — at least one folder failed to refresh
* ``2`` — usage error (Staging_Area missing, template missing or itself
  lacking the sentinel, collector CSV unresolved/missing)
"""

import argparse
import csv
import hashlib
import io
import json
import logging
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import azus_common
from azus_common import calculate_sha512
from prepare_dataset import (
    _FILE_LIST_HEADERS,
    ZIP_MODE_SINGLE,
    create_readme_html,
    create_readme_md,
    extract_collector_data,
    staging_zip_mode,
)

logger = logging.getLogger("azus.refresh_readme")

# The sentinel sentence the CURRENT template adds.  A README.md that does
# not contain this string was generated from an older template and needs
# to be refreshed.  Kept verbatim (do not reflow) so the substring test
# matches the rendered text exactly.
#
# UPDATE THIS whenever README_template.html's wording changes — that is the
# mechanism by which existing records become "stale" and get refreshed, and
# leaving it pointing at removed wording makes every README read as stale
# while refresh_folder then refuses to write one (it re-checks the sentinel
# in its own output).  This value tracks the July 2026 per-day rewording,
# which replaced "all available audio files are included individually".
#
# It must also sit on a SINGLE line of README_template.html: the check runs
# against README.md, whose converter joins wrapped lines, but the test suite
# and refresh_folder both also look for it in the HTML, where a sentence
# split across two template lines is not contiguous.
_SENTINEL = (
    "at the end of the version number indicates that the record has "
    "multiple zip files."
)

# Canonical README.md manifest fields, matching the auto-generated row in
# prepare_dataset.create_internal_file_list.  Used only when a stale
# folder's file_list.csv has no README.md row to update in place.
_README_FILE_TYPE = "Markdown (.md)"
_README_DESCRIPTION = (
    "Human and machine-readable documentation describing the dataset, "
    "collection methodology, site location, and data usage guidelines."
)

_ZIP_COPY_CHUNK = 1024 * 1024  # 1 MiB streaming copy for archive entries


# ===================================================================
#  Staleness scan
# ===================================================================

def readme_is_current(readme_path: Path) -> bool:
    """Report whether a README.md already contains the template sentinel.

    Args:
        readme_path: Path to a standalone ``README.md``.

    Returns:
        True when the file exists and contains :data:`_SENTINEL`; False
        when it is missing the sentinel or cannot be read as text.
    """
    try:
        return _SENTINEL in readme_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False


def scan_staging(
    staging_root: Path,
) -> Tuple[List[Tuple[str, Path]], List[Tuple[str, Path, str]]]:
    """Classify every staging folder as stale, current, or anomalous.

    A folder is *stale* (selected for refresh) when its ``README.md``
    exists, lacks the sentinel, AND the single dataset ZIP is present to
    swap the README inside.  Folders that are already current, or that
    cannot be refreshed (no ``README.md``, no ZIP, or a per-day layout
    whose archives carry no README to swap), are reported separately with
    their reason so nothing is skipped silently.

    Args:
        staging_root: The ``Staging_Area`` folder holding
            ``ESID_NNN_Staging`` subfolders.

    Returns:
        A ``(stale, skipped)`` tuple.  ``stale`` holds ``(esid, folder)``
        pairs in ESID order; ``skipped`` holds ``(esid, folder, reason)``
        for folders that were not selected (current or anomalous).
    """
    stale: List[Tuple[str, Path]] = []
    skipped: List[Tuple[str, Path, str]] = []
    for _, esid, folder in azus_common.find_esid_folders(staging_root):
        readme = folder / "README.md"
        if not readme.is_file():
            skipped.append((esid, folder, "no README.md — cannot refresh"))
            continue
        if readme_is_current(readme):
            skipped.append((esid, folder, "already current"))
            continue
        # This tool swaps README.md INSIDE the dataset archive, so it needs
        # the single-archive layout.  Per-day archives carry no metadata at
        # all, which makes their refresh a companion-only rewrite with no
        # archive surgery — genuinely simpler, but a different routine, and
        # a later phase.  Report the real reason rather than "no ZIP".
        mode = staging_zip_mode(folder, esid)
        if mode is not None and mode != ZIP_MODE_SINGLE:
            skipped.append((
                esid, folder,
                f"stale README but a {mode} ZIP layout — per-day refresh "
                "is not supported yet",
            ))
            continue
        zip_path = folder / f"ESID_{esid}.zip"
        if not zip_path.is_file():
            skipped.append(
                (esid, folder, "stale README but no ZIP — cannot refresh")
            )
            continue
        stale.append((esid, folder))
    return stale, skipped


def write_readme_state_report(
    csv_path: Path,
    stale: List[Tuple[str, Path]],
    skipped: List[Tuple[str, Path, str]],
) -> int:
    """Write a two-column CSV of every ESID folder's README state.

    Columns are ``ESID`` and ``README State``: the state is ``Current``
    only when the folder's README.md carries the template sentinel, and
    ``Stale`` otherwise (a missing README or an absent ZIP counts as Stale
    — the folder is not up to date).  Built from a completed
    :func:`scan_staging` so no folder is read twice; rows are in ESID
    order (canonical ESIDs are zero-padded, so a lexical sort matches).

    Args:
        csv_path: Destination CSV path.
        stale: The scan's stale ``(esid, folder)`` pairs.
        skipped: The scan's ``(esid, folder, reason)`` entries.

    Returns:
        The number of ESID folders written.
    """
    rows = [(esid, "Stale") for esid, _ in stale]
    rows += [
        (esid, "Current" if readme_is_current(folder / "README.md")
         else "Stale")
        for esid, folder, _ in skipped
    ]
    rows.sort()
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["ESID", "README State"])
        writer.writerows(rows)
    return len(rows)


# ===================================================================
#  file_list.csv row helpers
# ===================================================================

def _load_rows(path: Path) -> List[Dict[str, str]]:
    """Read a file_list.csv into a list of row dicts.

    Args:
        path: Path to the CSV file.

    Returns:
        The rows as dicts keyed by :data:`_FILE_LIST_HEADERS`.
    """
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _rows_to_bytes(rows: List[Dict[str, str]]) -> bytes:
    """Serialize file_list rows to CSV bytes in the canonical column order.

    Matches the on-disk format prepare_dataset.py writes (``\\r\\n`` line
    endings, UTF-8), so a rebuilt list is byte-compatible with a prepared
    one.

    Args:
        rows: Row dicts to serialize.

    Returns:
        The UTF-8-encoded CSV text.
    """
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf, fieldnames=_FILE_LIST_HEADERS, extrasaction="ignore"
    )
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _canonical_readme_row(size: int, sha512: str) -> Dict[str, str]:
    """Build a README.md manifest row from scratch.

    Args:
        size: README.md size in bytes.
        sha512: Hex SHA-512 digest of the README.md bytes.

    Returns:
        A row dict with the canonical README.md metadata.
    """
    return {
        "File Name": "README.md",
        "File Type": _README_FILE_TYPE,
        "Description": _README_DESCRIPTION,
        "File size (KB)": f"{size / 1024:.2f}",
        "File size (Bytes)": str(size),
        "Associated Data Dictionary": "N/A",
        "SHA-512 Hash": sha512,
        "Notes": "",
    }


def _apply_readme_row(
    rows: List[Dict[str, str]], size: int, sha512: str, insert_at: int
) -> None:
    """Update the README.md row's size + hash in place, or insert one.

    Only the size and hash fields change when a README.md row is present;
    every other field (including any project-specific Description text) is
    preserved.  When no README.md row exists, a canonical one is inserted
    at ``insert_at``.

    Args:
        rows: The row list to modify in place.
        size: New README.md size in bytes.
        sha512: New hex SHA-512 digest of the README.md bytes.
        insert_at: Index to insert a canonical row at when none exists.
    """
    kb = f"{size / 1024:.2f}"
    for row in rows:
        if (row.get("File Name") or "").strip() == "README.md":
            row["File size (KB)"] = kb
            row["File size (Bytes)"] = str(size)
            row["SHA-512 Hash"] = sha512
            return
    rows.insert(insert_at, _canonical_readme_row(size, sha512))


def _update_zip_row(
    rows: List[Dict[str, str]], zip_name: str, size: int, sha512: str
) -> bool:
    """Update the ZIP row's size + hash in place.

    Args:
        rows: The row list to modify in place.
        zip_name: The archive's file name (e.g. ``ESID_064.zip``).
        size: New ZIP size in bytes.
        sha512: New hex SHA-512 digest of the ZIP.

    Returns:
        True if a matching ZIP row was found and updated; False otherwise.
    """
    for row in rows:
        if (row.get("File Name") or "").strip() == zip_name:
            row["File size (KB)"] = f"{size / 1024:.2f}"
            row["File size (Bytes)"] = str(size)
            row["SHA-512 Hash"] = sha512
            return True
    return False


def _write_rows_atomic(path: Path, rows: List[Dict[str, str]]) -> None:
    """Write file_list rows to ``path`` atomically (temp file + replace).

    Args:
        path: Destination CSV path.
        rows: Row dicts to write.
    """
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=_FILE_LIST_HEADERS, extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)


# ===================================================================
#  ZIP rewrite
# ===================================================================

def _clone_zipinfo(info: zipfile.ZipInfo) -> zipfile.ZipInfo:
    """Copy the fields needed to re-add an entry, dropping stale ones.

    A fresh :class:`zipfile.ZipInfo` avoids carrying the source archive's
    CRC / compressed-size / header-offset fields, which the writer
    recomputes.  ``file_size`` IS carried over: when the entry is streamed
    back in via ``ZipFile.open(clone, "w")``, zipfile reserves a ZIP64
    header only when ``file_size * 1.05 > ZIP64_LIMIT`` (~2 GiB), so a
    clone left at ``file_size = 0`` would reserve none and then fail with
    "File size too large" on a >2 GiB entry.  Setting it mirrors the
    normal ``ZipFile.write(path)`` path (which stats the file first) and
    is overwritten by ``writestr`` for the small metadata entries.

    Args:
        info: The source entry's ZipInfo.

    Returns:
        A new ZipInfo with the same name, timestamp, compression,
        attributes, and uncompressed size.
    """
    clone = zipfile.ZipInfo(info.filename, date_time=info.date_time)
    clone.compress_type = info.compress_type
    clone.external_attr = info.external_attr
    clone.internal_attr = info.internal_attr
    clone.create_system = info.create_system
    clone.file_size = info.file_size
    return clone


def rewrite_zip_readme(
    zip_path: Path,
    esid: str,
    new_readme: bytes,
    new_internal_file_list: bytes,
) -> Tuple[str, int]:
    """Rewrite the ZIP with a new README.md and internal file_list.csv.

    ZIP archives have no in-place entry replacement, so every other entry
    (the WAV audio, CONFIG.TXT, metadata) is streamed verbatim into a new
    archive while the ``README.md`` and ``file_list.csv`` entries are
    replaced with the supplied bytes, each keeping its original arcname,
    timestamp, and compression.  The new archive is written to a temp file
    and swapped in with :func:`os.replace`, so the original is never left
    partially written.

    Args:
        zip_path: The dataset ZIP to rewrite.
        esid: Canonical ESID (used to build arcnames if an entry is
            absent).
        new_readme: New ``README.md`` bytes to seal into the archive.
        new_internal_file_list: New internal ``file_list.csv`` bytes (the
            manifest WITHOUT the ZIP row).

    Returns:
        A ``(sha512, size_bytes)`` tuple for the finalized archive.

    Raises:
        zipfile.BadZipFile: If the source archive is not readable.
    """
    tmp = zip_path.with_name(f".{zip_path.name}.refresh.tmp")
    readme_arc = f"ESID_{esid}/README.md"
    filelist_arc = f"ESID_{esid}/file_list.csv"
    saw_readme = False
    saw_filelist = False
    try:
        with zipfile.ZipFile(zip_path, "r") as zin, zipfile.ZipFile(
            tmp, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False
        ) as zout:
            for info in zin.infolist():
                base = info.filename.rsplit("/", 1)[-1]
                if base == "README.md":
                    out = _clone_zipinfo(info)
                    out.compress_type = zipfile.ZIP_DEFLATED
                    zout.writestr(out, new_readme)
                    readme_arc = info.filename
                    saw_readme = True
                elif base == "file_list.csv":
                    out = _clone_zipinfo(info)
                    out.compress_type = zipfile.ZIP_DEFLATED
                    zout.writestr(out, new_internal_file_list)
                    filelist_arc = info.filename
                    saw_filelist = True
                else:
                    with zin.open(info) as src, zout.open(
                        _clone_zipinfo(info), "w"
                    ) as dst:
                        shutil.copyfileobj(src, dst, _ZIP_COPY_CHUNK)
            if not saw_readme:
                logger.warning(
                    "  ZIP had no README.md entry — adding %s", readme_arc
                )
                ri = zipfile.ZipInfo(readme_arc)
                ri.compress_type = zipfile.ZIP_DEFLATED
                zout.writestr(ri, new_readme)
            if not saw_filelist:
                logger.warning(
                    "  ZIP had no file_list.csv entry — adding %s", filelist_arc
                )
                fi = zipfile.ZipInfo(filelist_arc)
                fi.compress_type = zipfile.ZIP_DEFLATED
                zout.writestr(fi, new_internal_file_list)

        sha512 = calculate_sha512(str(tmp))
        size = tmp.stat().st_size
        os.replace(tmp, zip_path)
        return sha512, size
    finally:
        if tmp.exists():
            tmp.unlink()


def _verify_refreshed(zip_path: Path) -> List[str]:
    """Confirm the rewritten ZIP is structurally sound (cheap re-open).

    Reads only the two small swapped entries — it deliberately does NOT
    decompress the gigabytes of audio or re-hash the whole archive.  The
    ZIP-row hash was taken from the exact bytes atomically renamed into
    place (the same trust model as prepare_dataset's own
    ``create_external_file_list``), and ``verify_dataset_integrity``
    re-hashes the full archive at upload time.  This immediate check just
    proves the swap landed: the archive opens, the README.md entry now
    carries the sentinel, and an internal file_list.csv entry is present.

    Args:
        zip_path: The finalized dataset ZIP.

    Returns:
        A list of human-readable problems; empty means sound.
    """
    problems: List[str] = []
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            readme_entry = next(
                (n for n in names if n.rsplit("/", 1)[-1] == "README.md"), None
            )
            if readme_entry is None:
                problems.append("no README.md entry in refreshed ZIP")
            elif _SENTINEL not in zf.read(readme_entry).decode(
                "utf-8", "replace"
            ):
                problems.append("refreshed ZIP README.md still lacks sentinel")
            if not any(
                n.rsplit("/", 1)[-1] == "file_list.csv" for n in names
            ):
                problems.append("no file_list.csv entry in refreshed ZIP")
    except (zipfile.BadZipFile, OSError) as exc:
        problems.append(f"refreshed ZIP not readable ({exc})")
    return problems


# ===================================================================
#  Per-folder refresh
# ===================================================================

def refresh_folder(
    esid: str,
    folder: Path,
    collector_csv: Path,
    template_path: Path,
) -> None:
    """Refresh one stale staging folder's README + ZIP + file_list.csv.

    Regenerates the README from the current template, swaps it inside the
    ZIP, and rebuilds ``file_list.csv`` (README.md row + ZIP row) so the
    dataset still passes the upload integrity gate.  The standalone
    ``README.md`` is written LAST, so an interrupted run leaves the folder
    still marked stale and is simply redone next time.

    Args:
        esid: Canonical ESID for this folder.
        folder: The ``ESID_NNN_Staging`` folder.
        collector_csv: Collector CSV providing the site's metadata row.
        template_path: README HTML template to render from.

    Raises:
        RuntimeError: If the collector row is missing, the regenerated
            README still lacks the sentinel, ``file_list.csv`` is missing
            or has no ZIP row, or the post-rewrite consistency check fails.
    """
    collector_data = extract_collector_data(collector_csv, esid)
    if collector_data is None:
        raise RuntimeError(
            f"no collector row for ESID {esid} in {collector_csv}"
        )

    file_list_path = folder / "file_list.csv"
    if not file_list_path.is_file():
        raise RuntimeError(f"no file_list.csv in {folder.name}")
    external_rows = _load_rows(file_list_path)
    zip_name = f"ESID_{esid}.zip"
    if not any(
        (r.get("File Name") or "").strip() == zip_name for r in external_rows
    ):
        raise RuntimeError(
            f"file_list.csv in {folder.name} has no ZIP row for {zip_name}"
        )

    # 1. Regenerate README into a temp dir; read the bytes (do not touch
    #    the standalone files yet — README.md is written last).
    with tempfile.TemporaryDirectory() as td:
        tmp_dir = Path(td)
        create_readme_html(collector_data, tmp_dir, template_path)
        create_readme_md(tmp_dir / "README.html", tmp_dir)
        new_html = (tmp_dir / "README.html").read_bytes()
        new_md = (tmp_dir / "README.md").read_bytes()

    if _SENTINEL not in new_md.decode("utf-8", "replace"):
        raise RuntimeError(
            f"regenerated README.md for ESID {esid} still lacks the sentinel "
            "— the template did not inject it; ZIP left untouched"
        )

    new_md_size = len(new_md)
    new_md_sha = hashlib.sha512(new_md).hexdigest()

    # 2. Build the internal file_list.csv (external rows minus the ZIP row,
    #    README.md row refreshed) to seal inside the archive.
    internal_rows = [
        dict(r)
        for r in external_rows
        if (r.get("File Name") or "").strip() != zip_name
    ]
    _apply_readme_row(internal_rows, new_md_size, new_md_sha, insert_at=0)
    internal_bytes = _rows_to_bytes(internal_rows)

    # 3. Rewrite the ZIP (README.md + internal file_list.csv swapped).
    logger.info("  Rewriting ZIP %s (streaming all entries)...", zip_name)
    new_zip_sha, new_zip_size = rewrite_zip_readme(
        folder / zip_name, esid, new_md, internal_bytes
    )

    # 4. Rebuild the standalone (external) file_list.csv: README.md row +
    #    the now-changed ZIP row.
    _apply_readme_row(external_rows, new_md_size, new_md_sha, insert_at=1)
    _update_zip_row(external_rows, zip_name, new_zip_size, new_zip_sha)
    _write_rows_atomic(file_list_path, external_rows)

    # 5. Confirm the refreshed archive is structurally sound before we
    #    mark the folder done.
    problems = _verify_refreshed(folder / zip_name)
    if problems:
        raise RuntimeError(
            f"post-refresh verification failed for ESID {esid}: "
            + "; ".join(problems)
        )

    # 6. Update the standalone README.html, then README.md LAST — its
    #    sentinel is this tool's up-to-date marker, so writing it last
    #    means an interrupted run stays flagged stale and is simply redone.
    #    The upload manifest is intentionally left as-is: it lists the same
    #    file names (only File Name is consumed at upload), so a README
    #    content change needs no manifest edit.
    (folder / "README.html").write_bytes(new_html)
    (folder / "README.md").write_bytes(new_md)
    logger.info("  ESID %s refreshed.", esid)


# ===================================================================
#  Collector CSV resolution + CLI
# ===================================================================

def resolve_collector_csv(
    collector_csv_arg: Optional[str], config_arg: Optional[str]
) -> Path:
    """Resolve the collector CSV path from --collector-csv or --config.

    Mirrors prepare_dataset.py: an explicit ``--collector-csv`` wins;
    otherwise the path is read from the first dataset entry in
    ``config.json`` (``uploads.datasets[0].collectors_csv``).

    Args:
        collector_csv_arg: Value of ``--collector-csv`` (or None).
        config_arg: Value of ``--config`` (or None).

    Returns:
        The resolved, existing collector CSV path.

    Raises:
        SystemExit: With code 2 if the path cannot be resolved or the
            file does not exist.
    """
    collector_csv_str = collector_csv_arg
    if not collector_csv_str and config_arg:
        config_path = Path(config_arg)
        if not config_path.exists():
            logger.error("Config file not found: %s", config_path)
            sys.exit(2)
        with open(config_path, "r", encoding="utf-8") as cfg_fh:
            config_data = json.load(cfg_fh)
        datasets = config_data.get("uploads", {}).get("datasets", [])
        if not datasets:
            logger.error(
                "No uploads.datasets entries in config.json — cannot "
                "determine collectors_csv path."
            )
            sys.exit(2)
        collector_csv_str = datasets[0].get("collectors_csv", "")
        if not collector_csv_str:
            logger.error(
                "collectors_csv is empty in config.json datasets[0] — add "
                "the path or use --collector-csv."
            )
            sys.exit(2)
        logger.info("Using collectors_csv from config.json: %s", collector_csv_str)

    if not collector_csv_str:
        logger.error(
            "Collector CSV path not provided. Supply --collector-csv or "
            "--config pointing to a config.json with "
            "uploads.datasets[0].collectors_csv set."
        )
        sys.exit(2)

    collector_csv = Path(collector_csv_str)
    if not collector_csv.exists():
        logger.error("Collector CSV not found: %s", collector_csv)
        sys.exit(2)
    return collector_csv


def main() -> None:
    """Command-line entry point.  See the module docstring for usage."""
    parser = argparse.ArgumentParser(
        description=(
            "Find prepared datasets in STAGING_AREA whose README.md predates "
            "the current template and refresh the README (standalone + inside "
            "the ZIP), rebuilding file_list.csv so each stays uploadable."
        ),
    )
    parser.add_argument(
        "staging_area", metavar="STAGING_AREA",
        help="Staging_Area folder holding ESID_NNN_Staging subfolders.",
    )
    parser.add_argument(
        "--config",
        help=(
            "Path to AZUS config.json; collectors_csv is read from its first "
            "uploads.datasets entry when --collector-csv is not given."
        ),
    )
    parser.add_argument(
        "--collector-csv",
        help="Path to the collectors CSV (overrides --config).",
    )
    parser.add_argument(
        "--readme-template",
        help=(
            "README HTML template (default: Resources/README_template.html). "
            "Must contain the current sentinel sentence."
        ),
    )
    parser.add_argument(
        "--list-only", action="store_true",
        help="List stale folders without modifying anything.",
    )
    parser.add_argument(
        "--report", metavar="CSV",
        help=(
            "Also write a CSV of every ESID folder's README state "
            "(columns: ESID, README State = Stale|Current) to this path, "
            "reflecting the state found at scan time. Combine with "
            "--list-only for a report-only run."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    staging_root = Path(args.staging_area)
    if not staging_root.is_dir():
        logger.error("Staging_Area folder not found: %s", staging_root)
        sys.exit(2)

    if args.readme_template:
        template_path = Path(args.readme_template)
    else:
        template_path = azus_common.PROJECT_ROOT / "Resources" / "README_template.html"
    if not template_path.is_file():
        logger.error("README template not found: %s", template_path)
        sys.exit(2)
    # Guard: refreshing from a template that itself lacks the sentinel would
    # produce READMEs that STILL look stale (and pointlessly rewrite ZIPs).
    if _SENTINEL not in template_path.read_text(encoding="utf-8"):
        logger.error(
            "Template %s does not contain the current sentinel sentence — "
            "it is not the up-to-date template. Aborting.", template_path,
        )
        sys.exit(2)

    logger.info("=" * 70)
    logger.info("REFRESH STALE README FILES IN STAGING_AREA")
    logger.info("=" * 70)
    logger.info("Scanning: %s", staging_root)
    logger.info("Template: %s", template_path)

    stale, skipped = scan_staging(staging_root)

    current = [s for s in skipped if s[2] == "already current"]
    anomalies = [s for s in skipped if s[2] != "already current"]
    logger.info("%d folder(s) already current.", len(current))
    for esid, folder, reason in anomalies:
        logger.warning("  ESID %s SKIPPED (%s): %s", esid, reason, folder)

    # Snapshot the state found at scan time (before any refresh).
    if args.report:
        try:
            count = write_readme_state_report(
                Path(args.report), stale, skipped
            )
        except OSError as exc:
            logger.error("Could not write report %s: %s", args.report, exc)
            sys.exit(2)
        logger.info(
            "Wrote README state report: %s (%d folder(s))", args.report, count,
        )

    if not stale:
        logger.info("No stale README.md files — nothing to do.")
        sys.exit(0)

    logger.info("%d folder(s) with a stale README.md:", len(stale))
    for esid, folder in stale:
        logger.info("  ESID %s  %s", esid, folder)

    if args.list_only:
        logger.info("--list-only: not modifying anything.")
        sys.exit(0)

    # Resolve the collector CSV only when there is real work to do.
    collector_csv = resolve_collector_csv(args.collector_csv, args.config)

    failures: List[str] = []
    for esid, folder in stale:
        logger.info("=" * 70)
        logger.info("[ESID %s] Refreshing %s", esid, folder)
        try:
            refresh_folder(esid, folder, collector_csv, template_path)
        except (RuntimeError, OSError, zipfile.BadZipFile) as exc:
            logger.error("[ESID %s] Refresh FAILED: %s", esid, exc)
            failures.append(esid)

    logger.info("=" * 70)
    logger.info(
        "Refresh complete: %d succeeded, %d failed.",
        len(stale) - len(failures), len(failures),
    )
    if failures:
        logger.error("Failed ESIDs: %s", ", ".join(failures))
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
