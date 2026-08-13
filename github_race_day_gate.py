#!/usr/bin/env python3
"""GitHub Actions gate for an approximately one-hour-before-race scan.

GitHub scheduled workflows have best-effort timing and a minimum five-minute cadence.
This gate reads a committed race-day schedule and permits one job during the configured
pre-race window. It deliberately has no persistent state: a run should be scheduled only
once per target race, or a workflow-dispatch should be used for a manual rerun.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HK_TZ = ZoneInfo("Asia/Hong_Kong")


def load_schedule(path: Path) -> tuple[dict, list[tuple[int, datetime]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("timezone", "Asia/Hong_Kong") != "Asia/Hong_Kong":
        raise ValueError("只支援 Asia/Hong_Kong。")
    meeting = payload.get("meeting") or {}
    date = str(meeting.get("race_date") or "").replace("/", "-")
    course = str(meeting.get("racecourse") or "").upper()
    if course not in {"ST", "HV"}:
        raise ValueError("meeting.racecourse 必須為 ST 或 HV。")
    times = meeting.get("race_start_times") or {}
    jobs = []
    for race_text, time_text in times.items():
        start = datetime.strptime(f"{date} {time_text}", "%Y-%m-%d %H:%M").replace(tzinfo=HK_TZ)
        jobs.append((int(race_text), start))
    if not jobs:
        raise ValueError("meeting.race_start_times 不可為空。")
    return payload, sorted(jobs)


def select_due_job(payload: dict, jobs: list[tuple[int, datetime]], now: datetime) -> dict:
    before = int(payload.get("trigger_minutes_before", 60))
    window = int(payload.get("trigger_window_minutes", 10))
    meeting = payload["meeting"]
    for race_no, start in jobs:
        target = start - timedelta(minutes=before)
        if target <= now < target + timedelta(minutes=window):
            return {
                "should_run": "true",
                "race_date": str(meeting["race_date"]),
                "racecourse": str(meeting["racecourse"]).upper(),
                "race_no": str(race_no),
                "scheduled_start_hk": start.isoformat(),
                "target_hk": target.isoformat(),
            }
    return {"should_run": "false", "race_date": "", "racecourse": "", "race_no": "", "scheduled_start_hk": "", "target_hk": ""}


def main() -> int:
    parser = argparse.ArgumentParser(description="GitHub Actions 賽日前一小時閘門")
    parser.add_argument("--config", required=True)
    parser.add_argument("--now", help="ISO-8601，供本機測試；預設為當前香港時間")
    parser.add_argument("--github-output", help="GitHub Actions GITHUB_OUTPUT 檔案路徑")
    args = parser.parse_args()
    payload, jobs = load_schedule(Path(args.config))
    now = datetime.fromisoformat(args.now).astimezone(HK_TZ) if args.now else datetime.now(HK_TZ)
    result = select_due_job(payload, jobs, now)
    print(json.dumps({"checked_at_hk": now.isoformat(), **result}, ensure_ascii=False))
    output_path = args.github_output or os.getenv("GITHUB_OUTPUT")
    if output_path:
        with Path(output_path).open("a", encoding="utf-8") as handle:
            for key, value in result.items():
                handle.write(f"{key}={value}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
