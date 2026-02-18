from __future__ import annotations
#azus.py
import json
from pathlib import Path
from typing import Any, Dict, Literal, Tuple
import hashlib
import os
from pathlib import Path
import glob
import csv

#for create_file_list
#from __future__ import annotations
from typing import Callable, Dict, List, Optional
#


def get_dataset_configs(dataset_type: Literal["annular", "total"]) -> Tuple[str, str]:
    """
    Parses a dictionary for the `dataset_dir` and `collectors_csv` key values.

    Args:
        config (Dict[str, Any]): A config dictionary.

    Returns:
        Tuple[str, str]: The dataset directory and collectors CSV file path.
    """

    current_dir = Path(__file__).parent
    config_path = current_dir / "config.json"

    with open(config_path, "r", encoding="utf-8") as file:
        config_data = json.load(file)

        if "uploads" not in config_data:
            raise ValueError(
                "Running deployment without a valid configuration for uploads"
            )

        uploads_config: Dict[str, Any] = config_data["uploads"]

        dataset_dir = ""
        collectors_csv = ""

        if dataset_type in uploads_config:
            if "dataset_dir" in uploads_config[dataset_type]:
                dataset_dir = uploads_config[dataset_type]["dataset_dir"]

            if "collectors_csv" in uploads_config[dataset_type]:
                collectors_csv = uploads_config[dataset_type]["collectors_csv"]

        return (dataset_dir, collectors_csv)
    
def calculate_sha512(filepath):
    """Calculate SHA-512 hash of a file."""
    sha512_hash = hashlib.sha512()
    
    try:
        with open(filepath, "rb") as f:
            # Read file in chunks to handle large files efficiently
            for chunk in iter(lambda: f.read(4096), b""):
                sha512_hash.update(chunk)
        return sha512_hash.hexdigest()
    except Exception as e:
        return f"Error: {str(e)}"

    return sha512_hash.hexdigest()



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

    print(base_dir)
    print(out_path)
    append_wav_files_with_sequence(directory_path=base_dir,                  # directory to scan
                                    existing_csv_path=out_path,    # MUST already exist
                                    recursive=False)

    return out_path



def append_wav_files_with_sequence(
    directory_path: str | Path,
    existing_csv_path: str | Path,
    *,
    recursive: bool = True,
) -> Path:
    """
    Find all .WAV files (case-insensitive) under `directory_path` and append
    rows to an EXISTING CSV spreadsheet.

    For each WAV file found, append 10 rows where:
      - File Name = basename.ext
      - Notes = 
      - All other columns are left unchanged/blank

    The CSV must already exist and contain the expected headers.

    Parameters
    ----------
    directory_path : str | Path
        Directory to search for .WAV files.
    existing_csv_path : str | Path
        Path to an existing CSV file to append to.
    recursive : bool
        If True, search subdirectories recursively.

    Returns
    -------
    Path
        Path to the updated CSV file.
    """
    HEADERS = [
        "File Name",
        "File Type",
        "Description",
        "File size (KB)",
        "Associated Data Dictionary",
        "SHA-512 Hash",
        "Notes",
        ]

    base_dir = directory_path
    csv_path =existing_csv_path

    if not base_dir.is_dir():
        raise NotADirectoryError(f"Not a directory: {base_dir}")

    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file does not exist: {csv_path}")

    # Find WAV files (case-insensitive)
    pattern = "*.WAV" if recursive else "*"
    wav_files = [
        p for p in base_dir.glob(pattern)
        if p.is_file() and p.suffix.lower() == ".wav"
    ]
    print(wav_files)

    # Append rows
    #newline="",
    with csv_path.open("a",  encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=HEADERS)

        writer.writerow(
            {"File Name": "file_list.csv",
             "File Type": "Comma Separated Variable (.CSV)",
             "Description": "A machine and human file that gives the following information on each file in the record: File Name, File Type, Description, File Size in kilobytes, Name of Associated Data Dictionary with the file, calculated SHA-512 Hash of the file as a unique identifier to insure data integrity during transfer and compression.",
             "File size (KB)":  "N/A",
             "Associated Data Dictionary": "file_list_data_dict.csv",
             "SHA-512 Hash": "N/A",
             "Notes":  "File cannot include its own hash function or file sizeas its inclusion would then change the hash." }) 

        for wav in sorted(wav_files):
        
            path2file=os.path.join(base_dir, wav.name)  
            writer.writerow(
                {
                    "File Name": wav.name,  # basename.ext
                    "File Type": "Waveform Audio File Format (.WAV)",
                    "Description": "A WAV formatted file generated, machine readable by the AudiMoth device containing the audio data recordings at a site.  The recording start  time is stamped into the filename using a YYYYMMDD_HHMMSS format, where: YYYY is the four digit year, MM is the two digit month, DD is the two digit date, hh is the two digit hour, mm is the two digit minutes, ss is the two digit seconds.",
                    "File size (KB)": str(os.path.getsize(path2file) / 1024.0),
                    "Associated Data Dictionary": "WAV_data_dict.csv",
                    "SHA-512 Hash": calculate_sha512(path2file),
                    "Notes": " ",
                }
            )
        

    return csv_path




