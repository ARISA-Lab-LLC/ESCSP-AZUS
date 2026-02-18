# How to Add Citations and Related Works to Zenodo Records

## Overview

Your AZUS upload system now supports adding citations and related works to Zenodo records through the `related_identifiers` metadata field.

**Current citations included by default:**
1. ✅ Main Eclipse Soundscapes paper (DOI: 10.1038/s41597-024-03940-2)
2. ✅ Project website (https://eclipsesoundscapes.org)

---

## Where to Add Citations

**File:** `standalone_tasks.py`  
**Function:** `get_default_related_identifiers()` (around line 737)

This function returns a list of citations that will be added to **every** Zenodo record you upload.

---

## Adding a New Citation

### Step 1: Find the Function

Open `standalone_tasks.py` and locate the `get_default_related_identifiers()` function (around line 737).

### Step 2: Add Your Citation

Add a new `RelatedIdentifier` to the list. Here's the template:

```python
def get_default_related_identifiers() -> List[RelatedIdentifier]:
    """Retrieve default citations and related works."""
    return [
        # Main Eclipse Soundscapes paper
        RelatedIdentifier(
            identifier="10.1038/s41597-024-03940-2",
            scheme="doi",
            relation_type="cites",
            resource_type=ResourceType(id="publication-article")
        ),
        
        # Project website
        RelatedIdentifier(
            identifier="https://eclipsesoundscapes.org",
            scheme="url",
            relation_type="isSupplementTo"
        ),
        
        # ADD YOUR NEW CITATION HERE:
        RelatedIdentifier(
            identifier="YOUR_DOI_OR_URL_HERE",
            scheme="doi",  # or "url", "arxiv", etc.
            relation_type="cites",  # or other relation type
            resource_type=ResourceType(id="publication-article")  # optional
        ),
    ]
```

### Step 3: Save and Test

```bash
# Test with dry run
python standalone_upload.py --dry-run

# If successful, upload
python standalone_upload.py
```

---

## Citation Examples

### Example 1: Citing a Paper (DOI)

```python
RelatedIdentifier(
    identifier="10.1234/example.doi.5678",
    scheme="doi",
    relation_type="cites",
    resource_type=ResourceType(id="publication-article")
)
```

### Example 2: Citing a Paper (arXiv)

```python
RelatedIdentifier(
    identifier="2301.12345",
    scheme="arxiv",
    relation_type="cites",
    resource_type=ResourceType(id="publication-preprint")
)
```

### Example 3: Referencing a Website

```python
RelatedIdentifier(
    identifier="https://example.com/project",
    scheme="url",
    relation_type="references"
)
```

### Example 4: Linking to Related Dataset

```python
RelatedIdentifier(
    identifier="10.5281/zenodo.1234567",
    scheme="doi",
    relation_type="isSupplementTo",
    resource_type=ResourceType(id="dataset")
)
```

### Example 5: Linking to Software/Code

```python
RelatedIdentifier(
    identifier="https://github.com/ARISA-Lab-LLC/ESCSP",
    scheme="url",
    relation_type="references",
    resource_type=ResourceType(id="software")
)
```

---

## Relation Types Explained

Choose the appropriate `relation_type` based on how the resource relates to your dataset:

### For Citations (Papers You're Citing)

- **`"cites"`** - This dataset cites the paper
  - Use when: Citing a paper that your work builds on or references
  - Example: "This dataset cites the Eclipse Soundscapes methodology paper"

- **`"references"`** - This dataset references the resource
  - Use when: General reference to related work
  - Example: "This dataset references the project documentation"

### For Related Datasets/Resources

- **`"isSupplementTo"`** - This dataset supplements the related resource
  - Use when: Your dataset adds to or extends another resource
  - Example: "This dataset supplements the main Eclipse Soundscapes collection"

- **`"isSupplementedBy"`** - This dataset is supplemented by the related resource
  - Use when: Another resource adds to or extends your dataset
  - Example: "This dataset is supplemented by the analysis code repository"

### For Collections/Parts

- **`"isPartOf"`** - This dataset is part of a larger collection
  - Use when: Your dataset is a component of a larger collection
  - Example: "This ESID is part of the 2024 eclipse collection"

- **`"hasPart"`** - This dataset contains the related resource
  - Use when: The related resource is a component of your dataset
  - Example: "This collection has individual ESID datasets as parts"

### For Derived Work

- **`"isDerivedFrom"`** - This dataset is derived from the related resource
  - Use when: Your dataset was created by processing another resource
  - Example: "This processed dataset is derived from raw sensor data"

- **`"isSourceOf"`** - This dataset is the source of the related resource
  - Use when: Your dataset was used to create another resource
  - Example: "This raw data is the source of the processed dataset"

### For Versions

- **`"isNewVersionOf"`** - This is a new version of the related resource
- **`"isPreviousVersionOf"`** - This is a previous version of the related resource
- **`"continues"`** - This dataset continues the related resource
- **`"isContinuedBy"`** - This dataset is continued by the related resource

---

## Identifier Schemes

Specify the type of identifier using the `scheme` parameter:

| Scheme | Description | Example |
|--------|-------------|---------|
| `"doi"` | Digital Object Identifier | `10.1234/example.5678` |
| `"url"` | Web URL | `https://example.com` |
| `"arxiv"` | arXiv preprint | `2301.12345` |
| `"isbn"` | Book ISBN | `978-3-16-148410-0` |
| `"pmid"` | PubMed ID | `12345678` |
| `"handle"` | Handle identifier | `20.500.12345/67890` |
| `"urn"` | URN | `urn:isbn:0451450523` |

---

## Resource Types

Optionally specify what type of resource you're linking to:

| Type | Use For |
|------|---------|
| `"publication-article"` | Journal articles |
| `"publication-preprint"` | Preprints (arXiv, bioRxiv) |
| `"dataset"` | Other datasets |
| `"software"` | Software, code repositories |
| `"image"` | Images, figures |
| `"presentation"` | Slides, presentations |
| `"poster"` | Conference posters |
| `"other"` | Other resource types |

---

## Complete Example

Here's a complete example with multiple citations:

```python
def get_default_related_identifiers() -> List[RelatedIdentifier]:
    """Retrieve default citations and related works."""
    return [
        # Main project paper
        RelatedIdentifier(
            identifier="10.1038/s41597-024-03940-2",
            scheme="doi",
            relation_type="cites",
            resource_type=ResourceType(id="publication-article")
        ),
        
        # Project website
        RelatedIdentifier(
            identifier="https://eclipsesoundscapes.org",
            scheme="url",
            relation_type="isSupplementTo"
        ),
        
        # Related methodology paper
        RelatedIdentifier(
            identifier="10.1234/methodology.2024",
            scheme="doi",
            relation_type="cites",
            resource_type=ResourceType(id="publication-article")
        ),
        
        # Code repository
        RelatedIdentifier(
            identifier="https://github.com/ARISA-Lab-LLC/ESCSP",
            scheme="url",
            relation_type="references",
            resource_type=ResourceType(id="software")
        ),
        
        # Related preprint
        RelatedIdentifier(
            identifier="2401.12345",
            scheme="arxiv",
            relation_type="references",
            resource_type=ResourceType(id="publication-preprint")
        ),
        
        # Main collection this is part of
        RelatedIdentifier(
            identifier="10.5281/zenodo.1234567",
            scheme="doi",
            relation_type="isPartOf",
            resource_type=ResourceType(id="dataset")
        ),
    ]
```

---

## Testing Your Changes

### 1. Validate Syntax

```bash
# Check for Python syntax errors
python -c "from standalone_tasks import get_default_related_identifiers; print('✅ Syntax OK')"
```

### 2. Test Citation Creation

```bash
# Run with dry-run to see metadata
python standalone_upload.py --dry-run
```

### 3. Check Zenodo Draft

After uploading, check the draft record on Zenodo to verify citations appear in the "Related works" section.

---

## Removing Citations

To remove a citation, simply delete or comment out the corresponding `RelatedIdentifier` block:

```python
def get_default_related_identifiers() -> List[RelatedIdentifier]:
    return [
        # Main paper - keep this
        RelatedIdentifier(
            identifier="10.1038/s41597-024-03940-2",
            scheme="doi",
            relation_type="cites",
            resource_type=ResourceType(id="publication-article")
        ),
        
        # Project website - keep this
        RelatedIdentifier(
            identifier="https://eclipsesoundscapes.org",
            scheme="url",
            relation_type="isSupplementTo"
        ),
        
        # Old citation - removed
        # RelatedIdentifier(
        #     identifier="10.1234/old.paper",
        #     scheme="doi",
        #     relation_type="cites",
        # ),
    ]
```

---

## Per-Dataset Custom Citations

The current implementation adds the same citations to **all** datasets. If you need different citations for different datasets:

### Option 1: Modify Based on Eclipse Type

```python
def get_default_related_identifiers(eclipse_type: str = "total") -> List[RelatedIdentifier]:
    """Get citations based on eclipse type."""
    
    # Common citations for all datasets
    common = [
        RelatedIdentifier(
            identifier="10.1038/s41597-024-03940-2",
            scheme="doi",
            relation_type="cites",
            resource_type=ResourceType(id="publication-article")
        ),
    ]
    
    # Additional citations for total eclipses
    if eclipse_type.lower() == "total":
        common.append(
            RelatedIdentifier(
                identifier="10.1234/total.eclipse.paper",
                scheme="doi",
                relation_type="cites",
                resource_type=ResourceType(id="publication-article")
            )
        )
    
    # Additional citations for annular eclipses
    elif eclipse_type.lower() == "annular":
        common.append(
            RelatedIdentifier(
                identifier="10.1234/annular.eclipse.paper",
                scheme="doi",
                relation_type="cites",
                resource_type=ResourceType(id="publication-article")
            )
        )
    
    return common
```

Then update `get_draft_config()` to pass the eclipse type:

```python
# In get_draft_config():
related_identifiers = get_default_related_identifiers(
    eclipse_type=data_collector.eclipse_type
)
```

### Option 2: Add to Collector CSV

Add a column to your collector CSV for custom citations, then read them per-dataset.

---

## Troubleshooting

### Citations Not Appearing on Zenodo

**Check:**
1. Did you save `standalone_tasks.py` after editing?
2. Did you restart the upload script?
3. Is the syntax correct (commas, quotes, etc.)?

**Test:**
```bash
python -c "
from standalone_tasks import get_default_related_identifiers
citations = get_default_related_identifiers()
print(f'Found {len(citations)} citations')
for c in citations:
    print(f'  - {c.identifier} ({c.relation_type})')
"
```

### Syntax Errors

Common mistakes:
- ❌ Missing comma after `RelatedIdentifier(...)` block
- ❌ Unmatched quotes in identifier
- ❌ Missing closing parenthesis

**Fix:** Use a Python syntax checker or IDE.

### Invalid DOI Format

Zenodo expects DOIs without the URL prefix:
- ✅ `"10.1234/example.5678"`
- ❌ `"https://doi.org/10.1234/example.5678"`

---

## Quick Reference

**File to edit:** `standalone_tasks.py`  
**Function:** `get_default_related_identifiers()` (line ~737)

**Common pattern:**
```python
RelatedIdentifier(
    identifier="<DOI_or_URL>",
    scheme="<doi|url|arxiv>",
    relation_type="<cites|references|isSupplementTo>",
    resource_type=ResourceType(id="<publication-article|dataset|software>")
)
```

**Test command:**
```bash
python standalone_upload.py --dry-run
```

---

## Summary

✅ **Citations are now automatically added to all uploads**  
✅ **Default citations:** Eclipse Soundscapes paper + project website  
✅ **To add more:** Edit `get_default_related_identifiers()` in `standalone_tasks.py`  
✅ **Format:** Use `RelatedIdentifier` objects with DOI/URL, scheme, and relation type  
✅ **Testing:** Use `--dry-run` to verify before uploading  

All uploaded datasets will include these citations in their Zenodo metadata!
