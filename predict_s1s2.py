#!/usr/bin/env python3
"""Cold-start-safe V10.2 prediction for HKJC overseas S1/S2 simulcast races.

This intentionally does not reuse Hong Kong horse/jockey ELO for overseas runners.
It creates a transparent prior from only public pre-race career records. Missing
odds leave EV and Kelly null; missing career records use a neutral field prior.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def prior_strength(row: dict[str, Any]) -> tuple[float, str, str]:
    starts = row.get("career_starts")
    wins = row.get("career_wins")
    places = row.get("career_places")
    if not isinstance(starts, (int, float)) or starts <= 0 or not isinstance(wins, (int, float)) or not isinstance(places, (int, float)):
        return 0.120, "neutral_prior", "missing_or_unusable_offshore_history"
    starts = float(starts)
    # Beta-smoothed public career rates.  The score is relative only and will be
    # normalized within this field; it is not a claim of transportable HK ELO.
    win_rate = (float(wins) + 1.0) / (starts + 10.0)
    top3_rate = (float(places) + 3.0) / (starts + 10.0)
    score = max(0.015, 0.62 * win_rate + 0.38 * (top3_rate / 3.0))
    tier = "career_prior_20plus" if starts >= 20 else "career_prior_under_20"
    return score, tier, "public_overseas_career_prior"


def plackett_luce_probabilities(weights: np.ndarray, simulations: int, seed: int) -> tuple[np.ndarray, np.ndarray]:
    if len(weights) < 2:
        raise ValueError("至少需要兩匹有效馬才可計算場內相對機率。")
    normalized = weights / weights.sum()
    rng = np.random.default_rng(seed)
    # Exponential race keys generate a Plackett-Luce ordering without Python loops.
    keys = rng.exponential(scale=1.0 / normalized, size=(simulations, len(weights)))
    top3 = np.argpartition(keys, kth=min(2, len(weights) - 1), axis=1)[:, : min(3, len(weights))]
    place_counts = np.bincount(top3.ravel(), minlength=len(weights))
    return normalized, place_counts / simulations


def expected_value(probability: float, odds: Any) -> float | None:
    if not isinstance(odds, (int, float)) or float(odds) <= 1.0:
        return None
    return probability * float(odds) - 1.0


def capped_kelly(probability: float, odds: Any, cap: float) -> float | None:
    if not isinstance(odds, (int, float)) or float(odds) <= 1.0:
        return None
    b = float(odds) - 1.0
    raw = (probability * float(odds) - 1.0) / b
    return max(0.0, min(cap, raw))


def write_db(db_path: Path, race_id: int, generated_at: str, model_version: str, rows: list[dict[str, Any]]) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    for row in rows:
        conn.execute(
            """INSERT INTO overseas_prerace_predictions(overseas_race_id,generated_at_utc,model_version,horse_no,predicted_win_probability,predicted_place_probability,cold_start_tier,prior_source,win_odds_at_capture,place_odds_at_capture,win_ev,place_ev,kelly_fraction,odds_snapshot_status,odds_snapshot_at_utc,odds_drop_flag,source_json_path)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (race_id, generated_at, model_version, row["horse_no"], row["predicted_win_probability"], row["predicted_place_probability"], row["cold_start_tier"], row["prior_source"], row.get("win_odds"), row.get("place_odds"), row.get("win_ev"), row.get("place_ev"), row.get("kelly_fraction"), row["odds_snapshot_status"], row.get("odds_snapshot_at_utc"), int(row.get("odds_drop_flag", False)), row.get("source_json_path")),
        )
    conn.commit()
    conn.close()


def markdown_report(payload: dict[str, Any]) -> str:
    lines = ["# V10.2 S1/S2 海外轉播賽預測", "", "> **🌍 海外轉播賽 (S1/S2)：冷啟動先驗模式。** 本報告不會將香港 ELO 硬套用至海外馬匹；賠率不可用時，EV 及 Kelly 保持空白。", "", "| 馬號 | 馬匹 | 勝出率 | 位置率 | 獨贏賠率 | 獨贏 EV | 位置賠率 | 位置 EV | Kelly | 先驗 |", "|---:|---|---:|---:|---:|---:|---:|---:|---:|---|"]
    for row in payload["predictions"]:
        fmt = lambda value, pct=False: "—" if value is None else (f"{value:.2%}" if pct else f"{value:.2f}")
        lines.append(f"| {row['horse_no']} | {row['horse_name']} | {fmt(row['predicted_win_probability'], True)} | {fmt(row['predicted_place_probability'], True)} | {fmt(row.get('win_odds'))} | {fmt(row.get('win_ev'), True)} | {fmt(row.get('place_odds'))} | {fmt(row.get('place_ev'), True)} | {fmt(row.get('kelly_fraction'), True)} | {row['cold_start_tier']} |")
    lines += ["", "## 資料完整性", "", f"- 賽卡狀態：`{payload['input_status']}`。", f"- 賠率完整狀態：`{payload['odds_snapshot_status']}`。", "- EV 公式：`p × 香港賽馬會顯示派彩倍數 − 1`；只在該欄賠率可用時計算。", "- 此輸出是場內相對機率與研究訊號，並非收益保證。"]
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="V10.2 S1/S2 海外轉播賽冷啟動預測。")
    parser.add_argument("--race-card", required=True)
    parser.add_argument("--db", default="hkjc_last_season.sqlite")
    parser.add_argument("--model-version", default="V10.2-overseas-prior-v1")
    parser.add_argument("--simulations", type=int, default=100000)
    parser.add_argument("--seed", type=int, default=102)
    parser.add_argument("--kelly-cap", type=float, default=0.05)
    parser.add_argument("--output-json", default="s1s2_prediction.json")
    parser.add_argument("--output-md", default="s1s2_prediction.md")
    parser.add_argument("--no-write-db", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    input_path = Path(args.race_card)
    card = json.loads(input_path.read_text(encoding="utf-8"))
    race = card.get("race", {})
    race_id = race.get("overseas_race_id")
    if not isinstance(race_id, int):
        raise SystemExit("race_card 缺少 overseas_race_id；請先以 fetch_hkjc_s1s2.py 建立官方賽卡。")
    runners = [row for row in card.get("runners", []) if isinstance(row.get("horse_no"), int) and row.get("horse_name")]
    if len(runners) < 2:
        raise SystemExit("race_card 可用馬匹少於兩匹。")
    strengths, meta = [], []
    for runner in runners:
        score, tier, source = prior_strength(runner)
        strengths.append(score)
        meta.append((tier, source))
    win, place = plackett_luce_probabilities(np.asarray(strengths, dtype=float), max(args.simulations, 1000), args.seed)
    generated_at = utc_now()
    odds_status = card.get("status", "degraded")
    predictions = []
    for runner, win_p, place_p, (tier, source) in zip(runners, win, place, meta):
        win_odds = runner.get("win_odds")
        place_odds = runner.get("place_odds")
        result = {
            "horse_no": runner["horse_no"], "horse_name": runner["horse_name"], "predicted_win_probability": float(win_p), "predicted_place_probability": float(place_p),
            "win_odds": win_odds, "place_odds": place_odds, "win_ev": expected_value(float(win_p), win_odds), "place_ev": expected_value(float(place_p), place_odds),
            "kelly_fraction": capped_kelly(float(win_p), win_odds, args.kelly_cap), "cold_start_tier": tier, "prior_source": source,
            "odds_snapshot_status": odds_status, "odds_snapshot_at_utc": None, "odds_drop_flag": False, "source_json_path": str(input_path.resolve()),
        }
        predictions.append(result)
    predictions.sort(key=lambda row: row["predicted_win_probability"], reverse=True)
    output = {"schema_version": "v10.2_s1s2_prediction_v1", "label": "🌍 海外轉播賽 (S1/S2)", "generated_at_utc": generated_at, "model_version": args.model_version, "input_status": card.get("status"), "odds_snapshot_status": odds_status, "race": race, "simulations": max(args.simulations, 1000), "predictions": predictions, "data_warning": "海外馬匹未使用香港 ELO；此為公開生涯資料的冷啟動場內相對先驗。賠率空缺不計算 EV／Kelly。"}
    atomic_json(Path(args.output_json), output)
    Path(args.output_md).write_text(markdown_report(output), encoding="utf-8")
    if not args.no_write_db:
        write_db(Path(args.db), race_id, generated_at, args.model_version, predictions)
    print(json.dumps({"output_json": args.output_json, "output_md": args.output_md, "runners": len(predictions), "odds_status": odds_status}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
