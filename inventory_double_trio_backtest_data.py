#!/usr/bin/env python3
"""Read-only inventory for auditable historical V10 Double Trio backtests."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

POOL_TABLES = (
    "pre_race_pool_events",
    "pre_race_pool_snapshots",
    "pre_race_pool_selection_quotes",
    "official_pool_payouts",
    "official_pool_result_members",
)


def scalar(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> int:
    return int(conn.execute(query, params).fetchone()[0])


def existing_tables(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def real_prediction_files(runtime_root: Path) -> list[Path]:
    if not runtime_root.is_dir():
        return []
    return [path for path in runtime_root.rglob("prediction.json") if "fixture" not in path.parts and "test" not in path.parts]


def json_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def main() -> int:
    parser = argparse.ArgumentParser(description="盤點孖T四匹複式真實回測資料完整度")
    parser.add_argument("--db", default="hkjc_last_season.sqlite")
    parser.add_argument("--runtime-root", default="runtime/pre_race")
    parser.add_argument("--output", default="reports/double_trio_backtest/data_inventory.json")
    args = parser.parse_args()

    db_path = Path(args.db)
    runtime_root = Path(args.runtime_root)
    conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    tables = existing_tables(conn)
    missing_tables = [table for table in POOL_TABLES if table not in tables]
    table_counts = {table: scalar(conn, f"SELECT COUNT(*) FROM {table}") for table in POOL_TABLES if table in tables}
    pool_events: dict[str, Any] = {"double_trio_event_count": 0, "events_with_snapshot": 0, "events_with_official_top_three": 0, "events_with_official_payout": 0}
    if not missing_tables:
        pool_events["double_trio_event_count"] = scalar(conn, "SELECT COUNT(*) FROM pre_race_pool_events WHERE pool_type='DOUBLE_TRIO'")
        pool_events["events_with_snapshot"] = scalar(conn, """
            SELECT COUNT(DISTINCT e.pool_event_id)
            FROM pre_race_pool_events e JOIN pre_race_pool_snapshots s ON s.pool_event_id=e.pool_event_id
            WHERE e.pool_type='DOUBLE_TRIO' AND s.status='complete' AND s.expected_leg_count=2
        """)
        pool_events["events_with_official_top_three"] = scalar(conn, """
            SELECT COUNT(*) FROM (
              SELECT e.pool_event_id
              FROM pre_race_pool_events e
              JOIN official_pool_result_members r ON r.pool_event_id=e.pool_event_id
              WHERE e.pool_type='DOUBLE_TRIO' AND r.leg_no IN (1,2) AND r.finish_position BETWEEN 1 AND 3
              GROUP BY e.pool_event_id
              HAVING COUNT(*)=6
            )
        """)
        pool_events["events_with_official_payout"] = scalar(conn, """
            SELECT COUNT(DISTINCT e.pool_event_id)
            FROM pre_race_pool_events e JOIN official_pool_payouts p ON p.pool_event_id=e.pool_event_id
            WHERE e.pool_type='DOUBLE_TRIO' AND p.payout_tier IN ('MAIN','CONSOLATION')
        """)
    conn.close()

    prediction_files = real_prediction_files(runtime_root)
    valid_predictions = 0
    n6_enriched_predictions = 0
    for path in prediction_files:
        payload = json_object(path)
        if payload is None:
            continue
        valid_predictions += 1
        if isinstance(payload.get("n6_integration"), dict) and payload["n6_integration"].get("status") == "available":
            n6_enriched_predictions += 1

    ready_components = (
        not missing_tables
        and pool_events["double_trio_event_count"] > 0
        and pool_events["events_with_snapshot"] > 0
        and pool_events["events_with_official_top_three"] > 0
        and pool_events["events_with_official_payout"] > 0
        and n6_enriched_predictions >= 2
    )
    result = {
        "schema": "v10_double_trio_backtest_data_inventory_v1",
        "database": str(db_path),
        "read_only": True,
        "pool_tables_present": sorted(set(POOL_TABLES) - set(missing_tables)),
        "pool_tables_missing": missing_tables,
        "pool_table_counts": table_counts,
        "official_double_trio": pool_events,
        "pre_race_prediction_artifacts": {
            "runtime_root": str(runtime_root),
            "non_fixture_prediction_file_count": len(prediction_files),
            "valid_prediction_file_count": valid_predictions,
            "n6_enriched_prediction_file_count": n6_enriched_predictions,
            "minimum_required_per_event": 2,
        },
        "backtest_readiness": "ready" if ready_components else "not_ready",
        "gate_reason": (
            None if ready_components else "正式回測要求：官方孖T兩關、賽前快照、兩關各自 N6 聯合排名工件、官方頭三及派彩必須可完整稽核；任一缺失即不得計算勝率或 ROI。"
        ),
        "sample_policy": "少於15個已完整結算的官方孖T事件只可標示為探索性；少於1個則一律 N/A。",
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
