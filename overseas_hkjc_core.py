"""Shared HKJC overseas simulcast acquisition primitives for V10.2.

The module accesses only public HKJC pages, is deliberately single threaded and
rate-limited, and records every source outcome.  It does not infer missing races,
prices, result fields, or fixture coverage from third-party sources.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

USER_AGENT = "Mozilla/5.0 (compatible; HKJCV10OverseasResearch/1.0; public-data-research)"
FIXTURE_URL = "https://racing.hkjc.com/en-us/overseas/simulcast_fixture?y={season_code}"
SUMMARY_URL = "https://racing.hkjc.com/en-us/overseas/race-summary?RaceDate={compact_date}&Racecourse={code}&redirect=Y"
RESULT_URL = "https://racing.hkjc.com/racing/overseas/english/results.aspx?para=/{compact_date}/{code}/{race_no}"
RACECARD_URL = "https://racing.hkjc.com/en-us/overseas/race-summary?RaceDate={compact_date}&Racecourse={code}&redirect=Y&focus=Y"
PARSER_VERSION = "v10.2-overseas-1"


@dataclass(frozen=True)
class OverseasMeeting:
    meeting_date: str
    simulcast_code: str
    meeting_name: str | None
    location: str | None
    fixture_url: str
    summary_url: str
    seed_race_no: int | None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_space(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\xa0", " ")).strip()


def first_int(value: str | None) -> int | None:
    match = re.search(r"-?\d+", value or "")
    return int(match.group()) if match else None


def first_float(value: str | None) -> float | None:
    match = re.search(r"-?\d+(?:\.\d+)?", (value or "").replace(",", ""))
    return float(match.group()) if match else None


def parse_date_ddmmyyyy(value: str) -> str | None:
    try:
        return datetime.strptime(normalize_space(value), "%d/%m/%Y").date().isoformat()
    except ValueError:
        return None


def parse_date_yyyymmdd(value: str) -> str | None:
    try:
        return datetime.strptime(value, "%Y%m%d").date().isoformat()
    except ValueError:
        return None


def compact_date(iso_date: str) -> str:
    return iso_date.replace("-", "")


def parse_margin(value: str | None) -> float | None:
    text = normalize_space(value)
    if text in {"", "-", "--", "---"}:
        return None
    if text in {"0", "0.0"}:
        return 0.0
    named = {"Nose": 0.05, "Short Head": 0.1, "Head": 0.2, "Neck": 0.3, "Short Neck": 0.25}
    if text.title() in named:
        return named[text.title()]
    if re.fullmatch(r"\d+/\d+", text):
        n, d = text.split("/")
        return int(n) / int(d)
    return first_float(text)


def safe_filename(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:20] + ".html"


class OfficialOverseasClient:
    def __init__(
        self,
        conn: sqlite3.Connection,
        raw_dir: Path,
        delay_min: float = 3.0,
        delay_max: float = 6.0,
        cooldown_every: int = 20,
        cooldown_seconds: float = 60.0,
        timeout: int = 40,
    ) -> None:
        self.conn = conn
        self.raw_dir = raw_dir
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8"})
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.cooldown_every = max(cooldown_every, 1)
        self.cooldown_seconds = cooldown_seconds
        self.timeout = timeout
        self.request_count = 0
        self.last_request_at = 0.0

    def _wait(self) -> None:
        gap = random.uniform(self.delay_min, self.delay_max)
        elapsed = time.monotonic() - self.last_request_at
        if elapsed < gap:
            time.sleep(gap - elapsed)
        if self.request_count and self.request_count % self.cooldown_every == 0:
            time.sleep(self.cooldown_seconds)

    def _record(self, url: str, kind: str, status: int | None, outcome: str, body: str | None, detail: str = "") -> int:
        body_path = None
        digest = None
        if body is not None:
            digest = hashlib.sha256(body.encode("utf-8", errors="ignore")).hexdigest()
            path = self.raw_dir / safe_filename(url)
            path.write_text(body, encoding="utf-8")
            body_path = str(path)
        self.conn.execute(
            """INSERT INTO overseas_source_documents(source_url,source_kind,http_status,fetched_at_utc,content_sha256,body_path,parser_version,outcome,detail)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source_url) DO UPDATE SET http_status=excluded.http_status,fetched_at_utc=excluded.fetched_at_utc,
               content_sha256=excluded.content_sha256,body_path=excluded.body_path,parser_version=excluded.parser_version,outcome=excluded.outcome,detail=excluded.detail""",
            (url, kind, status, utc_now(), digest, body_path, PARSER_VERSION, outcome, detail[:1500]),
        )
        row = self.conn.execute("SELECT document_id FROM overseas_source_documents WHERE source_url=?", (url,)).fetchone()
        self.conn.commit()
        return int(row[0])

    def get_rendered(self, url: str, kind: str, wait_mode: str = "table") -> tuple[str, int]:
        """Read an official React page after its data table has rendered.

        This is used only where the public HKJC endpoint serves an empty loading
        shell to ordinary HTTP clients.  The same request pacing and stop-on-error
        policy applies; no login, CAPTCHA bypass, or parallel browser sessions are used.
        """
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("缺少 Playwright，無法讀取官方動態賽期／賽事頁。") from exc
        self._wait()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path="/usr/bin/chromium", headless=True, args=["--no-sandbox"])
                try:
                    page = browser.new_page(locale="en-US", user_agent=USER_AGENT)
                    page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                    if wait_mode == "fixture":
                        page.wait_for_function("""() => Array.from(document.querySelectorAll('table tr')).some(
                            row => /\\d{2}\\/\\d{2}\\/\\d{4}/.test(row.innerText) && row.querySelector('a')
                        ) || document.body.innerText.includes('No Match Found')""", timeout=self.timeout * 1000)
                    elif wait_mode == "summary":
                        page.wait_for_function("""() => document.body.innerText.includes('Race Summary') &&
                            (document.body.innerText.includes('Race Card') || document.body.innerText.includes('Results'))""", timeout=self.timeout * 1000)
                    elif wait_mode == "result":
                        page.wait_for_function("""() => Array.from(document.querySelectorAll('table tr')).some(
                            row => /Dividend|Pool/.test(row.innerText)
                        ) || document.body.innerText.includes('No Match Found')""", timeout=self.timeout * 1000)
                    else:
                        page.wait_for_timeout(1500)
                    body = page.content()
                finally:
                    browser.close()
            self.last_request_at = time.monotonic()
            self.request_count += 1
            document_id = self._record(url, kind, 200, "ok", body, "rendered_public_page")
            return body, document_id
        except PlaywrightTimeoutError as exc:
            self.last_request_at = time.monotonic()
            self.request_count += 1
            self._record(url, kind, None, "parse_error", None, "dynamic_page_timeout")
            raise RuntimeError(f"官方動態頁未在時限內呈現可辨識資料：{url}") from exc
        except Exception as exc:
            self.last_request_at = time.monotonic()
            self.request_count += 1
            self._record(url, kind, None, "network_error", None, type(exc).__name__)
            raise RuntimeError(f"官方動態頁讀取失敗：{url}") from exc

    def get(self, url: str, kind: str) -> tuple[str, int]:
        for attempt in range(1, 4):
            self._wait()
            try:
                response = self.session.get(url, timeout=self.timeout)
                self.last_request_at = time.monotonic()
                self.request_count += 1
            except requests.RequestException as exc:
                self._record(url, kind, None, "network_error", None, str(exc))
                if attempt == 3:
                    raise RuntimeError(f"官方頁網絡錯誤：{url}") from exc
                time.sleep(10 * attempt)
                continue
            if response.status_code == 200:
                document_id = self._record(url, kind, 200, "ok", response.text)
                return response.text, document_id
            if response.status_code in {403, 429}:
                self._record(url, kind, response.status_code, "rate_limited", response.text, "stop_on_block")
                raise RuntimeError(f"HKJC 回傳 {response.status_code}；已停止，請稍後以 --resume 續跑。")
            if response.status_code in {500, 502, 503, 504} and attempt < 3:
                self._record(url, kind, response.status_code, "http_error", response.text, "temporary")
                time.sleep(15 * attempt)
                continue
            self._record(url, kind, response.status_code, "http_error", response.text)
            raise RuntimeError(f"官方頁 HTTP {response.status_code}：{url}")
        raise RuntimeError(f"無法讀取官方頁：{url}")


def init_overseas_db(db_path: Path, schema_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(schema_path.read_text(encoding="utf-8"))
    conn.commit()
    return conn


def meeting_from_link(href: str, label: str, location: str, fixture_url: str) -> OverseasMeeting | None:
    parsed = urlparse(href)
    query = parse_qs(parsed.query)
    compact = query.get("RaceDate", [None])[0]
    code = query.get("Racecourse", [None])[0]
    race_no = None
    para = query.get("para", [""])[0]
    match = re.search(r"/(\d{8})/(S\d+)/(\d+)", para, re.I)
    if match:
        compact, code, race = match.groups()
        race_no = int(race)
    else:
        match = re.search(r"/(\d{8})/(S\d+)/(\d+)/", parsed.path, re.I)
        if match:
            compact, code, race = match.groups()
            race_no = int(race)
    date_iso = parse_date_yyyymmdd(str(compact or ""))
    code = str(code or "").upper()
    if not date_iso or not re.fullmatch(r"S\d+", code):
        return None
    return OverseasMeeting(
        meeting_date=date_iso,
        simulcast_code=code,
        meeting_name=normalize_space(label) or None,
        location=normalize_space(location) or None,
        fixture_url=fixture_url,
        # Preserve the official fixture anchor.  Older seasons use a legacy page
        # path while newer seasons use race-summary; rewriting either loses source
        # compatibility and can turn a valid discovered meeting into a false gap.
        summary_url=href,
        seed_race_no=race_no,
    )


def parse_fixture(html: str, fixture_url: str) -> list[OverseasMeeting]:
    soup = BeautifulSoup(html, "html.parser")
    records: dict[tuple[str, str], OverseasMeeting] = {}
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        header = [normalize_space(cell.get_text(" ", strip=True)).lower() for cell in rows[0].find_all(["th", "td"])]
        if not any(value == "date" for value in header) or not any("location" in value for value in header):
            continue
        for row in rows[1:]:
            cells = row.find_all("td")
            if len(cells) < 3:
                continue
            date_iso = parse_date_ddmmyyyy(cells[0].get_text(" ", strip=True))
            anchor = row.find("a", href=True)
            if not date_iso or not anchor:
                continue
            meeting = meeting_from_link(urljoin(fixture_url, anchor["href"]), anchor.get_text(" ", strip=True), cells[2].get_text(" ", strip=True), fixture_url)
            if meeting is None or meeting.meeting_date != date_iso:
                continue
            key = (meeting.meeting_date, meeting.simulcast_code)
            existing = records.get(key)
            if existing is None or (meeting.seed_race_no or 0) > (existing.seed_race_no or 0):
                records[key] = meeting
    return sorted(records.values(), key=lambda item: (item.meeting_date, item.simulcast_code))


def extract_race_numbers(html: str, code: str) -> list[int]:
    soup = BeautifulSoup(html, "html.parser")
    numbers: set[int] = set()
    code_pattern = re.compile(rf"{re.escape(code)}[-\s]?(\d+)", re.I)
    for anchor in soup.find_all("a", href=True):
        text = normalize_space(anchor.get_text(" ", strip=True))
        match = code_pattern.search(text)
        if match:
            numbers.add(int(match.group(1)))
        href = anchor["href"]
        match = re.search(rf"/(?:{re.escape(code)})/(\d+)(?:/|$)", href, re.I)
        if match:
            numbers.add(int(match.group(1)))
    return sorted(number for number in numbers if number > 0)


def upsert_meeting(conn: sqlite3.Connection, meeting: OverseasMeeting, fixture_document_id: int | None, status: str = "discovered") -> int:
    conn.execute(
        """INSERT INTO overseas_meetings(meeting_date,simulcast_code,meeting_name,location,fixture_url,summary_url,fixture_document_id,discovery_status,discovered_at_utc)
        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(meeting_date,simulcast_code) DO UPDATE SET
        meeting_name=COALESCE(excluded.meeting_name,overseas_meetings.meeting_name),location=COALESCE(excluded.location,overseas_meetings.location),
        fixture_url=excluded.fixture_url,summary_url=excluded.summary_url,fixture_document_id=COALESCE(excluded.fixture_document_id,overseas_meetings.fixture_document_id),discovery_status=excluded.discovery_status""",
        (meeting.meeting_date, meeting.simulcast_code, meeting.meeting_name, meeting.location, meeting.fixture_url, meeting.summary_url, fixture_document_id, status, utc_now()),
    )
    row = conn.execute("SELECT meeting_id FROM overseas_meetings WHERE meeting_date=? AND simulcast_code=?", (meeting.meeting_date, meeting.simulcast_code)).fetchone()
    conn.commit()
    return int(row[0])


def upsert_race(conn: sqlite3.Connection, meeting_id: int, meeting: OverseasMeeting, race_no: int, **fields: Any) -> int:
    key = f"{meeting.meeting_date}:{meeting.simulcast_code}:{race_no}"
    conn.execute(
        """INSERT INTO overseas_races(meeting_id,race_no,official_race_key,race_name,race_class,distance_m,surface,going,scheduled_start_local,scheduled_start_utc,race_status,racecard_url,result_url,fetched_at_utc)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(meeting_id,race_no) DO UPDATE SET race_status=excluded.race_status,
        race_name=COALESCE(excluded.race_name,overseas_races.race_name),race_class=COALESCE(excluded.race_class,overseas_races.race_class),
        distance_m=COALESCE(excluded.distance_m,overseas_races.distance_m),surface=COALESCE(excluded.surface,overseas_races.surface),going=COALESCE(excluded.going,overseas_races.going),
        scheduled_start_local=COALESCE(excluded.scheduled_start_local,overseas_races.scheduled_start_local),scheduled_start_utc=COALESCE(excluded.scheduled_start_utc,overseas_races.scheduled_start_utc),
        racecard_url=COALESCE(excluded.racecard_url,overseas_races.racecard_url),result_url=COALESCE(excluded.result_url,overseas_races.result_url),fetched_at_utc=excluded.fetched_at_utc""",
        (meeting_id, race_no, key, fields.get("race_name"), fields.get("race_class"), fields.get("distance_m"), fields.get("surface"), fields.get("going"), fields.get("scheduled_start_local"), fields.get("scheduled_start_utc"), fields.get("race_status", "discovered"), fields.get("racecard_url"), fields.get("result_url"), utc_now()),
    )
    row = conn.execute("SELECT overseas_race_id FROM overseas_races WHERE meeting_id=? AND race_no=?", (meeting_id, race_no)).fetchone()
    conn.commit()
    return int(row[0])


def table_headers(table: Tag) -> list[str]:
    row = table.find("tr")
    if not row:
        return []
    return [normalize_space(cell.get_text(" ", strip=True)).lower() for cell in row.find_all(["th", "td"])]


def header_position(headers: list[str], candidates: Iterable[str]) -> int | None:
    for index, header in enumerate(headers):
        if any(candidate in header for candidate in candidates):
            return index
    return None


def parse_racecard_context(html: str) -> dict[str, Any]:
    """Extract only clearly labelled public race context; leave ambiguity as null."""
    text = normalize_space(BeautifulSoup(html, "html.parser").get_text(" ", strip=True))
    surface_match = re.search(r"\b(\d{3,4})m\s+(Turf|Dirt|All\s*Weather|Synthetic)\b", text, re.I)
    context: dict[str, Any] = {
        "distance_m": int(surface_match.group(1)) if surface_match else None,
        "surface": surface_match.group(2).upper().replace("  ", " ") if surface_match else None,
        "going": None,
    }
    conditions = ["GOOD TO YIELDING", "GOOD TO SOFT", "GOOD TO FIRM", "YIELDING TO SOFT", "SOFT TO HEAVY", "VERY HEAVY", "GOOD", "YIELDING", "SOFT", "HEAVY", "FIRM", "FAST"]
    for condition in conditions:
        if re.search(rf"\b{re.escape(condition)}\b", text, re.I):
            context["going"] = condition
            break
    return context


def parse_racecard_starters(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    best: list[dict[str, Any]] = []
    for table in soup.find_all("table"):
        headers = table_headers(table)
        horse_no_i = header_position(headers, ["horse number", "h.no", "馬號"])
        horse_i = header_position(headers, ["horse"])
        if horse_no_i is None or horse_i is None:
            continue
        jockey_i = header_position(headers, ["jockey"])
        trainer_i = header_position(headers, ["trainer"])
        weight_i = header_position(headers, ["weight"])
        draw_i = header_position(headers, ["draw"])
        career_i = header_position(headers, ["career"])
        gear_i = header_position(headers, ["gear"])
        rating_i = header_position(headers, ["rpr", "ifha", "international rating", "world rating"])
        last_run_i = header_position(headers, ["last run date", "last run"])
        going_history_i = header_position(headers, ["going record", "going history"])
        trainer_g1_i = header_position(headers, ["trainer g1", "trainer group 1"])
        parsed: list[dict[str, Any]] = []
        for row in table.find_all("tr")[1:]:
            cells = [normalize_space(cell.get_text(" ", strip=True)) for cell in row.find_all("td")]
            if len(cells) <= max(horse_no_i, horse_i):
                continue
            horse_no = first_int(cells[horse_no_i])
            horse = cells[horse_i]
            if horse_no is None or not horse or horse.lower() in {"horse", "horse name"}:
                continue
            career = cells[career_i] if career_i is not None and career_i < len(cells) else ""
            wins_places = re.search(r"(\d+)\s*\((\d+)-(\d+)-(\d+)\)", career)
            rating_header = headers[rating_i] if rating_i is not None else ""
            rating_type = "RPR" if "rpr" in rating_header else "IFHA" if "ifha" in rating_header else "WORLD_RATING" if "world" in rating_header else "INTERNATIONAL_RATING" if "international" in rating_header else None
            rating_value = first_float(cells[rating_i]) if rating_i is not None and rating_i < len(cells) and rating_type else None
            g1_text = cells[trainer_g1_i] if trainer_g1_i is not None and trainer_g1_i < len(cells) else ""
            g1_match = re.search(r"(\d+)\s*[-/]\s*(\d+)", g1_text)
            raw_fields = {headers[i]: cells[i] for i in range(min(len(headers), len(cells)))}
            parsed.append({
                "horse_no": horse_no, "horse_name": horse, "jockey": cells[jockey_i] if jockey_i is not None and jockey_i < len(cells) else None,
                "trainer": cells[trainer_i] if trainer_i is not None and trainer_i < len(cells) else None,
                "weight_lbs": first_float(cells[weight_i]) if weight_i is not None and weight_i < len(cells) else None,
                "draw": first_int(cells[draw_i]) if draw_i is not None and draw_i < len(cells) else None,
                "career_starts": int(wins_places.group(1)) if wins_places else None,
                "career_wins": int(wins_places.group(2)) if wins_places else None,
                "career_places": int(wins_places.group(3)) + int(wins_places.group(4)) if wins_places else None,
                "gear": cells[gear_i] if gear_i is not None and gear_i < len(cells) else None,
                "international_rating": rating_value, "rating_type": rating_type,
                "last_run_date": cells[last_run_i] if last_run_i is not None and last_run_i < len(cells) else None,
                "going_history_json": cells[going_history_i] if going_history_i is not None and going_history_i < len(cells) else None,
                "trainer_g1_starts": int(g1_match.group(2)) if g1_match else None, "trainer_g1_wins": int(g1_match.group(1)) if g1_match else None,
                "source_row_fields": raw_fields,
            })
        if len(parsed) > len(best):
            best = parsed
    return best


def parse_results(html: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    soup = BeautifulSoup(html, "html.parser")
    starters: list[dict[str, Any]] = []
    dividends: list[dict[str, Any]] = []
    for table in soup.find_all("table"):
        headers = table_headers(table)
        place_i = header_position(headers, ["pla.", "place", "pos."])
        horse_no_i = header_position(headers, ["h.no", "horse no", "horse number"])
        horse_i = header_position(headers, ["horse"])
        if place_i is not None and horse_no_i is not None and horse_i is not None:
            local: list[dict[str, Any]] = []
            for row in table.find_all("tr")[1:]:
                cells = [normalize_space(cell.get_text(" ", strip=True)) for cell in row.find_all("td")]
                if len(cells) <= max(place_i, horse_no_i, horse_i):
                    continue
                horse_no = first_int(cells[horse_no_i])
                horse = cells[horse_i]
                if horse_no is None or not horse:
                    continue
                local.append({"horse_no": horse_no, "horse_name": horse, "finish_pos_text": cells[place_i], "finish_pos": first_int(cells[place_i])})
            if len(local) > len(starters):
                starters = local
        normalized_headers = [item.upper() for item in headers]
        if "POOL" in normalized_headers and any("DIVIDEND" in item for item in normalized_headers):
            for row in table.find_all("tr")[1:]:
                cells = [normalize_space(cell.get_text(" ", strip=True)) for cell in row.find_all("td")]
                if len(cells) < 3:
                    continue
                pool, combination, dividend = cells[0], cells[1], first_float(cells[-1])
                if pool and combination and dividend is not None:
                    dividends.append({"pool_name": pool.upper(), "winning_combination": combination, "dividend_hkd": dividend})
    return starters, dividends


def apply_racecard(conn: sqlite3.Connection, overseas_race_id: int, starters: list[dict[str, Any]]) -> None:
    from overseas_feature_enrichment import ensure_enrichment_schema
    ensure_enrichment_schema(conn)
    for row in starters:
        conn.execute(
            """INSERT INTO overseas_starters(overseas_race_id,horse_no,horse_name,jockey,trainer,weight_lbs,draw,career_starts,career_wins,career_places,gear,international_rating,rating_type,rating_source_url,rating_as_of_utc,last_run_date,going_history_json,trainer_g1_starts,trainer_g1_wins,source_fields_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(overseas_race_id,horse_no) DO UPDATE SET horse_name=excluded.horse_name,
            jockey=COALESCE(excluded.jockey,overseas_starters.jockey),trainer=COALESCE(excluded.trainer,overseas_starters.trainer),
            weight_lbs=COALESCE(excluded.weight_lbs,overseas_starters.weight_lbs),draw=COALESCE(excluded.draw,overseas_starters.draw),
            career_starts=COALESCE(excluded.career_starts,overseas_starters.career_starts),career_wins=COALESCE(excluded.career_wins,overseas_starters.career_wins),
            career_places=COALESCE(excluded.career_places,overseas_starters.career_places),gear=COALESCE(excluded.gear,overseas_starters.gear),
            international_rating=COALESCE(excluded.international_rating,overseas_starters.international_rating),rating_type=COALESCE(excluded.rating_type,overseas_starters.rating_type),
            rating_source_url=COALESCE(excluded.rating_source_url,overseas_starters.rating_source_url),rating_as_of_utc=COALESCE(excluded.rating_as_of_utc,overseas_starters.rating_as_of_utc),
            last_run_date=COALESCE(excluded.last_run_date,overseas_starters.last_run_date),going_history_json=COALESCE(excluded.going_history_json,overseas_starters.going_history_json),
            trainer_g1_starts=COALESCE(excluded.trainer_g1_starts,overseas_starters.trainer_g1_starts),trainer_g1_wins=COALESCE(excluded.trainer_g1_wins,overseas_starters.trainer_g1_wins),source_fields_json=excluded.source_fields_json""",
            (overseas_race_id, row["horse_no"], row["horse_name"], row.get("jockey"), row.get("trainer"), row.get("weight_lbs"), row.get("draw"), row.get("career_starts"), row.get("career_wins"), row.get("career_places"), row.get("gear"), row.get("international_rating"), row.get("rating_type"), row.get("rating_source_url"), row.get("rating_as_of_utc"), row.get("last_run_date"), row.get("going_history_json"), row.get("trainer_g1_starts"), row.get("trainer_g1_wins"), json.dumps(row, ensure_ascii=False)),
        )
    conn.commit()


def apply_results(conn: sqlite3.Connection, overseas_race_id: int, starters: list[dict[str, Any]], dividends: list[dict[str, Any]], source_url: str) -> None:
    for row in starters:
        conn.execute(
            """INSERT INTO overseas_starters(overseas_race_id,horse_no,horse_name,finish_pos_text,finish_pos,source_fields_json)
            VALUES(?,?,?,?,?,?) ON CONFLICT(overseas_race_id,horse_no) DO UPDATE SET finish_pos_text=excluded.finish_pos_text,
            finish_pos=excluded.finish_pos,horse_name=COALESCE(excluded.horse_name,overseas_starters.horse_name),source_fields_json=excluded.source_fields_json""",
            (overseas_race_id, row["horse_no"], row["horse_name"], row.get("finish_pos_text"), row.get("finish_pos"), json.dumps(row, ensure_ascii=False)),
        )
    for row in dividends:
        conn.execute(
            """INSERT INTO overseas_dividends(overseas_race_id,pool_name,winning_combination,dividend_hkd,source_url)
            VALUES(?,?,?,?,?) ON CONFLICT(overseas_race_id,pool_name,winning_combination) DO UPDATE SET dividend_hkd=excluded.dividend_hkd,source_url=excluded.source_url""",
            (overseas_race_id, row["pool_name"], row["winning_combination"], row["dividend_hkd"], source_url),
        )
    conn.execute("UPDATE overseas_races SET race_status=?, fetched_at_utc=? WHERE overseas_race_id=?", ("completed" if starters else "partial", utc_now(), overseas_race_id))
    conn.commit()


def discover_fixture_season(conn: sqlite3.Connection, client: OfficialOverseasClient, season_code: str, start: str, end: str) -> list[OverseasMeeting]:
    url = FIXTURE_URL.format(season_code=season_code)
    try:
        html, document_id = client.get_rendered(url, "fixture", wait_mode="fixture")
        meetings = [item for item in parse_fixture(html, url) if start <= item.meeting_date <= end]
        status = "complete" if meetings else "empty"
        for meeting in meetings:
            upsert_meeting(conn, meeting, document_id)
        detail = "fixture parsed"
    except RuntimeError as exc:
        meetings = []
        status = "rate_limited" if "429" in str(exc) or "403" in str(exc) else "source_unavailable"
        detail = str(exc)
    conn.execute("INSERT INTO overseas_discovery_audit(season_code,requested_start_date,requested_end_date,fixture_url,discovered_meetings,status,checked_at_utc,detail) VALUES(?,?,?,?,?,?,?,?)", (season_code, start, end, url, len(meetings), status, utc_now(), detail))
    conn.commit()
    return meetings


def archive_meeting(conn: sqlite3.Connection, client: OfficialOverseasClient, meeting: OverseasMeeting) -> dict[str, Any]:
    row = conn.execute("SELECT meeting_id FROM overseas_meetings WHERE meeting_date=? AND simulcast_code=?", (meeting.meeting_date, meeting.simulcast_code)).fetchone()
    meeting_id = int(row[0]) if row else upsert_meeting(conn, meeting, None)
    try:
        summary_html, _ = client.get_rendered(meeting.summary_url, "summary", wait_mode="summary")
    except RuntimeError as exc:
        conn.execute("UPDATE overseas_meetings SET discovery_status='source_unavailable' WHERE meeting_id=?", (meeting_id,))
        conn.commit()
        return {"meeting": meeting.meeting_date + " " + meeting.simulcast_code, "status": "source_unavailable", "detail": str(exc), "races": 0}
    race_numbers = extract_race_numbers(summary_html, meeting.simulcast_code)
    if not race_numbers and meeting.seed_race_no:
        race_numbers = [meeting.seed_race_no]
    if not race_numbers:
        conn.execute("UPDATE overseas_meetings SET discovery_status='source_unavailable' WHERE meeting_id=?", (meeting_id,))
        conn.commit()
        return {"meeting": meeting.meeting_date + " " + meeting.simulcast_code, "status": "no_race_numbers", "races": 0}
    conn.execute("UPDATE overseas_meetings SET discovery_status='race_count_verified' WHERE meeting_id=?", (meeting_id,))
    conn.commit()
    archived = 0
    unavailable = 0
    for race_no in race_numbers:
        race_id = upsert_race(conn, meeting_id, meeting, race_no, race_status="discovered", racecard_url=meeting.summary_url, result_url=RESULT_URL.format(compact_date=compact_date(meeting.meeting_date), code=meeting.simulcast_code, race_no=race_no))
        card_starters = parse_racecard_starters(summary_html) if len(race_numbers) == 1 else []
        if card_starters:
            apply_racecard(conn, race_id, card_starters)
        result_url = RESULT_URL.format(compact_date=compact_date(meeting.meeting_date), code=meeting.simulcast_code, race_no=race_no)
        try:
            result_html, result_doc = client.get_rendered(result_url, "result", wait_mode="result")
            result_starters, dividends = parse_results(result_html)
            apply_results(conn, race_id, result_starters, dividends, result_url)
        except RuntimeError as exc:
            conn.execute("UPDATE overseas_races SET race_status='source_unavailable', fetched_at_utc=? WHERE overseas_race_id=?", (utc_now(), race_id))
            conn.commit()
            unavailable += 1
            continue
        conn.execute("UPDATE overseas_races SET result_document_id=? WHERE overseas_race_id=?", (result_doc, race_id))
        conn.commit()
        archived += 1
    status = "ok" if archived == len(race_numbers) else "partial"
    if unavailable:
        conn.execute("UPDATE overseas_meetings SET discovery_status='partial' WHERE meeting_id=?", (meeting_id,))
        conn.commit()
    return {"meeting": meeting.meeting_date + " " + meeting.simulcast_code, "status": status, "races": archived, "races_source_unavailable": unavailable}


def select_meetings(conn: sqlite3.Connection, start: str, end: str, statuses: tuple[str, ...] = ("discovered", "partial", "source_unavailable")) -> list[OverseasMeeting]:
    placeholders = ",".join("?" for _ in statuses)
    rows = conn.execute(f"SELECT meeting_date,simulcast_code,meeting_name,location,fixture_url,summary_url FROM overseas_meetings WHERE meeting_date BETWEEN ? AND ? AND discovery_status IN ({placeholders}) ORDER BY meeting_date,simulcast_code", (start, end, *statuses)).fetchall()
    return [OverseasMeeting(row[0], row[1], row[2], row[3], row[4], row[5], None) for row in rows]


def db_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = ["overseas_meetings", "overseas_races", "overseas_starters", "overseas_dividends", "overseas_source_documents"]
    return {table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in tables}
