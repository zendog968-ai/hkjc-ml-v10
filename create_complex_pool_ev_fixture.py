#!/usr/bin/env python3
"""Create synthetic, isolated fixtures for query_complex_pool_ev.py tests only."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT / "complex_pool_ev_fixture.sqlite"


def add_event(conn: sqlite3.Connection, pool_type: str, code: str, legs: int) -> int:
    cur = conn.execute(
        """INSERT INTO pre_race_pool_events
        (meeting_date, meeting_racecourse, pool_type, pool_event_code, expected_leg_count)
        VALUES ('2026-09-06', 'ST', ?, ?, ?)""",
        (pool_type, code, legs),
    )
    return int(cur.lastrowid)


def add_snapshot(conn: sqlite3.Connection, event_id: int, captured: str, anchor: str, payload_hash: str) -> int:
    cur = conn.execute(
        """INSERT INTO pre_race_pool_snapshots
        (pool_event_id, snapshot_label, captured_at_utc, anchor_leg_no,
         scheduled_anchor_start_utc, capture_delta_seconds, status, quote_completeness,
         source_file_path, payload_sha256, raw_payload_json, imported_at_utc)
        VALUES (?, 'T_MINUS_15', ?, 1, ?, 0, 'complete', 'full',
                'synthetic_fixture.json', ?, '{}', ?)""",
        (event_id, captured, anchor, payload_hash, captured),
    )
    return int(cur.lastrowid)


def add_quote(
    conn: sqlite3.Connection, snapshot_id: int, key: str, ordering: str,
    tier: str, value: float, unit: float, members: list[tuple[int, int, int, str]],
) -> None:
    cur = conn.execute(
        """INSERT INTO pre_race_pool_selection_quotes
        (pool_snapshot_id, selection_key, selection_ordering, quote_kind,
         quoted_payout_tier, quote_value, quote_unit, quote_is_return_inclusive)
        VALUES (?, ?, ?, 'ESTIMATED_DIVIDEND', ?, ?, ?, 1)""",
        (snapshot_id, key, ordering, tier, value, unit),
    )
    quote_id = int(cur.lastrowid)
    conn.executemany(
        """INSERT INTO pre_race_pool_selection_members
        (pool_selection_quote_id, leg_no, position_no, runner_no, horse_name)
        VALUES (?, ?, ?, ?, ?)""",
        [(quote_id, *member) for member in members],
    )


def main() -> None:
    if DB.exists():
        DB.unlink()
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript((ROOT / "schema_prerace_odds_snapshots.sql").read_text(encoding="utf-8"))
    conn.executescript((ROOT / "schema_prerace_complex_pool_snapshots.sql").read_text(encoding="utf-8"))

    tri_event = add_event(conn, "TRIFECTA_ORDERED", "TEST-TRI-R03", 1)
    conn.execute(
        """INSERT INTO pre_race_pool_event_legs
        (pool_event_id, leg_no, race_date, racecourse, race_no, scheduled_start_utc)
        VALUES (?, 1, '2026-09-06', 'ST', 3, '2026-09-06T08:30:00+00:00')""",
        (tri_event,),
    )
    tri_snapshot = add_snapshot(conn, tri_event, "2026-09-06T08:15:00+00:00", "2026-09-06T08:30:00+00:00", "1" * 64)
    tri_key = "L1:P1=2|L1:P2=7|L1:P3=4"
    add_quote(conn, tri_snapshot, tri_key, "ORDERED", "MAIN", 850.0, 10.0, [(1, 1, 2, "測試甲"), (1, 2, 7, "測試乙"), (1, 3, 4, "測試丙")])

    six_event = add_event(conn, "SIX_UP", "TEST-6UP-R03-R08", 6)
    for leg_no, race_no in enumerate(range(3, 9), start=1):
        scheduled = f"2026-09-06T{9 + leg_no:02d}:00:00+00:00"
        conn.execute(
            """INSERT INTO pre_race_pool_event_legs
            (pool_event_id, leg_no, race_date, racecourse, race_no, scheduled_start_utc)
            VALUES (?, ?, '2026-09-06', 'ST', ?, ?)""",
            (six_event, leg_no, race_no, scheduled),
        )
    six_snapshot = add_snapshot(conn, six_event, "2026-09-06T10:45:00+00:00", "2026-09-06T11:00:00+00:00", "2" * 64)
    six_members = [(1, 1, 2, "六寶甲"), (2, 1, 7, "六寶乙"), (3, 1, 4, "六寶丙"), (4, 1, 8, "六寶丁"), (5, 1, 1, "六寶戊"), (6, 1, 3, "六寶己")]
    six_key = "L1:P1=2|L2:P1=7|L3:P1=4|L4:P1=8|L5:P1=1|L6:P1=3"
    add_quote(conn, six_snapshot, six_key, "LEGGED", "SIX_WIN_BONUS", 2_000_000.0, 2.0, six_members)
    conn.commit()
    conn.close()

    (ROOT / "fixture_trifecta_candidates.json").write_text(json.dumps({
        "pool_snapshot_id": tri_snapshot,
        "model_generated_at_utc": "2026-09-06T08:14:00+00:00",
        "candidates": [{"selection_key": tri_key, "predicted_hit_probability": 0.015, "stake": 10.0}],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (ROOT / "fixture_six_win_bonus_candidates.json").write_text(json.dumps({
        "pool_snapshot_id": six_snapshot,
        "model_generated_at_utc": "2026-09-06T10:44:00+00:00",
        "candidates": [{"selection_key": six_key, "predicted_hit_probability": 0.000002, "stake": 2.0}],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Created synthetic EV fixture: {DB}")


if __name__ == "__main__":
    main()
