#!/usr/bin/env python3
"""Read-only evidence inventory for the V10.2 short-sprint research overlay.

This utility does not train or modify the production V10.2 model.  It examines
leakage-safe historical rows already present in ``elo_feature_store`` and reports
whether 1000m large-field hypotheses have enough Hong Kong evidence to become a
candidate-only experiment.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any


def draw_band(draw: float, field_size: float) -> str:
    percentile = draw / max(field_size, 1.0)
    if percentile <= 1 / 3:
        return "內檔"
    if percentile <= 2 / 3:
        return "中檔"
    return "外檔"


def safe_float(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value) if value is not None else fallback
    except (TypeError, ValueError):
        return fallback


def metric_block(rows: list[sqlite3.Row]) -> dict[str, Any]:
    grouped: dict[str, dict[str, float]] = defaultdict(lambda: {"runners": 0.0, "wins": 0.0, "expected": 0.0})
    for row in rows:
        band = draw_band(safe_float(row["draw"]), safe_float(row["field_size"], 1.0))
        grouped[band]["runners"] += 1.0
        grouped[band]["wins"] += safe_float(row["target_win"])
        grouped[band]["expected"] += 1.0 / max(safe_float(row["field_size"], 1.0), 1.0)
    result: dict[str, Any] = {}
    for band, stats in sorted(grouped.items()):
        result[band] = {
            "runners": int(stats["runners"]),
            "wins": int(stats["wins"]),
            "expected_wins": round(stats["expected"], 3),
            "win_rate": round(stats["wins"] / max(stats["runners"], 1.0), 5),
            "lift_vs_uniform": round(stats["wins"] / stats["expected"], 4) if stats["expected"] else None,
        }
    return result


def rank_signal(rows: list[sqlite3.Row], column: str) -> dict[str, Any]:
    by_race: dict[tuple[str, str, int], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        by_race[(str(row["race_date"]), str(row["racecourse"]), int(row["race_no"]))].append(row)
    top1_wins = 0
    top3_contains_winner = 0
    valid = 0
    for race_rows in by_race.values():
        ordered = sorted(race_rows, key=lambda item: safe_float(item[column]), reverse=True)
        if not ordered:
            continue
        valid += 1
        top1_wins += int(safe_float(ordered[0]["target_win"]) == 1.0)
        top3_contains_winner += int(any(safe_float(item["target_win"]) == 1.0 for item in ordered[:3]))
    return {
        "races": valid,
        "top1_win_rate": round(top1_wins / valid, 5) if valid else None,
        "top3_contains_winner_rate": round(top3_contains_winner / valid, 5) if valid else None,
    }


def analyze(db_path: Path, min_field_size: int) -> dict[str, Any]:
    uri = f"file:{db_path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT race_date, racecourse, race_no, surface, course_config, going,
               field_size, draw, target_win, horse_elo_pre, elo_vs_field,
               closing400_proxy_pre, closing400_trend_pre, track_bias_pre,
               track_bias_sample_pre, weight_lbs, weight_delta
        FROM elo_feature_store
        WHERE distance_m=1000 AND field_size>=?
        ORDER BY race_date, racecourse, race_no, draw
        """,
        (min_field_size,),
    ).fetchall()
    conn.close()
    contexts: dict[str, int] = defaultdict(int)
    for row in rows:
        contexts[f"{row['racecourse']}|{row['surface']}|{row['course_config']}|{row['going']}"] += 1
    races = {(row["race_date"], row["racecourse"], row["race_no"]) for row in rows}
    reliable_bias_rows = sum(1 for row in rows if safe_float(row["track_bias_sample_pre"]) >= 48.0)
    return {
        "database": str(db_path),
        "scope": {"distance_m": 1000, "minimum_field_size": min_field_size},
        "races": len(races),
        "runners": len(rows),
        "contexts": dict(sorted(contexts.items())),
        "draw_band_outcomes": metric_block(rows),
        "track_bias_sample": {
            "rows_at_or_above_48": reliable_bias_rows,
            "share": round(reliable_bias_rows / len(rows), 5) if rows else None,
            "note": "48 is the production shrinkage prior-runner threshold; this is an evidence flag, not a new model weight.",
        },
        "univariate_race_ranking": {
            "elo_vs_field": rank_signal(rows, "elo_vs_field"),
            "closing400_proxy_pre": rank_signal(rows, "closing400_proxy_pre"),
            "track_bias_pre": rank_signal(rows, "track_bias_pre"),
        },
        "research_only": True,
        "warning": "Univariate ranks are descriptive only. They cannot justify a production feature-weight change without chronological, multivariate candidate retraining.",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--min-field-size", type=int, default=14)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = analyze(args.db, args.min_field_size)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
