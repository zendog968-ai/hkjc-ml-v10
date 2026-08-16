#!/usr/bin/env python3
"""Fetch a public HKJC overseas S1/S2 race card with safe odds degradation.

The public race card is the source of runner identity.  Live odds are optional:
if the displayed odds table cannot be parsed, every odds field stays null and the
output status becomes degraded rather than fabricating prices.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from overseas_hkjc_core import (
    OfficialOverseasClient,
    OverseasMeeting,
    RACECARD_URL,
    apply_racecard,
    compact_date,
    init_overseas_db,
    parse_racecard_context,
    parse_racecard_starters,
    upsert_meeting,
    upsert_race,
)
from overseas_feature_enrichment import write_snapshot

DEFAULT_ODDS_URL = "https://bet.hkjc.com/en/racing/wp/{date}/{code}/{race_no}"
ROOT = Path(__file__).resolve().parent


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def clean_name(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def parse_odds_html(html: str) -> dict[str, dict[str, float | None]]:
    # Reuse the protected parser, which now recognises Chinese and English headers.
    from fetch_hkjc_live_odds import parse_visible_odds_table
    return parse_visible_odds_table(html)[0]


def fetch_rendered(url: str, timeout: int) -> str:
    from fetch_hkjc_live_odds import fetch_rendered_public_page
    return fetch_rendered_public_page(url, timeout)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="抓取 HKJC S1/S2 海外轉播賽排位及可用公開賠率。")
    parser.add_argument("--date", required=True, help="海外轉播賽日 YYYY-MM-DD")
    parser.add_argument("--simulcast-code", required=True, help="S1、S2 等官方轉播群組代碼")
    parser.add_argument("--race-no", required=True, type=int, help="官方海外場次")
    parser.add_argument("--db", default="hkjc_last_season.sqlite")
    parser.add_argument("--schema", default="schema_overseas_racing.sql")
    parser.add_argument("--raw-dir", default="archive/overseas_hkjc_raw")
    parser.add_argument("--output", default="s1s2_race_card.json")
    parser.add_argument("--racecard-url", help="覆蓋官方公開排位頁 URL；僅供官方 URL 格式變動時使用。")
    parser.add_argument("--odds-url", help="覆蓋公開賠率頁 URL。")
    parser.add_argument("--racecard-html", help="離線官方排位 HTML 測試檔。")
    parser.add_argument("--odds-html", help="離線官方賠率 HTML 測試檔。")
    parser.add_argument("--timeout", type=int, default=35)
    parser.add_argument("--snapshot-label", choices=["T_MINUS_15", "T_MINUS_5", "OTHER"], default="OTHER", help="只在實際賽前捕捉時標示 T_MINUS_15／T_MINUS_5。")
    parser.add_argument("--scheduled-start-utc", help="ISO UTC 開跑時間；T-15/T-5 必填，用於驗證捕捉偏差不超過 180 秒。")
    return parser


def snapshot_timing_ok(label: str, captured_at: datetime, scheduled_start_utc: str | None) -> tuple[bool, str | None]:
    if label == "OTHER":
        return True, None
    if not scheduled_start_utc:
        return False, "T-15/T-5 快照必須提供 --scheduled-start-utc；未驗證時間不會用於落飛計算。"
    try:
        start = datetime.fromisoformat(scheduled_start_utc.replace("Z", "+00:00"))
        if start.tzinfo is None:
            return False, "--scheduled-start-utc 必須含 UTC 時區。"
    except ValueError:
        return False, "--scheduled-start-utc 必須為 ISO UTC 時間。"
    expected = 900 if label == "T_MINUS_15" else 300
    delta = (start.astimezone(timezone.utc) - captured_at).total_seconds()
    if abs(delta - expected) > 180:
        return False, f"捕捉距離開跑 {delta:.0f} 秒，與 {label} 目標偏差超過 180 秒。"
    return True, None


def main() -> int:
    args = build_parser().parse_args()
    try:
        compact = compact_date(args.date)
    except Exception as exc:
        raise SystemExit("--date 必須是 YYYY-MM-DD") from exc
    code = args.simulcast_code.upper()
    if not re.fullmatch(r"S\d+", code) or args.race_no <= 0:
        raise SystemExit("--simulcast-code 必須為 S1、S2 等官方代碼，--race-no 必須為正整數。")
    db = init_overseas_db(Path(args.db), Path(args.schema))
    meeting = OverseasMeeting(args.date, code, None, None, "manual_s1s2_fetch", RACECARD_URL.format(compact_date=compact, code=code), args.race_no)
    meeting_id = upsert_meeting(db, meeting, None)
    race_id = upsert_race(db, meeting_id, meeting, args.race_no, racecard_url=args.racecard_url or meeting.summary_url)
    warnings: list[str] = []
    card_url = args.racecard_url or meeting.summary_url
    if args.racecard_html:
        card_html = Path(args.racecard_html).read_text(encoding="utf-8")
    else:
        client = OfficialOverseasClient(db, Path(args.raw_dir))
        card_html, _ = client.get(card_url, "racecard")
    race_context = parse_racecard_context(card_html)
    race_id = upsert_race(db, meeting_id, meeting, args.race_no, racecard_url=card_url, **race_context)
    card_captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    runners = parse_racecard_starters(card_html)
    for runner in runners:
        if runner.get("international_rating") is not None and runner.get("rating_type"):
            runner["rating_source_url"] = card_url
            runner["rating_as_of_utc"] = card_captured_at
    if not runners:
        raise SystemExit("官方排位頁未能解析出馬匹；已停止，請保存官方 HTML 後以 --racecard-html 檢查解析器。")
    apply_racecard(db, race_id, runners)
    odds_url = args.odds_url or DEFAULT_ODDS_URL.format(date=args.date, code=code, race_no=args.race_no)
    odds: dict[str, dict[str, float | None]] = {}
    try:
        odds_html = Path(args.odds_html).read_text(encoding="utf-8") if args.odds_html else fetch_rendered(odds_url, args.timeout)
        odds = parse_odds_html(odds_html)
    except Exception as exc:  # degraded outcome is intentional for live odds.
        warnings.append(f"公開海外賠率暫不可用：{type(exc).__name__}: {exc}")
    selected = []
    for runner in runners:
        values = odds.get(clean_name(str(runner["horse_name"])), {"win": None, "place": None})
        selected.append({**runner, "win_odds": values.get("win"), "place_odds": values.get("place")})
    complete_pairs = sum(item["win_odds"] is not None and item["place_odds"] is not None for item in selected)
    status = "complete" if not warnings and complete_pairs == len(selected) else "degraded"
    captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    timing_ok, timing_warning = snapshot_timing_ok(args.snapshot_label, datetime.fromisoformat(captured_at), args.scheduled_start_utc)
    if timing_warning:
        warnings.append(timing_warning)
    snapshot_status = "complete" if status == "complete" and timing_ok else "degraded"
    snapshot_id = write_snapshot(db, race_id, args.snapshot_label, captured_at, snapshot_status, odds_url, {int(item["horse_no"]): {"win": item["win_odds"], "place": item["place_odds"]} for item in selected})
    output = {
        "schema_version": "v10.2_s1s2_racecard_v1",
        "label": "🌍 海外轉播賽 (S1/S2)",
        "race": {"meeting_date": args.date, "simulcast_code": code, "race_no": args.race_no, "overseas_race_id": race_id, **race_context},
        "source": {"racecard_url": card_url, "odds_url": odds_url, "racecard_mode": "offline_html" if args.racecard_html else "public_hkjc", "odds_mode": "offline_html" if args.odds_html else "public_rendered_page"},
        "status": status,
        "odds_snapshot_at_utc": captured_at,
        "odds_snapshot_id": snapshot_id,
        "odds_snapshot_label": args.snapshot_label,
        "odds_snapshot_status": snapshot_status,
        "runners": selected,
        "complete_win_place_pairs": complete_pairs,
        "warnings": warnings,
        "odds_safety": "無可驗證公開賠率時，win_odds/place_odds 必須為 null；預測器會保留勝率但不產生 EV/Kelly。T-15/T-5 落飛只接受同場完整、身份匹配且時間偏差合格的雙快照。",
    }
    atomic_json(Path(args.output), output)
    print(json.dumps({"output": str(Path(args.output)), "status": status, "snapshot_status": snapshot_status, "snapshot_id": snapshot_id, "runners": len(selected), "warnings": warnings}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
