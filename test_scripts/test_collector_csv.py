#!/usr/bin/env python3
"""
Test collector CSV file parsing.

This script tests that the collectors CSV file can be parsed correctly
and contains valid data for all ESIDs.

Usage:
    python test_collector_csv.py <path_to_csv> [eclipse_type]

Arguments:
    path_to_csv    Path to the collectors CSV file
    eclipse_type   'total' or 'annular' (default: total)

Example:
    python test_collector_csv.py /path/to/2024_total_info_updated.csv total
    python test_collector_csv.py /path/to/2023_annular_info.csv annular
"""

import asyncio
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from tasks import parse_collectors_csv
from models.audiomoth import EclipseType


async def main():
    """Test collector CSV parsing."""
    
    print("=" * 70)
    print("AZUS COLLECTOR CSV TEST")
    print("=" * 70)
    
    # Check command line arguments
    if len(sys.argv) < 2:
        print("\nUsage: python test_collector_csv.py <path_to_csv> [eclipse_type]")
        print("\nArguments:")
        print("  path_to_csv    Path to the collectors CSV file")
        print("  eclipse_type   'total' or 'annular' (default: total)")
        print("\nExample:")
        print("  python test_collector_csv.py /path/to/2024_total_info_updated.csv total")
        sys.exit(1)
    
    csv_file = sys.argv[1]
    eclipse_type_str = sys.argv[2] if len(sys.argv) > 2 else "total"
    
    # Parse eclipse type
    if eclipse_type_str.lower() == "total":
        eclipse_type = EclipseType.TOTAL
    elif eclipse_type_str.lower() == "annular":
        eclipse_type = EclipseType.ANNULAR
    else:
        print(f"\n❌ Error: Invalid eclipse type '{eclipse_type_str}'")
        print(f"   Must be 'total' or 'annular'")
        sys.exit(1)
    
    csv_path = Path(csv_file)
    
    # Verify file exists
    if not csv_path.exists():
        print(f"\n❌ Error: CSV file not found: {csv_file}")
        sys.exit(1)
    
    if not csv_path.is_file():
        print(f"\n❌ Error: Not a file: {csv_file}")
        sys.exit(1)
    
    print(f"\n📋 Testing collector CSV:")
    print(f"   File:         {csv_path.name}")
    print(f"   Eclipse type: {eclipse_type.value}")
    
    # Parse the CSV
    print(f"\n{'='*70}")
    print("PARSING CSV FILE")
    print(f"{'='*70}\n")
    
    try:
        collectors = await parse_collectors_csv(
            csv_file_path=str(csv_path),
            eclipse_type=eclipse_type
        )
        print(f"✅ Successfully parsed CSV file")
        print(f"   Found {len(collectors)} collector record(s)")
    except FileNotFoundError as e:
        print(f"❌ Error: {e}")
        sys.exit(1)
    except ValueError as e:
        print(f"❌ Error: CSV validation failed")
        print(f"   {e}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)
    
    if len(collectors) == 0:
        print(f"\n⚠️  Warning: No collectors found in CSV")
        print(f"   Check that the CSV file has data rows")
        sys.exit(1)
    
    # Analyze collectors
    print(f"\n{'='*70}")
    print("COLLECTOR DATA ANALYSIS")
    print(f"{'='*70}\n")
    
    # Show first few collectors
    print(f"📊 Sample Collectors (first 5):")
    for i, collector in enumerate(collectors[:5], 1):
        print(f"\n   {i}. ESID {collector.esid}:")
        print(f"      Location:  {collector.latitude}, {collector.longitude}")
        print(f"      Coverage:  {collector.eclipse_coverage}%")
        print(f"      Eclipse:   {collector.eclipse_date}")
        print(f"      Type:      {collector.eclipse_type}")
    
    if len(collectors) > 5:
        print(f"\n   ... and {len(collectors) - 5} more collector(s)")
    
    # Check for duplicates
    print(f"\n🔍 Checking for duplicate ESIDs...")
    esids = [c.esid for c in collectors]
    unique_esids = set(esids)
    
    if len(esids) == len(unique_esids):
        print(f"   ✅ No duplicates found")
    else:
        duplicates = [esid for esid in unique_esids if esids.count(esid) > 1]
        print(f"   ⚠️  Found {len(duplicates)} duplicate ESID(s):")
        for esid in duplicates:
            count = esids.count(esid)
            print(f"      • ESID {esid} appears {count} times")
    
    # Check for missing required fields
    print(f"\n🔍 Checking required fields...")
    issues = []
    
    for collector in collectors:
        # Check ESID
        if not collector.esid or not collector.esid.strip():
            issues.append(f"Missing ESID for a record")
        
        # Check coordinates
        try:
            lat = float(collector.latitude)
            lon = float(collector.longitude)
            if not (-90 <= lat <= 90):
                issues.append(f"ESID {collector.esid}: Invalid latitude {lat}")
            if not (-180 <= lon <= 180):
                issues.append(f"ESID {collector.esid}: Invalid longitude {lon}")
        except (ValueError, TypeError):
            issues.append(f"ESID {collector.esid}: Invalid coordinates")
        
        # Check eclipse coverage
        try:
            coverage = float(collector.eclipse_coverage)
            if not (0 <= coverage <= 100):
                issues.append(f"ESID {collector.esid}: Invalid coverage {coverage}%")
        except (ValueError, TypeError):
            issues.append(f"ESID {collector.esid}: Invalid coverage value")
    
    if issues:
        print(f"   ⚠️  Found {len(issues)} issue(s):")
        for issue in issues[:10]:  # Show first 10
            print(f"      • {issue}")
        if len(issues) > 10:
            print(f"      ... and {len(issues) - 10} more")
    else:
        print(f"   ✅ All required fields look valid")
    
    # Summary
    print(f"\n{'='*70}")
    print("SUMMARY")
    print(f"{'='*70}")
    print(f"\n📊 Statistics:")
    print(f"   Total collectors: {len(collectors)}")
    print(f"   Unique ESIDs:     {len(unique_esids)}")
    print(f"   Duplicates:       {len(esids) - len(unique_esids)}")
    print(f"   Data issues:      {len(issues)}")
    
    # List all ESIDs
    print(f"\n📋 All ESIDs in this file:")
    esid_list = sorted(set(esids))
    
    # Print in columns
    cols = 5
    for i in range(0, len(esid_list), cols):
        row_esids = esid_list[i:i+cols]
        print(f"   {', '.join(row_esids)}")
    
    if len(issues) > 0:
        print(f"\n⚠️  WARNING: CSV has {len(issues)} data issue(s)")
        print(f"   Review the issues above and correct the CSV file")
        sys.exit(1)
    else:
        print(f"\n✅ SUCCESS: CSV file is valid and ready to use!")
        print(f"\nYou can proceed with uploads using this CSV file.")
        sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
