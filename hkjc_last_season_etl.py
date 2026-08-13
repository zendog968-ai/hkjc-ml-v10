#!/usr/bin/env python3
"""HKJC 2025/26 local-racing ETL.

The collector reads only public HKJC pages. It is intentionally single-threaded,
rate-limited, resumable, and stops rather than attempting to bypass access controls.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import random
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional

import requests
from bs4 import BeautifulSoup, Tag

BASE = "https://racing.hkjc.com/racing/information/Chinese/Racing"
FIXTURE_URL = BASE + "/Fixture.aspx?CalYear={year}&CalMonth={month:02d}"
RESULTS_ALL_URL = BASE + "/ResultsAll.aspx?RaceDate={race_date}&Racecourse={racecourse}"
RESULT_URL = BASE + "/LocalResults.aspx?RaceDate={race_date}&Racecourse={racecourse}&RaceNo={race_no}"
DEFAULT_SEASON_START = date(2025, 9, 7)
DEFAULT_SEASON_END = date(2026, 7, 15)
USER_AGENT = "Mozilla/5.0 (compatible; HKJCV10Research/1.0; public-data-research)"

RACE_ALT = {"1", "2", "3", "4", "5", "4YO", "4R", "G1", "G2", "G3", "GRIFFIN"}
VENUE_ALT = {"ST", "HV"}

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meetings (
    race_date TEXT NOT NULL,
    racecourse TEXT NOT NULL,
    scheduled_races INTEGER NOT NULL,
    fixture_source_url TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    PRIMARY KEY (race_date, racecourse)
);

CREATE TABLE IF NOT EXISTS races (
    race_date TEXT NOT NULL,
    racecourse TEXT NOT NULL,
    race_no INTEGER NOT NULL,
    race_id TEXT NOT NULL UNIQUE,
    race_name TEXT,
    race_class TEXT,
    distance_m INTEGER,
    surface TEXT,
    course_config TEXT,
    going TEXT,
    official_time TEXT,
    source_url TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    race_status TEXT NOT NULL DEFAULT 'completed',
    PRIMARY KEY (race_date, racecourse, race_no),
    FOREIGN KEY (race_date, racecourse) REFERENCES meetings(race_date, racecourse)
);

CREATE TABLE IF NOT EXISTS starters (
    race_date TEXT NOT NULL,
    racecourse TEXT NOT NULL,
    race_no INTEGER NOT NULL,
    horse_no INTEGER,
    horse_name TEXT NOT NULL,
    horse_code TEXT,
    finish_pos_text TEXT,
    finish_pos INTEGER,
    jockey TEXT,
    trainer TEXT,
    weight_lbs REAL,
    declared_weight_kg REAL,
    draw INTEGER,
    margin_text TEXT,
    margin_lengths REAL,
    running_positions TEXT,
    finish_time TEXT,
    win_odds REAL,
    PRIMARY KEY (race_date, racecourse, race_no, horse_name),
    FOREIGN KEY (race_date, racecourse, race_no)
        REFERENCES races(race_date, racecourse, race_no)
);

CREATE INDEX IF NOT EXISTS idx_starters_horse ON starters(horse_name, race_date);
CREATE INDEX IF NOT EXISTS idx_starters_jockey ON starters(jockey, race_date);
CREATE INDEX IF NOT EXISTS idx_starters_trainer ON starters(trainer, race_date);
CREATE INDEX IF NOT EXISTS idx_starters_result ON starters(finish_pos, race_date);
CREATE INDEX IF NOT EXISTS idx_races_condition ON races(racecourse, surface, distance_m, race_date);

CREATE TABLE IF NOT EXISTS crawl_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at TEXT NOT NULL,
    url TEXT NOT NULL,
    status_code INTEGER,
    outcome TEXT NOT NULL,
    detail TEXT
);
"""


@dataclass(frozen=True)
class Meeting:
    race_date: str
    racecourse: str
    scheduled_races: int
    source_url: str


class RateLimitedClient:
    """Public-site client with conservative timing and stop-on-block behaviour."""

    def __init__(
        self,
        db: sqlite3.Connection,
        delay_min: float,
        delay_max: float,
        cooldown_every: int,
        cooldown_seconds: float,
        timeout: int = 35,
    ) -> None:
        self.db = db
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": USER_AGENT, "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8"}
        )
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.cooldown_every = cooldown_every
        self.cooldown_seconds = cooldown_seconds
        self.timeout = timeout
        self.request_count = 0
        self.last_request_at = 0.0

    def _pause(self) -> None:
        minimum_gap = random.uniform(self.delay_min, self.delay_max)
        elapsed = time.monotonic() - self.last_request_at
        if elapsed < minimum_gap:
            time.sleep(minimum_gap - elapsed)
        if self.request_count and self.request_count % self.cooldown_every == 0:
            logging.info("完成 %s 次請求，冷卻 %.1f 秒。", self.request_count, self.cooldown_seconds)
            time.sleep(self.cooldown_seconds)

    def _log(self, url: str, status: Optional[int], outcome: str, detail: str = "") -> None:
        self.db.execute(
            "INSERT INTO crawl_log(fetched_at,url,status_code,outcome,detail) VALUES(?,?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), url, status, outcome, detail[:1000]),
        )
        self.db.commit()

    def get(self, url: str) -> str:
        """Fetch one public page; retry temporary errors but never evade blocking."""
        for attempt in range(1, 4):
            self._pause()
            try:
                response = self.session.get(url, timeout=self.timeout)
                self.last_request_at = time.monotonic()
                self.request_count += 1
            except requests.RequestException as exc:
                wait = 10 * attempt
                self._log(url, None, "network_error", str(exc))
                logging.warning("網絡錯誤（第 %s/3 次）：%s；%.0f 秒後重試。", attempt, exc, wait)
                time.sleep(wait)
                continue

            if response.status_code == 200:
                self._log(url, 200, "ok")
                return response.text
            if response.status_code in (403, 429):
                self._log(url, response.status_code, "blocked_or_rate_limited", response.text[:250])
                raise RuntimeError(
                    f"HKJC 回傳 HTTP {response.status_code}。抓取器已停止以遵守網站限制；"
                    "請稍後再以 --resume 續跑。"
                )
            if response.status_code in (500, 502, 503, 504) and attempt < 3:
                wait = 15 * attempt
                self._log(url, response.status_code, "temporary_server_error", response.text[:250])
                logging.warning("伺服器暫時錯誤 HTTP %s；%.0f 秒後重試。", response.status_code, wait)
                time.sleep(wait)
                continue

            self._log(url, response.status_code, "http_error", response.text[:250])
            response.raise_for_status()
        raise RuntimeError(f"無法完成請求：{url}")


def init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    # 向後兼容：若資料庫在加入 race_status 前已建立，執行一次安全遷移。
    columns = {row[1] for row in conn.execute("PRAGMA table_info(races)")}
    if "race_status" not in columns:
        conn.execute("ALTER TABLE races ADD COLUMN race_status TEXT NOT NULL DEFAULT 'completed'")
    conn.commit()
    return conn


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def first_int(text: str) -> Optional[int]:
    match = re.search(r"-?\d+", text or "")
    return int(match.group()) if match else None


def first_float(text: str) -> Optional[float]:
    match = re.search(r"\d+(?:\.\d+)?", (text or "").replace(",", ""))
    return float(match.group()) if match else None


def parse_margin(text: str) -> Optional[float]:
    """Convert common HKJC margin notation to a comparable length approximation."""
    value = normalize_space(text)
    if value in {"", "---", "-", "0"}:
        return 0.0 if value in {"---", "0"} else None
    named = {"鼻": 0.05, "短頭": 0.1, "頭": 0.2, "頸": 0.3, "短頸": 0.25}
    if value in named:
        return named[value]
    if re.fullmatch(r"\d+/\d+", value):
        n, d = value.split("/")
        return int(n) / int(d)
    mixed = re.fullmatch(r"(\d+)-(\d+)/(\d+)", value)
    if mixed:
        whole, n, d = mixed.groups()
        return int(whole) + int(n) / int(d)
    return first_float(value)


def chinese_months(start_date: date, end_date: date) -> Iterable[tuple[int, int]]:
    year, month = start_date.year, start_date.month
    while (year, month) <= (end_date.year, end_date.month):
        yield year, month
        month += 1
        if month == 13:
            year, month = year + 1, 1


def calendar_day(cell: Tag) -> Optional[int]:
    candidate = cell.select_one(".f_fl.f_fs14")
    if candidate:
        return first_int(candidate.get_text(" ", strip=True))
    first_p = cell.find("p")
    if first_p:
        return first_int(first_p.get_text(" ", strip=True))
    return None


def scheduled_race_count(cell: Tag) -> int:
    return sum(
        1
        for image in cell.find_all("img", alt=True)
        if normalize_space(image.get("alt", "")).upper() in RACE_ALT
    )


def parse_fixture_month(html: str, year: int, month: int, source_url: str, start_date: date, end_date: date) -> list[Meeting]:
    soup = BeautifulSoup(html, "html.parser")
    meetings: list[Meeting] = []
    for cell in soup.select("td.calendar"):
        day = calendar_day(cell)
        alts = {normalize_space(img.get("alt", "")).upper() for img in cell.find_all("img", alt=True)}
        venues = alts & VENUE_ALT
        race_count = scheduled_race_count(cell)
        if day is None or len(venues) != 1 or race_count == 0:
            continue
        try:
            meeting_date = date(year, month, day)
        except ValueError:
            continue
        if start_date <= meeting_date <= end_date:
            meetings.append(
                Meeting(meeting_date.isoformat(), next(iter(venues)), race_count, source_url)
            )
    return meetings


def discover_meetings(client: RateLimitedClient, db: sqlite3.Connection, start_date: date, end_date: date) -> list[Meeting]:
    found: dict[tuple[str, str], Meeting] = {}
    for year, month in chinese_months(start_date, end_date):
        url = FIXTURE_URL.format(year=year, month=month)
        html = client.get(url)
        meetings = parse_fixture_month(html, year, month, url, start_date, end_date)
        logging.info("賽期表 %04d-%02d：發現 %s 個賽日。", year, month, len(meetings))
        for meeting in meetings:
            found[(meeting.race_date, meeting.racecourse)] = meeting
            db.execute(
                """
                INSERT INTO meetings(race_date,racecourse,scheduled_races,fixture_source_url,discovered_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(race_date,racecourse) DO UPDATE SET
                  scheduled_races=excluded.scheduled_races,
                  fixture_source_url=excluded.fixture_source_url,
                  discovered_at=excluded.discovered_at
                """,
                (
                    meeting.race_date,
                    meeting.racecourse,
                    meeting.scheduled_races,
                    meeting.source_url,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
        db.commit()
    meetings = sorted(found.values(), key=lambda m: (m.race_date, m.racecourse))
    if not meetings:
        logging.warning("指定日期區間未解析到賽日；請核實賽期表或日期範圍。")
    return meetings


def next_value_for_label(soup: BeautifulSoup, label: str) -> str:
    """Read the next table cell after a Chinese metadata label."""
    for td in soup.find_all("td"):
        if normalize_space(td.get_text(" ", strip=True)).replace(" ", "") == label.replace(" ", ""):
            sibling = td.find_next_sibling("td")
            if sibling:
                return normalize_space(sibling.get_text(" ", strip=True))
    return ""


def parse_race_metadata(soup: BeautifulSoup) -> dict[str, Optional[str]]:
    all_text = normalize_space(soup.get_text(" ", strip=True))
    class_distance = re.search(
        r"((?:第一|第二|第三|第四|第五)班|新馬賽|一班|二班|三班|四班|五班)[^\-]{0,12}-\s*(\d+)米",
        all_text,
    )
    race_class = normalize_space(class_distance.group(1)) if class_distance else None
    distance = int(class_distance.group(2)) if class_distance else None
    going = next_value_for_label(soup, "場地狀況:") or next_value_for_label(soup, "場地狀況")
    track = next_value_for_label(soup, "賽道:") or next_value_for_label(soup, "賽道")
    official_time = next_value_for_label(soup, "時間:") or next_value_for_label(soup, "時間")
    race_name = ""
    if class_distance:
        start = class_distance.start()
        before = all_text[max(0, start - 120):start]
        fragments = re.split(r"(?:場地狀況|賽道|時間|分段時間)", before)
        race_name = normalize_space(fragments[-1])
    surface = None
    course_config = None
    if track:
        if "全天候" in track:
            surface = "全天候"
        elif "草地" in track:
            surface = "草地"
        quote = re.search(r'"([A-Z][+0-9]*)"', track)
        if quote:
            course_config = quote.group(1)
    return {
        "race_name": race_name or None,
        "race_class": race_class,
        "distance_m": distance,
        "surface": surface,
        "course_config": course_config,
        "going": going or None,
        "official_time": official_time or None,
    }


def find_result_table(soup: BeautifulSoup) -> Optional[Tag]:
    required = {"名次", "馬號", "馬名", "騎師", "練馬師", "檔位", "獨贏"}
    for table in soup.find_all("table"):
        header_rows = table.find_all("tr")[:2]
        header_text = " ".join(
            cell.get_text(" ", strip=True) for row in header_rows for cell in row.find_all("td")
        )
        if all(token in header_text for token in required):
            return table
    return None


def parse_horse_name_and_code(value: str) -> tuple[str, Optional[str]]:
    clean = normalize_space(value)
    match = re.match(r"^(.*?)\s*\(([A-Z]\d+)\)\s*$", clean)
    return (normalize_space(match.group(1)), match.group(2)) if match else (clean, None)


def parse_starters(soup: BeautifulSoup) -> list[dict[str, object]]:
    table = find_result_table(soup)
    if table is None:
        return []
    starters: list[dict[str, object]] = []
    for row in table.find_all("tr"):
        cells = [normalize_space(cell.get_text(" ", strip=True)) for cell in row.find_all("td", recursive=False)]
        if len(cells) < 10 or cells[0] == "名次":
            continue
        horse_name, horse_code = parse_horse_name_and_code(cells[2])
        # 「8 平頭馬」等官方同名次紀錄仍屬正式名次；撤回或未完成則保留為空值。
        finish_pos = first_int(cells[0]) if (cells[0].isdigit() or "平頭馬" in cells[0]) else None
        starters.append(
            {
                "horse_no": first_int(cells[1]),
                "horse_name": horse_name,
                "horse_code": horse_code,
                "finish_pos_text": cells[0],
                "finish_pos": finish_pos,
                "jockey": cells[3],
                "trainer": cells[4],
                "weight_lbs": first_float(cells[5]),
                "declared_weight_kg": first_float(cells[6]),
                "draw": first_int(cells[7]),
                "margin_text": cells[8],
                "margin_lengths": parse_margin(cells[8]),
                "running_positions": cells[9] if len(cells) > 9 else None,
                "finish_time": cells[10] if len(cells) > 10 else None,
                "win_odds": first_float(cells[11]) if len(cells) > 11 else None,
            }
        )
    return starters


def parse_actual_race_numbers(html: str) -> list[int]:
    """Extract official race numbers shown on a ResultsAll page for a meeting."""
    text = normalize_space(BeautifulSoup(html, "html.parser").get_text(" ", strip=True))
    race_nos = {int(value) for value in re.findall(r"第\s*(\d{1,2})\s*場", text)}
    return sorted(number for number in race_nos if 1 <= number <= 14)


def race_exists(db: sqlite3.Connection, meeting: Meeting, race_no: int) -> bool:
    return bool(
        db.execute(
            "SELECT 1 FROM races WHERE race_date=? AND racecourse=? AND race_no=?",
            (meeting.race_date, meeting.racecourse, race_no),
        ).fetchone()
    )


def store_race(
    db: sqlite3.Connection,
    meeting: Meeting,
    race_no: int,
    metadata: dict[str, Optional[str]],
    starters: list[dict[str, object]],
    source_url: str,
    race_status: str = "completed",
) -> None:
    if not starters and race_status not in {"cancelled", "void"}:
        raise ValueError("沒有解析到有效的出賽馬匹資料列")
    db.execute("BEGIN")
    try:
        db.execute(
            """
            INSERT OR REPLACE INTO races(
                race_date,racecourse,race_no,race_id,race_name,race_class,distance_m,
                surface,course_config,going,official_time,source_url,fetched_at,race_status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                meeting.race_date,
                meeting.racecourse,
                race_no,
                f"{meeting.race_date}_{meeting.racecourse}_{race_no}",
                metadata["race_name"],
                metadata["race_class"],
                metadata["distance_m"],
                metadata["surface"],
                metadata["course_config"],
                metadata["going"],
                metadata["official_time"],
                source_url,
                datetime.now().isoformat(timespec="seconds"),
                race_status,
            ),
        )
        db.execute(
            "DELETE FROM starters WHERE race_date=? AND racecourse=? AND race_no=?",
            (meeting.race_date, meeting.racecourse, race_no),
        )
        db.executemany(
            """
            INSERT INTO starters(
                race_date,racecourse,race_no,horse_no,horse_name,horse_code,finish_pos_text,
                finish_pos,jockey,trainer,weight_lbs,declared_weight_kg,draw,margin_text,
                margin_lengths,running_positions,finish_time,win_odds
            ) VALUES(
                :race_date,:racecourse,:race_no,:horse_no,:horse_name,:horse_code,:finish_pos_text,
                :finish_pos,:jockey,:trainer,:weight_lbs,:declared_weight_kg,:draw,:margin_text,
                :margin_lengths,:running_positions,:finish_time,:win_odds
            )
            """,
            [
                {
                    **starter,
                    "race_date": meeting.race_date,
                    "racecourse": meeting.racecourse,
                    "race_no": race_no,
                }
                for starter in starters
            ],
        )
        db.commit()
    except Exception:
        db.rollback()
        raise


def crawl_meetings(
    client: RateLimitedClient,
    db: sqlite3.Connection,
    meetings: list[Meeting],
    max_meetings: Optional[int],
    force: bool,
) -> None:
    targets = meetings[:max_meetings] if max_meetings else meetings
    completed = skipped = cancelled = 0
    logging.info("準備抓取 %s 個賽日；每個賽日先以官方全場賽果頁核實實際場次。", len(targets))
    for meeting_index, meeting in enumerate(targets, start=1):
        race_date_slash = datetime.strptime(meeting.race_date, "%Y-%m-%d").strftime("%Y/%m/%d")
        day_url = RESULTS_ALL_URL.format(race_date=race_date_slash, racecourse=meeting.racecourse)
        day_html = client.get(day_url)
        actual_race_nos = parse_actual_race_numbers(day_html)
        if not actual_race_nos:
            raise ValueError(f"未能由官方全場賽果頁核實 {meeting.race_date} {meeting.racecourse} 的場次。")
        db.execute(
            "UPDATE meetings SET scheduled_races=? WHERE race_date=? AND racecourse=?",
            (len(actual_race_nos), meeting.race_date, meeting.racecourse),
        )
        db.commit()
        logging.info(
            "[%s/%s] %s %s：官方核實 %s 場。",
            meeting_index,
            len(targets),
            meeting.race_date,
            meeting.racecourse,
            len(actual_race_nos),
        )
        for race_no in actual_race_nos:
            if not force and race_exists(db, meeting, race_no):
                skipped += 1
                continue
            url = RESULT_URL.format(
                race_date=race_date_slash,
                racecourse=meeting.racecourse,
                race_no=race_no,
            )
            html = client.get(url)
            soup = BeautifulSoup(html, "html.parser")
            starters = parse_starters(soup)
            page_text = soup.get_text(" ", strip=True)
            is_cancelled = "此場賽事宣佈取消" in page_text
            is_void = "此場賽事宣佈無效" in page_text
            if not starters and not (is_cancelled or is_void):
                client._log(url, 200, "parse_error", "No starters parsed and page is not marked cancelled or void")
                raise ValueError(f"未能解析 {meeting.race_date} {meeting.racecourse} 第 {race_no} 場；已停止保護資料品質。")
            metadata = parse_race_metadata(soup)
            race_status = "cancelled" if is_cancelled else "void" if is_void else "completed"
            store_race(db, meeting, race_no, metadata, starters, url, race_status)
            completed += 1
            cancelled += int(is_cancelled or is_void)
    logging.info("抓取結束：新增／更新 %s 場（其中取消／無效 %s 場）；略過已存在 %s 場。", completed, cancelled, skipped)


def export_csv(db: sqlite3.Connection, csv_path: Path) -> int:
    query = """
    SELECT
      s.race_date, s.racecourse, s.race_no, r.race_id, r.race_name, r.race_class,
      r.distance_m, r.surface, r.course_config, r.going, r.official_time AS race_official_time,
      s.horse_no, s.horse_name, s.horse_code, s.finish_pos_text, s.finish_pos,
      s.jockey, s.trainer, s.weight_lbs, s.declared_weight_kg, s.draw,
      s.margin_text, s.margin_lengths, s.running_positions, s.finish_time, s.win_odds,
      r.race_status, r.source_url
    FROM starters AS s
    JOIN races AS r
      ON r.race_date=s.race_date AND r.racecourse=s.racecourse AND r.race_no=s.race_no
    ORDER BY s.race_date, s.racecourse, s.race_no, COALESCE(s.finish_pos, 999), s.horse_no
    """
    cursor = db.execute(query)
    columns = [column[0] for column in cursor.description]
    rows = cursor.fetchall()
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows(rows)
    return len(rows)


def print_summary(db: sqlite3.Connection) -> None:
    summary = db.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM meetings) AS meetings,
          (SELECT COUNT(*) FROM races) AS races,
          (SELECT COUNT(*) FROM starters) AS starters,
          (SELECT COUNT(*) FROM races WHERE race_status IN ('cancelled','void')) AS cancelled_or_void_races,
          (SELECT MIN(race_date) FROM races) AS first_date,
          (SELECT MAX(race_date) FROM races) AS last_date
        """
    ).fetchone()
    print(
        json.dumps(
            dict(zip(["meetings", "races", "starters", "cancelled_or_void_races", "first_date", "last_date"], summary)),
            ensure_ascii=False,
            indent=2,
        )
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HKJC 2025/26 上一馬季賽果資料庫建置器")
    parser.add_argument("--db", default="hkjc_last_season.sqlite", help="SQLite 輸出位置")
    parser.add_argument("--start-date", default=DEFAULT_SEASON_START.isoformat(), help="抓取起始日期 YYYY-MM-DD")
    parser.add_argument("--end-date", default=DEFAULT_SEASON_END.isoformat(), help="抓取結束日期 YYYY-MM-DD")
    parser.add_argument("--csv", default="hkjc_last_season.csv", help="CSV 輸出位置")
    parser.add_argument("--discover-only", action="store_true", help="只下載賽期表並列出賽日")
    parser.add_argument("--max-meetings", type=int, help="只處理前 N 個賽日（供測試）")
    parser.add_argument("--force", action="store_true", help="重抓已存在的賽事")
    parser.add_argument("--delay-min", type=float, default=1.5, help="兩個請求間的最小秒數")
    parser.add_argument("--delay-max", type=float, default=2.3, help="兩個請求間的最大秒數")
    parser.add_argument("--cooldown-every", type=int, default=20, help="每 N 次請求作一次較長冷卻")
    parser.add_argument("--cooldown-seconds", type=float, default=20.0, help="較長冷卻的秒數")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.delay_min <= 0 or args.delay_max < args.delay_min:
        raise SystemExit("延遲參數無效。")
    try:
        start_date = date.fromisoformat(args.start_date)
        end_date = date.fromisoformat(args.end_date)
    except ValueError as exc:
        raise SystemExit("日期格式須為 YYYY-MM-DD。") from exc
    if end_date < start_date:
        raise SystemExit("結束日期不可早於起始日期。")
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)s %(message)s")
    db_path = Path(args.db)
    csv_path = Path(args.csv)
    db = init_db(db_path)
    client = RateLimitedClient(
        db,
        args.delay_min,
        args.delay_max,
        args.cooldown_every,
        args.cooldown_seconds,
    )
    try:
        meetings = discover_meetings(client, db, start_date, end_date)
        if args.discover_only:
            print("race_date,racecourse,scheduled_races")
            for meeting in meetings:
                print(f"{meeting.race_date},{meeting.racecourse},{meeting.scheduled_races}")
            return 0
        crawl_meetings(client, db, meetings, args.max_meetings, args.force)
        record_count = export_csv(db, csv_path)
        logging.info("已輸出 CSV：%s（%s 行馬匹出賽紀錄）。", csv_path, record_count)
        print_summary(db)
        return 0
    except Exception as exc:
        logging.error("工作停止：%s", exc)
        print_summary(db)
        return 2
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
