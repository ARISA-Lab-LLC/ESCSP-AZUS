#!/usr/bin/env python3
"""Fill an oversized ESID's ``Part_2_of_2`` folder from its ``Part_1_of_2`` twin.

PURPOSE
=======
Zenodo caps a record at 50 GB, so a site whose raw data exceeds that has
to become two records.  The operator does the renaming by hand: the
oversized ``Raw_Data/ESID#NNN/`` becomes ``Raw_Data/ESID#NNN_Part_1_of_2/``
and an empty sibling ``Raw_Data/ESID#NNN_Part_2_of_2/`` is created beside
it.  This tool performs the mechanical fill of Part 2:

  1. COPY every non-WAV companion file from Part 1 into Part 2
     (in practice ``CONFIG.TXT``), verified by SHA-512.
  2. ORDER the WAVs by their filename timestamp and choose the single cut
     that best balances total bytes between the two halves.
  3. MOVE the later half of the WAVs into Part 2.

It never renames a folder, never creates the Part 2 folder, and never
touches the collectors spreadsheet.

THE PLAN IS DERIVED FROM THE UNION OF BOTH FOLDERS
==================================================
The cut is computed from the union of Part 1's and Part 2's WAVs, never
from Part 1 alone.  The union does not change as files move, which is
what makes this tool safe to interrupt:

  * a fresh run and a half-finished run compute the IDENTICAL boundary;
  * an interrupted run resumes by moving only what is still in Part 1;
  * a finished pair re-runs as a no-op (``ALREADY_SPLIT``);
  * no journal or resume state is needed — the filesystem is the record.

ORDERING, AND NAMES THAT CARRY NO TIMESTAMP
===========================================
The convention is ``YYYYMMDD_HHMMSS.WAV``.  Real collector folders also
contain names that do not follow it: 8-hex-character names from old
AudioMoth firmware (``5D8F3A2B.WAV``), and ``19700101_*`` from a device
whose clock reset to the Unix epoch.  The sort key is total and
deterministic:

    (0, "YYYYMMDDHHMMSS", casefolded name, name)   parseable
    (1, "",               casefolded name, name)   not parseable

so ``19700101_*`` sorts naturally to the front (its timestamp parses)
while unparseable names sort last, in name order.  Unparseable names are
counted in their own report column and logged: for a folder made only of
them the cut is a name boundary, not a time boundary, and the report must
not imply otherwise.

Filesystem mtimes are deliberately NOT consulted.  ``prepare_dataset.py``
REWRITES pre-1980 mtimes via ``os.utime``, so in this pipeline mtime is a
mutated field and is not evidence of recording time.

WHAT IS COPIED, WHAT IS MOVED, WHAT IS NEVER TOUCHED
====================================================
Copied: every top-level non-WAV regular file that is not excluded.
Moved: the Part-2-bound WAVs.  Never copied:

  * ``upload_state.json`` — it binds a folder to ONE Zenodo draft, so
    copying it would point two records at the same draft.  Same for every
    other pattern in ``prepare_dataset._UPLOAD_ARTIFACT_PATTERNS``.
  * anything starting with ``.`` — ``.DS_Store``, ``._*`` AppleDouble
    sidecars, ``.prep_complete``, stale ``.partial`` files.
  * ``*.zip``, every subdirectory (interrupted preps leave
    ``ESID_*_Staging/`` build folders in ``Raw_Data/``), every symlink.

Nothing is ever recursed into.  Every skip is logged.

SAFETY MODEL (execution is opt-in and re-gated)
===============================================
Dry-run by default: without ``--perform-split`` the tool only reports.
Classification and mutation are separate, and the mutating path
re-scans both folders, re-plans from the fresh union, and re-runs every
gate from scratch — a plan that no longer matches is REFUSED, never
applied.  Refusals are logged as ``REFUSING to ...`` and return False;
they are never exceptions.  Unknowable state fails closed.  All folder
identity is by inode, never by path string: on the case-insensitive
filesystems this project runs on, ``_part_1_of_2`` and ``_Part_1_of_2``
are one directory and two strings, and a "move" between them would be a
no-op the report would call a success.

THE MOVE
========
Part 1 and Part 2 are siblings, so ``st_dev`` matches and a single
``os.rename`` moves each WAV atomically without copying a byte.  There is
nothing for a hash to detect there — ``rename(2)`` within a filesystem has
no partial state — and reading back tens of GB per pair would add hours
for no information, so the same-device path verifies by size and source
disappearance only.  That post-check exists for the one failure it CAN
catch: a filesystem that reports equal ``st_dev`` while implementing the
rename as a copy (firmlinks, overlay/FUSE mounts).  If it ever fires, the
whole pair aborts rather than continuing file by file.

A genuine cross-device move copies into ``.<name>.partial``, verifies
SHA-512 both sides, renames the partial into place, and only then unlinks
the source.  On any mismatch the partial is removed and the source is
left alone — the bias is always toward leaving a duplicate over risking a
gap.

COLLECTORS CSV — CHECKED, NEVER WRITTEN
=======================================
``prepare_dataset.extract_collector_data`` matches the ESID with a raw
string compare and exits 1 on a miss, with no fallback to the base
3-digit ESID.  A split site therefore needs its own
``NNN_Part_1_of_2`` / ``NNN_Part_2_of_2`` rows in the collectors
spreadsheet.  This tool REPORTS whether they exist (and whether a stale
bare ``NNN`` row remains) so prep cannot fail by surprise later.  It never
edits the spreadsheet.

AFTER A SPLIT
=============
Any ``file_list.csv`` and SHA-512 computed for the whole pre-split site is
now wrong, and any staged ZIP is stale.  Both halves need a fresh prep,
and the pre-split ``Staging_Area/ESID_NNN_Staging/`` should be discarded
— if it holds an ``upload_state.json`` there is also a live Zenodo record
for the un-split site that this tool does nothing about.  The
``Pre-Split Record`` column surfaces that.

OPERATIONAL WARNING
===================
This project lives on a Dropbox volume.  A running sync client races
every rename, can change a file's size between the dry run and the
execute run, and can materialise online-only placeholders mid-loop.
PAUSE SYNCING for the duration.  A size that changes between the two runs
is refused as ``PLAN_CHANGED`` rather than applied stale.

USAGE
=====
::

    # 1. Review the plan (read-only; changes nothing)
    python Resources/split_oversized_raw_folders.py /path/to/Raw_Data

    # 2. One site only
    python Resources/split_oversized_raw_folders.py /path/to/Raw_Data --esid 445

    # 3. Perform it
    python Resources/split_oversized_raw_folders.py /path/to/Raw_Data \\
        --esid 445 --perform-split

    # 4. Finish a run that was interrupted
    python Resources/split_oversized_raw_folders.py /path/to/Raw_Data \\
        --esid 445 --perform-split --resume

OUTPUT
======
One CSV row per pair, written incrementally (flushed per row) so a crash
cannot lose the record of pairs already split.  ``Cut Boundary`` is the
field to review: it names the last Part 1 file and the first Part 2 file,
so the chronological split can be checked without opening a folder.

EXIT CODES
==========
* ``0`` — every pair in scope is split (or already was) and nothing was
  flagged
* ``1`` — something needs attention: any refusal, any pending plan in a
  dry run, an incomplete move, a missing collectors row, a zero-byte or
  unparseable-name WAV, or an existing pre-split record
* ``2`` — usage error (folder missing, folder IS Staging_Area or
  Uploaded_Data, bad flag value, or ``--esid`` matched no pair)
"""

import argparse
import csv
import fnmatch
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import azus_common
from audit_wav_integrity import (
    _is_wav_name,
    compare_file_maps,
    human_size,
    scan_disk_wavs,
)
from prepare_dataset import _UPLOAD_ARTIFACT_PATTERNS

logger = logging.getLogger("azus.split_parts")

_STAGING_AREA = azus_common.STAGING_AREA
_UPLOADED_DATA = azus_common.UPLOADED_DATA

# Zenodo's per-record ceiling, read as DECIMAL GB.  The binary reading
# (50 * 1024**3 = 53,687,091,200) admits 3.7 GB more; since guessing wrong
# costs a rejected upload after hours of transfer, the default fails
# toward the smaller number and --limit-bytes can set either.
_DEFAULT_LIMIT_BYTES = 50_000_000_000

# Passed through to scan_disk_wavs.  This tool does not act on the "tiny"
# classification (only on zero-byte and on sizes it cannot trust), so the
# threshold is a fixed constant rather than a flag.
_TINY_THRESHOLD = 1024

# The trailing "_Part_1_of_2" of a Part 1 folder name.  Matched
# case-insensitively; only the captured digit is substituted, so the
# ESID#/ESID_ spelling and every letter case survive byte-for-byte.
_PART1_TAIL_RE = re.compile(r"(_Part_)(1)(_of_)(\d+)$", re.IGNORECASE)

# A canonical suffixed ESID that names one part of a split site.
_PART_ESID_RE = re.compile(r"^(\d{3})_Part_(\d+)_of_(\d+)$", re.IGNORECASE)

_CSV_COLUMNS = [
    "ESID#",
    "Part 1 Folder",
    "Part 2 Folder",
    "WAV Count",
    "WAV Size",
    "Unparseable-Name WAVs",
    "Zero-Byte WAVs",
    "Cut Boundary",
    "Part 1 WAVs",
    "Part 1 Size",
    "Part 2 WAVs",
    "Part 2 Size",
    "Imbalance",
    "Over Limit",
    "Non-WAV Copied",
    "Non-WAV Skipped",
    "Collectors CSV",
    "Pre-Split Record",
    "WAVs Already In Part 2",
    "WAVs Moved",
    "Verdict",
    "Action Taken",
    "Notes",
]

# Verdict constants — see the module docstring for the safety model.
SPLIT_PLANNED = "SPLIT_PLANNED"
SPLIT_DONE = "SPLIT_DONE"
ALREADY_SPLIT = "ALREADY_SPLIT"
RESUMABLE = "RESUMABLE"
NOT_A_2_PART_ESID = "NOT_A_2_PART_ESID"
PART2_MISSING = "PART2_MISSING"
PAIR_ALIASED = "PAIR_ALIASED"
NOT_DIRECT_CHILD = "NOT_DIRECT_CHILD"
PART2_BOUND_TO_DRAFT = "PART2_BOUND_TO_DRAFT"
UNTRUSTWORTHY_SIZES = "UNTRUSTWORTHY_SIZES"
TOO_FEW_WAVS = "TOO_FEW_WAVS"
PART2_HAS_WAVS = "PART2_HAS_WAVS"
PART2_UNEXPECTED_WAVS = "PART2_UNEXPECTED_WAVS"
NAME_COLLISION = "NAME_COLLISION"
STILL_OVERSIZED = "STILL_OVERSIZED"
PLAN_CHANGED = "PLAN_CHANGED"
PLAN_SELF_CHECK_FAILED = "PLAN_SELF_CHECK_FAILED"
INCOMPLETE = "INCOMPLETE"

_RECOMMENDED_ACTION = {
    SPLIT_PLANNED: "re-run with --perform-split to apply",
    RESUMABLE: "re-run with --perform-split --resume to finish",
    SPLIT_DONE: (
        "re-prep BOTH parts (prepare_dataset.py) and discard the "
        "pre-split ESID_NNN_Staging — its ZIP and file_list.csv are stale"
    ),
    ALREADY_SPLIT: "nothing to do",
    NOT_A_2_PART_ESID: "only _of_2 pairs are handled; split this site by hand",
    PART2_MISSING: "create the ESID#NNN_Part_2_of_2 folder, then re-run",
    PAIR_ALIASED: (
        "the two folder names resolve to ONE directory (case-insensitive "
        "filesystem) — fix the names before re-running"
    ),
    NOT_DIRECT_CHILD: "both folders must be direct children of RAW_DATA_DIR",
    PART2_BOUND_TO_DRAFT: (
        "Part 2 already has an upload_state.json — it is bound to a Zenodo "
        "draft; resolve that record before changing its contents"
    ),
    UNTRUSTWORTHY_SIZES: (
        "sizes cannot be trusted (cloud placeholders or unreadable files) "
        "— run audit_wav_integrity.py and fully sync the folder first"
    ),
    TOO_FEW_WAVS: (
        "fewer than 2 WAVs in the pair — no 2-way split exists; a single "
        "over-limit file needs a different remedy"
    ),
    PART2_HAS_WAVS: "re-run with --resume to finish an interrupted split",
    PART2_UNEXPECTED_WAVS: (
        "Part 2 holds a WAV this plan assigns to Part 1 — something other "
        "than this tool moved files; reconcile by hand"
    ),
    NAME_COLLISION: (
        "a companion file differs between the two folders — reconcile by hand"
    ),
    STILL_OVERSIZED: (
        "a half still exceeds the limit — this site needs more than 2 parts "
        "(or re-run with --allow-still-oversized)"
    ),
    PLAN_CHANGED: (
        "the folders changed between planning and execution (pause Dropbox "
        "syncing) — re-run the dry run and review the new plan"
    ),
    PLAN_SELF_CHECK_FAILED: "internal self-check failed — report this",
    INCOMPLETE: "some files did not move — re-run with --resume after fixing",
}


# =====================================================================
#  Pure helpers (no I/O — unit-tested directly)
# =====================================================================

def parse_wav_timestamp(name: str) -> Optional[str]:
    """Sortable ``YYYYMMDDHHMMSS`` token from a WAV filename, or None.

    Parses the project's AudioMoth convention ``YYYYMMDD_HHMMSS.WAV``.
    Both halves must be real calendar/clock values, so ``20241301_000000``
    (month 13) does not parse.  Extra trailing underscore-separated parts
    are tolerated (``20240408_120000_1.WAV``); the full name breaks ties
    in :func:`wav_sort_key`.

    Related: ``esid_wav_inventory.filename_year`` extracts only the year
    from the same convention, for a different report.

    Args:
        name: A WAV (base)name.

    Returns:
        The concatenated ``"YYYYMMDDHHMMSS"`` token, or None when the name
        does not carry a parseable date and time.
    """
    parts = Path(name).stem.split("_")
    if len(parts) < 2:
        return None
    date_token, time_token = parts[0], parts[1]
    if len(date_token) != 8 or len(time_token) != 6:
        return None
    try:
        datetime.strptime(date_token + time_token, "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return date_token + time_token


def wav_sort_key(name: str) -> Tuple[int, str, str, str]:
    """Total, deterministic ordering key for one WAV name.

    Timestamped names come first in chronological order; names that carry
    no parseable timestamp come last in name order.  Including both the
    casefolded and the raw name makes the order total even for names that
    differ only in case, so a plan never depends on ``iterdir`` order or
    on filesystem case behaviour.

    Args:
        name: A WAV (base)name.

    Returns:
        A 4-tuple suitable for ``sorted(key=...)``.
    """
    token = parse_wav_timestamp(name)
    return (0, token, name.casefold(), name) if token else (1, "", name.casefold(), name)


def parse_part_esid(esid: str) -> Optional[Tuple[str, int, int]]:
    """Split a canonical part-ESID into its base and part numbers.

    Args:
        esid: A canonical ESID, e.g. ``"445_Part_1_of_2"``.

    Returns:
        ``(base_esid, part_index, part_total)`` — e.g. ``("445", 1, 2)`` —
        or None when the ESID does not name a part of a split site
        (``"445"``, ``"120A"``).
    """
    m = _PART_ESID_RE.match(esid)
    if m is None:
        return None
    return m.group(1), int(m.group(2)), int(m.group(3))


def part2_folder_name(part1_name: str) -> Optional[str]:
    """Derive the Part 2 folder name from a Part 1 folder name.

    Only the ``1`` inside the trailing ``_Part_1_of_N`` is replaced, by
    offset, so the ``ESID#``/``ESID_`` spelling and every letter case are
    preserved exactly rather than reconstructed.

    Args:
        part1_name: A Part 1 folder basename, e.g. ``"ESID#445_Part_1_of_2"``.

    Returns:
        The sibling Part 2 basename, or None when the name has no
        ``_Part_1_of_N`` tail.
    """
    m = _PART1_TAIL_RE.search(part1_name)
    if m is None:
        return None
    return part1_name[: m.start(2)] + "2" + part1_name[m.end(2):]


def should_copy_non_wav(
    name: str, is_dir: bool, is_symlink: bool
) -> Tuple[bool, str]:
    """Decide whether one Part 1 entry is a companion file to copy.

    Args:
        name: The entry's basename.
        is_dir: True when the entry is a directory.
        is_symlink: True when the entry is a symbolic link.

    Returns:
        ``(True, "")`` to copy it, or ``(False, reason)`` naming why it is
        skipped (the reason appears in the report and the log).
    """
    if is_symlink:
        return False, "symlink"
    if is_dir:
        return False, "directory"
    if name.startswith("."):
        return False, "hidden"
    if name.lower().endswith(".wav"):
        return False, "WAV"
    if name.lower().endswith(".zip"):
        return False, "ZIP"
    for pattern in _UPLOAD_ARTIFACT_PATTERNS:
        if fnmatch.fnmatch(name, pattern):
            return False, "upload artifact (binds a folder to a Zenodo draft)"
    return True, ""


def choose_cut_index(ordered_sizes: Sequence[int]) -> Optional[int]:
    """Index of the first Part-2-bound file in a size-ordered sequence.

    Considers every cut that leaves both halves non-empty and returns the
    one minimising ``abs(left_bytes - right_bytes)``.  Ties break to the
    LARGEST index, which moves the fewest files and — for the common case
    of uniformly-sized recordings — leaves the odd file in Part 1.

    Args:
        ordered_sizes: Byte sizes in the tool's canonical WAV order.

    Returns:
        The cut index in ``[1, len - 1]``, or None when fewer than two
        files make any 2-way split impossible.
    """
    n = len(ordered_sizes)
    if n < 2:
        return None
    total = sum(ordered_sizes)
    prefix = 0
    best_index, best_imbalance = 1, None
    for index in range(1, n):
        prefix += ordered_sizes[index - 1]
        imbalance = abs(prefix - (total - prefix))
        # <= keeps the LAST index among equally-balanced cuts.
        if best_imbalance is None or imbalance <= best_imbalance:
            best_index, best_imbalance = index, imbalance
    return best_index


@dataclass(frozen=True)
class SplitPlan:
    """Which WAVs stay in Part 1 and which move to Part 2.

    Attributes:
        ordered_names: Every WAV in the union of both folders, in the
            tool's canonical order (see :func:`wav_sort_key`).
        sizes: ``{name: bytes}`` for every name in ``ordered_names``.
        cut_index: Index of the first Part-2-bound name, or None when the
            pair holds too few WAVs to split.
    """

    ordered_names: Tuple[str, ...]
    sizes: Mapping[str, int]
    cut_index: Optional[int]

    @property
    def part1_names(self) -> Tuple[str, ...]:
        """Names that belong in Part 1.

        Returns:
            The prefix of ``ordered_names`` before the cut (empty when
            there is no cut).
        """
        return () if self.cut_index is None else self.ordered_names[:self.cut_index]

    @property
    def part2_names(self) -> Tuple[str, ...]:
        """Names that belong in Part 2.

        Returns:
            The suffix of ``ordered_names`` from the cut onward (empty
            when there is no cut).
        """
        return () if self.cut_index is None else self.ordered_names[self.cut_index:]

    @property
    def part1_bytes(self) -> int:
        """Total bytes assigned to Part 1.

        Returns:
            Sum of ``sizes`` over :attr:`part1_names`.
        """
        return sum(self.sizes[n] for n in self.part1_names)

    @property
    def part2_bytes(self) -> int:
        """Total bytes assigned to Part 2.

        Returns:
            Sum of ``sizes`` over :attr:`part2_names`.
        """
        return sum(self.sizes[n] for n in self.part2_names)

    @property
    def total_bytes(self) -> int:
        """Total bytes across both halves.

        Returns:
            Sum of every size in the plan.
        """
        return sum(self.sizes[n] for n in self.ordered_names)

    @property
    def imbalance_bytes(self) -> int:
        """How far the two halves are from equal.

        Returns:
            ``abs(part1_bytes - part2_bytes)``.
        """
        return abs(self.part1_bytes - self.part2_bytes)

    @property
    def unparseable_names(self) -> Tuple[str, ...]:
        """Names carrying no parseable timestamp.

        Returns:
            Every name in the plan whose timestamp does not parse, in
            plan order.
        """
        return tuple(
            n for n in self.ordered_names if parse_wav_timestamp(n) is None
        )

    @property
    def cut_boundary(self) -> str:
        """Human-readable description of where the split falls.

        Returns:
            ``"<last Part 1 name>  ->  <first Part 2 name>"``, or ``""``
            when there is no cut.
        """
        if self.cut_index is None:
            return ""
        return f"{self.part1_names[-1]}  ->  {self.part2_names[0]}"

    def verify(self) -> List[str]:
        """Re-derive the plan independently and confirm it holds.

        The halves were produced by slicing at one chosen index; here the
        ordering, the partition, and the optimality of that index are all
        rebuilt from scratch — including a brute-force sweep of every
        candidate cut — so a wrong split can never be reported as if it
        had been checked.

        Returns:
            A list of human-readable failure messages; an empty list
            means every self-check passed.
        """
        errs: List[str] = []

        if set(self.ordered_names) != set(self.sizes):
            errs.append("ordered_names and sizes cover different name sets")
        if len(set(self.ordered_names)) != len(self.ordered_names):
            errs.append("ordered_names contains a duplicate")

        expected_order = tuple(sorted(self.ordered_names, key=wav_sort_key))
        if self.ordered_names != expected_order:
            errs.append("ordered_names is not in canonical sort order")

        if self.cut_index is None:
            if len(self.ordered_names) >= 2:
                errs.append("no cut chosen despite 2+ WAVs being present")
            return errs

        if not 1 <= self.cut_index < len(self.ordered_names):
            errs.append(f"cut_index {self.cut_index} leaves a half empty")
            return errs

        if set(self.part1_names) & set(self.part2_names):
            errs.append("the two halves share a name")
        if tuple(self.part1_names) + tuple(self.part2_names) != self.ordered_names:
            errs.append("the halves do not reconstruct ordered_names")
        if self.part1_bytes + self.part2_bytes != self.total_bytes:
            errs.append("half byte totals do not sum to the plan total")

        ordered_sizes = [self.sizes[n] for n in self.ordered_names]
        best = min(
            abs(sum(ordered_sizes[:k]) - sum(ordered_sizes[k:]))
            for k in range(1, len(ordered_sizes))
        )
        if self.imbalance_bytes != best:
            errs.append(
                f"imbalance {self.imbalance_bytes} is not the minimum "
                f"achievable ({best})"
            )
        return errs


def plan_split(union_sizes: Mapping[str, int]) -> SplitPlan:
    """Build the split plan for one pair from its union of WAVs.

    Args:
        union_sizes: ``{name: bytes}`` merged across Part 1 and Part 2.
            Because the union is invariant as files move, the same input
            is produced by a fresh pair and by a half-finished one — which
            is what makes an interrupted run resume to the same boundary.

    Returns:
        The :class:`SplitPlan`; its ``cut_index`` is None when the union
        holds fewer than two WAVs.
    """
    ordered = tuple(sorted(union_sizes, key=wav_sort_key))
    cut = choose_cut_index([union_sizes[n] for n in ordered])
    return SplitPlan(ordered_names=ordered, sizes=dict(union_sizes), cut_index=cut)


# =====================================================================
#  Identity guards (inode-based — never path strings)
# =====================================================================

def same_inode(a: Path, b: Path) -> bool:
    """Tell whether two paths are the same physical file or directory.

    Compares inodes via :func:`os.path.samefile`, which is immune to the
    case-variant aliases a case-insensitive filesystem produces.  String
    comparison of resolved paths is not.

    Args:
        a: First path.
        b: Second path.

    Returns:
        True when both exist and are the same inode; True (fail closed)
        when both exist but identity cannot be determined; False when one
        does not exist or they are provably different.
    """
    try:
        return os.path.samefile(a, b)
    except OSError:
        if not a.exists() or not b.exists():
            return False
        return True  # both exist but identity unknowable — fail closed


def path_is_or_inside(outer: Path, inner: Path) -> bool:
    """Tell whether ``inner`` IS ``outer`` or lives inside it.

    Walks ``inner``'s resolved path and every ancestor comparing inodes,
    which catches both the case-alias self-pair and any nested
    arrangement in one check.

    Args:
        outer: The containing candidate.
        inner: The path that must be independent of ``outer``.

    Returns:
        True when the two are the same directory or ``inner`` is nested
        within ``outer``; True (fail closed) when an ancestor cannot be
        stat'd.
    """
    try:
        outer_stat = outer.stat()
        resolved = inner.resolve()
        for ancestor in (resolved, *resolved.parents):
            st = ancestor.stat()
            if (st.st_dev, st.st_ino) == (outer_stat.st_dev, outer_stat.st_ino):
                return True
    except OSError:
        return True  # cannot prove independence — fail closed
    return False


def same_device(a: Path, b: Path) -> bool:
    """Tell whether two paths sit on the same filesystem.

    Args:
        a: First path.
        b: Second path.

    Returns:
        True when both report the same ``st_dev``; False when they differ
        or either cannot be stat'd (fail closed onto the verified
        cross-device copy path).
    """
    try:
        return a.stat().st_dev == b.stat().st_dev
    except OSError:
        return False


# =====================================================================
#  Read-only inspection
# =====================================================================

@dataclass(frozen=True)
class PartPair:
    """One ``Part_1_of_2`` folder and its ``Part_2_of_2`` sibling.

    Attributes:
        esid: Canonical ESID of Part 1, e.g. ``"445_Part_1_of_2"``.
        base_esid: The bare 3-digit site number, e.g. ``"445"``.
        part_total: The ``N`` from ``_of_N`` (only 2 is supported).
        part1: Path to the Part 1 folder.
        part2: Path to the expected Part 2 sibling (may not exist).
    """

    esid: str
    base_esid: str
    part_total: int
    part1: Path
    part2: Path


def find_part_pairs(raw_root: Path) -> List[PartPair]:
    """Discover every ``Part_1_of_N`` folder under ``raw_root``, in ESID order.

    Only Part 1 folders are returned; a Part 2 folder is reached through
    its sibling, and any other directory (plain ESID folders, leftover
    staging builds, ``.DS_Store``) is ignored.

    Args:
        raw_root: The Raw Data folder to scan (not recursive).

    Returns:
        Matching pairs sorted by :func:`azus_common.esid_sort_key`.
    """
    found: List[Tuple[Tuple[int, str], PartPair]] = []
    for entry in sorted(raw_root.iterdir()):
        if not entry.is_dir():
            continue
        canonical = azus_common.parse_esid(entry.name)
        if canonical is None:
            continue
        parsed = parse_part_esid(canonical)
        if parsed is None:
            continue
        base, index, total = parsed
        if index != 1:
            continue  # Part 2+ folders are destinations, not candidates
        sibling_name = part2_folder_name(entry.name)
        if sibling_name is None:
            continue
        found.append((
            azus_common.esid_sort_key(canonical),
            PartPair(
                esid=canonical, base_esid=base, part_total=total,
                part1=entry, part2=raw_root / sibling_name,
            ),
        ))
    found.sort(key=lambda t: t[0])
    return [pair for _, pair in found]


def wav_sizes(folder: Path) -> Tuple[Dict[str, int], List[str], List[str]]:
    """Measure every WAV in one folder, separating trust from content issues.

    Reuses :func:`audit_wav_integrity.scan_disk_wavs`, then splits its
    discrepancies into two very different classes.  A size the tool cannot
    TRUST makes the whole split arbitrary and must block; a file whose
    CONTENT is questionable but whose size is real does not — a truncated
    recording is usually just truncated, and refusing to split until it is
    fixed would make the tool unusable for the data that actually exists.

    The blocking class deliberately includes a WAV that failed ``stat``:
    ``scan_disk_wavs`` records those only as a discrepancy and omits them
    from its ``sizes`` map entirely, so such a file would be invisible to
    the plan and silently stranded in Part 1.

    Args:
        folder: The folder whose top-level WAVs are measured.

    Returns:
        ``(sizes, untrusted, content_warnings)`` — ``sizes`` is
        ``{name: bytes}``; ``untrusted`` is non-empty when the measurement
        itself cannot be relied on; ``content_warnings`` describes real
        files with questionable contents.
    """
    stats = scan_disk_wavs(folder, _TINY_THRESHOLD)
    untrusted: List[str] = list(stats.verify())
    warnings: List[str] = []

    on_disk = {
        entry.name for entry in folder.iterdir()
        if entry.is_file() and _is_wav_name(entry.name)
    }
    for name in sorted(on_disk - set(stats.sizes)):
        untrusted.append(f"{name}: size could not be read at all")

    for name, reason in stats.discrepancies:
        if "stat reports 0 bytes" in reason or "stat failed" in reason:
            untrusted.append(f"{name}: {reason}")
        else:
            warnings.append(f"{name}: {reason}")

    for name in stats.zero_names:
        warnings.append(f"{name}: zero bytes")
    return dict(stats.sizes), untrusted, warnings


def collectors_row_status(
    esid_column: Optional[List[str]], base_esid: str
) -> str:
    """Describe the collectors-spreadsheet rows for one split site.

    Args:
        esid_column: Every value of the spreadsheet's ``ESID`` column, or
            None when the spreadsheet could not be read.
        base_esid: The bare 3-digit site number.

    Returns:
        A short human-readable status for the report column.
    """
    if esid_column is None:
        return "spreadsheet unreadable"
    present = {value.strip().casefold() for value in esid_column}
    missing = [
        f"{base_esid}_Part_{n}_of_2" for n in (1, 2)
        if f"{base_esid}_Part_{n}_of_2".casefold() not in present
    ]
    if missing:
        return "MISSING: " + ", ".join(missing)
    if base_esid.casefold() in present:
        return f"both rows present (stale bare {base_esid} row also present)"
    return "both rows present"


def read_collectors_esids(path: Path) -> Optional[List[str]]:
    """Read the ``ESID`` column of the collectors spreadsheet.

    Read-only: this tool never writes to the spreadsheet.

    Args:
        path: Path to the collectors CSV.

    Returns:
        Every ``ESID`` cell value, or None when the file cannot be read
        or has no ``ESID`` column.
    """
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None or "ESID" not in reader.fieldnames:
                logger.warning("%s has no ESID column — cannot check rows.", path)
                return None
            return [(row.get("ESID") or "") for row in reader]
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Could not read collectors spreadsheet %s: %s", path, exc)
        return None


def pre_split_record(base_esid: str) -> str:
    """Locate a staging/uploaded folder left over from the un-split site.

    Its ZIP and ``file_list.csv`` describe the whole pre-split dataset and
    are now stale; if it holds an ``upload_state.json`` there is also a
    live Zenodo record for the un-split site that splitting does not
    address.

    Args:
        base_esid: The bare 3-digit site number.

    Returns:
        A short description of what was found, or ``""`` when nothing
        pre-split remains.
    """
    found: List[str] = []
    for root, suffix in ((_STAGING_AREA, "Staging"), (_UPLOADED_DATA, "Uploaded")):
        folder = root / f"ESID_{base_esid}_{suffix}"
        if folder.is_dir():
            bound = " (bound to a Zenodo draft)" if (
                folder / azus_common.STATE_FILENAME
            ).is_file() else ""
            found.append(f"{root.name}/{folder.name}{bound}")
    return "; ".join(found)


def _blank_row(pair: PartPair) -> Dict[str, object]:
    """Start a report row with the pair's identity and empty measurements.

    Args:
        pair: The pair being reported.

    Returns:
        A row dict with every column present.
    """
    row: Dict[str, object] = {column: "" for column in _CSV_COLUMNS}
    row.update({
        "ESID#": pair.base_esid,
        "Part 1 Folder": pair.part1.name,
        "Part 2 Folder": pair.part2.name,
        "WAVs Moved": 0,
        "Non-WAV Copied": 0,
    })
    return row


def assess_pair(
    pair: PartPair,
    args: argparse.Namespace,
    raw_root: Path,
    esid_column: Optional[List[str]],
) -> Tuple[Dict[str, object], Optional[SplitPlan]]:
    """Classify one pair and plan its split, mutating nothing.

    Every gate in the module's safety model is applied here, in order, so
    a ``SPLIT_PLANNED`` verdict is only ever advertised for a pair the
    execution path would actually accept.  :func:`execute_split` calls
    this again immediately before mutating, which is how the gates get
    re-derived from scratch rather than trusted.

    Args:
        pair: The pair to classify.
        args: Parsed CLI arguments.
        raw_root: The scanned Raw Data folder (for the containment check).
        esid_column: Collectors-spreadsheet ESID values, or None.

    Returns:
        ``(row, plan)`` — a fully populated report row whose ``Verdict``
        is set, and the plan, which is None for every refusal.
    """
    row = _blank_row(pair)
    row["Collectors CSV"] = (
        "not checked" if args.skip_collectors_check
        else collectors_row_status(esid_column, pair.base_esid)
    )
    row["Pre-Split Record"] = pre_split_record(pair.base_esid)

    def refuse(verdict: str, note: str = "") -> Tuple[Dict[str, object], None]:
        """Record a refusal verdict on the row and return no plan."""
        row["Verdict"] = verdict
        if note:
            row["Notes"] = note
        return row, None

    if pair.part_total != 2:
        return refuse(
            NOT_A_2_PART_ESID, f"folder names _of_{pair.part_total}, not _of_2"
        )
    if not pair.part2.is_dir():
        return refuse(PART2_MISSING, f"{pair.part2.name} does not exist")
    if path_is_or_inside(pair.part1, pair.part2) or path_is_or_inside(
        pair.part2, pair.part1
    ):
        return refuse(PAIR_ALIASED, "the two folder names are one directory")
    for folder in (pair.part1, pair.part2):
        if not same_inode(folder.parent, raw_root):
            return refuse(
                NOT_DIRECT_CHILD, f"{folder.name} is not a direct child of the scanned folder"
            )
    for pattern in _UPLOAD_ARTIFACT_PATTERNS:
        if any(pair.part2.glob(pattern)):
            return refuse(
                PART2_BOUND_TO_DRAFT, f"Part 2 contains {pattern}"
            )

    try:
        part1_sizes, untrusted1, warn1 = wav_sizes(pair.part1)
        part2_sizes, untrusted2, warn2 = wav_sizes(pair.part2)
    except OSError as exc:
        return refuse(UNTRUSTWORTHY_SIZES, f"could not scan a folder: {exc}")
    untrusted = untrusted1 + untrusted2
    if untrusted:
        return refuse(UNTRUSTWORTHY_SIZES, "; ".join(untrusted[:5]))

    # Part 1 is authoritative on a size the two folders disagree about: it
    # is the source of truth until its copy is unlinked.  A disagreement
    # is caught below, before the plan matters.
    union = dict(part2_sizes)
    union.update(part1_sizes)
    both = sorted(set(part1_sizes) & set(part2_sizes))
    differing = [n for n in both if part1_sizes[n] != part2_sizes[n]]

    plan = plan_split(union)
    self_check = plan.verify()
    if self_check:
        return refuse(PLAN_SELF_CHECK_FAILED, "; ".join(self_check[:5]))

    warnings = warn1 + warn2
    non_wav_bytes = 0
    copy_names: List[str] = []
    skipped: List[str] = []
    for entry in sorted(pair.part1.iterdir()):
        copy_it, reason = should_copy_non_wav(
            entry.name, entry.is_dir(), entry.is_symlink()
        )
        if copy_it:
            copy_names.append(entry.name)
            try:
                non_wav_bytes += entry.stat().st_size
            except OSError:
                pass
        elif reason not in ("WAV",):
            skipped.append(f"{entry.name} ({reason})")

    if not any(n.upper() == "CONFIG.TXT" for n in copy_names):
        warnings.append("no CONFIG.TXT in Part 1")

    part1_total = plan.part1_bytes + non_wav_bytes
    part2_total = plan.part2_bytes + non_wav_bytes
    over = [
        label for label, size in (("Part 1", part1_total), ("Part 2", part2_total))
        if size > args.limit_bytes
    ]

    row.update({
        "WAV Count": len(plan.ordered_names),
        "WAV Size": human_size(plan.total_bytes),
        "Unparseable-Name WAVs": len(plan.unparseable_names),
        "Zero-Byte WAVs": sum(1 for w in warnings if w.endswith(": zero bytes")),
        "Cut Boundary": plan.cut_boundary,
        "Part 1 WAVs": len(plan.part1_names),
        "Part 1 Size": human_size(part1_total),
        "Part 2 WAVs": len(plan.part2_names),
        "Part 2 Size": human_size(part2_total),
        "Imbalance": human_size(plan.imbalance_bytes),
        "Over Limit": " and ".join(over),
        "Non-WAV Copied": len(copy_names),
        "Non-WAV Skipped": ", ".join(skipped),
        "WAVs Already In Part 2": len(part2_sizes),
        "Notes": "; ".join(warnings[:5]),
    })

    if plan.cut_index is None:
        return refuse(TOO_FEW_WAVS, f"{len(plan.ordered_names)} WAV(s) in the pair")
    if differing:
        return refuse(
            NAME_COLLISION,
            f"{len(differing)} name(s) in both folders at different sizes "
            f"(e.g. {differing[0]}) — an interrupted copy",
        )
    unexpected = sorted(set(part2_sizes) & set(plan.part1_names))
    if unexpected:
        return refuse(
            PART2_UNEXPECTED_WAVS,
            f"{len(unexpected)} Part-1-bound WAV(s) already in Part 2 "
            f"(e.g. {unexpected[0]})",
        )
    if part2_sizes and not args.resume:
        return refuse(
            PART2_HAS_WAVS, f"Part 2 already holds {len(part2_sizes)} WAV(s)"
        )
    if over and not args.allow_still_oversized:
        return refuse(
            STILL_OVERSIZED,
            f"{' and '.join(over)} still over {human_size(args.limit_bytes)}",
        )

    remaining = [n for n in plan.part2_names if n not in part2_sizes]
    missing_companions = [
        n for n in copy_names if not (pair.part2 / n).exists()
    ]
    if not remaining and not missing_companions:
        row["Verdict"] = ALREADY_SPLIT
    elif part2_sizes:
        row["Verdict"] = RESUMABLE
    else:
        row["Verdict"] = SPLIT_PLANNED
    return row, plan


# =====================================================================
#  Mutation
# =====================================================================

def copy_non_wav_files(part1: Path, part2: Path) -> Tuple[int, List[str]]:
    """Copy Part 1's companion files into Part 2 and verify each one.

    Runs BEFORE any WAV moves: it costs seconds and is what surfaces a
    read-only Part 2, a full disk, or a permissions problem while the
    operation is still trivially reversible.  A companion already present
    and byte-identical is left alone; one present and different is a
    failure, never an overwrite.

    Args:
        part1: The source folder.
        part2: The destination folder.

    Returns:
        ``(copied_count, failures)``; ``failures`` is empty on success.
    """
    copied = 0
    failures: List[str] = []
    for entry in sorted(part1.iterdir()):
        copy_it, reason = should_copy_non_wav(
            entry.name, entry.is_dir(), entry.is_symlink()
        )
        if not copy_it:
            if reason != "WAV":
                logger.debug("  skipping %s (%s)", entry.name, reason)
            continue
        destination = part2 / entry.name
        try:
            source_digest = azus_common.calculate_sha512(str(entry))
            if destination.exists():
                if azus_common.calculate_sha512(str(destination)) == source_digest:
                    logger.info(
                        "  %s already present and identical — not recopied.",
                        entry.name,
                    )
                    continue
                failures.append(
                    f"{entry.name} exists in Part 2 with different content"
                )
                continue
            shutil.copy2(entry, destination)
            if azus_common.calculate_sha512(str(destination)) != source_digest:
                destination.unlink(missing_ok=True)
                failures.append(f"{entry.name} failed SHA-512 verification")
                continue
        except OSError as exc:
            failures.append(f"{entry.name}: {exc}")
            continue
        copied += 1
        logger.info("  Copied %s (SHA-512 verified).", entry.name)
    return copied, failures


def _copy_verify_replace(src: Path, dst: Path, expected_size: int) -> Tuple[bool, str]:
    """Move one file across filesystems: copy, verify, rename, unlink.

    The copy lands in a hidden ``.<name>.partial`` so an interruption can
    never leave a short file under the real name; ``.partial`` files do not
    match the project's WAV predicate, so no tool can mistake one for a
    recording.  The source is unlinked only after SHA-512 agreement, so a
    failure leaves a duplicate rather than a gap.

    (No explicit ``fsync``: durability against power loss is covered by
    the resume path — a duplicated name is detected and healed — while the
    read-back hash is what proves the copy itself is faithful.)

    Args:
        src: The file to move.
        dst: Its destination path.
        expected_size: The size the plan recorded for this file.

    Returns:
        ``(ok, note)``; on failure the note explains what was refused.
    """
    partial = dst.with_name(f".{dst.name}.partial")
    try:
        shutil.copyfile(src, partial)
        if partial.stat().st_size != expected_size:
            partial.unlink(missing_ok=True)
            return False, f"{src.name}: copy is {partial.stat().st_size}, expected {expected_size}"
        if src.stat().st_size != expected_size:
            partial.unlink(missing_ok=True)
            return False, f"{src.name}: source changed size during the copy"
        if azus_common.calculate_sha512(str(src)) != azus_common.calculate_sha512(
            str(partial)
        ):
            partial.unlink(missing_ok=True)
            return False, f"{src.name}: SHA-512 mismatch after copy"
        os.replace(partial, dst)
        src.unlink()
    except OSError as exc:
        partial.unlink(missing_ok=True)
        return False, f"{src.name}: {exc}"
    return True, ""


def move_one_wav(
    src: Path, part2: Path, expected_size: int, cross_device: bool
) -> Tuple[bool, str]:
    """Move one WAV into Part 2, verifying before and after.

    Args:
        src: The Part 1 file to move.
        part2: The destination folder.
        expected_size: The size the plan recorded for this file.
        cross_device: True to use the verified copy path instead of a
            plain rename.

    Returns:
        ``(ok, note)``; a False result means nothing was moved and the
        note says why.
    """
    destination = part2 / src.name
    try:
        if src.is_symlink() or not src.is_file():
            return False, f"{src.name}: no longer a regular file"
        actual = src.stat().st_size
        if actual != expected_size:
            return False, (
                f"{src.name}: is {actual} bytes, plan expected {expected_size}"
            )
        if destination.exists():
            # A completed move whose source was never unlinked (a crash
            # between the rename and the unlink).  Heal it, do not
            # overwrite: confirm the two are identical, then drop the source.
            if destination.stat().st_size != expected_size:
                return False, (
                    f"{src.name}: already in Part 2 at a different size"
                )
            if azus_common.calculate_sha512(str(src)) != azus_common.calculate_sha512(
                str(destination)
            ):
                return False, f"{src.name}: already in Part 2 with different content"
            src.unlink()
            return True, ""
        if not cross_device:
            try:
                os.rename(src, destination)
            except OSError as exc:
                # A legitimate mount boundary inside one reported st_dev.
                # Not a safety failure — fall through to the verified path.
                logger.warning(
                    "  %s: rename refused (%s) — using the verified copy path.",
                    src.name, exc,
                )
                return _copy_verify_replace(src, destination, expected_size)
            if destination.stat().st_size != expected_size or src.exists():
                return False, (
                    f"{src.name}: rename did not behave atomically "
                    "(destination size wrong or source still present)"
                )
            return True, ""
        return _copy_verify_replace(src, destination, expected_size)
    except OSError as exc:
        return False, f"{src.name}: {exc}"


def execute_split(
    pair: PartPair,
    args: argparse.Namespace,
    raw_root: Path,
    esid_column: Optional[List[str]],
    planned: SplitPlan,
) -> Tuple[Dict[str, object], Optional[SplitPlan]]:
    """Apply one pair's split, re-deriving every gate first.

    Nothing here trusts the earlier assessment: both folders are
    re-scanned, the plan is rebuilt from the fresh union, and every gate
    runs again.  A plan that no longer matches the reviewed one is refused
    as ``PLAN_CHANGED`` — which is what a running Dropbox sync looks like
    — rather than applied.

    Args:
        pair: The pair to split.
        args: Parsed CLI arguments.
        raw_root: The scanned Raw Data folder.
        esid_column: Collectors-spreadsheet ESID values, or None.
        planned: The plan the operator reviewed.

    Returns:
        ``(row, plan)`` with the outcome verdict recorded on the row.
    """
    row, plan = assess_pair(pair, args, raw_root, esid_column)
    if plan is None:
        logger.error(
            "REFUSING to split ESID %s — %s.", pair.base_esid, row["Verdict"]
        )
        return row, None
    if (plan.part2_names != planned.part2_names
            or any(plan.sizes[n] != planned.sizes.get(n) for n in plan.ordered_names)):
        changed = sorted(
            n for n in plan.ordered_names if plan.sizes[n] != planned.sizes.get(n)
        )
        row["Verdict"] = PLAN_CHANGED
        row["Notes"] = (
            f"{len(changed)} file(s) changed since planning"
            + (f" (e.g. {changed[0]})" if changed else " — the cut moved")
        )
        logger.error(
            "REFUSING to split ESID %s — the folders changed since planning.",
            pair.base_esid,
        )
        return row, None
    if row["Verdict"] == ALREADY_SPLIT:
        row["Action Taken"] = "none (already split)"
        return row, plan

    union_before = dict(plan.sizes)

    copied, failures = copy_non_wav_files(pair.part1, pair.part2)
    row["Non-WAV Copied"] = copied
    if failures:
        row["Verdict"] = NAME_COLLISION
        row["Notes"] = "; ".join(failures[:5])
        row["Action Taken"] = "companion copy failed — no WAV was touched"
        logger.error(
            "REFUSING to move any WAV for ESID %s — companion copy failed: %s",
            pair.base_esid, "; ".join(failures[:3]),
        )
        return row, None

    for stale in sorted(pair.part2.glob(".*.partial")):
        logger.warning("  Removing stale partial from a prior run: %s", stale.name)
        stale.unlink(missing_ok=True)

    cross_device = not same_device(pair.part1, pair.part2)
    if cross_device:
        logger.warning(
            "  Part 1 and Part 2 are on DIFFERENT filesystems — using the "
            "verified copy path (slower; every byte is hashed)."
        )

    moved = 0
    notes: List[str] = []
    aborted = False
    for name in plan.part2_names:
        source = pair.part1 / name
        if not source.exists() and (pair.part2 / name).exists():
            continue  # already moved by an earlier run
        ok, note = move_one_wav(source, pair.part2, plan.sizes[name], cross_device)
        if ok:
            moved += 1
            if args.verbose:
                logger.info("  Moved %s -> %s", name, pair.part2.name)
            continue
        notes.append(note)
        logger.error("  REFUSING to move %s — %s", name, note)
        if "did not behave atomically" in note:
            logger.error(
                "  Aborting ESID %s entirely: the filesystem reported one "
                "device but did not move the file atomically.", pair.base_esid,
            )
            aborted = True
            break

    row["WAVs Moved"] = moved
    row["Action Taken"] = f"moved {moved} WAV(s), copied {copied} companion(s)"

    union_after: Dict[str, int] = {}
    try:
        for folder in (pair.part1, pair.part2):
            sizes, _untrusted, _warn = wav_sizes(folder)
            union_after.update(sizes)
    except OSError as exc:
        notes.append(f"could not re-scan after the move: {exc}")
    matched, mismatch_notes = compare_file_maps(union_before, union_after)
    if not matched:
        notes.extend(mismatch_notes)

    if aborted or notes:
        row["Verdict"] = INCOMPLETE
        row["Notes"] = "; ".join(([str(row["Notes"])] if row["Notes"] else []) + notes)[:600]
    else:
        row["Verdict"] = SPLIT_DONE
    return row, plan


# =====================================================================
#  Reporting
# =====================================================================

def row_needs_attention(row: Mapping[str, object]) -> bool:
    """Tell whether one report row should make the run exit nonzero.

    Args:
        row: A completed report row.

    Returns:
        True when the pair was refused, is still pending, or carries a
        warning a human should read.
    """
    if row["Verdict"] not in (SPLIT_DONE, ALREADY_SPLIT):
        return True
    if row["Over Limit"] or row["Pre-Split Record"] or row["Notes"]:
        return True
    if str(row["Collectors CSV"]) not in ("both rows present", "not checked"):
        return True
    for column in ("Zero-Byte WAVs", "Unparseable-Name WAVs"):
        if int(row[column] or 0) > 0:
            return True
    return False


def log_pair(row: Mapping[str, object]) -> None:
    """Print one pair's plan and verdict to the screen.

    Args:
        row: A completed report row.
    """
    logger.info("-" * 70)
    logger.info(
        "ESID %s: %s -> %s", row["ESID#"], row["Part 1 Folder"], row["Part 2 Folder"]
    )
    if row["WAV Count"]:
        logger.info(
            "  %s WAV(s), %s total | Part 1: %s WAV(s) %s | Part 2: %s WAV(s) %s "
            "| imbalance %s",
            row["WAV Count"], row["WAV Size"], row["Part 1 WAVs"], row["Part 1 Size"],
            row["Part 2 WAVs"], row["Part 2 Size"], row["Imbalance"],
        )
        if row["Cut Boundary"]:
            logger.info("  Cut: %s", row["Cut Boundary"])
    if int(row["Unparseable-Name WAVs"] or 0):
        logger.warning(
            "  %s WAV(s) carry no parseable timestamp — for those the cut is a "
            "NAME boundary, not a time boundary.", row["Unparseable-Name WAVs"],
        )
    if row["Over Limit"]:
        logger.warning("  %s still exceeds the limit after the split.", row["Over Limit"])
    if row["Pre-Split Record"]:
        logger.warning("  Pre-split leftover: %s", row["Pre-Split Record"])
    if str(row["Collectors CSV"]).startswith("MISSING"):
        logger.warning("  Collectors spreadsheet — %s", row["Collectors CSV"])
    if row["Notes"]:
        logger.warning("  %s", row["Notes"])
    logger.info(
        "  Verdict: %s — %s", row["Verdict"],
        _RECOMMENDED_ACTION.get(str(row["Verdict"]), "review"),
    )


def main() -> None:
    """Command-line entry point.  See the module docstring for details."""
    parser = argparse.ArgumentParser(
        description=(
            "Fill each ESID#NNN_Part_2_of_2 folder from its "
            "ESID#NNN_Part_1_of_2 twin: copy the non-WAV companions, then "
            "move the later half of the WAVs so the two halves are close "
            "to equal in bytes. Dry-run by default — nothing moves without "
            "--perform-split."
        ),
    )
    parser.add_argument(
        "raw_data_dir", metavar="RAW_DATA_DIR",
        help="Folder holding the ESID#NNN_Part_1_of_2 / _Part_2_of_2 directories.",
    )
    parser.add_argument(
        "--perform-split", action="store_true",
        help=(
            "Actually copy the companions and move the WAVs. Without this "
            "flag the tool only reports the plan (dry run)."
        ),
    )
    parser.add_argument(
        "--esid", nargs="+", default=None, metavar="ESID_OR_CSV",
        help=(
            "Limit to specific sites. Accepts bare ESIDs (445), either part "
            "name (445_Part_2_of_2), and/or CSV files whose first column "
            "lists ESIDs. Default: every pair found."
        ),
    )
    parser.add_argument(
        "--resume", action="store_true",
        help=(
            "Proceed when Part 2 already holds WAVs, to finish an "
            "interrupted run. The plan is derived from BOTH folders, so a "
            "resumed run computes the same cut and moves only what is left."
        ),
    )
    parser.add_argument(
        "--limit-bytes", type=int, default=_DEFAULT_LIMIT_BYTES, metavar="BYTES",
        help=(
            "Zenodo's per-record ceiling, used to warn when a half is still "
            f"too big (default: {_DEFAULT_LIMIT_BYTES} = 50 decimal GB; pass "
            "53687091200 for the binary reading)."
        ),
    )
    parser.add_argument(
        "--allow-still-oversized", action="store_true",
        help="Split even when a half would still exceed --limit-bytes.",
    )
    parser.add_argument(
        "--collectors-csv", default=None, metavar="PATH",
        help=(
            "Collectors spreadsheet to CHECK for the two per-part rows "
            "(never written). Default: "
            "Resources/2024_Total_Zenodo_Form_Spreadsheet.csv."
        ),
    )
    parser.add_argument(
        "--skip-collectors-check", action="store_true",
        help="Do not check the collectors spreadsheet for per-part rows.",
    )
    parser.add_argument(
        "--output", default=None, metavar="PATH",
        help=(
            "CSV report path (default: "
            "split_oversized_raw_folders_YYYYMMDD_HHMMSS.csv in the cwd)."
        ),
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Log every file moved and every entry skipped.",
    )
    args = parser.parse_args()

    if args.limit_bytes < 1:
        parser.error(f"--limit-bytes must be >= 1 (got {args.limit_bytes}).")

    azus_common.configure_logging(args.verbose)

    raw_root = Path(args.raw_data_dir)
    if not raw_root.is_dir():
        logger.error("Raw data folder not found: %s", raw_root)
        sys.exit(2)
    for protected in (_STAGING_AREA, _UPLOADED_DATA):
        if same_inode(raw_root, protected):
            logger.error(
                "RAW_DATA_DIR is %s — moving WAVs inside a prepared staging "
                "folder would invalidate a file_list.csv that is already "
                "hashed. Point this at Raw_Data instead.", protected,
            )
            sys.exit(2)

    output_path = (
        Path(args.output) if args.output
        else azus_common.timestamped_output_path("split_oversized_raw_folders")
    )
    collectors_csv = Path(
        args.collectors_csv if args.collectors_csv
        else azus_common.PROJECT_ROOT / "Resources"
        / "2024_Total_Zenodo_Form_Spreadsheet.csv"
    )

    logger.info("=" * 70)
    logger.info(
        "OVERSIZED RAW FOLDER SPLIT — plan%s",
        " + PERFORM" if args.perform_split else " (dry run)",
    )
    logger.info("=" * 70)
    logger.info("Raw data:   %s", raw_root.resolve())
    # Spelled out both ways: human_size() is binary, so a decimal-GB limit
    # renders as a smaller-looking "GB" and has confused readers before.
    logger.info(
        "Limit:      %d bytes (%.1f GB decimal / %s binary)",
        args.limit_bytes, args.limit_bytes / 1e9, human_size(args.limit_bytes),
    )
    logger.info(
        "Collectors: %s",
        "not checked" if args.skip_collectors_check else collectors_csv,
    )
    logger.info("Output:     %s", output_path)
    logger.info("=" * 70)

    pairs = find_part_pairs(raw_root)
    if args.esid:
        try:
            requested = azus_common.load_esid_args(args.esid)
        except ValueError as exc:
            logger.error("%s", exc)
            sys.exit(2)
        wanted = set()
        for esid in requested:
            parsed = parse_part_esid(esid)
            wanted.add((parsed[0] if parsed else esid).casefold())
        pairs = [p for p in pairs if p.base_esid.casefold() in wanted]
        if not pairs:
            logger.error(
                "None of the requested ESID(s) has a Part_1_of_2 folder under %s.",
                raw_root,
            )
            sys.exit(2)
    if not pairs:
        logger.info("No ESID#NNN_Part_1_of_2 folders found — nothing to do.")
        sys.exit(0)
    logger.info("Found %d pair(s) to consider.", len(pairs))

    esid_column = (
        None if args.skip_collectors_check else read_collectors_esids(collectors_csv)
    )

    rows: List[Dict[str, object]] = []
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Written and flushed per pair: a crash must not lose the record of
    # pairs whose WAVs have already been moved.
    with open(output_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS)
        writer.writeheader()
        for pair in pairs:
            row, plan = assess_pair(pair, args, raw_root, esid_column)
            if args.perform_split and plan is not None and row["Verdict"] in (
                SPLIT_PLANNED, RESUMABLE, ALREADY_SPLIT
            ):
                row, _plan = execute_split(
                    pair, args, raw_root, esid_column, plan
                )
            elif not args.perform_split and plan is not None:
                row["Action Taken"] = "none (dry run)"
            log_pair(row)
            writer.writerow(row)
            fh.flush()
            rows.append(row)

    attention = [r for r in rows if row_needs_attention(r)]
    logger.info("=" * 70)
    logger.info("SUMMARY")
    logger.info("=" * 70)
    for verdict in sorted({str(r["Verdict"]) for r in rows}):
        logger.info(
            "%-24s %d", verdict, sum(1 for r in rows if r["Verdict"] == verdict)
        )
    logger.info("WAVs moved this run:     %d", sum(int(r["WAVs Moved"] or 0) for r in rows))
    logger.info("Report: %s (%d row(s))", output_path, len(rows))
    if not args.perform_split and any(
        r["Verdict"] in (SPLIT_PLANNED, RESUMABLE) for r in rows
    ):
        logger.warning(
            "Dry run — nothing was moved. Review the Cut Boundary column, then "
            "re-run with --perform-split."
        )
    if attention:
        logger.warning("%d pair(s) need attention — see the Verdict column.", len(attention))
    logger.info("=" * 70)
    sys.exit(1 if attention else 0)


if __name__ == "__main__":
    main()
