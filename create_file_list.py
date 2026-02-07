from __future__ import annotations

import csv
import glob
import os
from pathlib import Path
from typing import Callable, Dict, List, Optional


def create_file_list(
    directory_path: str | os.PathLike,
    template_csv_path: str | os.PathLike,
    output_csv_path: str | os.PathLike = "file_list.csv",
    *,
    calculate_sha512: Callable[[str], str],
) -> Path:
    """
    Create a new CSV ("file_list.csv" by default) using the headers from a template CSV.
    For each non-header row in the template, treat the 'File Name' cell as a glob pattern,
    find all matching files under `directory_path`, compute each file's size (KB) and SHA-512,
    and append a populated row to the output CSV.

    Requirements implemented:
      1) Create output CSV with same headers as template.
      2) For each template row, glob-match files using pattern in 'File Name'.
      3) Compute file size and SHA-512 via provided calculate_sha512(file_path).
      4) Append row: File Name becomes basename+extension, copy other template values,
         replace 'File size (KB)' and 'SHA-512 Hash'.

    Notes:
      - Uses KB = bytes / 1024 (rounded to 2 decimals).
      - Globbing is performed relative to `directory_path`. If the pattern includes subdirs,
        it will work (e.g., "data/**/*.parquet" with recursive patterns).
      - If no files match a template row, no rows are appended for that template row.

    Parameters
    ----------
    directory_path:
        Base directory in which to search for files.
    template_csv_path:
        Path to the template CSV (must include headers).
    output_csv_path:
        Path for the newly created output CSV. Default: "file_list.csv"
    calculate_sha512:
        Existing function that accepts a file path (string) and returns SHA-512 hex digest.

    Returns
    -------
    Path
        Path to the created output CSV.
    """
    base_dir = Path(directory_path).expanduser().resolve()
    template_path = Path(template_csv_path).expanduser().resolve()
    out_path = Path(output_csv_path).expanduser()

    if not base_dir.exists() or not base_dir.is_dir():
        raise NotADirectoryError(f"directory_path is not a valid directory: {base_dir}")

    if not template_path.exists() or not template_path.is_file():
        raise FileNotFoundError(f"template_csv_path not found: {template_path}")

    # Read template rows
    with template_path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError("Template CSV has no headers / fieldnames.")

        headers: List[str] = list(reader.fieldnames)

        # Find required columns (case-sensitive by default)
        required_cols = ["File Name", "File size (KB)", "SHA-512 Hash"]
        missing = [c for c in required_cols if c not in headers]
        if missing:
            raise ValueError(
                f"Template CSV is missing required column(s): {missing}. "
                f"Found headers: {headers}"
            )

        template_rows: List[Dict[str, str]] = list(reader)

    # Create output CSV with same headers
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as out_f:
        writer = csv.DictWriter(out_f, fieldnames=headers)
        writer.writeheader()

        # Process each template row
        for trow in template_rows:
            pattern = (trow.get("File Name") or "").strip()
            if not pattern:
                continue

            # Glob relative to base_dir; allow recursive patterns (**)
            glob_pattern = str(base_dir / pattern)
            matches = glob.glob(glob_pattern, recursive=True)

            # Only keep files
            matches = [m for m in matches if Path(m).is_file()]

            for file_path_str in matches:
                file_path = Path(file_path_str)

                # File size in KB
                size_bytes = file_path.stat().st_size
                size_kb = round(size_bytes / 1024.0, 2)

                # SHA-512 hash
                sha512_hex = calculate_sha512(str(file_path))

                # Build output row: copy template row, then overwrite required fields
                out_row = dict(trow)

                # Replace File Name with basename + extension (i.e., filename)
                out_row["File Name"] = file_path.name

                # Replace size/hash columns
                out_row["File size (KB)"] = str(size_kb)
                out_row["SHA-512 Hash"] = sha512_hex

                # Ensure all headers exist in row dict (DictWriter requires keys)
                for h in headers:
                    out_row.setdefault(h, "")

                writer.writerow(out_row)

    return out_path
