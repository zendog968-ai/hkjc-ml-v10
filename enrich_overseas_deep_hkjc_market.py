#!/usr/bin/env python3
"""Add a verified HKJC Win/Place market snapshot to one overseas deep-data artifact.

This is deliberately separate from V10.2 and N6.  The deep-score probabilities
are an uncalibrated research proxy, clearly labelled as such, and are used only
when the complete official HKJC runner list matches the saved public overseas
racecard exactly.  No bet is placed or transmitted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from fetch_hkjc_live_odds import (
    DEFAULT_MIN_INTERVAL_SECONDS,
    atomic_json_write,
    enforce_min_interval,
    fetch_rendered_public_page,
    parse_visible_odds_table,
)
from fetch_overseas_deep_data import clean_name, fetch_public, norm, run_schema

ROOT = Path(__file__).resolve().parent
PROBABILITY_METHOD = "uncalibrated_deep_score_softmax_temperature_20_plackett_luce"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def parse_odd(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) <= 1.0:
        return None
    return float(value)


def expected_value(probability: float, odds: float | None) -> float | None:
    return None if odds is None else probability * odds - 1.0


def capped_kelly(probability: float, odds: float | None, cap: float) -> float | None:
    if odds is None:
        return None
    b = odds - 1.0
    raw = (probability * odds - 1.0) / b
    return max(0.0, min(cap, raw))


def plackett_luce_probabilities(scores: list[float], place_dividends: int, simulations: int, seed: int) -> tuple[list[float], list[float]]:
    if len(scores) < 2:
        raise ValueError("有效公開深度評分少於兩匹，無法建立場內研究機率。")
    array = np.asarray(scores, dtype=float)
    if not np.isfinite(array).all():
        raise ValueError("公開深度評分存在非有限值。")
    # The temperature keeps a min-max research score from becoming an unjustifiably
    # concentrated win probability.  This is a proxy until a time-ordered overseas
    # calibration cohort is available.
    weights = np.exp((array - float(array.max())) / 20.0)
    win = weights / weights.sum()
    rng = np.random.default_rng(seed)
    keys = rng.exponential(scale=1.0 / win, size=(max(simulations, 5000), len(win)))
    positions = np.argpartition(keys, kth=place_dividends - 1, axis=1)[:, :place_dividends]
    place = np.bincount(positions.ravel(), minlength=len(win)) / float(len(keys))
    if not math.isclose(float(win.sum()), 1.0, abs_tol=1e-12):
        raise ValueError("研究勝率未能守恆至 100%。")
    return [float(value) for value in win], [float(value) for value in place]


def save_raw(raw_dir: Path, html: str) -> str:
    raw_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(html.encode("utf-8")).hexdigest()
    path = raw_dir / f"hkjc_s1_market_{digest[:16]}.html"
    if not path.exists():
        path.write_text(html, encoding="utf-8")
    return str(path)


def persist_snapshot(db_path: Path, schema_path: Path, payload: dict[str, Any]) -> None:
    market = payload["market_research"]
    race = payload["race"]
    conn = sqlite3.connect(db_path)
    try:
        run_schema(conn, schema_path)
        conn.execute("PRAGMA foreign_keys = ON")
        cursor = conn.execute(
            """INSERT INTO s1_market_research_snapshots
               (meeting_date,simulcast_code,race_no,source_url,captured_at_utc,source_status,expected_runner_count,matched_runner_count,probability_method,probability_sum,n6_status,research_only,warning_text)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (race["meeting_date"], race["simulcast_code"], race["race_no"], market["source_url"], market["captured_at_utc"], market["status"], market["expected_runner_count"], market["matched_runner_count"], market["probability_method"], market.get("probability_sum"), "disabled_non_hk", 1, " | ".join(market.get("warnings", []))),
        )
        snapshot_id = int(cursor.lastrowid)
        for row in payload["starters"]:
            entry = row.get("market_research") or {}
            conn.execute(
                """INSERT INTO s1_market_research_entries
                   (s1_market_snapshot_id,runner_no,horse_name,hkjc_horse_name,match_status,win_odds,place_odds,deep_score,research_win_probability,research_place_probability,win_ev,place_ev,kelly_fraction,ev_kelly_status)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (snapshot_id, row["runner_no"], row["horse_name"], entry.get("hkjc_horse_name"), entry.get("match_status", "unmatched"), entry.get("win_odds"), entry.get("place_odds"), row.get("deep_composite_score"), entry.get("research_win_probability"), entry.get("research_place_probability"), entry.get("win_ev"), entry.get("place_ev"), entry.get("kelly_fraction"), entry.get("ev_kelly_status", "unavailable_identity_or_odds")),
            )
        conn.commit()
    finally:
        conn.close()


def enrich(payload: dict[str, Any], odds_by_name: dict[str, dict[str, float | None]], source_url: str, captured_at_utc: str, place_dividends: int, simulations: int, seed: int, kelly_cap: float, warnings: list[str]) -> dict[str, Any]:
    starters = [row for row in payload.get("starters", []) if isinstance(row, dict)]
    race = payload.get("race") or {}
    expected_keys = [norm(str(row.get("horse_name") or "")) for row in starters]
    if not starters or any(not key for key in expected_keys) or len(set(expected_keys)) != len(expected_keys):
        raise ValueError("海外深度工件的馬匹身份不完整或重複；已停止賠率整合。")
    parsed_keys = {norm(name): (name, values) for name, values in odds_by_name.items() if norm(name)}
    matched = [key for key in expected_keys if key in parsed_keys]
    full_identity_match = len(matched) == len(starters) and len(parsed_keys) == len(starters)
    status = "complete" if full_identity_match else "identity_mismatch"
    if not full_identity_match:
        warnings.append(f"HKJC官方賠率馬匹身份未完整一對一對應：深度工件 {len(starters)} 匹、HKJC表 {len(parsed_keys)} 匹、匹配 {len(matched)} 匹；EV／Kelly已停止。")

    if status == "complete":
        scores = [float(row.get("deep_composite_score")) for row in starters]
        win_probabilities, place_probabilities = plackett_luce_probabilities(scores, place_dividends, simulations, seed)
    else:
        win_probabilities, place_probabilities = [None] * len(starters), [None] * len(starters)

    for row, win_probability, place_probability, runner_key in zip(starters, win_probabilities, place_probabilities, expected_keys):
        hkjc_name, odds = parsed_keys.get(runner_key, (None, {}))
        win_odds = parse_odd(odds.get("win"))
        place_odds = parse_odd(odds.get("place"))
        available = status == "complete" and win_probability is not None and win_odds is not None and place_odds is not None
        if status == "complete":
            row["hkjc_win_odds"] = win_odds
            row["hkjc_place_odds"] = place_odds
            row["source_hkjc_odds_url"] = source_url
        row["market_research"] = {
            "hkjc_horse_name": hkjc_name,
            "match_status": "matched" if runner_key in parsed_keys else "unmatched",
            "win_odds": win_odds,
            "place_odds": place_odds,
            "research_win_probability": win_probability,
            "research_place_probability": place_probability,
            "win_ev": expected_value(float(win_probability), win_odds) if available else None,
            "place_ev": expected_value(float(place_probability), place_odds) if available else None,
            "kelly_fraction": capped_kelly(float(win_probability), win_odds, kelly_cap) if available else None,
            "ev_kelly_status": "available_research_only" if available else "unavailable_identity_or_odds",
        }
    field_availability = payload.setdefault("field_availability", {})
    field_availability["hkjc_odds"] = "available_public" if status == "complete" else "unavailable_parse"
    payload.setdefault("scrape_run", {})["hkjc_odds_source"] = source_url if status == "complete" else None
    payload["market_research"] = {
        "status": status,
        "source_url": source_url,
        "captured_at_utc": captured_at_utc,
        "raw_source_status": "available_public" if odds_by_name else "degraded",
        "expected_runner_count": len(starters),
        "matched_runner_count": len(matched),
        "probability_method": PROBABILITY_METHOD,
        "probability_sum": round(sum(value for value in win_probabilities if value is not None), 12) if status == "complete" else None,
        "place_dividends": place_dividends,
        "kelly_cap": kelly_cap,
        "n6_status": "disabled_non_hk",
        "research_only": True,
        "warnings": warnings + ["海外深度評分尚未完成時間外校準；機率、EV及Kelly只作研究性比較，並非V10.2正式機率、EV或Kelly。"],
    }
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="整合 HKJC 官方海外 Win／Place 賠率到獨立 S1 深度研究工件；N6 必須停用。")
    parser.add_argument("--deep-input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--odds-url", required=True)
    parser.add_argument("--db", default="overseas_deep_racing.sqlite")
    parser.add_argument("--schema", default="schema_overseas_deep_racing.sql")
    parser.add_argument("--raw-dir", default="archive/overseas_deep_raw")
    parser.add_argument("--html", help="離線官方 HKJC HTML；指定時不發出網絡請求。")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--min-interval", type=int, default=DEFAULT_MIN_INTERVAL_SECONDS)
    parser.add_argument("--state-file", default="runtime/overseas_deep/.hkjc_s1_market_request_state.json")
    parser.add_argument("--prefer-static-public-html", action="store_true", help="優先以單次靜態官方 HTML 讀取；只在公開表格可見時使用。")
    parser.add_argument("--place-dividends", type=int, default=4, choices=[2, 3, 4])
    parser.add_argument("--simulations", type=int, default=50000)
    parser.add_argument("--seed", type=int, default=20260819)
    parser.add_argument("--kelly-cap", type=float, default=0.05)
    args = parser.parse_args()
    if not 0.0 < args.kelly_cap <= 0.05:
        raise SystemExit("kelly-cap 必須介乎 0 與 0.05，以維持既有 V10 上限。")
    payload = json.loads(Path(args.deep_input).read_text(encoding="utf-8"))
    race = payload.get("race") or {}
    if race.get("simulcast_code") not in {"S1", "S2"} or payload.get("n6_integration", {}).get("status") != "disabled_non_hk":
        raise SystemExit("只接受明確 S1/S2 及 N6 disabled_non_hk 的海外工件。")
    warnings: list[str] = []
    html = None
    try:
        if args.html:
            html = Path(args.html).read_text(encoding="utf-8")
        else:
            state_path = Path(args.state_file)
            enforce_min_interval(state_path, max(args.min_interval, DEFAULT_MIN_INTERVAL_SECONDS))
            atomic_json_write(state_path, {"last_request_epoch": time.time(), "url": args.odds_url})
            if args.prefer_static_public_html:
                html, error = fetch_public(args.odds_url, args.timeout)
                if html is None:
                    raise RuntimeError(f"HKJC static public HTML unavailable: {error}")
                warnings.append("HKJC官方頁以靜態公開HTML讀取；未使用登入或互動式操作。")
            else:
                html = fetch_rendered_public_page(args.odds_url, args.timeout)
        odds_by_name, metadata = parse_visible_odds_table(html)
        warnings.append(f"HKJC公開 Win／Place 表已解析 {metadata.get('rows_parsed', 0)} 匹；缺失 Win {len(metadata.get('missing_win_odds', []))}、Place {len(metadata.get('missing_place_odds', []))}。")
    except Exception as exc:
        odds_by_name = {}
        warnings.append(f"HKJC公開賠率不可用：{type(exc).__name__}: {exc}；EV／Kelly已停止。")
    enriched = enrich(payload, odds_by_name, args.odds_url, utc_now(), args.place_dividends, args.simulations, args.seed, args.kelly_cap, warnings)
    if html is not None:
        enriched.setdefault("raw_artifacts", {})["hkjc_market"] = save_raw(Path(args.raw_dir), html)
    atomic_json(Path(args.output), enriched)
    persist_snapshot(Path(args.db), Path(args.schema), enriched)
    market = enriched["market_research"]
    available = sum(1 for row in enriched["starters"] if row.get("market_research", {}).get("ev_kelly_status") == "available_research_only")
    print(json.dumps({"status": market["status"], "matched": market["matched_runner_count"], "expected": market["expected_runner_count"], "ev_kelly_available": available, "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
