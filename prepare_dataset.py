#!/usr/bin/env python3
"""
AZUS Dataset Preparation Script

This script takes a folder with WAV files and CONFIG.txt and prepares
a complete dataset ready for upload to Zenodo.

Input:  ESID#005/ (folder with .WAV files and CONFIG.TXT)
Output: Complete dataset with all required files

Usage:
    python prepare_dataset.py ESID#005 --collector-csv collectors.csv --eclipse-type total
    
What it creates:
    - ESID_005.zip (ZIP of all WAV files + CONFIG.TXT)
    - README.html (dataset description)
    - README.md (markdown version)
    - file_list.csv (list of all files with hashes)
    - total_eclipse_data.csv (metadata for this ESID)
    - All data dictionary files
    - License.txt
    - AudioMoth_Operation_Manual.pdf (if available)
"""

import os
import sys
import csv
import hashlib
import shutil
import zipfile
import argparse
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime


def calculate_sha512(filepath: str) -> str:
    """Calculate SHA-512 hash of a file."""
    sha512_hash = hashlib.sha512()
    
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            sha512_hash.update(chunk)
    
    return sha512_hash.hexdigest()


def get_esid_from_folder(folder_name: str) -> str:
    """Extract ESID number from folder name."""
    # Handle ESID#005 or ESID_005 or just 005
    folder_name = folder_name.replace('#', '_')
    
    if 'ESID_' in folder_name:
        return folder_name.split('ESID_')[1].split('_')[0]
    elif 'ESID' in folder_name:
        return folder_name.split('ESID')[1].split('_')[0]
    else:
        # Assume it's just the number
        return folder_name.strip()


def create_zip_file(source_dir: Path, output_dir: Path, esid: str) -> Path:
    """
    Create ZIP file containing all WAV files and CONFIG.TXT.
    
    Args:
        source_dir: Directory containing WAV files
        output_dir: Where to save the ZIP
        esid: ESID number
        
    Returns:
        Path to created ZIP file
    """
    zip_filename = f"ESID_{esid}.zip"
    zip_path = output_dir / zip_filename
    
    print(f"\n📦 Creating ZIP file: {zip_filename}")
    
    # Find all WAV files and CONFIG.TXT
    wav_files = sorted(source_dir.glob("*.WAV")) + sorted(source_dir.glob("*.wav"))
    config_file = source_dir / "CONFIG.TXT"
    
    if not config_file.exists():
        config_file = source_dir / "CONFIG.txt"
    
    if not config_file.exists():
        print(f"   ⚠️  Warning: CONFIG.TXT not found in {source_dir}")
    
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        # Add CONFIG.TXT
        if config_file.exists():
            zipf.write(config_file, config_file.name)
            print(f"   ✅ Added CONFIG.TXT")
        
        # Add all WAV files
        for i, wav_file in enumerate(wav_files, 1):
            zipf.write(wav_file, wav_file.name)
            if i % 100 == 0:
                print(f"   ... added {i} WAV files")
        
        print(f"   ✅ Added {len(wav_files)} WAV file(s)")
    
    zip_size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"   ✅ ZIP created: {zip_size_mb:.2f} MB")
    
    return zip_path


def extract_collector_data(csv_file: Path, esid: str) -> Optional[Dict[str, str]]:
    """
    Extract collector data for specific ESID from CSV.
    
    Args:
        csv_file: Path to collectors CSV
        esid: ESID to extract
        
    Returns:
        Dictionary with collector data or None
    """
    print(f"\n📋 Extracting collector data for ESID {esid}")
    
    if not csv_file.exists():
        print(f"   ❌ Collector CSV not found: {csv_file}")
        return None
    
    with open(csv_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        
        for row in reader:
            if row.get('ESID') == esid:
                print(f"   ✅ Found collector data")
                return row
    
    print(f"   ❌ No collector data found for ESID {esid}")
    return None


def create_single_collector_csv(collector_data: Dict[str, str], output_dir: Path) -> Path:
    """
    Create total_eclipse_data.csv with single row for this ESID.
    
    Args:
        collector_data: Row data from main CSV
        output_dir: Where to save file
        
    Returns:
        Path to created CSV
    """
    output_file = output_dir / "total_eclipse_data.csv"
    
    print(f"\n📄 Creating total_eclipse_data.csv")
    
    with open(output_file, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=collector_data.keys())
        writer.writeheader()
        writer.writerow(collector_data)
    
    print(f"   ✅ Created: {output_file.name}")
    
    return output_file


def create_file_list(output_dir: Path, esid: str, source_dir: Path) -> Path:
    """
    Create file_list.csv listing all files in the dataset.
    
    Args:
        output_dir: Output directory
        esid: ESID number
        source_dir: Source directory with WAV files
        
    Returns:
        Path to created file_list.csv
    """
    output_file = output_dir / "file_list.csv"
    
    print(f"\n📋 Creating file_list.csv")
    
    # Header
    headers = [
        "File Name",
        "File Type",
        "Description",
        "File size (KB)",
        "Associated Data Dictionary",
        "SHA-512 Hash",
        "Notes"
    ]
    
    rows = []
    
    # Add main ZIP file
    zip_file = output_dir / f"ESID_{esid}.zip"
    if zip_file.exists():
        size_kb = zip_file.stat().st_size / 1024
        sha512 = calculate_sha512(str(zip_file))
        rows.append({
            "File Name": zip_file.name,
            "File Type": "ZIP Archive (.zip)",
            "Description": "Compressed archive containing all AudioMoth WAV audio recordings and CONFIG.TXT configuration file for this data collection site.",
            "File size (KB)": f"{size_kb:.2f}",
            "Associated Data Dictionary": "N/A",
            "SHA-512 Hash": sha512,
            "Notes": "Extract to access individual WAV files and device configuration"
        })
        print(f"   ✅ Added ZIP file")
    
    # Add README.md
    readme_md = output_dir / "README.md"
    if readme_md.exists():
        size_kb = readme_md.stat().st_size / 1024
        sha512 = calculate_sha512(str(readme_md))
        rows.append({
            "File Name": "README.md",
            "File Type": "Markdown (.md)",
            "Description": "Human and machine-readable documentation describing the dataset, collection methodology, site location, eclipse timing, and data usage guidelines in Markdown format.",
            "File size (KB)": f"{size_kb:.2f}",
            "Associated Data Dictionary": "N/A",
            "SHA-512 Hash": sha512,
            "Notes": "View in any text editor or Markdown viewer"
        })
        print(f"   ✅ Added README.md")
    
    # Add metadata files
    metadata_files = [
        ("total_eclipse_data.csv", "Comma Separated Variable (.CSV)", 
         "Machine-readable metadata about this specific data collection site including ESID, location coordinates, eclipse timing, coverage percentage, and collector affiliations.",
         "2024_total_eclipse_data_data_dict.csv"),
        
        ("file_list.csv", "Comma Separated Variable (.CSV)",
         "A machine and human file that gives the following information on each file in the record: File Name, File Type, Description, File Size in kilobytes, Name of Associated Data Dictionary with the file, calculated SHA-512 Hash of the file as a unique identifier to insure data integrity during transfer and compression.",
         "file_list_data_dict.csv"),
        
        ("CONFIG.TXT", "Plain Text (.txt)",
         "AudioMoth device configuration file containing recording settings such as sample rate (Hz), gain level, firmware version, recording schedule, and other device parameters.",
         "CONFIG_data_dict.csv"),
        
        ("2024_total_eclipse_data_data_dict.csv", "Comma Separated Variable (.CSV)",
         "Data dictionary explaining all column headers, data types, valid values, and descriptions for the total_eclipse_data.csv file.",
         "N/A"),
        
        ("file_list_data_dict.csv", "Comma Separated Variable (.CSV)",
         "Data dictionary explaining all column headers, data types, and descriptions for the file_list.csv file.",
         "N/A"),
        
        ("CONFIG_data_dict.csv", "Comma Separated Variable (.CSV)",
         "Data dictionary explaining all parameters, values, and settings found in the CONFIG.TXT file.",
         "N/A"),
        
        ("WAV_data_dict.csv", "Comma Separated Variable (.CSV)",
         "Data dictionary explaining the WAV file format, naming convention (YYYYMMDD_HHMMSS), audio specifications, and metadata for the AudioMoth recordings.",
         "N/A"),
        
        ("License.txt", "Plain Text (.txt)",
         "Creative Commons Attribution 4.0 International (CC BY 4.0) license text specifying terms of use, attribution requirements, and rights granted for this dataset.",
         "N/A"),
        
        ("AudioMoth_Operation_Manual.pdf", "Portable Document Format (.PDF)",
         "Official AudioMoth device operation manual providing instructions for device setup, configuration, deployment, and data retrieval.",
         "N/A"),
    ]
    
    for filename, file_type, description, data_dict in metadata_files:
        file_path = output_dir / filename
        if file_path.exists():
            size_kb = file_path.stat().st_size / 1024
            sha512 = calculate_sha512(str(file_path))
            rows.append({
                "File Name": filename,
                "File Type": file_type,
                "Description": description,
                "File size (KB)": f"{size_kb:.2f}",
                "Associated Data Dictionary": data_dict,
                "SHA-512 Hash": sha512,
                "Notes": ""
            })
            print(f"   ✅ Added {filename}")
    
    # Add individual WAV files from source directory
    wav_files = sorted(source_dir.glob("*.WAV")) + sorted(source_dir.glob("*.wav"))
    
    print(f"   📝 Adding {len(wav_files)} WAV files...")
    
    for wav_file in wav_files:
        size_kb = wav_file.stat().st_size / 1024
        sha512 = calculate_sha512(str(wav_file))
        rows.append({
            "File Name": wav_file.name,
            "File Type": "Waveform Audio File Format (.WAV)",
            "Description": "A WAV formatted file generated, machine readable by the AudioMoth device containing the audio data recordings at a site. The recording start time is stamped into the filename using a YYYYMMDD_HHMMSS format, where: YYYY is the four digit year, MM is the two digit month, DD is the two digit date, HH is the two digit hour, mm is the two digit minutes, ss is the two digit seconds.",
            "File size (KB)": f"{size_kb:.2f}",
            "Associated Data Dictionary": "WAV_data_dict.csv",
            "SHA-512 Hash": sha512,
            "Notes": ""
        })
    
    print(f"   ✅ Added all WAV files")
    
    # Write CSV
    with open(output_file, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)
    
    print(f"   ✅ Created: {output_file.name} ({len(rows)} files)")
    
    return output_file


def create_readme_html(collector_data: Dict[str, str], output_dir: Path) -> Path:
    """
    Create README.html with dataset description.
    
    Args:
        collector_data: Collector metadata
        output_dir: Output directory
        
    Returns:
        Path to created README.html
    """
    output_file = output_dir / "README.html"
    
    print(f"\n📄 Creating README.html")
    
    # Extract key data
    esid = collector_data.get('ESID', 'Unknown')
    latitude = collector_data.get('Latitude', 'Unknown')
    longitude = collector_data.get('Longitude', 'Unknown')
    eclipse_type = collector_data.get('Local Eclipse Type', 'Total')
    coverage = collector_data.get('Eclipse Percent (%)', 'Unknown')
    eclipse_date = collector_data.get('Eclipse Date', 'Unknown')
    
    # Parse date
    try:
        eclipse_dt = datetime.strptime(eclipse_date, '%Y-%m-%d')
        formatted_date = eclipse_dt.strftime('%B %d, %Y')
        year = eclipse_dt.year
    except:
        formatted_date = eclipse_date
        year = '2024'
    
    # Create HTML
    html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Eclipse Soundscapes - ESID #{esid} Dataset</title>
</head>
<body>
<h1>{eclipse_date} {eclipse_type} Solar Eclipse - ESID #{esid}</h1>

<p>These are audio recordings taken by an Eclipse Soundscapes (ES) Data Collector during the week of the {formatted_date} {eclipse_type} Solar Eclipse.</p>

<h2>Data Site Location Information</h2>
<ul>
<li><strong>ESID:</strong> {esid}</li>
<li><strong>Latitude:</strong> {latitude}</li>
<li><strong>Longitude:</strong> {longitude}</li>
<li><strong>Type of Eclipse:</strong> {eclipse_type} Solar Eclipse</li>
<li><strong>Eclipse Coverage:</strong> {coverage}%</li>
<li><strong>WAV Files Time &amp; Date Settings:</strong> {collector_data.get('WAV Files Time & Date Settings', 'See CONFIG.TXT')}</li>
</ul>

<h2>Included Data</h2>
<ul>
<li><strong>Audio files in WAV format</strong> with the date and time in UTC within the file name: YYYYMMDD_HHMMSS meaning YearMonthDay_HourMinuteSecond<br>
For example, 20240411_141600.WAV means that this audio file starts on April 11, 2024 at 14:16:00 Coordinated Universal Time (UTC)</li>
<li><strong>CONFIG.TXT file:</strong> Includes AudioMoth device setting information, such as sample rate in Hertz (Hz), gain, firmware, etc.</li>
</ul>

<h2>Eclipse Information for This Location</h2>
<ul>
<li><strong>Eclipse Date:</strong> {collector_data.get('Eclipse Date', 'N/A')}</li>
<li><strong>Eclipse Start Time (UTC):</strong> {collector_data.get('Eclipse Start Time (UTC) (1st Contact)', 'N/A')}</li>
<li><strong>Totality Start Time (UTC):</strong> {collector_data.get('Totality Start Time (UTC) (2nd Contact)', 'N/A')}</li>
<li><strong>Eclipse Maximum (UTC):</strong> {collector_data.get('Eclipse Maximum (UTC)', 'N/A')}</li>
<li><strong>Totality End Time (UTC):</strong> {collector_data.get('Totality End Time (UTC) (3rd Contact)', 'N/A')}</li>
<li><strong>Eclipse End Time (UTC):</strong> {collector_data.get('Eclipse End Time (UTC) (4th Contact)', 'N/A')}</li>
</ul>

<h2>Audio Data Collection During Eclipse Week</h2>
<p>ES Data Collectors used AudioMoth devices to record audio data, known as soundscapes, over a 5-day period during the eclipse week: 2 days before the eclipse, the day of the eclipse, and 2 days after. The complete raw audio data collected by the Data Collector at the location mentioned above is provided here. This data may or may not cover the entire requested timeframe due to factors such as availability, technical issues, or other unforeseen circumstances.</p>

<h2>ES ID# Information</h2>
<p>Each AudioMoth recording device was assigned a unique Eclipse Soundscapes Identification Number (ES ID#). This identifier connects the audio data, submitted via a MicroSD card, with the latitude and longitude information provided by the data collector through an online form. The ES team used the ES ID# to link the audio data with its corresponding location information and then uploaded this raw audio data and location details to Zenodo. This process ensures the anonymity of the ES Data Collectors while allowing them to easily search for and access their audio data on Zenodo.</p>

<h2>TimeStamp Information</h2>
<p>The ES team and the Data Collectors took care to set the date and time on the AudioMoth recording devices using an AudioMoth time chime before deployment, ensuring that the recordings would have an automatic timestamp. However, participants also manually noted the date and start time as a backup in case the time chime setup failed. The notes above indicate whether the WAV audio files for this site were timestamped manually or with the automated AudioMoth time chime.</p>

<h2>Common Timestamp Error</h2>
<p>Some AudioMoth devices experienced a malfunction where the timestamp on audio files reverted to a date in 1970 or before, even after initially recording correctly. Despite this issue, the affected data was still included in this ES site's collected raw audio dataset.</p>

<h2>Latitude &amp; Longitude Information</h2>
<p>The latitude and longitude for each site was taken manually by data collectors and submitted to the ES team, either via a web form or on paper. It is shared in Decimal Degrees format.</p>

<h2>General Project Information</h2>
<p>The Eclipse Soundscapes Project is a NASA Volunteer Science project funded by NASA Science Activation that is studying how eclipses affect life on Earth during the October 14, 2023 annular solar eclipse and the April 8, 2024 total solar eclipse. Eclipse Soundscapes revisits an eclipse study from almost 100 years ago that showed that animals and insects are affected by solar eclipses! Like this study from 100 years ago, ES asked for the public's help. ES uses modern technology to continue to study how solar eclipses affect life on Earth! You can learn more at www.EclipseSoundscapes.org.</p>

<p>Eclipse Soundscapes is an enterprise of ARISA Lab, LLC and is supported by NASA award No. 80NSSC21M0008.</p>

<h2>Eclipse Data Version Definitions</h2>
<p>{{1st digit = year, 2nd digit = Eclipse type (1=Total Solar Eclipse, 9=Annular Solar Eclipse, 0=Partial Solar Eclipse), 3rd digit is unused and in place for future use}}</p>
<ul>
<li><strong>2023.9.0</strong> = Week of October 14, 2023 Annular Eclipse Audio Data, Path of Annularity (Annular Eclipse)</li>
<li><strong>2023.0.0</strong> = Week of October 14, 2023 Annular Eclipse Audio Data, OFF the Path of Annularity (Partial Eclipse)</li>
<li><strong>2024.1.0</strong> = Week of April 8, 2024 Total Solar Eclipse Audio Data, Path of Totality (Total Solar Eclipse)</li>
<li><strong>2024.0.0</strong> = Week of April 8, 2024 Total Solar Eclipse Audio Data, OFF the Path of Totality (Partial Solar Eclipse)</li>
</ul>
<p><em>*Please note that this dataset's version number is: {collector_data.get('Version', '2024.1.0')}</em></p>

<h2>Individual Site Citation (APA 7th edition)</h2>
<p>ARISA Lab, L.L.C., Winter, H., Severino, M., &amp; Volunteer Scientist. (2025). <i>{year} solar eclipse soundscapes audio data</i> [Audio dataset, ES ID# {esid}]. Zenodo. {{Insert DOI}}<br>
Collected by volunteer scientists as part of the Eclipse Soundscapes Project.<br>
This project is supported by NASA award No. 80NSSC21M0008.</p>

<h2>Eclipse Community Citation</h2>
<p>ARISA Lab, L.L.C., Winter, H., Severino, M., &amp; Volunteer Scientists. <i>2023 and 2024 solar eclipse soundscapes audio data</i> [Collection of audio datasets]. Eclipse Soundscapes Community, Zenodo. <a href="https://zenodo.org/communities/eclipsesoundscapes/">https://zenodo.org/communities/eclipsesoundscapes/</a><br>
Collected by volunteer scientists as part of the Eclipse Soundscapes Project.<br>
This project is supported by NASA award No. 80NSSC21M0008.</p>

</body>
</html>
"""
    
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(html_content)
    
    print(f"   ✅ Created: {output_file.name}")
    
    return output_file


def create_readme_md(readme_html: Path, output_dir: Path) -> Path:
    """
    Create README.md from README.html.
    
    Args:
        readme_html: Path to README.html
        output_dir: Output directory
        
    Returns:
        Path to created README.md
    """
    output_file = output_dir / "README.md"
    
    print(f"\n📄 Creating README.md")
    
    try:
        import html2text
        
        with open(readme_html, 'r', encoding='utf-8') as f:
            html_content = f.read()
        
        h = html2text.HTML2Text()
        h.body_width = 0  # Don't wrap lines
        
        markdown = h.handle(html_content)
        
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(markdown)
        
        print(f"   ✅ Created: {output_file.name}")
        
    except ImportError:
        print(f"   ⚠️  html2text not installed, creating simple markdown")
        
        # Simple conversion - strip HTML tags
        with open(readme_html, 'r', encoding='utf-8') as f:
            html_content = f.read()
        
        # Very basic HTML stripping
        import re
        text = re.sub('<[^<]+?>', '', html_content)
        
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(text)
        
        print(f"   ✅ Created simple markdown: {output_file.name}")
    
    return output_file


def copy_resource_files(resources_dir: Path, output_dir: Path):
    """
    Copy standard resource files (data dictionaries, license, manual).
    
    Args:
        resources_dir: Directory with resource files
        output_dir: Output directory
    """
    print(f"\n📁 Copying resource files")
    
    resource_files = [
        "2024_total_eclipse_data_data_dict.csv",
        "file_list_data_dict.csv",
        "CONFIG_data_dict.csv",
        "WAV_data_dict.csv",
        "License.txt",
        "AudioMoth_Operation_Manual.pdf",
    ]
    
    copied = 0
    missing = []
    
    for filename in resource_files:
        src = resources_dir / filename
        dst = output_dir / filename
        
        if src.exists():
            shutil.copy2(src, dst)
            print(f"   ✅ Copied: {filename}")
            copied += 1
        else:
            print(f"   ⚠️  Missing: {filename}")
            missing.append(filename)
    
    print(f"\n   Summary: Copied {copied}/{len(resource_files)} files")
    
    if missing:
        print(f"   ⚠️  Missing files (optional): {', '.join(missing)}")


def main():
    parser = argparse.ArgumentParser(
        description='Prepare AZUS dataset from raw WAV files'
    )
    parser.add_argument(
        'folder',
        help='Folder containing WAV files (e.g., ESID#005)'
    )
    parser.add_argument(
        '--collector-csv',
        required=True,
        help='Path to collectors CSV file'
    )
    parser.add_argument(
        '--eclipse-type',
        choices=['total', 'annular'],
        default='total',
        help='Eclipse type (default: total)'
    )
    parser.add_argument(
        '--resources-dir',
        help='Directory with resource files (data dictionaries, license, etc.)',
        default='Resources'
    )
    parser.add_argument(
        '--output-dir',
        help='Output directory (default: create ESID_XXX_Staging folder)'
    )
    
    args = parser.parse_args()
    
    # Paths
    source_dir = Path(args.folder)
    collector_csv = Path(args.collector_csv)
    resources_dir = Path(args.resources_dir)
    
    if not source_dir.exists():
        print(f"❌ Error: Source folder not found: {source_dir}")
        sys.exit(1)
    
    if not collector_csv.exists():
        print(f"❌ Error: Collector CSV not found: {collector_csv}")
        sys.exit(1)
    
    # Extract ESID
    esid = get_esid_from_folder(source_dir.name)
    
    # Create output directory
    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        output_dir = source_dir.parent / f"ESID_{esid}_Staging"
    
    output_dir.mkdir(exist_ok=True, parents=True)
    
    print("=" * 70)
    print("AZUS DATASET PREPARATION")
    print("=" * 70)
    print(f"\nESID:           {esid}")
    print(f"Source:         {source_dir}")
    print(f"Output:         {output_dir}")
    print(f"Collector CSV:  {collector_csv}")
    print(f"Eclipse type:   {args.eclipse_type}")
    
    # Step 1: Extract collector data
    collector_data = extract_collector_data(collector_csv, esid)
    
    if not collector_data:
        print("\n❌ Cannot proceed without collector data")
        print("\nMake sure:")
        print(f"  1. ESID {esid} exists in {collector_csv}")
        print(f"  2. CSV has correct headers")
        sys.exit(1)
    
    # Step 2: Create ZIP file
    zip_path = create_zip_file(source_dir, output_dir, esid)
    
    # Step 3: Create single-row collector CSV
    create_single_collector_csv(collector_data, output_dir)
    
    # Step 4: Copy resource files
    if resources_dir.exists():
        copy_resource_files(resources_dir, output_dir)
    else:
        print(f"\n⚠️  Resources directory not found: {resources_dir}")
        print(f"   Some files may be missing")
    
    # Step 5: Create README.html
    readme_html = create_readme_html(collector_data, output_dir)
    
    # Step 6: Create README.md
    create_readme_md(readme_html, output_dir)
    
    # Step 7: Create file_list.csv (must be last since it hashes all files)
    create_file_list(output_dir, esid, source_dir)
    
    # Summary
    print("\n" + "=" * 70)
    print("✅ DATASET PREPARATION COMPLETE")
    print("=" * 70)
    
    print(f"\n📁 Output directory: {output_dir}")
    print(f"\nFiles created:")
    
    for file in sorted(output_dir.iterdir()):
        size_mb = file.stat().st_size / (1024 * 1024)
        print(f"   ✅ {file.name:<50s} ({size_mb:>8.2f} MB)")
    
    print(f"\n📦 Ready for upload to Zenodo!")
    print(f"\nNext steps:")
    print(f"  1. Verify files in: {output_dir}")
    print(f"  2. Update config.json to point to: {output_dir.parent}")
    print(f"  3. Run: python standalone_upload.py")


if __name__ == "__main__":
    main()
