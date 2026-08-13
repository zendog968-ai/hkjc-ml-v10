#!/usr/bin/env python3
"""Quality checks for the V10.1 official-equipment feature integration."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import joblib

REQUIRED_FEATURES = {
    "is_first_time_blinker", "is_equip_added", "equipment_changed",
    "equipment_history_known_pre", "trainer_equip_change_roi_pre", "trainer_equip_change_sample_pre",
}


def scalar(conn: sqlite3.Connection, query: str) -> int:
    return int(conn.execute(query).fetchone()[0])


def main() -> int:
    parser = argparse.ArgumentParser(description="驗證 V10.1 裝備特徵整合")
    parser.add_argument("--db", default="hkjc_last_season.sqlite")
    parser.add_argument("--model", default="horse_model.pkl")
    parser.add_argument("--prediction", default="equipment_no_odds_test/prediction_without_odds.json")
    parser.add_argument("--output", default="equipment_integration_quality_report.json")
    args = parser.parse_args()
    conn = sqlite3.connect(args.db)
    starter_rows = scalar(conn, "SELECT COUNT(*) FROM starters")
    equipment_rows = scalar(conn, "SELECT COUNT(*) FROM starter_equipment")
    feature_rows = scalar(conn, "SELECT COUNT(*) FROM elo_feature_store")
    changed_rows = scalar(conn, "SELECT COUNT(*) FROM elo_feature_store WHERE equipment_changed=1")
    first_blinker_rows = scalar(conn, "SELECT COUNT(*) FROM elo_feature_store WHERE is_first_time_blinker=1")
    invalid_roi = scalar(conn, "SELECT COUNT(*) FROM elo_feature_store WHERE trainer_equip_change_roi_pre < 0.5 OR trainer_equip_change_roi_pre > 1.5")
    unknown_with_change = scalar(conn, "SELECT COUNT(*) FROM elo_feature_store WHERE equipment_history_known_pre=0 AND equipment_changed=1")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(elo_feature_store)")}
    conn.close()
    bundle = joblib.load(args.model)
    model_features = set(bundle["all_features"])
    prediction = json.loads(Path(args.prediction).read_text(encoding="utf-8"))
    prediction_fields = set(prediction["predictions"][0])
    expected_output = {"equipment", "previous_equipment", "is_first_time_blinker", "is_equip_added", "equipment_changed", "trainer_equip_change_roi"}
    failures: list[str] = []
    if not REQUIRED_FEATURES <= cols:
        failures.append("feature_store_missing_equipment_columns")
    if not REQUIRED_FEATURES <= model_features:
        failures.append("model_missing_equipment_features")
    if not expected_output <= prediction_fields:
        failures.append("prediction_missing_equipment_audit_fields")
    if invalid_roi:
        failures.append("trainer_equipment_weight_outside_bounds")
    if unknown_with_change:
        failures.append("unknown_prior_equipment_marked_as_changed")
    report = {
        "result": "PASS" if not failures else "FAIL",
        "starter_rows": starter_rows,
        "official_equipment_rows": equipment_rows,
        "official_equipment_coverage": equipment_rows / starter_rows if starter_rows else 0.0,
        "feature_rows": feature_rows,
        "equipment_changed_feature_rows": changed_rows,
        "first_time_blinker_feature_rows": first_blinker_rows,
        "trainer_equipment_weight_out_of_bounds": invalid_roi,
        "unknown_history_with_change_flag": unknown_with_change,
        "model_feature_version": bundle.get("feature_version"),
        "failures": failures,
    }
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
