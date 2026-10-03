# TESTAG Contest Results

This repository contains a compact archive of official amateur-radio contest results and one script that rebuilds PostgreSQL from the archive. The committed CSV files are the source of truth.

## Archive layout

Each populated contest, edition, and mode has exactly one score file and one metadata file:

```text
data/<contest>/<year>/<mode>/
├── scores.csv
└── meta.json
```

Contest directory names are compact:

| Directory | Contest |
|---|---|
| `cqwwdx` | CQ WW DX |
| `cqwpx` | CQ WPX |
| `waedx` | WAE DX |
| `wapcdx` | WAPC DX |
| `aadx` | All Asian DX |
| `iota` | RSGB IOTA |
| `arrldx` | ARRL DX |

Modes are `cw`, `ssb`, and `mixed` where the published result combines modes. Empty editions have no partition directory.

`meta.json` has exactly two properties: `retrieved_at_utc` and `source`. It records the source result URL and the retrieval timestamp.

## Score rows

Each `scores.csv` row represents one station result. It retains the source and normalized station callsign, location and category, raw and numeric score, QSOs, multipliers, operating time, eligibility, operator callsigns, and contest-specific score metrics. The operator list includes source spellings and normalized callsigns; `operator_list_complete` records whether the published list was complete. Contest-specific measures such as per-band QSOs and multipliers are kept in `metrics_json`.

The eight rank fields are paired official and calculated values:

- `category_world_rank_official`, `category_world_rank_calculated`
- `category_in_dxcc_rank_official`, `category_in_dxcc_rank_calculated`
- `overlay_world_rank_official`, `overlay_world_rank_calculated`
- `overlay_in_dxcc_rank_official`, `overlay_in_dxcc_rank_calculated`

Official values are filled only when the source explicitly reports that rank scope. Calculated ranks use descending final score and competition ranking within the exact category, with equal scores sharing a place and the next place skipped. Ineligible and review rows have no calculated place. DXCC places use the parser's explicit DXCC/entity value; they remain blank where the archive has no such value. Overlay places use only an explicit overlay label or category. CQ WW and CQ WPX source categories explicitly carry the overlay codes `C`, `R`, `Y`, and, for CQ WPX, `T`; those literal suffix codes are retained as overlay labels. No overlay-scope official rank values are present in this release.

The current release contains **1,133,264 station results in 466 populated partitions** and **587,469 operator memberships**. The archive has explicit DXCC/entity values for All Asian DX, ARRL DX, and WAPC DX. Official category-world values are retained for IOTA, and WAPC's official category ranks are stored in the in-DXCC pair because its source places repeat within a category across entity codes. Explicit overlay categories occur in CQ WW and CQ WPX; calculated worldwide overlay ranks are stored for eligible rows, while overlay in-DXCC fields remain blank because those partitions have no explicit DXCC/entity values.

## PostgreSQL build

Validate the archive without connecting to a database:

```sh
python build_db.py --validate-only
```

Install Psycopg 3 with its binary extra, then rebuild with a disposable or explicitly approved PostgreSQL database URL:

```sh
python -m pip install 'psycopg[binary]>=3.2,<4'
TESTAG_DATABASE_URL='postgresql://…' python build_db.py
```

`db/schema.sql` is the single canonical schema. The builder drops and recreates the TESTAG tables, commits each contest/year/mode partition as it loads, creates indexes after the data is loaded, and records the release only after row-count checks pass. If a connection drops during loading, use `python build_db.py --resume` with the same database URL to skip committed partitions. An absent release row means the load did not finish. A normal rebuild is destructive to existing TESTAG tables in the selected database. It does not collect or update source results. Never point it at a production database during routine work.

## Web consumer

The separate web repository reads these four PostgreSQL tables with SELECT-only
access. Its `/stats/:callsign` pages display all eight stored rank fields;
Statistics groups entries by partition and preserves official source URLs and
retrieval times. Release freshness uses `built_at_utc` and
`latest_retrieved_at_utc`. The web app does not initialize, rebuild, or write this
database.

## License and attribution

The dataset is designated **CC BY-NC-ND 4.0 International**. Attribution: **Gong BI1XJT <BI1XJT@rad1o.cn>**. See [LICENSE](LICENSE). Source websites retain their own rights and terms.
