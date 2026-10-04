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

## Incremental updates

After the initial build, update an approved database using the complete archive:

```sh
TESTAG_DATABASE_URL='postgresql://…' python build_db.py --incremental
```

The builder validates the entire archive, hashes each partition's `scores.csv`
and `meta.json`, and skips partitions whose digest and builder version match.
New partitions are inserted; changed partitions are replaced with all their
results and operator memberships. Rank validation remains partition-wide.
The output reports `added`, `replaced`, `removed`, and `unchanged` counts.

All incremental data changes, final count checks, and release publication share
one transaction. A failure leaves the previous published dataset intact. An
unchanged run preserves the release's build timestamp. The release table holds
only the current release after an incremental publication; it is not a history
of retained dataset snapshots. Writers are serialized with table locks. Ordinary
readers can read the committed dataset during updates, except during the first
upgrade of an older database, when adding tracking columns takes a table lock
that can block readers until commit.

The first incremental run against the previous schema adds nullable
`archive_digest` and `builder_version` columns to `contest_result_partition`
and reloads existing partitions once to establish trusted digests. Later runs
only reload changes. This is the only automatic schema migration; other
incompatible schema changes require a full rebuild. A changed builder version
reloads all partitions. `--resume` and `--incremental` are mutually exclusive;
resume now rejects changed loaded partitions rather than silently skipping them.
Older partial builds without tracking columns need an incremental update or a
full rebuild.

Partitions present in the database but missing from the archive cause an error.
Explicitly permit their removal with:

```sh
TESTAG_DATABASE_URL='postgresql://…' python build_db.py --incremental --allow-removals
```

Keep the archive stable throughout validation and loading; the builder checks
its digests again before writing and before publishing. Database contents are
owned by this builder: digests track archive changes, not arbitrary manual edits
to database rows. Initial setup still uses a normal full rebuild. Incremental
mode does not collect results from official sites.

## Builder verification

Run offline validation with `python build_db.py --validate-only`. Database
integration tests use temporary copies of small committed partitions and require
an explicitly selected disposable database. They reset the TESTAG tables there:

```sh
TESTAG_TEST_DATABASE_URL='postgresql://…' python -m unittest discover -s tests -v
```

Tests cover unchanged runs, new partitions, metadata and score corrections,
operator cleanup, explicit removals, legacy tracking-column upgrades, builder
version changes, resume checks, archive changes after validation, and rollback
of data and release metadata on loading or count-check failures. They also verify
reader visibility during loading and publication when returning to an older archive.

## Web consumer

The separate web repository reads these four PostgreSQL tables with SELECT-only
access. Its `/stats/:callsign` pages display all eight stored rank fields;
Statistics groups entries by partition and preserves official source URLs and
retrieval times. Release freshness uses `built_at_utc` and
`latest_retrieved_at_utc`. The web app does not initialize, rebuild, or write this
database.

## License and attribution

The dataset is designated **CC BY-NC-ND 4.0 International**. Attribution: **Gong BI1XJT <BI1XJT@rad1o.cn>**. See [LICENSE](LICENSE). Source websites retain their own rights and terms.
