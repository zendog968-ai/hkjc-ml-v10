#!/usr/bin/env python3
"""Backfill HKJC overseas simulcast races with verifiable coverage reporting.

Only public HKJC fixture, race-summary and results pages are read.  A 200 response
without parseable official result rows is recorded as partial; it is never treated
as a completed race.  Use --resume safely: all database writes are idempotent.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
from datetime import date
from pathlib import Path

from overseas_hkjc_core import (
    OfficialOverseasClient,
    archive_meeting,
    db_counts,
    discover_fixture_season,
    init_overseas_db,
    select_meetings,
)

ROOT = Path(__file__).resolve().parent
DEFAULT_SEASONS = ("2223", "2324", "2425", "2526", "2627")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="以 HKJC 官方來源回刷海外 S1/S2 賽事，並產生可稽核覆蓋報告。")
    parser.add_argument("--db", default="hkjc_last_season.sqlite", help="SQLite 資料庫路徑；海外表會安全加入此庫。")
    parser.add_argument("--schema", default="schema_overseas_racing.sql", help="海外資料庫 schema SQL。")
    parser.add_argument("--raw-dir", default="archive/overseas_hkjc_raw", help="官方來源 HTML 歸檔目錄。")
    parser.add_argument("--report-dir", default="overseas_backfill_reports", help="覆蓋與缺口報告輸出目錄。")
    parser.add_argument("--start-date", default="2023-01-01", help="開始日期 YYYY-MM-DD。")
    parser.add_argument("--end-date", default=date.today().isoformat(), help="結束日期 YYYY-MM-DD。")
    parser.add_argument("--seasons", default=",".join(DEFAULT_SEASONS), help="HKJC fixture 賽季代碼，以逗號分隔。")
    parser.add_argument("--discovery-only", action="store_true", help="只建立官方 fixture 發現清單與覆蓋稽核，不抓單場結果。")
    parser.add_argument("--resume", action="store_true", help="只處理未完成、部分或官方來源暫不可用的已發現群組。")
    parser.add_argument("--max-meetings", type=int, help="測試或分批執行上限；正式全量回刷請省略。")
    parser.add_argument("--delay-min", type=float, default=3.0, help="官方頁請求最短隨機間隔秒數。")
    parser.add_argument("--delay-max", type=float, default=6.0, help="官方頁請求最長隨機間隔秒數。")
    parser.add_argument("--cooldown-every", type=int, default=20, help="每 N 個官方頁請求後冷卻。")
    parser.add_argument("--cooldown-seconds", type=float, default=60.0, help="冷卻秒數。")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    return parser


def write_reports(conn, report_dir: Path, start: str, end: str, discovered_this_run: int, attempts: list[dict]) -> dict:
    report_dir.mkdir(parents=True, exist_ok=True)
    meeting_rows = conn.execute(
        """SELECT m.meeting_date,m.simulcast_code,m.meeting_name,m.location,m.discovery_status,
                  COUNT(r.overseas_race_id) AS races_discovered,
                  SUM(CASE WHEN r.race_status='completed' THEN 1 ELSE 0 END) AS races_completed,
                  SUM(CASE WHEN r.race_status='partial' THEN 1 ELSE 0 END) AS races_partial,
                  SUM(CASE WHEN r.race_status='source_unavailable' THEN 1 ELSE 0 END) AS races_unavailable
           FROM overseas_meetings m LEFT JOIN overseas_races r ON r.meeting_id=m.meeting_id
           WHERE m.meeting_date BETWEEN ? AND ?
           GROUP BY m.meeting_id ORDER BY m.meeting_date,m.simulcast_code""",
        (start, end),
    ).fetchall()
    columns = ["meeting_date", "simulcast_code", "meeting_name", "location", "discovery_status", "races_discovered", "races_completed", "races_partial", "races_unavailable"]
    csv_path = report_dir / "overseas_meeting_coverage.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in meeting_rows:
            writer.writerow({column: row[column] for column in columns})
    audit_rows = conn.execute(
        """SELECT season_code,requested_start_date,requested_end_date,fixture_url,discovered_meetings,status,checked_at_utc,detail
           FROM overseas_discovery_audit WHERE requested_start_date=? AND requested_end_date=? ORDER BY audit_id""",
        (start, end),
    ).fetchall()
    total_races = sum(int(row["races_discovered"] or 0) for row in meeting_rows)
    complete_races = sum(int(row["races_completed"] or 0) for row in meeting_rows)
    partial_races = sum(int(row["races_partial"] or 0) for row in meeting_rows)
    unavailable_discovery = [dict(row) for row in audit_rows if row["status"] != "complete"]
    summary = {
        "schema_version": "v10.2_overseas_backfill_coverage_v1",
        "date_range": {"start": start, "end": end},
        "database_counts": db_counts(conn),
        "meetings_in_scope": len(meeting_rows),
        "races_discovered": total_races,
        "races_completed": complete_races,
        "races_partial": partial_races,
        "completion_rate": (complete_races / total_races) if total_races else None,
        "fixture_discovery_issues": unavailable_discovery,
        "attempts_this_run": attempts,
        "discovered_this_run": discovered_this_run,
        "strict_status": "complete" if total_races and complete_races == total_races and not unavailable_discovery else "incomplete_or_unverifiable",
        "warning": "strict_status=complete only means every discovered race has parseable official rows. It does not establish fixture coverage for an official season page that returned empty or unavailable.",
    }
    (report_dir / "overseas_backfill_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (report_dir / "overseas_backfill_attempts.json").write_text(json.dumps(attempts, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)s %(message)s")
    try:
        start = date.fromisoformat(args.start_date).isoformat()
        end = date.fromisoformat(args.end_date).isoformat()
    except ValueError as exc:
        raise SystemExit("--start-date 與 --end-date 必須為 YYYY-MM-DD") from exc
    if start > end:
        raise SystemExit("開始日期不可晚於結束日期。")
    conn = init_overseas_db(Path(args.db), Path(args.schema))
    client = OfficialOverseasClient(
        conn, Path(args.raw_dir), args.delay_min, args.delay_max, args.cooldown_every, args.cooldown_seconds
    )
    seasons = [item.strip() for item in args.seasons.split(",") if item.strip()]
    discovered: list = []
    if not args.resume:
        for season in seasons:
            meetings = discover_fixture_season(conn, client, season, start, end)
            logging.info("賽季 %s：發現 %s 個官方海外轉播群組。", season, len(meetings))
            discovered.extend(meetings)
    targets = select_meetings(conn, start, end)
    if args.max_meetings is not None:
        targets = targets[: max(args.max_meetings, 0)]
    attempts: list[dict] = []
    if not args.discovery_only:
        for index, meeting in enumerate(targets, start=1):
            try:
                outcome = archive_meeting(conn, client, meeting)
            except RuntimeError as exc:
                outcome = {"meeting": f"{meeting.meeting_date} {meeting.simulcast_code}", "status": "error", "detail": str(exc), "races": 0}
                logging.error("%s", exc)
                if "已停止" in str(exc):
                    attempts.append(outcome)
                    break
            attempts.append(outcome)
            logging.info("[%s/%s] %s", index, len(targets), outcome)
    summary = write_reports(conn, Path(args.report_dir), start, end, len(discovered), attempts)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
