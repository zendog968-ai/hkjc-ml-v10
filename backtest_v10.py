#!/usr/bin/env python3
"""Leakage-free rolling backtest for V10 historical win probabilities."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from v10_win_probability import Runner, V10WinModel


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="hkjc_last_season.sqlite")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--output", default="v10_backtest_summary.json")
    parser.add_argument("--temperature", type=float, default=1.8)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    races = conn.execute(
        """
        SELECT r.race_date, r.racecourse, r.race_no, r.distance_m, r.surface, r.course_config, r.going
        FROM races AS r
        JOIN starters AS s
          ON s.race_date=r.race_date AND s.racecourse=r.racecourse AND s.race_no=r.race_no
        WHERE r.race_status='completed'
        GROUP BY r.race_date,r.racecourse,r.race_no
        HAVING SUM(CASE WHEN s.finish_pos=1 THEN 1 ELSE 0 END)=1
           AND SUM(CASE WHEN s.finish_pos IS NOT NULL THEN 1 ELSE 0 END)>=6
        ORDER BY r.race_date DESC, r.race_no DESC
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()

    evaluated = []
    for race_row in reversed(races):
        runner_rows = conn.execute(
            """
            SELECT horse_name, draw, weight_lbs, jockey, trainer, finish_pos
            FROM starters
            WHERE race_date=? AND racecourse=? AND race_no=?
              AND finish_pos IS NOT NULL AND draw IS NOT NULL AND weight_lbs IS NOT NULL
            ORDER BY horse_no
            """,
            (race_row["race_date"], race_row["racecourse"], race_row["race_no"]),
        ).fetchall()
        if len(runner_rows) < 6:
            continue
        race = {
            "racecourse": race_row["racecourse"],
            "distance_m": race_row["distance_m"],
            "surface": race_row["surface"],
            "course_config": race_row["course_config"],
            "going": race_row["going"],
        }
        runners = [
            Runner(
                horse_name=row["horse_name"],
                draw=row["draw"],
                weight_lbs=row["weight_lbs"],
                jockey=row["jockey"],
                trainer=row["trainer"],
            )
            for row in runner_rows
        ]
        model = V10WinModel(
            args.db,
            lookback=6,
            as_of_date=race_row["race_date"],
            temperature=args.temperature,
        )
        try:
            predictions, _ = model.predict(race, runners)
        finally:
            model.close()
        winner = next(row["horse_name"] for row in runner_rows if row["finish_pos"] == 1)
        winner_prediction = next(item for item in predictions if item.horse_name == winner)
        brier = sum(
            (item.predicted_win_probability - (1.0 if item.horse_name == winner else 0.0)) ** 2
            for item in predictions
        )
        uniform_probability = 1.0 / len(runners)
        uniform_brier = sum(
            (uniform_probability - (1.0 if row["horse_name"] == winner else 0.0)) ** 2
            for row in runner_rows
        )
        evaluated.append(
            {
                "race_date": race_row["race_date"],
                "racecourse": race_row["racecourse"],
                "race_no": race_row["race_no"],
                "field_size": len(runners),
                "winner": winner,
                "winner_model_rank": winner_prediction.rank,
                "winner_predicted_probability": winner_prediction.predicted_win_probability,
                "top_pick": predictions[0].horse_name,
                "top_pick_won": predictions[0].horse_name == winner,
                "winner_in_model_top3": any(item.horse_name == winner for item in predictions[:3]),
                "brier_score": brier,
                "uniform_brier_score": uniform_brier,
            }
        )

    summary = {
        "model": "V10 Historical Win Probability v1.0",
        "method": "For each historical race, only data before that race date is used.",
        "temperature": args.temperature,
        "evaluated_races": len(evaluated),
        "top_pick_win_rate": sum(item["top_pick_won"] for item in evaluated) / len(evaluated),
        "top3_contains_winner_rate": sum(item["winner_in_model_top3"] for item in evaluated) / len(evaluated),
        "mean_winner_predicted_probability": sum(item["winner_predicted_probability"] for item in evaluated) / len(evaluated),
        "mean_race_brier_score": sum(item["brier_score"] for item in evaluated) / len(evaluated),
        "mean_uniform_brier_score": sum(item["uniform_brier_score"] for item in evaluated) / len(evaluated),
        "race_results": evaluated,
    }
    Path(args.output).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in summary.items() if key != "race_results"}, ensure_ascii=False, indent=2))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
