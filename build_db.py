#!/usr/bin/env python3
"""Validate the compact score archive and build or incrementally update PostgreSQL."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
from dataclasses import dataclass, field
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
    "dxcc_adif",
    "dxcc_entity_inferred",
    "dxcc_resolution",
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
LEGACY_SCORE_FIELDS = [f for f in SCORE_FIELDS if f not in {"dxcc_adif", "dxcc_entity_inferred", "dxcc_resolution"}]
RESULT_COPY_FIELDS = [
    "entry_id",
    "contest_id",
    "edition_year",
    "mode",
    "station_callsign_source",
    "station_callsign",
    "location_code_source",
    "category_id",
    "dxcc_entity_source",
    "dxcc_adif",
    "dxcc_entity_inferred",
    "dxcc_resolution",
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
    "eligibility_basis_id",
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
    "result_id",
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
    partition_digests: dict[tuple[str, int, str], str] = field(default_factory=dict)

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
    if "dxcc_resolution" in row:
        status = row["dxcc_resolution"]
        adif = _parse_optional_int(row["dxcc_adif"], f"{label} dxcc_adif")
        if status not in DXCC_RESOLUTIONS or (adif is not None and not 1 <= adif <= 32767):
            raise ValueError(f"{label}: invalid DXCC resolution or ADIF identifier")
        if row["dxcc_entity_source"]:
            if status != "source" or row["dxcc_entity_inferred"]:
                raise ValueError(f"{label}: source DXCC must take precedence")
        elif status == "source":
            raise ValueError(f"{label}: source resolution requires a source DXCC")
        elif status == "callsign":
            if adif is None or not row["dxcc_entity_inferred"].strip():
                raise ValueError(f"{label}: callsign DXCC requires an ADIF ID and inferred label")
        elif adif is not None or row["dxcc_entity_inferred"]:
            raise ValueError(f"{label}: unresolved DXCC cannot carry an inferred entity")
    explicit_overlay = _explicit_overlay_category(partition.contest_path, row["category_source"])
    if row["overlay_category_source"] != (explicit_overlay or ""):
        raise ValueError(
            f"{label}: overlay_category_source must match a recognized explicit source category "
            f"suffix ({explicit_overlay or 'blank'!r})"
        )
    return len(operators), rank_count


DXCC_REFERENCE_COMMIT = "4adb0e70bf29faefc3b203bc355796cd0c6201fe"
DXCC_NEW_FIELDS = ["dxcc_adif", "dxcc_entity_inferred", "dxcc_resolution"]
DXCC_RESOLUTIONS = {"source", "callsign", "unresolved", "ambiguous_year", "ambiguous_prefix", "no_entity"}


class CallsignDxcc:
    """Offline adapter for a pinned CQRLOG parser; no contest date is invented."""

    def __init__(self, reference_root: Path):
        from functools import lru_cache
        root = reference_root.resolve()
        manifest_path = Path(__file__).resolve().parent / "dxcc" / "reference.json"
        self.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if self.manifest["commit"] != DXCC_REFERENCE_COMMIT:
            raise ValueError("Unexpected DXCC reference revision")
        for name, digest in self.manifest["files"].items():
            path = root / name
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError(f"DXCC reference does not match pinned snapshot: {name}")
        sys.path.insert(0, str(root / "parsers" / "python"))
        from dxcc import DxccTable, DxccSuffixRules, DxccResolver, MatchMode, build_country_table, build_deleted_table
        self.match_mode = MatchMode.PREFIX
        data = root / "data" / "dxcc"
        self.valid, self.deleted = DxccTable(), DxccTable()
        self.valid.load_from_string(build_country_table(data))
        self.deleted.load_from_string(build_deleted_table(data))
        self.rules = DxccSuffixRules()
        self.rules.load_exceptions(data / "Exceptions.tab")
        self.rules.load_ambiguous(data / "Ambiguous.tbl")
        self.resolver = DxccResolver(self.valid, self.deleted, self.rules)
        original_lookup = self.resolver.lookup
        self.probes: set[str] = set()
        def traced_lookup(call, date, *args):
            self.probes.add(call)
            return original_lookup(call, date, *args)
        self.resolver.lookup = traced_lookup
        self.cache: dict[tuple[str, int], tuple[int | None, str, str]] = {}
        self.aliases: dict[str, set[int]] = {}
        self.names: dict[int, str] = {}
        self.prefixes: set[str] = set()
        for filename in ("Country.tab", "CountryDel.tab"):
            table = DxccTable()
            table.load_from_file(data / filename)
            for index in range(table.count):
                entry = table.entry(index)
                if not entry.adif.isdigit() or int(entry.adif) <= 0:
                    continue
                adif = int(entry.adif)
                name = entry.country.encode("latin-1").decode("utf-8")
                self.names.setdefault(adif, name)
                self.aliases.setdefault(_entity_key(name), set()).add(adif)
                pattern = table.pattern(index)
                if re.fullmatch(r"[A-Z0-9]+", pattern):
                    self.prefixes.add(pattern)
        # Explicit source-label aliases, never callsign overrides.
        for alias in ("USA", "U.S.A.", "US", "United States of America"):
            self.aliases[_entity_key(alias)] = {291}
        self.candidates = []
        for table in (self.deleted, self.valid):
            exact: dict[str, list[Any]] = {}
            literal: dict[str, list[Any]] = {}
            wild: dict[str, list[Any]] = {}
            for index, kind in enumerate(table._kinds):
                entry = table.entry(index)
                if kind == 0:
                    exact.setdefault(table._targets[index], []).append(entry)
                elif kind == 1:
                    literal.setdefault(table._targets[index], []).append(entry)
                else:
                    first = table.pattern(index)[0]
                    wild.setdefault(first, []).append((table._matchers[index], entry))
            self.candidates.append((exact, literal, wild))
        self.boundaries = lru_cache(maxsize=20000)(self._boundaries)

    def _boundaries(self, key: str) -> tuple[str, ...]:
        from datetime import date, timedelta
        entries = []
        for exact, literal, wild in self.candidates:
            entries.extend(exact.get(key, ()))
            for length in range(1, len(key) + 1):
                entries.extend(literal.get(key[:length], ()))
            for regex, entry in wild.get(key[:1], ()):
                if regex.match(key):
                    entries.append(entry)
        dates = set()
        for entry in entries:
            dates.add(entry.valid_from)
            end = date.fromisoformat(entry.valid_to.replace("/", "-"))
            dates.add((end + timedelta(days=1)).isoformat().replace("-", "/"))
        return tuple(sorted(dates))

    def _on_date(self, call: str, date: str) -> tuple[int | None, str]:
        self.probes = set()
        key = self.resolver.effective_callsign(call, date).key
        self.probes.add(key)
        if key == "?":
            return None, "no_entity"
        for table in (self.deleted, self.valid):
            index = table.find(key, date, self.match_mode)
            if index < 0:
                continue
            entry = table.entry(index)
            # The upstream port loads Ambiguous.tbl but does not enforce it.
            # Accept a more specific area/exception; reject a generic ambiguous prefix.
            pattern = table.pattern(index)
            literal = re.match(r"[A-Z0-9]+", pattern)
            literal_length = len(literal.group()) if literal else 0
            for prefix in self.rules.ambiguous:
                if key.startswith(prefix) and not pattern.startswith("=") and literal_length <= len(prefix):
                    return None, "ambiguous_prefix"
            if entry.adif.isdigit() and int(entry.adif) > 0:
                return int(entry.adif), "callsign"
            return None, "unresolved"
        return None, "unresolved"

    def infer(self, call: str, year: int) -> tuple[int | None, str, str]:
        key = (call, year)
        if key in self.cache:
            return self.cache[key]
        if not re.fullmatch(r"[A-Z0-9]+(?:/[A-Z0-9]+){0,2}", call) or not any(c.isdigit() for c in call):
            result = (None, "", "unresolved")
            self.cache[key] = result
            return result
        dates = {f"{year}/01/01", f"{year}/12/31"}
        checked: dict[str, tuple[int | None, str]] = {}
        # Capture every key the resolver probes, then sample all validity boundaries
        # of matching records. Repeat when date-dependent reduction discovers new keys.
        while pending := dates - checked.keys():
            for day in sorted(pending):
                checked[day] = self._on_date(call, day)
                for probe in self.probes:
                    dates.update(d for d in self.boundaries(probe) if d.startswith(f"{year}/"))
        entities = {answer[0] for answer in checked.values()}
        if len(entities) == 1 and None not in entities:
            adif = entities.pop()
            result = (adif, self.names.get(adif, f"ADIF {adif}"), "callsign")
        elif any(entity is not None for entity in entities):
            result = (None, "", "ambiguous_year")
        else:
            statuses = {answer[1] for answer in checked.values()}
            status = "no_entity" if statuses == {"no_entity"} else "ambiguous_prefix" if "ambiguous_prefix" in statuses else "unresolved"
            result = (None, "", status)
        self.cache[key] = result
        return result

    def source_adif(self, label: str, year: int) -> int | None:
        value = label.strip()
        if value.isdigit() and int(value) > 0:
            return int(value)
        # AA publishes US call-area labels; districts share DXCC 291.
        if re.fullmatch(r"USA-[0-9]+", value.upper()):
            return 291
        aliases = self.aliases.get(_entity_key(value), set())
        if len(aliases) == 1:
            return next(iter(aliases))
        if value.upper() in self.prefixes:
            # A source prefix is an explicit entity label, not a station callsign.
            answers = set()
            dates = {f"{year}/01/01", f"{year}/12/31"}
            dates.update(d for d in self.boundaries(value.upper()) if d.startswith(f"{year}/"))
            for day in dates:
                result = self.resolver.lookup(value.upper(), day)
                answers.add(result.adif if result.found and result.adif > 0 else None)
            if len(answers) == 1:
                return next(iter(answers))
        return None


def recalculate_dxcc(data_root: Path, reference_root: Path) -> dict[str, Any]:
    validate_archive(data_root)
    parser = CallsignDxcc(reference_root)
    counts: dict[str, int] = {status: 0 for status in DXCC_RESOLUTIONS}
    source_unmapped: dict[str, int] = {}
    rank_counts = {"category_in_dxcc_ranked": 0, "overlay_in_dxcc_ranked": 0}
    for number, partition in enumerate(_partitions(data_root), start=1):
        rows = list(_score_rows(partition))
        for row in rows:
            if row["dxcc_entity_source"]:
                adif = parser.source_adif(row["dxcc_entity_source"], partition.year)
                name, status = "", "source"
                if adif is None:
                    label = row["dxcc_entity_source"]
                    source_unmapped[label] = source_unmapped.get(label, 0) + 1
            else:
                adif, name, status = parser.infer(row["station_callsign"], partition.year)
            row.update(dxcc_adif="" if adif is None else str(adif), dxcc_entity_inferred=name, dxcc_resolution=status)
            counts[status] += 1
        ranks = _calculate_partition_ranks(rows)
        for row in rows:
            for name in CALCULATED_RANK_FIELDS:
                value = ranks[row["entry_id"]][name]
                row[name] = "" if value is None else str(value)
            rank_counts["category_in_dxcc_ranked"] += bool(row["category_in_dxcc_rank_calculated"])
            rank_counts["overlay_in_dxcc_ranked"] += bool(row["overlay_in_dxcc_rank_calculated"])
        temporary = partition.scores_path.with_suffix(".csv.tmp")
        try:
            with temporary.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=SCORE_FIELDS, lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)
            temporary.replace(partition.scores_path)
        finally:
            temporary.unlink(missing_ok=True)
        if number % 25 == 0 or number == 1:
            print(f"Recalculated DXCC for {number} partitions: {counts}", file=sys.stderr, flush=True)
    stats = validate_archive(data_root)
    return dxcc_report(data_root, stats)


def dxcc_report(data_root: Path, stats: ArchiveStats) -> dict[str, Any]:
    counts = {status: 0 for status in sorted(DXCC_RESOLUTIONS)}
    by_contest: dict[str, dict[str, int]] = {}
    source_unmapped: dict[str, int] = {}
    unresolved = []
    category_ranked = overlay_ranked = 0
    for partition in _partitions(data_root):
        contest_counts = by_contest.setdefault(partition.contest_path, {status: 0 for status in counts})
        for row in _score_rows(partition):
            status = row.get("dxcc_resolution", "source" if row["dxcc_entity_source"] else "unresolved")
            counts[status] += 1
            contest_counts[status] += 1
            category_ranked += bool(row["category_in_dxcc_rank_calculated"])
            overlay_ranked += bool(row["overlay_in_dxcc_rank_calculated"])
            if status == "source" and not row.get("dxcc_adif"):
                label = row["dxcc_entity_source"]
                source_unmapped[label] = source_unmapped.get(label, 0) + 1
            elif status not in {"source", "callsign"}:
                unresolved.append((row["entry_id"], partition.contest_id, partition.year, partition.mode,
                                   row["station_callsign"], status))
    report = {"reference_commit": DXCC_REFERENCE_COMMIT, "resolution_counts": counts,
              "by_contest": by_contest, "unmapped_source_labels": source_unmapped,
              "category_in_dxcc_ranked": category_ranked, "overlay_in_dxcc_ranked": overlay_ranked,
              **stats.as_dict()}
    if data_root.resolve() == (Path(__file__).resolve().parent / "data").resolve():
        directory = Path(__file__).resolve().parent / "dxcc"
        (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        with (directory / "unresolved.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(("entry_id", "contest_id", "edition_year", "mode", "station_callsign", "reason"))
            writer.writerows(unresolved)
    return report


def _partition_digest(partition: Partition) -> str:
    digest = hashlib.sha256()
    for path in (partition.scores_path, partition.scores_path.with_name("meta.json")):
        digest.update(path.name.encode("utf-8") + b"\0")
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    reference = Path(__file__).resolve().parent / "dxcc" / "reference.json"
    if reference.is_file():
        digest.update(b"dxcc/reference.json\0" + reference.read_bytes())
    return digest.hexdigest()


def _partition_key(partition: Partition) -> tuple[str, int, str]:
    return (partition.contest_id, partition.year, partition.mode)


def _verify_archive_snapshot(data_root: Path, stats: ArchiveStats) -> None:
    current = {_partition_key(p): _partition_digest(p) for p in _partitions(data_root)}
    if current != stats.partition_digests:
        raise ValueError("Archive changed after validation; rerun with a stable archive")


def validate_archive(data_root: Path) -> ArchiveStats:
    stats = ArchiveStats()
    release_hash = hashlib.sha256()
    seen_entry_ids: set[str] = set()
    latest: datetime | None = None
    for partition in _partitions(data_root):
        stats.partitions += 1
        stats.partition_digests[_partition_key(partition)] = _partition_digest(partition)
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
            if reader.fieldnames not in (SCORE_FIELDS, LEGACY_SCORE_FIELDS):
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
    reference = Path(__file__).resolve().parent / "dxcc" / "reference.json"
    if reference.is_file():
        release_hash.update(b"dxcc/reference.json\0" + reference.read_bytes())
    stats.release_id = release_hash.hexdigest()
    return stats


def _score_rows(partition: Partition) -> Iterator[dict[str, Any]]:
    with partition.scores_path.open("r", newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        for row in reader:
            yield row


def _entity_key(value: str) -> str:
    return " ".join(value.casefold().split())


def _dxcc_key(row: dict[str, str]) -> str:
    if row.get("dxcc_adif"):
        return "adif:" + str(int(row["dxcc_adif"]))
    if row["dxcc_entity_source"].strip():
        return "source:" + _entity_key(row["dxcc_entity_source"])
    return ""


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
            lambda row: (row["category_source"], _dxcc_key(row)),
        ),
        (
            "overlay_world_rank_calculated",
            lambda row: _entity_key(row["overlay_category_source"]),
        ),
        (
            "overlay_in_dxcc_rank_calculated",
            lambda row: (
                _entity_key(row["overlay_category_source"]),
                _dxcc_key(row),
            ),
        ),
    ]
    for field, group_fn in group_specs:
        if field == "category_world_rank_calculated":
            eligible = rows
        elif field == "category_in_dxcc_rank_calculated":
            eligible = [row for row in rows if _dxcc_key(row)]
        elif field == "overlay_world_rank_calculated":
            eligible = [row for row in rows if row["overlay_category_source"].strip()]
        else:
            eligible = [
                row for row in rows
                if _dxcc_key(row) and row["overlay_category_source"].strip()
            ]
        ranked = _rank_groups(eligible, group_key=group_fn)
        for entry_id, value in ranked.items():
            ranks[entry_id][field] = value
    return ranks


def _json_value(value: str) -> Any:
    return json.loads(value)


def _db_entry(
    row: dict[str, str], partition: Partition,
    category_ids: dict[str, int], basis_ids: dict[str, int],
) -> tuple[Any, ...]:
    return (
        row["entry_id"],
        partition.contest_id,
        partition.year,
        partition.mode,
        row["station_callsign_source"],
        row["station_callsign"],
        row["location_code_source"] or None,
        category_ids[row["category_source"]],
        row["dxcc_entity_source"] or None,
        _parse_optional_int(row.get("dxcc_adif", ""), "dxcc_adif"),
        row.get("dxcc_entity_inferred", "") or None,
        row.get("dxcc_resolution", "source" if row["dxcc_entity_source"] else "unresolved"),
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
        basis_ids[row["eligibility_basis"]],
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
        json.dumps(_json_value(row["metrics_json"]), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )


def _operator_rows(row: dict[str, str], result_id: int) -> Iterator[tuple[Any, ...]]:
    for order, operator in enumerate(_json_value(row["operators_json"]), start=1):
        yield (result_id, order, operator["source"], operator["callsign"])


def _lookup_ids(cursor: Any, table: str, text_field: str, id_field: str, values: set[str]) -> dict[str, int]:
    # Identifiers come only from the two fixed builder-owned lookup definitions.
    cursor.execute(
        f"INSERT INTO {table} ({text_field}) "
        "SELECT value FROM unnest(%s::text[]) AS value "
        f"ORDER BY value ON CONFLICT ({text_field}) DO NOTHING",
        (sorted(values),),
    )
    cursor.execute(f"SELECT {text_field}, {id_field} FROM {table}")
    return dict(cursor.fetchall())


def _load_partition(connection: Any, partition: Partition, digest: str, builder_version: str) -> dict[str, int]:
    partition_counts = {"entries": 0, "operators": 0}
    rows = list(_score_rows(partition))
    with connection.cursor() as cursor:
        category_ids = _lookup_ids(
            cursor, "contest_result_category", "category_source", "category_id",
            {row["category_source"] for row in rows},
        )
        basis_ids = _lookup_ids(
            cursor, "contest_result_eligibility_basis", "eligibility_basis", "eligibility_basis_id",
            {row["eligibility_basis"] for row in rows},
        )
        result_ids: dict[str, int] = {}
        cursor.execute(
            "INSERT INTO contest_result_partition "
            "(contest_id, edition_year, mode, retrieved_at_utc, source_url, archive_digest, builder_version) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                partition.contest_id,
                partition.year,
                partition.mode,
                partition.meta["retrieved_at_utc"],
                partition.meta["source"],
                digest,
                builder_version,
            ),
        )
        for table, fields, row_source in (
            ("contest_result", RESULT_COPY_FIELDS, "entry"),
            ("contest_result_operator", OPERATOR_COPY_FIELDS, "operator"),
        ):
            if row_source == "operator":
                cursor.execute(
                    "SELECT entry_id, result_id FROM contest_result "
                    "WHERE contest_id = %s AND edition_year = %s AND mode = %s",
                    _partition_key(partition),
                )
                result_ids = dict(cursor.fetchall())
            columns = ", ".join(fields)
            output = io.StringIO(newline="")
            writer = csv.writer(output, lineterminator="\n")
            for row in rows:
                if row_source == "entry":
                    writer.writerow(_db_entry(row, partition, category_ids, basis_ids))
                    partition_counts["entries"] += 1
                else:
                    for operator_row in _operator_rows(row, result_ids[row["entry_id"]]):
                        writer.writerow(operator_row)
                        partition_counts["operators"] += 1
            payload = output.getvalue()
            with cursor.copy(
                f"COPY {table} ({columns}) FROM STDIN WITH (FORMAT CSV)"
            ) as copy:
                copy.write(payload)
    return partition_counts


def _copy_archive(connection: Any, data_root: Path, stats: ArchiveStats, builder_version: str, *, resume: bool = False) -> dict[str, int]:
    counts = {"partitions": 0, "entries": 0, "operators": 0}
    if resume:
        with connection.cursor() as cursor:
            cursor.execute("SELECT contest_id, edition_year, mode, archive_digest, builder_version FROM contest_result_partition")
            completed = set()
            for contest, year, mode, digest, version in cursor.fetchall():
                key = (contest, year, mode)
                if stats.partition_digests.get(key) != digest or version != builder_version:
                    raise ValueError("Cannot resume a changed archive or builder; use --incremental or rebuild")
                completed.add(key)
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
        partition_counts = _load_partition(
            connection, partition, stats.partition_digests[partition_key], builder_version
        )
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


def _sync_archive(
    connection: Any, data_root: Path, stats: ArchiveStats, builder_version: str,
    *, allow_removals: bool,
) -> dict[str, int]:
    changes = {"added": 0, "replaced": 0, "removed": 0, "unchanged": 0}
    with connection.cursor() as cursor:
        # Serialize writers; ordinary SELECT readers continue to use committed data.
        cursor.execute("LOCK TABLE contest_result_partition, contest_result, "
                       "contest_result_operator, contest_result_category, contest_result_eligibility_basis, "
                       "testag_dataset_release IN SHARE ROW EXCLUSIVE MODE")
        # Upgrade the previous schema without dropping its published data.
        cursor.execute("SELECT attname FROM pg_attribute "
                       "WHERE attrelid = 'contest_result_partition'::regclass "
                       "AND attnum > 0 AND NOT attisdropped")
        columns = {row[0] for row in cursor.fetchall()}
        for name in ("archive_digest", "builder_version"):
            if name not in columns:
                cursor.execute(f"ALTER TABLE contest_result_partition ADD COLUMN {name} text")
        cursor.execute("SELECT contest_id, edition_year, mode, archive_digest, builder_version "
                       "FROM contest_result_partition")
        existing = {(c, y, m): (d, v) for c, y, m, d, v in cursor.fetchall()}
        removed = existing.keys() - stats.partition_digests.keys()
        if removed and not allow_removals:
            raise ValueError(f"Archive is missing database partitions: {sorted(removed)}; "
                             "use --allow-removals with --incremental to explicitly remove them")
        changed = {key for key, digest in stats.partition_digests.items()
                   if existing.get(key) != (digest, builder_version)}
        # Support cascading partition deletion even for entries with no calculated rank.
        cursor.execute("CREATE INDEX IF NOT EXISTS contest_result_partition_idx "
                       "ON contest_result (contest_id, edition_year, mode)")
        # Delete all changed partitions before loading so entry IDs can move between partitions.
        for key in sorted(removed | (changed & existing.keys())):
            cursor.execute("DELETE FROM contest_result_partition "
                           "WHERE contest_id = %s AND edition_year = %s AND mode = %s", key)
        changes["removed"] = len(removed)
    for partition in _partitions(data_root):
        key = _partition_key(partition)
        if key not in changed:
            changes["unchanged"] += 1
            continue
        _load_partition(connection, partition, stats.partition_digests[key], builder_version)
        changes["replaced" if key in existing else "added"] += 1
    # No commit here: data changes, verification and release publication are atomic.
    return changes


def _sql_statements(sql: str) -> list[str]:
    return [statement.strip() for statement in sql.split(";") if statement.strip()]


def build_database(
    repository_root: Path,
    data_root: Path,
    database_url: str,
    stats: ArchiveStats,
    *,
    resume: bool = False,
    incremental: bool = False,
    allow_removals: bool = False,
) -> dict[str, int | str]:
    if resume and incremental:
        raise ValueError("--resume and --incremental are mutually exclusive")
    if allow_removals and not incremental:
        raise ValueError("--allow-removals requires --incremental")
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("Install psycopg 3 with its binary extra before building PostgreSQL") from exc

    schema_path = repository_root / "db" / "schema.sql"
    if not schema_path.is_file():
        raise ValueError(f"Missing canonical database schema: {schema_path}")
    schema_text = schema_path.read_text(encoding="utf-8")
    # Bump the semantic version when conversion/ranking behavior changes.
    builder_version = hashlib.sha256(("testag-builder-v4\0" + schema_text).encode("utf-8")).hexdigest()
    schema_statements = _sql_statements(schema_text)
    table_statements = [
        statement for statement in schema_statements
        if not statement.lstrip().upper().startswith("CREATE INDEX ")
    ]
    index_statements = [
        statement for statement in schema_statements
        if statement.lstrip().upper().startswith("CREATE INDEX ")
    ]
    _verify_archive_snapshot(data_root, stats)
    with psycopg.connect(database_url, client_encoding="UTF8") as connection:
        if resume or incremental:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT to_regclass('public.contest_result_partition'), "
                    "to_regclass('public.contest_result'), "
                    "to_regclass('public.contest_result_operator'), "
                    "to_regclass('public.testag_dataset_release')"
                )
                if any(value is None for value in cursor.fetchone()):
                    raise ValueError("TESTAG tables are missing; run a full rebuild first")
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT to_regclass('public.contest_result_category'), "
                    "to_regclass('public.contest_result_eligibility_basis'), "
                    "(SELECT data_type FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='contest_result' AND column_name='result_id'), "
                    "(SELECT data_type FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='contest_result' AND column_name='metrics_json'), "
                    "(SELECT data_type FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='contest_result' AND column_name='dxcc_resolution'), "
                    "(SELECT udt_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='contest_result' AND column_name='award_eligibility')"
                )
                category_table, basis_table, id_type, metrics_type, resolution_type, eligibility_type = cursor.fetchone()
                if category_table is None or basis_table is None or id_type != "integer" or metrics_type != "json" or resolution_type != "text" or eligibility_type != "testag_award_eligibility":
                    raise ValueError("Compact database schema requires a full rebuild; cannot resume or incrementally update the old schema")
        else:
            with connection.cursor() as cursor:
                for statement in table_statements:
                    cursor.execute(statement)
            connection.commit()
        _verify_archive_snapshot(data_root, stats)
        changes: dict[str, int] = {}
        if incremental:
            changes = _sync_archive(connection, data_root, stats, builder_version, allow_removals=allow_removals)
            loaded = {"partitions": stats.partitions, "entries": stats.entries, "operators": stats.operators}
        else:
            loaded = _copy_archive(connection, data_root, stats, builder_version, resume=resume)
        _verify_archive_snapshot(data_root, stats)
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
            if incremental:
                cursor.execute("SELECT release_id FROM testag_dataset_release")
                published = cursor.fetchall()
                if published == [(stats.release_id,)] and not any(changes[k] for k in ("added", "replaced", "removed")):
                    return {**stats.as_dict(), **loaded, **changes}
                cursor.execute("DELETE FROM testag_dataset_release")
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
    return {**stats.as_dict(), **loaded, **changes}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate score CSVs and build or update the TESTAG PostgreSQL database")
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent / "data")
    parser.add_argument("--database-url", default=os.environ.get("TESTAG_DATABASE_URL"))
    parser.add_argument("--validate-only", action="store_true", help="Validate the archive without connecting to PostgreSQL")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--recalculate-dxcc", action="store_true", help="Fill missing DXCC from the pinned offline parser and recalculate archive ranks")
    parser.add_argument("--dxcc-reference-dir", type=Path, help="Local pinned CQRLOG reference snapshot; required for --recalculate-dxcc")
    modes.add_argument("--incremental", action="store_true", help="Atomically load new or changed partitions without rebuilding")
    parser.add_argument("--allow-removals", action="store_true", help="Allow --incremental to delete partitions missing from the archive")
    modes.add_argument("--resume", action="store_true", help="Resume a partial rebuild without dropping already loaded tables")
    args = parser.parse_args(argv)
    try:
        if args.allow_removals and not args.incremental:
            raise ValueError("--allow-removals requires --incremental")
        if args.recalculate_dxcc:
            if args.validate_only or args.allow_removals:
                raise ValueError("--recalculate-dxcc cannot be combined with validation or removals")
            if not args.dxcc_reference_dir:
                raise ValueError("--recalculate-dxcc requires --dxcc-reference-dir")
            result = recalculate_dxcc(args.data_dir, args.dxcc_reference_dir)
            print(json.dumps({"status": "dxcc_recalculated", **result}, indent=2))
            return 0
        stats = validate_archive(args.data_dir)
        if args.validate_only:
            print(json.dumps({"status": "valid", **stats.as_dict()}, indent=2))
            return 0
        if not args.database_url:
            raise ValueError("Pass --database-url or set TESTAG_DATABASE_URL")
        result = build_database(
            Path(__file__).resolve().parent, args.data_dir, args.database_url, stats,
            resume=args.resume, incremental=args.incremental, allow_removals=args.allow_removals,
        )
        status = "database_updated" if args.incremental else "database_resumed" if args.resume else "database_rebuilt"
        print(json.dumps({"status": status, **result}, indent=2))
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
