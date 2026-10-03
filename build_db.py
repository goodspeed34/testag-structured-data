#!/usr/bin/env python3
"""Validate the compact score archive and rebuild its PostgreSQL database."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse


PATH_CONTEST_IDS = {
    "cqwwdx": "cqww-dx",
    "cqwpx": "cq-wpx",
    "waedx": "wae-dx",
    "wapcdx": "wapc-dx",
    "aadx": "aadx",
    "iota": "iota",
    "arrldx": "arrl-dx",
}
MODES = {"cw", "ssb", "mixed"}
EXPLICIT_OVERLAY_CODES = {
    "cqwwdx": {"C", "R", "Y"},
    "cqwpx": {"C", "R", "T", "Y"},
}
META_FIELDS = {"retrieved_at_utc", "source"}
SCORE_FIELDS = [
    "entry_id",
    "station_callsign_source",
    "station_callsign",
    "location_code_source",
    "category_source",
    "dxcc_entity_source",
    "overlay_category_source",
    "score_source",
    "score",
    "qso_count",
    "cq_zones",
    "cq_countries",
    "wpx_prefixes",
    "multipliers",
    "operating_hours_source",
    "operating_hours",
    "late_entry",
    "award_eligibility",
    "eligibility_basis",
    "operator_list_complete",
    "operators_json",
    "category_world_rank_official",
    "category_world_rank_calculated",
    "category_in_dxcc_rank_official",
    "category_in_dxcc_rank_calculated",
    "overlay_world_rank_official",
    "overlay_world_rank_calculated",
    "overlay_in_dxcc_rank_official",
    "overlay_in_dxcc_rank_calculated",
    "metrics_json",
]
RESULT_COPY_FIELDS = [
    "entry_id",
    "contest_id",
    "edition_year",
    "mode",
    "station_callsign_source",
    "station_callsign",
    "location_code_source",
    "category_source",
    "dxcc_entity_source",
    "overlay_category_source",
    "multipliers",
    "score_source",
    "score",
    "qso_count",
    "cq_zones",
    "cq_countries",
    "wpx_prefixes",
    "operating_hours_source",
    "operating_hours",
    "late_entry",
    "award_eligibility",
    "eligibility_basis",
    "operator_list_complete",
    "category_world_rank_official",
    "category_world_rank_calculated",
    "category_in_dxcc_rank_official",
    "category_in_dxcc_rank_calculated",
    "overlay_world_rank_official",
    "overlay_world_rank_calculated",
    "overlay_in_dxcc_rank_official",
    "overlay_in_dxcc_rank_calculated",
    "metrics_json",
]
OPERATOR_COPY_FIELDS = [
    "entry_id",
    "operator_order",
    "operator_callsign_source",
    "operator_callsign",
]
RANK_FIELDS = [
    "category_world_rank_official",
    "category_world_rank_calculated",
    "category_in_dxcc_rank_official",
    "category_in_dxcc_rank_calculated",
    "overlay_world_rank_official",
    "overlay_world_rank_calculated",
    "overlay_in_dxcc_rank_official",
    "overlay_in_dxcc_rank_calculated",
]
CALCULATED_RANK_FIELDS = [field for field in RANK_FIELDS if field.endswith("_calculated")]
LICENSE = "CC BY-NC-ND 4.0 International"
ATTRIBUTION = "Gong BI1XJT <BI1XJT@rad1o.cn>"


@dataclass(frozen=True)
class Partition:
    contest_path: str
    contest_id: str
    year: int
    mode: str
    scores_path: Path
    meta: dict[str, str]


@dataclass
class ArchiveStats:
    partitions: int = 0
    entries: int = 0
    operators: int = 0
    official_ranked_entries: int = 0
    official_rank_values: int = 0
    latest_retrieved_at: str | None = None
    release_id: str = ""

    def as_dict(self) -> dict[str, int | str]:
        return {
            "partitions": self.partitions,
            "entries": self.entries,
            "operators": self.operators,
            "official_ranked_entries": self.official_ranked_entries,
            "official_rank_values": self.official_rank_values,
            "latest_retrieved_at_utc": self.latest_retrieved_at or "",
            "release_id": self.release_id,
        }


def _parse_datetime(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO 8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a UTC offset: {value!r}")
    return parsed.astimezone(timezone.utc)


def _parse_bool(value: str, label: str) -> bool:
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    raise ValueError(f"{label} must be true or false, got {value!r}")


def _parse_optional_int(value: str, label: str) -> int | None:
    if value == "":
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be an integer, got {value!r}") from exc


def _parse_optional_official_rank(value: str, label: str) -> int | None:
    if value == "":
        return None
    try:
        parsed = int(value.replace(",", ""))
    except ValueError as exc:
        raise ValueError(f"{label} must be an integer rank, got {value!r}") from exc
    if parsed < 1:
        raise ValueError(f"{label} must be positive, got {value!r}")
    return parsed


def _parse_optional_decimal(value: str, label: str) -> Decimal | None:
    if value == "":
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{label} must be numeric, got {value!r}") from exc
    if not parsed.is_finite():
        raise ValueError(f"{label} must be finite, got {value!r}")
    return parsed


def _decode_json(value: str, label: str, expected_type: type) -> Any:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} contains invalid JSON: {exc}") from exc
    if not isinstance(decoded, expected_type):
        raise ValueError(f"{label} must contain {expected_type.__name__}")
    return decoded


def _partitions(data_root: Path) -> Iterator[Partition]:
    for child in data_root.iterdir() if data_root.exists() else ():
        if not child.is_dir() or child.name not in PATH_CONTEST_IDS:
            raise ValueError(f"Unexpected item in archive root: {child.name}")
    scores_files = sorted(data_root.glob("*/*/*/scores.csv"))
    if not scores_files:
        raise ValueError(f"No score files found under {data_root}")
    for scores_path in scores_files:
        relative = scores_path.relative_to(data_root)
        contest_path, year_text, mode = relative.parts[:3]
        actual_files = {item.name for item in scores_path.parent.iterdir()}
        if actual_files != {"scores.csv", "meta.json"}:
            raise ValueError(f"{relative.parent} must contain only scores.csv and meta.json")
        if contest_path not in PATH_CONTEST_IDS:
            raise ValueError(f"Unknown contest directory in {relative}")
        if len(year_text) != 4 or not year_text.isdigit():
            raise ValueError(f"Invalid edition directory in {relative}")
        if mode not in MODES:
            raise ValueError(f"Invalid mode directory in {relative}")

        meta_path = scores_path.with_name("meta.json")
        if not meta_path.is_file():
            raise ValueError(f"Missing metadata file for {relative}")
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid metadata JSON in {meta_path}: {exc}") from exc
        if not isinstance(meta, dict) or set(meta) != META_FIELDS:
            raise ValueError(f"{meta_path} must contain only {sorted(META_FIELDS)}")
        if not isinstance(meta["source"], str) or not meta["source"].strip():
            raise ValueError(f"{meta_path} has no source URL")
        if urlparse(meta["source"]).scheme not in {"http", "https"}:
            raise ValueError(f"{meta_path} source must be an HTTP(S) URL")
        _parse_datetime(meta["retrieved_at_utc"], f"{meta_path} retrieved_at_utc")
        yield Partition(
            contest_path=contest_path,
            contest_id=PATH_CONTEST_IDS[contest_path],
            year=int(year_text),
            mode=mode,
            scores_path=scores_path,
            meta=meta,
        )


def _validate_score_row(row: dict[str, str], partition: Partition) -> tuple[int, int]:
    label = f"{partition.scores_path}:{row.get('entry_id', '?')}"
    if row.get("entry_id", "") == "":
        raise ValueError(f"{label}: entry_id is required")
    if row.get("station_callsign", "") == "" or row.get("station_callsign_source", "") == "":
        raise ValueError(f"{label}: station callsign is required")
    if row.get("category_source", "") == "":
        raise ValueError(f"{label}: source category is required")
    if row.get("award_eligibility") not in {"eligible", "ineligible", "review"}:
        raise ValueError(f"{label}: invalid award_eligibility {row.get('award_eligibility')!r}")
    if row.get("eligibility_basis", "") == "":
        raise ValueError(f"{label}: eligibility_basis is required")
    _parse_bool(row.get("late_entry", ""), f"{label} late_entry")
    _parse_bool(row.get("operator_list_complete", ""), f"{label} operator_list_complete")
    for field in ("score", "qso_count", "cq_zones", "cq_countries", "wpx_prefixes", "multipliers"):
        _parse_optional_int(row.get(field, ""), f"{label} {field}")
    _parse_optional_decimal(row.get("operating_hours", ""), f"{label} operating_hours")

    operators = _decode_json(row.get("operators_json", ""), f"{label} operators_json", list)
    operator_callsigns: set[str] = set()
    for operator in operators:
        if not isinstance(operator, dict) or set(operator) != {"source", "callsign"}:
            raise ValueError(f"{label}: each operator must contain source and callsign")
        if not operator["source"] or not operator["callsign"]:
            raise ValueError(f"{label}: operator callsigns cannot be blank")
        if operator["callsign"] in operator_callsigns:
            raise ValueError(f"{label}: duplicate normalized operator callsign")
        operator_callsigns.add(operator["callsign"])

    rank_count = 0
    for field in RANK_FIELDS:
        value = row.get(field, "")
        if field.endswith("_calculated"):
            parsed_rank = _parse_optional_int(value, f"{label} {field}")
            if parsed_rank is not None and parsed_rank < 1:
                raise ValueError(f"{label}: {field} must be positive")
        elif _parse_optional_official_rank(value, f"{label} {field}") is not None:
            rank_count += 1
    for field in CALCULATED_RANK_FIELDS:
        if row.get(field) and row.get("award_eligibility") != "eligible":
            raise ValueError(f"{label}: ineligible/review rows cannot have a calculated rank")
    _decode_json(row.get("metrics_json", ""), f"{label} metrics_json", dict)
    if row.get("dxcc_entity_source") and not row["dxcc_entity_source"].strip():
        raise ValueError(f"{label}: dxcc_entity_source cannot contain only whitespace")
    if row.get("overlay_category_source") and not row["overlay_category_source"].strip():
        raise ValueError(f"{label}: overlay_category_source cannot contain only whitespace")
    explicit_overlay = _explicit_overlay_category(partition.contest_path, row["category_source"])
    if row["overlay_category_source"] != (explicit_overlay or ""):
        raise ValueError(
            f"{label}: overlay_category_source must match a recognized explicit source category "
            f"suffix ({explicit_overlay or 'blank'!r})"
        )
    return len(operators), rank_count


def validate_archive(data_root: Path) -> ArchiveStats:
    stats = ArchiveStats()
    release_hash = hashlib.sha256()
    seen_entry_ids: set[str] = set()
    latest: datetime | None = None
    for partition in _partitions(data_root):
        stats.partitions += 1
        retrieved = _parse_datetime(
            partition.meta["retrieved_at_utc"],
            f"{partition.scores_path.parent / 'meta.json'} retrieved_at_utc",
        )
        if latest is None or retrieved > latest:
            latest = retrieved
        for metadata_path in (partition.scores_path, partition.scores_path.with_name("meta.json")):
            release_hash.update(str(metadata_path.relative_to(data_root)).encode("utf-8"))
            release_hash.update(b"\0")
            with metadata_path.open("rb") as content:
                while chunk := content.read(1024 * 1024):
                    release_hash.update(chunk)

        with partition.scores_path.open("r", newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != SCORE_FIELDS:
                raise ValueError(
                    f"{partition.scores_path} columns do not match the required score fields"
                )
            partition_rows: list[dict[str, str]] = []
            for row in reader:
                if None in row:
                    raise ValueError(f"{partition.scores_path} contains a row with too many columns")
                if row["entry_id"] in seen_entry_ids:
                    raise ValueError(f"Duplicate entry_id across archive: {row['entry_id']}")
                seen_entry_ids.add(row["entry_id"])
                operator_count, rank_count = _validate_score_row(row, partition)
                stats.entries += 1
                stats.operators += operator_count
                stats.official_rank_values += rank_count
                if rank_count:
                    stats.official_ranked_entries += 1
                partition_rows.append(row)
            expected_ranks = _calculate_partition_ranks(partition_rows)
            for row in partition_rows:
                expected = expected_ranks[row["entry_id"]]
                for field in CALCULATED_RANK_FIELDS:
                    actual = _parse_optional_int(row[field], f"{row['entry_id']} {field}")
                    if actual != expected[field]:
                        raise ValueError(
                            f"{partition.scores_path}:{row['entry_id']}: {field} is {actual!r}, "
                            f"expected {expected[field]!r}"
                        )
    stats.latest_retrieved_at = latest.isoformat().replace("+00:00", "Z") if latest else None
    stats.release_id = release_hash.hexdigest()
    return stats


def _score_rows(partition: Partition) -> Iterator[dict[str, Any]]:
    with partition.scores_path.open("r", newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        for row in reader:
            yield row


def _entity_key(value: str) -> str:
    return " ".join(value.casefold().split())


def _explicit_overlay_category(contest_path: str, category_source: str) -> str | None:
    """Return a literal overlay code only when the source category carries one."""
    codes = EXPLICIT_OVERLAY_CODES.get(contest_path, set())
    match = re.search(r"\(([A-Z])\)\s*$", category_source.strip())
    if match and match.group(1) in codes:
        return match.group(1)
    return None


def _rank_groups(
    rows: list[dict[str, str]],
    *,
    group_key: Any,
) -> dict[str, int]:
    groups: dict[Any, list[tuple[str, int]]] = {}
    for row in rows:
        if row["award_eligibility"] != "eligible" or not row["score"]:
            continue
        score = int(row["score"])
        groups.setdefault(group_key(row), []).append((row["entry_id"], score))
    result: dict[str, int] = {}
    for group in groups.values():
        ordered = sorted(group, key=lambda item: -item[1])
        previous_score: int | None = None
        current_rank = 0
        for index, (entry_id, score) in enumerate(ordered, start=1):
            if score != previous_score:
                current_rank = index
                previous_score = score
            result[entry_id] = current_rank
    return result


def _calculate_partition_ranks(rows: list[dict[str, str]]) -> dict[str, dict[str, int | None]]:
    ranks: dict[str, dict[str, int | None]] = {
        row["entry_id"]: {field: None for field in CALCULATED_RANK_FIELDS}
        for row in rows
    }
    group_specs = [
        ("category_world_rank_calculated", lambda row: row["category_source"]),
        (
            "category_in_dxcc_rank_calculated",
            lambda row: (row["category_source"], _entity_key(row["dxcc_entity_source"])),
        ),
        (
            "overlay_world_rank_calculated",
            lambda row: _entity_key(row["overlay_category_source"]),
        ),
        (
            "overlay_in_dxcc_rank_calculated",
            lambda row: (
                _entity_key(row["overlay_category_source"]),
                _entity_key(row["dxcc_entity_source"]),
            ),
        ),
    ]
    for field, group_fn in group_specs:
        if field == "category_world_rank_calculated":
            eligible = rows
        elif field == "category_in_dxcc_rank_calculated":
            eligible = [row for row in rows if row["dxcc_entity_source"].strip()]
        elif field == "overlay_world_rank_calculated":
            eligible = [row for row in rows if row["overlay_category_source"].strip()]
        else:
            eligible = [
                row for row in rows
                if row["dxcc_entity_source"].strip() and row["overlay_category_source"].strip()
            ]
        ranked = _rank_groups(eligible, group_key=group_fn)
        for entry_id, value in ranked.items():
            ranks[entry_id][field] = value
    return ranks


def _json_value(value: str) -> Any:
    return json.loads(value)


def _db_entry(row: dict[str, str], partition: Partition) -> tuple[Any, ...]:
    return (
        row["entry_id"],
        partition.contest_id,
        partition.year,
        partition.mode,
        row["station_callsign_source"],
        row["station_callsign"],
        row["location_code_source"] or None,
        row["category_source"],
        row["dxcc_entity_source"] or None,
        row["overlay_category_source"] or None,
        _parse_optional_int(row["multipliers"], f"{row['entry_id']} multipliers"),
        row["score_source"] or None,
        _parse_optional_int(row["score"], f"{row['entry_id']} score"),
        _parse_optional_int(row["qso_count"], f"{row['entry_id']} qso_count"),
        _parse_optional_int(row["cq_zones"], f"{row['entry_id']} cq_zones"),
        _parse_optional_int(row["cq_countries"], f"{row['entry_id']} cq_countries"),
        _parse_optional_int(row["wpx_prefixes"], f"{row['entry_id']} wpx_prefixes"),
        row["operating_hours_source"] or None,
        _parse_optional_decimal(row["operating_hours"], f"{row['entry_id']} operating_hours"),
        _parse_bool(row["late_entry"], f"{row['entry_id']} late_entry"),
        row["award_eligibility"],
        row["eligibility_basis"],
        _parse_bool(row["operator_list_complete"], f"{row['entry_id']} operator_list_complete"),
        _parse_optional_official_rank(
            row["category_world_rank_official"],
            f"{row['entry_id']} category_world_rank_official",
        ),
        _parse_optional_int(row["category_world_rank_calculated"], f"{row['entry_id']} category_world_rank_calculated"),
        _parse_optional_official_rank(
            row["category_in_dxcc_rank_official"],
            f"{row['entry_id']} category_in_dxcc_rank_official",
        ),
        _parse_optional_int(row["category_in_dxcc_rank_calculated"], f"{row['entry_id']} category_in_dxcc_rank_calculated"),
        _parse_optional_official_rank(
            row["overlay_world_rank_official"],
            f"{row['entry_id']} overlay_world_rank_official",
        ),
        _parse_optional_int(row["overlay_world_rank_calculated"], f"{row['entry_id']} overlay_world_rank_calculated"),
        _parse_optional_official_rank(
            row["overlay_in_dxcc_rank_official"],
            f"{row['entry_id']} overlay_in_dxcc_rank_official",
        ),
        _parse_optional_int(row["overlay_in_dxcc_rank_calculated"], f"{row['entry_id']} overlay_in_dxcc_rank_calculated"),
        json.dumps(_json_value(row["metrics_json"]), ensure_ascii=False, sort_keys=True),
    )


def _operator_rows(row: dict[str, str]) -> Iterator[tuple[Any, ...]]:
    for order, operator in enumerate(_json_value(row["operators_json"]), start=1):
        yield (row["entry_id"], order, operator["source"], operator["callsign"])


def _copy_archive(connection: Any, data_root: Path, *, resume: bool = False) -> dict[str, int]:
    counts = {"partitions": 0, "entries": 0, "operators": 0}
    if resume:
        with connection.cursor() as cursor:
            cursor.execute("SELECT contest_id, edition_year, mode FROM contest_result_partition")
            completed = set(cursor.fetchall())
            cursor.execute("SELECT count(*) FROM contest_result")
            counts["entries"] = int(cursor.fetchone()[0])
            cursor.execute("SELECT count(*) FROM contest_result_operator")
            counts["operators"] = int(cursor.fetchone()[0])
        counts["partitions"] = len(completed)
    else:
        completed = set()
    for partition in _partitions(data_root):
        partition_key = (partition.contest_id, partition.year, partition.mode)
        if partition_key in completed:
            continue
        partition_counts = {"entries": 0, "operators": 0}
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO contest_result_partition "
                "(contest_id, edition_year, mode, retrieved_at_utc, source_url) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    partition.contest_id,
                    partition.year,
                    partition.mode,
                    partition.meta["retrieved_at_utc"],
                    partition.meta["source"],
                ),
            )
            for table, fields, row_source in (
                ("contest_result", RESULT_COPY_FIELDS, "entry"),
                ("contest_result_operator", OPERATOR_COPY_FIELDS, "operator"),
            ):
                columns = ", ".join(fields)
                output = io.StringIO(newline="")
                writer = csv.writer(output, lineterminator="\n")
                for row in _score_rows(partition):
                    if row_source == "entry":
                        writer.writerow(_db_entry(row, partition))
                        partition_counts["entries"] += 1
                    else:
                        for operator_row in _operator_rows(row):
                            writer.writerow(operator_row)
                            partition_counts["operators"] += 1
                payload = output.getvalue()
                with cursor.copy(
                    f"COPY {table} ({columns}) FROM STDIN WITH (FORMAT CSV)"
                ) as copy:
                    copy.write(payload)
        connection.commit()
        counts["partitions"] += 1
        counts["entries"] += partition_counts["entries"]
        counts["operators"] += partition_counts["operators"]
        if counts["partitions"] % 25 == 0 or counts["partitions"] == 1:
            print(
                f"Loaded {counts['partitions']} partitions: {counts['entries']} results, "
                f"{counts['operators']} operator memberships",
                file=sys.stderr,
                flush=True,
            )
    return counts


def _sql_statements(sql: str) -> list[str]:
    return [statement.strip() for statement in sql.split(";") if statement.strip()]


def build_database(
    repository_root: Path,
    data_root: Path,
    database_url: str,
    stats: ArchiveStats,
    *,
    resume: bool = False,
) -> dict[str, int | str]:
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("Install psycopg 3 with its binary extra before building PostgreSQL") from exc

    schema_path = repository_root / "db" / "schema.sql"
    if not schema_path.is_file():
        raise ValueError(f"Missing canonical database schema: {schema_path}")
    schema_statements = _sql_statements(schema_path.read_text(encoding="utf-8"))
    table_statements = [
        statement for statement in schema_statements
        if not statement.lstrip().upper().startswith("CREATE INDEX ")
    ]
    index_statements = [
        statement for statement in schema_statements
        if statement.lstrip().upper().startswith("CREATE INDEX ")
    ]
    with psycopg.connect(database_url) as connection:
        if resume:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT to_regclass('public.contest_result_partition'), "
                    "to_regclass('public.contest_result'), "
                    "to_regclass('public.contest_result_operator')"
                )
                if any(value is None for value in cursor.fetchone()):
                    raise ValueError("Cannot resume: TESTAG tables are missing; run a full rebuild first")
        else:
            with connection.cursor() as cursor:
                for statement in table_statements:
                    cursor.execute(statement)
            connection.commit()
        loaded = _copy_archive(connection, data_root, resume=resume)
        with connection.cursor() as cursor:
            for statement in index_statements:
                cursor.execute(statement)
            cursor.execute("SELECT count(*) FROM contest_result")
            loaded_entry_count = int(cursor.fetchone()[0])
            cursor.execute("SELECT count(*) FROM contest_result_operator")
            loaded_operator_count = int(cursor.fetchone()[0])
            cursor.execute("SELECT count(*) FROM contest_result_partition")
            loaded_partition_count = int(cursor.fetchone()[0])
            if (
                loaded["partitions"] != stats.partitions
                or loaded_partition_count != stats.partitions
                or loaded["entries"] != stats.entries
                or loaded["operators"] != stats.operators
                or loaded_entry_count != stats.entries
                or loaded_operator_count != stats.operators
            ):
                raise ValueError(
                    f"Database load count mismatch: COPY {loaded}, database "
                    f"{loaded_entry_count}/{loaded_operator_count}/{loaded_partition_count}, "
                    f"validated {stats.as_dict()}"
                )
            cursor.execute(
                "INSERT INTO testag_dataset_release "
                "(release_id, latest_retrieved_at_utc, dataset_license, attribution, "
                "partition_count, entry_count, operator_membership_count) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (release_id) DO UPDATE SET "
                "latest_retrieved_at_utc = EXCLUDED.latest_retrieved_at_utc, "
                "dataset_license = EXCLUDED.dataset_license, attribution = EXCLUDED.attribution, "
                "partition_count = EXCLUDED.partition_count, entry_count = EXCLUDED.entry_count, "
                "operator_membership_count = EXCLUDED.operator_membership_count",
                (
                    stats.release_id,
                    stats.latest_retrieved_at,
                    LICENSE,
                    ATTRIBUTION,
                    stats.partitions,
                    stats.entries,
                    stats.operators,
                ),
            )
    return {**stats.as_dict(), **loaded}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate score CSVs and rebuild the TESTAG PostgreSQL database")
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent / "data")
    parser.add_argument("--database-url", default=os.environ.get("TESTAG_DATABASE_URL"))
    parser.add_argument("--validate-only", action="store_true", help="Validate the archive without connecting to PostgreSQL")
    parser.add_argument("--resume", action="store_true", help="Resume a partial rebuild without dropping already loaded tables")
    args = parser.parse_args(argv)
    try:
        stats = validate_archive(args.data_dir)
        if args.validate_only:
            print(json.dumps({"status": "valid", **stats.as_dict()}, indent=2))
            return 0
        if not args.database_url:
            raise ValueError("Pass --database-url or set TESTAG_DATABASE_URL")
        result = build_database(Path(__file__).resolve().parent, args.data_dir, args.database_url, stats, resume=args.resume)
        print(json.dumps({"status": "database_resumed" if args.resume else "database_rebuilt", **result}, indent=2))
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
