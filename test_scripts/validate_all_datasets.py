#!/usr/bin/env python3
"""
Validate that all datasets have required files.

This script checks each dataset directory to ensure all required files
are present before attempting to upload to Zenodo.

Usage:
    python validate_all_datasets.py <dataset_directory>

Example:
    python validate_all_datasets.py /path/to/total_eclipse_datasets
"""

from pathlib import Path
import sys
import os


def validate_dataset(dataset_dir: Path) -> tuple[bool, list[str]]:
    """
    Check if dataset has all required files.
    
    Args:
        dataset_dir: Path to the dataset directory
        
    Returns:
        Tuple of (is_valid, missing_files)
        - is_valid: True if all required files are present
        - missing_files: List of missing filenames
    """
    # Required files for ESCSP/AudioMoth datasets
    required = [
        "README.html",
        "README.md",
        "2024_total_eclipse_data_data_dict.csv",
        "AudioMoth_Operation_Manual.pdf",
        "CONFIG.TXT",
        "CONFIG_data_dict.csv",
        "License.txt",
        "WAV_data_dict.csv",
        "file_list.csv",
        "file_list_data_dict.csv",
        "total_eclipse_data.csv",
    ]
    
    missing = []
    
    # Check each required file
    for filename in required:
        if not os.path.exists(os.path.join(dataset_dir, filename)):
            missing.append(filename)
    
    # Check for ZIP file (any ESID)
    zip_files = list(dataset_dir.glob("*.zip"))
    if not zip_files:
        missing.append("*.zip")
    
    return len(missing) == 0, missing


def main():
    """Main validation function."""
    
    # Check command line arguments
    if len(sys.argv) < 2:
        print("Usage: python validate_all_datasets.py <dataset_directory>")
        print("\nExample:")
        print("  python validate_all_datasets.py /path/to/total_eclipse_datasets")
        sys.exit(1)
    
    dataset_dir = Path(sys.argv[1])
    
    # Verify directory exists
    if not dataset_dir.exists():
        print(f"❌ Error: Directory not found: {dataset_dir}")
        sys.exit(1)
    
    if not dataset_dir.is_dir():
        print(f"❌ Error: Not a directory: {dataset_dir}")
        sys.exit(1)
    
    # Find all directories with ZIP files
    print(f"Scanning {dataset_dir} for datasets...")
    dataset_dirs = set()
    for zip_file in dataset_dir.rglob("*.zip"):
        dataset_dirs.add(zip_file.parent)
    
    if not dataset_dirs:
        print(f"\n❌ No ZIP files found in {dataset_dir}")
        print("Make sure your datasets contain .zip files")
        sys.exit(1)
    
    print(f"Found {len(dataset_dirs)} dataset(s) with ZIP files\n")
    print(f"{'='*70}")
    print("VALIDATING DATASETS")
    print(f"{'='*70}\n")
    
    # Validate each dataset
    valid = 0
    invalid = 0
    invalid_datasets = []
    
    for dir_path in sorted(dataset_dirs):
        is_valid, missing = validate_dataset(dir_path)
        
        # Show relative path if possible
        try:
            display_path = dir_path.relative_to(dataset_dir)
        except ValueError:
            display_path = dir_path.name
        
        if is_valid:
            print(f"✅ VALID:   {display_path}")
            valid += 1
        else:
            print(f"❌ INVALID: {display_path}")
            print(f"   Missing ({len(missing)}): {', '.join(missing[:3])}", end="")
            if len(missing) > 3:
                print(f" and {len(missing) - 3} more...")
            else:
                print()
            invalid += 1
            invalid_datasets.append((str(display_path), missing))
    
    # Print summary
    print(f"\n{'='*70}")
    print("VALIDATION SUMMARY")
    print(f"{'='*70}")
    print(f"Valid datasets:   {valid}")
    print(f"Invalid datasets: {invalid}")
    print(f"Total:            {valid + invalid}")
    
    # Show details of invalid datasets
    if invalid_datasets:
        print(f"\n{'='*70}")
        print(f"⚠️  WARNING: {invalid} dataset(s) are missing required files!")
        print(f"{'='*70}")
        print("\nFix these before uploading:\n")
        
        for name, missing in invalid_datasets:
            print(f"📁 {name}:")
            for file in missing:
                print(f"   ❌ {file}")
            print()
        
        print("💡 Tip: Generate missing files using:")
        print("   - README files: python readme_test_script.py")
        print("   - file_list.csv: python file_list_test_script.py")
        print("   - Other files: Check your dataset preparation workflow")
        
        sys.exit(1)
    else:
        print(f"\n{'='*70}")
        print("✅ SUCCESS: All datasets are valid and ready for upload!")
        print(f"{'='*70}")
        print(f"\nNext steps:")
        print(f"  1. Test upload one dataset: python test_single_upload.py")
        print(f"  2. If test succeeds, run batch upload via Prefect")
        
        sys.exit(0)


if __name__ == "__main__":
    main()
