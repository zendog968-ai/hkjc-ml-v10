#!/usr/bin/env python3
"""Audit leakage-safe overseas feature readiness after a backfill batch.

This program intentionally does not manufacture historic pre-race features from
post-race rows.  It initializes additive feature columns, counts only completed
races with a known scheduled start, and reports which fields would be usable for
future pre-race S1/S2 predictions.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from overseas_cold_start_priors import ensure_prediction_prior_columns
from overseas_feature_enrichment import ensure_enrichment_schema


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0] or 0)


def build_report(conn: sqlite3.Connection) -> dict[str, Any]:
    completed_with_start = scalar(conn, "SELECT COUNT(*) FROM overseas_races WHERE race_status='completed' AND scheduled_start_utc IS NOT NULL")
    completed_without_start = scalar(conn, "SELECT COUNT(*) FROM overseas_races WHERE race_status='completed' AND scheduled_start_utc IS NULL")
    complete_snapshot_pairs = scalar(conn, """
        SELECT COUNT(*) FROM (
          SELECT overseas_race_id
          FROM overseas_odds_snapshots
          WHERE status='complete' AND snapshot_label IN ('T_MINUS_15','T_MINUS_5')
          GROUP BY overseas_race_id
          HAVING COUNT(DISTINCT snapshot_label)=2
        )
    """)
    return {
        "schema_version": "v10.2_overseas_feature_readiness_v1",
        "generated_at_utc": utc_now(),
        "feature_schema_ready": True,
        "archive_coverage": {
            "meetings": scalar(conn, "SELECT COUNT(*) FROM overseas_meetings"),
            "races": scalar(conn, "SELECT COUNT(*) FROM overseas_races"),
            "completed_races_with_scheduled_start": completed_with_start,
            "completed_races_without_scheduled_start": completed_without_start,
            "starters_with_finish_position": scalar(conn, "SELECT COUNT(*) FROM overseas_starters WHERE finish_pos IS NOT NULL"),
            "starters_with_international_rating": scalar(conn, "SELECT COUNT(*) FROM overseas_starters WHERE international_rating IS NOT NULL AND rating_as_of_utc IS NOT NULL"),
            "starters_with_last_run_date": scalar(conn, "SELECT COUNT(*) FROM overseas_starters WHERE last_run_date IS NOT NULL"),
            "starters_with_going_history": scalar(conn, "SELECT COUNT(*) FROM overseas_starters WHERE going_history_json IS NOT NULL"),
            "starters_with_trainer_g1": scalar(conn, "SELECT COUNT(*) FROM overseas_starters WHERE trainer_g1_starts IS NOT NULL AND trainer_g1_as_of_utc IS NOT NULL"),
            "complete_t15_t5_snapshot_pairs": complete_snapshot_pairs,
            "stored_prerace_predictions": scalar(conn, "SELECT COUNT(*) FROM overseas_prerace_predictions"),
        },
        "feature_calculation_policy": {
            "future_prerace_predictions": "ready_to_use_only_pre_cutoff_completed_rows_with_scheduled_start",
            "historical_prediction_feature_rebuild": "not_run_without_saved_pre_race_card_and_model_timestamp",
            "reason": "Rebuilding RPR, rest, going, G1, weight, recent-top4 or odds-drop features after results without original pre-race provenance would create future-data leakage.",
        },
        "calibration_gate": {
            "minimum_complete_settled_overseas_races": 100,
            "current_complete_settled_overseas_races": completed_with_start,
            "status": "eligible_for_walk_forward" if completed_with_start >= 100 else "not_eligible_insufficient_time_valid_history",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="V10.2 海外回刷後特徵工程可用性稽核。")
    parser.add_argument("--db", default="hkjc_last_season.sqlite")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    conn = sqlite3.connect(args.db)
    try:
        ensure_enrichment_schema(conn)
        ensure_prediction_prior_columns(conn)
        report = build_report(conn)
    finally:
        conn.close()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
