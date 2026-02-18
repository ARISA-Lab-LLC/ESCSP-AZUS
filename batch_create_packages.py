#!/usr/bin/env python3
"""
Batch Create Upload Packages

Process multiple ESID directories at once, creating upload packages
for each one based on their file_list.csv.

Usage:
    python batch_create_packages.py <staging_dir> [esid1 esid2 ...]
    
Examples:
    # Process all ESIDs in staging directory
    python batch_create_packages.py /path/to/Staging_Area
    
    # Process specific ESIDs
    python batch_create_packages.py /path/to/Staging_Area 005 006 007
    
    # Process from current directory
    python batch_create_packages.py . 005 006 007
"""

import os
import sys
import subprocess
from pathlib import Path
from typing import List


def find_esid_directories(staging_dir: Path) -> List[str]:
    """
    Find all ESID directories in staging area.
    
    Args:
        staging_dir: Path to staging directory
        
    Returns:
        List of ESID numbers found
    """
    esids = []
    
    for item in staging_dir.iterdir():
        if item.is_dir():
            # Match ESID_XXX or ESID#XXX pattern
            name = item.name
            if name.startswith('ESID_') or name.startswith('ESID#'):
                # Extract number
                esid = name.replace('ESID_', '').replace('ESID#', '').split('_')[0]
                
                # Verify it has file_list.csv
                if (item / 'file_list.csv').exists():
                    esids.append(esid)
    
    return sorted(esids)


def process_esid(staging_dir: Path, esid: str, script_path: Path) -> bool:
    """
    Process a single ESID.
    
    Args:
        staging_dir: Staging directory
        esid: ESID number
        script_path: Path to create_upload_package.py
        
    Returns:
        True if successful, False otherwise
    """
    print(f"\n{'='*70}")
    print(f"Processing ESID {esid}")
    print(f"{'='*70}")
    
    try:
        result = subprocess.run(
            ['python3', str(script_path), str(staging_dir), esid],
            check=True,
            capture_output=False,
            text=True
        )
        
        return result.returncode == 0
        
    except subprocess.CalledProcessError as e:
        print(f"\n❌ Failed to process ESID {esid}")
        return False
    except Exception as e:
        print(f"\n❌ Error processing ESID {esid}: {e}")
        return False


def main():
    if len(sys.argv) < 2:
        print("Usage: python batch_create_packages.py <staging_dir> [esid1 esid2 ...]")
        print("\nExamples:")
        print("  # Process all ESIDs")
        print("  python batch_create_packages.py /path/to/Staging_Area")
        print("\n  # Process specific ESIDs")
        print("  python batch_create_packages.py /path/to/Staging_Area 005 006 007")
        sys.exit(1)
    
    staging_dir = Path(sys.argv[1]).resolve()
    
    if not staging_dir.exists():
        print(f"❌ Error: Staging directory not found: {staging_dir}")
        sys.exit(1)
    
    # Find script
    script_path = Path(__file__).parent / 'create_upload_package.py'
    
    if not script_path.exists():
        print(f"❌ Error: create_upload_package.py not found at: {script_path}")
        sys.exit(1)
    
    print("=" * 70)
    print("BATCH CREATE UPLOAD PACKAGES")
    print("=" * 70)
    print(f"\nStaging directory: {staging_dir}")
    print(f"Script location:   {script_path}")
    
    # Determine which ESIDs to process
    if len(sys.argv) > 2:
        # Process specified ESIDs
        esids = [esid.strip() for esid in sys.argv[2:]]
        print(f"\nProcessing specified ESIDs: {', '.join(esids)}")
    else:
        # Find all ESIDs
        print(f"\nScanning for ESID directories...")
        esids = find_esid_directories(staging_dir)
        
        if not esids:
            print(f"\n❌ No ESID directories found in {staging_dir}")
            print(f"\nLooking for directories matching:")
            print(f"  - ESID_XXX/ with file_list.csv")
            print(f"  - ESID#XXX/ with file_list.csv")
            sys.exit(1)
        
        print(f"\nFound {len(esids)} ESID(s): {', '.join(esids)}")
    
    # Confirm
    response = input(f"\nProcess {len(esids)} ESID(s)? (yes/no): ")
    
    if response.lower() != 'yes':
        print("\n❌ Cancelled by user")
        sys.exit(0)
    
    # Process each ESID
    results = {}
    
    for i, esid in enumerate(esids, 1):
        print(f"\n\n{'#'*70}")
        print(f"# Processing {i}/{len(esids)}: ESID {esid}")
        print(f"{'#'*70}")
        
        success = process_esid(staging_dir, esid, script_path)
        results[esid] = success
    
    # Summary
    print("\n\n" + "=" * 70)
    print("BATCH PROCESSING SUMMARY")
    print("=" * 70)
    
    successful = [esid for esid, success in results.items() if success]
    failed = [esid for esid, success in results.items() if not success]
    
    print(f"\nTotal processed:  {len(esids)}")
    print(f"✅ Successful:    {len(successful)}")
    print(f"❌ Failed:        {len(failed)}")
    
    if successful:
        print(f"\n✅ Successful ESIDs:")
        for esid in successful:
            print(f"   - ESID {esid}")
    
    if failed:
        print(f"\n❌ Failed ESIDs:")
        for esid in failed:
            print(f"   - ESID {esid}")
        print(f"\nReview errors above and retry failed ESIDs")
    
    # List created packages
    print(f"\n📦 Upload packages created:")
    for esid in successful:
        esid_dir = staging_dir / f"ESID_{esid}"
        if not esid_dir.exists():
            esid_dir = staging_dir / f"ESID#{esid}"
        
        zip_file = esid_dir / f"ESID_{esid}_Upload_Package.zip"
        if zip_file.exists():
            size_mb = zip_file.stat().st_size / (1024 * 1024)
            print(f"   ✅ {zip_file.name:<40s} ({size_mb:>8.2f} MB)")
    
    print(f"\n📋 Next steps:")
    print(f"   1. Review created packages")
    print(f"   2. Update config.json")
    print(f"   3. Upload to Zenodo:")
    print(f"      python standalone_upload.py")
    
    sys.exit(0 if len(failed) == 0 else 1)


if __name__ == "__main__":
    main()
