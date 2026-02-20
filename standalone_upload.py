#!/usr/bin/env python3
"""
Standalone AZUS Upload Script (No Prefect Required)

This script provides the same functionality as the Prefect-based upload system
but runs directly without requiring a Prefect server.

Usage:
    python standalone_upload.py [--config config.json]
    
Example:
    python standalone_upload.py
    python standalone_upload.py --config /path/to/custom_config.json
"""

import asyncio
import argparse
import json
import logging
import sys
from pathlib import Path
from typing import List, Optional, Dict, Any

# Import from existing AZUS modules (Prefect-free functions)
from standalone_tasks import (
    list_dir_files,
    get_esid_file_pairs,
    parse_collectors_csv,
    create_upload_data,
    save_result_csv,
    rename_dir_files,
    get_draft_config,
    get_recording_dates,
    find_dataset_files,
)

from standalone_uploader import (
    upload_to_zenodo,
    get_credentials_from_env,
)

from models.audiomoth import (
    EclipseType,
    DataCollector,
    UploadData,
    PersistedResult,
)


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('azus_upload.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


class UploadTracker:
    """Track uploaded files to avoid duplicates."""
    
    def __init__(self, tracker_file: str = ".uploaded_files.txt"):
        self.tracker_file = Path(tracker_file)
        self.uploaded_files = self._load_uploaded_files()
    
    def _load_uploaded_files(self) -> set:
        """Load list of previously uploaded files."""
        if self.tracker_file.exists():
            with open(self.tracker_file, 'r') as f:
                return set(line.strip() for line in f if line.strip())
        return set()
    
    def is_uploaded(self, file_path: str) -> bool:
        """Check if file has already been uploaded."""
        return file_path in self.uploaded_files
    
    def mark_uploaded(self, file_path: str):
        """Mark a file as uploaded."""
        self.uploaded_files.add(file_path)
        with open(self.tracker_file, 'a') as f:
            f.write(f"{file_path}\n")
    
    def get_count(self) -> int:
        """Get number of uploaded files."""
        return len(self.uploaded_files)


async def save_result(
    esid: str,
    zip_file: str,
    success: bool,
    success_file: str,
    failure_file: str,
    api_response: Optional[Dict[str, Any]] = None,
    error_type: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    """
    Save the result of an upload.
    
    Args:
        esid: A unique AudioMoth ID
        zip_file: The uploaded file path
        success: Whether upload was successful
        success_file: CSV file to save successful results
        failure_file: CSV file to save failed results
        api_response: Response from Zenodo API
        error_type: Type of error if failed
        error_message: Error message if failed
    """
    results_file = success_file if success else failure_file
    persisted_result = PersistedResult(esid=esid)
    
    if not success:
        persisted_result.error_type = error_type or "Unknown"
        persisted_result.error_message = error_message or "Upload failed"
    
    if api_response:
        persisted_result.update(api_response)
    
    await save_result_csv(file=results_file, result=persisted_result)
    
    if success:
        logger.info(f"✅ ESID {esid}: Upload successful")
    else:
        logger.error(f"❌ ESID {esid}: Upload failed - {error_message}")


async def get_upload_data(
    data_dir: str,
    data_collectors_file: str,
    eclipse_type: EclipseType,
    failure_results_file: str,
    tracker: UploadTracker,
) -> List[UploadData]:
    """
    Retrieve all datasets to upload in the given directory.
    
    Args:
        data_dir: Directory containing the AudioMoth datasets
        data_collectors_file: CSV file with data collector info
        eclipse_type: Type of eclipse (TOTAL or ANNULAR)
        failure_results_file: CSV file to save failures
        tracker: Upload tracker to skip already uploaded files
        
    Returns:
        List of UploadData objects ready for upload
    """
    if not data_dir:
        raise ValueError("Missing data directory")
    
    if not data_collectors_file:
        raise ValueError("Missing data collectors file")
    
    logger.info(f"📂 Loading data collectors from: {data_collectors_file}")
    data_collectors: List[DataCollector] = await parse_collectors_csv(
        csv_file_path=data_collectors_file,
        eclipse_type=eclipse_type
    )
    logger.info(f"✅ Loaded {len(data_collectors)} data collector records")
    
    logger.info(f"📂 Scanning directory: {data_dir}")
    
    # Look for ZIP files in ESID subdirectories (ESID_XXX/ESID_XXX.zip)
    data_path = Path(data_dir)
    dir_files = []
    
    # Find all ESID_XXX subdirectories
    for subdir in data_path.iterdir():
        if subdir.is_dir() and (subdir.name.startswith('ESID_') or subdir.name.startswith('ESID#')):
            # Look for ESID_XXX.zip inside this directory
            for zip_file in subdir.glob('ESID_*.zip'):
                dir_files.append(str(zip_file))
    
    logger.info(f"✅ Found {len(dir_files)} ZIP files in ESID subdirectories")
    
    # Skip already uploaded files
    original_count = len(dir_files)
    dir_files = [file for file in dir_files if not tracker.is_uploaded(file)]
    skipped = original_count - len(dir_files)
    
    if skipped > 0:
        logger.info(f"⏭️  Skipped {skipped} already uploaded file(s)")
    
    if not dir_files:
        logger.warning("⚠️  No new files to upload")
        return []
    
    # Extract ESID from filenames
    esid_file_pairs = await get_esid_file_pairs(files=dir_files)
    
    # Create upload data
    upload_data, unmatched_ids = await create_upload_data(
        esid_file_pairs=esid_file_pairs,
        data_collectors=data_collectors
    )
    
    # Log unmatched ESIDs
    for esid in unmatched_ids:
        logger.warning(f"⚠️  No collector data found for ESID: {esid}")
        await save_result_csv(
            file=failure_results_file,
            result=PersistedResult(
                esid=esid,
                error_message="Unable to find data collector info"
            ),
        )
    
    logger.info(f"✅ Prepared {len(upload_data)} dataset(s) for upload")
    return upload_data


async def upload_dataset(
    data: UploadData,
    delete_failures: bool = False,
    auto_publish: bool = False,
    related_identifiers_csv: Optional[str] = None,
    references_csv: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Upload a single dataset to Zenodo.
    
    Args:
        data: The upload data
        delete_failures: Delete record if upload fails
        auto_publish: Auto-publish after successful upload
        related_identifiers_csv: Path to CSV with related identifiers
        references_csv: Path to CSV with bibliographic references
        
    Returns:
        Dictionary with success status and response
    """
    logger.info(f"🚀 Starting upload for ESID {data.esid}")
    logger.info(f"   ZIP file: {Path(data.zip_file).name}")
    logger.info(f"   Total files: {len(data.all_files)}")
    
    try:
        # Get recording dates from ZIP file
        start_date, end_date = await get_recording_dates(zip_file=data.zip_file)
        logger.debug(f"   Recording period: {start_date} to {end_date}")
        
        # Update data collector with dates
        data.data_collector.first_recording_day = start_date
        data.data_collector.last_recording_day = end_date
        
        # Create draft configuration
        config = await get_draft_config(
            data_collector=data.data_collector,
            readme_html_path=data.readme_html,
            related_identifiers_csv=related_identifiers_csv,
            references_csv=references_csv
        )
        
        # Upload to Zenodo
        logger.info(f"📤 Uploading to Zenodo...")
        result = await upload_to_zenodo(
            files=data.all_files,
            config=config,
            delete_on_failure=delete_failures,
            auto_publish=auto_publish,
        )
        
        return result
        
    except Exception as e:
        # Print full traceback for debugging
        import traceback
        logger.error(f"❌ Exception during upload: {e}")
        logger.error("Full traceback:")
        logger.error(traceback.format_exc())
        
        return {
            'successful': False,
            'error': {
                'type': type(e).__name__,
                'error_message': str(e)
            },
            'api_response': None
        }


async def upload_datasets(
    successful_results_file: str,
    failure_results_file: str,
    annular_dir: Optional[str] = None,
    total_dir: Optional[str] = None,
    annular_data_collector_csv: Optional[str] = None,
    total_data_collector_csv: Optional[str] = None,
    related_identifiers_csv: Optional[str] = None,
    references_csv: Optional[str] = None,
    auto_publish: bool = False,
    delete_failures: bool = False,
) -> Dict[str, int]:
    """
    Main upload function - uploads all datasets.
    
    Args:
        successful_results_file: CSV file for successful uploads
        failure_results_file: CSV file for failed uploads
        annular_dir: Directory with annular eclipse data
        total_dir: Directory with total eclipse data
        annular_data_collector_csv: CSV with annular collector info
        total_data_collector_csv: CSV with total collector info
        related_identifiers_csv: CSV with related identifiers (citations, related works)
        references_csv: CSV with bibliographic references
        auto_publish: Auto-publish successful uploads
        delete_failures: Delete failed records
        
    Returns:
        Dictionary with upload statistics
    """
    if not annular_dir and not total_dir:
        raise ValueError("Missing directories for annular and/or total eclipse data")
    
    if not annular_data_collector_csv and not total_data_collector_csv:
        raise ValueError("Missing data collector files")
    
    if annular_dir and not annular_data_collector_csv:
        raise ValueError("Missing data collector file for annular eclipse data")
    
    if total_dir and not total_data_collector_csv:
        raise ValueError("Missing data collector file for total eclipse data")
    
    # Initialize upload tracker
    tracker = UploadTracker()
    logger.info(f"📊 Upload tracker: {tracker.get_count()} file(s) previously uploaded")
    
    stats = {
        'total_processed': 0,
        'successful': 0,
        'failed': 0,
        'skipped': 0
    }
    
    # Process annular eclipse data
    if annular_dir:
        logger.info("=" * 70)
        logger.info("PROCESSING ANNULAR ECLIPSE DATA")
        logger.info("=" * 70)
        
        await rename_dir_files(directory=annular_dir)
        
        annular_upload_data = await get_upload_data(
            data_dir=annular_dir,
            data_collectors_file=annular_data_collector_csv,
            eclipse_type=EclipseType.ANNULAR,
            failure_results_file=failure_results_file,
            tracker=tracker,
        )
        
        for i, data in enumerate(annular_upload_data, 1):
            logger.info(f"\n📦 Processing {i}/{len(annular_upload_data)}: ESID {data.esid}")
            
            result = await upload_dataset(
                data=data,
                delete_failures=delete_failures,
                auto_publish=auto_publish,
                related_identifiers_csv=related_identifiers_csv,
                references_csv=references_csv
            )
            
            stats['total_processed'] += 1
            
            if result['successful']:
                stats['successful'] += 1
                tracker.mark_uploaded(data.zip_file)
                await save_result(
                    esid=data.esid,
                    zip_file=data.zip_file,
                    success=True,
                    success_file=successful_results_file,
                    failure_file=failure_results_file,
                    api_response=result.get('api_response')
                )
            else:
                stats['failed'] += 1
                error = result.get('error', {})
                await save_result(
                    esid=data.esid,
                    zip_file=data.zip_file,
                    success=False,
                    success_file=successful_results_file,
                    failure_file=failure_results_file,
                    error_type=error.get('type'),
                    error_message=error.get('error_message')
                )
    
    # Process total eclipse data
    if total_dir:
        logger.info("\n" + "=" * 70)
        logger.info("PROCESSING TOTAL ECLIPSE DATA")
        logger.info("=" * 70)
        
        await rename_dir_files(directory=total_dir)
        
        total_upload_data = await get_upload_data(
            data_dir=total_dir,
            data_collectors_file=total_data_collector_csv,
            eclipse_type=EclipseType.TOTAL,
            failure_results_file=failure_results_file,
            tracker=tracker,
        )
        
        for i, data in enumerate(total_upload_data, 1):
            logger.info(f"\n📦 Processing {i}/{len(total_upload_data)}: ESID {data.esid}")
            
            result = await upload_dataset(
                data=data,
                delete_failures=delete_failures,
                auto_publish=auto_publish,
                related_identifiers_csv=related_identifiers_csv,
                references_csv=references_csv
            )
            
            stats['total_processed'] += 1
            
            if result['successful']:
                stats['successful'] += 1
                tracker.mark_uploaded(data.zip_file)
                await save_result(
                    esid=data.esid,
                    zip_file=data.zip_file,
                    success=True,
                    success_file=successful_results_file,
                    failure_file=failure_results_file,
                    api_response=result.get('api_response')
                )
            else:
                stats['failed'] += 1
                error = result.get('error', {})
                await save_result(
                    esid=data.esid,
                    zip_file=data.zip_file,
                    success=False,
                    success_file=successful_results_file,
                    failure_file=failure_results_file,
                    error_type=error.get('type'),
                    error_message=error.get('error_message')
                )
    
    return stats


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='AZUS Standalone Upload - Upload datasets to Zenodo without Prefect'
    )
    parser.add_argument(
        '--config',
        type=str,
        default='config.json',
        help='Path to configuration file (default: config.json)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be uploaded without actually uploading'
    )
    
    args = parser.parse_args()
    
    # Load configuration
    config_path = Path(args.config)
    if not config_path.exists():
        logger.error(f"❌ Configuration file not found: {config_path}")
        sys.exit(1)
    
    logger.info(f"📋 Loading configuration from: {config_path}")
    
    with open(config_path, 'r', encoding='utf-8') as f:
        config_data = json.load(f)
    
    if 'uploads' not in config_data:
        logger.error("❌ Invalid configuration: missing 'uploads' section")
        sys.exit(1)
    
    uploads_config = config_data['uploads']
    
    # Extract configuration
    annular_dir = ""
    annular_csv = ""
    total_dir = ""
    total_csv = ""
    
    if 'annular' in uploads_config:
        annular_dir = uploads_config['annular'].get('dataset_dir', '')
        annular_csv = uploads_config['annular'].get('collectors_csv', '')
    
    if 'total' in uploads_config:
        total_dir = uploads_config['total'].get('dataset_dir', '')
        total_csv = uploads_config['total'].get('collectors_csv', '')
    
    # Extract optional CSV files for related identifiers and references
    related_identifiers_csv = uploads_config.get('related_identifiers_csv', '')
    references_csv = uploads_config.get('references_csv', '')
    
    # Check credentials
    try:
        credentials = get_credentials_from_env()
        logger.info("✅ Zenodo credentials loaded from environment")
    except ValueError as e:
        logger.error(f"❌ {e}")
        logger.error("   Please set INVENIO_RDM_ACCESS_TOKEN and INVENIO_RDM_BASE_URL")
        logger.error("   Run: source set_env.sh")
        sys.exit(1)
    
    if args.dry_run:
        logger.info("🔍 DRY RUN MODE - No uploads will be performed")
    
    # Display configuration
    logger.info("\n" + "=" * 70)
    logger.info("AZUS STANDALONE UPLOAD")
    logger.info("=" * 70)
    logger.info(f"Configuration file: {config_path}")
    logger.info(f"Annular directory: {annular_dir or 'Not configured'}")
    logger.info(f"Total directory: {total_dir or 'Not configured'}")
    logger.info(f"Auto-publish: {uploads_config.get('auto_publish', False)}")
    logger.info(f"Delete failures: {uploads_config.get('delete_failures', False)}")
    logger.info("=" * 70)
    
    # Validate CSV files before proceeding
    if not args.dry_run:
        logger.info("\n" + "=" * 70)
        logger.info("VALIDATING CSV FILES")
        logger.info("=" * 70)
        
        csv_files_to_check = []
        if total_csv:
            csv_files_to_check.append(('total', total_csv))
        if annular_csv:
            csv_files_to_check.append(('annular', annular_csv))
        
        for eclipse_type, csv_file in csv_files_to_check:
            logger.info(f"\n📋 Checking {eclipse_type} CSV: {Path(csv_file).name}")
            try:
                # Quick validation - just try to parse
                from standalone_tasks import parse_collectors_csv
                from models.audiomoth import EclipseType
                
                eclipse_enum = EclipseType.TOTAL if eclipse_type == 'total' else EclipseType.ANNULAR
                collectors = await parse_collectors_csv(csv_file, eclipse_enum)
                logger.info(f"   ✅ Valid - {len(collectors)} records")
            except Exception as e:
                logger.error(f"   ❌ CSV validation failed!")
                logger.error(f"\n{str(e)}\n")
                logger.error("=" * 70)
                logger.error("CSV VALIDATION ERROR")
                logger.error("=" * 70)
                logger.error("\nYour CSV has data issues that must be fixed before uploading.")
                logger.error("\nTo diagnose and fix the issue:")
                logger.error(f"  python validate_csv.py {csv_file} --fix --eclipse-type {eclipse_type}")
                logger.error("\nOr manually check:")
                logger.error("  - All required columns have values")
                logger.error("  - 'Local Eclipse Type' is 'Annular', 'Total', or 'Partial'")
                logger.error("  - Coordinates are valid numbers")
                logger.error("  - No empty required fields")
                sys.exit(1)
    
    if args.dry_run:
        logger.info("\n✅ Dry run complete - configuration is valid")
        sys.exit(0)
    
    # Confirm before proceeding
    print("\n⚠️  You are about to upload datasets to Zenodo.")
    print("   This will create REAL records on Zenodo.")
    response = input("\nProceed? (yes/no): ")
    
    if response.lower() != 'yes':
        logger.info("❌ Upload cancelled by user")
        sys.exit(0)
    
    # Run upload
    try:
        stats = await upload_datasets(
            annular_dir=annular_dir,
            annular_data_collector_csv=annular_csv,
            total_dir=total_dir,
            total_data_collector_csv=total_csv,
            related_identifiers_csv=related_identifiers_csv,
            references_csv=references_csv,
            successful_results_file=uploads_config.get('successful_results_file'),
            failure_results_file=uploads_config.get('failure_results_file'),
            delete_failures=uploads_config.get('delete_failures', False),
            auto_publish=uploads_config.get('auto_publish', False),
        )
        
        # Display summary
        logger.info("\n" + "=" * 70)
        logger.info("UPLOAD SUMMARY")
        logger.info("=" * 70)
        logger.info(f"Total processed: {stats['total_processed']}")
        logger.info(f"✅ Successful:   {stats['successful']}")
        logger.info(f"❌ Failed:       {stats['failed']}")
        logger.info(f"⏭️  Skipped:      {stats['skipped']}")
        logger.info("=" * 70)
        
        if stats['failed'] > 0:
            logger.warning(f"\n⚠️  {stats['failed']} upload(s) failed")
            logger.info(f"   Check {uploads_config.get('failure_results_file')} for details")
        
        if stats['successful'] > 0:
            logger.info(f"\n✅ {stats['successful']} upload(s) successful")
            logger.info(f"   Results saved to {uploads_config.get('successful_results_file')}")
        
        sys.exit(0 if stats['failed'] == 0 else 1)
        
    except KeyboardInterrupt:
        logger.warning("\n\n⚠️  Upload interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.error(f"\n❌ Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
