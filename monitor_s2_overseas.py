#!/usr/bin/env python3
"""Validated, rate-limited S2 overseas deep-data monitor.

This utility is intentionally inert without a signed-off official manifest. It
uses the existing isolated overseas schema and does not invoke N6 or touch the
Hong Kong V10 database.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
ALLOWED_HOSTS = {"www.racingpost.com", "racingpost.com", "www.attheraces.com", "attheraces.com", "bet.hkjc.com"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def valid_url(value: object, allowed_hosts: set[str]) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme == "https" and parsed.hostname in allowed_hosts


def load_manifest(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"S2 manifest 不可讀取：{type(exc).__name__}") from exc
    if not isinstance(data, dict) or data.get("simulcast_code") != "S2":
        raise SystemExit("S2 manifest 必須明確宣告 simulcast_code 為 S2。")
    if not isinstance(data.get("meeting_date"), str) or not isinstance(data.get("venue"), str) or not isinstance(data.get("events"), list):
        raise SystemExit("S2 manifest 缺少 meeting_date、venue 或 events。")
    return data


def validate_event(event: object) -> tuple[bool, str]:
    if not isinstance(event, dict):
        return False, "event_not_object"
    if not isinstance(event.get("race_no"), int) or not 1 <= event["race_no"] <= 20:
        return False, "invalid_race_no"
    if not isinstance(event.get("local_start_time"), str) or not isinstance(event.get("hkt_start_time"), str):
        return False, "missing_start_time"
    if not valid_url(event.get("racing_post_url"), {"www.racingpost.com", "racingpost.com"}):
        return False, "unverified_racing_post_url"
    if not valid_url(event.get("at_the_races_url"), {"www.attheraces.com", "attheraces.com"}):
        return False, "unverified_at_the_races_url"
    hkjc = event.get("hkjc_win_place_url")
    if hkjc is not None and not valid_url(hkjc, {"bet.hkjc.com"}):
        return False, "unverified_hkjc_url"
    return True, "ready"


def main() -> int:
    parser = argparse.ArgumentParser(description="S2 公開海外深度資料監控；僅接受已核實官方 manifest。")
    parser.add_argument("--manifest", default="runtime/s2_monitor/s2_official_manifest.json")
    parser.add_argument("--db", default="overseas_deep_racing.sqlite")
    parser.add_argument("--schema", default="schema_overseas_deep_racing.sql")
    parser.add_argument("--runtime-dir", default="runtime/overseas_deep")
    parser.add_argument("--raw-dir", default="archive/overseas_deep_raw")
    parser.add_argument("--report", default="reports/overseas_deep/S2_MONITOR_STATUS.json")
    parser.add_argument("--request-delay-seconds", type=float, default=2.0)
    parser.add_argument("--timeout", type=int, default=25)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.request_delay_seconds < 1.0:
        raise SystemExit("request-delay-seconds 不可低於 1 秒。")
    manifest_path, report_path = Path(args.manifest), Path(args.report)
    if not manifest_path.is_file():
        atomic_json(report_path, {"status": "awaiting_official_manifest", "checked_at_utc": now(), "simulcast_code": "S2", "n6_status": "disabled_non_hk", "message": "尚未有已核實的 S2 官方場次映射；不會猜測賽場、場次或來源 URL。"})
        print(json.dumps({"status": "awaiting_official_manifest", "report": str(report_path)}, ensure_ascii=False))
        return 0
    manifest = load_manifest(manifest_path)
    lock_path = ROOT / "runtime" / "s2_monitor" / ".s2_monitor.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            atomic_json(report_path, {"status": "already_running", "checked_at_utc": now(), "simulcast_code": "S2", "n6_status": "disabled_non_hk"})
            return 0
        report = {"schema": "v10_s2_overseas_monitor_v1", "status": "completed", "checked_at_utc": now(), "meeting_date": manifest["meeting_date"], "venue": manifest["venue"], "simulcast_code": "S2", "n6_status": "disabled_non_hk", "request_delay_seconds": args.request_delay_seconds, "events": [], "notice": "只存取 manifest 已核實的公開來源；Timeform 受限內容不存取。市場 EV／Kelly 必須由獨立官方 HKJC 頁完成全場身份匹配後才可啟用。"}
        events = manifest["events"]
        for index, event in enumerate(events):
            valid, reason = validate_event(event)
            item = {"race_no": event.get("race_no") if isinstance(event, dict) else None, "validation": reason, "n6_status": "disabled_non_hk"}
            if not valid:
                item["status"] = "skipped"
                report["events"].append(item)
                continue
            output = Path(args.runtime_dir) / f"{str(manifest['venue']).lower().replace(' ', '_')}_s2_{event['race_no']}_deep.json"
            item["output"] = str(output)
            if args.dry_run:
                item["status"] = "validated_dry_run"
                report["events"].append(item)
                continue
            command = [sys.executable, str(ROOT / "fetch_overseas_deep_data.py"), "--date", manifest["meeting_date"], "--simulcast-code", "S2", "--race-no", str(event["race_no"]), "--venue", manifest["venue"], "--racing-post-url", event["racing_post_url"], "--at-the-races-url", event["at_the_races_url"], "--local-start-time", event["local_start_time"], "--hkt-start-time", event["hkt_start_time"], "--db", args.db, "--schema", args.schema, "--raw-dir", args.raw_dir, "--output", str(output), "--timeout", str(args.timeout), "--skip-hkjc-odds"]
            completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
            item["returncode"] = completed.returncode
            item["status"] = "complete" if completed.returncode == 0 else "failed"
            if completed.returncode != 0:
                item["stderr_tail"] = completed.stderr[-500:]
            report["events"].append(item)
            if index < len(events) - 1:
                time.sleep(args.request_delay_seconds)
        atomic_json(report_path, report)
        print(json.dumps({"status": report["status"], "events": len(report["events"]), "report": str(report_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
