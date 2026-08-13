#!/usr/bin/env python3
"""Predict a new HKJC race with V10.1 win and model-derived place market analysis.

Win probabilities come from the calibrated LightGBM win model. Place probabilities
are not a separately trained model: they are estimated from the same race-relative
win strengths with a deterministic Plackett-Luce ranking simulation. Market odds are
optional and never used as model features.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any, Optional

import joblib
import numpy as np
import pandas as pd

from build_elo_features import (
    INITIAL_CLOSING, INITIAL_ELO, class_level, draw_band,
    race_closing_proxy, smoothed_rate,
)

WITHDRAWN = {"WV", "WV-A", "WX-A", "WXNR"}
PLACE_SIMULATION_BATCH_SIZE = 25_000


def clean_text(value: str) -> str:
    return "".join(str(value or "").split())


def load_card(path: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    race = payload.get("race", {})
    required = {"racecourse", "distance_m", "surface"}
    missing = required - set(race)
    if missing:
        raise ValueError(f"race 欄位缺少：{', '.join(sorted(missing))}")
    race = {
        "racecourse": str(race["racecourse"]).upper(),
        "distance_m": int(race["distance_m"]),
        "surface": str(race["surface"]),
        "race_class": str(race.get("race_class") or "未知"),
        "course_config": str(race.get("course_config") or "未知"),
        "going": str(race.get("going") or "未知"),
    }
    runners = payload.get("runners", [])
    if not 2 <= len(runners) <= 20:
        raise ValueError("每場須提供 2 至 20 匹馬。")
    required_runner = {"horse_name", "draw", "weight_lbs", "jockey", "trainer"}
    for runner in runners:
        missing_runner = required_runner - set(runner)
        if missing_runner:
            raise ValueError(f"馬匹資料缺少：{', '.join(sorted(missing_runner))}")
    return race, runners


def entity_rating(conn: sqlite3.Connection, entity_type: str, key: str) -> float:
    row = conn.execute(
        "SELECT rating FROM elo_current_state WHERE entity_type=? AND entity_key=?", (entity_type, key)
    ).fetchone()
    return float(row[0]) if row else INITIAL_ELO


def history_rows(conn: sqlite3.Connection, horse: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT s.finish_pos, s.margin_lengths, s.weight_lbs, s.running_positions, r.race_class,
               (SELECT COUNT(*) FROM starters sx WHERE sx.race_date=s.race_date AND sx.racecourse=s.racecourse
                 AND sx.race_no=s.race_no AND sx.finish_pos IS NOT NULL AND sx.finish_pos_text NOT IN ('WV','WV-A','WX-A','WXNR')) AS field_size
        FROM starters AS s
        JOIN races AS r ON r.race_date=s.race_date AND r.racecourse=s.racecourse AND r.race_no=s.race_no
        WHERE s.horse_name=? AND r.race_status='completed' AND s.finish_pos IS NOT NULL
          AND s.finish_pos_text NOT IN ('WV','WV-A','WX-A','WXNR')
        ORDER BY s.race_date DESC, s.race_no DESC
        LIMIT 12
        """,
        (horse,),
    ).fetchall()


def horse_rates(conn: sqlite3.Connection, horse: str) -> tuple[int, int, int]:
    row = conn.execute(
        """
        SELECT COUNT(*), SUM(CASE WHEN s.finish_pos=1 THEN 1 ELSE 0 END),
               SUM(CASE WHEN s.finish_pos<=3 THEN 1 ELSE 0 END)
        FROM starters s JOIN races r ON r.race_date=s.race_date AND r.racecourse=s.racecourse AND r.race_no=s.race_no
        WHERE r.race_status='completed' AND s.horse_name=? AND s.finish_pos IS NOT NULL
          AND s.finish_pos_text NOT IN ('WV','WV-A','WX-A','WXNR')
        """,
        (horse,),
    ).fetchone()
    return (int(row[0] or 0), int(row[1] or 0), int(row[2] or 0))


def condition_rate(conn: sqlite3.Connection, horse: str, race: dict[str, Any]) -> tuple[int, int]:
    row = conn.execute(
        """
        SELECT COUNT(*), SUM(CASE WHEN s.finish_pos=1 THEN 1 ELSE 0 END)
        FROM starters s JOIN races r ON r.race_date=s.race_date AND r.racecourse=s.racecourse AND r.race_no=s.race_no
        WHERE r.race_status='completed' AND s.horse_name=? AND r.racecourse=? AND r.distance_m=? AND r.surface=?
          AND s.finish_pos IS NOT NULL AND s.finish_pos_text NOT IN ('WV','WV-A','WX-A','WXNR')
        """,
        (horse, race["racecourse"], race["distance_m"], race["surface"]),
    ).fetchone()
    return int(row[0] or 0), int(row[1] or 0)


def trainer_rate(conn: sqlite3.Connection, trainer: str) -> tuple[int, int]:
    row = conn.execute(
        """
        SELECT COUNT(*), SUM(CASE WHEN s.finish_pos=1 THEN 1 ELSE 0 END)
        FROM starters s JOIN races r ON r.race_date=s.race_date AND r.racecourse=s.racecourse AND r.race_no=s.race_no
        WHERE r.race_status='completed' AND s.trainer=? AND s.finish_pos IS NOT NULL
          AND s.finish_pos_text NOT IN ('WV','WV-A','WX-A','WXNR')
        """,
        (trainer,),
    ).fetchone()
    return int(row[0] or 0), int(row[1] or 0)


def track_bias_summary(conn: sqlite3.Connection, race: dict[str, Any]) -> dict[str, tuple[int, float]]:
    """Build historical draw-band bias for the supplied course conditions."""
    low, high = (0, 1200) if race["distance_m"] <= 1200 else (1201, 1650) if race["distance_m"] <= 1650 else (1651, 9999)
    rows = conn.execute(
        """
        SELECT s.finish_pos, s.draw,
               (SELECT COUNT(*) FROM starters sx WHERE sx.race_date=s.race_date AND sx.racecourse=s.racecourse
                 AND sx.race_no=s.race_no AND sx.finish_pos IS NOT NULL AND sx.finish_pos_text NOT IN ('WV','WV-A','WX-A','WXNR')) AS field_size
        FROM starters s JOIN races r ON r.race_date=s.race_date AND r.racecourse=s.racecourse AND r.race_no=s.race_no
        WHERE r.race_status='completed' AND r.racecourse=? AND r.surface=?
          AND COALESCE(r.course_config,'未知')=? AND COALESCE(r.going,'未知')=?
          AND r.distance_m BETWEEN ? AND ? AND s.finish_pos IS NOT NULL
          AND s.finish_pos_text NOT IN ('WV','WV-A','WX-A','WXNR')
        """,
        (race["racecourse"], race["surface"], race["course_config"], race["going"], low, high),
    ).fetchall()
    stats: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
    for row in rows:
        band = draw_band(row["draw"], int(row["field_size"]))
        stats[band][0] += 1.0
        stats[band][1] += float(int(row["finish_pos"]) == 1)
        stats[band][2] += 1.0 / int(row["field_size"])
    return {band: (int(values[0]), (values[1] + 4.0) / (values[2] + 4.0)) for band, values in stats.items()}


def make_features(conn: sqlite3.Connection, race: dict[str, Any], runners: list[dict[str, Any]]) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    conn.row_factory = sqlite3.Row
    horse_ratings = [entity_rating(conn, "horse", str(row["horse_name"])) for row in runners]
    jockey_ratings = [entity_rating(conn, "jockey", str(row["jockey"])) for row in runners]
    horse_mean = mean(horse_ratings)
    jockey_mean = mean(jockey_ratings)
    field_size = len(runners)
    current_class = class_level(race["race_class"])
    bias_summary = track_bias_summary(conn, race)
    model_rows: list[dict[str, Any]] = []
    audit_rows: list[dict[str, Any]] = []
    for runner, horse_elo, jockey_elo in zip(runners, horse_ratings, jockey_ratings):
        horse = str(runner["horse_name"])
        jockey = str(runner["jockey"])
        trainer = str(runner["trainer"])
        history = history_rows(conn, horse)
        starts, wins, top3 = horse_rates(conn, horse)
        cond_starts, cond_wins = condition_rate(conn, horse, race)
        trainer_starts, trainer_wins = trainer_rate(conn, trainer)
        closings = [
            race_closing_proxy(int(row["finish_pos"]), int(row["field_size"]), float(row["margin_lengths"] or 20.0), row["running_positions"])
            for row in history
        ]
        recent_finish = mean(int(row["finish_pos"]) / int(row["field_size"]) for row in history[:6]) if history else 0.5
        recent_margin = mean(float(row["margin_lengths"] or 20.0) for row in history[:6]) if history else 6.0
        recent_win = mean(int(int(row["finish_pos"]) == 1) for row in history[:5]) if history else 0.0
        closing_pre = mean(closings[:6]) if closings else INITIAL_CLOSING
        closing_trend = (mean(closings[:3]) - mean(closings[:6])) if len(closings) >= 3 else 0.0
        last_weight = float(history[0]["weight_lbs"]) if history and history[0]["weight_lbs"] is not None else None
        prior_class = class_level(history[0]["race_class"]) if history else current_class
        class_drop = current_class - prior_class
        weight_delta = (float(runner["weight_lbs"]) - last_weight) if last_weight is not None else 0.0
        runner_draw_band = draw_band(int(runner["draw"]), field_size)
        track_sample, track_bias = bias_summary.get(runner_draw_band, (0, 1.0))
        condition_key = f"{horse}|{race['racecourse']}|{race['distance_m']}|{race['surface']}"
        model_rows.append(
            {
                "distance_m": race["distance_m"], "field_size": field_size, "draw": int(runner["draw"]),
                "draw_pct": int(runner["draw"]) / field_size, "weight_lbs": float(runner["weight_lbs"]),
                "weight_delta": weight_delta, "horse_elo_pre": horse_elo,
                "horse_condition_elo_pre": entity_rating(conn, "horse_condition", condition_key), "jockey_elo_pre": jockey_elo,
                "trainer_win_rate_pre": smoothed_rate(trainer_wins, trainer_starts, 0.08, 20.0),
                "horse_win_rate_pre": smoothed_rate(wins, starts, 0.08, 10.0),
                "horse_top3_rate_pre": smoothed_rate(top3, starts, 0.24, 10.0),
                "condition_win_rate_pre": smoothed_rate(cond_wins, cond_starts, 0.08, 5.0),
                "recent_finish_fraction_pre": recent_finish, "recent_margin_pre": recent_margin,
                "recent_win_rate_pre": recent_win, "closing400_proxy_pre": closing_pre,
                "closing400_trend_pre": closing_trend, "elo_vs_field": horse_elo - horse_mean,
                "jockey_elo_vs_field": jockey_elo - jockey_mean, "track_bias_pre": track_bias,
                "track_bias_sample_pre": track_sample, "class_level": current_class,
                "class_drop_from_last_pre": class_drop, "class_weight_interaction_pre": class_drop * weight_delta,
                "racecourse": race["racecourse"], "race_class": race["race_class"], "surface": race["surface"],
                "course_config": race["course_config"], "going": race["going"],
            }
        )
        audit_rows.append(
            {
                "horse_name": horse, "historical_starts": starts, "condition_starts": cond_starts,
                "horse_elo": horse_elo, "jockey_elo": jockey_elo, "closing400_proxy": closing_pre,
                "track_bias": track_bias, "track_bias_sample": track_sample, "class_drop_from_last": class_drop,
                "data_warning": "樣本不足" if (cond_starts < 2 or track_sample < 12) else "樣本充足",
            }
        )
    return pd.DataFrame(model_rows), audit_rows


def coerce_usable_odds(value: Any) -> float | None:
    """Return a usable published payout multiple; null, 0, SCR and invalid values become None."""
    if value is None or str(value).strip().upper() in {"", "0", "0.0", "SCR", "WV", "WXNR", "N/A", "NONE", "NULL", "-", "--"}:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 1.0 and math.isfinite(number) else None


def load_odds_overlay(path: str | None, market: str) -> tuple[dict[str, float], list[str]]:
    """Load a flat / combined overlay; malformed or unavailable files degrade to no odds."""
    if not path:
        return {}, []
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return {}, ["__overlay_not_json_object__"]
        raw = payload.get(market, payload)
        if not isinstance(raw, dict):
            return {}, ["__market_section_unavailable__"]
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [f"__overlay_unavailable__:{type(exc).__name__}"]
    valid: dict[str, float] = {}
    invalid: list[str] = []
    for horse, odds in raw.items():
        value = coerce_usable_odds(odds)
        if value is None:
            invalid.append(str(horse))
            continue
        valid[clean_text(str(horse))] = value
    return valid, invalid


def declared_place_positions(field_size: int, racecourse: str) -> int:
    """HKJC place dividend count inferred from supplied declared starters.

    Local ST/HV races pay the first 3 for 7+ starters and first 2 for 4-6.
    Designated simulcast races may pay four places for 21+ starters.
    """
    if field_size < 4:
        raise ValueError("少於 4 匹出賽馬，無法按標準位置派彩規則估算位置機率。")
    if racecourse.upper() in {"ST", "HV"}:
        return 2 if field_size <= 6 else 3
    if field_size <= 6:
        return 2
    return 4 if field_size >= 21 else 3


def plackett_luce_place_probability(win_probability: np.ndarray, place_positions: int, simulations: int, seed: int) -> tuple[np.ndarray, np.ndarray, float]:
    """Estimate place probability with batched, fully vectorized NumPy top-k sampling.

    Gumbel-top-k samples Plackett-Luce rankings. ``argpartition`` finds unordered
    top-k membership rather than sorting every runner, and fixed-size batches cap
    memory. There are no Python loops over simulations or runners.
    """
    if simulations < 1_000:
        raise ValueError("位置模擬次數必須至少為 1000。")
    strengths = np.clip(np.asarray(win_probability, dtype=float), 1e-12, None)
    if strengths.ndim != 1 or len(strengths) < 2:
        raise ValueError("位置模擬需要至少兩匹馬的有限勝率。")
    place_positions = min(max(int(place_positions), 1), len(strengths))
    log_strengths = np.log(strengths)
    counts = np.zeros(len(strengths), dtype=np.int64)
    rng = np.random.default_rng(seed)
    started = time.perf_counter()
    for offset in range(0, simulations, PLACE_SIMULATION_BATCH_SIZE):
        batch_size = min(PLACE_SIMULATION_BATCH_SIZE, simulations - offset)
        scores = log_strengths + rng.gumbel(size=(batch_size, len(strengths)))
        top_indices = np.argpartition(scores, -place_positions, axis=1)[:, -place_positions:]
        counts += np.bincount(top_indices.ravel(), minlength=len(strengths))
    elapsed_seconds = time.perf_counter() - started
    probabilities = counts / float(simulations)
    standard_error = np.sqrt(probabilities * (1.0 - probabilities) / simulations)
    return probabilities, standard_error, elapsed_seconds


def kelly_fraction(probability: float, odds: Optional[float]) -> tuple[Optional[float], float, float]:
    if odds is None or odds <= 1.0:
        return None, 0.0, 0.0
    ev = probability * odds - 1.0
    full = max(0.0, ev / (odds - 1.0))
    quarter_capped = min(0.05, full * 0.25) if full > 0 else 0.0
    return ev, full, quarter_capped


def label_suggestion(probability: float, ev: Optional[float], starts: int, condition_starts: int, market_label: str) -> str:
    if starts < 3 or condition_starts < 1:
        return "樣本不足，僅供觀察"
    if ev is None:
        return f"等待{market_label}賠率後比較"
    if ev >= 0.10:
        return f"{market_label}模型相對看好；仍須留意臨場變化"
    if ev <= -0.10:
        return f"{market_label}市場定價偏高／不宜作主要選項"
    return f"{market_label}中性觀察"


def predict(
    db_path: str,
    model_path: str,
    card_path: str,
    output_json: str,
    output_csv: str,
    win_odds_overlay_path: str | None = None,
    place_odds_overlay_path: str | None = None,
    place_simulations: int = 100_000,
    simulation_seed: int = 2026101,
) -> dict[str, Any]:
    race, runners = load_card(card_path)
    win_overlay, invalid_win_odds = load_odds_overlay(win_odds_overlay_path, "win")
    place_overlay, invalid_place_odds = load_odds_overlay(place_odds_overlay_path, "place")
    # Inline race-card odds remain supported; explicit overlay has priority.
    for runner in runners:
        key = clean_text(str(runner["horse_name"]))
        if key in win_overlay:
            runner["market_odds"] = win_overlay[key]
        if key in place_overlay:
            runner["place_odds"] = place_overlay[key]
    bundle = joblib.load(model_path)
    conn = sqlite3.connect(db_path)
    feature_df, audit_rows = make_features(conn, race, runners)
    conn.close()
    x = bundle["preprocessor"].transform(feature_df[bundle["all_features"]])
    raw_probability = bundle["model"].predict_proba(x)[:, 1]
    calibrated_probability = bundle["calibrator"].predict(raw_probability)
    win_probability = calibrated_probability / calibrated_probability.sum()
    place_positions = declared_place_positions(len(runners), race["racecourse"])
    place_probability, place_standard_error, place_simulation_seconds = plackett_luce_place_probability(
        win_probability, place_positions, place_simulations, simulation_seed
    )
    field_average = 1.0 / len(runners)
    predictions: list[dict[str, Any]] = []
    for runner, audit, raw, calibrated, win_prob, place_prob, place_se in zip(
        runners, audit_rows, raw_probability, calibrated_probability, win_probability, place_probability, place_standard_error
    ):
        win_odds_value = coerce_usable_odds(runner.get("market_odds"))
        place_odds_value = coerce_usable_odds(runner.get("place_odds"))
        win_ev, win_kelly_full, win_kelly_quarter = kelly_fraction(float(win_prob), win_odds_value)
        place_ev, place_kelly_full, place_kelly_quarter = kelly_fraction(float(place_prob), place_odds_value)
        predictions.append(
            {
                "horse_name": runner["horse_name"], "draw": int(runner["draw"]), "weight_lbs": float(runner["weight_lbs"]),
                "jockey": runner["jockey"], "trainer": runner["trainer"],
                # Kept for backward compatibility with existing Win-only downstream files.
                "market_odds": win_odds_value, "raw_win_probability": float(raw),
                "calibrated_binary_probability": float(calibrated), "predicted_win_probability": float(win_prob),
                "model_heat_index": float(100.0 * win_prob / field_average),
                "market_implied_probability": (1.0 / win_odds_value) if win_odds_value else None,
                "ev_per_unit": win_ev, "kelly_full_fraction": win_kelly_full,
                "kelly_quarter_fraction_capped": win_kelly_quarter,
                "place_dividend_positions": place_positions,
                "predicted_place_probability": float(place_prob),
                "place_probability_standard_error": float(place_se),
                "place_probability_method": f"Plackett-Luce win-strength proxy; {place_simulations} simulations",
                "place_market_odds": place_odds_value,
                "place_market_implied_probability": (1.0 / place_odds_value) if place_odds_value else None,
                "place_ev_per_unit": place_ev, "place_kelly_full_fraction": place_kelly_full,
                "place_kelly_quarter_fraction_capped": place_kelly_quarter,
                "historical_starts": audit["historical_starts"], "condition_starts": audit["condition_starts"],
                "horse_elo": audit["horse_elo"], "jockey_elo": audit["jockey_elo"],
                "closing400_proxy": audit["closing400_proxy"], "track_bias": audit["track_bias"],
                "track_bias_sample": audit["track_bias_sample"], "class_drop_from_last": audit["class_drop_from_last"],
                "data_warning": audit["data_warning"],
                "win_suggestion": label_suggestion(float(win_prob), win_ev, audit["historical_starts"], audit["condition_starts"], "獨贏"),
                "place_suggestion": label_suggestion(float(place_prob), place_ev, audit["historical_starts"], audit["condition_starts"], "位置"),
            }
        )
    predictions.sort(key=lambda row: row["predicted_win_probability"], reverse=True)
    for rank, row in enumerate(predictions, start=1):
        row["rank"] = rank
    input_horses = {clean_text(str(row["horse_name"])) for row in runners}
    result = {
        "model": "HKJC V10.1 LightGBM v1 + place proxy", "race": race,
        "market_overlays": {
            "win_overlay_path": win_odds_overlay_path, "place_overlay_path": place_odds_overlay_path,
            "matched_win_odds": len(input_horses & set(win_overlay)), "matched_place_odds": len(input_horses & set(place_overlay)),
            "invalid_win_odds_keys": invalid_win_odds, "invalid_place_odds_keys": invalid_place_odds,
        },
        "place_model": {
            "dividend_positions": place_positions, "simulation_count": place_simulations, "seed": simulation_seed,
            "simulation_elapsed_seconds": place_simulation_seconds, "simulation_batch_size": PLACE_SIMULATION_BATCH_SIZE,
            "method": "Plackett-Luce ranking simulation from calibrated race-relative win strengths; not a separately trained Place model.",
            "rule_note": "以輸入的出賽馬數判定位置派彩名次；如有撤回馬，必須更新 race_card 後重新預測。",
        },
        "note": "市場賠率只作比較與 EV 計算，未用作模型特徵。獨贏及位置 EV 均以 EV=p×odds−1 計算，且不會再重複扣除已反映於公開派彩倍數內的彩池扣除比例。位置機率為由獨贏強度推導的模擬代理，並非獨立訓練的 Place 模型。",
        "predictions": predictions,
    }
    Path(output_json).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    columns = list(predictions[0].keys())
    with Path(output_csv).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(predictions)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="調用 horse_model.pkl 進行獨贏與位置機率、EV 及市場比較")
    parser.add_argument("--db", default="hkjc_last_season.sqlite")
    parser.add_argument("--model", default="horse_model.pkl")
    parser.add_argument("--race-card", required=True)
    parser.add_argument("--win-odds-overlay", help="可選：odds_overlay.json 或含 win 的合併覆蓋檔")
    parser.add_argument("--place-odds-overlay", help="可選：place_odds_overlay.json 或含 place 的合併覆蓋檔")
    parser.add_argument("--place-simulations", type=int, default=100_000, help="位置名次模擬次數，至少 1000")
    parser.add_argument("--simulation-seed", type=int, default=2026101, help="位置模擬隨機種子；相同輸入可重現")
    parser.add_argument("--output-json", default="prediction.json")
    parser.add_argument("--output-csv", default="prediction.csv")
    args = parser.parse_args()
    result = predict(
        args.db, args.model, args.race_card, args.output_json, args.output_csv,
        args.win_odds_overlay, args.place_odds_overlay, args.place_simulations, args.simulation_seed,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
