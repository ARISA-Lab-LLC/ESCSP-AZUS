#!/usr/bin/env python3
"""Per-ESID WAV integrity report — disk files vs. ZIP archive entries.

WHAT THIS TOOL DOES
===================
Zenodo uploads have been failing, and one class of root cause is bad
source data: zero-byte, truncated, or placeholder WAV recordings, or a
mismatch between the WAV files on disk and the WAV entries baked into
``ESID_NNN.zip``.  This tool makes those problems visible in one CSV.

EVERY NUMBER IS DOUBLE-CHECKED
==============================
Getting correct data is paramount, so each size is measured TWO
independent ways and any disagreement is reported in a dedicated
cross-check column — a single measurement is never trusted on its own:

  * DISK WAV size is measured by (1) the filesystem (``stat``) AND
    (2) the file's own RIFF/WAVE header, which declares its length.
    These MUST agree.  When they don't, the file is flagged:
      - stat says 0 but bytes are readable  -> cloud placeholder /
        stale metadata (Dropbox online-only files do this).  Such a
        file is NOT counted as zero-byte; it is flagged instead.
      - stat < header-declared length        -> truncated recording.
      - no valid RIFF/WAVE header            -> not a real WAV.

  * ZIP WAV size (uncompressed ``file_size`` from the archive index)
    is cross-checked against the entry's CRC and compressed size:
    a ``file_size == 0`` entry must have ``CRC == 0``; a nonzero CRC
    means the size field is unreliable and the entry actually has data.

  * MATCH compares the two sides FILE BY FILE (name -> size), not by
    aggregate totals.  Two different file sets can share a count and a
    byte total (e.g. two WAVs with swapped sizes), so aggregate-only
    comparison can report a false match; per-file comparison cannot.

Hidden macOS sidecar files (``._name.WAV`` AppleDouble, ``.DS_Store``)
are skipped and counted separately — they are not recordings.

USAGE
=====
    python Resources/audit_wav_integrity.py /path/to/Raw_Data
    python Resources/audit_wav_integrity.py /path/to/Raw_Data \
        --output my_report.csv --tiny-threshold 4096 --verbose

EXIT CODES
==========
    0  every ESID is clean (no zero/tiny WAVs, no mismatches, no
       cross-check discrepancies, no unreadable ZIPs).  "ZIP Not Found"
       alone does NOT fail the run.
    1  at least one problem found (see the CSV and the summary block).
    2  usage error (raw-data folder missing, bad threshold, etc.).
"""

import argparse
import csv
import logging
import re
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import azus_common

logger = logging.getLogger("azus.wav_audit")

# Shared project layout (see azus_common.py).
_PROJECT_ROOT = azus_common.PROJECT_ROOT
_STAGING_AREA = azus_common.STAGING_AREA
_UPLOADED_DATA = azus_common.UPLOADED_DATA

# A valid WAV has a 44-byte header before any audio; files below ~1 KB
# contain no usable recording.
_DEFAULT_TINY_THRESHOLD = 1024

# WAV ChunkSize is a uint32, so the RIFF header cannot describe files
# larger than 4 GiB - 1.  Above this we skip the header/stat comparison
# rather than raise a false "truncated" flag.
_UINT32_MAX = 0xFFFFFFFF

_CSV_COLUMNS = [
    "ESID#",
    "Disk WAV Count",
    "Disk WAV Bytes",
    "Disk WAV Size",
    "Disk Zero-Byte WAVs",
    "Disk Tiny WAVs",
    "Disk Cross-Check",
    "ZIP Location",
    "ZIP WAV Count",
    "ZIP WAV Bytes",
    "ZIP WAV Size",
    "ZIP Zero-Byte WAVs",
    "ZIP Tiny WAVs",
    "ZIP Cross-Check",
    "Match",
    "Notes",
]


def human_size(num_bytes: int) -> str:
    """Readable size that never rounds a nonzero value to '0' (unlike a
    GB column, where a 4 MB folder would display 0.0).

    Args:
        num_bytes: Size in bytes to render.

    Returns:
        A string like ``"0 B"``, ``"4.00 MB"``, or ``"1.50 GB"`` —
        bytes are shown as an integer, larger units to two decimals,
        and everything at or above 1 GiB is reported in GB.
    """
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} GB"


# =====================================================================
#  Pure helpers (no I/O — unit-tested directly)
# =====================================================================

def parse_riff_declared_size(header: bytes) -> Optional[int]:
    """Total file size a WAV header declares, or None if not a valid WAV.

    A canonical WAV begins ``RIFF`` <uint32 ChunkSize LE> ``WAVE``.  The
    declared total size is ``ChunkSize + 8`` (ChunkSize counts everything
    after the first 8 bytes).  Returns None for anything that is not a
    RIFF/WAVE header (too short, wrong magic, RF64, AppleDouble, etc.).

    Args:
        header: The first bytes of the file (at least the leading 12
            for a valid check).

    Returns:
        The declared total file size in bytes, or None when ``header``
        is not a well-formed RIFF/WAVE header.
    """
    if len(header) < 12 or header[0:4] != b"RIFF" or header[8:12] != b"WAVE":
        return None
    return int.from_bytes(header[4:8], "little") + 8


@dataclass
class SizeVerdict:
    """Outcome of classifying one WAV's size against its cross-check.

    Attributes:
        is_zero: True when the file is genuinely empty (0 bytes with a
            corroborating cross-check).
        is_tiny: True when the file is nonzero but below the tiny
            threshold.
        discrepancy: A human-readable cross-check failure message, or
            None when the two measurements agree.
    """
    is_zero: bool
    is_tiny: bool
    discrepancy: Optional[str]


def classify_disk_wav(
    stat_size: int, header: bytes, tiny_threshold: int
) -> SizeVerdict:
    """Classify one on-disk WAV using stat size AND its RIFF header.

    The two measurements must agree.  When they don't, the file is
    flagged (never silently counted as zero or tiny):

      * stat_size == 0 but header bytes ARE readable  -> placeholder /
        stale metadata; flagged, NOT counted as zero-byte.
      * stat_size == 0 and nothing readable           -> truly empty.
      * stat_size < header-declared length            -> truncated.
      * no valid RIFF/WAVE header (and size in range) -> not a real WAV.

    Args:
        stat_size: Size reported by the filesystem (``stat``), in bytes.
        header: The file's leading bytes for the RIFF cross-check.
        tiny_threshold: Nonzero files smaller than this are counted tiny.

    Returns:
        A :class:`SizeVerdict` recording the zero/tiny flags and any
        cross-check discrepancy.
    """
    riff = parse_riff_declared_size(header)

    if stat_size == 0:
        if header:  # stat claims empty, yet we read bytes -> stat is lying
            return SizeVerdict(
                is_zero=False, is_tiny=False,
                discrepancy=(
                    f"stat reports 0 bytes but {len(header)} byte(s) are "
                    "readable (possible cloud placeholder / stale metadata)"
                ),
            )
        return SizeVerdict(is_zero=True, is_tiny=False, discrepancy=None)

    is_tiny = stat_size < tiny_threshold
    if stat_size > _UINT32_MAX:
        # Too big for a uint32 RIFF size field — can't cross-check here.
        return SizeVerdict(is_zero=False, is_tiny=is_tiny, discrepancy=None)
    if riff is None:
        return SizeVerdict(
            is_zero=False, is_tiny=is_tiny,
            discrepancy="no valid RIFF/WAVE header (not a well-formed WAV)",
        )
    if stat_size < riff:
        return SizeVerdict(
            is_zero=False, is_tiny=is_tiny,
            discrepancy=(f"truncated: header declares {riff} bytes, "
                         f"file is only {stat_size}"),
        )
    if stat_size > riff:
        return SizeVerdict(
            is_zero=False, is_tiny=is_tiny,
            discrepancy=(f"header declares {riff} bytes but file is "
                         f"{stat_size} (unexpected trailing data)"),
        )
    return SizeVerdict(is_zero=False, is_tiny=is_tiny, discrepancy=None)


def classify_zip_entry(
    file_size: int, compress_size: int, compress_type: int,
    crc: int, tiny_threshold: int,
) -> SizeVerdict:
    """Classify one ZIP WAV entry, cross-checking the size field.

    All inputs come from the archive's central directory (no
    decompression).  An entry that declares ``file_size == 0`` is only
    genuinely empty if its CRC is also 0 (CRC-32 of no data is 0) and,
    for a STORED entry, its compressed size is 0 too.  Otherwise the
    size field is unreliable and the entry actually contains data.

    Args:
        file_size: Uncompressed size the entry declares, in bytes.
        compress_size: Compressed (stored) size of the entry, in bytes.
        compress_type: The entry's compression method (e.g.
            ``zipfile.ZIP_STORED``).
        crc: The entry's CRC-32 checksum.
        tiny_threshold: Nonzero entries smaller than this are tiny.

    Returns:
        A :class:`SizeVerdict` recording the zero/tiny flags and any
        cross-check discrepancy.
    """
    if file_size == 0:
        if crc != 0:
            return SizeVerdict(
                is_zero=False, is_tiny=False,
                discrepancy=(f"size=0 but CRC={crc:#010x} is nonzero — "
                             "entry has data; size field unreliable"),
            )
        if compress_size > 0:
            return SizeVerdict(
                is_zero=False, is_tiny=False,
                discrepancy=(f"size=0 but compressed size={compress_size} — "
                             "entry has data; size field unreliable"),
            )
        return SizeVerdict(is_zero=True, is_tiny=False, discrepancy=None)

    disc = None
    if compress_type == zipfile.ZIP_STORED and compress_size != file_size:
        disc = (f"stored entry size mismatch: file_size={file_size}, "
                f"compressed={compress_size}")
    return SizeVerdict(
        is_zero=False, is_tiny=file_size < tiny_threshold, discrepancy=disc,
    )


def compare_file_maps(
    disk_sizes: Dict[str, int], zip_sizes: Dict[str, int]
) -> Tuple[bool, List[str]]:
    """Compare disk vs ZIP as {name: size} maps — per file, not aggregate.

    Returns (is_match, notes).  Catches the three real divergences an
    aggregate (count + total-bytes) comparison misses: a file missing
    from the ZIP, an extra file in the ZIP, and — crucially — a file
    present on both sides at a DIFFERENT size (truncation / swap).

    Args:
        disk_sizes: ``{name: size}`` for the WAVs found on disk.
        zip_sizes: ``{name: size}`` for the WAVs found in the ZIP.

    Returns:
        A ``(is_match, notes)`` tuple: ``is_match`` is True only when
        the two maps agree file-for-file, and ``notes`` lists each
        divergence found (empty when they match).
    """
    disk_names, zip_names = set(disk_sizes), set(zip_sizes)
    missing = sorted(disk_names - zip_names)
    extra = sorted(zip_names - disk_names)
    size_diff = sorted(
        n for n in (disk_names & zip_names) if disk_sizes[n] != zip_sizes[n]
    )
    notes: List[str] = []
    if missing:
        notes.append(f"{len(missing)} disk WAV(s) missing from ZIP "
                     f"(e.g. {missing[0]})")
    if extra:
        notes.append(f"{len(extra)} ZIP WAV(s) not on disk "
                     f"(e.g. {extra[0]})")
    if size_diff:
        n = size_diff[0]
        notes.append(f"{len(size_diff)} WAV(s) differ in size "
                     f"(e.g. {n}: disk {disk_sizes[n]} vs ZIP {zip_sizes[n]})")
    return (not notes), notes


# =====================================================================
#  Aggregated stats + I/O scanners
# =====================================================================

@dataclass
class WavStats:
    """Aggregated WAV statistics for one side (disk or ZIP).

    Every file is recorded once in ``ledger`` (name, size, is_zero,
    is_tiny) as it is applied, IN ADDITION to updating the running
    aggregates.  ``verify()`` recomputes the aggregates from the ledger
    and confirms they match — a belt-and-suspenders self-check that
    catches any accumulation logic error, independent of the values the
    CSV reports.

    Attributes:
        count: Number of WAVs applied to this side.
        total_bytes: Running sum of every applied file's size.
        sizes: ``{name: size}`` map (last-write-wins on a repeat name).
        zero_names: Names classified as genuinely zero-byte.
        tiny_names: Names classified as tiny (nonzero, below threshold).
        discrepancies: ``(name, reason)`` pairs for cross-check failures.
        skipped_sidecars: Count of macOS sidecar files skipped.
        ledger: Per-file ``(name, size, is_zero, is_tiny)`` record kept
            for the independent :meth:`verify` recomputation.
    """
    count: int = 0
    total_bytes: int = 0
    sizes: Dict[str, int] = field(default_factory=dict)   # name -> size
    zero_names: List[str] = field(default_factory=list)
    tiny_names: List[str] = field(default_factory=list)
    discrepancies: List[Tuple[str, str]] = field(default_factory=list)
    skipped_sidecars: int = 0
    ledger: List[Tuple[str, int, bool, bool]] = field(default_factory=list)

    @property
    def names(self) -> Set[str]:
        """Set of every applied file name.

        Returns:
            The keys of ``sizes`` as a set.
        """
        return set(self.sizes)

    @property
    def zero_count(self) -> int:
        """Number of genuinely zero-byte files.

        Returns:
            The length of ``zero_names``.
        """
        return len(self.zero_names)

    @property
    def tiny_count(self) -> int:
        """Number of tiny (nonzero, below-threshold) files.

        Returns:
            The length of ``tiny_names``.
        """
        return len(self.tiny_names)

    @property
    def discrepancy_count(self) -> int:
        """Number of cross-check discrepancies recorded.

        Returns:
            The length of ``discrepancies``.
        """
        return len(self.discrepancies)

    def _apply(self, name: str, size: int, verdict: SizeVerdict) -> None:
        """Record one classified file in the ledger and aggregates.

        Args:
            name: File (base)name being applied.
            size: The file's size in bytes.
            verdict: The :class:`SizeVerdict` from classifying the file.
        """
        self.ledger.append((name, size, verdict.is_zero, verdict.is_tiny))
        self.count += 1
        self.total_bytes += size
        self.sizes[name] = size
        if verdict.is_zero:
            self.zero_names.append(name)
        elif verdict.is_tiny:
            self.tiny_names.append(name)
        if verdict.discrepancy:
            self.discrepancies.append((name, verdict.discrepancy))

    def verify(self) -> List[str]:
        """Re-derive every reported aggregate from the raw ledger and
        confirm it matches the incrementally-maintained field.  Returns a
        list of human-readable failures ([] means all checks passed).

        This is an INDEPENDENT recomputation: the aggregates were built
        by ``_apply`` incrementally; here they are rebuilt from scratch
        from the ledger of per-file facts.  A bug in the accumulation
        (e.g. counting a file but not adding its bytes) makes the two
        disagree, so a wrong number can never be reported as if verified.

        Returns:
            A list of human-readable failure messages; an empty list
            means every self-check passed.
        """
        errs: List[str] = []

        if self.count != len(self.ledger):
            errs.append(
                f"count {self.count} != {len(self.ledger)} ledger entries")

        ledger_bytes = sum(size for _, size, _, _ in self.ledger)
        if self.total_bytes != ledger_bytes:
            errs.append(
                f"total_bytes {self.total_bytes} != {ledger_bytes} "
                "re-summed from ledger")

        recomputed_sizes: Dict[str, int] = {}
        for name, size, _, _ in self.ledger:
            recomputed_sizes[name] = size          # last-write-wins, like _apply
        if recomputed_sizes != self.sizes:
            errs.append("sizes map disagrees with ledger recomputation")

        ledger_zero = sum(1 for _, _, is_zero, _ in self.ledger if is_zero)
        if self.zero_count != ledger_zero:
            errs.append(
                f"zero-byte count {self.zero_count} != {ledger_zero} in ledger")

        ledger_tiny = sum(1 for _, _, _, is_tiny in self.ledger if is_tiny)
        if self.tiny_count != ledger_tiny:
            errs.append(
                f"tiny count {self.tiny_count} != {ledger_tiny} in ledger")

        both = sorted(set(self.zero_names) & set(self.tiny_names))
        if both:
            errs.append(f"file(s) counted BOTH zero and tiny: {both}")

        return errs


def _is_wav_name(name: str) -> bool:
    """True for a real WAV filename — excludes macOS AppleDouble
    sidecars (``._foo.WAV``), which also end in '.wav' but are not
    recordings.

    Args:
        name: File (base)name to test.

    Returns:
        True when ``name`` ends in ``.wav`` (any case) and is not an
        AppleDouble sidecar, otherwise False.
    """
    return name.lower().endswith(".wav") and not name.startswith("._")


def scan_disk_wavs(folder: Path, tiny_threshold: int) -> WavStats:
    """Stat the top-level .wav files of one raw ESID folder and
    cross-check each against its RIFF header.  Not recursive.

    Args:
        folder: The ESID folder whose top-level WAVs are scanned.
        tiny_threshold: Nonzero files smaller than this are counted tiny.

    Returns:
        A :class:`WavStats` aggregating every WAV in the folder;
        per-file stat/read failures are captured as discrepancies
        rather than raised, and macOS sidecars are skipped.
    """
    stats = WavStats()
    for entry in sorted(folder.iterdir()):
        if not entry.is_file() or not entry.name.lower().endswith(".wav"):
            continue
        if entry.name.startswith("._"):
            stats.skipped_sidecars += 1
            continue
        try:
            stat_size = entry.stat().st_size
        except OSError as exc:
            stats.discrepancies.append((entry.name, f"stat failed: {exc}"))
            continue
        try:
            with open(entry, "rb") as fh:
                header = fh.read(12)
        except OSError as exc:
            # Cannot read the file for the header cross-check — flag it,
            # but still classify zero/tiny from the stat size we have, and
            # route through _apply so the ledger stays complete.
            verdict = SizeVerdict(
                is_zero=(stat_size == 0),
                is_tiny=(0 < stat_size < tiny_threshold),
                discrepancy=f"unreadable for header check: {exc}",
            )
            stats._apply(entry.name, stat_size, verdict)
            continue
        verdict = classify_disk_wav(stat_size, header, tiny_threshold)
        stats._apply(entry.name, stat_size, verdict)
    return stats


def scan_zip_wavs(
    zip_path: Path, tiny_threshold: int
) -> Tuple[Optional[WavStats], Optional[str]]:
    """Read the .wav entries of a ZIP's index (no extraction) and
    cross-check each entry's declared size.  Returns (stats, None) or
    (None, error) when the archive cannot be read.

    Args:
        zip_path: Path to the ``ESID_NNN.zip`` archive to read.
        tiny_threshold: Nonzero entries smaller than this are tiny.

    Returns:
        A ``(stats, None)`` tuple on success, or ``(None, error)`` when
        the archive is unreadable — ``error`` being a short message
        such as ``"BadZipFile: ..."``.
    """
    stats = WavStats()
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                basename = info.filename.rsplit("/", 1)[-1]
                if not _is_wav_name(basename):
                    if basename.lower().endswith(".wav"):
                        stats.skipped_sidecars += 1
                    continue
                if basename in stats.sizes:
                    stats.discrepancies.append(
                        (basename, "duplicate WAV basename in ZIP")
                    )
                verdict = classify_zip_entry(
                    info.file_size, info.compress_size, info.compress_type,
                    info.CRC, tiny_threshold,
                )
                stats._apply(basename, info.file_size, verdict)
    except (zipfile.BadZipFile, OSError) as exc:
        return None, f"{exc.__class__.__name__}: {exc}"
    return stats, None


# =====================================================================
#  Row assembly
# =====================================================================

def find_raw_esid_folders(raw_root: Path) -> List[Tuple[int, str, Path]]:
    """Discover ESID subfolders, sorted numerically.  Non-ESID
    subfolders are skipped with a warning.

    Args:
        raw_root: Folder whose immediate subfolders are candidate ESIDs.

    Returns:
        A list of ``(esid_int, esid_padded, folder)`` tuples sorted by
        ESID number; duplicate padded ESIDs are all included and logged.
    """
    found: List[Tuple[int, str, Path]] = []
    for entry in sorted(raw_root.iterdir()):
        if not entry.is_dir():
            continue
        padded = azus_common.parse_esid(entry.name)
        if padded is None:
            logger.warning("Skipping non-ESID subfolder: %s", entry.name)
            continue
        found.append((int(padded), padded, entry))
    found.sort(key=lambda t: t[0])

    seen: Dict[str, Path] = {}
    for _, padded, folder in found:
        if padded in seen:
            logger.warning(
                "Duplicate ESID %s: both %s and %s — both will be reported.",
                padded, seen[padded].name, folder.name,
            )
        else:
            seen[padded] = folder
    return found


def locate_zip(esid_padded: str) -> Tuple[Optional[Path], str]:
    """Find ESID_NNN.zip in Staging_Area/ first, then Uploaded_Data/.

    Args:
        esid_padded: Zero-padded 3-digit ESID (e.g. ``"046"``).

    Returns:
        A ``(zip_path, location)`` tuple: the located archive and either
        ``"Staging"`` or ``"Uploaded"``, or ``(None, "Not Found")`` when
        no matching ZIP exists in either location.
    """
    for base, location in (
        (_STAGING_AREA / f"ESID_{esid_padded}_Staging", "Staging"),
        (_UPLOADED_DATA / f"ESID_{esid_padded}_Uploaded", "Uploaded"),
    ):
        zip_path = base / f"ESID_{esid_padded}.zip"
        if zip_path.is_file():
            return zip_path, location
    return None, "Not Found"


def _cross_check_cell(stats: Optional[WavStats]) -> str:
    """Render one side's cross-check column value.

    Args:
        stats: The side's :class:`WavStats`, or None when unavailable.

    Returns:
        ``""`` when ``stats`` is None, ``"OK"`` when there are no
        discrepancies, else ``"DISCREPANCY (N)"`` with the count.
    """
    if stats is None:
        return ""
    if stats.discrepancy_count == 0:
        return "OK"
    return f"DISCREPANCY ({stats.discrepancy_count})"


def build_row(
    esid_padded: str,
    disk: WavStats,
    zip_location: str,
    zip_stats: Optional[WavStats],
    zip_error: Optional[str],
) -> Dict[str, object]:
    """Assemble one CSV row, including the file-by-file Match verdict,
    both cross-check columns, and explanatory Notes.

    Args:
        esid_padded: Zero-padded 3-digit ESID for the row.
        disk: The disk-side :class:`WavStats`.
        zip_location: ``"Staging"``, ``"Uploaded"``, or ``"Not Found"``.
        zip_stats: The ZIP-side :class:`WavStats`, or None when the ZIP
            is missing or unreadable.
        zip_error: The ZIP read error message, or None.

    Returns:
        A dict keyed by :data:`_CSV_COLUMNS` for one report row.  The
        Match cell is ``"N/A"`` with no ZIP, otherwise ``"YES"``/``"NO"``.
    """
    notes: List[str] = []
    for name, reason in disk.discrepancies:
        notes.append(f"disk {name}: {reason}")

    row: Dict[str, object] = {
        "ESID#": esid_padded,
        "Disk WAV Count": disk.count,
        "Disk WAV Bytes": disk.total_bytes,
        "Disk WAV Size": human_size(disk.total_bytes),
        "Disk Zero-Byte WAVs": disk.zero_count,
        "Disk Tiny WAVs": disk.tiny_count,
        "Disk Cross-Check": _cross_check_cell(disk),
        "ZIP Location": zip_location,
        "ZIP WAV Count": "",
        "ZIP WAV Bytes": "",
        "ZIP WAV Size": "",
        "ZIP Zero-Byte WAVs": "",
        "ZIP Tiny WAVs": "",
        "ZIP Cross-Check": "",
        "Match": "N/A",
        "Notes": "",
    }

    if zip_stats is None:
        notes.append(
            f"ZIP unreadable: {zip_error}" if zip_error else "no ZIP found"
        )
        row["Notes"] = "; ".join(notes)
        return row

    row["ZIP WAV Count"] = zip_stats.count
    row["ZIP WAV Bytes"] = zip_stats.total_bytes
    row["ZIP WAV Size"] = human_size(zip_stats.total_bytes)
    row["ZIP Zero-Byte WAVs"] = zip_stats.zero_count
    row["ZIP Tiny WAVs"] = zip_stats.tiny_count
    row["ZIP Cross-Check"] = _cross_check_cell(zip_stats)
    for name, reason in zip_stats.discrepancies:
        notes.append(f"zip {name}: {reason}")

    match_ok, match_notes = compare_file_maps(disk.sizes, zip_stats.sizes)
    # A duplicate basename in the ZIP collapses in the size map, so guard
    # it explicitly: entry count must equal the number of distinct names.
    if zip_stats.count != len(zip_stats.sizes):
        match_ok = False
        match_notes.append("ZIP contains duplicate WAV basenames")
    row["Match"] = "YES" if match_ok else "NO"
    notes.extend(match_notes)

    row["Notes"] = "; ".join(notes)
    return row


def row_has_problem(row: Dict[str, object]) -> bool:
    """True when a row should fail the run (exit 1): a mismatch, an
    unreadable ZIP, any zero/tiny WAV, or any cross-check discrepancy.

    Args:
        row: A row dict produced by :func:`build_row`.

    Returns:
        True when the row indicates a problem (mismatch, self-check
        failure, unreadable ZIP, zero/tiny WAVs, or a cross-check
        discrepancy); a "ZIP Not Found" alone is not a problem.
    """
    if row["Match"] == "NO":
        return True
    notes = str(row["Notes"])
    if "SELF-CHECK FAILED" in notes:
        return True
    if notes.startswith("ZIP unreadable") or "; ZIP unreadable" in notes:
        return True
    for col in ("Disk Zero-Byte WAVs", "Disk Tiny WAVs",
                "ZIP Zero-Byte WAVs", "ZIP Tiny WAVs"):
        if isinstance(row[col], int) and row[col] > 0:
            return True
    for col in ("Disk Cross-Check", "ZIP Cross-Check"):
        if str(row[col]).startswith("DISCREPANCY"):
            return True
    return False


def write_report(rows: List[Dict[str, object]], output_path: Path) -> None:
    """Write the assembled rows to the CSV.

    Args:
        rows: Row dicts from :func:`build_row`.
        output_path: Destination CSV path (overwritten if present).
    """
    with open(output_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    logger.info("Report written: %s (%d row(s))", output_path, len(rows))


def _log_verbose(esid: str, disk: WavStats, zip_stats: Optional[WavStats]) -> None:
    """Log every zero/tiny/discrepant WAV for one ESID (``--verbose``).

    Args:
        esid: Zero-padded 3-digit ESID being logged.
        disk: The disk-side :class:`WavStats`.
        zip_stats: The ZIP-side :class:`WavStats`, or None to skip it.
    """
    for label, stats in (("disk", disk), ("ZIP", zip_stats)):
        if stats is None:
            continue
        for name in stats.zero_names:
            logger.info("  [ESID %s] zero-byte %s WAV: %s", esid, label, name)
        for name in stats.tiny_names:
            logger.info("  [ESID %s] tiny %s WAV: %s", esid, label, name)
        for name, reason in stats.discrepancies:
            logger.info("  [ESID %s] %s cross-check: %s — %s",
                        esid, label, name, reason)


def main() -> None:
    """Command-line entry point.  See the module docstring for details."""
    parser = argparse.ArgumentParser(
        description=(
            "Walk every ESID_NNN subfolder of RAW_DATA_DIR and write a CSV "
            "comparing the .WAV files on disk against the .WAV entries in "
            "the matching ESID_NNN.zip. Every size is measured two "
            "independent ways; disagreements appear in the Cross-Check "
            "columns."
        ),
    )
    parser.add_argument("raw_data_dir", metavar="RAW_DATA_DIR",
                        help="Folder whose ESID_NNN subfolders hold raw .WAV files.")
    parser.add_argument("--output", metavar="PATH", default=None,
                        help="CSV output path (default: "
                             "wav_integrity_report_YYYYMMDD_HHMMSS.csv in cwd).")
    parser.add_argument("--tiny-threshold", type=int,
                        default=_DEFAULT_TINY_THRESHOLD, metavar="BYTES",
                        help="Files with 0 < size < BYTES are counted 'tiny' "
                             f"(default: {_DEFAULT_TINY_THRESHOLD}).")
    parser.add_argument("--verbose", action="store_true",
                        help="Name every zero/tiny/discrepant WAV in the log.")
    args = parser.parse_args()

    if args.tiny_threshold < 1:
        parser.error(f"--tiny-threshold must be >= 1 (got {args.tiny_threshold}).")

    azus_common.configure_logging()

    raw_root = Path(args.raw_data_dir)
    if not raw_root.is_dir():
        logger.error("Raw data folder not found: %s", raw_root)
        sys.exit(2)

    output_path = (
        Path(args.output) if args.output
        else azus_common.timestamped_output_path("wav_integrity_report")
    )

    logger.info("=" * 70)
    logger.info("AZUS WAV INTEGRITY AUDIT (every size double-checked)")
    logger.info("=" * 70)
    logger.info("Raw data:       %s", raw_root.resolve())
    logger.info("ZIP lookup:     %s | %s", _STAGING_AREA, _UPLOADED_DATA)
    logger.info("Tiny threshold: < %d bytes", args.tiny_threshold)
    logger.info("Output:         %s", output_path)
    logger.info("=" * 70)

    esid_folders = find_raw_esid_folders(raw_root)
    if not esid_folders:
        logger.error("No ESID_NNN subfolders found in %s", raw_root)
        sys.exit(2)

    rows: List[Dict[str, object]] = []
    problem_esids: List[str] = []
    zip_not_found: List[str] = []
    discrepancy_esids: List[str] = []
    self_check_failed: List[str] = []

    for _, esid_padded, folder in esid_folders:
        disk = scan_disk_wavs(folder, args.tiny_threshold)
        zip_path, zip_location = locate_zip(esid_padded)
        zip_stats: Optional[WavStats] = None
        zip_error: Optional[str] = None
        if zip_path is not None:
            zip_stats, zip_error = scan_zip_wavs(zip_path, args.tiny_threshold)

        row = build_row(esid_padded, disk, zip_location, zip_stats, zip_error)

        # --- Belt-and-suspenders: independently re-derive the aggregates
        # from each side's ledger and confirm the reported numbers match.
        sc_errors = [f"disk: {e}" for e in disk.verify()]
        if zip_stats is not None:
            sc_errors += [f"zip: {e}" for e in zip_stats.verify()]
        if sc_errors:
            for e in sc_errors:
                logger.error("[ESID %s] SELF-CHECK FAILED — %s", esid_padded, e)
            row["Notes"] = "; ".join(
                x for x in (f"SELF-CHECK FAILED: {' | '.join(sc_errors)}",
                            str(row["Notes"])) if x
            )
            self_check_failed.append(esid_padded)
        elif args.verbose:
            logger.info(
                "  [ESID %s] self-check OK (disk %d file(s)/%d bytes%s)",
                esid_padded, disk.count, disk.total_bytes,
                f"; ZIP {zip_stats.count} entr(ies)/{zip_stats.total_bytes} bytes"
                if zip_stats else "",
            )

        rows.append(row)

        disc = disk.discrepancy_count + (
            zip_stats.discrepancy_count if zip_stats else 0)
        logger.info(
            "[ESID %s] disk: %d WAV(s) %s, %d zero, %d tiny, %d cross-check | "
            "ZIP (%s): %s | Match: %s",
            esid_padded, disk.count, human_size(disk.total_bytes),
            disk.zero_count, disk.tiny_count, disk.discrepancy_count,
            zip_location,
            (f"{zip_stats.count} WAV(s) {human_size(zip_stats.total_bytes)}, "
             f"{zip_stats.zero_count} zero, {zip_stats.tiny_count} tiny, "
             f"{zip_stats.discrepancy_count} cross-check")
            if zip_stats else "-",
            row["Match"],
        )
        if disk.skipped_sidecars or (zip_stats and zip_stats.skipped_sidecars):
            logger.info("  [ESID %s] skipped %d disk + %d ZIP macOS sidecar "
                        "file(s) (._*.WAV)", esid_padded,
                        disk.skipped_sidecars,
                        zip_stats.skipped_sidecars if zip_stats else 0)
        if args.verbose:
            _log_verbose(esid_padded, disk, zip_stats)

        if row_has_problem(row):
            problem_esids.append(esid_padded)
        if disc:
            discrepancy_esids.append(esid_padded)
        if zip_location == "Not Found":
            zip_not_found.append(esid_padded)

    write_report(rows, output_path)

    logger.info("=" * 70)
    logger.info("SUMMARY")
    logger.info("=" * 70)
    logger.info("ESIDs scanned:            %d", len(rows))
    logger.info("Disk WAVs total:          %d (%s)",
                sum(int(r["Disk WAV Count"]) for r in rows),
                human_size(sum(int(r["Disk WAV Bytes"]) for r in rows)))
    logger.info("ESIDs with problems:      %d%s", len(problem_esids),
                f" ({', '.join(problem_esids)})" if problem_esids else "")
    logger.info("ESIDs w/ cross-check flags: %d%s", len(discrepancy_esids),
                f" ({', '.join(discrepancy_esids)})" if discrepancy_esids else "")
    if self_check_failed:
        logger.error("Internal self-check:      FAILED for %d ESID(s): %s "
                     "— DO NOT trust the totals for these rows; please report.",
                     len(self_check_failed), ", ".join(self_check_failed))
    else:
        logger.info("Internal self-check:      PASSED for all %d ESID(s) "
                    "(aggregates re-derived from per-file ledger)", len(rows))
    logger.info("ZIP not found (info):     %d%s", len(zip_not_found),
                f" ({', '.join(zip_not_found)})" if zip_not_found else "")
    logger.info("Report: %s", output_path)
    logger.info("=" * 70)

    sys.exit(1 if problem_esids else 0)


if __name__ == "__main__":
    main()
