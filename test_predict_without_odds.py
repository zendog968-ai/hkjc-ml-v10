#!/usr/bin/env python3
"""Integration test for V10.1 predict.py when a race card has no market odds.

This verifies output behaviour and field integrity only. It does not claim to be a
leakage-free historical performance backtest; use train_lightgbm.py reports for that.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from pathlib import Path
from typing import Any

from predict import predict


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="模擬沒有即時賠率時的 V10.1 預測輸出")
    parser.add_argument("--db", default="hkjc_last_season.sqlite")
    parser.add_argument("--model", default="horse_model.pkl")
    parser.add_argument("--date", default="2026-07-15", help="歷史測試賽日 YYYY-MM-DD")
    parser.add_argument("--racecourse", default="HV", choices=["ST", "HV"], type=str.upper)
    parser.add_argument("--race-no", default=1, type=int)
    parser.add_argument("--output-dir", default="no_odds_test_output")
    return parser.parse_args()


def load_card_without_odds(conn: sqlite3.Connection, date: str, racecourse: str, race_no: int) -> tuple[dict[str, Any], dict[str, int]]:
    conn.row_factory = sqlite3.Row
    race = conn.execute(
        """
        SELECT race_date, racecourse, race_no, race_class, distance_m, surface, course_config, going
        FROM races WHERE race_date=? AND racecourse=? AND race_no=? AND race_status='completed'
        """,
        (date, racecourse, race_no),
    ).fetchone()
    if race is None:
        raise ValueError("找不到指定的已完成賽事；請以 --date、--racecourse、--race-no 指定另一場。")
    runners = conn.execute(
        """
        SELECT horse_no, horse_name, draw, weight_lbs, jockey, trainer, finish_pos
        FROM starters
        WHERE race_date=? AND racecourse=? AND race_no=?
          AND horse_no IS NOT NULL AND finish_pos IS NOT NULL
          AND finish_pos_text NOT IN ('WV','WV-A','WX-A','WXNR')
        ORDER BY horse_no
        """,
        (date, racecourse, race_no),
    ).fetchall()
    if len(runners) < 2:
        raise ValueError("指定賽事的有效出賽馬匹少於 2 匹。")
    actual_positions: dict[str, int] = {}
    payload_runners = []
    for row in runners:
        # Deliberately omit market_odds. This is the condition under test.
        payload_runners.append(
            {
                "horse_no": int(row["horse_no"]),
                "horse_name": row["horse_name"],
                "draw": int(row["draw"] or 0),
                "weight_lbs": float(row["weight_lbs"] or 0),
                "jockey": row["jockey"],
                "trainer": row["trainer"],
            }
        )
        actual_positions[row["horse_name"]] = int(row["finish_pos"])
    payload = {
        "race": {
            "race_date": race["race_date"],
            "racecourse": race["racecourse"],
            "race_no": int(race["race_no"]),
            "race_class": race["race_class"],
            "distance_m": int(race["distance_m"]),
            "surface": race["surface"],
            "course_config": race["course_config"],
            "going": race["going"],
        },
        "runners": payload_runners,
    }
    return payload, actual_positions


def assert_no_odds_contract(result: dict[str, Any], runner_count: int) -> dict[str, Any]:
    predictions = result.get("predictions", [])
    if len(predictions) != runner_count:
        raise AssertionError(f"輸出馬匹數不符：預期 {runner_count}，實際 {len(predictions)}")
    total_probability = sum(float(row["predicted_win_probability"]) for row in predictions)
    if not math.isclose(total_probability, 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise AssertionError(f"場內勝率未正規化至 1：{total_probability}")
    required = ["predicted_win_probability", "model_heat_index", "horse_elo", "jockey_elo", "data_warning", "suggestion"]
    for row in predictions:
        for key in required:
            if row.get(key) in (None, ""):
                raise AssertionError(f"無賠率模式缺少必要輸出欄位：{key}，馬匹：{row.get('horse_name')}")
        if row.get("market_odds") is not None:
            raise AssertionError("測試輸入無賠率，但輸出出現 market_odds。")
        if row.get("market_implied_probability") is not None or row.get("ev_per_unit") is not None:
            raise AssertionError("無賠率模式下 market_implied_probability 與 ev_per_unit 必須為 null。")
        if float(row.get("kelly_full_fraction", -1)) != 0.0 or float(row.get("kelly_quarter_fraction_capped", -1)) != 0.0:
            raise AssertionError("無賠率模式下 Kelly 比例必須為 0。")
        if row.get("suggestion") not in {"等待市場賠率後比較", "樣本不足，僅供觀察"}:
            raise AssertionError(f"無賠率模式出現不合規建議：{row.get('suggestion')}")
    return {
        "prediction_count": len(predictions),
        "probability_sum": total_probability,
        "odds_fields_null": True,
        "kelly_zero": True,
        "required_non_odds_fields_present": True,
    }


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(args.db)
    card, actual_positions = load_card_without_odds(conn, args.date, args.racecourse, args.race_no)
    conn.close()
    card_path = output_dir / "race_card_without_odds.json"
    result_path = output_dir / "prediction_without_odds.json"
    csv_path = output_dir / "prediction_without_odds.csv"
    card_path.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
    result = predict(args.db, args.model, str(card_path), str(result_path), str(csv_path))
    checks = assert_no_odds_contract(result, len(card["runners"]))
    top_pick = result["predictions"][0]
    summary = {
        "test_name": "predict.py 無即時賠率整合測試",
        "scope": "驗證無賠率時的欄位與輸出規則；不評估歷史預測準確度。",
        "input_race": card["race"],
        "checks": checks,
        "top_ranked_horse": top_pick["horse_name"],
        "historical_finish_position_for_reference_only": actual_positions.get(top_pick["horse_name"]),
        "output_files": {
            "race_card_without_odds": str(card_path),
            "prediction_json": str(result_path),
            "prediction_csv": str(csv_path),
        },
        "result": "PASS",
    }
    summary_path = output_dir / "test_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
