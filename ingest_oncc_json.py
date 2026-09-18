#!/usr/bin/env python3
"""Ingest ONCC qualitative pre-race JSON into an isolated research SQLite database.

This module deliberately has no V10/N6 imports and never opens the formal HKJC
SQLite database. It creates or updates only the explicitly supplied extension DB.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import Any, Iterable

TABLE = "oncc_qualitative_features"
FORMAL_DB_NAMES = {
    "hkjc_last_season.sqlite",
    "hkjc_last_season.db",
    "horse_model.pkl",
}

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    race_date TEXT NOT NULL CHECK (race_date GLOB '????-??-??'),
    race_no INTEGER NOT NULL CHECK (race_no > 0),
    horse_no INTEGER NOT NULL CHECK (horse_no > 0),
    horse_name TEXT NOT NULL,
    trainer TEXT,
    jockey TEXT,
    trackwork_comment TEXT,
    condition_score REAL,
    expert_recommend_count INTEGER CHECK (expert_recommend_count IS NULL OR expert_recommend_count >= 0),
    trial_observation TEXT,
    effort_level TEXT,
    surge_rating TEXT,
    stable_report TEXT,
    ambition_flag INTEGER CHECK (ambition_flag IS NULL OR ambition_flag IN (0, 1)),
    PRIMARY KEY (race_date, race_no, horse_no)
);
"""

INSERT = f"""
INSERT OR REPLACE INTO {TABLE} (
    race_date, race_no, horse_no, horse_name, trainer, jockey,
    trackwork_comment, condition_score, expert_recommend_count,
    trial_observation, effort_level, surge_rating,
    stable_report, ambition_flag
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _as_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        if "records" in payload:
            payload = payload["records"]
        elif "horses" in payload:
            payload = payload["horses"]
        else:
            payload = [payload]
    if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
        raise ValueError("JSON must be one record object, a list of records, or an object with records/horses")
    return payload


def _required_int(row: dict[str, Any], key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{key} must be a positive integer")
    return value


def _optional_float(value: Any, key: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be numeric or null")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{key} must be a finite real number or null")
    return value


def _optional_nonnegative_int(value: Any, key: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{key} must be a non-negative integer or null")
    return value


def _optional_bool(value: Any, key: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be boolean or null")
    return int(value)


def _optional_text(value: Any, key: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text or null")
    return value


def normalize(row: dict[str, Any]) -> tuple[Any, ...]:
    race_date = row.get("race_date")
    if not isinstance(race_date, str):
        raise ValueError("race_date must be an ISO date string")
    try:
        date.fromisoformat(race_date)
    except ValueError as exc:
        raise ValueError("race_date must use YYYY-MM-DD") from exc

    horse_name = row.get("horse_name")
    if not isinstance(horse_name, str) or not horse_name.strip():
        raise ValueError("horse_name must be non-empty text")

    morning = row.get("morning_trackwork") or {}
    expert = row.get("expert_tips") or {}
    trial = row.get("barrier_trial") or {}
    stable = row.get("stable_intel") or {}
    for name, value in (("morning_trackwork", morning), ("expert_tips", expert), ("barrier_trial", trial), ("stable_intel", stable)):
        if not isinstance(value, dict):
            raise ValueError(f"{name} must be an object")

    return (
        race_date,
        _required_int(row, "race_no"),
        _required_int(row, "horse_no"),
        horse_name.strip(),
        _optional_text(row.get("trainer"), "trainer"),
        _optional_text(row.get("jockey"), "jockey"),
        _optional_text(morning.get("comment"), "morning_trackwork.comment"),
        _optional_float(morning.get("condition_score"), "morning_trackwork.condition_score"),
        _optional_nonnegative_int(expert.get("recommend_count"), "expert_tips.recommend_count"),
        _optional_text(trial.get("observation"), "barrier_trial.observation"),
        _optional_text(trial.get("effort_level"), "barrier_trial.effort_level"),
        _optional_text(trial.get("surge_rating"), "barrier_trial.surge_rating"),
        _optional_text(stable.get("report"), "stable_intel.report"),
        _optional_bool(stable.get("ambition_flag"), "stable_intel.ambition_flag"),
    )


def guard_database_path(db_path: Path) -> None:
    if db_path.name in FORMAL_DB_NAMES:
        raise ValueError(f"refusing formal database/model path: {db_path}")
    if "hkjc_last_season" in db_path.name.lower():
        raise ValueError(f"refusing path containing formal V10 database name: {db_path}")
    if db_path.suffix.lower() not in {".sqlite", ".db"}:
        raise ValueError("--db must end in .sqlite or .db")


def initialize_database(db_path: Path) -> None:
    guard_database_path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as con:
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute(SCHEMA)
        con.commit()


def ingest(input_path: Path, db_path: Path) -> int:
    guard_database_path(db_path)
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    rows = [normalize(row) for row in _as_records(payload)]
    initialize_database(db_path)
    with sqlite3.connect(db_path) as con:
        con.executemany(INSERT, rows)
        con.commit()
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="ONCC JSON file; required unless --init-only is used")
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--init-only", action="store_true", help="Create the isolated table without ingesting records")
    args = parser.parse_args()
    try:
        if args.init_only:
            initialize_database(args.db)
            count = 0
        else:
            if args.input is None:
                parser.error("--input is required unless --init-only is used")
            count = ingest(args.input, args.db)
    except (OSError, json.JSONDecodeError, sqlite3.Error, ValueError) as exc:
        print(f"ingest_failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"status": "ok", "rows_ingested": count, "table": TABLE, "n6_status": "disabled_non_hk", "formal_v10_changed": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

__all__ = ["TABLE", "SCHEMA", "normalize", "ingest", "guard_database_path"]

def _unused_os_marker() -> None:
    # Kept intentionally empty; no environment variables are read by this importer.
    os.fspath(Path("."))
