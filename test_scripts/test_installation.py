#!/usr/bin/env python3
"""
Quick test to verify AZUS installation and configuration.

This script checks that:
- Python version is correct
- All dependencies are installed
- Environment variables are set
- Zenodo API is accessible
- Configuration file is valid

Usage:
    python test_installation.py

Run this BEFORE attempting any uploads to catch setup issues early.
"""

import sys
from pathlib import Path
import importlib.util


def check_python_version():
    """Check Python version is 3.9 or higher."""
    print("Checking Python version...")
    
    version = sys.version_info
    version_str = f"{version.major}.{version.minor}.{version.micro}"
    
    if version.major >= 3 and version.minor >= 9:
        print(f"  ✅ Python {version_str} (OK)")
        return True
    else:
        print(f"  ❌ Python {version_str} (Need 3.9+)")
        print(f"     Download from: https://www.python.org/downloads/")
        return False


def check_module(module_name, package_name=None):
    """Check if a Python module is installed."""
    if package_name is None:
        package_name = module_name
    
    spec = importlib.util.find_spec(module_name)
    if spec is not None:
        print(f"  ✅ {package_name}")
        return True
    else:
        print(f"  ❌ {package_name} (not installed)")
        return False


def check_dependencies():
    """Check all required dependencies are installed."""
    print("\nChecking dependencies...")
    
    modules = [
        ("prefect", "prefect"),
        ("prefect_invenio_rdm", "prefect-invenio-rdm"),
        ("html2text", "html-to-markdown"),
        ("pydantic", "pydantic"),
        ("pathlib", "pathlib"),
    ]
    
    all_installed = True
    for module_name, package_name in modules:
        if not check_module(module_name, package_name):
            all_installed = False
    
    if not all_installed:
        print(f"\n  💡 Install missing packages:")
        print(f"     pip install -r requirements.txt")
    
    return all_installed


def check_environment_variables():
    """Check required environment variables are set."""
    print("\nChecking environment variables...")
    
    import os
    
    token = os.getenv("INVENIO_RDM_ACCESS_TOKEN")
    base_url = os.getenv("INVENIO_RDM_BASE_URL")
    
    all_set = True
    
    if token and token != "ZENODO_ACESS_TOKEN":  # Check it's not the placeholder
        print(f"  ✅ INVENIO_RDM_ACCESS_TOKEN (set)")
    else:
        print(f"  ❌ INVENIO_RDM_ACCESS_TOKEN (not set or still placeholder)")
        print(f"     Edit set_env.sh and run: source set_env.sh")
        all_set = False
    
    if base_url:
        print(f"  ✅ INVENIO_RDM_BASE_URL ({base_url})")
    else:
        print(f"  ❌ INVENIO_RDM_BASE_URL (not set)")
        print(f"     Edit set_env.sh and run: source set_env.sh")
        all_set = False
    
    return all_set


def check_config_file():
    """Check config.json exists and is valid."""
    print("\nChecking configuration file...")
    
    config_path = Path("config.json")
    
    if not config_path.exists():
        print(f"  ❌ config.json not found")
        print(f"     Create config.json with your dataset paths")
        return False
    
    try:
        import json
        with open(config_path, 'r') as f:
            config = json.load(f)
        
        print(f"  ✅ config.json exists and is valid JSON")
        
        # Check required sections
        if "uploads" in config:
            print(f"  ✅ 'uploads' section found")
            
            # Check for total or annular
            has_dataset = False
            if "total" in config["uploads"]:
                total_dir = config["uploads"]["total"].get("dataset_dir")
                if total_dir:
                    print(f"  ✅ Total eclipse dataset: {total_dir}")
                    has_dataset = True
                    
                    # Check if directory exists
                    if not Path(total_dir).exists():
                        print(f"     ⚠️  Warning: Directory does not exist")
            
            if "annular" in config["uploads"]:
                annular_dir = config["uploads"]["annular"].get("dataset_dir")
                if annular_dir:
                    print(f"  ✅ Annular eclipse dataset: {annular_dir}")
                    has_dataset = True
                    
                    # Check if directory exists
                    if not Path(annular_dir).exists():
                        print(f"     ⚠️  Warning: Directory does not exist")
            
            if not has_dataset:
                print(f"  ⚠️  Warning: No dataset directories configured")
        else:
            print(f"  ❌ 'uploads' section not found in config.json")
            return False
        
        return True
        
    except json.JSONDecodeError as e:
        print(f"  ❌ config.json is not valid JSON: {e}")
        return False
    except Exception as e:
        print(f"  ❌ Error reading config.json: {e}")
        return False


def check_zenodo_api():
    """Check if Zenodo API is accessible."""
    print("\nChecking Zenodo API access...")
    
    import os
    
    token = os.getenv("INVENIO_RDM_ACCESS_TOKEN")
    base_url = os.getenv("INVENIO_RDM_BASE_URL")
    
    if not token or not base_url:
        print(f"  ⚠️  Skipping (environment variables not set)")
        return False
    
    try:
        import requests
        
        # Try to access the API
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        response = requests.get(
            f"{base_url}records",
            headers=headers,
            params={"size": 1},
            timeout=10
        )
        
        if response.status_code == 200:
            print(f"  ✅ Zenodo API accessible")
            print(f"  ✅ Authentication successful")
            return True
        elif response.status_code == 401:
            print(f"  ❌ Authentication failed (401 Unauthorized)")
            print(f"     Check your API token is correct")
            return False
        else:
            print(f"  ⚠️  Unexpected response: {response.status_code}")
            print(f"     API may be accessible but response unexpected")
            return False
            
    except ImportError:
        print(f"  ⚠️  Skipping (requests module not installed)")
        print(f"     Install with: pip install requests")
        return False
    except requests.exceptions.Timeout:
        print(f"  ❌ Connection timeout")
        print(f"     Check your internet connection")
        return False
    except requests.exceptions.ConnectionError:
        print(f"  ❌ Connection error")
        print(f"     Check your internet connection")
        print(f"     Verify Zenodo is accessible: {base_url}")
        return False
    except Exception as e:
        print(f"  ❌ Error: {e}")
        return False


def check_azus_files():
    """Check that required AZUS files exist."""
    print("\nChecking AZUS files...")
    
    required_files = [
        "tasks.py",
        "flows.py",
        "uploads.py",
        "models/audiomoth.py",
        "models/invenio.py",
    ]
    
    all_exist = True
    for file_path in required_files:
        path = Path(file_path)
        if path.exists():
            print(f"  ✅ {file_path}")
        else:
            print(f"  ❌ {file_path} (missing)")
            all_exist = False
    
    if not all_exist:
        print(f"\n  ⚠️  Warning: Some AZUS files are missing")
        print(f"     Make sure you're in the AZUS directory")
        print(f"     Current directory: {Path.cwd()}")
    
    return all_exist


def main():
    """Run all checks."""
    print("=" * 70)
    print("AZUS INSTALLATION TEST")
    print("=" * 70)
    print("\nThis script checks your AZUS setup to catch issues before uploading.")
    
    checks = [
        ("Python version", check_python_version),
        ("Dependencies", check_dependencies),
        ("Environment variables", check_environment_variables),
        ("Configuration file", check_config_file),
        ("AZUS files", check_azus_files),
        ("Zenodo API", check_zenodo_api),
    ]
    
    results = {}
    for name, check_func in checks:
        try:
            results[name] = check_func()
        except Exception as e:
            print(f"\n❌ Error during '{name}' check: {e}")
            results[name] = False
    
    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    
    for name, result in results.items():
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status}: {name}")
    
    print(f"\nPassed: {passed}/{total} checks")
    
    if passed == total:
        print("\n" + "=" * 70)
        print("✅ ALL CHECKS PASSED!")
        print("=" * 70)
        print("\nYour AZUS installation appears to be correctly configured.")
        print("\nNext steps:")
        print("  1. Validate your datasets:")
        print("     python test_scripts/validate_all_datasets.py /path/to/datasets")
        print("\n  2. Test upload one dataset:")
        print("     python test_scripts/test_single_upload.py")
        print("\n  3. If test succeeds, proceed with batch uploads")
        return 0
    else:
        print("\n" + "=" * 70)
        print("❌ SOME CHECKS FAILED")
        print("=" * 70)
        print("\nPlease fix the issues above before attempting to upload.")
        print("\nCommon fixes:")
        print("  • Missing dependencies: pip install -r requirements.txt")
        print("  • Environment variables: source set_env.sh")
        print("  • Wrong directory: cd /path/to/AZUS")
        return 1


if __name__ == "__main__":
    sys.exit(main())
