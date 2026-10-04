# TESTAG Results Archive Architecture

> Dataset license: CC BY-NC-ND 4.0 International

## Purpose

The repository owns the committed contest-result archive and the PostgreSQL builder. Result files are the source of truth; PostgreSQL is derived. This repository contains no source collector or refresh pipeline.

The archive covers CQ WW DX, CQ WPX, WAE DX, WAPC DX, All Asian DX, RSGB IOTA, and ARRL DX, using published CW, SSB, and combined mixed modes where available. RTTY is out of scope.

## Layout and files

A populated partition is `data/<contest>/<year>/<mode>/` and contains only:

- `scores.csv`: one station result per row, including callsigns and score details;
- `meta.json`: exactly `retrieved_at_utc` and `source`.

Contest directory names are `cqwwdx`, `cqwpx`, `waedx`, `wapcdx`, `aadx`, `iota`, and `arrldx`. Year and mode come from the directory path, so they are not repeated in the CSV.

The score file preserves source and normalized station callsigns, location, exact source category, raw and numeric score, QSO counts, contest multipliers, operating-time values, eligibility and its basis, operator callsigns, and contest-specific scoring measures. Source spellings remain beside normalized values. Operator callsigns are stored with each result, and `operator_list_complete` flags truncated or unavailable lists. `metrics_json` holds result measures that do not have a common column, such as band-level QSOs and multipliers, QTC counts, points, and source status fields. It excludes parser traces and raw source rows.

## Rank contract

Every row has the same eight rank fields, with source-reported and calculated values kept separately:

| Rank scope | Official field | Calculated field |
|---|---|---|
| Category, worldwide | `category_world_rank_official` | `category_world_rank_calculated` |
| Category, within DXCC/entity | `category_in_dxcc_rank_official` | `category_in_dxcc_rank_calculated` |
| Explicit overlay, worldwide | `overlay_world_rank_official` | `overlay_world_rank_calculated` |
| Explicit overlay, within DXCC/entity | `overlay_in_dxcc_rank_official` | `overlay_in_dxcc_rank_calculated` |

Official rank fields retain the source's displayed value only when its scope matches one of these four types. An unfiltered score-list position, a continental leader position, or a regional placing is not treated as one of these values. IOTA's “WORLD” positions are stored as category-world official ranks. WAPC's official rank values repeat within a ranking group across explicit entity codes, so they are stored as category in-DXCC official ranks. A blank official value means the source view did not report the requested scope. This release has no overlay-scope official values.

Calculated ranks are worldwide competition ranks based on descending final score. Category worldwide groups use exact source category. Category DXCC groups use exact source category plus canonical ADIF ID where available, falling back to a normalized contest-provided entity label. Overlay groups use only an explicit overlay label/category, with DXCC as an additional key for the in-DXCC scope. CQ WW and CQ WPX categories carry explicit parenthesized overlay codes: `C`, `R`, and `Y` for CQ WW; `C`, `R`, `T`, and `Y` for CQ WPX. The literal code is retained in `overlay_category_source`; the builder does not infer an overlay from scores or other fields. Equal scores share a rank and the following rank skips. Only eligible rows with numeric scores are ranked; ineligible and review rows stay blank.

Contest-provided DXCC/entity text stays in `dxcc_entity_source` and takes
precedence. Missing values are inferred from the station callsign by a pinned,
offline CQRLOG parser and Martin OK1RR's historical country tables. The archive
adds `dxcc_adif`, `dxcc_entity_inferred`, and `dxcc_resolution`. Inference requires
a constant entity throughout the edition year, since exact contest dates are
not archived. Deleted entities, portable callsigns, generic ambiguous prefixes,
and mobile operations are handled explicitly; unresolved cases have no in-DXCC
rank. Supplied entity labels are mapped to ADIF codes when possible without
changing their source text. Both calculated in-DXCC scopes are recalculated;
all official ranks and worldwide rank values remain unchanged.

[DXCC policy and provenance](dxcc/README.md) documents the exact revision,
hashes, source-label transformations, and reproduction command.
`dxcc/report.json` records coverage; `dxcc/unresolved.csv` identifies entries that
could not be assigned safely. These are explicit inferences, not source claims.

## PostgreSQL

`db/schema.sql` is the single canonical PostgreSQL schema. It defines six tables:

- `contest_result_partition`: one row per contest/year/mode, including the `meta.json` source URL and retrieval timestamp, plus nullable `archive_digest` and `builder_version` tracking columns;
- `contest_result`: one row per station result, with an internal integer `result_id` primary key, unique original `entry_id`, score, eligibility, source/inferred entity labels, optional ADIF ID, DXCC resolution status, explicit overlay labels, integer official and calculated ranks, lookup foreign keys, and compact contest-specific metrics as PostgreSQL `json`;
- `contest_result_operator`: one row per result/operator membership, linked through integer `result_id`;
- `contest_result_category`: exact source category strings, stored once and referenced by integer `category_id`;
- `contest_result_eligibility_basis`: exact eligibility explanations, stored once and referenced by integer `eligibility_basis_id`;
- `testag_dataset_release`: the loaded archive digest, build time, latest source retrieval time, license, attribution, and row counts.

`build_db.py` validates the compact archive, then drops and recreates these owned tables from `db/schema.sql`. It commits the empty schema, imports each contest/year/mode partition in its own transaction, creates indexes after loading, checks all row counts, and records the release last. If loading fails midway, rerun normally to reset the tables, or pass `--resume` to skip committed partitions in an existing partial build. Resume verifies the digests and builder versions of committed partitions, rejects changed loaded partitions, adds missing partitions, and performs the same final count checks. Partial builds using the former text-key/JSONB schema require a full rebuild. Absence of a release row marks the load as incomplete. Official rank strings in CSV are parsed as integers for PostgreSQL; the original display text remains in the archive. The schema deliberately uses the same `cw`, `ssb`, and `mixed` mode set as the archive builder.

Rebuilding is destructive to the TESTAG tables in the selected database. Use a disposable or explicitly approved database.

Run offline validation with:

```sh
python build_db.py --validate-only
```

Validation checks the exact partition contents and metadata keys, CSV fields, score types, callsigns, eligibility, operator lists, and all calculated rank values. It recomputes the four calculated rank scopes in memory and compares them with the committed CSV fields. Database loading requires Psycopg 3 and a PostgreSQL URL.

## Compact storage contract

The original entry IDs and published fields are preserved. Offline DXCC
recalculation enriches CSV rows as documented above. PostgreSQL uses a
32-bit identity `result_id` for its primary key and all operator relationships;
`entry_id` remains unique and available to consumers. Internal numeric IDs are
local to a database build and may change after partition replacement or rebuild.
They must not be used as public identifiers. The integer sequence fails at its
capacity rather than wrapping; it also consumes values during failed loads.

Category and eligibility explanation lookup rows retain exact source strings.
The builder inserts distinct values transactionally and reuses their IDs across
partitions. Unreferenced lookup rows are retained for reuse; a full rebuild
recreates only the values present in the archive. Source text is preserved without abbreviation; callsign-inferred DXCC values
are stored separately and explicitly marked. Award eligibility and all eight rank values remain in each result.

Metrics use validated PostgreSQL `json` objects serialized as UTF-8 with sorted
keys and compact separators. JSON values are preserved; whitespace is removed.
No JSON indexing or filtering is used by the current web consumer. The four
category/overlay rank indexes are removed; stored ranks remain available. The
station lookup, operator lookup, partition deletion index, primary keys and
uniqueness constraints remain, including original entry-ID uniqueness.

This is an incompatible database contract change. Rebuild an approved target
from the complete archive with `python build_db.py` before running the updated
web app; `--incremental` cannot migrate the former schema. Reapply the web role's
SELECT grants after rebuilding, including both lookup tables. The web queries
return source/inferred DXCC separately and join lookup text and use numeric result relationships internally while returning
the same public fields, archive IDs, rank values and ordering.

## Incremental publication

`--incremental` requires the six compact-schema TESTAG tables and a complete validated
archive. Validation stores SHA-256 digests of each partition's score and metadata
file bytes, delimited by filenames. These digests are separate from the existing
whole-archive release digest. Both include the pinned DXCC reference manifest,
so inference provenance changes invalidate incremental fingerprints. `builder_version` is a
SHA-256 digest of the canonical schema and an explicit semantic builder version
(`testag-builder-v4`); bump that version for conversion/ranking behavior changes.
A differing builder version forces partition replacement but does not migrate
incompatible schemas. Such schemas require a full rebuild.

The builder compares `(contest_id, edition_year, mode)` keys and tracking values,
inserts new partitions, and replaces changed ones. All changed/removed partition
rows are deleted before insertion so entry IDs may move between partitions.
Existing cascading foreign keys remove station results and operator memberships.
An unconditional contest/year/mode index supports these deletes even for entries
without calculated ranks.
The eight ranks remain validated against the complete partition; no individual
score or official rank is silently repaired. Offline DXCC recalculation is an
explicit archive transformation separate from database loading. Missing archive partitions are rejected
unless `--allow-removals` is supplied with `--incremental`.

A single transaction serializes writers with SHARE ROW EXCLUSIVE locks on all
six tables, applies changes, verifies counts and stable archive digests, then
replaces the release row. Failed updates roll back all data and release changes.
An unchanged run retains the existing release timestamp. Release records describe
the current dataset; incremental publication removes older release rows so
freshness and counts cannot select a stale release after returning to an older
archive. Readers see committed data while ordinary incremental loading runs;
separate web queries may span a publication, since web requests do not currently
use a single database snapshot.

For an otherwise compatible compact schema, incremental mode adds missing nullable
`archive_digest` and `builder_version` columns in this transaction. Null tracking
values force one replacement of each legacy partition. ALTER TABLE requires a
stronger lock and may block readers until this first upgrade commits. Subsequent
runs avoid ALTER TABLE entirely. The former text-key/JSONB schema is rejected by both incremental and resume modes before any writes; adoption requires a full rebuild. No other schema migrations are implemented.
Full builds store tracking values as they load each partition; resume checks them.

Digest checks detect ordinary file changes between validation and publication;
the archive must remain stable throughout the command. Skipped partitions are
trusted as builder-owned database content; manual same-count row edits are not
detected. Validation still scans the whole archive, so incremental loading saves
database writes rather than eliminating validation work. Tests live under
`tests/` and require an explicitly selected disposable database; `build_db.py`
remains the only executable project script.

## Web consumer contract

The independent Deno/Material UI web app reads the six canonical tables using
SELECT-only access. Callsign pages match normalized station and operator indexes,
join `contest_result_partition` for official URLs and retrieval times, and
display all eight rank fields without calculating or repairing them. The web
Statistics page aggregates entries, eligible entries, and entries containing at
least one official rank per contest/year/mode. Release metadata comes from
`built_at_utc`, `latest_retrieved_at_utc`, license, attribution, and counts in
`testag_dataset_release`. A release row must exist before results are served.
The enriched DXCC schema requires a full rebuild of older compact databases
before running the updated web app. The web app makes no schema changes and performs no database imports.

## Rights

The dataset is designated **CC BY-NC-ND 4.0 International**. Attribution is **Gong BI1XJT <BI1XJT@rad1o.cn>**. Source websites retain their own rights and terms.

Award eligibility is stored as the four-byte PostgreSQL enum
`testag_award_eligibility` (`eligible`, `ineligible`, `review`), preserving the
CSV and public API labels. Text-eligibility databases require a full rebuild;
resume and incremental imports reject that older schema. The builder recreates
this owned enum after dropping its dependent TESTAG tables.

Cloud publication may use a compressed custom-format `pg_dump` of a complete
locally built `public` schema and a `pg_restore` through the provider's direct
endpoint. Restore without source ownership or ACLs, clean only backup-owned
objects in the approved target, and use one transaction with exit-on-error so a
failed restore rolls back. Verify the published release and rank counts afterward.
Pool endpoints remain appropriate for application queries after publication.

Prisma's `public` schema also hosts managed extensions. Exclude the backup's
`SCHEMA - public` entry from the restore list so `--clean` preserves that schema
and its extension dependencies. This does not omit the TESTAG tables or enum.
