"""Standalone tasks for AZUS without Prefect dependencies.

This module contains all the task functions from tasks.py but without
Prefect decorators, allowing them to run independently.
"""

import os
import glob
import csv
import zipfile
import tempfile
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any
from datetime import datetime

# Import models (no Prefect dependency)
from models.invenio import (
    Metadata,
    Identifier,
    PersonOrganization,
    Affiliation,
    Role,
    Creator,
    ResourceType,
    License,
    Language,
    Date,
    DateType,
    Funder,
    Award,
    AwardTitle,
    Funding,
    Subject,
    Contributor,
    RelatedIdentifier,
)

from models.audiomoth import (
    EclipseType,
    DataCollector,
    UploadData,
    PersistedResult,
)

UPLOAD_DATE_FORMAT = "%Y-%m-%d"


async def save_result_csv(file: str, result: PersistedResult) -> None:
    """
    Save an upload result to a local CSV file.
    
    Args:
        file: The CSV file to add the result
        result: The upload result
    """
    if not file:
        raise ValueError("Invalid file")
    
    output_file = Path(file)
    new_file = False
    
    if not output_file.exists():
        print(f"Creating CSV file {file}")
        new_file = True
        output_file.parent.mkdir(exist_ok=True, parents=True)
    
    result_dict = result.model_dump()
    headers = result_dict.keys()
    
    with open(file, mode="a", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=headers)
        if new_file:
            writer.writeheader()
        writer.writerow(result_dict)


async def get_recording_dates(zip_file: str) -> Tuple[str, str]:
    """
    Retrieve the start and end date of a dataset recording.
    
    This function reads WAV filenames from the ZIP without extracting
    to avoid temp directory space issues.
    
    Args:
        zip_file: A zipped dataset file
        
    Returns:
        Tuple containing the start and end date
    """
    if not zip_file:
        raise ValueError("Invalid file")
    
    if not os.path.exists(zip_file):
        raise ValueError(f"File does not exist: {zip_file}")
    
    # Open ZIP and read filenames WITHOUT extracting
    with zipfile.ZipFile(zip_file, "r") as zip_ref:
        # Get list of files in ZIP
        all_files = zip_ref.namelist()
        
        # Filter for .WAV files
        wav_files = [
            Path(file).stem for file in all_files if file.lower().endswith(".wav")
        ]
        
        # Parse file names and extract dates
        dates: List[datetime.date] = []
        for file in wav_files:
            try:
                # Extract date from filename (YYYYMMDD_HMS format)
                date_str = file.split("_")[0]
                date = datetime.strptime(date_str, "%Y%m%d").date()
                if date.year < 2023:
                    # Don't consider dates before 2023
                    continue
                dates.append(date)
            except (ValueError, IndexError):
                # Ignore files that don't match expected format
                continue
        
        if not dates:
            raise ValueError("No valid dates found in the .WAV file names.")
        
        # Find earliest and latest dates
        earliest_date = min(dates)
        latest_date = max(dates)
        
        # Return formatted dates
        return (
            earliest_date.strftime(UPLOAD_DATE_FORMAT),
            latest_date.strftime(UPLOAD_DATE_FORMAT),
        )


async def read_upload_manifest(
    manifest_path: Path,
    dataset_dir: Path
) -> Dict[str, Optional[str]]:
    """
    Read ESID_XXX_to_upload.csv and find all listed files.
    
    Args:
        manifest_path: Path to the manifest CSV
        dataset_dir: Directory to search for files
        
    Returns:
        Dictionary mapping filenames to their full paths
    """
    print(f"📋 Reading upload manifest: {manifest_path.name}")
    
    files_to_upload = []
    
    with open(manifest_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        
        if 'File Name' not in reader.fieldnames:
            raise ValueError(
                f"Manifest CSV missing 'File Name' column. "
                f"Found columns: {reader.fieldnames}"
            )
        
        for row in reader:
            filename = row.get('File Name', '').strip()
            if filename:
                files_to_upload.append(filename)
    
    print(f"   ✅ Manifest lists {len(files_to_upload)} files to upload")
    
    # Find each file
    found_files = {}
    missing_files = []
    
    for filename in files_to_upload:
        file_path = dataset_dir / filename
        
        if file_path.exists() and file_path.is_file():
            found_files[filename] = str(file_path)
        else:
            found_files[filename] = None
            missing_files.append(filename)
    
    # Log summary
    found_count = len([f for f in found_files.values() if f is not None])
    
    print(f"   ✅ Found {found_count}/{len(files_to_upload)} files")
    
    if missing_files:
        print(f"   ⚠️  Missing {len(missing_files)} files:")
        for filename in missing_files[:5]:
            print(f"      - {filename}")
        if len(missing_files) > 5:
            print(f"      ... and {len(missing_files) - 5} more")
        
        # This is a critical error - files in manifest must exist
        raise FileNotFoundError(
            f"Missing {len(missing_files)} files listed in manifest. "
            f"First missing: {missing_files[0]}"
        )
    
    return found_files


async def find_dataset_files(
    zip_file_path: str,
    required_files: Optional[List[str]] = None
) -> Dict[str, Optional[str]]:
    """
    Find all files associated with a dataset based on the ZIP file location.
    
    This function now checks for ESID_XXX_to_upload.csv manifest first.
    If found, uses that to determine files. Otherwise falls back to default behavior.
    
    Args:
        zip_file_path: Path to the main ZIP file
        required_files: List of filenames to look for (used if no manifest)
        
    Returns:
        Dictionary mapping filenames to their full paths
    """
    # Validate input
    zip_path = Path(zip_file_path)
    
    if not zip_path.exists():
        raise FileNotFoundError(f"ZIP file not found: {zip_file_path}")
    
    if not zip_path.is_file():
        raise ValueError(f"Path is not a file: {zip_file_path}")
    
    # Get the directory containing the ZIP file
    dataset_dir = zip_path.parent
    
    # Extract ESID from ZIP filename (e.g., "ESID_005.zip" -> "005")
    zip_name = zip_path.stem
    if zip_name.startswith("ESID_"):
        esid = zip_name.replace("ESID_", "").split("_")[0]
    else:
        esid = None
    
    # Look for upload manifest
    if esid:
        manifest_path = dataset_dir / f"ESID_{esid}_to_upload.csv"
        
        if manifest_path.exists():
            print(f"✅ Found upload manifest: {manifest_path.name}")
            return await read_upload_manifest(manifest_path, dataset_dir)
    
    # No manifest found, use default file discovery
    print(f"ℹ️  No upload manifest found, using default file discovery")
    
    # Default list of required files
    if required_files is None:
        required_files = [
            "README.html",
            "README.md",
            "2024_total_eclipse_data_data_dict.csv",
            "AudioMoth_Operation_Manual.pdf",
            "CONFIG.TXT",
            "CONFIG_data_dict.csv",
            "License.txt",
            "WAV_data_dict.csv",
            "file_list.csv",
            "file_list_data_dict.csv",
            "total_eclipse_data.csv",
        ]
    
    found_files = {}
    missing_files = []
    
    # Search for each required file
    for filename in required_files:
        file_path = dataset_dir / filename
        
        if file_path.exists() and file_path.is_file():
            found_files[filename] = str(file_path)
        else:
            found_files[filename] = None
            missing_files.append(filename)
    
    # Log summary
    found_count = len([f for f in found_files.values() if f is not None])
    total_count = len(required_files)
    
    print(f"Found {found_count}/{total_count} files for {zip_path.name}")
    
    if missing_files:
        print(f"Warning: Missing {len(missing_files)} files: {', '.join(missing_files[:5])}")
    
    return found_files


async def rename_dir_files(directory: str) -> None:
    """
    Rename all files in directory, replacing '#' with '_'.
    
    Args:
        directory: A local directory
    """
    if not directory:
        raise ValueError("Invalid directory")
    
    if not os.path.isdir(directory):
        raise ValueError(f"Invalid directory: {directory}")
    
    for root, _, files in os.walk(directory):
        for file in files:
            if file.startswith("ESID#") and file.endswith("zip"):
                old_path = os.path.join(root, file)
                new_file_name = file.replace("#", "_")
                new_path = os.path.join(root, new_file_name)
                os.rename(old_path, new_path)


async def list_dir_files(
    directory: str, file_pattern: Optional[str] = "*"
) -> List[str]:
    """
    Retrieve all files in a directory matching the given pattern.
    
    Args:
        directory: A local directory
        file_pattern: A glob pattern to match file names
        
    Returns:
        List of matching files
    """
    if not directory:
        raise ValueError("A valid directory path must be provided")
    
    if not os.path.isdir(directory):
        raise ValueError(f"Invalid directory: {directory}")
    
    search_pattern = os.path.join(directory, file_pattern)
    matching_files = glob.glob(search_pattern, recursive=False)
    
    # Filter out directories, keeping only files
    files = [file for file in matching_files if os.path.isfile(file)]
    
    return files


async def get_esid_file_pairs(files: List[str]) -> List[Tuple[str, str]]:
    """
    Retrieve ESID from file names and create pairs.
    
    Args:
        files: A list of file paths
        
    Returns:
        List of (ESID, file) pairs
    """
    pairs = [(Path(file).stem.split("_")[-1].strip(), file) for file in files]
    return pairs


async def create_upload_data(
    esid_file_pairs: List[Tuple[str, str]],
    data_collectors: List[DataCollector],
) -> Tuple[List[UploadData], List[str]]:
    """
    Combine ESIDs, data files and collectors into upload data.
    
    Args:
        esid_file_pairs: List of (ESID, zip_file) pairs
        data_collectors: List of data collectors
        
    Returns:
        Tuple of upload data list and unmatched ESIDs
    """
    if len(esid_file_pairs) > len(data_collectors):
        print(
            f"Warning: Number of ES IDs ({len(esid_file_pairs)}) > "
            f"number of collectors ({len(data_collectors)})"
        )
    
    upload_data = []
    unmatched_ids = []
    
    # Create lookup dictionary
    collector_dict = {dc.esid: dc for dc in data_collectors}
    
    for esid, zip_file in esid_file_pairs:
        # Check if we have collector info
        if esid not in collector_dict:
            print(f"Warning: No collector info found for ESID: {esid}")
            unmatched_ids.append(esid)
            continue
        
        # Find all associated files
        dataset_files = await find_dataset_files(zip_file)
        
        # Prepare additional files list
        additional_files = [
            path for filename, path in dataset_files.items()
            if path and filename not in ["README.html", "README.md"]
        ]
        
        # Create upload data
        data = UploadData(
            esid=esid,
            data_collector=collector_dict[esid],
            zip_file=zip_file,
            readme_html=dataset_files.get("README.html"),
            readme_md=dataset_files.get("README.md"),
            additional_files=additional_files,
        )
        
        print(
            f"Prepared ESID {esid}: "
            f"ZIP + README.md + {len(additional_files)} files = "
            f"{len(data.all_files)} total"
        )
        
        # Warn if README.html missing
        if not data.readme_html:
            print(f"Warning: ESID {esid} - README.html not found")
        
        upload_data.append(data)
    
    return (upload_data, unmatched_ids)


async def parse_collectors_csv(
    csv_file_path: str, eclipse_type: EclipseType
) -> List[DataCollector]:
    """
    Parse a CSV file for all data collectors.
    
    Args:
        csv_file_path: A CSV file path
        eclipse_type: The type of eclipse
        
    Returns:
        List of data collectors
    """
    with open(csv_file_path, mode="r", encoding="utf-8") as csv_file:
        csv_reader = csv.DictReader(csv_file)
        
        # Validate headers
        csv_headers = csv_reader.fieldnames
        if not csv_headers:
            raise ValueError("No headers found in the CSV file.")
        
        expected_headers = [
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
        
        if eclipse_type == EclipseType.TOTAL:
            expected_headers.extend(
                ["Totality Start Time (UTC) (2nd Contact)", "Totality End Time (UTC) (3rd Contact)"]
            )
        
        missing_headers = set(expected_headers) - set(csv_headers)
        if missing_headers:
            raise ValueError(f"Expected CSV headers not found: {missing_headers}")
        
        data = [DataCollector.model_validate(row) for row in csv_reader]
        
        print(f"Parsed {len(data)} rows from CSV")
        
        return data


async def get_draft_config(
    data_collector: DataCollector,
    readme_html_path: Optional[str] = None,
    related_identifiers_csv: Optional[str] = None,
    references_csv: Optional[str] = None,
    reserve_doi: bool = False
):
    """
    Create a draft record configuration.
    
    Args:
        data_collector: A data collector
        readme_html_path: Path to README.html file
        related_identifiers_csv: Path to CSV with related identifiers (citations, related works)
        references_csv: Path to CSV with bibliographic references
        reserve_doi: Whether to reserve a DOI for this record (default: False)
        
    Returns:
        DraftConfig object
    """
    # Import here to avoid circular dependency
    from prefect_invenio_rdm.models.records import DraftConfig, Access
    
    # Get description from README.html - REQUIRED
    if not readme_html_path:
        raise ValueError(
            "README.html path is required but was not provided.\n"
            "Please run prepare_dataset.py first to generate README.html,\n"
            "or provide a valid readme_html_path parameter."
        )
    
    if not Path(readme_html_path).exists():
        raise FileNotFoundError(
            f"README.html file not found at: {readme_html_path}\n"
            f"Please run prepare_dataset.py to generate README.html before uploading.\n"
            f"Expected location: {readme_html_path}"
        )
    
    print(f"✅ Using description from README.html: {readme_html_path}")
    try:
        description = Path(readme_html_path).read_text(encoding='utf-8')
    except Exception as e:
        raise RuntimeError(
            f"Failed to read README.html file at {readme_html_path}\n"
            f"Error: {e}\n"
            f"Please verify the file exists and has read permissions."
        )
    
    dates: Optional[List[Date]] = []
    if data_collector.first_recording_day:
        dates.append(
            Date(
                date=data_collector.first_recording_day,
                type=DateType(id="collected"),
                description="Day of first recording",
            )
        )
    if data_collector.last_recording_day:
        dates.append(
            Date(
                date=data_collector.last_recording_day,
                type=DateType(id="collected"),
                description="Day of last recording",
            )
        )
    
    creators = get_default_creators()
    
    affiliations = [
        Affiliation(name=affiliation)
        for affiliation in parse_values_from_str(data_collector.affiliation)
    ]
    
    creators.append(
        Creator(
            person_or_org=PersonOrganization(
                type="personal", given_name="", family_name="Volunteer Scientist"
            ),
            role=Role(id="datacollector"),
            affiliations=affiliations,
        )
    )
    
    subjects = [
        Subject(subject=subject)
        for subject in parse_values_from_str(data_collector.subjects)
    ]
    
    # Load related identifiers from CSV (if provided)
    related_identifiers = read_related_identifiers_from_csv(related_identifiers_csv)
    
    # Load references from CSV (if provided)
    references = read_references_from_csv(references_csv)
    
    # Configure DOI reservation based on parameter
    if reserve_doi:
        # Request DOI reservation from Zenodo
        pids = {"doi": {"provider": "datacite", "client": "zenodo"}}
        print(f"🔗 DOI reservation: ENABLED (Zenodo will assign a DOI)")
    else:
        # Do not request DOI reservation
        pids = {}
        print(f"🔗 DOI reservation: DISABLED (no DOI will be assigned)")
    
    metadata = Metadata(
        resource_type=ResourceType(id="dataset"),
        title=f"{data_collector.eclipse_date} {data_collector.eclipse_label()} ESID#{data_collector.esid}",
        publication_date=datetime.now().strftime(UPLOAD_DATE_FORMAT),
        creators=creators,
        description=description,
        funding=get_fundings(),
        rights=[License(id="cc-by-4.0")],
        languages=[Language(id="eng")],
        dates=dates,
        version=data_collector.version,
        publisher="Zenodo",
        subjects=subjects,
        related_identifiers=related_identifiers if related_identifiers else None,
        references=references if references else None,
        contributors=[
            Contributor(
                person_or_org=PersonOrganization(
                    type="personal",
                    given_name="Joel",
                    family_name="Goncalves",
                    identifiers=[
                        Identifier(scheme="orcid", identifier="0009-0009-0945-3544")
                    ],
                ),
                role=Role(id="projectmember"),
                affiliations=[Affiliation(name="ARISA Lab, L.L.C.")],
            ),
            Contributor(
                person_or_org=PersonOrganization(
                    type="personal",
                    given_name="Brent",
                    family_name="Pease",
                    identifiers=[
                        Identifier(scheme="orcid", identifier="0000-0003-1528-6075")
                    ],
                ),
                role=Role(id="researcher"),
                affiliations=[
                    Affiliation(
                        name="Southern Illinois University (SIU), Carbondale, United States"
                    )
                ],
            ),
        ],
    )
    
    return DraftConfig(
        record_access=Access.PUBLIC,
        files_access=Access.PUBLIC,
        files_enabled=True,
        metadata=metadata.to_dict(),
        community_id="2ca990ba-e151-4741-a456-6b80da71c69d",
        custom_fields={
            "code:codeRepository": "https://github.com/ARISA-Lab-LLC/ESCSP",
            "code:developmentStatus": {"id": "wip"},
            "code:programmingLanguage": [{"id": "python"}],
            "ac:captureDevice": ["AudioMoth v.1.1.0, Firmware 1.8"],
        },
        pids=pids,  # Use configured DOI reservation setting
    )


def parse_values_from_str(string: str, delimeter: str = ":") -> List[str]:
    """Parse values from a delimited string."""
    values = string.split(sep=delimeter)
    return [x.strip() for x in values]


def get_fundings() -> List[Funding]:
    """Retrieve project fundings."""
    return [
        Funding(
            funder=Funder(id="027ka1x80"),
            award=Award(
                title=AwardTitle(en="Eclipse Soundscapes: Citizen Science Project"),
                number="80NSSC21M0008",
                identifiers=[
                    Identifier(
                        scheme="url",
                        identifier="https://science.nasa.gov/sciact-team/eclipse-soundscapes/",
                    )
                ],
            ),
        )
    ]


def read_related_identifiers_from_csv(csv_path: Optional[str]) -> List[RelatedIdentifier]:
    """
    Read related identifiers (citations, related works) from a CSV file.
    
    The CSV should have these columns:
    - identifier: The identifier value (DOI, URL, arXiv ID, etc.)
    - scheme: The identifier scheme (doi, url, arxiv, isbn, pmid, handle, urn)
    - relation_type: The relationship type (cites, references, isSupplementTo, etc.)
    - resource_type: (Optional) The resource type (publication-article, dataset, software, etc.)
    
    Args:
        csv_path: Path to the CSV file. If None or empty, returns empty list.
        
    Returns:
        List of RelatedIdentifier objects, or empty list if file doesn't exist
        
    Example CSV:
        identifier,scheme,relation_type,resource_type
        10.1038/s41597-024-03940-2,doi,cites,publication-article
        https://eclipsesoundscapes.org,url,isSupplementTo,
        10.5281/zenodo.1234567,doi,references,dataset
    """
    # Return empty list if no path provided
    if not csv_path or not csv_path.strip():
        return []
    
    csv_file = Path(csv_path)
    
    # Return empty list if file doesn't exist
    if not csv_file.exists():
        print(f"ℹ️  Related identifiers CSV not found: {csv_path}")
        return []
    
    related_identifiers = []
    
    try:
        with open(csv_file, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            
            # Validate required columns
            required_cols = {"identifier", "scheme", "relation_type"}
            if not required_cols.issubset(set(reader.fieldnames or [])):
                print(f"⚠️  Related identifiers CSV missing required columns: {required_cols}")
                print(f"   Found columns: {reader.fieldnames}")
                return []
            
            for row_num, row in enumerate(reader, start=2):  # Start at 2 (header is 1)
                # Skip empty rows
                if not row.get("identifier") or not row.get("identifier").strip():
                    continue
                
                # Get resource type if provided
                resource_type = None
                resource_type_str = row.get("resource_type", "").strip()
                if resource_type_str:
                    resource_type = ResourceType(id=resource_type_str)
                
                try:
                    related_id = RelatedIdentifier(
                        identifier=row["identifier"].strip(),
                        scheme=row["scheme"].strip(),
                        relation_type=row["relation_type"].strip(),
                        resource_type=resource_type
                    )
                    related_identifiers.append(related_id)
                except Exception as e:
                    print(f"⚠️  Error parsing related identifier on row {row_num}: {e}")
                    print(f"   Row data: {row}")
                    continue
        
        print(f"✅ Loaded {len(related_identifiers)} related identifier(s) from {csv_file.name}")
        
    except Exception as e:
        print(f"⚠️  Error reading related identifiers CSV {csv_path}: {e}")
        return []
    
    return related_identifiers


def read_references_from_csv(csv_path: Optional[str]) -> List[str]:
    """
    Read references (bibliographic citations) from a CSV file.
    
    The CSV should have one column:
    - reference: The full citation string
    
    Args:
        csv_path: Path to the CSV file. If None or empty, returns empty list.
        
    Returns:
        List of reference strings, or empty list if file doesn't exist
        
    Example CSV:
        reference
        "Henshaw, W. D., et al. (2024). Eclipse Soundscapes Project Data. Scientific Data, 11(1), 1098."
        "Pease, B. P., et al. (2024). Audiovisual data during the April 8, 2024 total solar eclipse."
    """
    # Return empty list if no path provided
    if not csv_path or not csv_path.strip():
        return []
    
    csv_file = Path(csv_path)
    
    # Return empty list if file doesn't exist
    if not csv_file.exists():
        print(f"ℹ️  References CSV not found: {csv_path}")
        return []
    
    references = []
    
    try:
        with open(csv_file, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            
            # Check for reference column
            if "reference" not in (reader.fieldnames or []):
                print(f"⚠️  References CSV missing 'reference' column")
                print(f"   Found columns: {reader.fieldnames}")
                return []
            
            for row_num, row in enumerate(reader, start=2):
                # Skip empty rows
                ref = row.get("reference", "").strip()
                if ref:
                    references.append(ref)
        
        print(f"✅ Loaded {len(references)} reference(s) from {csv_file.name}")
        
    except Exception as e:
        print(f"⚠️  Error reading references CSV {csv_path}: {e}")
        return []
    
    return references


def get_default_creators() -> List[Creator]:
    """Retrieve the default list of creators."""
    return [
        Creator(
            person_or_org=PersonOrganization(
                type="organizational", name="ARISA Lab, L.L.C."
            ),
            role=Role(id="hostinginstitution"),
            affiliations=[
                Affiliation(name="National Aeronautics and Space Administration (NASA)")
            ],
        ),
        Creator(
            person_or_org=PersonOrganization(
                type="personal",
                given_name="Henry",
                family_name="Winter",
                identifiers=[
                    Identifier(scheme="orcid", identifier="0000-0002-6678-590X")
                ],
            ),
            role=Role(id="datamanager"),
            affiliations=[Affiliation(name="ARISA Lab, L.L.C.")],
        ),
        Creator(
            person_or_org=PersonOrganization(
                type="personal",
                given_name="MaryKay",
                family_name="Severino",
                identifiers=[
                    Identifier(scheme="orcid", identifier="0000-0002-2363-7421")
                ],
            ),
            role=Role(id="projectleader"),
            affiliations=[Affiliation(name="ARISA Lab, L.L.C.")],
        ),
    ]
