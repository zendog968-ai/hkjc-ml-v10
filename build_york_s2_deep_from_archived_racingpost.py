#!/usr/bin/env python3
"""Materialise York S2 research artifacts from archived public Racing Post text.

The source text is captured through the approved public text-extraction route and
is retained unchanged with SHA-256 provenance. This utility neither requests a
website nor accesses V10 local data or N6. It does not infer unavailable ATR,
Timeform or pace fields. RPR/TS-only rankings are explicitly research-only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fetch_overseas_deep_data import clean_name, norm, persist, score_rows

ROOT = Path(__file__).resolve().parent
DATE = "2026-08-20"
MANIFEST: tuple[dict[str, Any], ...] = (
    {"race_no": 1, "rp_id": "923370", "local_start_time": "13:50 BST", "hkt_start_time": "20:50 HKT", "place_dividends": 3},
    {"race_no": 2, "rp_id": "910568", "local_start_time": "14:25 BST", "hkt_start_time": "21:25 HKT", "place_dividends": 4},
    {"race_no": 3, "rp_id": "923371", "local_start_time": "15:00 BST", "hkt_start_time": "22:00 HKT", "place_dividends": 3},
    {"race_no": 4, "rp_id": "922439", "local_start_time": "15:35 BST", "hkt_start_time": "22:35 HKT", "place_dividends": 3},
    {"race_no": 5, "rp_id": "924990", "local_start_time": "16:10 BST", "hkt_start_time": "23:10 HKT", "place_dividends": 3},
    {"race_no": 6, "rp_id": "924992", "local_start_time": "16:45 BST", "hkt_start_time": "23:45 HKT", "place_dividends": 4},
    {"race_no": 7, "rp_id": "924991", "local_start_time": "17:20 BST", "hkt_start_time": "00:20 HKT (+1)", "place_dividends": 4},
)
SUPERSCRIPTS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_display_name(value: str) -> str:
    value = value.translate(SUPERSCRIPTS)
    value = re.sub(r"\s+", " ", value).strip()
    return clean_name(value)


def number_or_none(value: str) -> int | None:
    return int(value) if value.isdigit() else None


def parse_card(text: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    head = text.split("Show all racecards for this meeting", 1)[0]
    race_name_match = re.search(r"^## \*\*(.+?)\*\*$", head, flags=re.M)
    runner_count_match = re.search(r"Runners:\s*(\d+)\s*\(MAX", head)
    going_match = re.search(r"Going:\s*([^\n]+)", head)
    class_match = re.search(r"Flat Turf,\s*([^\n]+)", head)
    distance_match = re.search(r"## \*\*(\d+(?:m\d*f?|f)(?:\s*\([^)]*\))?)\*\*", head)
    pattern = re.compile(
        r"Silk\n\n(?P<no>\d+|NR) \((?P<draw>\d+)\)\n\n(?P<horse>[^\n]+)\n.*?"
        r"OR:\s*(?P<or>-|\d+)\s*TS:\s*(?P<ts>-|\d+)\s*RPR:\s*(?P<rpr>\d+)",
        flags=re.S,
    )
    starters: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen_numbers: set[int] = set()
    for match in pattern.finditer(head):
        no_text = match.group("no")
        horse = clean_display_name(match.group("horse"))
        if no_text == "NR":
            warnings.append(f"公開 Racing Post 快照列為 NR，已排除：{horse}")
            continue
        runner_no = int(no_text)
        if runner_no in seen_numbers:
            continue
        key = norm(horse)
        if not key:
            warnings.append(f"無法正規化的馬名已排除：{match.group('horse')}")
            continue
        seen_numbers.add(runner_no)
        starters.append({
            "runner_no": runner_no,
            "draw_no": int(match.group("draw")),
            "horse_name": horse,
            "official_rating": number_or_none(match.group("or")),
            "racing_post_rating": int(match.group("rpr")),
            "top_speed_rating": number_or_none(match.group("ts")),
            "at_the_races_rating": None,
            "sire": None,
            "dam": None,
            "damsire": None,
            "pace_hint": None,
            "distance_runs": None,
            "distance_wins": None,
            "similar_going_runs": None,
            "similar_going_wins": None,
            "course_runs": None,
            "course_wins": None,
            "hkjc_win_odds": None,
            "hkjc_place_odds": None,
            "source_rpr_ts_url": None,
            "source_form_url": None,
            "source_hkjc_odds_url": None,
            "data_completeness": "partial_rpr_ts_only",
        })
    starters.sort(key=lambda row: int(row["runner_no"]))
    expected = int(runner_count_match.group(1)) if runner_count_match else None
    race = {
        "race_name": race_name_match.group(1).replace("**", "") if race_name_match else None,
        "distance_text": distance_match.group(1) if distance_match else None,
        "going": going_match.group(1).strip() if going_match else None,
        "race_class": class_match.group(1).strip() if class_match else None,
        "declared_runners": expected,
    }
    if expected is None:
        warnings.append("公開 Racing Post 快照未能解析公布出馬數。")
    if expected is not None and expected != len(starters):
        warnings.append(f"Racing Post 公布出馬 {expected} 匹；可解析有效列 {len(starters)} 匹。此差異保留，不能視為全資料完成。")
    if not starters:
        warnings.append("未能從公開 Racing Post 快照解析任何有效馬匹。")
    return race, starters, warnings


def make_payload(event: dict[str, Any], source_path: Path, atr_url: str) -> dict[str, Any]:
    text = source_path.read_text(encoding="utf-8")
    sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    race_fields, starters, warnings = parse_card(text)
    score_rows(starters)
    rp_url = f"https://www.racingpost.com/racecards/107/york/{DATE}/{event['rp_id']}/"
    roster_complete = race_fields.get("declared_runners") == len(starters) and bool(starters)
    source_status = "complete_rpr_ts_roster" if roster_complete else "partial_roster_or_parser"
    return {
        "schema_version": "v10_overseas_deep_scraper_v1",
        "scrape_run": {
            "meeting_date": DATE,
            "simulcast_code": "S2",
            "race_no": event["race_no"],
            "venue": "York",
            "status": "complete" if roster_complete else "partial",
            "n6_status": "disabled_non_hk",
            "fetched_at_utc": utc_now(),
            "racing_post_url": rp_url,
            "at_the_races_url": atr_url,
            "timeform_url": None,
            "hkjc_odds_source": None,
            "source_notes": " | ".join([f"來源解析狀態：{source_status}"] + warnings + ["使用已歸檔的公開 Racing Post 文字快照；沒有新增外部請求。At The Races 逐馬條件欄位在此工件未完整解析，因此不被推定或填補。Timeform 受限欄位未存取。"]),
        },
        "race": {
            "meeting_date": DATE,
            "simulcast_code": "S2",
            "race_no": event["race_no"],
            "venue": "York",
            "local_start_time": event["local_start_time"],
            "hkt_start_time": event["hkt_start_time"],
            "source_status": "complete" if roster_complete else "partial",
            **race_fields,
        },
        "n6_integration": {"status": "disabled_non_hk", "message": "S1/S2 使用海外公開深度研究；HK訓練N6不會被呼叫。"},
        "field_availability": {
            "rpr": "available_public" if starters else "unavailable_parse",
            "top_speed": "available_public" if any(row.get("top_speed_rating") is not None for row in starters) else "unavailable_parse",
            "at_the_races_condition_form": "unavailable_parse_not_in_archived_per_race_extract",
            "pace_setup": "unavailable_paid_or_restricted",
            "timeform_tfr": "unavailable_paid_or_restricted",
            "hkjc_odds": "not_requested",
        },
        "scoring_method": "Public Racing Post RPR/TS-only min-max composite (RPR nominal 50%, TS nominal 25%; missing ATR condition fields are not imputed and available components are renormalized). This is an uncalibrated overseas research score, never V10.2 probability/EV/Kelly.",
        "starters": starters,
        "raw_artifacts": {"racing_post_webextract_markdown": str(source_path), "racing_post_webextract_sha256": sha256},
        "source_provenance": {"approved_text_extraction": True, "no_network_request_in_materialisation": True, "place_dividends_by_hkjc_notice": event["place_dividends"]},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="將已歸檔 York S2 公開 Racing Post 賽卡文字轉為隔離深度研究工件。")
    parser.add_argument("--source-dir", default="archive/overseas_deep_raw/s2_2026-08-20_webextract")
    parser.add_argument("--output-dir", default="runtime/overseas_deep")
    parser.add_argument("--db", default="overseas_deep_racing.sqlite")
    parser.add_argument("--schema", default="schema_overseas_deep_racing.sql")
    parser.add_argument("--summary", default="reports/overseas_deep/york_s2_public_deep_2026-08-20.json")
    parser.add_argument("--atr-url", default="https://www.attheraces.com/racecards/York/20-August-2026")
    args = parser.parse_args()
    source_dir = Path(args.source_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    events: list[dict[str, Any]] = []
    for event in MANIFEST:
        source_path = source_dir / f"racingpost_s2_{event['race_no']}_{event['rp_id']}.md"
        if not source_path.is_file():
            raise SystemExit(f"缺少已歸檔來源：{source_path}")
        payload = make_payload(event, source_path, args.atr_url)
        output = output_dir / f"york_s2_{event['race_no']}_deep.json"
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        persist(Path(args.db), Path(args.schema), payload)
        events.append({
            "race_no": event["race_no"],
            "racing_post_id": event["rp_id"],
            "output": str(output),
            "source_status": payload["race"]["source_status"],
            "declared_runners": payload["race"]["declared_runners"],
            "parsed_active_runners": len(payload["starters"]),
            "ranked_runners": sum(row.get("deep_rank") is not None for row in payload["starters"]),
            "warnings": payload["scrape_run"]["source_notes"],
        })
    summary = {"schema": "v10_york_s2_public_deep_batch_v1", "meeting_date": DATE, "simulcast_code": "S2", "n6_status": "disabled_non_hk", "events": events}
    summary_path = Path(args.summary)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": "complete", "events": len(events), "summary": str(summary_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
