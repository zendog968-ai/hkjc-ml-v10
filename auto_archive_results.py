#!/usr/bin/env python3
"""Archive official local and overseas HKJC results regardless of prediction history.

A failed or unavailable official page is reported as a gap.  It is never replaced
with inferred standings, final prices from another source, or a silent success.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path

from overseas_hkjc_core import (
    OfficialOverseasClient,
    archive_meeting,
    discover_fixture_season,
    init_overseas_db,
)

ROOT = Path(__file__).resolve().parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="無條件歸檔 HKJC 本地及海外轉播賽官方賽果。")
    parser.add_argument("--date", default=date.today().isoformat(), help="待歸檔賽日 YYYY-MM-DD")
    parser.add_argument("--db", default="hkjc_last_season.sqlite")
    parser.add_argument("--schema", default="schema_overseas_racing.sql")
    parser.add_argument("--archive-dir", default="archive/result_archive_runs")
    parser.add_argument("--raw-dir", default="archive/overseas_hkjc_raw")
    parser.add_argument("--seasons", default="2223,2324,2425,2526,2627", help="海外 fixture 賽季代碼。")
    parser.add_argument("--skip-local", action="store_true")
    parser.add_argument("--skip-overseas", action="store_true")
    parser.add_argument("--local-force", action="store_true", help="要求本地 ETL 重抓該日既有賽果。")
    parser.add_argument("--no-audit", action="store_true", help="只歸檔，不自動呼叫賽後覆盤引擎。")
    parser.add_argument("--telegram", action="store_true", help="覆盤有賽前預測時，僅在主機已有 Telegram 環境變數才嘗試通知。")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        target = date.fromisoformat(args.date).isoformat()
    except ValueError as exc:
        raise SystemExit("--date 必須為 YYYY-MM-DD") from exc
    archive_dir = Path(args.archive_dir) / target
    archive_dir.mkdir(parents=True, exist_ok=True)
    result: dict = {"schema_version": "v10.2_auto_archive_v1", "target_date": target, "local": {"status": "skipped"}, "overseas": {"status": "skipped", "meetings": []}}
    if not args.skip_local:
        cmd = [sys.executable, str(ROOT / "hkjc_last_season_etl.py"), "--db", args.db, "--start-date", target, "--end-date", target, "--csv", str(archive_dir / "local_results_export.csv"), "--delay-min", "2.0", "--delay-max", "3.5"]
        if args.local_force:
            cmd.append("--force")
        completed = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
        result["local"] = {"status": "ok" if completed.returncode == 0 else "partial_or_error", "returncode": completed.returncode, "stdout": completed.stdout[-4000:], "stderr": completed.stderr[-4000:]}
    if not args.skip_overseas:
        conn = init_overseas_db(Path(args.db), Path(args.schema))
        client = OfficialOverseasClient(conn, Path(args.raw_dir))
        discovered = []
        for season in [item.strip() for item in args.seasons.split(",") if item.strip()]:
            discovered.extend(discover_fixture_season(conn, client, season, target, target))
        outcomes = []
        for meeting in {(item.meeting_date, item.simulcast_code): item for item in discovered}.values():
            try:
                outcomes.append(archive_meeting(conn, client, meeting))
            except RuntimeError as exc:
                outcomes.append({"meeting": f"{meeting.meeting_date} {meeting.simulcast_code}", "status": "error", "detail": str(exc), "races": 0})
                if "已停止" in str(exc):
                    break
        result["overseas"] = {"status": "ok" if outcomes and all(item.get("status") == "ok" for item in outcomes) else ("no_official_simulcast_found" if not outcomes else "partial_or_error"), "meetings": outcomes}
        conn.close()
    # Run post-race audit independently of conversational prediction requests.
    # Only completed official races are considered.  The audit engine itself
    # emits archived_only without sending a report when no pre-race batch exists.
    audit_runs = []
    if not args.no_audit:
        audit_targets = []
        conn = sqlite3.connect(args.db)
        conn.row_factory = sqlite3.Row
        if not args.skip_local:
            for row in conn.execute("SELECT race_date,racecourse,race_no FROM races WHERE race_date=? AND race_status='completed' ORDER BY racecourse,race_no", (target,)):
                audit_targets.append(("local", f"{row['race_date']}:{row['racecourse']}:{row['race_no']}"))
        if not args.skip_overseas:
            for row in conn.execute("SELECT m.meeting_date,m.simulcast_code,r.race_no FROM overseas_races r JOIN overseas_meetings m ON m.meeting_id=r.meeting_id WHERE m.meeting_date=? AND r.race_status='completed' ORDER BY m.simulcast_code,r.race_no", (target,)):
                audit_targets.append(("overseas", f"{row['meeting_date']}:{row['simulcast_code']}:{row['race_no']}"))
        conn.close()
        for scope, race_key in audit_targets:
            cmd = [sys.executable, str(ROOT / "post_race_audit.py"), "--scope", scope, "--race-key", race_key, "--db", args.db, "--schema", args.schema, "--report-dir", str(archive_dir / "audits")]
            if args.telegram:
                cmd.append("--telegram")
            proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
            audit_runs.append({"scope": scope, "race_key": race_key, "returncode": proc.returncode, "stdout": proc.stdout[-2000:], "stderr": proc.stderr[-2000:]})
    result["audits"] = audit_runs
    output = archive_dir / "auto_archive_results_summary.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary": str(output), "local": result["local"]["status"], "overseas": result["overseas"]["status"]}, ensure_ascii=False, indent=2))
    return 0 if result["local"]["status"] in {"ok", "skipped"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
