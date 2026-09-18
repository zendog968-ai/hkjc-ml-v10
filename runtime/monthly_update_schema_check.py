#!/usr/bin/env python3
"""Read-only schema and latest-date inspection for the V10 monthly update preflight."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="hkjc_last_season.sqlite")
    args = parser.parse_args()
    db = Path(args.db).resolve()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        date_columns: dict[str, list[str]] = {}
        latest_values: dict[str, str | None] = {}
        for table in tables:
            cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table!r})")]
            candidates = [col for col in cols if col.lower() in {"race_date", "meeting_date", "date"}]
            if candidates:
                date_columns[table] = candidates
                for col in candidates:
                    quoted_table = '"' + table.replace('"', '""') + '"'
                    quoted_col = '"' + col.replace('"', '""') + '"'
                    latest_values[f"{table}.{col}"] = conn.execute(
                        f"SELECT MAX({quoted_col}) FROM {quoted_table}"
                    ).fetchone()[0]
        print(json.dumps({"database": str(db), "tables": tables, "date_columns": date_columns, "latest_values": latest_values}, ensure_ascii=False, indent=2))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
