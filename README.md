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

Official values are filled only when the source explicitly reports that rank scope. Calculated ranks use descending final score and competition ranking within the exact category, with equal scores sharing a place and the next place skipped. Ineligible and review rows have no calculated place. DXCC places group by canonical ADIF entity, using the contest-provided entity first and historical callsign inference when it is missing. Unmapped source labels retain their own groups; unresolved missing entities have no DXCC place. Overlay places use only an explicit overlay label or category. CQ WW and CQ WPX source categories explicitly carry the overlay codes `C`, `R`, `Y`, and, for CQ WPX, `T`; those literal suffix codes are retained as overlay labels. No overlay-scope official rank values are present in this release.

The archive contains **1,133,264 station results in 466 partitions** and
**587,469 operator memberships**. Contest-provided DXCC/entity labels remain
unchanged. Missing entities now use an explicitly marked, historical callsign
inference. The three added fields are `dxcc_adif`, `dxcc_entity_inferred`, and
`dxcc_resolution`; both calculated in-DXCC ranks use the effective entity.
Official ranks remain unchanged. Recalculation inferred **981,496** missing entities and retained **137,449**
contest-provided entities. It populated **1,054,170** category in-DXCC ranks and
**74,903** overlay in-DXCC ranks. **14,319** entries remain ambiguous, unresolved,
or without a DXCC entity and have no in-DXCC rank.

See [DXCC recalculation](dxcc/README.md) for the pinned reference, historical
policy, reproduction command, coverage report, and unresolved-entry list. The
recalculation command modifies the CSV archive offline; database publication is
separate. Both the former text-key schema and earlier compact databases require
a full rebuild to adopt the added DXCC columns, followed by the updated web app.

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
readers can read the committed dataset during updates. Adding missing tracking
columns to a compatible database takes a stronger table lock and can block
readers until commit.

The compact schema uses internal integer result IDs and lookup tables for exact
category and eligibility explanation text. Metrics are compact PostgreSQL `json`
objects; all eight ranks remain stored without the former four rank indexes.
Original entry IDs and all published result fields are preserved; the separate
DXCC recalculation command enriches CSVs and recalculates in-DXCC ranks. Internal numeric IDs may
change after rebuilds or partition replacements and are not public identifiers.
Unused lookup strings remain available for reuse until a full rebuild.

**Upgrading the former text-key/JSONB or pre-inference compact database requires a full rebuild** with
`python build_db.py` against an approved target, followed by the updated web app.
Reapply the read-only web role's SELECT grants to all six tables after rebuilding.
Both `--incremental` and `--resume` reject the old schema without writing to it.
After that one rebuild, incremental updates work normally. Only missing tracking
columns on an otherwise compatible compact schema are added automatically; that
operation can temporarily block readers. Other incompatible changes require a
full rebuild. A changed builder version reloads all partitions.
`--resume` and `--incremental` are mutually exclusive; resume rejects changed
loaded partitions rather than silently skipping them.

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

Before DXCC enrichment, a complete disposable compact build measured on 2026-10-04 occupied approximately
496 MiB, compared with 668 MiB for the previous local schema (about 26% smaller).
This excludes cluster WAL. That storage comparison confirmed identical values for
all 1,133,264 results, 587,469 operator memberships, and 466 source partitions.
Actual sizes vary with PostgreSQL version and maintenance history. Measure with
`SELECT pg_size_pretty(pg_database_size(current_database()));`.

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
reader visibility during loading, publication when returning to an older archive,
lossless compact storage, and rejection of an incompatible schema.

## Web consumer

The separate web repository reads these six PostgreSQL tables with SELECT-only
access. Its `/stats/:callsign` pages display all eight stored rank fields;
Statistics groups entries by partition and preserves official source URLs and
retrieval times. Release freshness uses `built_at_utc` and
`latest_retrieved_at_utc`. The web app does not initialize, rebuild, or write this
database.

## License and attribution

The dataset is designated **CC BY-NC-ND 4.0 International**. Attribution: **Gong BI1XJT <BI1XJT@rad1o.cn>**. See [LICENSE](LICENSE). Source websites retain their own rights and terms.

Award eligibility is stored as the four-byte PostgreSQL enum
`testag_award_eligibility` (`eligible`, `ineligible`, `review`), preserving the
CSV and public API labels. Text-eligibility databases require a full rebuild;
resume and incremental imports reject that older schema. The builder recreates
this owned enum after dropping its dependent TESTAG tables.

For cloud import jobs, build a disposable local source database and export its
`public` schema with `pg_dump -Fc -n public --no-owner --no-acl -f db_dump.bak`.
Restore using the provider's direct connection, rather than its application
pool: `pg_restore --clean --if-exists --no-owner --no-acl --exit-on-error
--single-transaction -d "$RESTORE_DATABASE_URL" db_dump.bak`. Cleaning replaces
the TESTAG objects included in the backup; use only an explicitly approved
restore target. The archive and release must be complete before exporting.
Verify the release counts and calculated ranks after restoration. Keep generated
backups outside the repository. The deployed web app can use a pooled connection
with a read-only database role after the import completes.

When the target's `public` schema hosts provider-managed extensions, use
`pg_restore --list db_dump.bak` to create a restore list, remove only the
`SCHEMA - public` entry, and pass the list with `--use-list`. This preserves the
existing schema and extensions while replacing TESTAG objects. Never cascade-drop
provider-managed objects to make a restore succeed.
