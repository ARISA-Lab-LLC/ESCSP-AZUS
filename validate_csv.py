#!/usr/bin/env python3
"""
CSV Validation and Repair Tool for AZUS

This script checks your collector CSV for common data issues and can
automatically fix them.

Usage:
    python validate_csv.py <csv_file> [--fix] [--eclipse-type total|annular]

Examples:
    # Check for issues
    python validate_csv.py collectors.csv
    
    # Check and auto-fix issues
    python validate_csv.py collectors.csv --fix
    
    # Specify eclipse type for validation
    python validate_csv.py collectors.csv --eclipse-type total
"""

import csv
import sys
import argparse
from pathlib import Path
from typing import List, Dict, Tuple


def validate_csv(csv_path: str, eclipse_type: str = "total") -> Tuple[List[str], List[Dict]]:
    """
    Validate CSV file and return issues found.
    
    Returns:
        Tuple of (issues, rows_with_problems)
    """
    issues = []
    problem_rows = []
    
    # Required headers
    required_headers = [
        "ESID",
        "Data Collector Affiliations",
        "Latitude",
        "Longitude",
        "Local Eclipse Type",
        "Eclipse Percent (%)",
        "WAV Files Time & Date Settings",
        "Eclipse Date",
        "Eclipse Start Time (UTC) (1st Contact)",
        "Eclipse Maximum (UTC)",
        "Eclipse End Time (UTC) (4th Contact)",
        "Version",
        "Keywords and subjects",
    ]
    
    if eclipse_type.lower() == "total":
        required_headers.extend([
            "Totality Start Time (UTC) (2nd Contact)",
            "Totality End Time (UTC) (3rd Contact)"
        ])
    
    print(f"🔍 Validating CSV: {csv_path}")
    print(f"   Eclipse type: {eclipse_type}")
    print()
    
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames
        
        # Check headers
        print("📋 Checking headers...")
        missing_headers = set(required_headers) - set(headers)
        if missing_headers:
            issues.append(f"Missing required headers: {missing_headers}")
            print(f"   ❌ Missing headers: {missing_headers}")
        else:
            print(f"   ✅ All required headers present")
        
        # Check each row
        print("\n📊 Checking data rows...")
        row_num = 1
        rows = list(reader)
        
        for row in rows:
            row_num += 1
            row_issues = []
            
            # Check ESID
            if not row.get("ESID", "").strip():
                row_issues.append("ESID is empty")
            
            # Check Local Eclipse Type (THIS IS THE MAIN ISSUE)
            eclipse_type_value = row.get("Local Eclipse Type", "").strip()
            if not eclipse_type_value:
                row_issues.append("Local Eclipse Type is empty")
            elif eclipse_type_value not in ["Annular", "Total", "Partial"]:
                row_issues.append(f"Invalid Local Eclipse Type: '{eclipse_type_value}' (must be 'Annular', 'Total', or 'Partial')")
            
            # Check coordinates
            try:
                lat = float(row.get("Latitude", "0"))
                if not (-90 <= lat <= 90):
                    row_issues.append(f"Invalid latitude: {lat}")
            except ValueError:
                row_issues.append(f"Invalid latitude: '{row.get('Latitude')}'")
            
            try:
                lon = float(row.get("Longitude", "0"))
                if not (-180 <= lon <= 180):
                    row_issues.append(f"Invalid longitude: {lon}")
            except ValueError:
                row_issues.append(f"Invalid longitude: '{row.get('Longitude')}'")
            
            # Check Eclipse Percent
            try:
                coverage = float(row.get("Eclipse Percent (%)", "0"))
                if not (0 <= coverage <= 100):
                    row_issues.append(f"Invalid coverage: {coverage}%")
            except ValueError:
                row_issues.append(f"Invalid coverage: '{row.get('Eclipse Percent (%)')}'")
            
            # Check required text fields
            required_fields = [
                "Data Collector Affiliations",
                "WAV Files Time & Date Settings",
                "Eclipse Date",
                "Eclipse Start Time (UTC) (1st Contact)",
                "Eclipse Maximum (UTC)",
                "Version",
            ]
            
            for field in required_fields:
                if not row.get(field, "").strip():
                    row_issues.append(f"{field} is empty")
            
            if row_issues:
                esid = row.get("ESID", f"Row {row_num}")
                issues.append(f"Row {row_num} (ESID {esid}): {', '.join(row_issues)}")
                problem_rows.append({
                    'row_num': row_num,
                    'esid': row.get("ESID", ""),
                    'issues': row_issues,
                    'data': row
                })
    
    return issues, problem_rows


def fix_csv(csv_path: str, eclipse_type: str = "total") -> None:
    """
    Automatically fix common CSV issues.
    """
    backup_path = csv_path + ".backup"
    
    print(f"💾 Creating backup: {backup_path}")
    
    # Read original
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames
        rows = list(reader)
    
    # Create backup
    with open(backup_path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)
    
    print(f"✅ Backup created")
    print(f"\n🔧 Fixing issues...")
    
    fixed_count = 0
    
    for i, row in enumerate(rows, 1):
        # Fix empty Local Eclipse Type based on eclipse_type parameter
        if not row.get("Local Eclipse Type", "").strip():
            if eclipse_type.lower() == "total":
                row["Local Eclipse Type"] = "Total"
            else:
                row["Local Eclipse Type"] = "Annular"
            print(f"   Row {i+1}: Set Local Eclipse Type to '{row['Local Eclipse Type']}'")
            fixed_count += 1
        
        # Fix common typos/variations in Local Eclipse Type
        eclipse_value = row.get("Local Eclipse Type", "").strip()
        if eclipse_value.lower() == "total solar eclipse":
            row["Local Eclipse Type"] = "Total"
            print(f"   Row {i+1}: Normalized 'Total Solar Eclipse' → 'Total'")
            fixed_count += 1
        elif eclipse_value.lower() == "annular solar eclipse":
            row["Local Eclipse Type"] = "Annular"
            print(f"   Row {i+1}: Normalized 'Annular Solar Eclipse' → 'Annular'")
            fixed_count += 1
        elif eclipse_value.lower() == "partial solar eclipse":
            row["Local Eclipse Type"] = "Partial"
            print(f"   Row {i+1}: Normalized 'Partial Solar Eclipse' → 'Partial'")
            fixed_count += 1
        
        # Set default values for optional fields if empty
        if eclipse_type.lower() == "total":
            if not row.get("Totality Start Time (UTC) (2nd Contact)", "").strip():
                row["Totality Start Time (UTC) (2nd Contact)"] = "N/A"
            
            if not row.get("Totality End Time (UTC) (3rd Contact)", "").strip():
                row["Totality End Time (UTC) (3rd Contact)"] = "N/A"
        
        if not row.get("Eclipse End Time (UTC) (4th Contact)", "").strip():
            row["Eclipse End Time (UTC) (4th Contact)"] = "N/A"
    
    # Write fixed version
    with open(csv_path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)
    
    print(f"\n✅ Fixed {fixed_count} issue(s)")
    print(f"💾 Updated file: {csv_path}")
    print(f"📋 Original saved as: {backup_path}")


def main():
    parser = argparse.ArgumentParser(
        description='Validate and optionally fix AZUS collector CSV files'
    )
    parser.add_argument(
        'csv_file',
        help='Path to the collector CSV file'
    )
    parser.add_argument(
        '--fix',
        action='store_true',
        help='Automatically fix common issues'
    )
    parser.add_argument(
        '--eclipse-type',
        choices=['total', 'annular'],
        default='total',
        help='Eclipse type (default: total)'
    )
    
    args = parser.parse_args()
    
    csv_path = args.csv_file
    
    # Check file exists
    if not Path(csv_path).exists():
        print(f"❌ Error: File not found: {csv_path}")
        sys.exit(1)
    
    print("=" * 70)
    print("AZUS CSV VALIDATION TOOL")
    print("=" * 70)
    print()
    
    # Validate
    issues, problem_rows = validate_csv(csv_path, args.eclipse_type)
    
    if not issues:
        print("\n" + "=" * 70)
        print("✅ SUCCESS: CSV is valid!")
        print("=" * 70)
        sys.exit(0)
    
    # Show issues
    print("\n" + "=" * 70)
    print(f"⚠️  FOUND {len(issues)} ISSUE(S)")
    print("=" * 70)
    print()
    
    for issue in issues[:20]:  # Show first 20
        print(f"  • {issue}")
    
    if len(issues) > 20:
        print(f"\n  ... and {len(issues) - 20} more issues")
    
    # Show problem rows summary
    if problem_rows:
        print("\n" + "=" * 70)
        print("PROBLEM ROWS SUMMARY")
        print("=" * 70)
        print()
        
        # Group by issue type
        empty_eclipse_type = [r for r in problem_rows if any('Local Eclipse Type is empty' in i for i in r['issues'])]
        
        if empty_eclipse_type:
            print(f"🔴 {len(empty_eclipse_type)} row(s) with empty 'Local Eclipse Type':")
            for row in empty_eclipse_type[:10]:
                esid = row['esid'] or f"Row {row['row_num']}"
                print(f"   - {esid}")
            if len(empty_eclipse_type) > 10:
                print(f"   ... and {len(empty_eclipse_type) - 10} more")
    
    # Offer to fix
    if args.fix:
        print("\n" + "=" * 70)
        print("AUTO-FIX MODE")
        print("=" * 70)
        
        response = input("\n⚠️  This will modify your CSV file. Continue? (yes/no): ")
        
        if response.lower() == 'yes':
            fix_csv(csv_path, args.eclipse_type)
            
            # Re-validate
            print("\n" + "=" * 70)
            print("RE-VALIDATING")
            print("=" * 70)
            print()
            
            issues, _ = validate_csv(csv_path, args.eclipse_type)
            
            if not issues:
                print("\n" + "=" * 70)
                print("✅ SUCCESS: All issues fixed!")
                print("=" * 70)
                print("\nYou can now run your upload:")
                print("  python standalone_upload.py")
                sys.exit(0)
            else:
                print("\n" + "=" * 70)
                print(f"⚠️  {len(issues)} issue(s) remain")
                print("=" * 70)
                print("\nSome issues require manual correction.")
                print("Please review the CSV file and fix remaining issues.")
                sys.exit(1)
        else:
            print("\n❌ Auto-fix cancelled")
            sys.exit(1)
    else:
        print("\n" + "=" * 70)
        print("NEXT STEPS")
        print("=" * 70)
        print("\nOption 1: Auto-fix (recommended)")
        print(f"  python validate_csv.py {csv_path} --fix --eclipse-type {args.eclipse_type}")
        print("\nOption 2: Manual fix")
        print(f"  1. Open {csv_path} in a spreadsheet editor")
        print(f"  2. Find rows with empty 'Local Eclipse Type'")
        print(f"  3. Set to 'Annular', 'Total', or 'Partial'")
        print(f"  4. Save and run this validation again")
        
        sys.exit(1)


if __name__ == "__main__":
    main()
