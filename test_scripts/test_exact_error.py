#!/usr/bin/env python3
"""
Test script to find EXACTLY where the .value error is happening.

This will show the full traceback with line numbers.
"""

import sys
import asyncio
import traceback
from pathlib import Path

# Make sure we import from local directory
sys.path.insert(0, '/Volumes/DB_Library/Dropbox/programs/ESCSP_Data/AZUS_Workspace')

print("=" * 70)
print("DEBUGGING: Finding .value attribute error")
print("=" * 70)
print()

# Test imports
print("Step 1: Testing imports...")
try:
    from models.audiomoth import EclipseType, DataCollector
    print("✅ Imported from models.audiomoth")
    print(f"   EclipseType.TOTAL = {EclipseType.TOTAL}")
    print(f"   EclipseType.TOTAL.value = {EclipseType.TOTAL.value}")
except Exception as e:
    print(f"❌ Import failed: {e}")
    traceback.print_exc()
    sys.exit(1)

print()
print("Step 2: Loading collector data...")
try:
    from standalone_tasks import parse_collectors_csv
    
    csv_file = "/Volumes/DB_Library/Dropbox/programs/ESCSP_Data/AZUS_Workspace/Resources/2024_Total_Zenodo_Form_Spreadsheet.csv"
    
    async def load_data():
        return await parse_collectors_csv(csv_file, EclipseType.TOTAL)
    
    collectors = asyncio.run(load_data())
    print(f"✅ Loaded {len(collectors)} collectors")
    
    # Find ESID 012
    collector = next((c for c in collectors if c.esid == "012"), None)
    if not collector:
        print("❌ ESID 012 not found")
        sys.exit(1)
    
    print(f"✅ Found ESID 012")
    print(f"   eclipse_type value: {repr(collector.eclipse_type)}")
    print(f"   eclipse_type type: {type(collector.eclipse_type)}")
    
except Exception as e:
    print(f"❌ Loading failed: {e}")
    traceback.print_exc()
    sys.exit(1)

print()
print("Step 3: Testing eclipse_label() method...")
try:
    label = collector.eclipse_label()
    print(f"✅ eclipse_label() = {label}")
except Exception as e:
    print(f"❌ eclipse_label() failed: {e}")
    print()
    print("FULL TRACEBACK:")
    traceback.print_exc()
    sys.exit(1)

print()
print("Step 4: Testing get_draft_config()...")
try:
    from standalone_tasks import get_draft_config
    
    async def test_config():
        return await get_draft_config(collector, readme_html_path=None)
    
    config = asyncio.run(test_config())
    print(f"✅ get_draft_config() succeeded")
    print(f"   Title: {config.metadata.get('title', 'N/A')[:50]}...")
    
except Exception as e:
    print(f"❌ get_draft_config() failed: {e}")
    print()
    print("FULL TRACEBACK:")
    traceback.print_exc()
    sys.exit(1)

print()
print("Step 5: Testing DraftConfig creation...")
try:
    from standalone_uploader import create_draft_record, get_credentials_from_env
    from prefect_invenio_rdm.models.records import DraftConfig
    
    # This is what actually gets sent to Zenodo
    print("   Creating DraftConfig object...")
    draft_config = DraftConfig(
        record_access=config.record_access,
        files_access=config.files_access,
        files_enabled=config.files_enabled,
        metadata=config.metadata,
        community_id=config.community_id,
        custom_fields=config.custom_fields,
        pids=config.pids,
    )
    print(f"✅ DraftConfig created")
    
    print()
    print("   Checking metadata...")
    print(f"   Title: {draft_config.metadata.get('title', 'N/A')[:50]}...")
    print(f"   Metadata keys: {list(draft_config.metadata.keys())[:5]}...")
    
except Exception as e:
    print(f"❌ DraftConfig creation failed: {e}")
    print()
    print("FULL TRACEBACK:")
    traceback.print_exc()
    sys.exit(1)

print()
print("=" * 70)
print("✅ ALL TESTS PASSED!")
print("=" * 70)
print()
print("The error is NOT in the code we've checked.")
print("It must be happening during the actual Zenodo API call.")
print()
print("Next: Replace standalone_upload.py with the version that has")
print("full traceback logging, then run the upload again to see exactly")
print("where the error occurs.")
