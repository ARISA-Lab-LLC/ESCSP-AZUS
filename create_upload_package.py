#!/usr/bin/env python3
"""
Create Upload Package ZIP from file_list.csv

This script reads file_list.csv from an ESID staging directory and creates
a ZIP file containing all files listed in it. This creates the final package
ready for upload to Zenodo.

Usage:
    python create_upload_package.py <staging_dir> <esid>
    
Examples:
    python create_upload_package.py /path/to/Staging_Area 005
    python create_upload_package.py . 005
    
What it does:
    1. Reads file_list.csv from ESID_XXX subfolder
    2. Finds all files listed in the CSV
    3. Creates ESID_XXX_Upload_Package.zip
    4. Verifies all files are included

The created ZIP contains everything needed for Zenodo upload.
"""

import os
import sys
import csv
import zipfile
import argparse
from pathlib import Path
from typing import List, Tuple


def read_file_list(file_list_path: Path) -> List[str]:
    """
    Read file_list.csv and extract all filenames.
    
    Args:
        file_list_path: Path to file_list.csv
        
    Returns:
        List of filenames to include in ZIP
    """
    print(f"\n📋 Reading file list from: {file_list_path.name}")
    
    if not file_list_path.exists():
        raise FileNotFoundError(f"file_list.csv not found: {file_list_path}")
    
    filenames = []
    
    with open(file_list_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        
        if 'File Name' not in reader.fieldnames:
            raise ValueError(
                f"file_list.csv missing 'File Name' column. "
                f"Found columns: {reader.fieldnames}"
            )
        
        for row in reader:
            filename = row.get('File Name', '').strip()
            if filename:
                filenames.append(filename)
    
    print(f"   ✅ Found {len(filenames)} files in list")
    
    return filenames


def find_files(esid_dir: Path, filenames: List[str]) -> Tuple[List[Path], List[str]]:
    """
    Find all files from the list in the ESID directory.
    
    Args:
        esid_dir: ESID staging directory
        filenames: List of filenames to find
        
    Returns:
        Tuple of (found_files, missing_files)
    """
    print(f"\n🔍 Searching for files in: {esid_dir}")
    
    found_files = []
    missing_files = []
    
    for filename in filenames:
        file_path = esid_dir / filename
        
        if file_path.exists():
            found_files.append(file_path)
        else:
            missing_files.append(filename)
    
    print(f"   ✅ Found: {len(found_files)} files")
    
    if missing_files:
        print(f"   ⚠️  Missing: {len(missing_files)} files")
    
    return found_files, missing_files


def create_upload_zip(
    files: List[Path],
    output_path: Path,
    esid: str
) -> Path:
    """
    Create ZIP file containing all files.
    
    Args:
        files: List of file paths to include
        output_path: Where to save the ZIP
        esid: ESID number for naming
        
    Returns:
        Path to created ZIP file
    """
    zip_filename = f"ESID_{esid}.zip"
    zip_path = output_path / zip_filename
    
    print(f"\n📦 Creating upload package: {zip_filename}")
    
    # Track what we're adding
    total_size = 0
    file_count = 0
    
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for file_path in files:
            # Add to ZIP with just the filename (no directory structure)
            arcname = file_path.name
            
            zipf.write(file_path, arcname)
            
            file_size = file_path.stat().st_size
            total_size += file_size
            file_count += 1
            
            # Show progress for large batches
            if file_count % 100 == 0:
                print(f"   ... added {file_count} files")
    
    # Final summary
    zip_size_mb = zip_path.stat().st_size / (1024 * 1024)
    total_size_mb = total_size / (1024 * 1024)
    compression_ratio = (1 - (zip_path.stat().st_size / total_size)) * 100 if total_size > 0 else 0
    
    print(f"   ✅ Added {file_count} files")
    print(f"   📊 Total uncompressed: {total_size_mb:.2f} MB")
    print(f"   📊 ZIP file size: {zip_size_mb:.2f} MB")
    print(f"   📊 Compression: {compression_ratio:.1f}%")
    
    return zip_path


def create_upload_manifest(
    filenames: List[str],
    esid: str,
    output_dir: Path,
    zip_filename: str
) -> Path:
    """
    Create ESID_XXX_to_upload.csv manifest file.
    
    This CSV lists all files that should be uploaded to Zenodo:
    - All files from file_list.csv EXCEPT .WAV files
    - The ESID_XXX.zip file
    
    Args:
        filenames: List of all filenames from file_list.csv
        esid: ESID number
        output_dir: Directory where manifest should be saved
        zip_filename: Name of the ZIP file to include
        
    Returns:
        Path to created manifest CSV
    """
    manifest_filename = f"ESID_{esid}_to_upload.csv"
    manifest_path = output_dir / manifest_filename
    
    print(f"\n📋 Creating upload manifest: {manifest_filename}")
    
    # Filter out WAV files and collect files to upload
    files_to_upload = []
    
    # Add the main ZIP file first
    files_to_upload.append(zip_filename)
    
    # Add all non-WAV files from file_list.csv
    wav_count = 0
    for filename in filenames:
        # Skip WAV files
        if filename.lower().endswith('.wav'):
            wav_count += 1
            continue
        
        # Skip the ZIP file if it's already in the list (it shouldn't be, but just in case)
        if filename == zip_filename:
            continue
        
        files_to_upload.append(filename)
    
    # Write manifest CSV
    with open(manifest_path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['File Name'])  # Header
        for filename in files_to_upload:
            writer.writerow([filename])
    
    print(f"   ✅ Created manifest with {len(files_to_upload)} files")
    print(f"   📊 Included: {len(files_to_upload) - 1} metadata files + 1 ZIP")
    print(f"   📊 Excluded: {wav_count} WAV files")
    print(f"   💾 Saved to: {manifest_path}")
    
    return manifest_path


def verify_zip_contents(zip_path: Path, expected_files: List[str]) -> Tuple[List[str], List[str]]:
    """
    Verify ZIP contains all expected files.
    
    Args:
        zip_path: Path to ZIP file
        expected_files: List of filenames that should be in ZIP
        
    Returns:
        Tuple of (files_in_zip, missing_from_zip)
    """
    print(f"\n🔍 Verifying ZIP contents...")
    
    with zipfile.ZipFile(zip_path, 'r') as zipf:
        files_in_zip = zipf.namelist()
    
    # Check what's missing
    expected_set = set(expected_files)
    actual_set = set(files_in_zip)
    
    missing = expected_set - actual_set
    extra = actual_set - expected_set
    
    if not missing and not extra:
        print(f"   ✅ All {len(expected_files)} files present")
        return files_in_zip, []
    
    if missing:
        print(f"   ⚠️  Missing {len(missing)} files from ZIP")
    
    if extra:
        print(f"   ℹ️  Found {len(extra)} extra files in ZIP")
    
    return files_in_zip, list(missing)


def main():
    parser = argparse.ArgumentParser(
        description='Create upload package ZIP from file_list.csv'
    )
    parser.add_argument(
        'staging_dir',
        help='Path to staging directory (or use "." for current directory)'
    )
    parser.add_argument(
        'esid',
        help='ESID number (e.g., 005)'
    )
    parser.add_argument(
        '--verify-only',
        action='store_true',
        help='Only verify files exist, do not create ZIP'
    )
    
    args = parser.parse_args()
    
    # Setup paths
    staging_dir = Path(args.staging_dir).resolve()
    esid = args.esid.strip()
    
    # Ensure ESID has no '#' or 'ESID_' prefix
    esid = esid.replace('#', '').replace('ESID_', '').replace('ESID', '').strip()
    
    # Find ESID subfolder
    esid_dir = staging_dir / f"ESID_{esid}"
    
    if not esid_dir.exists():
        # Try alternate naming
        esid_dir = staging_dir / f"ESID#{esid}"
        
        if not esid_dir.exists():
            print(f"❌ Error: ESID directory not found")
            print(f"   Looked for:")
            print(f"   - {staging_dir / f'ESID_{esid}'}")
            print(f"   - {staging_dir / f'ESID#{esid}'}")
            print(f"\n   Available directories:")
            for d in sorted(staging_dir.iterdir()):
                if d.is_dir() and 'ESID' in d.name:
                    print(f"   - {d.name}")
            sys.exit(1)
    
    print("=" * 70)
    print("CREATE UPLOAD PACKAGE FROM FILE_LIST.CSV")
    print("=" * 70)
    print(f"\nStaging directory: {staging_dir}")
    print(f"ESID directory:    {esid_dir}")
    print(f"ESID:              {esid}")
    
    # Read file_list.csv
    file_list_path = esid_dir / "file_list.csv"
    
    try:
        filenames = read_file_list(file_list_path)
    except Exception as e:
        print(f"\n❌ Error reading file_list.csv: {e}")
        sys.exit(1)
    
    # Find files
    found_files, missing_files = find_files(esid_dir, filenames)
    
    if missing_files:
        print(f"\n⚠️  WARNING: Missing files:")
        for filename in missing_files[:20]:  # Show first 20
            print(f"   ❌ {filename}")
        
        if len(missing_files) > 20:
            print(f"   ... and {len(missing_files) - 20} more")
        
        if not args.verify_only:
            response = input(f"\nContinue anyway? (yes/no): ")
            if response.lower() != 'yes':
                print("\n❌ Cancelled by user")
                sys.exit(1)
    
    if args.verify_only:
        print("\n" + "=" * 70)
        print("VERIFICATION SUMMARY")
        print("=" * 70)
        print(f"Total files in list:  {len(filenames)}")
        print(f"Files found:          {len(found_files)}")
        print(f"Files missing:        {len(missing_files)}")
        
        if missing_files:
            print(f"\n❌ Verification failed - {len(missing_files)} files missing")
            sys.exit(1)
        else:
            print(f"\n✅ Verification passed - all files present")
            sys.exit(0)
    
    # Create ZIP in ESID directory (keep everything organized together)
    try:
        zip_path = create_upload_zip(found_files, esid_dir, esid)
    except Exception as e:
        print(f"\n❌ Error creating ZIP: {e}")
        sys.exit(1)
    
    # Verify ZIP contents
    files_in_zip, missing_from_zip = verify_zip_contents(zip_path, filenames)
    
    # Create upload manifest CSV in same ESID directory
    try:
        manifest_path = create_upload_manifest(
            filenames=filenames,
            esid=esid,
            output_dir=esid_dir,
            zip_filename=zip_path.name
        )
    except Exception as e:
        print(f"\n❌ Error creating upload manifest: {e}")
        sys.exit(1)
    
    # Final summary
    print("\n" + "=" * 70)
    print("✅ UPLOAD PACKAGE CREATED")
    print("=" * 70)
    print(f"\n📦 ZIP file: {zip_path}")
    print(f"📊 Contains: {len(files_in_zip)} files")
    print(f"💾 Size: {zip_path.stat().st_size / (1024 * 1024):.2f} MB")
    
    print(f"\n📋 Upload manifest: {manifest_path}")
    print(f"   This lists all files that will be uploaded to Zenodo")
    
    if missing_files:
        print(f"\n⚠️  Note: {len(missing_files)} files from file_list.csv were not found and not included")
    
    if missing_from_zip:
        print(f"\n⚠️  Warning: {len(missing_from_zip)} files missing from ZIP")
    else:
        print(f"\n✅ All files from file_list.csv are in the ZIP")
    
    print(f"\n📋 Next steps:")
    print(f"   1. Verify ZIP contents:")
    print(f"      unzip -l {esid_dir.name}/{zip_path.name}")
    print(f"   2. Review upload manifest:")
    print(f"      cat {esid_dir.name}/{manifest_path.name}")
    print(f"   3. Update config.json to point to: {esid_dir.parent}")
    print(f"   4. Upload to Zenodo:")
    print(f"      python standalone_upload.py")


if __name__ == "__main__":
    main()
