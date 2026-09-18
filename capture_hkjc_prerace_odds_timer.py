#!/usr/bin/env python3
"""Bounded HKJC pre-race odds snapshot runner.

This runner captures only complete public Win/Place snapshots at T-15/T-5,
never invokes V10/N6 inference, and never imports degraded captures.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HKT = timezone(timedelta(hours=8))
DEFAULT_SCHEDULE = Path("runtime/pre_race_schedule_current.json")
DEFAULT_ROOT = Path("runtime/pre_race")
LOCK_PATH = Path("/run/lock/hkjc-prerace-odds-capture.lock")
TOLERANCE_SECONDS = 90


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_schedule(path: Path) -> tuple[str, str, dict[str, str]]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    meeting = obj.get("meeting") or {}
    date = str(meeting.get("race_date") or "").replace("/", "-")
    course = str(meeting.get("racecourse") or "").upper()
    times = meeting.get("race_start_times") or {}
    if date != "2026-09-06" or course != "ST" or not isinstance(times, dict):
        raise ValueError("只接受已核實的2026-09-06 ST manifest")
    return date, course, {str(k): str(v) for k, v in times.items()}


def start_dt(date: str, hhmm: str) -> datetime:
    hh, mm = (int(x) for x in hhmm.split(":", 1))
    return datetime.fromisoformat(f"{date}T{hh:02d}:{mm:02d}:00+08:00")


def due_label(now: datetime, start: datetime) -> str | None:
    seconds = (start - now).total_seconds()
    for label, target in (("T_MINUS_15", 900), ("T_MINUS_5", 300)):
        if abs(seconds - target) <= TOLERANCE_SECONDS:
            return label
    return None


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--schedule", type=Path, default=DEFAULT_SCHEDULE)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--now-hkt", help="離線測試用 ISO HKT 時間；不發送網絡請求")
    parser.add_argument("--fetch-script", type=Path, default=Path("fetch_hkjc_live_odds.py"))
    args = parser.parse_args()
    if not args.schedule.exists():
        print(json.dumps({"status": "safe_skip", "reason": "schedule_manifest_missing"}, ensure_ascii=False))
        return 0
    date, course, times = load_schedule(args.schedule)
    now = parse_dt(args.now_hkt) if args.now_hkt else datetime.now(HKT)
    now = now.astimezone(HKT)
    due: list[tuple[str, str, datetime]] = []
    for race_no, hhmm in sorted(times.items(), key=lambda item: int(item[0])):
        start = start_dt(date, hhmm)
        label = due_label(now, start)
        if label:
            due.append((race_no, label, start))
    if not due:
        print(json.dumps({"status": "idle", "reason": "no_t15_or_t5_window", "now_hkt": now.isoformat()}, ensure_ascii=False))
        return 0
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "safe_skip", "reason": "same_workflow_locked"}, ensure_ascii=False))
            return 0
        results = []
        for race_no, label, start in due:
            out_dir = args.root / "odds_snapshots" / date.replace("-", "") / f"ST_{race_no}"
            out_dir.mkdir(parents=True, exist_ok=True)
            stem = f"{date}_ST_{int(race_no):02d}_{label}"
            snapshot = out_dir / f"{stem}.json"
            if snapshot.exists() and snapshot.stat().st_size > 0:
                results.append({"race_no": int(race_no), "label": label, "status": "already_captured", "snapshot": str(snapshot)})
                continue
            metadata = out_dir / f"{stem}.meta.json"
            state = args.root / "odds_state.json"
            url = f"https://bet.hkjc.com/ch/racing/wp/{date.replace('-', '/')}/ST/{int(race_no)}"
            cmd = [
                sys.executable, str(args.fetch_script),
                "--url", url,
                "--output", str(out_dir / f"{stem}.win.json"),
                "--place-output", str(out_dir / f"{stem}.place.json"),
                "--combined-output", str(out_dir / f"{stem}.combined.json"),
                "--metadata-output", str(metadata),
                "--state-file", str(state),
                "--min-interval", "60",
                "--snapshot-output", str(snapshot),
                "--snapshot-label", label,
                "--race-date", date,
                "--racecourse", course,
                "--race-no", race_no,
            ]
            if args.dry_run or args.now_hkt:
                results.append({"race_no": int(race_no), "label": label, "would_run": cmd, "start_hkt": start.isoformat()})
                continue
            proc = subprocess.run(cmd, cwd=Path.cwd(), text=True, capture_output=True, check=False)
            meta = {}
            if metadata.exists():
                try:
                    meta = json.loads(metadata.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    meta = {"status": "invalid_metadata"}
            complete = proc.returncode == 0 and meta.get("status") == "complete" and snapshot.exists()
            if not complete:
                # Do not let a degraded or partial source result reach the archive importer.
                if snapshot.exists():
                    snapshot.unlink()
                results.append({"race_no": int(race_no), "label": label, "status": "rejected_fail_closed", "returncode": proc.returncode, "metadata_status": meta.get("status"), "stderr_tail": proc.stderr[-500:]})
                continue
            results.append({"race_no": int(race_no), "label": label, "status": "complete_snapshot_ready", "snapshot": str(snapshot)})
        atomic_json(args.root / "last_timer_run.json", {"ran_at_hkt": now.isoformat(), "results": results})
        print(json.dumps({"status": "ok", "due": len(due), "results": results}, ensure_ascii=False, indent=2))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
