#!/usr/bin/env python3
"""Normalize fetched 116117 pages into relational tables and a web directory."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable


TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "snapshots": ("snapshot_id", "fetched_at", "page_count", "raw_record_count", "unique_record_count", "duplicate_count"),
    "practices": ("practice_id", "strasse", "hausnummer", "plz", "ort", "district", "tel", "fax", "email", "web", "lat", "lon", "is_berlin", "doctor_count"),
    "doctors": (
        "doctor_id", "practice_id", "vorname", "name", "titel", "namezusatz", "anrede", "geschlecht",
        "tel", "fax", "email", "web", "source_rank", "focus", "has_psychotherapy", "language_count",
        "access_level", "open_days", "weekly_open_hours", "has_early_slots", "has_evening_slots",
        "has_saturday_slots", "next_open_date", "no_hours_listed", "keineSprechzeiten",
    ),
    "doctor_languages": ("doctor_id", "code", "name"),
    "doctor_specialties": ("doctor_id", "code", "name", "parent_code"),
    "doctor_services": ("doctor_id", "heading", "service"),
    "doctor_qualifications": ("doctor_id", "kind", "code", "name"),
    "doctor_accessibility": ("doctor_id", "scheme", "code", "name"),
    "availability_slots": ("doctor_id", "date", "type", "start", "end", "minutes"),
}

TEXT_KEYS = {"code", "id", "key", "schluessel", "wert", "name", "label", "title", "titel", "bezeichnung", "text", "value"}
CHILD_KEYS = {"children", "child", "items", "values", "services", "sprechzeiten", "angebote", "features", "unterpunkte", "unterkategorien"}
FOCUS_CODES = {"516": "fertility", "517": "perinatal", "515": "oncology"}
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TIME_PATTERN = re.compile(r"^\s*(\d{1,2}:\d{2})\s*[-–]\s*(\d{1,2}:\d{2})\s*$")


def clean_text(value: Any) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    return str(value).strip()


def normalized(value: Any) -> str:
    return " ".join(clean_text(value).casefold().split())


def normalize_street(value: Any) -> str:
    street = normalized(value).replace("ß", "ss")
    return re.sub(r"(?:strasse|str\.)\s*$", "strasse", street)


def digits(value: Any) -> str:
    return re.sub(r"\D", "", clean_text(value))


def stable_id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]


def doctor_identity(record: dict[str, Any]) -> str:
    fields = ("vorname", "name", "strasse", "hausnummer", "plz", "tel")
    parts = [normalized(record.get(field)) for field in fields]
    parts[2] = normalize_street(record.get("strasse"))
    parts[-1] = digits(record.get("tel"))
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def nonempty(value: Any) -> bool:
    return value not in (None, "", [], {})


def truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() not in {"", "0", "false", "nein", "no", "none"}
    return bool(value)


def code_name_pairs(value: Any, parent_code: str = "") -> list[tuple[str, str, str]]:
    pairs: list[tuple[str, str, str]] = []

    def walk(node: Any, inherited_parent: str = "") -> None:
        if isinstance(node, list):
            for child in node:
                walk(child, inherited_parent)
            return
        if isinstance(node, dict):
            lowered = {str(key).casefold(): child for key, child in node.items()}
            code = next((clean_text(lowered[key]) for key in ("code", "id", "key", "schluessel", "wert") if clean_text(lowered.get(key))), "")
            label = next((clean_text(lowered[key]) for key in ("name", "label", "title", "bezeichnung", "text", "value") if clean_text(lowered.get(key))), "")
            current_parent = inherited_parent
            if code or label:
                pairs.append((code, label, inherited_parent))
                current_parent = code or inherited_parent
            known = TEXT_KEYS | CHILD_KEYS
            for key, child in node.items():
                key_text = str(key)
                key_lower = key_text.casefold()
                if key_lower in known:
                    if isinstance(child, (dict, list)):
                        walk(child, current_parent)
                elif key_text.isdigit():
                    if isinstance(child, (str, int, float)):
                        pairs.append((key_text, clean_text(child), inherited_parent))
                    else:
                        walk(child, key_text)
                elif isinstance(child, (dict, list)):
                    walk(child, current_parent)
            return
        if node is not None:
            pairs.append(("", clean_text(node), inherited_parent))

    walk(value, parent_code)
    unique: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for pair in pairs:
        if pair not in seen and (pair[0] or pair[1]):
            unique.append(pair)
            seen.add(pair)
    return unique


def has_child_code(value: Any, wanted: set[str]) -> bool:
    return any(code in wanted for code, _, _ in code_name_pairs(value))


def email_value(value: Any) -> str:
    email = clean_text(value).lower()
    return email if EMAIL_PATTERN.fullmatch(email) else ""


def web_value(value: Any) -> str:
    web = clean_text(value)
    return re.sub(r"^http://https://", "https://", web, flags=re.IGNORECASE)


def berlin_record(record: dict[str, Any]) -> bool:
    postal_code = clean_text(record.get("plz"))
    city = normalized(record.get("ort"))
    return postal_code.isdigit() and 10115 <= int(postal_code) <= 14199 and city == "berlin"


def parse_date(value: Any) -> date | None:
    raw = clean_text(value)
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def parse_time(value: Any) -> tuple[int, str] | None:
    match = TIME_PATTERN.fullmatch(clean_text(value))
    if not match:
        return None
    parsed: list[tuple[int, str]] = []
    for text in match.groups():
        try:
            parsed_time = datetime.strptime(text, "%H:%M").time()
        except ValueError:
            return None
        parsed.append((parsed_time.hour * 60 + parsed_time.minute, parsed_time.strftime("%H:%M")))
    return parsed[0][0], f"{parsed[0][1]}|{parsed[1][1]}"


def time_entries(tsz: Any) -> tuple[list[dict[str, Any]], int, dict[date, list[tuple[int, int]]], int]:
    rows: list[dict[str, Any]] = []
    invalid_count = 0
    intervals: dict[date, list[tuple[int, int]]] = defaultdict(list)
    window_days = len(tsz) if isinstance(tsz, list) else 0
    if not isinstance(tsz, list):
        return rows, invalid_count, intervals, window_days
    for day_entry in tsz:
        if not isinstance(day_entry, dict):
            continue
        open_date = parse_date(day_entry.get("d"))
        for type_entry in day_entry.get("typTsz") or []:
            if not isinstance(type_entry, dict):
                continue
            slot_type = clean_text(type_entry.get("typ"))
            for slot in type_entry.get("sprechzeiten") or []:
                raw_time = slot.get("z") if isinstance(slot, dict) else slot
                if not clean_text(raw_time):
                    continue
                parsed = parse_time(raw_time)
                if parsed is None or open_date is None:
                    invalid_count += 1
                    continue
                start_minutes, normalized_pair = parsed
                start_text, end_text = normalized_pair.split("|")
                end_minutes = int(end_text[:2]) * 60 + int(end_text[3:])
                duration = end_minutes - start_minutes
                if duration <= 0:
                    duration += 24 * 60
                rows.append({
                    "date": open_date.isoformat(),
                    "type": slot_type,
                    "start": start_text,
                    "end": end_text,
                    "minutes": duration,
                })
                intervals[open_date].append((start_minutes, start_minutes + duration))
    return rows, invalid_count, intervals, window_days


def merged_intervals(intervals: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def services_from(value: Any) -> list[tuple[str, str]]:
    output: list[tuple[str, str]] = []

    def walk(node: Any, heading: str = "") -> None:
        if isinstance(node, list):
            for child in node:
                walk(child, heading)
            return
        if not isinstance(node, dict):
            text = clean_text(node)
            if text:
                output.append((heading, text))
            return
        lower = {str(key).casefold(): child for key, child in node.items()}
        label = next((clean_text(lower[key]) for key in ("heading", "headingname", "category", "title", "name", "bezeichnung") if clean_text(lower.get(key))), "")
        current_heading = label or heading
        child_values = [child for key, child in node.items() if str(key).casefold() in CHILD_KEYS]
        if child_values:
            for child in child_values:
                walk(child, current_heading)
            return
        service = next((clean_text(lower[key]) for key in ("service", "value", "text", "label", "name", "bezeichnung") if clean_text(lower.get(key))), "")
        if service and (service != current_heading or not current_heading):
            output.append((current_heading, service))
        elif label:
            output.append((heading, label))
        else:
            for child in node.values():
                if isinstance(child, (dict, list)):
                    walk(child, current_heading)

    walk(value)
    return list(dict.fromkeys(output))


def build_tables(raw_dir: Path, berlin_only: bool = False) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    page_files = sorted(raw_dir.glob("page_*.json"))
    if not page_files:
        raise FileNotFoundError(f"No page_*.json files found in {raw_dir}")

    all_records: list[dict[str, Any]] = []
    for page_file in page_files:
        payload = json.loads(page_file.read_text(encoding="utf-8"))
        records = payload.get("arztPraxisDatas") or []
        if isinstance(records, list):
            all_records.extend(record for record in records if isinstance(record, dict))

    seen_records: set[str] = set()
    unique_records: list[dict[str, Any]] = []
    for record in all_records:
        identity = doctor_identity(record)
        if identity in seen_records:
            continue
        seen_records.add(identity)
        unique_records.append(record)
    duplicates = len(all_records) - len(unique_records)
    excluded_berlin = sum(not berlin_record(record) for record in unique_records) if berlin_only else 0
    records = [record for record in unique_records if not berlin_only or berlin_record(record)]

    table_rows: dict[str, list[dict[str, Any]]] = {name: [] for name in TABLE_COLUMNS}
    snapshot_dir = raw_dir
    manifest_path = snapshot_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    snapshot_id = clean_text(manifest.get("snapshot")) or snapshot_dir.name
    table_rows["snapshots"].append({
        "snapshot_id": snapshot_id,
        "fetched_at": manifest.get("updated_at") or manifest.get("started_at") or datetime.now(timezone.utc).isoformat(),
        "page_count": len(page_files),
        "raw_record_count": len(all_records),
        "unique_record_count": len(unique_records),
        "duplicate_count": duplicates,
    })

    practices: dict[str, dict[str, Any]] = {}
    doctors: dict[str, dict[str, Any]] = {}
    slot_rows: list[dict[str, Any]] = []
    invalid_times = 0

    for record in records:
        street = clean_text(record.get("strasse"))
        house_number = clean_text(record.get("hausnummer"))
        postal_code = clean_text(record.get("plz"))
        phone = digits(record.get("tel"))
        practice_id = stable_id(normalize_street(street), normalized(house_number), normalized(postal_code), phone)
        if practice_id not in practices:
            practices[practice_id] = {
                "practice_id": practice_id,
                "strasse": street,
                "hausnummer": house_number,
                "plz": postal_code,
                "ort": clean_text(record.get("ort")),
                "district": "",
                "tel": clean_text(record.get("tel")),
                "fax": clean_text(record.get("fax")),
                "email": email_value(record.get("email")),
                "web": web_value(record.get("web")),
                "lat": record.get("lat"),
                "lon": record.get("lon"),
                "is_berlin": int(berlin_record(record)),
                "doctor_count": 0,
            }

        first_name = normalized(record.get("vorname"))
        name_prefix = normalized(record.get("namezusatz"))
        last_name = normalized(record.get("name"))
        doctor_id = stable_id(first_name, name_prefix, last_name, practice_id)
        if doctor_id in doctors:
            continue

        language_pairs = code_name_pairs(record.get("fs"))
        language_pairs = [pair for pair in language_pairs if pair[0] or pair[1]]
        specialty_pairs = code_name_pairs(record.get("fg"))
        access_pairs = code_name_pairs(record.get("bf"))
        detailed_pairs = code_name_pairs(record.get("bfk2025"))
        legacy_codes = {code for code, _, _ in access_pairs if code}
        if "104" in legacy_codes:
            access_level = "none"
        elif "101" in legacy_codes:
            access_level = "full"
        elif legacy_codes & {"102", "103"}:
            access_level = "partial"
        elif nonempty(record.get("bfk2025")) and not legacy_codes:
            access_level = "detailed"
        else:
            access_level = "unknown"

        slots, invalid_count, day_intervals, window_days = time_entries(record.get("tsz"))
        invalid_times += invalid_count
        for slot in slots:
            slot_rows.append({"doctor_id": doctor_id, **slot})
        merged_by_day = {day: merged_intervals(intervals) for day, intervals in day_intervals.items()}
        total_minutes = sum(end - start for ranges in merged_by_day.values() for start, end in ranges)
        open_days = sum(bool(ranges) for ranges in merged_by_day.values())
        window_days = window_days or 28
        weekly_hours = round(total_minutes / 60 / (window_days / 7), 2)
        valid_dates = sorted(day for day, ranges in merged_by_day.items() if ranges)
        today = date.today()
        next_open = next((day.isoformat() for day in valid_dates if day >= today), "")
        all_intervals = [interval for ranges in merged_by_day.values() for interval in ranges]
        doctors[doctor_id] = {
            "doctor_id": doctor_id,
            "practice_id": practice_id,
            "vorname": clean_text(record.get("vorname")),
            "name": clean_text(record.get("name")),
            "titel": clean_text(record.get("titel")),
            "namezusatz": clean_text(record.get("namezusatz")),
            "anrede": clean_text(record.get("anrede")),
            "geschlecht": clean_text(record.get("geschlecht")),
            "tel": clean_text(record.get("tel")),
            "fax": clean_text(record.get("fax")),
            "email": email_value(record.get("email")),
            "web": web_value(record.get("web")),
            "source_rank": clean_text(record.get("id")),
            "focus": next((FOCUS_CODES[code] for code in ("516", "517", "515") if has_child_code(record.get("fg"), {code})), "general"),
            "has_psychotherapy": int(truthy(record.get("psy"))),
            "language_count": len({code or normalized(label) for code, label, _ in language_pairs}),
            "access_level": access_level,
            "open_days": open_days,
            "weekly_open_hours": weekly_hours,
            "has_early_slots": int(any(start <= 8 * 60 for start, _ in all_intervals)),
            "has_evening_slots": int(any(end >= 18 * 60 for _, end in all_intervals)),
            "has_saturday_slots": int(any(day.weekday() == 5 and ranges for day, ranges in merged_by_day.items())),
            "next_open_date": next_open,
            "no_hours_listed": int(
                truthy(record.get("keineSprechzeiten"))
                or not any(clean_text(slot.get("z")) for day in (record.get("tsz") or []) if isinstance(day, dict)
                           for type_entry in (day.get("typTsz") or []) if isinstance(type_entry, dict)
                           for slot in (type_entry.get("sprechzeiten") or []) if isinstance(slot, dict))
            ),
            "keineSprechzeiten": int(truthy(record.get("keineSprechzeiten"))),
        }
        practices[practice_id]["doctor_count"] += 1

        for code, name, parent_code in language_pairs:
            table_rows["doctor_languages"].append({"doctor_id": doctor_id, "code": code, "name": name})
        for code, name, parent_code in specialty_pairs:
            table_rows["doctor_specialties"].append({"doctor_id": doctor_id, "code": code, "name": name, "parent_code": parent_code})
        for heading, service in services_from(record.get("kvg")):
            table_rows["doctor_services"].append({"doctor_id": doctor_id, "heading": heading, "service": service})
        for field, kind in (("zb", "additional_training"), ("psy", "psychotherapy"), ("zm", "second_opinion")):
            for code, name, _ in code_name_pairs(record.get(field)):
                table_rows["doctor_qualifications"].append({"doctor_id": doctor_id, "kind": kind, "code": code, "name": name})
        for code, name, _ in access_pairs:
            table_rows["doctor_accessibility"].append({"doctor_id": doctor_id, "scheme": "legacy", "code": code, "name": name})
        for code, name, _ in detailed_pairs:
            table_rows["doctor_accessibility"].append({"doctor_id": doctor_id, "scheme": "2025", "code": code, "name": name})

    table_rows["practices"] = list(practices.values())
    table_rows["doctors"] = list(doctors.values())
    table_rows["availability_slots"] = slot_rows
    for name, rows in table_rows.items():
        if name not in {"snapshots", "practices", "doctors", "availability_slots"}:
            table_rows[name] = list(dict.fromkeys(tuple(sorted(row.items())) for row in rows))
            table_rows[name] = [dict(row) for row in table_rows[name]]

    coverage_fields = ("vorname", "name", "titel", "namezusatz", "strasse", "hausnummer", "plz", "ort", "tel", "email", "web", "fg", "fs", "bf", "bfk2025", "tsz")
    coverage = {
        field: {"present": sum(nonempty(record.get(field)) for record in unique_records), "total": len(unique_records)}
        for field in coverage_fields
    }
    qa = {
        "snapshot": snapshot_id,
        "pages": len(page_files),
        "raw_records": len(all_records),
        "unique_records": len(unique_records),
        "duplicate_records": duplicates,
        "records_excluded_by_berlin_filter": excluded_berlin,
        "practices": len(practices),
        "doctors": len(doctors),
        "berlin_practices": sum(row["is_berlin"] for row in practices.values()),
        "availability_slots": len(slot_rows),
        "unparseable_times": invalid_times,
        "field_coverage": coverage,
        "output_filter": "berlin-only" if berlin_only else "all records",
    }
    return table_rows, qa


def sqlite_type(column: str) -> str:
    if column in {"is_berlin", "doctor_count", "source_rank", "has_psychotherapy", "language_count", "open_days", "has_early_slots", "has_evening_slots", "has_saturday_slots", "no_hours_listed", "keineSprechzeiten", "page_count", "raw_record_count", "unique_record_count", "duplicate_count", "minutes"}:
        return "INTEGER"
    if column in {"lat", "lon", "weekly_open_hours"}:
        return "REAL"
    return "TEXT"


def write_outputs(table_rows: dict[str, list[dict[str, Any]]], qa: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_dir = output_dir / "csv"
    csv_dir.mkdir(exist_ok=True)
    for table, columns in TABLE_COLUMNS.items():
        with (csv_dir / f"{table}.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(table_rows[table])

    database_path = output_dir / "directory.sqlite"
    with sqlite3.connect(database_path) as connection:
        for table, columns in TABLE_COLUMNS.items():
            column_defs = ", ".join(f'"{column}" {sqlite_type(column)}' for column in columns)
            connection.execute(f'DROP TABLE IF EXISTS "{table}"')
            connection.execute(f'CREATE TABLE "{table}" ({column_defs})')
            placeholders = ", ".join("?" for _ in columns)
            column_names = ", ".join(f'"{column}"' for column in columns)
            if table_rows[table]:
                connection.executemany(
                    f'INSERT INTO "{table}" ({column_names}) VALUES ({placeholders})',
                    [tuple(row.get(column) for column in columns) for row in table_rows[table]],
                )
        connection.execute("DROP VIEW IF EXISTS v_berlin_doctors")
        connection.execute(
            "CREATE VIEW v_berlin_doctors AS "
            "SELECT doctors.*, practices.plz, practices.ort, practices.strasse, practices.hausnummer "
            "FROM doctors JOIN practices USING (practice_id) WHERE practices.is_berlin = 1"
        )

    practices_web = []
    doctors_by_practice: dict[str, list[dict[str, Any]]] = defaultdict(list)
    nested_tables = {
        "languages": ("doctor_languages", ("code", "name")),
        "specialties": ("doctor_specialties", ("code", "name", "parent_code")),
        "services": ("doctor_services", ("heading", "service")),
        "qualifications": ("doctor_qualifications", ("kind", "code", "name")),
        "accessibility": ("doctor_accessibility", ("scheme", "code", "name")),
    }
    nested_by_doctor: dict[str, dict[str, list[dict[str, str]]]] = defaultdict(dict)
    for nested_name, (table_name, fields) in nested_tables.items():
        grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in table_rows[table_name]:
            grouped[row["doctor_id"]].append({field: row[field] for field in fields})
        for doctor_id, values in grouped.items():
            nested_by_doctor[doctor_id][nested_name] = values
    for doctor in table_rows["doctors"]:
        doctor_data: dict[str, Any] = {
            key: doctor[key]
            for key in (
                "doctor_id", "vorname", "name", "titel", "namezusatz", "geschlecht", "email", "web", "focus",
                "has_psychotherapy", "language_count", "access_level", "open_days", "weekly_open_hours",
                "has_early_slots", "has_evening_slots", "has_saturday_slots", "next_open_date", "no_hours_listed",
            )
        }
        for nested_name in nested_tables:
            doctor_data[nested_name] = nested_by_doctor[doctor["doctor_id"]].get(nested_name, [])
        doctors_by_practice[doctor["practice_id"]].append(doctor_data)
    for practice in table_rows["practices"]:
        practices_web.append({
            "practice_id": practice["practice_id"],
            "address": {
                "street": practice["strasse"],
                "house_number": practice["hausnummer"],
                "postal_code": practice["plz"],
                "city": practice["ort"],
                "district": practice["district"],
            },
            "contact": {"phone": practice["tel"], "fax": practice["fax"], "email": practice["email"], "web": practice["web"]},
            "location": {"lat": practice["lat"], "lon": practice["lon"]},
            "is_berlin": bool(practice["is_berlin"]),
            "doctors": doctors_by_practice[practice["practice_id"]],
        })
    (output_dir / "practices_web.json").write_text(json.dumps(practices_web, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        f"Snapshot: {qa['snapshot']}",
        f"Filter: {qa['output_filter']}",
        f"Pages: {qa['pages']}",
        f"Raw records: {qa['raw_records']}",
        f"Unique records: {qa['unique_records']}",
        f"Duplicate records removed: {qa['duplicate_records']}",
        f"Records excluded by Berlin filter: {qa['records_excluded_by_berlin_filter']}",
        f"Practices: {qa['practices']}",
        f"Berlin practices: {qa['berlin_practices']}",
        f"Doctors: {qa['doctors']}",
        f"Availability slots: {qa['availability_slots']}",
        f"Unparseable times: {qa['unparseable_times']}",
        "",
        "Field coverage (present / unique records):",
    ]
    for field, counts in qa["field_coverage"].items():
        lines.append(f"  {field}: {counts['present']} / {counts['total']}")
    (output_dir / "qa_report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", help="Snapshot directory name under data/raw")
    parser.add_argument("--raw-root", default="data/raw")
    parser.add_argument("--output-root", default="data/processed")
    parser.add_argument("--berlin-only", action="store_true", help="Keep PLZ 10115-14199 records whose city is Berlin")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    raw_dir = Path(args.raw_root) / args.snapshot
    output_dir = Path(args.output_root) / args.snapshot
    try:
        table_rows, qa = build_tables(raw_dir, berlin_only=args.berlin_only)
        write_outputs(table_rows, qa, output_dir)
    except (OSError, ValueError, sqlite3.Error, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print((output_dir / "qa_report.txt").read_text(encoding="utf-8"))
    print(f"Outputs written to {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())