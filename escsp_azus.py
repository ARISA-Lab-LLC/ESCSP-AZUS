#escsp_azus.py
import csv
import os
from pathlib import Path
import re
import html2text
import csv
from html.parser import HTMLParser



def update_file_sizes(input_csv="file_list_Template.csv", output_csv="file_list.csv", base_path='.'):
    """
    Read a CSV file, update file sizes based on actual files, and write to a new CSV.
    
    Args:
        input_csv: Path to input CSV file
        output_csv: Path to output CSV file
        base_path: Base directory where files are located (default: current directory)
    """
    
    # Read the CSV file
    with open(input_csv, 'r', encoding='utf-8') as infile:
        reader = csv.DictReader(infile)
        fieldnames = reader.fieldnames
        rows = list(reader)
    
    # Find the column names (case-insensitive search)
    filename_col = None
    filesize_col = None
    
    for field in fieldnames:
        if 'file name' in field.lower():
            filename_col = field
        elif 'file size' in field.lower() and 'kb' in field.lower():
            filesize_col = field
    
    if not filename_col or not filesize_col:
        print(f"Error: Could not find required columns.")
        print(f"Available columns: {fieldnames}")
        return
    
    print(f"Found columns: '{filename_col}' and '{filesize_col}'")
    print(f"\nProcessing files from base path: {os.path.abspath(base_path)}\n")
    
    # Update file sizes
    updated_count = 0
    missing_files = []
    
    for row in rows:
        filename = row[filename_col]
        if filename:
            filepath = os.path.join(base_path, filename)
            
            if os.path.exists(filepath):
                # Get file size in KB
                file_size_bytes = os.path.getsize(filepath)
                file_size_kb = file_size_bytes / 1024
                row[filesize_col] = f"{file_size_kb:.2f}"
                updated_count += 1
                print(f"✓ Updated: {filename} -> {file_size_kb:.2f} KB")
            else:
                missing_files.append(filename)
                print(f"✗ File not found: {filename}")
    
    # Write the updated CSV
    with open(output_csv, 'w', newline='', encoding='utf-8') as outfile:
        writer = csv.DictWriter(outfile, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    
    # Summary
    print(f"\n{'='*60}")
    print(f"Summary:")
    print(f"  Total rows processed: {len(rows)}")
    print(f"  Files updated: {updated_count}")
    print(f"  Files not found: {len(missing_files)}")
    print(f"  Output saved to: {output_csv}")
    print(f"{'='*60}")
    
    if missing_files:
        print(f"\nMissing files:")
        for f in missing_files:
            print(f"  - {f}")
    return


def extract_by_esid(
    input_csv_path,
    output_csv_path,
    esid_value,
    error_log_path="csv_extract_error.txt"
):
    """
    Extract rows from a CSV where ESID == esid_value.

    Parameters:
        input_csv_path (str | Path): Source CSV file
        output_csv_path (str | Path): Destination CSV file
        esid_value (str): ESID value to match
        error_log_path (str | Path): Error log file for missing ESIDs
    """

    input_csv_path = Path(input_csv_path)
    output_csv_path = Path(output_csv_path)
    error_log_path = Path(error_log_path)

    if not input_csv_path.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_csv_path}")

    matched_rows = []

    with input_csv_path.open(newline="", encoding="utf-8") as infile:
        reader = csv.DictReader(infile)

        if "ESID" not in reader.fieldnames:
            raise ValueError("CSV does not contain an 'ESID' column")

        for row in reader:
            if row["ESID"] == esid_value:
                matched_rows.append(row)

        fieldnames = reader.fieldnames

    if matched_rows:
        output_csv_path.parent.mkdir(parents=True, exist_ok=True)

        with output_csv_path.open("w", newline="", encoding="utf-8") as outfile:
            writer = csv.DictWriter(outfile, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(matched_rows)

        print(f"Extracted {len(matched_rows)} row(s) to {output_csv_path}")



    else:
        with error_log_path.open("a", encoding="utf-8") as errfile:
            errfile.write(f"{esid_value}\n")

        print(
            f"No match found for ESID '{esid_value}'. "
            f"Logged to {error_log_path}"
        )

def rename_csv_headers(
    input_csv,
    output_csv,
    header_mapping,
    keep_unmapped=True,
    lowercase_unmapped=False,
    warn_missing=True
):
    """
    Rename CSV headers based on a custom mapping dictionary.
    
    Args:
        input_csv: Path to input CSV file
        output_csv: Path to output CSV file with renamed headers
        header_mapping: Dict mapping old header names to new names.
                       Example: {'OldName': 'new_name', 'FirstName': 'first_name'}
        keep_unmapped: If True, keep headers not in mapping (default: True)
                      If False, exclude unmapped columns from output
        lowercase_unmapped: If True, lowercase unmapped headers (default: False)
        warn_missing: If True, warn about headers in mapping not found in CSV
        
    Returns:
        Tuple of (rows_processed, original_headers, new_headers, unmapped_headers)
    """
    input_path = Path(input_csv)
    output_path = Path(output_csv)
    
    if not input_path.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_path}")
    
    # Read CSV
    rows = []
    original_headers = None
    
    with input_path.open('r', newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        original_headers = reader.fieldnames
        
        if not original_headers:
            raise ValueError(f"CSV file has no headers: {input_path}")
        
        rows = list(reader)
    
    # Build new header list and track unmapped
    new_headers = []
    unmapped_headers = []
    headers_to_keep = []  # Original headers to keep in output
    
    for old_header in original_headers:
        if old_header in header_mapping:
            # Header is in mapping - rename it
            new_headers.append(header_mapping[old_header])
            headers_to_keep.append(old_header)
        else:
            # Header not in mapping
            unmapped_headers.append(old_header)
            if keep_unmapped:
                if lowercase_unmapped:
                    new_headers.append(old_header.lower())
                else:
                    new_headers.append(old_header)
                headers_to_keep.append(old_header)
    
    # Warn about headers in mapping that don't exist in CSV
    if warn_missing:
        missing_headers = set(header_mapping.keys()) - set(original_headers)
        if missing_headers:
            print(f"Warning: Headers in mapping not found in CSV:")
            for h in sorted(missing_headers):
                print(f"  - '{h}' (mapped to '{header_mapping[h]}')")
            print()
    
    # Check for duplicate new headers
    duplicate_headers = set([h for h in new_headers if new_headers.count(h) > 1])
    if duplicate_headers:
        raise ValueError(
            f"Duplicate headers in output: {duplicate_headers}. "
            "Check your mapping - multiple old headers map to the same new header."
        )
    
    # Write output CSV
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    with output_path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        
        # Write new header row
        writer.writerow(new_headers)
        
        # Write data rows (in new column order)
        for row in rows:
            writer.writerow([row[old_h] for old_h in headers_to_keep])
    
    # Print summary
    print(f"✓ Processed {len(rows)} rows")
    print(f"✓ Output saved to: {output_path}")
    print(f"\nHeader transformations:")
    for old_h, new_h in zip(headers_to_keep, new_headers):
        if old_h in header_mapping:
            print(f"  {old_h} → {new_h}")
        elif old_h != new_h:
            print(f"  {old_h} → {new_h} (auto-lowercased)")
    
    if unmapped_headers:
        if keep_unmapped:
            print(f"\nUnmapped headers (kept as-is): {len(unmapped_headers)}")
            for h in unmapped_headers:
                print(f"  - {h}")
        else:
            print(f"\nUnmapped headers (excluded): {len(unmapped_headers)}")
            for h in unmapped_headers:
                print(f"  - {h}")
    
    return len(rows), original_headers, new_headers, unmapped_headers


def create_header_mapping_template(input_csv, style='lowercase'):
    """
    Generate a template header mapping dictionary from a CSV file.
    
    Args:
        input_csv: Path to CSV file
        style: Conversion style - 'lowercase', 'snake_case', or 'keep'
        
    Returns:
        Dictionary with headers mapped based on style
    """
    input_path = Path(input_csv)
    
    if not input_path.exists():
        raise FileNotFoundError(f"CSV not found: {input_path}")
    
    with input_path.open('r', newline='', encoding='utf-8') as f:
        reader = csv.reader(f)
        headers = next(reader)
    
    def to_snake_case(text):
        """Convert PascalCase or camelCase to snake_case."""
        import re
        # Insert underscore before uppercase letters
        s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', text)
        # Insert underscore before uppercase letters preceded by lowercase
        s2 = re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1)
        return s2.lower()
    
    # Generate mapping based on style
    if style == 'lowercase':
        mapping = {h: h.lower() for h in headers}
    elif style == 'snake_case':
        mapping = {h: to_snake_case(h) for h in headers}
    elif style == 'keep':
        mapping = {h: h for h in headers}
    else:
        raise ValueError(f"Unknown style: {style}. Use 'lowercase', 'snake_case', or 'keep'")
    
    print(f"# Header mapping template ({style} style):")
    print("# Copy and customize the right side as needed")
    print("header_mapping = {")
    for old, new in mapping.items():
        print(f"    '{old}': '{new}',")
    print("}")
    print()
    
    return mapping

class HTMLToMarkdown(HTMLParser):
    """Convert HTML to Markdown."""
    
    def __init__(self):
        super().__init__()
        self.markdown = []
        self.current_tag = []
        self.list_level = 0
        self.in_code = False
        self.in_pre = False
        
    def handle_starttag(self, tag, attrs):
        self.current_tag.append(tag)
        attrs_dict = dict(attrs)
        
        if tag == 'h1':
            self.markdown.append('\n# ')
        elif tag == 'h2':
            self.markdown.append('\n## ')
        elif tag == 'h3':
            self.markdown.append('\n### ')
        elif tag == 'h4':
            self.markdown.append('\n#### ')
        elif tag == 'h5':
            self.markdown.append('\n##### ')
        elif tag == 'h6':
            self.markdown.append('\n###### ')
        elif tag == 'p':
            self.markdown.append('\n\n')
        elif tag == 'br':
            self.markdown.append('  \n')
        elif tag == 'strong' or tag == 'b':
            self.markdown.append('**')
        elif tag == 'em' or tag == 'i':
            self.markdown.append('*')
        elif tag == 'code':
            self.in_code = True
            self.markdown.append('`')
        elif tag == 'pre':
            self.in_pre = True
            self.markdown.append('\n```\n')
        elif tag == 'a':
            self.markdown.append('[')
        elif tag == 'ul' or tag == 'ol':
            self.list_level += 1
            self.markdown.append('\n')
        elif tag == 'li':
            indent = '  ' * (self.list_level - 1)
            if self.current_tag[-2] == 'ol' if len(self.current_tag) > 1 else False:
                self.markdown.append(f'{indent}1. ')
            else:
                self.markdown.append(f'{indent}- ')
        elif tag == 'blockquote':
            self.markdown.append('\n> ')
        elif tag == 'hr':
            self.markdown.append('\n---\n')
        elif tag == 'img':
            alt = attrs_dict.get('alt', '')
            src = attrs_dict.get('src', '')
            self.markdown.append(f'![{alt}]({src})')
            
    def handle_endtag(self, tag):
        if not self.current_tag:
            return
            
        if self.current_tag[-1] == tag:
            self.current_tag.pop()
        
        if tag in ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']:
            self.markdown.append('\n')
        elif tag == 'p':
            self.markdown.append('\n')
        elif tag == 'strong' or tag == 'b':
            self.markdown.append('**')
        elif tag == 'em' or tag == 'i':
            self.markdown.append('*')
        elif tag == 'code':
            self.in_code = False
            self.markdown.append('`')
        elif tag == 'pre':
            self.in_pre = False
            self.markdown.append('\n```\n')
        elif tag == 'a':
            # Find the href from the tag
            self.markdown.append('](URL)')  # Will be fixed in post-processing
        elif tag == 'ul' or tag == 'ol':
            self.list_level -= 1
            self.markdown.append('\n')
        elif tag == 'li':
            self.markdown.append('\n')
            
    def handle_data(self, data):
        if self.in_pre or self.in_code:
            self.markdown.append(data)
        else:
            # Clean up whitespace but preserve intentional spacing
            cleaned = ' '.join(data.split())
            if cleaned:
                self.markdown.append(cleaned)
    
    def get_markdown(self):
        result = ''.join(self.markdown)
        # Clean up excessive newlines
        result = re.sub(r'\n{3,}', '\n\n', result)
        return result.strip()


def html_to_markdown_simple(html_content):
    """
    Convert HTML to Markdown using a simple parser.
    
    Args:
        html_content: HTML string to convert
        
    Returns:
        Markdown string
    """
    # Handle links separately to preserve href
    link_pattern = re.compile(r'<a\s+(?:[^>]*?\s+)?href="([^"]*)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
    
    def replace_link(match):
        href = match.group(1)
        text = match.group(2)
        # Strip HTML from link text
        text = re.sub(r'<[^>]+>', '', text)
        return f'[{text}]({href})'
    
    html_with_links = link_pattern.sub(replace_link, html_content)
    
    # Parse the rest
    parser = HTMLToMarkdown()
    parser.feed(html_with_links)
    markdown = parser.get_markdown()
    
    return markdown


def generate_readme_file(
    csv_file,
    template_file,
    output_file,
    error_log_file="csv_template_error.txt",
    save_markdown=True,
    markdown_options=None
):
    """
    Generate a README (or HTML/text) file by replacing $header_name placeholders
    in a template with values from a single-row CSV.
    
    If output_file ends with .html and save_markdown=True, also creates a .md version.

    Args:
        csv_file: Path to CSV file with header row and one data row
        template_file: Path to template file with $placeholder variables
        output_file: Path where rendered output will be saved
        error_log_file: Path to log file for unknown placeholders
        save_markdown: If True and output is .html, also save as .md
        markdown_options: Dict of html2text options (optional)
        
    Returns:
        1  on success
       -1  if unknown placeholders are found
    """
    csv_path = Path(csv_file)
    template_path = Path(template_file)
    output_path = Path(output_file)
    error_log_path = Path(error_log_file)

    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")
    if not template_path.exists():
        raise FileNotFoundError(f"Template not found: {template_path}")

    # Load single CSV row
    with csv_path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        row = next(reader, None)
        if row is None:
            raise ValueError(f"CSV has headers but no data row: {csv_path}")
        if next(reader, None) is not None:
            raise ValueError(f"CSV contains more than one data row: {csv_path}")

    template_text = template_path.read_text(encoding="utf-8")

    pattern = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
    unknown_placeholders = set()

    def repl(match):
        key = match.group(1)
        if key in row and row[key] is not None:
            return str(row[key])
        unknown_placeholders.add(key)
        return match.group(0)

    rendered = pattern.sub(repl, template_text)

    # Handle unknown placeholders
    if unknown_placeholders:
        with error_log_path.open("a", encoding="utf-8") as err:
            for key in sorted(unknown_placeholders):
                err.write(f"{key}\n")
        return -1

    # Write output only if successful
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered, encoding="utf-8")

    # Convert to Markdown if output is HTML and save_markdown is True
    if save_markdown and output_path.suffix.lower() in ['.html', '.htm']:
        markdown_content = html_to_markdown(rendered, markdown_options)
        markdown_path = output_path.with_suffix('.md')
        markdown_path.write_text(markdown_content, encoding="utf-8")
        print(f"✓ HTML saved to: {output_path}")
        print(f"✓ Markdown saved to: {markdown_path}")

    return 1


def html_to_markdown(html_content, options=None):
    """
    Convert HTML to Markdown using html2text.
    
    Args:
        html_content: HTML string to convert
        options: Dict of html2text configuration options
        
    Returns:
        Markdown string
    """
    h = html2text.HTML2Text()
    
    # Default options optimized for README files
    default_options = {
        'body_width': 0,              # Don't wrap lines (0 = infinite)
        'unicode_snob': True,          # Use unicode characters
        'ignore_links': False,         # Keep links
        'ignore_images': False,        # Keep images
        'ignore_emphasis': False,      # Keep bold/italic
        'skip_internal_links': False,  # Keep internal links
        'inline_links': True,          # Use inline link style [text](url)
        'protect_links': True,         # Don't mangle URLs
        'mark_code': True,             # Properly mark code blocks
        'wrap_links': False,           # Don't wrap URLs
        'wrap_list_items': False,      # Don't wrap list items
        'escape_snob': False,          # Don't escape special chars unnecessarily
        'use_automatic_links': True,   # Auto-link URLs
    }
    
    # Merge user options with defaults
    if options:
        default_options.update(options)
    
    # Apply options to html2text instance
    for key, value in default_options.items():
        if hasattr(h, key):
            setattr(h, key, value)
    
    # Convert HTML to Markdown
    markdown = h.handle(html_content)
    
    # Clean up excessive blank lines (more than 2 consecutive)
    markdown = re.sub(r'\n{3,}', '\n\n', markdown)
    
    return markdown.strip()










