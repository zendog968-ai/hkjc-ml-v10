#!/usr/bin/env python3
"""Create a signed-by-content V10 training provenance manifest and source-quality summary.

This utility is intentionally read-only against the V10 SQLite database.  It is
invoked after a successful model update and writes only Git-ignored runtime
reports.  It never fetches web pages, loads N6, or changes prediction artifacts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
HKT = ZoneInfo("Asia/Hong_Kong")
REQUIRED_ARTIFACTS = (
    "hkjc_last_season.sqlite",
    "hkjc_last_season.csv",
    "horse_model.pkl",
    "lightgbm_training_report.json",
    "v101_quality_report.json",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_metadata(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    stat = path.stat()
    return {
        "path": path.name,
        "size_bytes": stat.st_size,
        "mtime_hkt": datetime.fromtimestamp(stat.st_mtime, tz=HKT).isoformat(),
        "sha256": sha256(path),
    }


def git_ref() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def read_db_summary(db_path: Path, window_days: int) -> dict[str, Any]:
    uri = f"file:{db_path}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA query_only=ON")
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    scalar = lambda sql: conn.execute(sql).fetchone()[0]
    summary: dict[str, Any] = {
        "access": "mode=ro&immutable=1; PRAGMA query_only=ON",
        "meetings": scalar("SELECT COUNT(*) FROM meetings"),
        "races": scalar("SELECT COUNT(*) FROM races"),
        "starters": scalar("SELECT COUNT(*) FROM starters"),
        "latest_race_date": scalar("SELECT MAX(race_date) FROM races"),
        "latest_feature_date": scalar("SELECT MAX(race_date) FROM elo_feature_store"),
        "feature_rows": scalar("SELECT COUNT(*) FROM elo_feature_store"),
    }
    if "crawl_log" in tables:
        cutoff = (datetime.now(HKT) - timedelta(days=window_days)).replace(tzinfo=None).isoformat(timespec="seconds")
        outcomes = [
            {"status_code": row[0], "outcome": row[1], "count": row[2]}
            for row in conn.execute(
                """SELECT status_code, outcome, COUNT(*)
                   FROM crawl_log WHERE fetched_at >= ?
                   GROUP BY status_code, outcome ORDER BY outcome, status_code""",
                (cutoff,),
            )
        ]
        blocked = sum(item["count"] for item in outcomes if item["status_code"] in (403, 429))
        summary["source_quality_window_days"] = window_days
        summary["crawl_outcomes"] = outcomes
        summary["blocked_or_rate_limited_count"] = blocked
        summary["stop_on_403_429_policy"] = True
    conn.close()
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Create V10 training provenance manifest.")
    parser.add_argument("--output-dir", default="runtime/training_manifests")
    parser.add_argument("--quality-window-days", type=int, default=14)
    args = parser.parse_args()
    if args.quality_window_days < 1:
        raise SystemExit("quality window must be at least one day")

    now = datetime.now(HKT)
    artifacts = {name: artifact_metadata(ROOT / name) for name in REQUIRED_ARTIFACTS}
    training = json.loads((ROOT / "lightgbm_training_report.json").read_text(encoding="utf-8"))
    quality = json.loads((ROOT / "v101_quality_report.json").read_text(encoding="utf-8"))
    database = read_db_summary(ROOT / "hkjc_last_season.sqlite", args.quality_window_days)
    manifest: dict[str, Any] = {
        "schema_version": "v1",
        "created_at_hkt": now.isoformat(),
        "purpose": "training provenance and data-quality audit only",
        "production_contract": {
            "v10_probability_ev_kelly_modified": False,
            "n6_imported": False,
            "network_requests": 0,
        },
        "git_commit": git_ref(),
        "artifacts": artifacts,
        "database": database,
        "training": {
            "model": training.get("model"),
            "feature_version": training.get("feature_version"),
            "split": training.get("split"),
            "test_row_metrics": training.get("test_row_metrics"),
            "test_race_metrics": training.get("test_race_metrics"),
        },
        "v101_quality": quality,
    }
    canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    manifest["manifest_sha256"] = hashlib.sha256(canonical).hexdigest()

    output_dir = ROOT / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = now.strftime("%Y%m%dT%H%M%S%z")
    dated_path = output_dir / f"training_manifest_{timestamp}.json"
    latest_path = output_dir / "latest.json"
    payload = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    dated_path.write_text(payload, encoding="utf-8")
    temp = latest_path.with_suffix(".tmp")
    temp.write_text(payload, encoding="utf-8")
    os.replace(temp, latest_path)
    print(json.dumps({"manifest": str(dated_path), "latest": str(latest_path), "sha256": manifest["manifest_sha256"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
