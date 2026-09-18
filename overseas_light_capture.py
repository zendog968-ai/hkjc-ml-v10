#!/usr/bin/env python3
"""Capture one approved overseas HKJC public Win/Place page into isolated SQLite.

This module is deliberately separate from V10 prediction and N6. It makes no
model call, does not open the production database, and stores no probability,
EV, or Kelly value. Every run is manifest-gated, rate-limited and limited to
one approved overseas event by default.
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup

UTC = dt.timezone.utc
SCHEMA_VERSION = "overseas_light_capture_v1"
N6_STATUS = "disabled_non_hk"
ALLOWED_CODES = {"S1", "S2", "S3"}
ALLOWED_COUNTRIES = {"australia", "united kingdom", "england", "great britain", "uk"}
ALLOWED_HOSTS = {"bet.hkjc.com", "racing.hkjc.com"}


def utc_now() -> str:
    return dt.datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    body = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with open(temporary, "xb") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object")
    return value


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def column_key(value: str) -> str:
    return re.sub(r"[^a-z]", "", value.lower())


def parse_int(value: str) -> int | None:
    match = re.fullmatch(r"\s*(\d+)\s*", value)
    return int(match.group(1)) if match else None


def parse_positive_float(value: str) -> float | None:
    try:
        number = float(value.replace(",", "").strip())
    except ValueError:
        return None
    return number if number > 1.0 else None


def is_scratched(cells: list[str]) -> bool:
    merged = "|".join(cells).upper()
    return "SCR" in merged or "退出" in merged or "撤回" in merged


def parse_manifest(path: Path) -> dict[str, Any]:
    manifest = read_json(path)
    if manifest.get("schema_version") != "overseas_light_capture_manifest_v1":
        raise ValueError("manifest schema_version is not approved")
    if manifest.get("enabled") is not True:
        raise PermissionError("manifest is not enabled; no network request is permitted")
    if manifest.get("jurisdiction") != "overseas":
        raise ValueError("manifest jurisdiction must be overseas")
    allowed_hosts = set(manifest.get("allowed_hosts", []))
    if not allowed_hosts or not allowed_hosts.issubset(ALLOWED_HOSTS):
        raise ValueError("manifest host allowlist is missing or contains an unapproved host")
    interval = manifest.get("minimum_request_interval_seconds", 60)
    if not isinstance(interval, int) or interval < 60:
        raise ValueError("minimum_request_interval_seconds must be an integer of at least 60")
    events = manifest.get("events")
    if not isinstance(events, list) or not events:
        raise ValueError("manifest must include at least one approved overseas event")
    approved: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("event must be an object")
        code = str(event.get("simulcast_code", "")).upper()
        race_no = event.get("race_no")
        country = str(event.get("country", "")).casefold()
        event_key = str(event.get("event_key", ""))
        if code not in ALLOWED_CODES or not isinstance(race_no, int) or race_no < 1:
            raise ValueError("event is not a supported overseas simulcast event")
        if country not in ALLOWED_COUNTRIES:
            raise ValueError("event country is not an approved Australia/UK value")
        if event_key != f"{event.get('meeting_date')}:{code}:{race_no}" or event_key in seen:
            raise ValueError("event_key is invalid or duplicated")
        seen.add(event_key)
        source_url = str(event.get("hkjc_win_place_url", ""))
        parsed = urlparse(source_url)
        expected_path = f"/en/racing/wp/{event.get('meeting_date')}/{code}/{race_no}"
        if parsed.scheme != "https" or parsed.hostname not in allowed_hosts or parsed.path != expected_path:
            raise ValueError("event page URL failed exact host/path validation")
        scheduled = dt.datetime.fromisoformat(str(event.get("scheduled_start_utc", "")).replace("Z", "+00:00"))
        if scheduled.tzinfo is None:
            raise ValueError("scheduled_start_utc must include UTC timezone")
        approved.append({**event, "simulcast_code": code, "scheduled_start_utc": scheduled.astimezone(UTC).isoformat().replace("+00:00", "Z")})
    return {**manifest, "events": approved, "minimum_request_interval_seconds": interval}


def guard_runtime(database: Path, require_n6_disabled: bool) -> None:
    if require_n6_disabled and os.environ.get("N6_STATUS") != N6_STATUS:
        raise RuntimeError("N6_STATUS environment guard is not disabled_non_hk")
    prohibited_names = {"hkjc_last_season.sqlite", "horse_model.pkl"}
    if database.name in prohibited_names or any(part == "archive" for part in database.resolve().parts):
        raise ValueError("refusing a production or archive database path")
    source_tree = ast.parse(Path(__file__).read_text(encoding="utf-8"), filename=__file__)
    for node in ast.walk(source_tree):
        imported = []
        if isinstance(node, ast.Import):
            imported = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported = [node.module or ""]
        if any(name == "n6" or name.startswith("n6.") or name == "torch" or name.startswith("torch.") for name in imported):
            raise RuntimeError("source-level N6/model import guard failed")


def ensure_interval(state_path: Path, minimum_seconds: int) -> None:
    if not state_path.exists():
        return
    previous = read_json(state_path)
    previous_epoch = float(previous.get("last_attempt_epoch", 0.0))
    elapsed = time.time() - previous_epoch
    if elapsed < minimum_seconds:
        raise RuntimeError(f"rate_limited_wait_seconds={int(minimum_seconds - elapsed) + 1}")


def fetch_page(url: str, timeout_seconds: int, expected_race_no: int) -> str:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("playwright is unavailable on this host") from exc
    try:
        with sync_playwright() as playwright:
            # Use the Playwright-managed browser path; the host need not expose a system Chromium binary.
            browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
            try:
                page = browser.new_page(locale="en-HK", user_agent="Mozilla/5.0 (overseas metadata-market research; no-bet reader)")
                response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)
                if response is not None and response.status in {403, 429}:
                    raise RuntimeError(f"public_source_http_{response.status}")
                page.wait_for_function(r"""(raceNo) => new RegExp(`Race\\s+${raceNo}\\s+\\d{2}/\\d{2}`, 'i').test(document.body.innerText)""", arg=expected_race_no, timeout=timeout_seconds * 1000)
                page.wait_for_function(r"""() => Array.from(document.querySelectorAll('table')).some(t => /Horse\s*Name/i.test(t.innerText) && /Win/i.test(t.innerText) && /Place/i.test(t.innerText))""", timeout=timeout_seconds * 1000)
                page.wait_for_function(r"""(raceNo) => {
                    const odds = Array.from(document.querySelectorAll(`[id^="odds_WIN_${raceNo}_"]`));
                    return odds.length >= 2 && odds.every((node) => node.textContent.trim().length > 0);
                }""", arg=expected_race_no, timeout=timeout_seconds * 1000)
                page.wait_for_timeout(750)
                return page.content()
            finally:
                browser.close()
    except PlaywrightTimeoutError as exc:
        raise RuntimeError("public_page_table_timeout") from exc


def parse_page(html: str, event: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    soup = BeautifulSoup(html, "html.parser")
    page_text = clean(soup.get_text(" ", strip=True))
    race_no = int(event["race_no"])
    meta = re.search(rf"Race\s+{race_no}\s+\d{{2}}/\d{{2}},\s*[A-Z]{{3}},\s*(\d{{2}}:\d{{2}}),\s*([^,]+),\s*(.+?),\s*([^,]+),\s*(YIELDING\s+TO\s+SOFT|GOOD\s+TO\s+SOFT|SOFT|HEAVY|GOOD(?:\s+\d)?|FIRM|SYNTHETIC)\b", page_text, re.I)
    if meta is None:
        raise ValueError("public page metadata did not match the approved overseas event")
    hkt_time, page_country, detail_text, page_venue, going = meta.groups()
    country_aliases = {"australia": {"australia"}, "united kingdom": {"united kingdom", "england", "great britain", "uk"}, "england": {"united kingdom", "england", "great britain", "uk"}, "great britain": {"united kingdom", "england", "great britain", "uk"}, "uk": {"united kingdom", "england", "great britain", "uk"}}
    manifest_country = str(event["country"]).casefold()
    if clean(page_country).casefold() not in country_aliases[manifest_country] or clean(page_venue).casefold() != clean(str(event["venue"])).casefold():
        raise ValueError("public page country or venue does not match approved manifest")
    tables = soup.find_all("table")
    for table in tables:
        rows = table.find_all("tr")
        header_pos = None
        columns: dict[str, int] = {}
        for index, row in enumerate(rows):
            header = [clean(cell.get_text(" ", strip=True)) for cell in row.find_all(["th", "td"])]
            candidate = {column_key(value): pos for pos, value in enumerate(header)}
            if {"no", "horsename", "draw", "wt", "win", "place"}.issubset(candidate):
                header_pos, columns = index, candidate
                break
        if header_pos is None:
            continue
        starters: list[dict[str, Any]] = []
        header_width = len(rows[header_pos].find_all(["th", "td"]))
        for row in rows[header_pos + 1:]:
            cells = [clean(cell.get_text(" ", strip=True)) for cell in row.find_all("td")]
            if len(cells) < 3:
                continue
            cells += [""] * max(0, header_width - len(cells))
            runner_no = parse_int(cells[columns["no"]])
            horse_name = cells[columns["horsename"]]
            if runner_no is None or not horse_name or horse_name.casefold() in {"field", "f"}:
                continue
            scratched = is_scratched(cells)
            entry = {
                "runner_no": runner_no,
                "horse_name": horse_name,
                "is_scratched": scratched,
                "draw_no": None if scratched else parse_int(cells[columns["draw"]]),
                "weight_lbs": None if scratched else parse_positive_float(cells[columns["wt"]]),
                "hkjc_win_odds": None if scratched else parse_positive_float(cells[columns["win"]]),
                "hkjc_place_odds": None if scratched else parse_positive_float(cells[columns["place"]]),
            }
            if not scratched and any(entry[field] is None for field in ("draw_no", "weight_lbs", "hkjc_win_odds", "hkjc_place_odds")):
                raise ValueError(f"active runner data is incomplete: {runner_no}")
            starters.append(entry)
        if starters:
            starters.sort(key=lambda row: int(row["runner_no"]))
            if len({row["runner_no"] for row in starters}) != len(starters):
                raise ValueError("runner numbers are duplicated")
            return {"hkt_start_time": hkt_time, "detail": detail_text, "country": clean(page_country), "venue": clean(page_venue), "going": going.upper()}, starters
    raise ValueError("no approved Win/Place starter table found")


def initialise_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS capture_run (
      run_id INTEGER PRIMARY KEY,
      captured_at_utc TEXT NOT NULL,
      schema_version TEXT NOT NULL,
      manifest_sha256 TEXT NOT NULL,
      n6_status TEXT NOT NULL CHECK(n6_status='disabled_non_hk'),
      model_probability_status TEXT NOT NULL CHECK(model_probability_status='N/A'),
      ev_status TEXT NOT NULL CHECK(ev_status='N/A'),
      kelly_status TEXT NOT NULL CHECK(kelly_status='N/A'),
      formal_v10_changed INTEGER NOT NULL CHECK(formal_v10_changed=0),
      status TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS race_snapshot (
      race_id INTEGER PRIMARY KEY,
      run_id INTEGER NOT NULL REFERENCES capture_run(run_id),
      event_key TEXT NOT NULL UNIQUE,
      meeting_date TEXT NOT NULL,
      simulcast_code TEXT NOT NULL CHECK(simulcast_code IN ('S1','S2','S3')),
      race_no INTEGER NOT NULL CHECK(race_no>0),
      country TEXT NOT NULL,
      venue TEXT NOT NULL,
      scheduled_start_utc TEXT NOT NULL,
      parsed_hkt_start_time TEXT NOT NULL,
      going TEXT NOT NULL,
      source_url TEXT NOT NULL,
      raw_html_path TEXT NOT NULL,
      raw_html_sha256 TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS starter_market_snapshot (
      starter_id INTEGER PRIMARY KEY,
      race_id INTEGER NOT NULL REFERENCES race_snapshot(race_id),
      runner_no INTEGER NOT NULL,
      horse_name TEXT NOT NULL,
      is_scratched INTEGER NOT NULL CHECK(is_scratched IN (0,1)),
      draw_no INTEGER,
      weight_lbs REAL,
      hkjc_win_odds REAL,
      hkjc_place_odds REAL,
      model_win_probability REAL CHECK(model_win_probability IS NULL),
      model_place_probability REAL CHECK(model_place_probability IS NULL),
      win_ev REAL CHECK(win_ev IS NULL),
      place_ev REAL CHECK(place_ev IS NULL),
      kelly_fraction REAL CHECK(kelly_fraction IS NULL),
      UNIQUE(race_id,runner_no)
    );
    """)


def event_already_captured(database: Path, event_key: str) -> bool:
    if not database.exists():
        return False
    conn = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT 1 FROM race_snapshot WHERE event_key=?", (event_key,)).fetchone() is not None
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


def persist_capture(database: Path, event: dict[str, Any], page_meta: dict[str, Any], starters: list[dict[str, Any]], raw_path: Path, raw_sha256: str, manifest_sha256: str) -> dict[str, int | str]:
    database.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database)
    try:
        initialise_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM race_snapshot WHERE event_key=?", (event["event_key"],)).fetchone():
            raise RuntimeError("event already captured while obtaining database lock")
        conn.execute("INSERT INTO capture_run(captured_at_utc,schema_version,manifest_sha256,n6_status,model_probability_status,ev_status,kelly_status,formal_v10_changed,status) VALUES(?,?,?,?,?,?,?,?,?)", (utc_now(), SCHEMA_VERSION, manifest_sha256, N6_STATUS, "N/A", "N/A", "N/A", 0, "complete"))
        run_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute("INSERT INTO race_snapshot(run_id,event_key,meeting_date,simulcast_code,race_no,country,venue,scheduled_start_utc,parsed_hkt_start_time,going,source_url,raw_html_path,raw_html_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (run_id,event["event_key"],event["meeting_date"],event["simulcast_code"],event["race_no"],event["country"],event["venue"],event["scheduled_start_utc"],page_meta["hkt_start_time"],page_meta["going"],event["hkjc_win_place_url"],str(raw_path),raw_sha256))
        race_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        for row in starters:
            conn.execute("INSERT INTO starter_market_snapshot(race_id,runner_no,horse_name,is_scratched,draw_no,weight_lbs,hkjc_win_odds,hkjc_place_odds,model_win_probability,model_place_probability,win_ev,place_ev,kelly_fraction) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (race_id,row["runner_no"],row["horse_name"],int(row["is_scratched"]),row["draw_no"],row["weight_lbs"],row["hkjc_win_odds"],row["hkjc_place_odds"],None,None,None,None,None))
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        model_fields = conn.execute("SELECT COUNT(*) FROM starter_market_snapshot WHERE model_win_probability IS NOT NULL OR model_place_probability IS NOT NULL OR win_ev IS NOT NULL OR place_ev IS NOT NULL OR kelly_fraction IS NOT NULL").fetchone()[0]
        if integrity != "ok" or fk_errors or model_fields != 0:
            raise RuntimeError("SQLite validation failed")
        conn.commit()
        return {"integrity_check": integrity, "foreign_key_violations": len(fk_errors), "model_fields_populated": model_fields, "active_runners": sum(not row["is_scratched"] for row in starters), "scratched_rows": sum(row["is_scratched"] for row in starters)}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--raw-dir", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--offline-html", type=Path, help="approved previously captured public HTML; never used by the systemd timer")
    parser.add_argument("--max-races", type=int, default=1)
    parser.add_argument("--min-interval-seconds", type=int, default=60)
    parser.add_argument("--timeout-seconds", type=int, default=40)
    parser.add_argument("--require-n6-disabled", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.max_races != 1 or args.min_interval_seconds < 60:
        raise SystemExit("REFUSED: this light capture only permits one event/run and a >=60 second source interval")
    try:
        guard_runtime(args.database, args.require_n6_disabled)
        manifest_bytes = args.manifest.read_bytes()
        raw_manifest = json.loads(manifest_bytes.decode("utf-8"))
        if not isinstance(raw_manifest, dict) or raw_manifest.get("schema_version") != "overseas_light_capture_manifest_v1":
            raise ValueError("manifest schema_version is not approved")
        if raw_manifest.get("enabled") is not True:
            outcome: dict[str, Any] = {"status":"disabled_manifest_no_network_no_sqlite_write","n6_status":N6_STATUS,"model_probability_status":"N/A","ev_status":"N/A","kelly_status":"N/A","formal_v10_changed":False,"n6_service_changed":False}
            outcome["reported_at_utc"] = utc_now()
            write_json_atomic(args.report_dir / "latest.json", outcome)
            print(json.dumps(outcome,ensure_ascii=False,sort_keys=True))
            return 0
        manifest = parse_manifest(args.manifest.resolve())
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        now = dt.datetime.now(UTC)
        uncaptured = [item for item in manifest["events"] if not event_already_captured(args.database, item["event_key"])]
        eligible = []
        for item in uncaptured:
            scheduled = dt.datetime.fromisoformat(item["scheduled_start_utc"].replace("Z", "+00:00")).astimezone(UTC)
            if scheduled - dt.timedelta(hours=6) <= now < scheduled:
                eligible.append(item)
        event = eligible[0] if eligible else None
        if event is None:
            outcome: dict[str, Any] = {"status":"no_uncaptured_event_in_pre_race_window","uncaptured_event_count":len(uncaptured),"n6_status":N6_STATUS,"model_probability_status":"N/A","ev_status":"N/A","kelly_status":"N/A","formal_v10_changed":False,"n6_service_changed":False}
        elif args.dry_run:
            outcome = {"status":"dry_run_passed_no_network_no_sqlite_write","event_key":event["event_key"],"n6_status":N6_STATUS,"model_probability_status":"N/A","ev_status":"N/A","kelly_status":"N/A","formal_v10_changed":False,"n6_service_changed":False,"manifest_sha256":manifest_sha256}
        else:
            scheduled_start = dt.datetime.fromisoformat(event["scheduled_start_utc"].replace("Z", "+00:00")).astimezone(UTC)
            window_open = scheduled_start - dt.timedelta(hours=6)
            now = dt.datetime.now(UTC)
            if now < window_open or now >= scheduled_start:
                outcome = {"status":"not_in_pre_race_capture_window","event_key":event["event_key"],"window_open_utc":window_open.isoformat().replace("+00:00","Z"),"scheduled_start_utc":event["scheduled_start_utc"],"n6_status":N6_STATUS,"model_probability_status":"N/A","ev_status":"N/A","kelly_status":"N/A","formal_v10_changed":False,"n6_service_changed":False}
                outcome["reported_at_utc"] = utc_now()
                write_json_atomic(args.report_dir / "latest.json", outcome)
                print(json.dumps(outcome,ensure_ascii=False,sort_keys=True))
                return 0
            if args.offline_html:
                offline_html = args.offline_html.resolve()
                if offline_html.suffix.lower() != ".html" or not offline_html.is_file() or "archive" in offline_html.parts:
                    raise ValueError("offline replay accepts only an approved non-archive .html fixture")
                html = offline_html.read_text(encoding="utf-8")
                source_mode = "offline_approved_public_html_replay"
            else:
                ensure_interval(args.state_file, max(manifest["minimum_request_interval_seconds"], args.min_interval_seconds, 60))
                write_json_atomic(args.state_file, {"last_attempt_epoch":time.time(),"attempted_at_utc":utc_now(),"event_key":event["event_key"]})
                html = fetch_page(event["hkjc_win_place_url"], args.timeout_seconds, int(event["race_no"]))
                source_mode = "public_rendered_page"
            page_meta, starters = parse_page(html, event)
            raw_bytes = html.encode("utf-8")
            raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
            raw_path = args.raw_dir / event["meeting_date"] / f"{event['event_key'].replace(':','_')}_{utc_now().replace(':','')}.html"
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_bytes(raw_bytes)
            os.chmod(raw_path,0o600)
            checks = persist_capture(args.database.resolve(), event, page_meta, starters, raw_path.resolve(), raw_sha256, manifest_sha256)
            outcome = {"status":"captured","capture_mode":source_mode,"event_key":event["event_key"],"n6_status":N6_STATUS,"model_probability_status":"N/A","ev_status":"N/A","kelly_status":"N/A","formal_v10_changed":False,"n6_service_changed":False,"source_url":event["hkjc_win_place_url"],"raw_html_sha256":raw_sha256,"raw_html_path":str(raw_path.resolve()),"going":page_meta["going"],**checks}
    except RuntimeError as exc:
        status = "rate_limited_no_network_no_sqlite_write" if str(exc).startswith("rate_limited_wait_seconds=") else "refused_or_failed"
        outcome = {"status":status,"reason":f"{type(exc).__name__}:{exc}","n6_status":N6_STATUS,"model_probability_status":"N/A","ev_status":"N/A","kelly_status":"N/A","formal_v10_changed":False,"n6_service_changed":False}
    except Exception as exc:
        outcome = {"status":"refused_or_failed","reason":f"{type(exc).__name__}:{exc}","n6_status":N6_STATUS,"model_probability_status":"N/A","ev_status":"N/A","kelly_status":"N/A","formal_v10_changed":False,"n6_service_changed":False}
    outcome["reported_at_utc"] = utc_now()
    report_path = args.report_dir / "latest.json"
    write_json_atomic(report_path, outcome)
    print(json.dumps(outcome,ensure_ascii=False,sort_keys=True))
    return 0 if outcome["status"] in {"captured","no_uncaptured_approved_event","no_uncaptured_event_in_pre_race_window","dry_run_passed_no_network_no_sqlite_write","disabled_manifest_no_network_no_sqlite_write","rate_limited_no_network_no_sqlite_write","not_in_pre_race_capture_window"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
