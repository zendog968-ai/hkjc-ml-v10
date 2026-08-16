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

from overseas_cold_start_priors import ensure_prediction_prior_columns, hierarchical_prior
from overseas_feature_enrichment import ensure_enrichment_schema, feature_enrichment, odds_drop_ratios


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


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
    ensure_prediction_prior_columns(conn)
    ensure_enrichment_schema(conn)
    for row in rows:
        conn.execute(
            """INSERT INTO overseas_prerace_predictions(overseas_race_id,generated_at_utc,model_version,horse_no,predicted_win_probability,predicted_place_probability,cold_start_tier,prior_source,win_odds_at_capture,place_odds_at_capture,win_ev,place_ev,kelly_fraction,odds_snapshot_status,odds_snapshot_at_utc,odds_drop_flag,source_json_path,prior_confidence,prior_uncertainty,prior_detail_json,international_rating,rating_type,days_since_last_run,going_suitability,trainer_g1_win_rate,odds_drop_ratio,odds_drop_weight,weight_lbs,field_weight_mean,weight_advantage_lbs,recent_top4_rate,recent_top4_starts,weight_log_signal,recent_top4_log_signal,feature_detail_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (race_id, generated_at, model_version, row["horse_no"], row["predicted_win_probability"], row["predicted_place_probability"], row["cold_start_tier"], row["prior_source"], row.get("win_odds"), row.get("place_odds"), row.get("win_ev"), row.get("place_ev"), row.get("kelly_fraction"), row["odds_snapshot_status"], row.get("odds_snapshot_at_utc"), int(row.get("odds_drop_flag", False)), row.get("source_json_path"), row["prior_confidence"], row["prior_uncertainty"], json.dumps(row["prior_detail"], ensure_ascii=False), row.get("international_rating"), row.get("rating_type"), row.get("days_since_last_run"), row.get("going_suitability"), row.get("trainer_g1_win_rate"), row.get("odds_drop_ratio"), row.get("odds_drop_weight"), row.get("weight_lbs"), row.get("field_weight_mean"), row.get("weight_advantage_lbs"), row.get("recent_top4_rate"), row.get("recent_top4_starts"), row.get("weight_log_signal"), row.get("recent_top4_log_signal"), json.dumps(row["feature_detail"], ensure_ascii=False)),
        )
    conn.commit()
    conn.close()


def markdown_report(payload: dict[str, Any]) -> str:
    lines = ["# V10.2 S1/S2 海外轉播賽預測", "", "> **🌍 海外轉播賽 (S1/S2)：冷啟動先驗模式。** 本報告不會將香港 ELO 硬套用至海外馬匹；賠率不可用時，EV 及 Kelly 保持空白。", "", "| 馬號 | 馬匹 | 勝出率 | 位置率 | 獨贏賠率 | 獨贏 EV | 位置賠率 | 位置 EV | Kelly | 先驗／信心 | 落飛 |", "|---:|---|---:|---:|---:|---:|---:|---:|---:|---|---|"]
    for row in payload["predictions"]:
        fmt = lambda value, pct=False: "—" if value is None else (f"{value:.2%}" if pct else f"{value:.2f}")
        lines.append(f"| {row['horse_no']} | {row['horse_name']} | {fmt(row['predicted_win_probability'], True)} | {fmt(row['predicted_place_probability'], True)} | {fmt(row.get('win_odds'))} | {fmt(row.get('win_ev'), True)} | {fmt(row.get('place_odds'))} | {fmt(row.get('place_ev'), True)} | {fmt(row.get('kelly_fraction'), True)} | {row['cold_start_tier']}／{fmt(row.get('prior_confidence'), True)} | {'🔥' if row.get('odds_drop_flag') else '—'} |")
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
    parser.add_argument("--odds-drop-log-weight", type=float, default=0.20, help="只有完整 T-15/T-5 同馬賠率急跌 >=20% 時套用的 S1/S2 實驗性 log-strength 權重；未校準時請設為 0。")
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
    generated_at = utc_now()
    prior_conn = sqlite3.connect(Path(args.db))
    prior_conn.execute("PRAGMA foreign_keys = ON")
    prior_conn.row_factory = sqlite3.Row
    strengths, meta, enrichments = [], [], []
    try:
        ensure_enrichment_schema(prior_conn)
        rating_values = [float(row["international_rating"]) for row in runners if isinstance(row.get("international_rating"), (int, float)) and str(row.get("rating_type") or "").upper() in {"RPR", "IFHA", "WORLD_RATING", "INTERNATIONAL_RATING"} and isinstance(row.get("rating_source_url"), str) and row.get("rating_source_url") and isinstance(row.get("rating_as_of_utc"), str) and row.get("rating_as_of_utc") <= generated_at]
        field_rating_mean = float(np.mean(rating_values)) if rating_values else None
        field_weights = [float(row["weight_lbs"]) for row in runners if isinstance(row.get("weight_lbs"), (int, float)) and float(row["weight_lbs"]) > 0]
        field_weight_mean = float(np.mean(field_weights)) if field_weights else None
        drops = odds_drop_ratios(prior_conn, race_id, generated_at)
        for runner in runners:
            prior = hierarchical_prior(prior_conn, runner, len(runners), generated_at)
            enrichment = feature_enrichment(prior_conn, {**race, "meeting_date": race.get("meeting_date")}, runner, len(runners), generated_at, field_rating_mean, field_weight_mean, drops.get(int(runner["horse_no"])), args.odds_drop_log_weight)
            strengths.append(prior.strength * math.exp(enrichment["log_strength_signal"]))
            meta.append(prior)
            enrichments.append(enrichment)
    finally:
        prior_conn.close()
    win, place = plackett_luce_probabilities(np.asarray(strengths, dtype=float), max(args.simulations, 1000), args.seed)
    odds_status = card.get("status", "degraded")
    predictions = []
    for runner, win_p, place_p, prior, enrichment in zip(runners, win, place, meta, enrichments):
        win_odds = runner.get("win_odds")
        place_odds = runner.get("place_odds")
        result = {
            "horse_no": runner["horse_no"], "horse_name": runner["horse_name"], "predicted_win_probability": float(win_p), "predicted_place_probability": float(place_p),
            "win_odds": win_odds, "place_odds": place_odds, "win_ev": expected_value(float(win_p), win_odds), "place_ev": expected_value(float(place_p), place_odds),
            "kelly_fraction": capped_kelly(float(win_p), win_odds, args.kelly_cap), "cold_start_tier": prior.tier, "prior_source": prior.source, "prior_confidence": prior.confidence, "prior_uncertainty": prior.uncertainty, "prior_detail": prior.detail,
            "odds_snapshot_status": odds_status, "odds_snapshot_at_utc": card.get("odds_snapshot_at_utc"), "odds_drop_flag": enrichment["odds_drop_flag"], "source_json_path": str(input_path.resolve()), "international_rating": enrichment["international_rating"], "rating_type": enrichment["rating_type"], "days_since_last_run": enrichment["days_since_last_run"], "going_suitability": enrichment["going_suitability"], "trainer_g1_win_rate": enrichment["trainer_g1_win_rate"], "weight_lbs": enrichment["weight_lbs"], "field_weight_mean": enrichment["field_weight_mean"], "weight_advantage_lbs": enrichment["weight_advantage_lbs"], "recent_top4_rate": enrichment["recent_top4_rate"], "recent_top4_starts": enrichment["recent_top4_starts"], "weight_log_signal": enrichment["weight_log_signal"], "recent_top4_log_signal": enrichment["recent_top4_log_signal"], "odds_drop_ratio": enrichment["odds_drop_ratio"], "odds_drop_weight": enrichment["odds_drop_weight"], "feature_detail": enrichment["detail"],
        }
        predictions.append(result)
    predictions.sort(key=lambda row: row["predicted_win_probability"], reverse=True)
    output = {"schema_version": "v10.2_s1s2_prediction_v1", "label": "🌍 海外轉播賽 (S1/S2)", "generated_at_utc": generated_at, "model_version": args.model_version, "input_status": card.get("status"), "odds_snapshot_status": odds_status, "race": race, "simulations": max(args.simulations, 1000), "predictions": predictions, "data_warning": "海外馬匹未使用香港 ELO；分層先驗及特徵強化只使用公開賽前欄位與預測時點前的海外 archive。未有驗證 RPR／IFHA、久休日期、場地／G1歷史或完整 T-15/T-5 快照時，對應訊號會退回中性。落飛權重屬海外實驗性校準，須以時間外資料驗證。賠率空缺不計算 EV／Kelly。"}
    atomic_json(Path(args.output_json), output)
    Path(args.output_md).write_text(markdown_report(output), encoding="utf-8")
    if not args.no_write_db:
        write_db(Path(args.db), race_id, generated_at, args.model_version, predictions)
    print(json.dumps({"output_json": args.output_json, "output_md": args.output_md, "runners": len(predictions), "odds_status": odds_status}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
