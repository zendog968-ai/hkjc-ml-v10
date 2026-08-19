#!/usr/bin/env python3
"""Polite public-page deep-data prototype for overseas S1/S2 racing.

This module deliberately has no import of V10 local SQLite or N6.  It never
bypasses login, paywalls, robots controls, or anti-bot mechanisms. Restricted
fields are recorded as unavailable instead of being inferred.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
DEFAULT_RP = "https://www.racingpost.com/racecards/107/york/2026-08-19/924986/"
DEFAULT_ATR = "https://www.attheraces.com/racecards/York/19-August-2026"
DEFAULT_TIMEFORM = "https://www.timeform.com/horse-racing/racecards/york/2026-08-19/1350/62/1"
DEFAULT_HKJC_ODDS = "https://bet.hkjc.com/en/racing/wp/2026-08-19/S1/1"
USER_AGENT = "HKJCV10Research/1.0 (+read-only public racecard research; contact administrator)"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_name(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    value = re.sub(r"\s*\((?:IRE|GB|FR|USA|GER|AUS|NZ)\)\s*$", "", value, flags=re.I)
    return re.sub(r"\s+(?:IRE|GB|FR|USA|GER|AUS|NZ)$", "", value, flags=re.I).strip()


def norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", clean_name(value).lower())


def fetch_public(url: str, timeout: int) -> tuple[str | None, str | None]:
    try:
        response = requests.get(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en-GB,en;q=0.9"}, timeout=timeout)
        if response.status_code != 200:
            return None, f"HTTP {response.status_code}"
        return response.text, None
    except requests.RequestException as exc:
        return None, f"{type(exc).__name__}: {exc}"


def page_lines(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    return [re.sub(r"\s+", " ", line).strip() for line in soup.get_text("\n").splitlines() if line.strip()]


def parse_racing_post(html: str) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Extract public card fields without accessing subscriber-only sections."""
    lines = page_lines(html)
    joined = "\n".join(lines)
    race = {
        "race_name": None, "distance_text": None, "going": None, "race_class": None, "declared_runners": None,
    }
    heading = next((line for line in lines if "Handicap" in line or "Stakes" in line), None)
    race["race_name"] = heading
    distance = next((line for line in lines if re.fullmatch(r"\d+(?:½|\.\d+)?f.*", line)), None)
    race["distance_text"] = distance
    going_match = re.search(r"\bGoing:\s*([A-Za-z ]+)", joined)
    race["going"] = going_match.group(1).strip() if going_match else None
    runner_match = re.search(r"Runners:\s*(\d+)", joined)
    race["declared_runners"] = int(runner_match.group(1)) if runner_match else None
    class_match = re.search(r"\bClass\s*(\d+)", joined)
    race["race_class"] = f"Class {class_match.group(1)}" if class_match else None

    starters: dict[str, dict[str, Any]] = {}
    soup = BeautifulSoup(html, "html.parser")
    rating_pattern = re.compile(r"OR\s*:?\s*(\d+)\s+TS\s*:?\s*(-|\d+)\s+RPR\s*:?\s*(\d+)", re.I)
    # Horse identity is extracted from the public horse-profile link below; this
    # expression only binds the displayed runner number and draw, avoiding any
    # dependency on varying UK equipment, colour or age wording.
    runner_pattern = re.compile(r"\b(\d+)\s*\(\s*(\d+)\s*\)")
    for rating_text in soup.find_all(string=re.compile(r"\bRPR\b", re.I)):
        parent = rating_text.parent
        for _ in range(12):
            if parent is None:
                break
            snippet = " ".join(parent.get_text(" ", strip=True).split())
            rating_match = rating_pattern.search(snippet)
            runner_match = runner_pattern.search(snippet)
            if rating_match and runner_match:
                horse_link = next((anchor for anchor in parent.find_all("a", href=True) if "/profile/horse/" in str(anchor.get("href"))), None)
                horse = clean_name(horse_link.get_text(" ", strip=True) if horse_link else "")
                key = norm(horse)
                if key and key not in starters:
                    pace_text = snippet.lower()
                    starters[key] = {
                        "runner_no": int(runner_match.group(1)), "draw_no": int(runner_match.group(2)), "horse_name": horse,
                        "official_rating": int(rating_match.group(1)), "top_speed_rating": None if rating_match.group(2) == "-" else int(rating_match.group(2)),
                        "racing_post_rating": int(rating_match.group(3)),
                        "pace_hint": "front_runner_hint" if "enjoys making it" in pace_text else ("held_up_hint" if "too much to do" in pace_text else None),
                        "source_rpr_ts_url": None,
                    }
                break
            parent = parent.parent
    return race, starters


def parse_atr(html: str) -> dict[str, dict[str, Any]]:
    """Extract only explicitly public pedigree and condition records from ATR."""
    soup = BeautifulSoup(html, "html.parser")
    rows: dict[str, dict[str, Any]] = {}
    for card in soup.select("div.horse"):
        horse_link = next((anchor for anchor in card.find_all("a", href=True) if "/form/horse/" in str(anchor.get("href"))), None)
        if horse_link is None:
            continue
        horse = clean_name(horse_link.get_text(" ", strip=True))
        if len(horse) < 2:
            continue
        block = " ".join(card.get_text(" ", strip=True).split())
        def condition(label: str) -> tuple[int | None, int | None]:
            found = re.search(rf"{label}:\s*(\d+)\s+runs,\s*(\d+)\s+win", block, flags=re.I)
            return (int(found.group(1)), int(found.group(2))) if found else (None, None)
        distance_runs, distance_wins = condition("Distance")
        going_runs, going_wins = condition("Similar Going")
        course_runs, course_wins = condition("Course")
        pedigree = re.search(r"\b[bcfg]\s+[a-z]\s+(.+?)\s+-\s+([^()]+?)\s*\(([^)]+)\)", block, flags=re.I)
        rows[norm(horse)] = {
            "horse_name": horse,
            "distance_runs": distance_runs, "distance_wins": distance_wins,
            "similar_going_runs": going_runs, "similar_going_wins": going_wins,
            "course_runs": course_runs, "course_wins": course_wins,
            "sire": clean_name(pedigree.group(1)) if pedigree else None,
            "dam": clean_name(pedigree.group(2)) if pedigree else None,
            "damsire": clean_name(pedigree.group(3)) if pedigree else None,
            "at_the_races_rating": None,
        }
    return rows


def fetch_hkjc_odds(url: str, timeout: int) -> tuple[dict[str, dict[str, float | None]], str | None]:
    try:
        from fetch_hkjc_live_odds import fetch_rendered_public_page, parse_visible_odds_table
        html = fetch_rendered_public_page(url, timeout)
        parsed, _ = parse_visible_odds_table(html)
        return {norm(name): values for name, values in parsed.items()}, None
    except Exception as exc:
        return {}, f"HKJC odds unavailable: {type(exc).__name__}"


def minmax(values: list[float], value: float | None) -> float | None:
    if value is None or not values:
        return None
    low, high = min(values), max(values)
    return 0.5 if math.isclose(low, high) else (value - low) / (high - low)


def smoothed_win_rate(row: dict[str, Any], wins_key: str, runs_key: str) -> float | None:
    """Use a Beta(1, 4) prior so thin public condition samples shrink safely."""
    wins, runs = number(row.get(wins_key)), number(row.get(runs_key))
    if wins is None or runs is None or runs < 0 or wins < 0 or wins > runs:
        return None
    return (wins + 1.0) / (runs + 5.0)


def score_rows(rows: list[dict[str, Any]]) -> None:
    rpr_values = [float(row["racing_post_rating"]) for row in rows if row.get("racing_post_rating") is not None]
    ts_values = [float(row["top_speed_rating"]) for row in rows if row.get("top_speed_rating") is not None]
    distance_values = [value for row in rows if (value := smoothed_win_rate(row, "distance_wins", "distance_runs")) is not None]
    going_values = [value for row in rows if (value := smoothed_win_rate(row, "similar_going_wins", "similar_going_runs")) is not None]
    course_values = [value for row in rows if (value := smoothed_win_rate(row, "course_wins", "course_runs")) is not None]
    for row in rows:
        parts: list[tuple[float, float]] = []
        component_specs = (
            (0.50, rpr_values, number(row.get("racing_post_rating"))),
            (0.25, ts_values, number(row.get("top_speed_rating"))),
            (0.10, distance_values, smoothed_win_rate(row, "distance_wins", "distance_runs")),
            (0.10, going_values, smoothed_win_rate(row, "similar_going_wins", "similar_going_runs")),
            (0.05, course_values, smoothed_win_rate(row, "course_wins", "course_runs")),
        )
        for weight, values, value in component_specs:
            scaled = minmax(values, value)
            if scaled is not None:
                parts.append((weight, scaled))
        if not parts:
            row["deep_composite_score"] = None
            continue
        denominator = sum(weight for weight, _ in parts)
        row["deep_composite_score"] = round(100.0 * sum(weight * score for weight, score in parts) / denominator, 2)
    ranked = sorted((row for row in rows if row.get("deep_composite_score") is not None), key=lambda item: (-float(item["deep_composite_score"]), int(item["runner_no"])))
    for rank, row in enumerate(ranked, start=1):
        row["deep_rank"] = rank


def number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def run_schema(conn: sqlite3.Connection, schema_path: Path) -> None:
    conn.executescript(schema_path.read_text(encoding="utf-8"))


def save_raw(raw_dir: Path, label: str, content: str | None) -> str | None:
    if content is None:
        return None
    raw_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    path = raw_dir / f"{label}_{digest[:16]}.html"
    if not path.exists():
        path.write_text(content, encoding="utf-8")
    return str(path)


def persist(db_path: Path, schema_path: Path, payload: dict[str, Any]) -> None:
    conn = sqlite3.connect(db_path)
    run_schema(conn, schema_path)
    run = payload["scrape_run"]
    conn.execute("""INSERT INTO deep_scrape_runs(meeting_date,simulcast_code,race_no,venue,status,n6_status,fetched_at_utc,racing_post_url,at_the_races_url,timeform_url,hkjc_odds_source,source_notes)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (run["meeting_date"], run["simulcast_code"], run["race_no"], run["venue"], run["status"], "disabled_non_hk", run["fetched_at_utc"], run["racing_post_url"], run["at_the_races_url"], run["timeform_url"], run["hkjc_odds_source"], run["source_notes"]))
    run_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    race = payload["race"]
    conn.execute("""INSERT INTO s1_races(scrape_run_id,meeting_date,simulcast_code,race_no,venue,local_start_time,hkt_start_time,race_name,distance_text,surface,going,race_class,declared_runners,source_status)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (run_id, race["meeting_date"], race["simulcast_code"], race["race_no"], race["venue"], race.get("local_start_time"), race.get("hkt_start_time"), race.get("race_name"), race.get("distance_text"), "Turf", race.get("going"), race.get("race_class"), race.get("declared_runners"), race["source_status"]))
    race_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    for row in payload["starters"]:
        cols = ("runner_no","draw_no","horse_name","official_rating","racing_post_rating","top_speed_rating","at_the_races_rating","sire","dam","damsire","pace_hint","distance_runs","distance_wins","similar_going_runs","similar_going_wins","course_runs","course_wins","hkjc_win_odds","hkjc_place_odds","deep_composite_score","deep_rank","data_completeness","source_rpr_ts_url","source_form_url","source_hkjc_odds_url")
        values = tuple(row.get(col) for col in cols)
        placeholders = ",".join("?" for _ in cols)
        conn.execute(f"INSERT INTO s1_starters(s1_race_id,{','.join(cols)}) VALUES(?,{placeholders})", (race_id, *values))
        starter_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        field_statuses = (
            ("racing_post_rating", "available_public" if row.get("racing_post_rating") is not None else "unavailable_parse", row.get("source_rpr_ts_url")),
            ("top_speed_rating", "available_public" if row.get("top_speed_rating") is not None else "unavailable_parse", row.get("source_rpr_ts_url")),
            ("at_the_races_form", "available_public" if row.get("distance_runs") is not None or row.get("similar_going_runs") is not None else "unavailable_parse", row.get("source_form_url")),
            ("timeform_pace_setup", "unavailable_paid_or_restricted", run.get("timeform_url")),
            ("timeform_tfr", "unavailable_paid_or_restricted", run.get("timeform_url")),
            ("hkjc_win_odds", "available_public" if row.get("hkjc_win_odds") is not None else "unavailable_parse", row.get("source_hkjc_odds_url") or run.get("hkjc_odds_source")),
        )
        for field_name, availability, source_url in field_statuses:
            conn.execute("INSERT INTO s1_source_field_status(s1_starter_id,field_name,availability,source_url,captured_at_utc) VALUES(?,?,?,?,?)", (starter_id, field_name, availability, source_url, run["fetched_at_utc"]))
    conn.commit(); conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="海外 S1/S2 公開深度資料原型（RPR/TS/公開 form；N6 停用）")
    parser.add_argument("--date", default="2026-08-19")
    parser.add_argument("--simulcast-code", default="S1")
    parser.add_argument("--race-no", type=int, default=1)
    parser.add_argument("--venue", default="York")
    parser.add_argument("--racing-post-url", default=DEFAULT_RP)
    parser.add_argument("--at-the-races-url", default=DEFAULT_ATR)
    parser.add_argument("--timeform-url", default=DEFAULT_TIMEFORM)
    parser.add_argument("--hkjc-odds-url", default=DEFAULT_HKJC_ODDS)
    parser.add_argument("--db", default="overseas_deep_racing.sqlite")
    parser.add_argument("--schema", default="schema_overseas_deep_racing.sql")
    parser.add_argument("--raw-dir", default="archive/overseas_deep_raw")
    parser.add_argument("--output", default="runtime/overseas_deep/york_s1_1_deep.json")
    parser.add_argument("--timeout", type=int, default=25)
    args = parser.parse_args()
    if not re.fullmatch(r"S\d+", args.simulcast_code.upper()) or args.race_no < 1:
        raise SystemExit("simulcast-code 必須為 S1/S2，race-no 必須為正整數")
    rp_html, rp_error = fetch_public(args.racing_post_url, args.timeout)
    atr_html, atr_error = fetch_public(args.at_the_races_url, args.timeout)
    rp_race, rp_rows = parse_racing_post(rp_html) if rp_html else ({}, {})
    atr_rows = parse_atr(atr_html) if atr_html else {}
    odds, odds_error = fetch_hkjc_odds(args.hkjc_odds_url, args.timeout)
    rows: list[dict[str, Any]] = []
    for key, rp in rp_rows.items():
        atr = atr_rows.get(key, {})
        hkjc = odds.get(key, {})
        merged = {**rp, **{name: value for name, value in atr.items() if name != "horse_name"}, "hkjc_win_odds": hkjc.get("win"), "hkjc_place_odds": hkjc.get("place"), "source_rpr_ts_url": args.racing_post_url, "source_form_url": args.at_the_races_url, "source_hkjc_odds_url": args.hkjc_odds_url if hkjc else None}
        present = sum(merged.get(field) is not None for field in ("racing_post_rating", "top_speed_rating", "at_the_races_rating", "distance_runs", "similar_going_runs", "hkjc_win_odds"))
        merged["data_completeness"] = "complete" if present >= 5 else ("partial" if present >= 2 else "degraded")
        rows.append(merged)
    score_rows(rows)
    status = "complete" if len(rows) >= 10 and not rp_error else ("partial" if rows else "failed")
    fetched_at = utc_now()
    notes = []
    if rp_error: notes.append(f"Racing Post: {rp_error}")
    if atr_error: notes.append(f"At The Races: {atr_error}")
    if odds_error: notes.append(odds_error)
    notes.append("Timeform premium/Pace fields are not accessed; retained as unavailable_paid_or_restricted.")
    payload = {
        "schema_version": "v10_overseas_deep_scraper_v1",
        "scrape_run": {"meeting_date": args.date, "simulcast_code": args.simulcast_code.upper(), "race_no": args.race_no, "venue": args.venue, "status": status, "n6_status": "disabled_non_hk", "fetched_at_utc": fetched_at, "racing_post_url": args.racing_post_url, "at_the_races_url": args.at_the_races_url, "timeform_url": args.timeform_url, "hkjc_odds_source": args.hkjc_odds_url, "source_notes": " | ".join(notes)},
        "race": {"meeting_date": args.date, "simulcast_code": args.simulcast_code.upper(), "race_no": args.race_no, "venue": args.venue, "local_start_time": "13:50 BST", "hkt_start_time": "20:50 HKT", "source_status": "complete" if rp_rows else "degraded", **rp_race},
        "n6_integration": {"status": "disabled_non_hk", "message": "S1/S2 uses overseas deep-data scoring only; HK-trained N6 Neural Score is not invoked."},
        "field_availability": {"rpr": "available_public" if rp_rows else "unavailable_parse", "top_speed": "available_public" if rp_rows else "unavailable_parse", "pace_setup": "unavailable_paid_or_restricted", "timeform_tfr": "unavailable_paid_or_restricted", "hkjc_odds": "available_public" if odds else "unavailable_parse"},
        "scoring_method": "Public-field min-max composite: RPR 50%, TS 25%, smoothed distance win-rate 10%, smoothed similar-going win-rate 10%, smoothed course win-rate 5%. Missing fields are reweighted, never imputed; condition rates use a Beta(1,4) prior. This is an overseas research score, not V10 probability/EV/Kelly.",
        "starters": rows,
        "raw_artifacts": {"racing_post": save_raw(Path(args.raw_dir), "racing_post", rp_html), "at_the_races": save_raw(Path(args.raw_dir), "at_the_races", atr_html)},
    }
    output = Path(args.output); output.parent.mkdir(parents=True, exist_ok=True); output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    persist(Path(args.db), Path(args.schema), payload)
    print(json.dumps({"status": status, "starters": len(rows), "ranked": sum(item.get("deep_rank") is not None for item in rows), "n6_status": "disabled_non_hk", "output": str(output), "warnings": notes}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
