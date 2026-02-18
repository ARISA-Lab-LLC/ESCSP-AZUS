#!/usr/bin/env python3
"""
Test uploading a single dataset to verify everything works.

This script performs a complete test upload of one dataset to Zenodo,
showing all steps and requiring confirmation before proceeding.

Usage:
    1. Edit the configuration section below with your test dataset details
    2. Run: python test_single_upload.py
    3. Follow the interactive prompts
    4. Verify the upload on Zenodo before running batch uploads

Important:
    - This creates a REAL record on Zenodo (not auto-published)
    - You must manually publish or delete the record afterward
    - Use Zenodo Sandbox for testing: https://sandbox.zenodo.org
"""

import asyncio
from pathlib import Path
import sys

# Add parent directory to path to import AZUS modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from flows import upload_dataset
from tasks import create_upload_data, parse_collectors_csv, find_dataset_files
from models.audiomoth import EclipseType


async def main():
    """Main test upload function."""
    
    print("=" * 70)
    print("AZUS TEST UPLOAD - SINGLE DATASET")
    print("=" * 70)
    
    # ============================================================
    # CONFIGURATION - EDIT THESE VALUES FOR YOUR TEST
    # ============================================================
    
    # Which ESID to test with (must exist in your collectors CSV)
    test_esid = "004"  # ← CHANGE THIS TO YOUR TEST ESID
    
    # Path to the ZIP file for this ESID
    zip_file = "/Volumes/DB_Library/Dropbox/programs/ESCSP_Data/AZUS_Workspace/Staging_Area/ESID_004/ESID_004.zip"  # ← CHANGE THIS TO YOUR ZIP FILE PATH
    
    # Path to collectors CSV file
    collectors_csv = "/Volumes/DB_Library/Dropbox/programs/ESCSP_Data/AZUS_Workspace/Staging_Area/ESID_004/total_eclipse_data.csv"  # ← CHANGE THIS
    
    # Eclipse type (TOTAL or ANNULAR)
    eclipse_type = EclipseType.TOTAL  # ← CHANGE TO EclipseType.ANNULAR if needed
    
    # ============================================================
    # END CONFIGURATION
    # ============================================================
    
    print(f"\n📋 Test Configuration:")
    print(f"   ESID:          {test_esid}")
    print(f"   ZIP file:      {zip_file}")
    print(f"   Collectors:    {collectors_csv}")
    print(f"   Eclipse type:  {eclipse_type.value}")
    
    # Verify paths exist
    if not Path(zip_file).exists():
        print(f"\n❌ ERROR: ZIP file not found: {zip_file}")
        print("Please update the zip_file path in the configuration section.")
        return
    
    if not Path(collectors_csv).exists():
        print(f"\n❌ ERROR: Collectors CSV not found: {collectors_csv}")
        print("Please update the collectors_csv path in the configuration section.")
        return
    
    # ============================================================
    # STEP 1: Find all associated files
    # ============================================================
    print(f"\n{'='*70}")
    print("STEP 1: Finding Associated Files")
    print(f"{'='*70}")
    print(f"Searching for files in: {Path(zip_file).parent}")
    
    try:
        files = await find_dataset_files(zip_file)
    except FileNotFoundError as e:
        print(f"\n❌ ERROR: {e}")
        return
    except Exception as e:
        print(f"\n❌ ERROR: Unexpected error: {e}")
        return
    
    print("\n📁 Files found:")
    found_count = 0
    missing_count = 0
    
    for filename, filepath in sorted(files.items()):
        if filepath:
            print(f"   ✅ {filename}")
            found_count += 1
        else:
            print(f"   ❌ {filename} - MISSING!")
            missing_count += 1
    
    print(f"\n📊 Summary: {found_count}/{len(files)} files found")
    
    if missing_count > 0:
        print(f"\n⚠️  WARNING: {missing_count} file(s) are missing!")
        print("The upload will proceed, but the Zenodo record may be incomplete.")
        print("\nMissing files:")
        for filename, filepath in files.items():
            if not filepath:
                print(f"   • {filename}")
        
        response = input("\n❓ Continue anyway? (yes/no): ")
        if response.lower() != 'yes':
            print("\n❌ Upload cancelled by user")
            return
    
    # ============================================================
    # STEP 2: Load collector data
    # ============================================================
    print(f"\n{'='*70}")
    print("STEP 2: Loading Collector Data")
    print(f"{'='*70}")
    print(f"Reading collectors from: {collectors_csv}")
    
    try:
        collectors = await parse_collectors_csv(
            csv_file_path=collectors_csv,
            eclipse_type=eclipse_type
        )
        print(f"\n✅ Loaded {len(collectors)} collector record(s)")
    except FileNotFoundError as e:
        print(f"\n❌ ERROR: {e}")
        return
    except ValueError as e:
        print(f"\n❌ ERROR: Invalid CSV file: {e}")
        return
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        return
    
    # Find collector for this ESID
    collector = next((c for c in collectors if c.esid == test_esid), None)
    if not collector:
        print(f"\n❌ ERROR: No collector found for ESID '{test_esid}'")
        print(f"\nAvailable ESIDs in CSV:")
        available_esids = [c.esid for c in collectors[:10]]  # Show first 10
        for esid in available_esids:
            print(f"   • {esid}")
        if len(collectors) > 10:
            print(f"   ... and {len(collectors) - 10} more")
        print(f"\nPlease update test_esid in the configuration section.")
        return
    
    print(f"✅ Found collector for ESID '{test_esid}'")
    print(f"   Latitude:  {collector.latitude}")
    print(f"   Longitude: {collector.longitude}")
    print(f"   Coverage:  {collector.eclipse_coverage}%")
    
    # ============================================================
    # STEP 3: Create upload data
    # ============================================================
    print(f"\n{'='*70}")
    print("STEP 3: Preparing Upload Data")
    print(f"{'='*70}")
    
    try:
        upload_data, unmatched = await create_upload_data(
            esid_file_pairs=[(test_esid, zip_file)],
            data_collectors=collectors
        )
    except Exception as e:
        print(f"\n❌ ERROR: Failed to create upload data: {e}")
        return
    
    if not upload_data:
        print("\n❌ ERROR: Failed to create upload data")
        print("Check that the ESID matches a collector in the CSV")
        return
    
    data = upload_data[0]
    
    print(f"\n✅ Upload data prepared successfully")
    print(f"\n📦 Dataset Details:")
    print(f"   ESID:              {data.esid}")
    print(f"   ZIP file:          {Path(data.zip_file).name}")
    print(f"   README.html:       {'✅ Found' if data.readme_html else '❌ Missing (will generate)'}")
    print(f"   README.md:         {'✅ Found' if data.readme_md else '❌ Missing'}")
    print(f"   Additional files:  {len(data.additional_files)}")
    print(f"   Total files:       {len(data.all_files)}")
    
    print(f"\n📋 Files that will be uploaded to Zenodo:")
    for i, file_path in enumerate(data.all_files, 1):
        file_size = Path(file_path).stat().st_size / (1024 * 1024)  # MB
        print(f"   {i:2d}. {Path(file_path).name:<45s} ({file_size:>8.2f} MB)")
    
    total_size = sum(Path(f).stat().st_size for f in data.all_files) / (1024 * 1024)
    print(f"\n   Total size: {total_size:.2f} MB")
    
    # ============================================================
    # STEP 4: Confirm upload
    # ============================================================
    print(f"\n{'='*70}")
    print("STEP 4: Upload Confirmation")
    print(f"{'='*70}")
    
    print(f"\n⚠️  YOU ARE ABOUT TO UPLOAD TO ZENODO:")
    print(f"   • {len(data.all_files)} files ({total_size:.2f} MB)")
    print(f"   • For ESID: {test_esid}")
    print(f"   • This will create a REAL record on Zenodo")
    print(f"   • The record will NOT be auto-published")
    print(f"   • You must manually publish or delete it afterward")
    
    if "sandbox" not in str(collectors_csv).lower():
        print(f"\n🚨 NOTE: You appear to be uploading to PRODUCTION Zenodo")
        print(f"   Consider using Zenodo Sandbox for testing:")
        print(f"   https://sandbox.zenodo.org")
    
    response = input("\n❓ Proceed with upload? (yes/no): ")
    if response.lower() != 'yes':
        print("\n❌ Upload cancelled by user")
        print("No files were uploaded to Zenodo.")
        return
    
    # ============================================================
    # STEP 5: Upload to Zenodo
    # ============================================================
    print(f"\n{'='*70}")
    print("STEP 5: Uploading to Zenodo")
    print(f"{'='*70}")
    print("\n🚀 Starting upload...")
    print("⏱️  This may take several minutes depending on file sizes...")
    print("Please wait...\n")
    
    try:
        result = await upload_dataset(
            data=data,
            delete_failures=True,   # Delete record if upload fails
            auto_publish=False      # Don't auto-publish (manual review required)
        )
    except Exception as e:
        print(f"\n❌ UPLOAD FAILED WITH EXCEPTION!")
        print(f"Error: {e}")
        print(f"\n🔍 Check that:")
        print(f"   • Your Zenodo API token is correct")
        print(f"   • You have internet connection")
        print(f"   • Zenodo is accessible (check status.zenodo.org)")
        return
    
    # ============================================================
    # STEP 6: Show results
    # ============================================================
    print(f"\n{'='*70}")
    print("UPLOAD RESULTS")
    print(f"{'='*70}")
    
    if result.successful:
        print("\n✅ UPLOAD SUCCESSFUL!")
        
        if result.api_response:
            record_id = result.api_response.get('id')
            doi = result.api_response.get('doi', 'Not yet assigned')
            
            if 'links' in result.api_response:
                record_url = result.api_response['links'].get('self_html')
                
                print(f"\n📄 Zenodo Record Details:")
                print(f"   Record ID: {record_id}")
                print(f"   DOI:       {doi}")
                print(f"   Status:    Draft (not published)")
                print(f"\n🌐 View your record at:")
                print(f"   {record_url}")
                
                print(f"\n{'='*70}")
                print("NEXT STEPS - VERY IMPORTANT!")
                print(f"{'='*70}")
                print(f"\n1️⃣  VERIFY THE UPLOAD")
                print(f"   • Click the URL above")
                print(f"   • Check that all {len(data.all_files)} files are present")
                print(f"   • Verify the description matches your README.html")
                print(f"   • Review metadata (title, creators, dates)")
                print(f"   • Test downloading one file to ensure it works")
                
                print(f"\n2️⃣  PUBLISH OR DELETE")
                print(f"   ✅ If everything looks good:")
                print(f"      → Click the green 'Publish' button on Zenodo")
                print(f"      → Record becomes public and gets a permanent DOI")
                
                print(f"\n   ❌ If something is wrong:")
                print(f"      → Click 'Delete' to remove the draft")
                print(f"      → Fix the issue and run this test again")
                
                print(f"\n3️⃣  PROCEED TO BATCH UPLOAD")
                print(f"   • Once test upload is verified and published")
                print(f"   • Run batch upload via Prefect for remaining datasets")
                
            else:
                print(f"\n⚠️  Warning: API response received but no record link")
                print(f"Check Zenodo dashboard manually")
        else:
            print(f"\n⚠️  Warning: Upload succeeded but no API response received")
            print(f"Check your Zenodo uploads page manually")
    
    else:
        print("\n❌ UPLOAD FAILED!")
        
        if result.error:
            print(f"\n🔴 Error Details:")
            print(f"   Type:    {result.error.type}")
            print(f"   Message: {result.error.error_message}")
        
        print(f"\n🔍 Troubleshooting Steps:")
        print(f"   1. Verify all files exist and are readable:")
        print(f"      ls -la {Path(zip_file).parent}")
        
        print(f"\n   2. Check your Zenodo API token:")
        print(f"      echo $INVENIO_RDM_ACCESS_TOKEN")
        
        print(f"\n   3. Verify you loaded environment variables:")
        print(f"      source set_env.sh")
        
        print(f"\n   4. Check Zenodo service status:")
        print(f"      https://status.zenodo.org")
        
        print(f"\n   5. Review the error message above for specific issues")
        
        print(f"\n   6. Try uploading a smaller test file first")


if __name__ == "__main__":
    # Run async main function
    asyncio.run(main())
