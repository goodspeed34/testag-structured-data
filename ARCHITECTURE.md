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

Calculated ranks are worldwide competition ranks based on descending final score. Category worldwide groups use exact source category. Category DXCC groups use exact source category plus the explicit parser DXCC/entity value. Overlay groups use only an explicit overlay label/category, with DXCC as an additional key for the in-DXCC scope. CQ WW and CQ WPX categories carry explicit parenthesized overlay codes: `C`, `R`, and `Y` for CQ WW; `C`, `R`, `T`, and `Y` for CQ WPX. The literal code is retained in `overlay_category_source`; the builder does not infer an overlay from scores or other fields. Equal scores share a rank and the following rank skips. Only eligible rows with numeric scores are ranked; ineligible and review rows stay blank.

The archive's parser-provided DXCC/entity value is retained in `dxcc_entity_source`. The builder does not infer an entity from arbitrary QTH, island, region, or callsign text. Current explicit values are present for All Asian DX, ARRL DX, and WAPC DX; other current partitions have no in-DXCC rank. Explicit overlay categories are present in CQ WW and CQ WPX, so their eligible rows have calculated worldwide overlay ranks. No DXCC/entity values are present in those partitions, so overlay in-DXCC ranks remain blank.

## PostgreSQL

`db/schema.sql` is the single canonical PostgreSQL schema. It defines four tables:

- `contest_result_partition`: one row per contest/year/mode, including the `meta.json` source URL and retrieval timestamp;
- `contest_result`: one row per station result, with score, eligibility, explicit entity/overlay labels, integer official and calculated ranks, and contest-specific metrics as JSONB;
- `contest_result_operator`: one row per result/operator membership;
- `testag_dataset_release`: the loaded archive digest, build time, latest source retrieval time, license, attribution, and row counts.

`build_db.py` validates the compact archive, then drops and recreates these owned tables from `db/schema.sql`. It commits the empty schema, imports each contest/year/mode partition in its own transaction, creates indexes after loading, checks all row counts, and records the release last. If loading fails midway, rerun normally to reset the tables, or pass `--resume` to skip committed partitions in an existing partial build. Resume counts the committed tables, adds missing partitions, and performs the same final count checks. Absence of a release row marks the load as incomplete. Official rank strings in CSV are parsed as integers for PostgreSQL; the original display text remains in the archive. The schema deliberately uses the same `cw`, `ssb`, and `mixed` mode set as the archive builder.

Rebuilding is destructive to the TESTAG tables in the selected database. Use a disposable or explicitly approved database.

Run offline validation with:

```sh
python build_db.py --validate-only
```

Validation checks the exact partition contents and metadata keys, CSV fields, score types, callsigns, eligibility, operator lists, and all calculated rank values. It recomputes the four calculated rank scopes in memory and compares them with the committed CSV fields. Database loading requires Psycopg 3 and a PostgreSQL URL.

## Web consumer contract

The independent Deno/Material UI web app reads the four canonical tables using
SELECT-only access. Callsign pages match normalized station and operator indexes,
join `contest_result_partition` for official URLs and retrieval times, and
display all eight rank fields without calculating or repairing them. The web
Statistics page aggregates entries, eligible entries, and entries containing at
least one official rank per contest/year/mode. Release metadata comes from
`built_at_utc`, `latest_retrieved_at_utc`, license, attribution, and counts in
`testag_dataset_release`. A release row must exist before results are served.
The web app makes no schema changes and performs no database imports.

## Rights

The dataset is designated **CC BY-NC-ND 4.0 International**. Attribution is **Gong BI1XJT <BI1XJT@rad1o.cn>**. Source websites retain their own rights and terms.
