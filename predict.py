#!/usr/bin/env python3
"""V10.2 HKJC race prediction: ensemble win strength, place proxy and market audit.

The LightGBM + CatBoost ensemble uses only pre-race historical / official card features.
Odds snapshots are deliberately post-model market context: they add transparent reporting
labels but are not currently model-training features because insufficient labelled historical
snapshot coverage exists.
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
from catboost import Pool

from build_elo_features import (
    INITIAL_CLOSING, INITIAL_ELO, class_level, draw_band,
    race_closing_proxy, smoothed_rate, trainer_equipment_change_weight,
)
from equipment_features import equipment_feature_flags
from v102_feature_utils import (
    body_weight_features, cold_start_prior_score, distance_match_prior,
    is_new_horse_from_prior_starts, trial_prior,
)

WITHDRAWN = {"WV", "WV-A", "WX-A", "WXNR"}
PLACE_SIMULATION_BATCH_SIZE = 25_000
ODDS_DROP_SIGNAL_THRESHOLD = -0.20


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
        "racecourse": str(race["racecourse"]).upper(), "distance_m": int(race["distance_m"]),
        "surface": str(race["surface"]), "race_class": str(race.get("race_class") or "未知"),
        "course_config": str(race.get("course_config") or "未知"), "going": str(race.get("going") or "未知"),
        "race_date": str(race.get("race_date") or "").replace("/", "-"),
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
    row = conn.execute("SELECT rating FROM elo_current_state WHERE entity_type=? AND entity_key=?", (entity_type, key)).fetchone()
    return float(row[0]) if row else INITIAL_ELO


def history_rows(conn: sqlite3.Connection, horse: str, before_date: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT s.finish_pos, s.margin_lengths, s.weight_lbs, s.declared_weight_kg AS horse_body_weight_lbs,
               s.running_positions, r.race_class, COALESCE(e.equipment_raw, s.equipment) AS equipment,
               (SELECT COUNT(*) FROM starters sx WHERE sx.race_date=s.race_date AND sx.racecourse=s.racecourse
                 AND sx.race_no=s.race_no AND sx.finish_pos IS NOT NULL AND sx.finish_pos_text NOT IN ('WV','WV-A','WX-A','WXNR')) AS field_size
        FROM starters AS s JOIN races AS r ON r.race_date=s.race_date AND r.racecourse=s.racecourse AND r.race_no=s.race_no
        LEFT JOIN starter_equipment AS e ON e.race_date=s.race_date AND e.racecourse=s.racecourse AND e.race_no=s.race_no AND e.horse_name=s.horse_name
        WHERE s.horse_name=? AND r.race_status='completed' AND s.finish_pos IS NOT NULL
          AND s.finish_pos_text NOT IN ('WV','WV-A','WX-A','WXNR') AND (?='' OR s.race_date<?)
        ORDER BY s.race_date DESC, s.race_no DESC LIMIT 12
        """, (horse, before_date, before_date),
    ).fetchall()


def horse_rates(conn: sqlite3.Connection, horse: str, before_date: str) -> tuple[int, int, int]:
    row = conn.execute(
        """SELECT COUNT(*), SUM(CASE WHEN s.finish_pos=1 THEN 1 ELSE 0 END), SUM(CASE WHEN s.finish_pos<=3 THEN 1 ELSE 0 END)
        FROM starters s JOIN races r ON r.race_date=s.race_date AND r.racecourse=s.racecourse AND r.race_no=s.race_no
        WHERE r.race_status='completed' AND s.horse_name=? AND s.finish_pos IS NOT NULL
          AND s.finish_pos_text NOT IN ('WV','WV-A','WX-A','WXNR') AND (?='' OR s.race_date<?)""",
        (horse, before_date, before_date),
    ).fetchone()
    return int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)


def condition_rate(conn: sqlite3.Connection, horse: str, race: dict[str, Any]) -> tuple[int, int]:
    row = conn.execute(
        """SELECT COUNT(*), SUM(CASE WHEN s.finish_pos=1 THEN 1 ELSE 0 END)
        FROM starters s JOIN races r ON r.race_date=s.race_date AND r.racecourse=s.racecourse AND r.race_no=s.race_no
        WHERE r.race_status='completed' AND s.horse_name=? AND r.racecourse=? AND r.distance_m=? AND r.surface=?
          AND s.finish_pos IS NOT NULL AND s.finish_pos_text NOT IN ('WV','WV-A','WX-A','WXNR')
          AND (?='' OR s.race_date<?)""",
        (horse, race["racecourse"], race["distance_m"], race["surface"], race["race_date"], race["race_date"]),
    ).fetchone()
    return int(row[0] or 0), int(row[1] or 0)


def trainer_rate(conn: sqlite3.Connection, trainer: str, before_date: str) -> tuple[int, int]:
    row = conn.execute(
        """SELECT COUNT(*), SUM(CASE WHEN s.finish_pos=1 THEN 1 ELSE 0 END)
        FROM starters s JOIN races r ON r.race_date=s.race_date AND r.racecourse=s.racecourse AND r.race_no=s.race_no
        WHERE r.race_status='completed' AND s.trainer=? AND s.finish_pos IS NOT NULL
          AND s.finish_pos_text NOT IN ('WV','WV-A','WX-A','WXNR') AND (?='' OR s.race_date<?)""",
        (trainer, before_date, before_date),
    ).fetchone()
    return int(row[0] or 0), int(row[1] or 0)


def trainer_equipment_change_stats(conn: sqlite3.Connection, trainer: str, before_date: str) -> tuple[int, int]:
    rows = conn.execute(
        """SELECT s.horse_name, s.race_date, s.race_no, s.finish_pos, COALESCE(e.equipment_raw,s.equipment) AS equipment
        FROM starters s JOIN races r ON r.race_date=s.race_date AND r.racecourse=s.racecourse AND r.race_no=s.race_no
        LEFT JOIN starter_equipment e ON e.race_date=s.race_date AND e.racecourse=s.racecourse AND e.race_no=s.race_no AND e.horse_name=s.horse_name
        WHERE r.race_status='completed' AND s.trainer=? AND s.finish_pos IS NOT NULL
          AND s.finish_pos_text NOT IN ('WV','WV-A','WX-A','WXNR') AND (?='' OR s.race_date<?)
        ORDER BY s.horse_name,s.race_date,s.race_no""", (trainer, before_date, before_date),
    ).fetchall()
    previous_by_horse: dict[str, object] = {}; starts = wins = 0
    for row in rows:
        previous = previous_by_horse.get(str(row["horse_name"]))
        flags = equipment_feature_flags(row["equipment"], previous, previous is not None)
        if flags["equipment_changed"]:
            starts += 1; wins += int(row["finish_pos"] == 1)
        previous_by_horse[str(row["horse_name"])] = row["equipment"]
    return starts, wins


def track_bias_summary(conn: sqlite3.Connection, race: dict[str, Any]) -> dict[str, tuple[int, float]]:
    low, high = (0, 1200) if race["distance_m"] <= 1200 else (1201, 1650) if race["distance_m"] <= 1650 else (1651, 9999)
    rows = conn.execute(
        """SELECT s.finish_pos,s.draw,(SELECT COUNT(*) FROM starters sx WHERE sx.race_date=s.race_date AND sx.racecourse=s.racecourse AND sx.race_no=s.race_no AND sx.finish_pos IS NOT NULL AND sx.finish_pos_text NOT IN ('WV','WV-A','WX-A','WXNR')) AS field_size
        FROM starters s JOIN races r ON r.race_date=s.race_date AND r.racecourse=s.racecourse AND r.race_no=s.race_no
        WHERE r.race_status='completed' AND r.racecourse=? AND r.surface=? AND COALESCE(r.course_config,'未知')=?
          AND COALESCE(r.going,'未知')=? AND r.distance_m BETWEEN ? AND ? AND s.finish_pos IS NOT NULL
          AND s.finish_pos_text NOT IN ('WV','WV-A','WX-A','WXNR') AND (?='' OR s.race_date<?)""",
        (race["racecourse"], race["surface"], race["course_config"], race["going"], low, high, race["race_date"], race["race_date"]),
    ).fetchall()
    stats: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
    for row in rows:
        band = draw_band(row["draw"], int(row["field_size"])); stats[band][0] += 1; stats[band][1] += float(int(row["finish_pos"]) == 1); stats[band][2] += 1.0 / int(row["field_size"])
    # Same conservative two-stage shrunk form used by the feature builder.
    output: dict[str, tuple[int, float]] = {}
    for band, (starts, wins, expected) in stats.items():
        prior = 48.0
        if starts <= 0 or expected <= 0:
            output[band] = (int(starts), 1.0); continue
        base = expected / starts; prior_expected = max(base * prior, 1e-12)
        posterior = (wins + prior_expected) / (expected + prior_expected)
        bias = 1.0 + starts / (starts + prior) * (posterior - 1.0)
        output[band] = (int(starts), min(1.25, max(0.75, bias)))
    return output


def new_horse_prior(conn: sqlite3.Connection, horse_code: Any, race: dict[str, Any]) -> sqlite3.Row | None:
    code = str(horse_code or "").strip().upper()
    if not code:
        return None
    try:
        return conn.execute(
            """SELECT suggested_distance_text,latest_trial_position,latest_trial_margin_lengths,latest_trial_qualified
            FROM horse_new_horse_priors WHERE horse_code=? AND as_of_date<=? ORDER BY as_of_date DESC LIMIT 1""",
            (code, race["race_date"]),
        ).fetchone()
    except sqlite3.OperationalError:
        return None


def make_features(conn: sqlite3.Connection, race: dict[str, Any], runners: list[dict[str, Any]]) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    conn.row_factory = sqlite3.Row
    horse_ratings = [entity_rating(conn, "horse", str(row["horse_name"])) for row in runners]
    jockey_ratings = [entity_rating(conn, "jockey", str(row["jockey"])) for row in runners]
    horse_mean, jockey_mean, field_size = mean(horse_ratings), mean(jockey_ratings), len(runners)
    current_class, bias_summary = class_level(race["race_class"]), track_bias_summary(conn, race)
    model_rows: list[dict[str, Any]] = []; audit_rows: list[dict[str, Any]] = []
    for runner, horse_elo, jockey_elo in zip(runners, horse_ratings, jockey_ratings):
        horse, jockey, trainer = str(runner["horse_name"]), str(runner["jockey"]), str(runner["trainer"])
        history = history_rows(conn, horse, race["race_date"])
        starts, wins, top3 = horse_rates(conn, horse, race["race_date"])
        cond_starts, cond_wins = condition_rate(conn, horse, race)
        trainer_starts, trainer_wins = trainer_rate(conn, trainer, race["race_date"])
        closings = [race_closing_proxy(int(row["finish_pos"]), int(row["field_size"]), float(row["margin_lengths"] or 20.0), row["running_positions"]) for row in history]
        recent_finish = mean(int(row["finish_pos"]) / int(row["field_size"]) for row in history[:6]) if history else 0.5
        recent_margin = mean(float(row["margin_lengths"] or 20.0) for row in history[:6]) if history else 6.0
        recent_win = mean(int(int(row["finish_pos"]) == 1) for row in history[:5]) if history else 0.0
        closing_pre = mean(closings[:6]) if closings else INITIAL_CLOSING
        closing_trend = mean(closings[:3]) - mean(closings[:6]) if len(closings) >= 3 else 0.0
        last_weight = float(history[0]["weight_lbs"]) if history and history[0]["weight_lbs"] is not None else None
        last_body_weight = float(history[0]["horse_body_weight_lbs"]) if history and history[0]["horse_body_weight_lbs"] is not None else None
        prior_class = class_level(history[0]["race_class"]) if history else current_class
        previous_equipment = history[0]["equipment"] if history else None
        equipment_flags = equipment_feature_flags(runner.get("equipment"), previous_equipment, bool(history and previous_equipment is not None))
        body_features = body_weight_features(runner.get("horse_body_weight_lbs"), last_body_weight)
        is_new = is_new_horse_from_prior_starts(starts)
        prior = new_horse_prior(conn, runner.get("horse_code"), race) if is_new else None
        pedigree_match, pedigree_known = distance_match_prior(prior["suggested_distance_text"] if prior else None, race["distance_m"])
        trial_features = trial_prior(prior["latest_trial_position"] if prior else None, prior["latest_trial_margin_lengths"] if prior else None, prior["latest_trial_qualified"] if prior else None)
        cold_prior = cold_start_prior_score(pedigree_match, pedigree_known, trial_features["latest_trial_position_pre"], trial_features["latest_trial_margin_pre"], trial_features["latest_trial_qualified_pre"], trial_features["trial_prior_known_pre"])
        change_starts, change_wins = trainer_equipment_change_stats(conn, trainer, race["race_date"])
        class_drop = current_class - prior_class
        weight_delta = float(runner["weight_lbs"]) - last_weight if last_weight is not None else 0.0
        band = draw_band(int(runner["draw"]), field_size); track_sample, track_bias = bias_summary.get(band, (0, 1.0))
        condition_key = f"{horse}|{race['racecourse']}|{race['distance_m']}|{race['surface']}"
        row = {
            "distance_m": race["distance_m"], "field_size": field_size, "draw": int(runner["draw"]), "draw_pct": int(runner["draw"]) / field_size,
            "weight_lbs": float(runner["weight_lbs"]), "weight_delta": weight_delta, **body_features,
            "is_new_horse": is_new, "pedigree_distance_match_pre": pedigree_match, "pedigree_prior_known_pre": pedigree_known,
            **trial_features, "cold_start_prior_pre": cold_prior,
            "horse_elo_pre": horse_elo, "horse_condition_elo_pre": entity_rating(conn, "horse_condition", condition_key), "jockey_elo_pre": jockey_elo,
            "trainer_win_rate_pre": smoothed_rate(trainer_wins, trainer_starts, 0.08, 20.0), "horse_win_rate_pre": smoothed_rate(wins, starts, 0.08, 10.0),
            "horse_top3_rate_pre": smoothed_rate(top3, starts, 0.24, 10.0), "condition_win_rate_pre": smoothed_rate(cond_wins, cond_starts, 0.08, 5.0),
            "recent_finish_fraction_pre": recent_finish, "recent_margin_pre": recent_margin, "recent_win_rate_pre": recent_win,
            "closing400_proxy_pre": closing_pre, "closing400_trend_pre": closing_trend, "elo_vs_field": horse_elo - horse_mean,
            "jockey_elo_vs_field": jockey_elo - jockey_mean, "track_bias_pre": track_bias, "track_bias_sample_pre": track_sample,
            "class_level": current_class, "class_drop_from_last_pre": class_drop, "class_weight_interaction_pre": class_drop * weight_delta,
            **equipment_flags, "trainer_equip_change_roi_pre": trainer_equipment_change_weight(change_wins, change_starts, trainer_wins, trainer_starts) if equipment_flags["equipment_changed"] else 1.0,
            "trainer_equip_change_sample_pre": change_starts, "racecourse": race["racecourse"], "race_class": race["race_class"], "surface": race["surface"],
            "course_config": race["course_config"], "going": race["going"],
        }
        model_rows.append(row)
        audit_rows.append({
            "horse_name": horse, "historical_starts": starts, "condition_starts": cond_starts, "horse_elo": horse_elo, "jockey_elo": jockey_elo,
            "closing400_proxy": closing_pre, "track_bias": track_bias, "track_bias_sample": track_sample, "class_drop_from_last": class_drop,
            "equipment": runner.get("equipment"), "previous_equipment": previous_equipment, "is_first_time_blinker": equipment_flags["is_first_time_blinker"],
            "is_equip_added": equipment_flags["is_equip_added"], "equipment_changed": equipment_flags["equipment_changed"],
            "trainer_equip_change_roi": row["trainer_equip_change_roi_pre"], "trainer_equip_change_sample": change_starts,
            "horse_body_weight_lbs": body_features["horse_body_weight_pre"] if body_features["horse_body_weight_known_pre"] else None,
            "body_weight_delta": body_features["body_weight_delta_pre"] if body_features["body_weight_delta_known_pre"] else None,
            "is_extreme_body_weight_change": body_features["is_extreme_body_weight_change_pre"], "is_new_horse": is_new,
            "pedigree_prior_known": pedigree_known, "trial_prior_known": trial_features["trial_prior_known_pre"], "cold_start_prior": cold_prior,
            "data_warning": "新馬先驗資料不足" if is_new and not (pedigree_known or trial_features["trial_prior_known_pre"]) else ("樣本不足" if cond_starts < 2 or track_sample < 12 else "樣本充足"),
        })
    return pd.DataFrame(model_rows), audit_rows


def coerce_usable_odds(value: Any) -> float | None:
    if value is None or str(value).strip().upper() in {"", "0", "0.0", "SCR", "WV", "WXNR", "N/A", "NONE", "NULL", "-", "--"}:
        return None
    try: number = float(value)
    except (TypeError, ValueError): return None
    return number if number > 1.0 and math.isfinite(number) else None


def load_odds_overlay(path: str | None, market: str) -> tuple[dict[str, float], list[str]]:
    if not path: return {}, []
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8")); raw = payload.get(market, payload)
        if not isinstance(raw, dict): return {}, ["__market_section_unavailable__"]
    except (OSError, json.JSONDecodeError) as exc: return {}, [f"__overlay_unavailable__:{type(exc).__name__}"]
    valid: dict[str, float] = {}; invalid: list[str] = []
    for horse, odds in raw.items():
        value = coerce_usable_odds(odds)
        if value is None: invalid.append(str(horse))
        else: valid[clean_text(str(horse))] = value
    return valid, invalid


def load_snapshot(path: str | None) -> tuple[dict[str, dict[str, float | None]], dict[str, Any]]:
    if not path: return {}, {"status": "not_supplied"}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8")); odds = payload.get("odds", {})
        if not isinstance(odds, dict): raise ValueError("odds not object")
        return {clean_text(str(k)): v for k, v in odds.items() if isinstance(v, dict)}, {"status": payload.get("status", "unknown"), "label": payload.get("snapshot_label"), "captured_at_utc": payload.get("captured_at_utc"), "race": payload.get("race")}
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return {}, {"status": "unavailable", "warning": type(exc).__name__}


def declared_place_positions(field_size: int, racecourse: str) -> int:
    if field_size < 4: raise ValueError("少於 4 匹出賽馬，無法按標準位置派彩規則估算位置機率。")
    return 2 if racecourse.upper() in {"ST", "HV"} and field_size <= 6 else 3


def plackett_luce_place_probability(win_probability: np.ndarray, place_positions: int, simulations: int, seed: int) -> tuple[np.ndarray, np.ndarray, float]:
    if simulations < 1_000: raise ValueError("位置模擬次數必須至少為 1000。")
    strengths = np.clip(np.asarray(win_probability, dtype=float), 1e-12, None)
    if strengths.ndim != 1 or len(strengths) < 2: raise ValueError("位置模擬需要至少兩匹馬的有限勝率。")
    place_positions = min(max(int(place_positions), 1), len(strengths)); log_strengths = np.log(strengths)
    counts = np.zeros(len(strengths), dtype=np.int64); rng = np.random.default_rng(seed); started = time.perf_counter()
    for offset in range(0, simulations, PLACE_SIMULATION_BATCH_SIZE):
        batch_size = min(PLACE_SIMULATION_BATCH_SIZE, simulations - offset)
        scores = log_strengths + rng.gumbel(size=(batch_size, len(strengths)))
        top = np.argpartition(scores, -place_positions, axis=1)[:, -place_positions:]
        counts += np.bincount(top.ravel(), minlength=len(strengths))
    p = counts / float(simulations); return p, np.sqrt(p * (1.0 - p) / simulations), time.perf_counter() - started


def kelly_fraction(probability: float, odds: Optional[float]) -> tuple[Optional[float], float, float]:
    if odds is None or odds <= 1.0: return None, 0.0, 0.0
    ev = probability * odds - 1.0; full = max(0.0, ev / (odds - 1.0)); return ev, full, min(0.05, full * 0.25) if full else 0.0


def label_suggestion(probability: float, ev: Optional[float], starts: int, condition_starts: int, label: str) -> str:
    if starts < 3 or condition_starts < 1: return "樣本不足，僅供觀察"
    if ev is None: return f"等待{label}賠率後比較"
    if ev >= 0.10: return f"{label}模型相對看好；仍須留意臨場變化"
    if ev <= -0.10: return f"{label}市場定價偏高／不宜作主要選項"
    return f"{label}中性觀察"


def ensemble_probabilities(bundle: dict[str, Any], feature_df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    features = bundle["all_features"]
    if bundle.get("bundle_type") == "v10_2_ensemble":
        x_lgb = bundle["lightgbm_preprocessor"].transform(feature_df[features])
        lgb_raw = bundle["lightgbm_model"].predict(x_lgb)
        lgb_prob = bundle["lightgbm_calibrator"].predict(lgb_raw)
        pool = Pool(feature_df[features], cat_features=bundle["catboost_categorical_indices"])
        cat_raw = bundle["catboost_model"].predict(pool)
        cat_prob = bundle["catboost_calibrator"].predict(cat_raw)
        weights = bundle["ensemble_weights"]
        return lgb_prob, cat_prob, weights["lightgbm"] * lgb_prob + weights["catboost"] * cat_prob
    # V10.1 compatibility.
    x = bundle["preprocessor"].transform(feature_df[features])
    raw = bundle["model"].predict_proba(x)[:, 1]
    prob = bundle["calibrator"].predict(raw)
    return prob, np.full_like(prob, np.nan), prob


def predict(db_path: str, model_path: str, card_path: str, output_json: str, output_csv: str, win_odds_overlay_path: str | None = None, place_odds_overlay_path: str | None = None, early_snapshot_path: str | None = None, late_snapshot_path: str | None = None, place_simulations: int = 100_000, simulation_seed: int = 2026101) -> dict[str, Any]:
    race, runners = load_card(card_path); win_overlay, invalid_win = load_odds_overlay(win_odds_overlay_path, "win"); place_overlay, invalid_place = load_odds_overlay(place_odds_overlay_path, "place")
    early, early_meta = load_snapshot(early_snapshot_path); late, late_meta = load_snapshot(late_snapshot_path)
    for runner in runners:
        key = clean_text(str(runner["horse_name"])); runner["market_odds"] = win_overlay.get(key, runner.get("market_odds")); runner["place_odds"] = place_overlay.get(key, runner.get("place_odds"))
    bundle = joblib.load(model_path); conn = sqlite3.connect(db_path); feature_df, audit_rows = make_features(conn, race, runners); conn.close()
    lgb_prob, cat_prob, combined_prob = ensemble_probabilities(bundle, feature_df); win_probability = combined_prob / np.clip(combined_prob.sum(), 1e-12, None)
    place_positions = declared_place_positions(len(runners), race["racecourse"]); place_probability, place_se, simulation_seconds = plackett_luce_place_probability(win_probability, place_positions, place_simulations, simulation_seed)
    predictions: list[dict[str, Any]] = []; average = 1.0 / len(runners)
    for runner, audit, lgb, cat, raw, win_prob, place_prob, se in zip(runners, audit_rows, lgb_prob, cat_prob, combined_prob, win_probability, place_probability, place_se):
        key = clean_text(str(runner["horse_name"])); win_odds = coerce_usable_odds(runner.get("market_odds")); place_odds = coerce_usable_odds(runner.get("place_odds"))
        early_win = coerce_usable_odds((early.get(key) or {}).get("win")); late_win = coerce_usable_odds((late.get(key) or {}).get("win"))
        drop = (late_win - early_win) / early_win if early_win and late_win else None
        # Numerical tolerance ensures an exact -20% ratio such as 6.4 / 8.0 is not lost to binary floating-point rounding.
        drop_flag = bool(drop is not None and drop <= ODDS_DROP_SIGNAL_THRESHOLD + 1e-12)
        win_ev, win_full, win_quarter = kelly_fraction(float(win_prob), win_odds); place_ev, place_full, place_quarter = kelly_fraction(float(place_prob), place_odds)
        predictions.append({
            "horse_no": int(runner["horse_no"]) if runner.get("horse_no") is not None else None, "horse_name": runner["horse_name"], "horse_code": runner.get("horse_code"), "draw": int(runner["draw"]), "weight_lbs": float(runner["weight_lbs"]), "jockey": runner["jockey"], "trainer": runner["trainer"],
            "lightgbm_calibrated_probability": float(lgb), "catboost_calibrated_probability": None if np.isnan(cat) else float(cat), "ensemble_raw_probability": float(raw), "predicted_win_probability": float(win_prob), "model_heat_index": float(100.0 * win_prob / average),
            "market_odds": win_odds, "market_implied_probability": 1.0 / win_odds if win_odds else None, "ev_per_unit": win_ev, "kelly_full_fraction": win_full, "kelly_quarter_fraction_capped": win_quarter,
            "place_dividend_positions": place_positions, "predicted_place_probability": float(place_prob), "place_probability_standard_error": float(se), "place_probability_method": f"Plackett-Luce ensemble-strength proxy; {place_simulations} simulations", "place_market_odds": place_odds, "place_market_implied_probability": 1.0 / place_odds if place_odds else None, "place_ev_per_unit": place_ev, "place_kelly_full_fraction": place_full, "place_kelly_quarter_fraction_capped": place_quarter,
            "odds_t_minus_15": early_win, "odds_t_minus_5": late_win, "odds_drop_ratio": drop, "gate_money_drop_flag": drop_flag, "market_movement_label": "🔥 閘前資金落飛" if drop_flag else None,
            "historical_starts": audit["historical_starts"], "condition_starts": audit["condition_starts"], "horse_elo": audit["horse_elo"], "jockey_elo": audit["jockey_elo"], "closing400_proxy": audit["closing400_proxy"], "track_bias": audit["track_bias"], "track_bias_sample": audit["track_bias_sample"], "class_drop_from_last": audit["class_drop_from_last"],
            "equipment": audit["equipment"], "previous_equipment": audit["previous_equipment"], "is_first_time_blinker": audit["is_first_time_blinker"], "is_equip_added": audit["is_equip_added"], "equipment_changed": audit["equipment_changed"], "trainer_equip_change_roi": audit["trainer_equip_change_roi"], "trainer_equip_change_sample": audit["trainer_equip_change_sample"],
            "horse_body_weight_lbs": audit["horse_body_weight_lbs"], "body_weight_delta": audit["body_weight_delta"], "is_extreme_body_weight_change": audit["is_extreme_body_weight_change"], "is_new_horse": audit["is_new_horse"], "pedigree_prior_known": audit["pedigree_prior_known"], "trial_prior_known": audit["trial_prior_known"], "cold_start_prior": audit["cold_start_prior"], "data_warning": audit["data_warning"],
            "suggestion": label_suggestion(float(win_prob), win_ev, audit["historical_starts"], audit["condition_starts"], "市場"), "win_suggestion": label_suggestion(float(win_prob), win_ev, audit["historical_starts"], audit["condition_starts"], "獨贏"), "place_suggestion": label_suggestion(float(place_prob), place_ev, audit["historical_starts"], audit["condition_starts"], "位置"),
        })
    predictions.sort(key=lambda row: row["predicted_win_probability"], reverse=True)
    for rank, row in enumerate(predictions, 1): row["rank"] = rank
    horses = {clean_text(str(row["horse_name"])) for row in runners}
    result = {
        "model": "HKJC V10.2 LightGBM + CatBoost ensemble + place proxy", "model_feature_version": bundle.get("feature_version"), "race": race,
        "market_overlays": {"win_overlay_path": win_odds_overlay_path, "place_overlay_path": place_odds_overlay_path, "matched_win_odds": len(horses & set(win_overlay)), "matched_place_odds": len(horses & set(place_overlay)), "invalid_win_odds_keys": invalid_win, "invalid_place_odds_keys": invalid_place},
        "market_movement": {"early_snapshot_path": early_snapshot_path, "late_snapshot_path": late_snapshot_path, "early": early_meta, "late": late_meta, "odds_drop_formula": "(T_MINUS_5 win odds - T_MINUS_15 win odds) / T_MINUS_15 win odds", "signal_threshold": ODDS_DROP_SIGNAL_THRESHOLD, "important_note": "落飛僅描述公開賠率變動，並非大戶身份或內幕資訊的證明；現階段因歷史快照不足，未加入模型訓練特徵。"},
        "place_model": {"dividend_positions": place_positions, "simulation_count": place_simulations, "seed": simulation_seed, "simulation_elapsed_seconds": simulation_seconds, "simulation_batch_size": PLACE_SIMULATION_BATCH_SIZE, "method": "Plackett-Luce ranking simulation from ensemble race-relative win strengths; not a separately trained Place model."},
        "note": "賠率只作市場比較與雙快照標記。Win／Place EV 使用 EV=p×odds−1；模型輸出為研究性機率，並不構成勝出或回報保證。", "predictions": predictions,
    }
    Path(output_json).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    with Path(output_csv).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(predictions[0])); writer.writeheader(); writer.writerows(predictions)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="調用 V10.2 集成模型進行 Win／Place 機率、EV、賠率落飛與市場比較")
    parser.add_argument("--db", default="hkjc_last_season.sqlite"); parser.add_argument("--model", default="horse_model.pkl"); parser.add_argument("--race-card", required=True)
    parser.add_argument("--win-odds-overlay"); parser.add_argument("--place-odds-overlay")
    parser.add_argument("--odds-snapshot-early", help="V10.2：賽前15分鐘賠率快照"); parser.add_argument("--odds-snapshot-late", help="V10.2：賽前5分鐘賠率快照")
    parser.add_argument("--place-simulations", type=int, default=100_000); parser.add_argument("--simulation-seed", type=int, default=2026101)
    parser.add_argument("--output-json", default="prediction.json"); parser.add_argument("--output-csv", default="prediction.csv")
    args = parser.parse_args(); result = predict(args.db, args.model, args.race_card, args.output_json, args.output_csv, args.win_odds_overlay, args.place_odds_overlay, args.odds_snapshot_early, args.odds_snapshot_late, args.place_simulations, args.simulation_seed)
    print(json.dumps(result, ensure_ascii=False, indent=2)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
