#!/usr/bin/env python3
"""Read-only verifier for the isolated overseas light-capture SQLite database."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def value(connection: sqlite3.Connection, statement: str) -> int | str:
    return connection.execute(statement).fetchone()[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    database = args.database.resolve()
    if database.name != "overseas_light_capture.sqlite" or "overseas_light_capture" not in database.parts:
        raise SystemExit("REFUSED: verifier only accepts the isolated overseas_light_capture database")
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        result = {
            "database": str(database),
            "integrity_check": value(connection, "PRAGMA integrity_check"),
            "foreign_key_violations": len(connection.execute("PRAGMA foreign_key_check").fetchall()),
            "capture_runs": value(connection, "SELECT COUNT(*) FROM capture_run"),
            "race_snapshots": value(connection, "SELECT COUNT(*) FROM race_snapshot"),
            "starter_rows": value(connection, "SELECT COUNT(*) FROM starter_market_snapshot"),
            "n6_status_values": [row[0] for row in connection.execute("SELECT DISTINCT n6_status FROM capture_run ORDER BY n6_status")],
            "formal_v10_changed_rows": value(connection, "SELECT COUNT(*) FROM capture_run WHERE formal_v10_changed != 0"),
            "model_or_ev_populated": value(connection, "SELECT COUNT(*) FROM starter_market_snapshot WHERE model_win_probability IS NOT NULL OR model_place_probability IS NOT NULL OR win_ev IS NOT NULL OR place_ev IS NOT NULL OR kelly_fraction IS NOT NULL"),
            "active_rows_missing_required_data": value(connection, "SELECT COUNT(*) FROM starter_market_snapshot WHERE is_scratched=0 AND (draw_no IS NULL OR weight_lbs IS NULL OR hkjc_win_odds IS NULL OR hkjc_place_odds IS NULL)"),
            "scratched_rows_with_data": value(connection, "SELECT COUNT(*) FROM starter_market_snapshot WHERE is_scratched=1 AND (draw_no IS NOT NULL OR weight_lbs IS NOT NULL OR hkjc_win_odds IS NOT NULL OR hkjc_place_odds IS NOT NULL)"),
        }
    finally:
        connection.close()
    result["passed"] = (
        result["integrity_check"] == "ok"
        and result["foreign_key_violations"] == 0
        and result["capture_runs"] >= 1
        and result["race_snapshots"] >= 1
        and result["starter_rows"] >= 2
        and result["n6_status_values"] == ["disabled_non_hk"]
        and result["formal_v10_changed_rows"] == 0
        and result["model_or_ev_populated"] == 0
        and result["active_rows_missing_required_data"] == 0
        and result["scratched_rows_with_data"] == 0
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
