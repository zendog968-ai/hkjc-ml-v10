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
from pathlib import Path
from typing import Any

from overseas_hkjc_core import (
    OfficialOverseasClient,
    OverseasMeeting,
    RACECARD_URL,
    apply_racecard,
    compact_date,
    init_overseas_db,
    parse_racecard_starters,
    upsert_meeting,
    upsert_race,
)

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
    return parser


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
    runners = parse_racecard_starters(card_html)
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
    output = {
        "schema_version": "v10.2_s1s2_racecard_v1",
        "label": "🌍 海外轉播賽 (S1/S2)",
        "race": {"meeting_date": args.date, "simulcast_code": code, "race_no": args.race_no, "overseas_race_id": race_id},
        "source": {"racecard_url": card_url, "odds_url": odds_url, "racecard_mode": "offline_html" if args.racecard_html else "public_hkjc", "odds_mode": "offline_html" if args.odds_html else "public_rendered_page"},
        "status": status,
        "runners": selected,
        "complete_win_place_pairs": complete_pairs,
        "warnings": warnings,
        "odds_safety": "無可驗證公開賠率時，win_odds/place_odds 必須為 null；預測器會保留勝率但不產生 EV/Kelly。",
    }
    atomic_json(Path(args.output), output)
    print(json.dumps({"output": str(Path(args.output)), "status": status, "runners": len(selected), "warnings": warnings}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
