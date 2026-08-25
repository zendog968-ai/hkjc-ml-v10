#!/usr/bin/env python3
"""Fetch a public HKJC race card and convert it to the JSON input required by predict.py.

This parser deliberately reads only the public HKJC race-card page, makes one request,
and does not bypass rate limits or access controls. Live odds can be supplied separately
as an overlay JSON because their page is dynamic and changes continuously.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests
from bs4 import BeautifulSoup, Tag

BASE_URL = "https://racing.hkjc.com/racing/information/Chinese/Racing/RaceCard.aspx"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; HKJCV10Research/1.1; public-data-research)",
    "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8",
}


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\xa0", " ")).strip()


def first_number(value: str) -> Optional[float]:
    match = re.search(r"\d+(?:\.\d+)?", value or "")
    return float(match.group()) if match else None


def strip_horse_code(value: str) -> str:
    return normalize(re.sub(r"\s*\([A-Z]\d+\)\s*$", "", normalize(value)))


def find_table(soup: BeautifulSoup) -> Optional[Tag]:
    required = ("馬號", "馬名", "騎師", "練馬師", "檔位")
    for table in soup.find_all("table"):
        text = normalize(table.get_text(" ", strip=True))
        if all(token in text for token in required):
            return table
    return None


def header_map(table: Tag) -> dict[str, int]:
    rows = table.find_all("tr")[:4]
    best: list[str] = []
    for row in rows:
        cells = [normalize(cell.get_text(" ", strip=True)) for cell in row.find_all(["td", "th"], recursive=False)]
        if len(cells) > len(best):
            best = cells
    mapping: dict[str, int] = {}
    targets = {
        "horse_no": "馬號", "horse_name": "馬名", "horse_code": "烙號",
        "jockey": "騎師", "trainer": "練馬師", "weight_lbs": "負磅", "draw": "檔位",
        "equipment": "配備", "horse_body_weight_lbs": "排位體重",
        "official_body_weight_delta_lbs": "排位體重+/-",
    }
    for field, token in targets.items():
        for index, label in enumerate(best):
            if token in label:
                mapping[field] = index
                break
    return mapping


def metadata(soup: BeautifulSoup) -> dict[str, Any]:
    text = normalize(soup.get_text(" ", strip=True))
    matched = re.search(r"((?:第一|第二|第三|第四|第五)班|新馬賽|一班|二班|三班|四班|五班)[^\-]{0,12}-\s*(\d+)米", text)
    race_class = normalize(matched.group(1)) if matched else "未知"
    distance_m = int(matched.group(2)) if matched else None
    track = ""
    going = ""
    for td in soup.find_all("td"):
        label = normalize(td.get_text(" ", strip=True)).replace(" ", "")
        sibling = td.find_next_sibling("td")
        if sibling and label in {"場地狀況:", "場地狀況"}:
            going = normalize(sibling.get_text(" ", strip=True))
        if sibling and label in {"賽道:", "賽道"}:
            track = normalize(sibling.get_text(" ", strip=True))
    surface = "全天候" if "全天候" in track else "草地" if "草地" in track else "未知"
    course_match = re.search(r'"([A-Z][+0-9]*)"', track)
    return {
        "race_class": race_class,
        "distance_m": distance_m,
        "surface": surface,
        "course_config": course_match.group(1) if course_match else "未知",
        "going": going or "未知",
    }


def parse_odds_overlay(path: Optional[str]) -> dict[str, float]:
    if not path:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return {str(key): float(value) for key, value in payload.items() if value not in (None, "")}


def fetch(
    date: str,
    racecourse: str,
    race_no: int,
    output: str,
    odds_overlay: Optional[str] = None,
    raw_html_output: Optional[str] = None,
) -> dict[str, Any]:
    time.sleep(1.5)  # conservative pause before a single public request
    response = requests.get(
        BASE_URL,
        params={"RaceDate": date, "Racecourse": racecourse.upper(), "RaceNo": race_no},
        headers=HEADERS,
        timeout=35,
    )
    if response.status_code in {403, 429}:
        raise RuntimeError(f"HKJC 回傳 HTTP {response.status_code}；已停止，請稍後重試。")
    response.raise_for_status()
    fetched_at_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    raw_html = response.content
    raw_sha256 = hashlib.sha256(raw_html).hexdigest()
    raw_path: str | None = None
    if raw_html_output:
        raw_target = Path(raw_html_output)
        raw_target.parent.mkdir(parents=True, exist_ok=True)
        raw_target.write_bytes(raw_html)
        raw_path = str(raw_target)
    soup = BeautifulSoup(response.text, "html.parser")
    table = find_table(soup)
    if table is None:
        raise ValueError("官方排位表頁未找到完整排位資料；賽事可能尚未公佈或網址資料不正確。")
    mapping = header_map(table)
    required = {"horse_no", "horse_name", "jockey", "trainer", "weight_lbs", "draw"}
    if required - set(mapping):
        raise ValueError(f"排位表欄位不完整：缺少 {', '.join(sorted(required-set(mapping)))}")
    odds = parse_odds_overlay(odds_overlay)
    runners = []
    for row in table.find_all("tr"):
        cells = [normalize(cell.get_text(" ", strip=True)) for cell in row.find_all("td", recursive=False)]
        if not cells or len(cells) <= max(mapping.values()):
            continue
        horse_no = first_number(cells[mapping["horse_no"]])
        draw = first_number(cells[mapping["draw"]])
        weight = first_number(cells[mapping["weight_lbs"]])
        name = strip_horse_code(cells[mapping["horse_name"]])
        if horse_no is None or draw is None or weight is None or not name or name == "馬名":
            continue
        def optional_cell(field: str) -> str | None:
            return normalize(cells[mapping[field]]) if field in mapping and len(cells) > mapping[field] else None

        body_weight = first_number(optional_cell("horse_body_weight_lbs") or "")
        official_body_weight_delta = first_number(optional_cell("official_body_weight_delta_lbs") or "")
        runner = {
            "horse_no": int(horse_no), "horse_name": name,
            "horse_code": optional_cell("horse_code"),
            "draw": int(draw), "weight_lbs": float(weight),
            "jockey": normalize(cells[mapping["jockey"]]), "trainer": normalize(cells[mapping["trainer"]]),
            # Some historical or exceptional cards omit public equipment / body-weight columns.
            "equipment": optional_cell("equipment"),
            "horse_body_weight_lbs": body_weight,
            "official_body_weight_delta_lbs": official_body_weight_delta,
        }
        if name in odds:
            runner["market_odds"] = odds[name]
        runners.append(runner)
    if len(runners) < 2:
        raise ValueError("未能從官方頁解析至少兩匹有效出賽馬。")
    race = metadata(soup)
    if race["distance_m"] is None:
        raise ValueError("未能從官方頁解析路程。")
    race.update({"racecourse": racecourse.upper(), "race_date": date, "race_no": race_no})
    payload = {
        "race": race,
        "runners": sorted(runners, key=lambda row: row["horse_no"]),
        "source": {
            "url": response.url,
            "fetched_at_utc": fetched_at_utc,
            "raw_html_sha256": raw_sha256,
            "raw_html_path": raw_path,
        },
    }
    Path(output).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="讀取 HKJC 官方排位表並輸出模型輸入 JSON")
    parser.add_argument("--date", required=True, help="YYYY/MM/DD")
    parser.add_argument("--racecourse", required=True, choices=["ST", "HV", "st", "hv"])
    parser.add_argument("--race-no", required=True, type=int)
    parser.add_argument("--output", default="race_card.json")
    parser.add_argument("--odds-overlay", help="可選 JSON：{馬名: HKJC獨贏賠率}，供 EV 比較使用")
    parser.add_argument("--raw-html-output", help="可選：封存原始官方排位表HTML；不影響既有JSON介面")
    args = parser.parse_args()
    result = fetch(args.date, args.racecourse, args.race_no, args.output, args.odds_overlay, args.raw_html_output)
    print(json.dumps({"race": result["race"], "runner_count": len(result["runners"])}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
