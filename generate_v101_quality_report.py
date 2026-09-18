#!/usr/bin/env python3
"""Create a reproducible V10.1 monthly-update quality report from actual artefacts."""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

REQUIRED_FEATURES = [
    "track_bias_pre",
    "track_bias_sample_pre",
    "class_level",
    "class_drop_from_last_pre",
    "class_weight_interaction_pre",
]


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--training-report", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    report_path = Path(args.training_report).resolve()
    output_path = Path(args.output).resolve()
    training = json.loads(report_path.read_text(encoding="utf-8"))

    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        columns = {row[1] for row in connection.execute("PRAGMA table_info(elo_feature_store)")}
        missing_columns = [column for column in REQUIRED_FEATURES if column not in columns]
        null_counts: dict[str, int | None] = {}
        feature_rows = int(connection.execute("SELECT COUNT(*) FROM elo_feature_store").fetchone()[0])
        for column in REQUIRED_FEATURES:
            if column in columns:
                null_counts[column] = int(connection.execute(
                    f"SELECT COUNT(*) FROM elo_feature_store WHERE {quote_identifier(column)} IS NULL"
                ).fetchone()[0])
            else:
                null_counts[column] = None
        dataset_last_race_date = connection.execute("SELECT MAX(race_date) FROM races").fetchone()[0]
        race_count = int(connection.execute("SELECT COUNT(*) FROM races").fetchone()[0])
        starter_count = int(connection.execute("SELECT COUNT(*) FROM starters").fetchone()[0])
    finally:
        connection.close()

    test_metrics = training.get("test_race_metrics", {})
    row_metrics = training.get("test_row_metrics", {})
    all_checks_passed = (
        integrity == "ok"
        and not foreign_key_violations
        and feature_rows > 0
        and not missing_columns
        and all(value == 0 for value in null_counts.values() if value is not None)
        and bool(test_metrics)
        and bool(row_metrics)
    )
    output = {
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "selected_profile": "expanded_v10_1",
        "model": training.get("model"),
        "feature_version": training.get("feature_version"),
        "dataset": {
            "database": str(db_path),
            "last_race_date": dataset_last_race_date,
            "race_count": race_count,
            "starter_count": starter_count,
            "feature_rows": feature_rows,
            "integrity_check": integrity,
            "foreign_key_violations": len(foreign_key_violations),
        },
        "new_feature_null_counts": null_counts,
        "missing_required_features": missing_columns,
        "time_ordered_test_metrics": {
            "split": training.get("split", {}).get("test", {}),
            "test_row_metrics": row_metrics,
            "test_race_metrics": test_metrics,
        },
        "all_checks_passed": all_checks_passed,
        "limitations": training.get("limitations", []),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if all_checks_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
