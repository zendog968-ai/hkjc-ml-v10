#!/usr/bin/env python3
"""Store public HKJC new-horse pedigree and optional structured trial priors.

The official initial-horse page is only queried for runners with no earlier local start.
Barrier-trial values are optional because the official trial archive is date-based rather
than a stable per-horse API. A caller can provide a previously collected structured trial
JSON; missing trial data remains neutral instead of being fabricated.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

from v102_feature_utils import normalize_horse_code

NEW_HORSE_URL = "https://racing.hkjc.com/racing/chinese/racing-info/newhorse.asp"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; HKJCV10.2Research/1.0; public-data-research)"}


def normalized_text(html: str) -> str:
    return re.sub(r"\s+", " ", BeautifulSoup(html, "html.parser").get_text(" ", strip=True))


def parse_new_horse_page(html: str) -> dict[str, str | None]:
    text = normalized_text(html)
    sire = None
    match = re.search(r"父系\s*[:：]\s*(.+?)(?:\s*毛色\s*[:：]|\s*出生年份\s*[:：]|\||母系\s*[:：])", text)
    if match:
        sire = match.group(1).strip()[:160]
    distance = None
    match = re.search(r"合適路程\s*[:：]\s*(.+?)(?:\s*父系特點\s*[:：]|\||母系簡介|$)", text)
    if match:
        distance = match.group(1).strip()[:240]
    return {"sire_name": sire, "suggested_distance_text": distance}


def load_trial_map(path: str | None) -> dict[str, dict[str, Any]]:
    if not path:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    source = payload.get("trials", payload) if isinstance(payload, dict) else {}
    return {str(key).upper(): value for key, value in source.items() if isinstance(value, dict)}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS horse_new_horse_priors (
            horse_code TEXT NOT NULL,
            horse_name TEXT,
            as_of_date TEXT NOT NULL,
            sire_name TEXT,
            suggested_distance_text TEXT,
            pedigree_source_url TEXT NOT NULL DEFAULT '',
            latest_trial_date TEXT,
            latest_trial_position REAL,
            latest_trial_margin_lengths REAL,
            latest_trial_qualified TEXT,
            trial_source_url TEXT NOT NULL DEFAULT '',
            fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (horse_code, as_of_date)
        );
        """
    )
    conn.commit()


def is_local_new_horse(conn: sqlite3.Connection, horse_name: str, race_date: str) -> bool:
    prior = conn.execute(
        "SELECT COUNT(*) FROM starters WHERE horse_name=? AND race_date<? AND finish_pos IS NOT NULL",
        (horse_name, race_date.replace("/", "-")),
    ).fetchone()[0]
    return int(prior) == 0


def main() -> int:
    parser = argparse.ArgumentParser(description="V10.2 新馬血統／可選試閘先驗回填器")
    parser.add_argument("--db", default="hkjc_last_season.sqlite")
    parser.add_argument("--race-card", required=True)
    parser.add_argument("--trial-json", help="可選結構化試閘資料：{horse_code:{date,position,margin_lengths,qualified,source_url}}")
    parser.add_argument("--delay-seconds", type=float, default=2.5)
    parser.add_argument("--report", default="new_horse_priors_report.json")
    args = parser.parse_args()
    if args.delay_seconds < 1.0:
        raise SystemExit("--delay-seconds 不可少於 1 秒。")

    card = json.loads(Path(args.race_card).read_text(encoding="utf-8"))
    race = card.get("race", {})
    race_date = str(race.get("race_date", ""))
    if not race_date:
        raise SystemExit("race_card 缺少 race.race_date。")
    trials = load_trial_map(args.trial_json)
    conn = sqlite3.connect(args.db)
    ensure_schema(conn)
    report: dict[str, Any] = {"race_date": race_date, "queried": 0, "stored": 0, "skipped_not_new": [], "warnings": []}

    for runner in card.get("runners", []):
        horse_name = str(runner.get("horse_name", "")).strip()
        code = normalize_horse_code(runner.get("horse_code"))
        if not horse_name or not code:
            report["warnings"].append(f"{horse_name or '未知馬匹'}：缺少官方烙號，無法取得新馬血統頁。")
            continue
        if not is_local_new_horse(conn, horse_name, race_date):
            report["skipped_not_new"].append(horse_name)
            continue
        report["queried"] += 1
        source_url = f"{NEW_HORSE_URL}?HorseNo={code}"
        values: dict[str, Any] = {"sire_name": None, "suggested_distance_text": None}
        try:
            time.sleep(args.delay_seconds)
            response = requests.get(NEW_HORSE_URL, params={"HorseNo": code}, headers=HEADERS, timeout=30)
            if response.status_code in {403, 429}:
                report["warnings"].append(f"{horse_name}：HKJC HTTP {response.status_code}，已停止後續官方請求。")
                break
            response.raise_for_status()
            values.update(parse_new_horse_page(response.text))
        except requests.RequestException as exc:
            report["warnings"].append(f"{horse_name}：血統頁暫不可用（{type(exc).__name__}）。")
        trial = trials.get(code, {})
        conn.execute(
            """
            INSERT OR REPLACE INTO horse_new_horse_priors(
              horse_code,horse_name,as_of_date,sire_name,suggested_distance_text,pedigree_source_url,
              latest_trial_date,latest_trial_position,latest_trial_margin_lengths,latest_trial_qualified,
              trial_source_url,fetched_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                code, horse_name, race_date.replace("/", "-"), values["sire_name"], values["suggested_distance_text"],
                source_url, trial.get("date"), trial.get("position"), trial.get("margin_lengths"),
                trial.get("qualified"), trial.get("source_url", ""), datetime.now().isoformat(timespec="seconds"),
            ),
        )
        report["stored"] += 1
    conn.commit()
    conn.close()
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
