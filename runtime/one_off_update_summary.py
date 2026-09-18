#!/usr/bin/env python3
"""Compare two V10 update audit snapshots and summarize official-access outcomes."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--before", required=True)
    parser.add_argument("--after", required=True)
    parser.add_argument("--db", default="hkjc_last_season.sqlite")
    parser.add_argument("--log", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    before = read_json(Path(args.before))
    after = read_json(Path(args.after))
    db_before = before["database"]
    db_after = after["database"]
    conn = sqlite3.connect(args.db)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    equipment_status = {}
    if "equipment_profile_log" in tables:
        equipment_status = dict(conn.execute("SELECT status, COUNT(*) FROM equipment_profile_log GROUP BY status"))
    outcomes = [
        {"status_code": row[0], "outcome": row[1], "count": row[2]}
        for row in conn.execute(
            "SELECT status_code, outcome, COUNT(*) FROM crawl_log GROUP BY status_code, outcome ORDER BY outcome, status_code"
        )
    ]
    conn.close()
    log_text = Path(args.log).read_text(encoding="utf-8", errors="replace")
    result = {
        "race_days_added": db_after["meetings"] - db_before["meetings"],
        "races_added": db_after["races"] - db_before["races"],
        "starters_added": db_after["starters"] - db_before["starters"],
        "feature_rows_delta": db_after.get("elo_feature_rows", 0) - db_before.get("elo_feature_rows", 0),
        "latest_race_date_before": db_before["latest_race_date"],
        "latest_race_date_after": db_after["latest_race_date"],
        "csv_rows_delta": (after.get("csv_data_rows") or 0) - (before.get("csv_data_rows") or 0),
        "artifacts_changed": {
            name: before["artifacts"].get(name, {}).get("sha256") != after["artifacts"].get(name, {}).get("sha256")
            for name in after["artifacts"]
        },
        "equipment_profile_status_counts": equipment_status,
        "official_403_or_429_observed": ("HTTP 403" in log_text or "HTTP 429" in log_text or "blocked_or_rate_limited" in log_text),
        "log_contains_stop_error": ("工作停止" in log_text or "Traceback" in log_text),
        "crawl_outcomes": outcomes,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
