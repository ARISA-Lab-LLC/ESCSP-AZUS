#!/usr/bin/env python3
"""
Test file discovery for a specific dataset.

This script tests the find_dataset_files() function on a single
dataset to verify all files can be found correctly.

Usage:
    python test_file_discovery.py <path_to_zip_file>

Example:
    python test_file_discovery.py /path/to/ESID_004.zip
"""

import asyncio
import sys
from pathlib import Path

# Add parent directory to path to import AZUS modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from tasks import find_dataset_files


async def main():
    """Test file discovery for a dataset."""
    
    print("=" * 70)
    print("AZUS FILE DISCOVERY TEST")
    print("=" * 70)
    
    # Check command line arguments
    if len(sys.argv) < 2:
        print("\nUsage: python test_file_discovery.py <path_to_zip_file>")
        print("\nExample:")
        print("  python test_file_discovery.py /path/to/ESID_004.zip")
        print("\nThis will search for all required files in the same directory")
        print("as the ZIP file and show you what was found.")
        sys.exit(1)
    
    zip_file = sys.argv[1]
    zip_path = Path(zip_file)
    
    # Verify ZIP file exists
    if not zip_path.exists():
        print(f"\n❌ Error: ZIP file not found: {zip_file}")
        sys.exit(1)
    
    if not zip_path.is_file():
        print(f"\n❌ Error: Not a file: {zip_file}")
        sys.exit(1)
    
    print(f"\n📂 Testing file discovery for:")
    print(f"   ZIP file:  {zip_path.name}")
    print(f"   Directory: {zip_path.parent}")
    
    # Run file discovery
    print(f"\n{'='*70}")
    print("SEARCHING FOR FILES")
    print(f"{'='*70}\n")
    
    try:
        files = await find_dataset_files(str(zip_path))
    except Exception as e:
        print(f"\n❌ Error during file discovery: {e}")
        sys.exit(1)
    
    # Categorize results
    found_files = []
    missing_files = []
    
    for filename, filepath in sorted(files.items()):
        if filepath:
            found_files.append((filename, filepath))
        else:
            missing_files.append(filename)
    
    # Show results
    print("📋 Required Files:")
    print()
    
    if found_files:
        print(f"✅ FOUND ({len(found_files)} files):")
        for filename, filepath in found_files:
            file_size = Path(filepath).stat().st_size / 1024  # KB
            print(f"   ✓ {filename:<45s} ({file_size:>8.1f} KB)")
    
    if missing_files:
        print(f"\n❌ MISSING ({len(missing_files)} files):")
        for filename in missing_files:
            print(f"   ✗ {filename}")
    
    # Summary
    print(f"\n{'='*70}")
    print("SUMMARY")
    print(f"{'='*70}")
    print(f"Found:   {len(found_files)}/{len(files)} files")
    print(f"Missing: {len(missing_files)}/{len(files)} files")
    
    if len(missing_files) == 0:
        print(f"\n✅ SUCCESS: All required files found!")
        print(f"\nThis dataset is ready for upload.")
        sys.exit(0)
    else:
        print(f"\n⚠️  WARNING: {len(missing_files)} file(s) missing")
        print(f"\nMissing files:")
        for filename in missing_files:
            print(f"   • {filename}")
        
        print(f"\n💡 How to generate missing files:")
        
        if "README.html" in missing_files or "README.md" in missing_files:
            print(f"   • README files:")
            print(f"     python readme_test_script.py")
        
        if "file_list.csv" in missing_files:
            print(f"   • file_list.csv:")
            print(f"     python file_list_test_script.py")
        
        print(f"\n   • Other files should be part of your dataset preparation workflow")
        
        print(f"\nDataset is NOT ready for upload until all files are present.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
