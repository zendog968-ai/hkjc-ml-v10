#!/usr/bin/env python3
"""V10.2 persistent-host pre-race scheduler with T-15 / T-5 odds snapshots.

Install as a per-minute Cron process. Every configured race has two idempotent stages:
T_MINUS_15 collects race-card, optional new-horse priors and public odds; T_MINUS_5
collects a second odds snapshot, calls the ensemble predictor and writes the filter report.
No messages are sent and no bet is placed.
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
DEFAULT_SNAPSHOT_MINUTES = (15, 5)


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
        json.dump(payload, handle, ensure_ascii=False, indent=2); handle.write("\n"); temporary = handle.name
    os.replace(temporary, path)


def load_json(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else fallback
    except (OSError, json.JSONDecodeError):
        return fallback


def normalize_date(value: str) -> str:
    return datetime.strptime(str(value).replace("/", "-"), "%Y-%m-%d").strftime("%Y/%m/%d")


def parse_start(date: str, time_text: str) -> datetime:
    return datetime.strptime(f"{date.replace('/', '-')} {str(time_text).strip()}", "%Y-%m-%d %H:%M").replace(tzinfo=HK_TZ)


def load_jobs(config_path: Path) -> tuple[tuple[int, ...], list[RaceJob]]:
    payload = load_json(config_path, {})
    if payload.get("timezone", "Asia/Hong_Kong") != "Asia/Hong_Kong":
        raise ValueError("目前只支援 Asia/Hong_Kong 賽事排程。")
    meeting = payload.get("meeting")
    if not isinstance(meeting, dict): raise ValueError("排程設定缺少 meeting 物件。")
    date, racecourse = normalize_date(meeting.get("race_date", "")), str(meeting.get("racecourse", "")).upper()
    if racecourse not in {"ST", "HV"}: raise ValueError("meeting.racecourse 必須為 ST 或 HV。")
    times = meeting.get("race_start_times")
    if not isinstance(times, dict) or not times: raise ValueError("meeting.race_start_times 必須為 {場次: 'HH:MM'}。")
    configured = payload.get("snapshot_minutes_before", list(DEFAULT_SNAPSHOT_MINUTES))
    try: offsets = tuple(sorted({int(value) for value in configured}, reverse=True))
    except (TypeError, ValueError): raise ValueError("snapshot_minutes_before 必須為分鐘整數陣列，例如 [15,5]。")
    if not offsets or any(value < 1 or value > 120 for value in offsets): raise ValueError("snapshot_minutes_before 每個值必須介乎 1 至 120。")
    jobs = [RaceJob(date, racecourse, int(no), parse_start(date, str(start))) for no, start in times.items()]
    return offsets, sorted(jobs, key=lambda item: item.race_no)


def parse_now(value: str | None) -> datetime:
    if not value: return datetime.now(HK_TZ)
    parsed = datetime.fromisoformat(value)
    return (parsed.replace(tzinfo=HK_TZ) if parsed.tzinfo is None else parsed).astimezone(HK_TZ)


def due_stages(jobs: list[RaceJob], offsets: tuple[int, ...], now: datetime) -> list[tuple[RaceJob, int]]:
    now_minute = now.replace(second=0, microsecond=0); due: list[tuple[RaceJob, int]] = []
    for job in jobs:
        for offset in offsets:
            target = (job.start_at - timedelta(minutes=offset)).replace(second=0, microsecond=0)
            if target <= now_minute < target + timedelta(minutes=1): due.append((job, offset))
    return due


def run_command(command: list[str], output_dir: Path, step: str, timeout: int) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(command, cwd=output_dir.parent.parent, capture_output=True, text=True, timeout=timeout, check=False)
    log_path = output_dir / f"{step}.log"
    log_path.write_text("$ " + " ".join(command) + "\n\n--- stdout ---\n" + (completed.stdout or "") + "\n--- stderr ---\n" + (completed.stderr or ""), encoding="utf-8")
    return {"step": step, "returncode": completed.returncode, "log": str(log_path), "command": command}


def script_paths(project_dir: Path) -> dict[str, Path]:
    names = {"racecard": "fetch_hkjc_racecard.py", "odds": "fetch_hkjc_live_odds.py", "predict": "predict.py", "filter": "filter_high_probability.py", "new_horse": "enrich_hkjc_new_horse_priors.py"}
    paths = {key: project_dir / name for key, name in names.items()}
    missing = [path.name for key, path in paths.items() if key != "new_horse" and not path.exists()]
    if missing: raise FileNotFoundError("缺少流程腳本：" + ", ".join(missing))
    return paths


def execute_stage(job: RaceJob, offset: int, project_dir: Path, output_root: Path, odds_min_interval: int) -> dict[str, Any]:
    paths = script_paths(project_dir)
    for required in (project_dir / "hkjc_last_season.sqlite", project_dir / "horse_model.pkl"):
        if not required.exists(): raise FileNotFoundError(f"缺少模型資料：{required.name}")
    output_dir = output_root / job.key; output_dir.mkdir(parents=True, exist_ok=True); python = sys.executable
    card = output_dir / "race_card.json"; win = output_dir / "odds_overlay.json"; place = output_dir / "place_odds_overlay.json"; combined = output_dir / "odds_overlay_combined.json"; meta = output_dir / "odds_overlay.meta.json"
    snapshot = output_dir / f"odds_t_minus_{offset}.json"; outcomes: list[dict[str, Any]] = []
    if offset == max(DEFAULT_SNAPSHOT_MINUTES) or not card.exists():
        commands = [("racecard", [python, str(paths["racecard"]), "--date", job.date, "--racecourse", job.racecourse, "--race-no", str(job.race_no), "--output", str(card)], 90)]
        for name, command, timeout in commands:
            result = run_command(command, output_dir, name, timeout); outcomes.append(result)
            if result["returncode"]: return {"status": "failed", "failed_step": name, "output_dir": str(output_dir), "steps": outcomes}
        if paths["new_horse"].exists():
            result = run_command([python, str(paths["new_horse"]), "--db", str(project_dir / "hkjc_last_season.sqlite"), "--race-card", str(card), "--report", str(output_dir / "new_horse_priors_report.json")], output_dir, "new_horse_priors", 180)
            outcomes.append(result)  # Non-fatal: unknown priors safely remain neutral.
    odds_command = [python, str(paths["odds"]), "--race-card", str(card), "--output", str(win), "--place-output", str(place), "--combined-output", str(combined), "--metadata-output", str(meta), "--snapshot-output", str(snapshot), "--snapshot-label", f"T_MINUS_{offset}", "--race-date", job.date, "--racecourse", job.racecourse, "--race-no", str(job.race_no), "--min-interval", str(odds_min_interval), "--state-file", str(output_root / "live_odds_rate_limit_state.json")]
    result = run_command(odds_command, output_dir, f"odds_t_minus_{offset}", 90); outcomes.append(result)
    if result["returncode"]: return {"status": "failed", "failed_step": "odds", "output_dir": str(output_dir), "steps": outcomes}
    # Only the final (T-5) stage runs prediction / report. It tolerates a missing T-15 snapshot.
    if offset == min(DEFAULT_SNAPSHOT_MINUTES):
        prediction, csv_file = output_dir / "prediction.json", output_dir / "prediction.csv"
        early = output_dir / "odds_t_minus_15.json"; filtered, markdown = output_dir / "high_probability_filter.json", output_dir / "pre_race_report.md"
        commands = [
            ("predict", [python, str(paths["predict"]), "--db", str(project_dir / "hkjc_last_season.sqlite"), "--model", str(project_dir / "horse_model.pkl"), "--race-card", str(card), "--win-odds-overlay", str(win), "--place-odds-overlay", str(place), "--odds-snapshot-early", str(early), "--odds-snapshot-late", str(snapshot), "--output-json", str(prediction), "--output-csv", str(csv_file)], 180),
            ("filter", [python, str(paths["filter"]), "--prediction", str(prediction), "--output", str(filtered), "--markdown-output", str(markdown)], 60),
        ]
        for name, command, timeout in commands:
            result = run_command(command, output_dir, name, timeout); outcomes.append(result)
            if result["returncode"]: return {"status": "failed", "failed_step": name, "output_dir": str(output_dir), "steps": outcomes}
        payload = load_json(filtered, {})
        return {"status": "completed", "stage": f"T_MINUS_{offset}", "output_dir": str(output_dir), "steps": outcomes, "selection_count": payload.get("selection_count", 0), "markdown_report": str(markdown), "whatsapp_direct_link": (payload.get("whatsapp") or {}).get("direct_link"), "odds_status": load_json(meta, {}).get("status", "unknown")}
    return {"status": "snapshot_collected", "stage": f"T_MINUS_{offset}", "output_dir": str(output_dir), "snapshot": str(snapshot), "steps": outcomes, "odds_status": load_json(meta, {}).get("status", "unknown")}


def process(config_path: Path, project_dir: Path, output_root: Path, state_path: Path, now: datetime, dry_run: bool, odds_min_interval: int) -> dict[str, Any]:
    offsets, jobs = load_jobs(config_path); candidates = due_stages(jobs, offsets, now); state = load_json(state_path, {"runs": {}}); runs = state.setdefault("runs", {})
    result: dict[str, Any] = {"checked_at": now.isoformat(), "snapshot_minutes_before": list(offsets), "configured_jobs": len(jobs), "due_stages": [{"job": job.key, "offset": offset} for job, offset in candidates], "processed": [], "dry_run": dry_run}
    for job, offset in candidates:
        job_state = runs.setdefault(job.key, {"stages": {}}); stage_key = f"T_MINUS_{offset}"; prior = job_state.setdefault("stages", {}).get(stage_key, {})
        if prior.get("status") in {"snapshot_collected", "completed"}:
            result["processed"].append({"job": job.key, "stage": stage_key, "status": "already_completed"}); continue
        planned = {"job": job.key, "stage": stage_key, "race_date": job.date, "racecourse": job.racecourse, "race_no": job.race_no, "scheduled_start": job.start_at.isoformat(), "target_trigger": (job.start_at - timedelta(minutes=offset)).isoformat()}
        if dry_run: result["processed"].append({**planned, "status": "dry_run_due"}); continue
        try: outcome = execute_stage(job, offset, project_dir, output_root, odds_min_interval)
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc: outcome = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        record = {**planned, "executed_at": now.isoformat(), **outcome}; job_state["stages"][stage_key] = record; result["processed"].append(record)
    if not dry_run: state["updated_at"] = now.isoformat(); atomic_write_json(state_path, state)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="每分鐘 Cron 觸發的 V10.2 賽前 T-15／T-5 雙快照排程器")
    parser.add_argument("--config", required=True); parser.add_argument("--project-dir", default="."); parser.add_argument("--output-root", default="runtime/pre_race"); parser.add_argument("--state-file", default="runtime/pre_race_state.json"); parser.add_argument("--odds-min-interval", type=int, default=60); parser.add_argument("--now"); parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.odds_min_interval < 60: raise SystemExit("--odds-min-interval 不可少於 60 秒。")
    lock = Path(args.state_file).with_suffix(".lock"); lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("w", encoding="utf-8") as handle:
        try: fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: print(json.dumps({"status": "skipped_locked"}, ensure_ascii=False)); return 0
        result = process(Path(args.config), Path(args.project_dir).resolve(), Path(args.output_root), Path(args.state_file), parse_now(args.now), args.dry_run, args.odds_min_interval)
    print(json.dumps(result, ensure_ascii=False, indent=2)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
