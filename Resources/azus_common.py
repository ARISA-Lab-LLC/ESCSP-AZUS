#!/usr/bin/env python3
"""Shared helpers for the AZUS pipeline and its Resources/ tools.

WHY THIS MODULE EXISTS
======================
Before it, the boilerplate below was copy-pasted across up to eight
tools — and the copies had already drifted (four different ESID regex
variants, three independent definitions of the ``.prep_complete``
sentinel name, two copies of ``calculate_sha512``).  Drift in these
particular helpers is dangerous: the sentinel name and the ESID parse
are semantics every tool must agree on, or one tool silently ignores a
dataset another tool reports.

Everything here is dependency-free (stdlib only) and side-effect-free
at import time, so any script can import it safely.

IMPORT PATTERNS
===============
* Tools inside ``Resources/`` (run as scripts, so ``Resources/`` is on
  ``sys.path``)::

      import azus_common

* Modules at the project root (``standalone_tasks.py``)::

      sys.path.insert(0, str(Path(__file__).resolve().parent / "Resources"))
      import azus_common

WHAT DELIBERATELY STAYS OUT
===========================
Domain logic (Zenodo API calls, WAV/RIFF analysis, upload orchestration)
stays in its owning module — this file is for the cross-cutting
constants and small pure helpers only.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------
# Project layout
# ---------------------------------------------------------------------
# This file lives in Resources/ inside the project root.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
STAGING_AREA = PROJECT_ROOT / "Staging_Area"
UPLOADED_DATA = PROJECT_ROOT / "Uploaded_Data"

# ---------------------------------------------------------------------
# Cross-tool contract constants
# ---------------------------------------------------------------------
# Touched by prepare_dataset.py as its VERY LAST action; every other
# tool treats its presence as "preparation finished cleanly".
PREP_SENTINEL = ".prep_complete"

# Written by the uploader immediately after draft creation; links a
# staging folder to its Zenodo draft so re-runs RESUME instead of
# creating a duplicate record.
STATE_FILENAME = "upload_state.json"

# Read-buffer size for streaming file hashes (64 KB).
HASH_BUFFER_SIZE = 65_536

# ---------------------------------------------------------------------
# ESID parsing — THE single definition of a valid ESID token
# ---------------------------------------------------------------------
# Project invariant: ESIDs are 000–999, written in folder/file names as
# ESID_073, ESID#73, ESID_073_Staging, ESID_073_Uploaded, etc.
# (Title-pattern matching in the Zenodo report tools is deliberately
# separate — record titles have their own user-configurable patterns.)
_ESID_NAME_RE = re.compile(r"^ESID[_#](\d{1,3})(?!\d)", re.IGNORECASE)


def parse_esid(name: str) -> Optional[str]:
    """Extract the zero-padded 3-digit ESID from a folder/file name.

    Accepts every naming variant the pipeline produces: ``ESID_073``,
    ``ESID#73``, ``ESID_4``, ``ESID_073_Staging``, ``ESID_073.zip``.
    Enforces the project invariant that ESIDs are 000–999: a name whose
    digit run is longer than three digits (``ESID_0733``) is malformed
    and returns None rather than being silently reinterpreted.

    Args:
        name: A folder or file basename (not a full path).

    Returns:
        The ESID as a zero-padded 3-digit string (``"073"``), or None
        when the name carries no valid ESID token.
    """
    m = _ESID_NAME_RE.match(name.strip())
    if m is None:
        return None
    return f"{int(m.group(1)):03d}"


def find_esid_folders(root: Path) -> List[Tuple[int, str, Path]]:
    """Find every ESID_NNN subdirectory of ``root``, sorted numerically.

    Non-ESID directories (``.DS_Store`` siblings, ``backup``, etc.) are
    silently ignored.  The padded form is always 3-digit.

    Args:
        root: Parent folder containing ESID directories.

    Returns:
        List of ``(numeric_esid, padded_str, folder_path)`` tuples in
        ascending numeric order.  Empty when ``root`` is not a
        directory or has no matching subdirectories.
    """
    found: List[Tuple[int, str, Path]] = []
    if not root.is_dir():
        return found
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        padded = parse_esid(entry.name)
        if padded is None:
            continue
        found.append((int(padded), padded, entry))
    found.sort(key=lambda t: t[0])
    return found


# ---------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------

def calculate_digests(filepath: str, algorithms: Tuple[str, ...]) -> dict:
    """Compute several digests of a file in ONE streaming read.

    Multi-GB ZIPs used to be read end-to-end once per algorithm (SHA-512
    for the manifest check, md5 for the Zenodo checksum) — feeding every
    hasher from the same 64 KB chunk loop halves that disk traffic.

    Args:
        filepath: Path to the file.
        algorithms: hashlib algorithm names, e.g. ``("sha512", "md5")``.

    Returns:
        Dict mapping each algorithm name to its hex digest.
    """
    hashers = {name: hashlib.new(name) for name in algorithms}
    with open(filepath, "rb") as fh:
        for chunk in iter(lambda: fh.read(HASH_BUFFER_SIZE), b""):
            for hasher in hashers.values():
                hasher.update(chunk)
    return {name: hasher.hexdigest() for name, hasher in hashers.items()}


def calculate_sha512(filepath: str) -> str:
    """Calculate the SHA-512 hash of a file, streaming in 64 KB chunks.

    Args:
        filepath: Path to the file.

    Returns:
        Hex-encoded SHA-512 digest string.
    """
    return calculate_digests(filepath, ("sha512",))["sha512"]


def configure_logging(verbose: bool = False) -> None:
    """Set up the standard AZUS log format on stdout.

    Args:
        verbose: When True, log at DEBUG instead of INFO.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def timestamped_output_path(prefix: str) -> Path:
    """Default report path: ``<prefix>_YYYYMMDD_HHMMSS.csv`` in the cwd.

    The timestamp keeps repeated runs from clobbering each other —
    reports are audit artifacts worth keeping side by side.

    Args:
        prefix: Report name prefix, e.g. ``"wav_integrity_report"``.

    Returns:
        The timestamped path in the current working directory.
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path.cwd() / f"{prefix}_{stamp}.csv"
