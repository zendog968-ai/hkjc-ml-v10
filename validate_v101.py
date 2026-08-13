#!/usr/bin/env python3
"""Final integrity checks for the V10.1 feature store and trained model artifacts."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

REQUIRED_COLUMNS = [
    "track_bias_pre", "track_bias_sample_pre", "class_level",
    "class_drop_from_last_pre", "class_weight_interaction_pre",
]


def main() -> int:
    conn = sqlite3.connect("hkjc_last_season.sqlite")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(elo_feature_store)")}
    missing = sorted(set(REQUIRED_COLUMNS) - columns)
    if missing:
        raise SystemExit(f"缺少 V10.1 欄位：{missing}")
    feature_rows = conn.execute("SELECT COUNT(*) FROM elo_feature_store").fetchone()[0]
    null_counts = {
        column: conn.execute(f"SELECT COUNT(*) FROM elo_feature_store WHERE {column} IS NULL").fetchone()[0]
        for column in REQUIRED_COLUMNS
    }
    profile = json.loads(Path("feature_profile_comparison.json").read_text(encoding="utf-8"))
    model_report = json.loads(Path("lightgbm_training_report.json").read_text(encoding="utf-8"))
    result = {
        "feature_rows": feature_rows,
        "new_feature_null_counts": null_counts,
        "selected_profile": profile["selected_profile"],
        "model": model_report["model"],
        "feature_version": model_report["feature_version"],
        "test_race_metrics": model_report["test_race_metrics"],
        "all_checks_passed": not missing and all(value == 0 for value in null_counts.values()),
    }
    Path("v101_quality_report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
