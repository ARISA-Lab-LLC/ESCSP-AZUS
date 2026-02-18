# Quick Fix: CSV Validation Errors

## The Problem

You're seeing this error:
```
ValidationError: 1 validation error for DataCollector
Local Eclipse Type
  Input should be 'Annular', 'Total' or 'Partial' [type=enum, input_value='', input_type=str]
```

**Translation:** Your CSV has empty or invalid values in the "Local Eclipse Type" column.

## Quick Fix (Automatic)

Run the validation tool with auto-fix:

```bash
# For total eclipse CSV
python validate_csv.py /path/to/collectors.csv --fix --eclipse-type total

# For annular eclipse CSV
python validate_csv.py /path/to/collectors.csv --fix --eclipse-type annular
```

This will:
1. Create a backup of your CSV (`.backup` file)
2. Find all empty "Local Eclipse Type" values
3. Set them to "Total" or "Annular" based on your eclipse type
4. Save the fixed CSV

Then try your upload again:
```bash
python standalone_upload.py
```

## Manual Fix

If you prefer to fix manually:

### 1. Find the Problem Rows

```bash
# Check your CSV without fixing
python validate_csv.py /path/to/collectors.csv --eclipse-type total
```

This shows which rows have issues.

### 2. Open in Spreadsheet Editor

Open your CSV in Excel, Google Sheets, or LibreOffice.

### 3. Fix the "Local Eclipse Type" Column

For each row, set "Local Eclipse Type" to one of:
- `Total` (for total solar eclipse)
- `Annular` (for annular solar eclipse)
- `Partial` (for partial eclipse)

**Important:** The value must be exactly one of these three words (case-sensitive).

### 4. Save and Validate

```bash
# Check it's fixed
python validate_csv.py /path/to/collectors.csv --eclipse-type total
```

Should show: `✅ SUCCESS: CSV is valid!`

## Common Issues

### Issue 1: Empty Values

**Problem:**
```
Local Eclipse Type is empty
```

**Fix:**
Set to `Total`, `Annular`, or `Partial` based on the actual eclipse type for that location.

### Issue 2: Wrong Format

**Problem:**
```
Invalid Local Eclipse Type: 'Total Solar Eclipse'
```

**Fix:**
Change `Total Solar Eclipse` → `Total`  
Change `Annular Solar Eclipse` → `Annular`  
Change `Partial Solar Eclipse` → `Partial`

The auto-fix tool handles this automatically.

### Issue 3: Typos

**Problem:**
```
Invalid Local Eclipse Type: 'Totla'
```

**Fix:**
Correct the typo: `Totla` → `Total`

### Issue 4: Mixed Case

**Problem:**
```
Invalid Local Eclipse Type: 'total'
```

**Fix:**
Capitalize properly: `total` → `Total`

## Valid Values Reference

| Valid Value | Description | Use For |
|------------|-------------|---------|
| `Total` | Total solar eclipse | Locations in path of totality |
| `Annular` | Annular solar eclipse | Locations in path of annularity |
| `Partial` | Partial eclipse | Locations outside totality/annularity paths |

## Why This Happens

The "Local Eclipse Type" field was added to the new CSV format to replace the old "Type of Eclipse" field. If you:

1. Migrated from old CSV format
2. Left some cells blank
3. Used a different spelling/format

...then you'll see this error.

## After Fixing

Once your CSV is valid:

```bash
# Test (no upload)
python standalone_upload.py --dry-run

# Actual upload
python standalone_upload.py
```

## Still Having Issues?

### Check Your Headers

Make sure your CSV has this exact header:
```
Local Eclipse Type
```

NOT:
- `Type of Eclipse` (old format)
- `Eclipse Type`
- `Type Of Eclipse`
- `local eclipse type` (wrong case)

### Check for Hidden Characters

Sometimes CSVs have hidden characters. To check:

```bash
# On Mac/Linux
cat -A your_file.csv | head -1

# Should show:
# ESID,Data Collector Affiliations,Latitude,Longitude,Local Eclipse Type,...
```

### Re-export Your CSV

If you edited in Excel or Google Sheets:
1. Save as CSV (UTF-8)
2. Don't use "CSV (MS-DOS)" or other variants
3. Make sure encoding is UTF-8

## Prevention

To avoid this in future:

1. Use the validation tool BEFORE uploading:
   ```bash
   python validate_csv.py your_file.csv --eclipse-type total
   ```

2. Set up a template with dropdown validation in your spreadsheet editor

3. Use the auto-fix tool as part of your workflow:
   ```bash
   python validate_csv.py input.csv --fix --eclipse-type total
   python standalone_upload.py
   ```

## Example Workflow

```bash
# 1. Validate your CSV
python validate_csv.py collectors.csv --eclipse-type total

# 2. If issues found, auto-fix
python validate_csv.py collectors.csv --fix --eclipse-type total

# 3. Verify it's fixed
python validate_csv.py collectors.csv --eclipse-type total

# 4. Upload
python standalone_upload.py
```

---

**Need help?** Run the validation tool - it will show you exactly what's wrong:
```bash
python validate_csv.py your_file.csv
```
