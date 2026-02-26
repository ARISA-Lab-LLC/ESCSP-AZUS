# Directory Tree: `azus/`

*Generated: 2026-02-25 20:10:59*  
*Updated: Feb 25, 2026 — includes Raw_Data/, updated scripts, and all guides*

---

## Scan Settings

| Setting | Value |
|---------|-------|
| Root path | `azus/` |
| Max depth | unlimited |
| Show hidden | False |
| Show file stats | True |
| Excluded patterns | `__pycache__`, `.git`, `.svn`, `.hg`, `*.pyc`, `*.pyo`, `.DS_Store`, `Thumbs.db`, `*.egg-info`, `.env`, `node_modules`, `*.log` |

---

## Tree

```
azus/
├── **Guides/**
│   ├── CITATIONS_USER_GUIDE.md  *(12.4 KB, 2026-02-25 14:09)*
│   ├── CSV_FIX_GUIDE.md  *(4.5 KB, 2026-02-25 14:09)*
│   ├── DIRECTORY_STRUCTURE_GUIDE.md  *(14.3 KB, 2026-02-25 20:09)*
│   ├── PREFECT_VS_STANDALONE.md  *(9.3 KB, 2026-02-25 14:09)*
│   ├── REFACTORING_CHANGELOG.md  *(7.1 KB, 2026-02-25 14:09)*
│   ├── STANDALONE_README.md  *(9.6 KB, 2026-02-25 14:09)*
│   └── TEST_UPLOAD_GUIDE.md  *(16.1 KB, 2026-02-25 20:09)*
├── **models/**
│   ├── __init__.py  *(32 B, 2026-02-25 20:10)*
│   ├── audiomoth.py  *(11.0 KB, 2026-02-25 14:09)*
│   └── invenio.py  *(7.4 KB, 2026-02-25 14:09)*
├── **Raw_Data/**
│   └── .gitkeep  *(57 B, 2026-02-25 20:10)*
├── **Records/**
│   └── .gitkeep  *(56 B, 2026-02-25 20:10)*
├── **Resources/**
│   ├── 2023_Annular_Zenodo_Form_Spreadsheet.csv  *(11.0 KB, 2026-02-25 14:09)*
│   ├── 2024_Total_Zenodo_Form_Spreadsheet.csv  *(77.3 KB, 2026-02-25 14:09)*
│   ├── AudioMoth_Operation_Manual.pdf  *(3.6 MB, 2026-02-25 14:09)*
│   ├── collectors.csv  *(11.0 KB, 2026-02-25 14:09)*
│   ├── CONFIG_data_dict.csv  *(11.4 KB, 2026-02-25 14:09)*
│   ├── file_list_data_dict.csv  *(985 B, 2026-02-25 14:09)*
│   ├── file_list_Template.csv  *(2.7 KB, 2026-02-25 14:09)*
│   ├── License.txt  *(18.2 KB, 2026-02-25 14:09)*
│   ├── project_config.json  *(3.8 KB, 2026-02-25 14:09)*
│   ├── README_template.html  *(12.9 KB, 2026-02-25 14:09)*
│   ├── references.csv  *(507 B, 2026-02-25 14:09)*
│   ├── related_identifiers.csv  *(312 B, 2026-02-25 14:09)*
│   ├── related_identifiers1.csv  *(316 B, 2026-02-25 14:09)*
│   ├── related_identifiers2.csv  *(448 B, 2026-02-25 14:09)*
│   └── WAV_data_dict.csv  *(1.6 KB, 2026-02-25 14:09)*
├── **Staging_Area/**
│   └── .gitkeep  *(61 B, 2026-02-25 20:10)*
├── **templates/**
│   ├── config.json.example  *(1.8 KB, 2026-02-25 20:09)*
│   ├── project_config.json.example  *(3.5 KB, 2026-02-25 14:09)*
│   ├── README_template.html.example  *(2.0 KB, 2026-02-25 14:09)*
│   ├── references.csv.example  *(251 B, 2026-02-25 14:09)*
│   ├── related_identifiers.csv.example  *(312 B, 2026-02-25 14:09)*
│   └── set_env.sh.example  *(239 B, 2026-02-25 14:09)*
├── prepare_dataset.py  *(23.1 KB, 2026-02-25 20:09)*
├── README.md  *(1.6 KB, 2026-02-25 20:10)*
├── requirements-standalone.txt  *(235 B, 2026-02-25 14:09)*
├── standalone_tasks.py  *(49.6 KB, 2026-02-25 20:09)*
└── standalone_uploader.py  *(11.2 KB, 2026-02-25 20:09)*
```

---

## Summary

| Metric | Count |
|--------|-------|
| Directories | 7 |
| Files | 39 |
| Total size | 4.0 MB |

### File Types

| Extension | Count |
|-----------|-------|
| `.csv` | 11 |
| `.md` | 8 |
| `.py` | 6 |
| `.example` | 6 |
| `.gitkeep` | 3 |
| `.txt` | 2 |
| `.pdf` | 1 |
| `.html` | 1 |
| `.json` | 1 |

---

## Notes

- `Raw_Data/` — place raw AudioMoth recording folders here (e.g. `ESID#004/` with WAV files and `CONFIG.TXT`). Populated at runtime, not tracked by git.
- `Staging_Area/` — output of `prepare_dataset.py`. Populated at runtime, not tracked by git.
- `Records/` — upload result logs. Created automatically on first run.
- `Resources/config.json` — **not included** (contains personal paths). Create from `templates/config.json.example`.
- `Resources/set_env.sh` — **not included** (contains API credentials). Create from `templates/set_env.sh.example`.
