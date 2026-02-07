#escsp_azus.py
import csv
import os
from pathlib import Path



def update_file_sizes(input_csv="file_list_Template.csv", output_csv="file_list.csv", base_path='.'):
    """
    Read a CSV file, update file sizes based on actual files, and write to a new CSV.
    
    Args:
        input_csv: Path to input CSV file
        output_csv: Path to output CSV file
        base_path: Base directory where files are located (default: current directory)
    """
    
    # Read the CSV file
    with open(input_csv, 'r', encoding='utf-8') as infile:
        reader = csv.DictReader(infile)
        fieldnames = reader.fieldnames
        rows = list(reader)
    
    # Find the column names (case-insensitive search)
    filename_col = None
    filesize_col = None
    
    for field in fieldnames:
        if 'file name' in field.lower():
            filename_col = field
        elif 'file size' in field.lower() and 'kb' in field.lower():
            filesize_col = field
    
    if not filename_col or not filesize_col:
        print(f"Error: Could not find required columns.")
        print(f"Available columns: {fieldnames}")
        return
    
    print(f"Found columns: '{filename_col}' and '{filesize_col}'")
    print(f"\nProcessing files from base path: {os.path.abspath(base_path)}\n")
    
    # Update file sizes
    updated_count = 0
    missing_files = []
    
    for row in rows:
        filename = row[filename_col]
        if filename:
            filepath = os.path.join(base_path, filename)
            
            if os.path.exists(filepath):
                # Get file size in KB
                file_size_bytes = os.path.getsize(filepath)
                file_size_kb = file_size_bytes / 1024
                row[filesize_col] = f"{file_size_kb:.2f}"
                updated_count += 1
                print(f"✓ Updated: {filename} -> {file_size_kb:.2f} KB")
            else:
                missing_files.append(filename)
                print(f"✗ File not found: {filename}")
    
    # Write the updated CSV
    with open(output_csv, 'w', newline='', encoding='utf-8') as outfile:
        writer = csv.DictWriter(outfile, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    
    # Summary
    print(f"\n{'='*60}")
    print(f"Summary:")
    print(f"  Total rows processed: {len(rows)}")
    print(f"  Files updated: {updated_count}")
    print(f"  Files not found: {len(missing_files)}")
    print(f"  Output saved to: {output_csv}")
    print(f"{'='*60}")
    
    if missing_files:
        print(f"\nMissing files:")
        for f in missing_files:
            print(f"  - {f}")
    return


def extract_by_esid(
    input_csv_path,
    output_csv_path,
    esid_value,
    error_log_path="csv_extract_error.txt"
):
    """
    Extract rows from a CSV where ESID == esid_value.

    Parameters:
        input_csv_path (str | Path): Source CSV file
        output_csv_path (str | Path): Destination CSV file
        esid_value (str): ESID value to match
        error_log_path (str | Path): Error log file for missing ESIDs
    """

    input_csv_path = Path(input_csv_path)
    output_csv_path = Path(output_csv_path)
    error_log_path = Path(error_log_path)

    if not input_csv_path.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_csv_path}")

    matched_rows = []

    with input_csv_path.open(newline="", encoding="utf-8") as infile:
        reader = csv.DictReader(infile)

        if "ESID" not in reader.fieldnames:
            raise ValueError("CSV does not contain an 'ESID' column")

        for row in reader:
            if row["ESID"] == esid_value:
                matched_rows.append(row)

        fieldnames = reader.fieldnames

    if matched_rows:
        output_csv_path.parent.mkdir(parents=True, exist_ok=True)

        with output_csv_path.open("w", newline="", encoding="utf-8") as outfile:
            writer = csv.DictWriter(outfile, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(matched_rows)

        print(f"Extracted {len(matched_rows)} row(s) to {output_csv_path}")

    else:
        with error_log_path.open("a", encoding="utf-8") as errfile:
            errfile.write(f"{esid_value}\n")

        print(
            f"No match found for ESID '{esid_value}'. "
            f"Logged to {error_log_path}"
        )



