#!/usr/bin/env python3
"""List the unique ESID numbers among the subfolders of a folder.

Scans the immediate subfolders of MAIN_FOLDER, pulls the ESID number out
of every folder named like ESID_073, ESID_073_Staging, or ESID#73
(case-insensitive — the same convention the other AZUS tools use),
de-duplicates them (so ESID_073_Staging and ESID_073_Uploaded count as
one), and prints them zero-padded in numeric order, one per line.

The ESID list goes to stdout (pipe-friendly); the count goes to stderr.

USAGE
=====
    python Resources/list_esids.py MAIN_FOLDER
"""

import sys
from pathlib import Path

import azus_common


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("Usage: python Resources/list_esids.py MAIN_FOLDER")

    folder = Path(sys.argv[1])
    if not folder.is_dir():
        sys.exit(f"Not a folder: {folder}")

    esids = set()
    for entry in folder.iterdir():
        if entry.is_dir():
            padded = azus_common.parse_esid(entry.name)
            if padded is not None:
                esids.add(padded)

    for esid in sorted(esids):
        print(esid)
    print(f"{len(esids)} unique ESID(s)", file=sys.stderr)


if __name__ == "__main__":
    main()
