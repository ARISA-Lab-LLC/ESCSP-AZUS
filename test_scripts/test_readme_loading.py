#!/usr/bin/env python3
"""
Test README.html loading and usage as Zenodo description.

This script tests that README.html can be read correctly and will
be used as the Zenodo description in the upload workflow.

Usage:
    python test_readme_loading.py <path_to_readme.html>

Example:
    python test_readme_loading.py /path/to/README.html
"""

import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    """Test README.html loading."""
    
    print("=" * 70)
    print("AZUS README.HTML LOADING TEST")
    print("=" * 70)
    
    # Check command line arguments
    if len(sys.argv) < 2:
        print("\nUsage: python test_readme_loading.py <path_to_readme.html>")
        print("\nExample:")
        print("  python test_readme_loading.py /path/to/README.html")
        print("\nThis will verify that README.html can be loaded and used")
        print("as the Zenodo record description.")
        sys.exit(1)
    
    readme_path = Path(sys.argv[1])
    
    # Verify file exists
    if not readme_path.exists():
        print(f"\n❌ Error: README.html not found: {readme_path}")
        sys.exit(1)
    
    if not readme_path.is_file():
        print(f"\n❌ Error: Not a file: {readme_path}")
        sys.exit(1)
    
    print(f"\n📄 Testing README.html:")
    print(f"   File: {readme_path}")
    
    # Test reading the file
    print(f"\n{'='*70}")
    print("READING FILE")
    print(f"{'='*70}\n")
    
    try:
        content = readme_path.read_text(encoding='utf-8')
        print(f"✅ Successfully read README.html")
    except UnicodeDecodeError as e:
        print(f"❌ Error: File encoding issue: {e}")
        print(f"   README.html must be UTF-8 encoded")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Error reading file: {e}")
        sys.exit(1)
    
    # Analyze content
    print(f"\n{'='*70}")
    print("CONTENT ANALYSIS")
    print(f"{'='*70}\n")
    
    file_size = readme_path.stat().st_size
    char_count = len(content)
    line_count = len(content.splitlines())
    
    print(f"📊 File Statistics:")
    print(f"   File size:   {file_size:,} bytes ({file_size/1024:.1f} KB)")
    print(f"   Characters:  {char_count:,}")
    print(f"   Lines:       {line_count:,}")
    
    # Check for HTML structure
    has_html_tag = '<html' in content.lower()
    has_body_tag = '<body' in content.lower()
    has_head_tag = '<head' in content.lower()
    
    print(f"\n📋 HTML Structure:")
    print(f"   <html> tag:  {'✅' if has_html_tag else '⚠️  Missing'}")
    print(f"   <head> tag:  {'✅' if has_head_tag else '⚠️  Missing'}")
    print(f"   <body> tag:  {'✅' if has_body_tag else '⚠️  Missing'}")
    
    if not (has_html_tag and has_body_tag):
        print(f"\n   ⚠️  Note: File may be HTML fragment (no <html> or <body>)")
        print(f"   This is OK - content will still be used as description")
    
    # Check for expected Eclipse Soundscapes content
    print(f"\n🔍 Content Keywords:")
    keywords = [
        ("Eclipse Soundscapes", "Eclipse Soundscapes"),
        ("AudioMoth", "AudioMoth"),
        ("ESID", "ESID#"),
        ("Zenodo", "Zenodo"),
        ("NASA", "NASA"),
        ("UTC", "UTC"),
    ]
    
    for keyword, search_term in keywords:
        found = search_term in content
        print(f"   {keyword:<20s} {'✅ Found' if found else '⚠️  Not found'}")
    
    # Show preview
    print(f"\n{'='*70}")
    print("CONTENT PREVIEW")
    print(f"{'='*70}\n")
    
    # Get first 500 characters for preview
    preview_length = 500
    preview = content[:preview_length]
    if len(content) > preview_length:
        preview += "..."
    
    print(preview)
    
    if len(content) > preview_length:
        print(f"\n[... {len(content) - preview_length} more characters ...]")
    
    # Summary
    print(f"\n{'='*70}")
    print("SUMMARY")
    print(f"{'='*70}")
    
    issues = []
    
    if file_size == 0:
        issues.append("File is empty")
    
    if file_size > 5 * 1024 * 1024:  # 5 MB
        issues.append("File is very large (>5MB)")
    
    if char_count < 100:
        issues.append("Content seems very short (<100 characters)")
    
    if "Eclipse Soundscapes" not in content:
        issues.append("Does not contain 'Eclipse Soundscapes'")
    
    if issues:
        print(f"\n⚠️  WARNINGS ({len(issues)}):")
        for issue in issues:
            print(f"   • {issue}")
        print(f"\nREADME.html can be loaded but may have issues.")
    else:
        print(f"\n✅ SUCCESS: README.html looks good!")
        print(f"\nThis file will be used as the Zenodo description.")
        print(f"Make sure the content is accurate and properly formatted.")
    
    # Test with get_draft_config simulation
    print(f"\n{'='*70}")
    print("TESTING WITH AZUS")
    print(f"{'='*70}\n")
    
    print(f"When you upload, AZUS will:")
    print(f"  1. Read this file: {readme_path}")
    print(f"  2. Use its content as the Zenodo description")
    print(f"  3. The description will appear on the record page")
    print(f"  4. This file will NOT be uploaded (only used for description)")
    print(f"  5. README.md WILL be uploaded as a separate file")
    
    print(f"\n💡 Tip: Always verify the description on Zenodo after upload")
    
    sys.exit(0 if len(issues) == 0 else 1)


if __name__ == "__main__":
    main()
