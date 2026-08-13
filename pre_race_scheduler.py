#!/usr/bin/env python3
"""Run the V10.1 pre-race workflow once, 15 minutes before each configured race.

Install this as a *per-minute* cron job on a persistent Linux host. The script uses a
Hong Kong time-zone schedule file because the public race-card parser does not expose a
stable machine-readable post-time field. All output is per-race and the state file makes
repeated cron invocations idempotent.

The workflow reads public data only and does not send WhatsApp messages or place bets.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

HK_TZ = ZoneInfo("Asia/Hong_Kong")
DEFAULT_TRIGGER_MINUTES = 15


@dataclass(frozen=True)
class RaceJob:
    date: str
    racecourse: str
    race_no: int
    start_at: datetime

    @property
    def key(self) -> str:
        return f"{self.date}_{self.racecourse}_R{self.race_no:02d}"


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temp_name = handle.name
    os.replace(temp_name, path)


def load_json(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else fallback
    except (OSError, json.JSONDecodeError):
        return fallback


def normalize_date(value: str) -> str:
    parsed = datetime.strptime(str(value).replace("/", "-"), "%Y-%m-%d")
    return parsed.strftime("%Y/%m/%d")


def parse_start(date: str, time_text: str) -> datetime:
    clean = str(time_text).strip()
    parsed = datetime.strptime(f"{date.replace('/', '-')} {clean}", "%Y-%m-%d %H:%M")
    return parsed.replace(tzinfo=HK_TZ)


def load_jobs(config_path: Path) -> tuple[int, list[RaceJob]]:
    payload = load_json(config_path, {})
    if payload.get("timezone", "Asia/Hong_Kong") != "Asia/Hong_Kong":
        raise ValueError("目前只支援 Asia/Hong_Kong 賽事排程。")
    meeting = payload.get("meeting")
    if not isinstance(meeting, dict):
        raise ValueError("排程設定缺少 meeting 物件。")
    date = normalize_date(meeting.get("race_date", ""))
    racecourse = str(meeting.get("racecourse", "")).upper()
    if racecourse not in {"ST", "HV"}:
        raise ValueError("meeting.racecourse 必須為 ST 或 HV。")
    times = meeting.get("race_start_times")
    if not isinstance(times, dict) or not times:
        raise ValueError("meeting.race_start_times 必須為 {場次: 'HH:MM'}。")
    trigger_minutes = int(payload.get("trigger_minutes_before", DEFAULT_TRIGGER_MINUTES))
    if trigger_minutes < 1 or trigger_minutes > 120:
        raise ValueError("trigger_minutes_before 必須介乎 1 至 120。")
    jobs: list[RaceJob] = []
    for race_no_text, start_time in times.items():
        race_no = int(race_no_text)
        if race_no < 1 or race_no > 20:
            raise ValueError(f"無效場次：{race_no}")
        jobs.append(RaceJob(date, racecourse, race_no, parse_start(date, str(start_time))))
    return trigger_minutes, sorted(jobs, key=lambda row: row.race_no)


def parse_now(value: str | None) -> datetime:
    if not value:
        return datetime.now(HK_TZ)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=HK_TZ)
    return parsed.astimezone(HK_TZ)


def due_jobs(jobs: list[RaceJob], trigger_minutes: int, now: datetime) -> list[RaceJob]:
    """Return jobs whose one-minute Cron trigger bucket is active.

    Cron is expected to run at a minute boundary. A one-minute half-open bucket avoids
    duplicate execution while preserving exactly a 15-minute pre-race target.
    """
    now_minute = now.replace(second=0, microsecond=0)
    due: list[RaceJob] = []
    for job in jobs:
        target = (job.start_at - timedelta(minutes=trigger_minutes)).replace(second=0, microsecond=0)
        if target <= now_minute < target + timedelta(minutes=1):
            due.append(job)
    return due


def command_log_path(output_dir: Path, step: str) -> Path:
    return output_dir / f"{step}.log"


def run_command(command: list[str], output_dir: Path, step: str, timeout: int = 120) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        command,
        cwd=output_dir.parent.parent,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    log = (
        "$ " + " ".join(command) + "\n\n"
        "--- stdout ---\n" + (completed.stdout or "") + "\n"
        "--- stderr ---\n" + (completed.stderr or "") + "\n"
    )
    command_log_path(output_dir, step).write_text(log, encoding="utf-8")
    return {"step": step, "returncode": completed.returncode, "log": str(command_log_path(output_dir)), "command": command}


def execute_job(job: RaceJob, project_dir: Path, output_root: Path, odds_min_interval: int) -> dict[str, Any]:
    """Run the four deterministic pre-race steps, stopping after a hard failure."""
    scripts = {
        "racecard": project_dir / "fetch_hkjc_racecard.py",
        "odds": project_dir / "fetch_hkjc_live_odds.py",
        "predict": project_dir / "predict.py",
        "filter": project_dir / "filter_high_probability.py",
    }
    missing = [str(path.name) for path in scripts.values() if not path.exists()]
    if missing:
        raise FileNotFoundError("缺少流程腳本：" + ", ".join(missing))
    for required in (project_dir / "hkjc_last_season.sqlite", project_dir / "horse_model.pkl"):
        if not required.exists():
            raise FileNotFoundError(f"缺少模型資料：{required.name}")

    output_dir = output_root / job.key
    output_dir.mkdir(parents=True, exist_ok=True)
    race_card = output_dir / "race_card.json"
    win_overlay = output_dir / "odds_overlay.json"
    place_overlay = output_dir / "place_odds_overlay.json"
    combined_overlay = output_dir / "odds_overlay_combined.json"
    odds_meta = output_dir / "odds_overlay.meta.json"
    prediction = output_dir / "prediction.json"
    prediction_csv = output_dir / "prediction.csv"
    filter_output = output_dir / "high_probability_filter.json"
    filter_markdown = output_dir / "pre_race_report.md"
    python = sys.executable
    date_arg = job.date
    outcomes: list[dict[str, Any]] = []

    steps = [
        ("racecard", [python, str(scripts["racecard"]), "--date", date_arg, "--racecourse", job.racecourse,
                      "--race-no", str(job.race_no), "--output", str(race_card)], 90),
        ("odds", [python, str(scripts["odds"]), "--race-card", str(race_card), "--output", str(win_overlay),
                  "--place-output", str(place_overlay), "--combined-output", str(combined_overlay),
                  "--metadata-output", str(odds_meta), "--min-interval", str(odds_min_interval),
                  "--state-file", str(output_root / "live_odds_rate_limit_state.json")], 90),
        ("predict", [python, str(scripts["predict"]), "--db", str(project_dir / "hkjc_last_season.sqlite"),
                     "--model", str(project_dir / "horse_model.pkl"), "--race-card", str(race_card),
                     "--win-odds-overlay", str(win_overlay), "--place-odds-overlay", str(place_overlay),
                     "--output-json", str(prediction), "--output-csv", str(prediction_csv)], 180),
        ("filter", [python, str(scripts["filter"]), "--prediction", str(prediction), "--output", str(filter_output),
                    "--markdown-output", str(filter_markdown)], 60),
    ]
    for name, command, timeout in steps:
        outcome = run_command(command, output_dir, name, timeout)
        outcomes.append(outcome)
        if outcome["returncode"] != 0:
            return {"status": "failed", "failed_step": name, "output_dir": str(output_dir), "steps": outcomes}
    filter_payload = load_json(filter_output, {})
    return {
        "status": "completed",
        "output_dir": str(output_dir),
        "steps": outcomes,
        "selection_count": filter_payload.get("selection_count", 0),
        "strategy_selection_counts": filter_payload.get("selection_counts", {}),
        "markdown_report": str(filter_markdown),
        "whatsapp_direct_link": (filter_payload.get("whatsapp") or {}).get("direct_link"),
        "odds_status": (load_json(odds_meta, {}).get("status") or "unknown"),
    }


def process(
    config_path: Path,
    project_dir: Path,
    output_root: Path,
    state_path: Path,
    now: datetime,
    dry_run: bool,
    odds_min_interval: int,
) -> dict[str, Any]:
    trigger_minutes, jobs = load_jobs(config_path)
    candidates = due_jobs(jobs, trigger_minutes, now)
    state = load_json(state_path, {"runs": {}})
    runs = state.setdefault("runs", {})
    result: dict[str, Any] = {
        "checked_at": now.isoformat(),
        "trigger_minutes_before": trigger_minutes,
        "configured_jobs": len(jobs),
        "due_jobs": [job.key for job in candidates],
        "processed": [],
        "dry_run": dry_run,
    }
    for job in candidates:
        prior = runs.get(job.key, {})
        if prior.get("status") == "completed":
            result["processed"].append({"job": job.key, "status": "already_completed"})
            continue
        planned = {
            "job": job.key,
            "race_date": job.date,
            "racecourse": job.racecourse,
            "race_no": job.race_no,
            "scheduled_start": job.start_at.isoformat(),
            "target_trigger": (job.start_at - timedelta(minutes=trigger_minutes)).isoformat(),
        }
        if dry_run:
            result["processed"].append({**planned, "status": "dry_run_due"})
            continue
        try:
            outcome = execute_job(job, project_dir, output_root, odds_min_interval)
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            outcome = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        record = {**planned, "executed_at": now.isoformat(), **outcome}
        runs[job.key] = record
        result["processed"].append(record)
    if not dry_run:
        state["updated_at"] = now.isoformat()
        atomic_write_json(state_path, state)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="每分鐘 Cron 觸發的 V10.1 賽前 15 分鐘排程器")
    parser.add_argument("--config", required=True, help="賽日與官方開跑時間設定 JSON")
    parser.add_argument("--project-dir", default=".")
    parser.add_argument("--output-root", default="runtime/pre_race")
    parser.add_argument("--state-file", default="runtime/pre_race_state.json")
    parser.add_argument("--odds-min-interval", type=int, default=60)
    parser.add_argument("--now", help="ISO-8601 模擬時間，供測試使用")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.odds_min_interval < 60:
        raise SystemExit("--odds-min-interval 不可少於 60 秒。")
    lock_path = Path(args.state_file).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "skipped_locked"}, ensure_ascii=False))
            return 0
        result = process(
            Path(args.config), Path(args.project_dir).resolve(), Path(args.output_root), Path(args.state_file),
            parse_now(args.now), args.dry_run, args.odds_min_interval,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
