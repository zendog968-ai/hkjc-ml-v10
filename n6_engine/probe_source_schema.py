#!/usr/bin/env python3
"""Inspect V10 SQLite metadata using an immutable read-only connection only."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

N6_ROOT = Path(__file__).resolve().parent
V10_DB = Path("/home/ubuntu/hkjc_v10_database/hkjc_last_season.sqlite")
OUTPUT = N6_ROOT / "reports" / "v10_source_schema.json"
TABLES = ("races", "starters", "elo_feature_store", "elo_current_state")


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def connect_read_only() -> sqlite3.Connection:
    if not V10_DB.is_file():
        raise FileNotFoundError(f"V10 source database is unavailable: {V10_DB}")
    connection = sqlite3.connect(f"file:{V10_DB}?mode=ro&immutable=1", uri=True)
    connection.execute("PRAGMA query_only = ON")
    return connection


def main() -> int:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "database": str(V10_DB),
        "access": "SQLite URI mode=ro&immutable=1; PRAGMA query_only=ON",
        "tables": {},
    }
    with connect_read_only() as connection:
        table_data: dict[str, object] = {}
        for table in TABLES:
            columns = [
                {"name": row[1], "type": row[2], "not_null": bool(row[3]), "primary_key": bool(row[5])}
                for row in connection.execute(f"PRAGMA table_info({quote_identifier(table)})")
            ]
            count = connection.execute(f"SELECT COUNT(*) FROM {quote_identifier(table)}").fetchone()[0]
            sample = connection.execute(f"SELECT * FROM {quote_identifier(table)} LIMIT 2").fetchall()
            names = [column["name"] for column in columns]
            table_data[table] = {
                "row_count": count,
                "columns": columns,
                "sample_rows": [dict(zip(names, row)) for row in sample],
            }
        payload["tables"] = table_data
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(OUTPUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
