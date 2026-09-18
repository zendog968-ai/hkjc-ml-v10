#!/usr/bin/env python3
"""Snapshot selected V10 update state without modifying model or database artifacts."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = [
    "hkjc_last_season.sqlite",
    "hkjc_last_season.csv",
    "horse_model.pkl",
    "lightgbm_training_report.json",
    "v101_quality_report.json",
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"exists": False}
    stat = path.stat()
    return {
        "exists": True,
        "size_bytes": stat.st_size,
        "mtime_utc": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(),
        "sha256": sha256(path),
    }


def db_summary(db_path: Path) -> dict[str, object]:
    conn = sqlite3.connect(db_path)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    query = lambda sql: conn.execute(sql).fetchone()[0]
    result: dict[str, object] = {
        "races": query("SELECT COUNT(*) FROM races"),
        "starters": query("SELECT COUNT(*) FROM starters"),
        "meetings": query("SELECT COUNT(*) FROM meetings"),
        "latest_race_date": query("SELECT MAX(race_date) FROM races"),
        "crawl_log_rows": query("SELECT COUNT(*) FROM crawl_log") if "crawl_log" in tables else None,
    }
    if "elo_feature_store" in tables:
        result["elo_feature_rows"] = query("SELECT COUNT(*) FROM elo_feature_store")
        result["elo_feature_latest_date"] = query("SELECT MAX(race_date) FROM elo_feature_store")
    conn.close()
    return result


def csv_row_count(path: Path) -> int | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return max(sum(1 for _ in csv.reader(handle)) - 1, 0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    db_path = ROOT / "hkjc_last_season.sqlite"
    csv_path = ROOT / "hkjc_last_season.csv"
    result = {
        "label": args.label,
        "captured_at": datetime.now().astimezone().isoformat(),
        "database": db_summary(db_path),
        "csv_data_rows": csv_row_count(csv_path),
        "artifacts": {name: artifact(ROOT / name) for name in ARTIFACTS},
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
