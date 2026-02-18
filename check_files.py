#!/usr/bin/env python3
"""
Verify AZUS files are up to date.

This script checks if you have the fixed versions of the files.
"""

import sys
from pathlib import Path


def check_file(filepath, search_string, should_exist=True):
    """Check if a file contains (or doesn't contain) a specific string."""
    path = Path(filepath)
    
    if not path.exists():
        print(f"❌ File not found: {filepath}")
        return False
    
    content = path.read_text()
    contains = search_string in content
    
    if should_exist and contains:
        print(f"✅ CORRECT: {path.name}")
        return True
    elif not should_exist and not contains:
        print(f"✅ CORRECT: {path.name}")
        return True
    elif should_exist and not contains:
        print(f"❌ OLD VERSION: {path.name} - needs update")
        return False
    else:  # not should_exist but contains
        print(f"❌ OLD VERSION: {path.name} - needs update")
        return False


def main():
    """Check all critical files."""
    print("=" * 70)
    print("AZUS FILE VERSION CHECK")
    print("=" * 70)
    print()
    
    all_good = True
    
    # Check audiomoth.py - should have string comparison
    print("Checking audiomoth.py...")
    result = check_file(
        "audiomoth.py",
        'if self.eclipse_type == "Total"',
        should_exist=True
    )
    if not result:
        print("   ❌ Still has: if self.eclipse_type == EclipseType.TOTAL")
        print("   ✅ Should have: if self.eclipse_type == \"Total\"")
        all_good = False
    print()
    
    # Check standalone_tasks.py - should have string comparison
    print("Checking standalone_tasks.py...")
    result = check_file(
        "standalone_tasks.py",
        'if data_collector.eclipse_type == "Total"',
        should_exist=True
    )
    if not result:
        print("   ❌ Still has: if data_collector.eclipse_type == EclipseType.TOTAL")
        print("   ✅ Should have: if data_collector.eclipse_type == \"Total\"")
        all_good = False
    print()
    
    # Check standalone_tasks.py - should NOT extract ZIP
    print("Checking standalone_tasks.py (temp directory fix)...")
    result = check_file(
        "standalone_tasks.py",
        "zip_ref.extractall",
        should_exist=False
    )
    if not result:
        print("   ❌ Still extracts ZIP to temp directory")
        print("   ✅ Should just read filenames without extraction")
        all_good = False
    print()
    
    # Summary
    print("=" * 70)
    if all_good:
        print("✅ ALL FILES ARE UP TO DATE!")
        print("=" * 70)
        print("\nYou can proceed with upload:")
        print("  python standalone_upload.py")
        return 0
    else:
        print("❌ SOME FILES NEED TO BE UPDATED")
        print("=" * 70)
        print("\nFiles to replace:")
        print("  1. audiomoth.py")
        print("  2. standalone_tasks.py")
        print("\nDownload the fixed versions and replace your current files.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
