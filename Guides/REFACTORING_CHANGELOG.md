# AZUS Refactoring Change Log

## Summary

Transformed AZUS from an Eclipse Soundscapes–specific tool into a **generalizable
citizen science data upload platform**.  All project-specific identity is now in
configuration files, not Python code.

---

## Files Created (New)

| File | Purpose |
|------|---------|
| `Resources/project_config.json` | Eclipse Soundscapes identity — all creators, contributors, funding, community, custom fields, CSV headers, file lists |
| `Resources/README_template.html` | HTML template for Zenodo descriptions (moved from inline Python) |
| `templates/project_config.json.example` | Documented blank template for new projects |
| `templates/config.json.example` | Blank config template (replaces hardcoded annular/total sections with a `datasets` list) |
| `templates/README_template.html.example` | Documented blank README template |
| `templates/set_env.sh.example` | Blank credentials template |
| `models/__init__.py` | Package init |
| `Records/` | Directory for upload result CSVs |

## Files Refactored

### `models/audiomoth.py` (294 → 300 lines)
- **Removed** `from prefect.blocks.core import Block` — zero Prefect dependency
- **Removed** `UploadedFilesBlock` class (Prefect Block, dead code)
- **Added** `DraftConfig` model — replaces `prefect_invenio_rdm.models.records.DraftConfig`
- **Added** `Access` enum — replaces `prefect_invenio_rdm.models.records.Access`
- **Renamed** `EclipseType` → `DatasetCategory` (with backward-compatible alias)
- **Generalized** `eclipse_label()` — now handles Partial eclipses too
- **Fixed** `PersistedResult.update()` — loop over `_DIRECT_FIELDS` set instead of 12 if-blocks

### `models/invenio.py` (317 → 300 lines)
- No structural changes — already clean Pydantic models
- Enhanced docstrings with Sphinx-compatible format

### `standalone_tasks.py` (826 + 620 → 1,442 lines — two files merged)
**Merged** `standalone_upload.py` into `standalone_tasks.py` — single module for the entire pipeline.

Key changes:
- **Added** `load_project_config()` — reads `Resources/project_config.json`
- **Added** `build_creators()`, `build_contributors()`, `build_fundings()` — config-driven metadata builders replacing ~200 lines of hardcoded Python
- **Removed** all `async/await` — every function is now plain synchronous (was gratuitous async over synchronous I/O)
- **Removed** hardcoded file list (`required_files = [...]`) — now reads from `project_config.json`
- **Removed** hardcoded CSV header validation — now reads from `project_config.json`
- **Removed** hardcoded title construction — now uses `string.Template` from config
- **Removed** `from prefect_invenio_rdm.models.records import DraftConfig, Access` — uses local models
- **Eliminated** duplicate annular/total processing — `upload_datasets()` now iterates a single `datasets` list from config
- **Fixed** all `print()` → `logging.getLogger("azus")` 
- **Fixed** SHA-512 buffer: 4 KB → 64 KB (`_HASH_BUFFER_SIZE = 65_536`)
- **Added** `UploadTracker` class (moved from standalone_upload.py)
- **Added** `save_result()` helper (moved from standalone_upload.py)
- **Added** CLI `main()` with `--config` and `--dry-run` flags

### `standalone_uploader.py` (361 → 362 lines)
- **Removed** `async` from `upload_to_zenodo()` — was async but used synchronous `requests`
- **Consolidated** triple-duplicated error handling (HTTPError / RequestException / Exception) into a single `except` clause + `_cleanup_failed_draft()` helper
- **Added** `_auth_headers()` helper to eliminate repeated header construction
- **Fixed** all `print()` → `logging.getLogger("azus.uploader")`

### `prepare_dataset.py` (640 → 605 lines)
- **Removed** 80-line inline HTML template — now reads `Resources/README_template.html`
- **Added** `string.Template` substitution for `$variable` placeholders
- **Added** `--readme-template` CLI flag for custom template path
- **Fixed** SHA-512 buffer: 4 KB → 64 KB
- **Fixed** all `print()` → `logging.getLogger("azus.prepare")`

### `requirements-standalone.txt`
- **Removed** `prefect-invenio-rdm>=0.0.6` — no longer needed
- Only requires: `pydantic>=2.0.0`, `requests>=2.28.0`

## Files Removed (Dead Code)

| File | Lines | Reason |
|------|-------|--------|
| `tasks.py` | 1,018 | Deprecated Prefect workflow — functionality lives in standalone_tasks.py |
| `flows.py` | 422 | Deprecated Prefect flow — replaced by standalone_tasks.main() |
| `audiomoth.old` | 203 | Old version of audiomoth.py |
| `debug_value_error.py` | 82 | One-time debug script |
| `file_list_test_script.py` | 34 | Test script |
| `readme_test_script.py` | 30 | Test script |
| `extract_test_script.py` | 55 | Test script |
| **Total removed** | **1,844** | |

## Files Unchanged (Keep As-Is)

| File | Purpose |
|------|---------|
| `validate_csv.py` | CSV validation and repair tool |
| `create_upload_package.py` | ZIP package creator from file_list.csv |
| `batch_create_packages.py` | Batch processing wrapper |
| `escsp_azus.py` | Utility functions (file size updater, ESID extractor) |

## Config Structure Change

**Before** (config.json):
```json
{
  "uploads": {
    "annular": {"dataset_dir": "...", "collectors_csv": "..."},
    "total": {"dataset_dir": "...", "collectors_csv": "..."}
  }
}
```

**After** (config.json):
```json
{
  "project_config": "Resources/project_config.json",
  "readme_template": "Resources/README_template.html",
  "uploads": {
    "datasets": [
      {"name": "2024 Total Eclipse", "dataset_dir": "...", "collectors_csv": "...", "dataset_category": "Total"},
      {"name": "2023 Annular Eclipse", "dataset_dir": "...", "collectors_csv": "...", "dataset_category": "Annular"}
    ]
  }
}
```

This eliminates the hardcoded annular/total dichotomy and supports any number of dataset categories.

## Metrics

| Metric | Before | After | Change |
|--------|--------|-------|--------|
| Active Python lines | 3,056 | 3,010 | −46 (−1.5%) |
| Dead code lines | 1,844 | 0 | −1,844 (−100%) |
| **Total Python** | **4,900** | **3,010** | **−1,890 (−39%)** |
| External dependencies | 3 (pydantic, requests, prefect-invenio-rdm) | 2 (pydantic, requests) | −1 |
| Hardcoded identity locations | 8+ across 3+ files | 0 (config file only) | −100% |
| Files to edit for new project | 3+ Python files | 2 JSON/HTML files | −67% |
| SHA-512 buffer size | 4 KB | 64 KB | 16× improvement |
| async functions (synchronous I/O) | ~20 | 0 | −100% |
| Duplicate processing blocks | 2 (annular + total) | 0 (single loop) | −100% |
| Inline HTML template lines | ~80 | 0 | −100% |

## How to Adopt AZUS for a New Project

1. Copy `templates/project_config.json.example` → `Resources/project_config.json`
2. Fill in your project's creators, contributors, funding, community ID, etc.
3. Copy `templates/README_template.html.example` → `Resources/README_template.html`
4. Customize the HTML template with your project's description format
5. Copy `templates/config.json.example` → `Resources/config.json`
6. Set your dataset directories and CSV paths
7. Copy `templates/set_env.sh.example` → `Resources/set_env.sh`
8. Add your Zenodo API token
9. Run: `source Resources/set_env.sh && python standalone_tasks.py`

**No Python code needs to be edited.**
