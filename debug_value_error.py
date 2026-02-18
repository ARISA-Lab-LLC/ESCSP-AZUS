#!/usr/bin/env python3
"""
Debug script to find where .value attribute error is happening.

This loads your actual data and tries to create the metadata to see
where the error occurs.
"""

import sys
import asyncio
from pathlib import Path

# Add current directory to path
sys.path.insert(0, '.')

from standalone_tasks import parse_collectors_csv, get_draft_config
from models.audiomoth import EclipseType


async def main():
    """Test metadata creation with your actual data."""
    
    print("=" * 70)
    print("DEBUG: Finding .value attribute error")
    print("=" * 70)
    print()
    
    # Load collectors
    csv_file = "/Volumes/DB_Library/Dropbox/programs/ESCSP_Data/AZUS_Workspace/Resources/2024_Total_Zenodo_Form_Spreadsheet.csv"
    
    print(f"Loading collectors from: {csv_file}")
    collectors = await parse_collectors_csv(csv_file, EclipseType.TOTAL)
    print(f"✅ Loaded {len(collectors)} collectors")
    print()
    
    # Find ESID 012
    collector = next((c for c in collectors if c.esid == "012"), None)
    
    if not collector:
        print("❌ Could not find ESID 012 in CSV")
        return
    
    print(f"✅ Found collector for ESID 012")
    print(f"   Eclipse type value: {repr(collector.eclipse_type)}")
    print(f"   Eclipse type type: {type(collector.eclipse_type)}")
    print()
    
    # Try to call eclipse_label
    print("Testing eclipse_label() method...")
    try:
        label = collector.eclipse_label()
        print(f"✅ eclipse_label() returned: {label}")
    except Exception as e:
        print(f"❌ eclipse_label() failed: {e}")
        import traceback
        traceback.print_exc()
        return
    print()
    
    # Try to create draft config
    print("Testing get_draft_config()...")
    try:
        config = await get_draft_config(collector, readme_html_path=None)
        print(f"✅ get_draft_config() succeeded")
        print(f"   Title: {config.metadata.get('title', 'N/A')}")
    except Exception as e:
        print(f"❌ get_draft_config() failed: {e}")
        import traceback
        traceback.print_exc()
        return
    print()
    
    print("=" * 70)
    print("✅ ALL TESTS PASSED - No .value error found!")
    print("=" * 70)
    print()
    print("This means the error is happening somewhere else.")
    print("Run the upload with the updated standalone_upload.py to see full traceback.")


if __name__ == "__main__":
    asyncio.run(main())
