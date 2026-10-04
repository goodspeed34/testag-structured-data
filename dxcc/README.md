# DXCC inference and recalculation

Contest-provided `dxcc_entity_source` values always take precedence and remain
unchanged. Missing entities are inferred from station callsigns by the CQRLOG
Python DXCC parser with Martin OK1RR's historical country tables. Operator
callsigns are not used to determine the station's entity.

`reference.json` pins the parser and reference revision, individual file SHA-256
hashes, attribution, and the resolution policy. The upstream parser is GPL-2.0;
the country tables have their own attribution and are not relicensed as TESTAG
data. Neither the parser nor the country tables are redistributed in this
repository. They remain an external, offline input to recalculation.

## Reproduce the recalculation

Obtain the documented official reference once, then run offline:

```sh
git clone https://github.com/ok2cqr/cqrlog-cnty-files .cache/dxcc-reference
git -C .cache/dxcc-reference checkout --detach 4adb0e70bf29faefc3b203bc355796cd0c6201fe
python build_db.py --recalculate-dxcc --dxcc-reference-dir .cache/dxcc-reference
python build_db.py --validate-only
```

Recalculation validates the existing archive before changing it, checks every
pinned reference file, atomically replaces each partition's CSV, then validates
the complete enriched archive. It is resumable by rerunning the same command
with the same reference; publication is a separate database command. Partition
metadata and all published score/result fields and official ranks are preserved.
Only three DXCC fields and the two calculated in-DXCC ranks change. Worldwide
category and overlay ranks are recomputed but retain the same values.

## Fields and policy

- `dxcc_entity_source`: exact entity supplied by the contest, if any.
- `dxcc_adif`: canonical numeric DXCC identifier, where it can be determined.
- `dxcc_entity_inferred`: country label supplied by callsign inference; blank
  whenever the contest already supplied an entity.
- `dxcc_resolution`: `source`, `callsign`, `unresolved`, `ambiguous_year`,
  `ambiguous_prefix`, or `no_entity`.

The archive records an edition year, not an exact contest date. No date is
invented. Inference checks the start/end of the year and every relevant validity
boundary in the country tables, including keys probed during portable-call
reduction. It accepts an entity only if its ADIF ID is constant throughout the
year. Deleted entities are searched before current ones. Maritime/aeronautical
mobile operations have no entity. Generic matches to prefixes listed as
ambiguous by upstream are rejected unless an exact exception or a more specific
literal area prefix resolves them. The underlying reference can contain gaps or
errors: these are documented parser inferences, not official contest assertions.

Explicit source entity codes and names are mapped to a canonical ADIF ID using
reference entity names/prefixes where possible. Case and whitespace are normalized
for matching only; published text stays unchanged. Explicit `USA-<district>`
labels and the aliases `USA`, `U.S.A.`, `US`, and `United States of America` map to
DXCC 291. Numeric source values retain their stated ADIF ID. If a source label
cannot be mapped, its existing case/whitespace-normalized source label remains
the ranking key, and it is listed in `report.json`; no station-based override is
applied to a supplied entity.

Category in-DXCC ranks group eligible numeric scores by exact category and
canonical ADIF ID, falling back to an unmapped source label. Overlay in-DXCC
ranks additionally require an explicit overlay label. Ties use competition
ranking. Unresolved rows have no calculated in-DXCC rank; they still participate
in applicable worldwide ranks. Existing official ranks remain untouched.

`report.json` records totals, counts by contest, ranking coverage, source labels
without canonical IDs, reference revision, and the archive digest.
`unresolved.csv` lists each unresolved entry and its reason. Ordinary validation
checks field consistency and independently recalculates stored ranks; reproducing
callsign assignments requires the pinned external reference and recalculation.
The reference manifest participates in archive and partition digests.
