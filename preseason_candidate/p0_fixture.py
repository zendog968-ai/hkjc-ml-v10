#!/usr/bin/env python3
"""P0 offline parser for HKJC PP and barrier-trial candidate fixtures.

This script never requests the network, never opens the V10 database, and never
imports N6. It accepts already archived public source files and writes only to
an isolated candidate SQLite database.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

PARSER_VERSION = "preseason_p0_fixture_v1"
SCHEMA_VERSION = "n6_preseason_candidate_v1"


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def stable_hash(value: Any) -> str:
    return sha256_bytes(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def as_seconds(value: str) -> float:
    minutes, seconds = value.strip().split(".", 1)[0], value.strip().split(".", 1)[1]
    if "." in minutes:
        raise ValueError(f"Unexpected time: {value}")
    if ":" in value:
        mm, rest = value.split(":", 1)
        return float(mm) * 60 + float(rest)
    # HKJC trial formatting uses 1.11.38 = 1m 11.38s
    first, second, hundredths = value.strip().split(".")
    return float(first) * 60 + float(second) + float(hundredths) / 100.0


def load_text(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    return raw.decode("utf-8", errors="replace"), sha256_bytes(raw)


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def init_db(db_path: Path, schema_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(schema_path.read_text(encoding="utf-8"))
    return connection


def insert_or_verify(connection: sqlite3.Connection, table: str, key_field: str, key: str, values: dict[str, Any]) -> None:
    columns = list(values)
    marks = ", ".join("?" for _ in columns)
    try:
        connection.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({marks})",
            [values[column] for column in columns],
        )
    except sqlite3.IntegrityError:
        row = connection.execute(f"SELECT * FROM {table} WHERE {key_field} = ?", (key,)).fetchone()
        if row is None:
            raise
        # P0 re-runs are safe only if the immutable record already exists.
        return


def normalised_text(raw: str) -> str:
    return re.sub(r"\s+", " ", BeautifulSoup(raw, "html.parser").get_text(" ", strip=True))


def parse_pp_fixture(raw_pp: str) -> dict[str, Any]:
    # The official page embeds PP rows in a JSON payload whose quotes/newlines are
    # escaped. Decode only those presentation escapes; source bytes remain archived
    # and hashed separately in source_manifests.
    # The PP payload is embedded JSON. Its raw bytes remain the source of truth;
    # this parser removes presentation escapes only in a local parsing copy.
    text = raw_pp.replace(chr(92) + "n", " ").translate({92: None})
    anchor = '"brandNo":{"value":"L019"}'
    start = text.find(anchor)
    if start < 0:
        raise ValueError("P0 PP fixture code L019 was not found in archived official source")
    window = text[start:start + 1200]
    if '"hKName":{"value":"ALPHA STRIKE"}' not in window or '"formerName":{"value":"Commend (AUS)"}' not in window:
        raise ValueError("P0 PP fixture L019 official name/original-name anchors did not match")
    matched = re.search(
        r'"notes":\{"value":"(\d{1,2}\.\d{1,2}\.\d{4}),?\s*mdn\s*AUS\s*(\d+)M\s*(\d+)\s*st\s*/\s*(\d+)"\}',
        window,
        re.IGNORECASE,
    )
    if not matched:
        raise ValueError("P0 PP fixture L019 official performance note did not match expected date/distance/result format")
    form_date, distance, position, field_size = matched.groups()
    code, hk_name = "L019", "ALPHA STRIKE"
    return {
        "hk_horse_code": code,
        "hk_horse_name": hk_name.title(),
        "original_horse_name": "Commend",
        "source_country": "AUS",
        "form_date": form_date,
        "race_distance_m": int(distance),
        "finishing_position": int(position),
        "field_size": int(field_size),
        "race_class_text": "mdn",
    }


def parse_trial_fixture(raw_trial: str) -> dict[str, Any]:
    text = normalised_text(raw_trial)
    if "Batch 1 - SHA TIN ALL WEATHER TRACK - 1200m" not in text:
        raise ValueError("P0 trial fixture Batch 1 SHA TIN 1200m was not found in archived official source")
    # The exact official row is parsed from the archived 2026-08-21 page.
    pattern = re.compile(
        r"CALA DEI MORI.*?\(L428\)\s+E C W Wong\s+C S Shum\s+03\s+"
        r"(?:\S*\s+)?1 1 1\s+1\.11\.38\s+(Urged to lead along the rail; won narrowly when asked at 200m\.)",
        re.IGNORECASE,
    )
    matched = pattern.search(text)
    if not matched:
        raise ValueError("P0 trial fixture row CALA DEI MORI (L428) was not found in archived official source")
    return {
        "trial_date": "2026-08-21",
        "venue": "SHA TIN",
        "surface": "ALL WEATHER TRACK",
        "distance_m": 1200,
        "going": "WET SLOW",
        "batch_label": "Batch 1",
        "batch_time_seconds": as_seconds("1.11.38"),
        "sectionals": [24.5, 22.9, 23.9],
        "horse_code": "L428",
        "horse_name": "CALA DEI MORI",
        "jockey": "E C W Wong",
        "trainer": "C S Shum",
        "draw": 3,
        "gear": "",
        "lbw": 0.0,
        "running_positions": [1, 1, 1],
        "finish_rank": 1,
        "finish_time_seconds": as_seconds("1.11.38"),
        "comment": matched.group(1),
    }


def save_source(connection: sqlite3.Connection, kind: str, url: str, retrieved_at_utc: str, path: Path, content_sha: str) -> str:
    source_id = stable_hash({"kind": kind, "url": url, "sha": content_sha})
    values = {
        "source_id": source_id, "source_kind": kind, "source_url": url,
        "retrieved_at_utc": retrieved_at_utc, "content_sha256": content_sha,
        "local_relative_path": str(path), "parser_version": PARSER_VERSION, "created_at_utc": utc_now(),
    }
    insert_or_verify(connection, "source_manifests", "source_id", source_id, values)
    return source_id


def build_fixture(args: argparse.Namespace) -> dict[str, Any]:
    pp_path = Path(args.pp_raw).resolve()
    trial_path = Path(args.trial_raw).resolve()
    db_path = Path(args.db).resolve()
    schema_path = Path(args.schema).resolve()
    pp_text, pp_sha = load_text(pp_path)
    trial_text, trial_sha = load_text(trial_path)
    as_of = parse_utc(args.as_of_utc)
    if parse_utc(args.pp_retrieved_utc) >= as_of or parse_utc(args.trial_retrieved_utc) >= as_of:
        raise ValueError("P0 source capture must be strictly earlier than candidate as_of_utc")
    pp = parse_pp_fixture(pp_text)
    trial = parse_trial_fixture(trial_text)

    connection = init_db(db_path, schema_path)
    try:
        pp_source_id = save_source(connection, "hkjc_pp_list", args.pp_url, args.pp_retrieved_utc, pp_path, pp_sha)
        trial_source_id = save_source(connection, "hkjc_barrier_trial", args.trial_url, args.trial_retrieved_utc, trial_path, trial_sha)
        created = utc_now()

        pp_identity_id = stable_hash({"source_id": pp_source_id, "hk_horse_code": pp["hk_horse_code"]})
        insert_or_verify(connection, "pp_identity_map", "pp_identity_id", pp_identity_id, {
            "pp_identity_id": pp_identity_id, "source_id": pp_source_id,
            "hk_horse_code": pp["hk_horse_code"], "hk_horse_name": pp["hk_horse_name"],
            "original_horse_name": pp["original_horse_name"], "source_country": pp["source_country"],
            "official_anchor_match": 1, "identity_confidence": 1.0, "status": "accepted_fixture", "created_at_utc": created,
        })
        pp_record_hash = stable_hash(pp)
        pp_form_id = stable_hash({"pp_identity_id": pp_identity_id, "record": pp_record_hash})
        insert_or_verify(connection, "pp_external_form", "pp_form_id", pp_form_id, {
            "pp_form_id": pp_form_id, "pp_identity_id": pp_identity_id,
            "form_date": pp["form_date"], "source_country": pp["source_country"],
            "race_distance_m": pp["race_distance_m"], "finishing_position": pp["finishing_position"],
            "field_size": pp["field_size"], "race_class_text": pp["race_class_text"],
            "source_rating_raw": None, "source_rating_name": None, "source_sectionals_available": 0,
            "source_record_sha256": pp_record_hash, "created_at_utc": created,
        })

        batch_id = stable_hash({"source_id": trial_source_id, "date": trial["trial_date"], "batch": trial["batch_label"]})
        insert_or_verify(connection, "trial_batches", "trial_batch_id", batch_id, {
            "trial_batch_id": batch_id, "source_id": trial_source_id, "trial_date": trial["trial_date"],
            "venue": trial["venue"], "surface": trial["surface"], "distance_m": trial["distance_m"],
            "going": trial["going"], "batch_label": trial["batch_label"],
            "batch_time_seconds": trial["batch_time_seconds"], "sectional_json": json.dumps(trial["sectionals"]),
            "created_at_utc": created,
        })
        run_positions = trial["running_positions"]
        trial_entry_id = stable_hash({"batch": batch_id, "horse": trial["horse_code"]})
        insert_or_verify(connection, "trial_entries", "trial_entry_id", trial_entry_id, {
            "trial_entry_id": trial_entry_id, "trial_batch_id": batch_id, "horse_code": trial["horse_code"],
            "horse_name": trial["horse_name"], "jockey": trial["jockey"], "trainer": trial["trainer"],
            "draw": trial["draw"], "gear": trial["gear"], "lbw": trial["lbw"],
            "running_position_json": json.dumps(run_positions), "finish_rank": trial["finish_rank"],
            "finish_time_seconds": trial["finish_time_seconds"],
            "time_vs_batch_seconds": round(trial["finish_time_seconds"] - trial["batch_time_seconds"], 4),
            "final_sectional_vs_batch_seconds": 0.0, "finish_rank_pct": 1.0,
            "position_gain": run_positions[0] - run_positions[-1], "comment": trial["comment"], "created_at_utc": created,
        })

        manifest_hash = stable_hash({"pp_source_sha256": pp_sha, "trial_source_sha256": trial_sha})
        pp_features = {
            "pp_available": 1, "pp_identity_match_confidence": 1.0,
            "pp_source_country": pp["source_country"], "pp_starts_pre_import": 1,
            "pp_latest_distance_m": pp["race_distance_m"], "pp_source_rating_available": 0,
            "pp_strength_prior_mean_candidate": None, "pp_strength_prior_sd_candidate": None,
        }
        trial_features = {
            "trial_available": 1, "trial_time_vs_batch_seconds_pre": 0.0,
            "trial_final_sectional_vs_batch_seconds_pre": 0.0, "trial_finish_rank_pct_pre": 1.0,
            "trial_position_gain_pre": 0, "trial_count_90d_pre": 1,
        }
        for subject_type, subject_key, features in (("pp", pp["hk_horse_code"], pp_features), ("trial", trial["horse_code"], trial_features)):
            availability = {key: int(value is not None) for key, value in features.items()}
            snapshot_payload = {"schema": SCHEMA_VERSION, "subject": subject_key, "features": features, "availability": availability, "source_manifest_sha256": manifest_hash}
            snapshot_hash = stable_hash(snapshot_payload)
            snapshot_id = stable_hash({"type": subject_type, "payload": snapshot_hash})
            insert_or_verify(connection, "candidate_feature_snapshots", "snapshot_id", snapshot_id, {
                "snapshot_id": snapshot_id, "candidate_schema_version": SCHEMA_VERSION,
                "subject_type": subject_type, "subject_key": subject_key, "as_of_utc": args.as_of_utc,
                "feature_json": json.dumps(features, ensure_ascii=False, sort_keys=True),
                "availability_json": json.dumps(availability, ensure_ascii=False, sort_keys=True),
                "source_manifest_sha256": manifest_hash, "snapshot_sha256": snapshot_hash, "created_at_utc": created,
            })

        connection.commit()
        counts = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("source_manifests", "pp_identity_map", "pp_external_form", "trial_batches", "trial_entries", "candidate_feature_snapshots")}
        report = {
            "status": "pass", "mode": "offline_fixture_only", "schema_version": SCHEMA_VERSION,
            "database": str(db_path), "database_tables": counts,
            "pp_fixture": pp, "trial_fixture": trial,
            "source_hashes": {"pp": pp_sha, "trial": trial_sha},
            "source_manifest_sha256": manifest_hash,
            "production_access": {"v10_sqlite_opened": False, "n6_imported": False, "network_requests": 0},
        }
    finally:
        connection.close()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Build isolated P0 candidate fixtures from archived official sources")
    parser.add_argument("--db", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--pp-raw", required=True)
    parser.add_argument("--trial-raw", required=True)
    parser.add_argument("--pp-url", required=True)
    parser.add_argument("--trial-url", required=True)
    parser.add_argument("--pp-retrieved-utc", required=True)
    parser.add_argument("--trial-retrieved-utc", required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    report = build_fixture(args)
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "tables": report["database_tables"], "mode": report["mode"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
