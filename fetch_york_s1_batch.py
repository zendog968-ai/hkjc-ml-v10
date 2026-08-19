#!/usr/bin/env python3
"""Polite, source-labelled York S1 public deep-data batch runner.

The official public York card for 2026-08-19 contains seven races.  This runner
therefore processes verified S1-2 through S1-7 sequentially and records S1-8
as `not_scheduled` rather than inventing a non-existent race.  It neither calls
N6 nor accesses the local V10 database.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fetch_overseas_deep_data import DEFAULT_ATR, USER_AGENT, fetch_public

ROOT = Path(__file__).resolve().parent
DATE = "2026-08-19"
YORK_S1_MANIFEST: tuple[dict[str, Any], ...] = (
    {"race_no": 2, "local_start_time": "14:25 BST", "hkt_start_time": "21:25 HKT", "racing_post_id": "924985"},
    {"race_no": 3, "local_start_time": "15:00 BST", "hkt_start_time": "22:00 HKT", "racing_post_id": "923369"},
    {"race_no": 4, "local_start_time": "15:35 BST", "hkt_start_time": "22:35 HKT", "racing_post_id": "922438"},
    {"race_no": 5, "local_start_time": "16:10 BST", "hkt_start_time": "23:10 HKT", "racing_post_id": "924987"},
    {"race_no": 6, "local_start_time": "16:45 BST", "hkt_start_time": "23:45 HKT", "racing_post_id": "924988"},
    {"race_no": 7, "local_start_time": "17:20 BST", "hkt_start_time": "00:20 HKT (+1)", "racing_post_id": "924989"},
)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="York S1-2 至 S1-7 限速公開深度資料批次抓取；S1-8 只記錄官方賽卡缺席狀態。")
    parser.add_argument("--date", default=DATE, help="賽日；此已核實 York manifest 只接受 2026-08-19。")
    parser.add_argument("--db", default="overseas_deep_racing.sqlite")
    parser.add_argument("--schema", default="schema_overseas_deep_racing.sql")
    parser.add_argument("--raw-dir", default="archive/overseas_deep_raw")
    parser.add_argument("--runtime-dir", default="runtime/overseas_deep")
    parser.add_argument("--summary", default="reports/overseas_deep/york_s1_batch_2026-08-19.json")
    parser.add_argument("--at-the-races-url", default=DEFAULT_ATR)
    parser.add_argument("--at-the-races-cache", default="archive/overseas_deep_raw/york_2026-08-19_attheraces.html")
    parser.add_argument("--request-delay-seconds", type=float, default=2.0)
    parser.add_argument("--timeout", type=int, default=25)
    parser.add_argument("--with-hkjc-odds", action="store_true", help="僅在官方 S1 賠率 URL 已逐場核實時使用；預設不抓取。")
    args = parser.parse_args()
    if args.date != DATE:
        raise SystemExit("此 York batch manifest 只對已核實賽日 2026-08-19 有效；其他日期須先建立官方映射。")
    if args.request_delay_seconds < 1.0:
        raise SystemExit("request-delay-seconds 不可低於 1 秒。")

    cache_path = Path(args.at_the_races_cache)
    if cache_path.is_file():
        atr_mode = "cached_public_page"
    else:
        atr_html, error = fetch_public(args.at_the_races_url, args.timeout)
        if not atr_html:
            raise SystemExit(f"At The Races 公開頁不可用：{error}")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(atr_html, encoding="utf-8")
        atr_mode = "fresh_public_page"

    summary: dict[str, Any] = {
        "schema": "v10_york_s1_deep_batch_v1",
        "meeting_date": args.date,
        "venue": "York",
        "simulcast_code": "S1",
        "n6_status": "disabled_non_hk",
        "started_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "at_the_races_mode": atr_mode,
        "request_delay_seconds": args.request_delay_seconds,
        "events": [],
        "notice": "Only public Racing Post and At The Races pages are accessed. Timeform restricted fields are not accessed. HKJC odds are omitted unless explicitly enabled with verified URLs.",
    }
    for index, event in enumerate(YORK_S1_MANIFEST):
        race_no = int(event["race_no"])
        output = Path(args.runtime_dir) / f"york_s1_{race_no}_deep.json"
        rp_url = f"https://www.racingpost.com/racecards/107/york/{args.date}/{event['racing_post_id']}/"
        racing_post_cache: Path | None = None
        if output.is_file():
            try:
                previous = json.loads(output.read_text(encoding="utf-8"))
                cached_path = previous.get("raw_artifacts", {}).get("racing_post")
                candidate = Path(str(cached_path)) if cached_path else None
                if candidate is not None and candidate.is_file():
                    racing_post_cache = candidate
            except (OSError, ValueError, json.JSONDecodeError):
                racing_post_cache = None
        command = [
            sys.executable, str(ROOT / "fetch_overseas_deep_data.py"),
            "--date", args.date, "--simulcast-code", "S1", "--race-no", str(race_no), "--venue", "York",
            "--racing-post-url", rp_url, "--at-the-races-url", args.at_the_races_url,
            "--at-the-races-html", str(cache_path), "--local-start-time", str(event["local_start_time"]),
            "--hkt-start-time", str(event["hkt_start_time"]), "--db", args.db, "--schema", args.schema,
            "--raw-dir", args.raw_dir, "--output", str(output), "--timeout", str(args.timeout),
        ]
        if racing_post_cache is not None:
            command.extend(["--racing-post-html", str(racing_post_cache)])
        if not args.with_hkjc_odds:
            command.append("--skip-hkjc-odds")
        completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
        item = {**event, "racing_post_url": rp_url, "racing_post_mode": "cached_public_page" if racing_post_cache is not None else "fresh_public_page", "output": str(output), "returncode": completed.returncode}
        if completed.returncode == 0 and output.is_file():
            payload = json.loads(output.read_text(encoding="utf-8"))
            item.update({"status": payload.get("scrape_run", {}).get("status"), "starters": len(payload.get("starters", [])), "n6_status": payload.get("n6_integration", {}).get("status")})
        else:
            item.update({"status": "failed", "stderr_tail": completed.stderr[-500:]})
        summary["events"].append(item)
        if index < len(YORK_S1_MANIFEST) - 1:
            time.sleep(args.request_delay_seconds)

    summary["events"].append({
        "race_no": 8,
        "status": "not_scheduled",
        "reason": "The verified public York card lists seven races (13:50 through 17:20 BST); no eighth York race is created.",
        "n6_status": "disabled_non_hk",
    })
    summary["finished_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    atomic_json(Path(args.summary), summary)
    complete = sum(event.get("status") == "complete" for event in summary["events"])
    print(json.dumps({"status": "complete", "races_complete": complete, "events": len(summary["events"]), "summary": str(args.summary)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
