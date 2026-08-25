#!/usr/bin/env python3
"""Read-only coverage and integrity report for auditable T-15/T-5 snapshot archives.

This tool never sends network requests, never opens the V10 production database,
and never changes V10 probabilities, EV or Kelly.  It evaluates only a separately
provided candidate snapshot SQLite archive created by import_prerace_odds_snapshots.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def iso_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def db_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Report candidate pre-race snapshot coverage.")
    parser.add_argument("--db", required=True, help="Candidate snapshot SQLite archive only")
    parser.add_argument("--output", required=True)
    parser.add_argument("--minimum-complete-races", type=int, default=150)
    args = parser.parse_args()
    archive = Path(args.db)
    output = Path(args.output)
    if args.minimum_complete_races < 1:
        raise SystemExit("minimum complete races must be positive")
    if not archive.is_file():
        result: dict[str, Any] = {
            "schema_version": "v1",
            "status": "not_initialized",
            "archive": str(archive),
            "network_requests": 0,
            "v10_database_opened": False,
            "n6_imported": False,
            "message": "No candidate archive exists yet; capture remains manual and approval-gated.",
        }
    else:
        uri = f"file:{archive.resolve()}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True)
        conn.execute("PRAGMA query_only=ON")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"pre_race_odds_snapshots", "pre_race_odds_runner_prices"}
        if not required.issubset(tables):
            raise SystemExit(f"archive lacks required tables: {sorted(required - tables)}")
        races = conn.execute(
            """SELECT race_date, racecourse, race_no,
                      SUM(snapshot_label='T_MINUS_15') AS t15_count,
                      SUM(snapshot_label='T_MINUS_5') AS t5_count
                 FROM pre_race_odds_snapshots
                 WHERE status='complete'
                 GROUP BY race_date, racecourse, race_no"""
        ).fetchall()
        complete: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for race_date, course, race_no, t15_count, t5_count in races:
            if t15_count != 1 or t5_count != 1:
                rejected.append({"race": f"{race_date}|{course}|{race_no}", "reason": "requires exactly one complete T-15 and one complete T-5 snapshot"})
                continue
            snapshots = conn.execute(
                """SELECT s.snapshot_label, s.captured_at_utc, s.payload_sha256,
                          COUNT(p.horse_name) AS runner_prices,
                          SUM(p.win_odds IS NOT NULL) AS win_prices
                     FROM pre_race_odds_snapshots AS s
                     JOIN pre_race_odds_runner_prices AS p USING(snapshot_id)
                    WHERE s.race_date=? AND s.racecourse=? AND s.race_no=?
                    GROUP BY s.snapshot_id, s.snapshot_label, s.captured_at_utc, s.payload_sha256
                    ORDER BY s.snapshot_label""",
                (race_date, course, race_no),
            ).fetchall()
            detail = {label: {"captured_at_utc": captured, "payload_sha256": sha, "runner_prices": runners, "win_prices": wins} for label, captured, sha, runners, wins in snapshots}
            if iso_utc(detail["T_MINUS_15"]["captured_at_utc"]) >= iso_utc(detail["T_MINUS_5"]["captured_at_utc"]):
                rejected.append({"race": f"{race_date}|{course}|{race_no}", "reason": "T-15 capture timestamp is not earlier than T-5"})
                continue
            if not detail["T_MINUS_15"]["win_prices"] or not detail["T_MINUS_5"]["win_prices"]:
                rejected.append({"race": f"{race_date}|{course}|{race_no}", "reason": "one or both snapshots lack win prices"})
                continue
            complete.append({"race_date": race_date, "racecourse": course, "race_no": race_no, "snapshots": detail})
        conn.close()
        result = {
            "schema_version": "v1",
            "status": "ready_for_accumulation",
            "archive": str(archive.resolve()),
            "archive_sha256": db_sha256(archive),
            "minimum_complete_races": args.minimum_complete_races,
            "complete_races": len(complete),
            "minimum_reached": len(complete) >= args.minimum_complete_races,
            "incomplete_or_rejected_races": len(rejected),
            "rejections": rejected,
            "complete_race_examples": complete[:3],
            "network_requests": 0,
            "v10_database_opened": False,
            "n6_imported": False,
            "automatic_capture_enabled": False,
        }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
