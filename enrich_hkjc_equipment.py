#!/usr/bin/env python3
"""Backfill HKJC official per-run equipment into an existing V10.1 database.

Historical LocalResults pages do not expose the equipment column used by the model.
This script reads the public HKJC horse-performance page once per horse code, parses its
per-run equipment column, and matches official race links to the existing `starters` rows.
It is deliberately sequential, rate-limited, resumable, and stops on 403/429.
"""
from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup, Tag

HORSE_URL = "https://racing.hkjc.com/zh-hk/local/information/horse?horseno={horse_code}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; HKJCV10Equipment/1.0; public-data-research)",
    "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8",
}


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\xa0", " ")).strip()


def init_schema(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(starters)")}
    if "equipment" not in columns:
        conn.execute("ALTER TABLE starters ADD COLUMN equipment TEXT")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS starter_equipment (
            race_date TEXT NOT NULL,
            racecourse TEXT NOT NULL,
            race_no INTEGER NOT NULL,
            horse_name TEXT NOT NULL,
            horse_code TEXT,
            equipment_raw TEXT,
            source_url TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (race_date, racecourse, race_no, horse_name)
        );
        CREATE INDEX IF NOT EXISTS idx_starter_equipment_horse ON starter_equipment(horse_code, race_date);
        CREATE TABLE IF NOT EXISTS equipment_profile_log (
            horse_code TEXT PRIMARY KEY,
            source_url TEXT NOT NULL,
            status TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            detail TEXT
        );
        """
    )
    conn.commit()


def horse_codes(conn: sqlite3.Connection, force: bool) -> list[str]:
    where = "" if force else "WHERE l.horse_code IS NULL OR l.status <> 'ok'"
    return [
        row[0]
        for row in conn.execute(
            f"""
            SELECT DISTINCT s.horse_code
            FROM starters AS s
            LEFT JOIN equipment_profile_log AS l ON l.horse_code=s.horse_code
            {where}
            ORDER BY s.horse_code
            """
        )
        if row[0]
    ]


def find_form_table(soup: BeautifulSoup) -> tuple[Tag | None, dict[str, int]]:
    for table in soup.find_all("table"):
        for row in table.find_all("tr")[:4]:
            cells = [normalize(cell.get_text(" ", strip=True)) for cell in row.find_all(["td", "th"], recursive=False)]
            if "日期" in " ".join(cells) and "配備" in " ".join(cells) and "場次" in " ".join(cells):
                mapping: dict[str, int] = {}
                for index, label in enumerate(cells):
                    if "日期" in label:
                        mapping["date"] = index
                    elif "配備" in label:
                        mapping["equipment"] = index
                    elif "場次" in label:
                        mapping["race"] = index
                if {"date", "equipment", "race"}.issubset(mapping):
                    return table, mapping
    return None, {}


def parse_href(href: str) -> tuple[str, str, int] | None:
    parsed = urlparse(href)
    query = parse_qs(parsed.query)
    date_raw = (query.get("racedate") or query.get("RaceDate") or [""])[0]
    course = ((query.get("Racecourse") or query.get("racecourse") or [""])[0]).upper()
    no_raw = (query.get("RaceNo") or query.get("raceno") or [""])[0]
    try:
        date = datetime.strptime(date_raw.replace("/", "-"), "%Y-%m-%d").date().isoformat()
        race_no = int(no_raw)
    except ValueError:
        return None
    return (date, course, race_no) if course in {"ST", "HV"} else None


def parse_profile(html: str) -> list[tuple[str, str, int, str]]:
    soup = BeautifulSoup(html, "html.parser")
    table, mapping = find_form_table(soup)
    if table is None:
        return []
    records: list[tuple[str, str, int, str]] = []
    for row in table.find_all("tr"):
        cells = row.find_all("td", recursive=False)
        text_cells = [normalize(cell.get_text(" ", strip=True)) for cell in cells]
        if len(text_cells) <= max(mapping.values()) or text_cells[mapping["date"]] == "日期":
            continue
        link = cells[mapping["race"]].find("a", href=True) if len(cells) > mapping["race"] else None
        key = parse_href(link["href"]) if link else None
        if key is None:
            continue
        equipment = text_cells[mapping["equipment"]] or "--"
        records.append((*key, equipment))
    return records


def fetch(session: requests.Session, code: str, timeout: int) -> tuple[str, str]:
    url = HORSE_URL.format(horse_code=code)
    response = session.get(url, timeout=timeout)
    if response.status_code in {403, 429}:
        raise RuntimeError(f"HKJC 回傳 HTTP {response.status_code}；已停止以遵守網站限制。")
    response.raise_for_status()
    return url, response.text


def update_equipment(conn: sqlite3.Connection, code: str, url: str, records: Iterable[tuple[str, str, int, str]]) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    updated = 0
    conn.execute("BEGIN")
    try:
        for race_date, course, race_no, equipment in records:
            matching = conn.execute(
                """SELECT horse_name FROM starters
                   WHERE race_date=? AND racecourse=? AND race_no=? AND horse_code=?""",
                (race_date, course, race_no, code),
            ).fetchall()
            for (horse_name,) in matching:
                conn.execute(
                    """INSERT INTO starter_equipment(race_date,racecourse,race_no,horse_name,horse_code,equipment_raw,source_url,fetched_at)
                       VALUES(?,?,?,?,?,?,?,?)
                       ON CONFLICT(race_date,racecourse,race_no,horse_name) DO UPDATE SET
                         horse_code=excluded.horse_code, equipment_raw=excluded.equipment_raw,
                         source_url=excluded.source_url, fetched_at=excluded.fetched_at""",
                    (race_date, course, race_no, horse_name, code, equipment, url, now),
                )
                conn.execute(
                    """UPDATE starters SET equipment=?
                       WHERE race_date=? AND racecourse=? AND race_no=? AND horse_name=?""",
                    (equipment, race_date, course, race_no, horse_name),
                )
                updated += 1
        conn.execute(
            """INSERT INTO equipment_profile_log(horse_code,source_url,status,fetched_at,detail)
               VALUES(?,?,?,?,?)
               ON CONFLICT(horse_code) DO UPDATE SET source_url=excluded.source_url,status=excluded.status,
                 fetched_at=excluded.fetched_at,detail=excluded.detail""",
            (code, url, "ok", now, f"matched_runs={updated}"),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return updated


def main() -> int:
    parser = argparse.ArgumentParser(description="限速回填 HKJC 官方馬匹逐場配備資料")
    parser.add_argument("--db", default="hkjc_last_season.sqlite")
    parser.add_argument("--delay-seconds", type=float, default=2.5)
    parser.add_argument("--timeout", type=int, default=35)
    parser.add_argument("--max-horses", type=int, help="只處理前 N 匹，供測試或分批續跑")
    parser.add_argument("--force", action="store_true", help="重新抓取已有成功記錄的馬匹")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)s %(message)s")
    if args.delay_seconds < 1.5:
        raise SystemExit("為尊重官方網站，--delay-seconds 不可小於 1.5。")
    conn = sqlite3.connect(args.db)
    init_schema(conn)
    codes = horse_codes(conn, args.force)
    if args.max_horses:
        codes = codes[:args.max_horses]
    session = requests.Session()
    session.headers.update(HEADERS)
    updated = 0
    try:
        for index, code in enumerate(codes, start=1):
            if index > 1:
                time.sleep(args.delay_seconds)
            try:
                url, html = fetch(session, code, args.timeout)
                records = parse_profile(html)
                updated += update_equipment(conn, code, url, records)
                logging.info("[%s/%s] %s：官方往績 %s 筆，資料庫配備更新 %s 筆。", index, len(codes), code, len(records), updated)
            except RuntimeError:
                raise
            except requests.RequestException as exc:
                now = datetime.now().isoformat(timespec="seconds")
                conn.execute(
                    """INSERT INTO equipment_profile_log(horse_code,source_url,status,fetched_at,detail)
                       VALUES(?,?,?,?,?) ON CONFLICT(horse_code) DO UPDATE SET status=excluded.status,fetched_at=excluded.fetched_at,detail=excluded.detail""",
                    (code, HORSE_URL.format(horse_code=code), "network_error", now, str(exc)[:500]),
                )
                conn.commit()
                logging.warning("%s 網絡錯誤：%s；保留續跑標記。", code, exc)
        print({"horse_profiles_processed": len(codes), "starter_equipment_updates": updated})
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
