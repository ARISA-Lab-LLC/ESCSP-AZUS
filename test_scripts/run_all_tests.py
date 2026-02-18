#!/usr/bin/env python3
"""
Run all AZUS tests in sequence.

This script runs all validation and test scripts to provide a complete
check of your AZUS setup and datasets before uploading.

Usage:
    python run_all_tests.py <config_file>

Arguments:
    config_file    Path to config.json (optional, default: ../config.json)

Example:
    python run_all_tests.py
    python run_all_tests.py ../config.json

This will:
    1. Check AZUS installation
    2. Validate configuration
    3. Test collector CSV files
    4. Validate all datasets
    5. Show summary and next steps
"""

import sys
import subprocess
from pathlib import Path
import json


def run_command(description, command, allow_failure=False):
    """
    Run a command and return success status.
    
    Args:
        description: What this command does
        command: Command to run as list
        allow_failure: If True, failure won't stop the test suite
    
    Returns:
        True if successful, False otherwise
    """
    print(f"\n{'='*70}")
    print(f"{description}")
    print(f"{'='*70}\n")
    
    try:
        result = subprocess.run(
            command,
            capture_output=False,
            text=True,
            check=False
        )
        
        if result.returncode == 0:
            print(f"\n✅ {description}: PASSED")
            return True
        else:
            print(f"\n❌ {description}: FAILED")
            if not allow_failure:
                print(f"   Fix this issue before continuing")
            return False
            
    except FileNotFoundError:
        print(f"\n❌ Error: Command not found: {command[0]}")
        return False
    except Exception as e:
        print(f"\n❌ Error running command: {e}")
        return False


def main():
    """Run all tests."""
    
    print("=" * 70)
    print("AZUS COMPLETE TEST SUITE")
    print("=" * 70)
    print("\nThis will run all validation and test scripts to verify your")
    print("AZUS installation and datasets are ready for upload.")
    
    # Get config file path
    if len(sys.argv) > 1:
        config_file = Path(sys.argv[1])
    else:
        config_file = Path(__file__).parent.parent / "config.json"
    
    print(f"\nConfiguration file: {config_file}")
    
    # Check config exists
    if not config_file.exists():
        print(f"\n❌ Error: Configuration file not found: {config_file}")
        print(f"   Create config.json or specify path as argument")
        sys.exit(1)
    
    # Load config
    try:
        with open(config_file, 'r') as f:
            config = json.load(f)
    except Exception as e:
        print(f"\n❌ Error loading config.json: {e}")
        sys.exit(1)
    
    # Get script directory
    script_dir = Path(__file__).parent
    
    # Track results
    results = []
    
    # ========================================================================
    # TEST 1: Installation Check
    # ========================================================================
    test_script = script_dir / "test_installation.py"
    if test_script.exists():
        success = run_command(
            "TEST 1: Installation Check",
            ["python3", str(test_script)]
        )
        results.append(("Installation", success))
    else:
        print(f"\n⚠️  Skipping: {test_script.name} not found")
        results.append(("Installation", None))
    
    # ========================================================================
    # TEST 2: Collector CSV Files
    # ========================================================================
    test_script = script_dir / "test_collector_csv.py"
    
    if test_script.exists():
        # Test total eclipse CSV
        if "uploads" in config and "total" in config["uploads"]:
            total_csv = config["uploads"]["total"].get("collectors_csv")
            if total_csv and Path(total_csv).exists():
                success = run_command(
                    "TEST 2a: Total Eclipse Collector CSV",
                    ["python3", str(test_script), total_csv, "total"],
                    allow_failure=True
                )
                results.append(("Total CSV", success))
            else:
                print(f"\n⚠️  Skipping: Total CSV not configured or not found")
                results.append(("Total CSV", None))
        
        # Test annular eclipse CSV
        if "uploads" in config and "annular" in config["uploads"]:
            annular_csv = config["uploads"]["annular"].get("collectors_csv")
            if annular_csv and Path(annular_csv).exists():
                success = run_command(
                    "TEST 2b: Annular Eclipse Collector CSV",
                    ["python3", str(test_script), annular_csv, "annular"],
                    allow_failure=True
                )
                results.append(("Annular CSV", success))
            else:
                print(f"\n⚠️  Skipping: Annular CSV not configured or not found")
                results.append(("Annular CSV", None))
    else:
        print(f"\n⚠️  Skipping: {test_script.name} not found")
        results.append(("Collector CSVs", None))
    
    # ========================================================================
    # TEST 3: Dataset Validation
    # ========================================================================
    test_script = script_dir / "validate_all_datasets.py"
    
    if test_script.exists():
        # Validate total eclipse datasets
        if "uploads" in config and "total" in config["uploads"]:
            total_dir = config["uploads"]["total"].get("dataset_dir")
            if total_dir and Path(total_dir).exists():
                success = run_command(
                    "TEST 3a: Total Eclipse Datasets",
                    ["python3", str(test_script), total_dir],
                    allow_failure=True
                )
                results.append(("Total Datasets", success))
            else:
                print(f"\n⚠️  Skipping: Total dataset directory not configured or not found")
                results.append(("Total Datasets", None))
        
        # Validate annular eclipse datasets
        if "uploads" in config and "annular" in config["uploads"]:
            annular_dir = config["uploads"]["annular"].get("dataset_dir")
            if annular_dir and Path(annular_dir).exists():
                success = run_command(
                    "TEST 3b: Annular Eclipse Datasets",
                    ["python3", str(test_script), annular_dir],
                    allow_failure=True
                )
                results.append(("Annular Datasets", success))
            else:
                print(f"\n⚠️  Skipping: Annular dataset directory not configured or not found")
                results.append(("Annular Datasets", None))
    else:
        print(f"\n⚠️  Skipping: {test_script.name} not found")
        results.append(("Dataset Validation", None))
    
    # ========================================================================
    # SUMMARY
    # ========================================================================
    print(f"\n{'='*70}")
    print("TEST SUITE SUMMARY")
    print(f"{'='*70}\n")
    
    # Count results
    passed = sum(1 for _, result in results if result is True)
    failed = sum(1 for _, result in results if result is False)
    skipped = sum(1 for _, result in results if result is None)
    total = len(results)
    
    # Show results
    print("Test Results:")
    for name, result in results:
        if result is True:
            status = "✅ PASS"
        elif result is False:
            status = "❌ FAIL"
        else:
            status = "⚠️  SKIP"
        print(f"  {status}  {name}")
    
    print(f"\nOverall:")
    print(f"  Passed:  {passed}/{total}")
    print(f"  Failed:  {failed}/{total}")
    print(f"  Skipped: {skipped}/{total}")
    
    # Determine overall status
    if failed == 0 and passed > 0:
        print(f"\n{'='*70}")
        print("✅ ALL TESTS PASSED!")
        print(f"{'='*70}")
        print("\nYour AZUS setup is ready for uploads!")
        
        print(f"\n📋 Next Steps:")
        print(f"  1. Review the test results above")
        print(f"  2. Perform a test upload with one dataset:")
        print(f"     python test_scripts/test_single_upload.py")
        print(f"  3. Verify the test upload on Zenodo")
        print(f"  4. If successful, proceed with batch uploads")
        
        return 0
        
    elif failed > 0:
        print(f"\n{'='*70}")
        print(f"❌ SOME TESTS FAILED")
        print(f"{'='*70}")
        print(f"\nYou have {failed} test failure(s) that need to be fixed.")
        
        print(f"\n📋 What to do:")
        print(f"  1. Review the failed tests above")
        print(f"  2. Fix the issues identified")
        print(f"  3. Run this test suite again")
        print(f"  4. All tests must pass before uploading")
        
        return 1
        
    else:
        print(f"\n{'='*70}")
        print(f"⚠️  NO TESTS RUN")
        print(f"{'='*70}")
        print(f"\nAll tests were skipped. This usually means:")
        print(f"  • config.json is not configured")
        print(f"  • Dataset directories don't exist")
        print(f"  • Test scripts are missing")
        
        print(f"\n📋 What to do:")
        print(f"  1. Configure config.json with your dataset paths")
        print(f"  2. Ensure dataset directories exist")
        print(f"  3. Run this test suite again")
        
        return 1


if __name__ == "__main__":
    sys.exit(main())
