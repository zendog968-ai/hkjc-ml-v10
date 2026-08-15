#!/usr/bin/env python3
"""Create synthetic isolated fixtures for Double Trio batch backtest tests only."""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT / "double_trio_batch_fixture.sqlite"
CANDIDATE_ROOT = ROOT / "double_trio_batch_fixture_candidates"


def key_for(leg1: tuple[int, int, int], leg2: tuple[int, int, int] | None = None) -> str:
    parts = [f"L1:P{i}={runner}" for i, runner in enumerate(sorted(leg1), start=1)]
    if leg2 is not None:
        parts.extend(f"L2:P{i}={runner}" for i, runner in enumerate(sorted(leg2), start=1))
    return "|".join(parts)


def add_event(
    conn: sqlite3.Connection,
    code: str,
    captured: str,
    start: str,
    candidate_leg1: tuple[int, int, int],
    candidate_leg2: tuple[int, int, int],
    actual_leg1: tuple[int, int, int],
    actual_leg2: tuple[int, int, int],
    model_generated: str,
    settlement: str,
) -> None:
    cur = conn.execute(
        """INSERT INTO pre_race_pool_events
        (meeting_date,meeting_racecourse,pool_type,pool_event_code,expected_leg_count)
        VALUES ('2026-09-06','ST','DOUBLE_TRIO',?,2)""",
        (code,),
    )
    event_id = int(cur.lastrowid)
    race_base = int(code[-1]) * 2
    conn.executemany(
        """INSERT INTO pre_race_pool_event_legs
        (pool_event_id,leg_no,race_date,racecourse,race_no,scheduled_start_utc)
        VALUES (?,?,'2026-09-06','ST',?,?)""",
        [(event_id, 1, race_base, start), (event_id, 2, race_base + 1, '2026-09-06T23:00:00+00:00')],
    )
    cur = conn.execute(
        """INSERT INTO pre_race_pool_snapshots
        (pool_event_id,snapshot_label,captured_at_utc,anchor_leg_no,scheduled_anchor_start_utc,
         capture_delta_seconds,status,quote_completeness,source_file_path,payload_sha256,
         raw_payload_json,imported_at_utc)
        VALUES (?,'T_MINUS_15',?,1,?,0,'complete','full','synthetic-dt.json',?, '{}',?)""",
        (event_id, captured, start, str(event_id) * 64, captured),
    )
    snapshot_id = int(cur.lastrowid)
    main_key = key_for(candidate_leg1, candidate_leg2)
    cur = conn.execute(
        """INSERT INTO pre_race_pool_selection_quotes
        (pool_snapshot_id,selection_key,selection_ordering,quote_kind,quoted_payout_tier,
         quote_value,quote_unit,quote_is_return_inclusive)
        VALUES (?,?,'LEGGED','ESTIMATED_DIVIDEND','MAIN',100000.0,10.0,1)""",
        (snapshot_id, main_key),
    )
    quote_id = int(cur.lastrowid)
    conn.executemany(
        """INSERT INTO pre_race_pool_selection_members
        (pool_selection_quote_id,leg_no,position_no,runner_no,horse_name)
        VALUES (?,?,?,?,?)""",
        [(quote_id, 1, pos, runner, f"馬{runner}") for pos, runner in enumerate(sorted(candidate_leg1), start=1)]
        + [(quote_id, 2, pos, runner, f"馬{runner}") for pos, runner in enumerate(sorted(candidate_leg2), start=1)],
    )
    # Persist actual finishing order deliberately not sorted to prove each Double Trio leg is unordered.
    conn.executemany(
        """INSERT INTO official_pool_result_members
        (pool_event_id,leg_no,finish_position,runner_no,horse_name)
        VALUES (?,?,?,?,?)""",
        [(event_id, 1, pos, runner, f"結果馬{runner}") for pos, runner in enumerate(actual_leg1, start=1)]
        + [(event_id, 2, pos, runner, f"結果馬{runner}") for pos, runner in enumerate(actual_leg2, start=1)],
    )
    actual_main_key = key_for(actual_leg1, actual_leg2)
    actual_consolation_key = key_for(actual_leg1)
    if settlement == "MAIN":
        conn.execute(
            """INSERT INTO official_pool_payouts
            (pool_event_id,payout_tier,winning_selection_key,payout_per_unit,payout_unit,
             payout_is_return_inclusive,result_source_url)
            VALUES (?,'MAIN',?,100000.0,10.0,1,'https://example.invalid/dt-main')""",
            (event_id, actual_main_key),
        )
    elif settlement == "CONSOLATION":
        conn.execute(
            """INSERT INTO official_pool_payouts
            (pool_event_id,payout_tier,winning_selection_key,payout_per_unit,payout_unit,
             payout_is_return_inclusive,result_source_url)
            VALUES (?,'CONSOLATION',?,5000.0,10.0,1,'https://example.invalid/dt-consolation')""",
            (event_id, actual_consolation_key),
        )
    elif settlement != "LOSS":
        raise ValueError(f"未知 settlement={settlement}")
    (CANDIDATE_ROOT / f"{code}.json").write_text(json.dumps({
        "pool_snapshot_id": snapshot_id,
        "model_generated_at_utc": model_generated,
        "candidates": [{"selection_key": main_key, "predicted_hit_probability": 0.0002, "stake": 10.0}],
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    if DB.exists():
        DB.unlink()
    if CANDIDATE_ROOT.exists():
        shutil.rmtree(CANDIDATE_ROOT)
    CANDIDATE_ROOT.mkdir()
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript((ROOT / "schema_prerace_odds_snapshots.sql").read_text(encoding="utf-8"))
    conn.executescript((ROOT / "schema_prerace_complex_pool_snapshots.sql").read_text(encoding="utf-8"))

    # Main hit: both unordered legs match despite different recorded finish order.
    add_event(conn, "TEST-DT-1", "2026-09-06T08:15:00+00:00", "2026-09-06T08:30:00+00:00", (2, 5, 9), (1, 4, 8), (9, 2, 5), (8, 1, 4), "2026-09-06T08:14:00+00:00", "MAIN")
    # Consolation: first leg matches; no MAIN payout exists; the second leg intentionally differs.
    add_event(conn, "TEST-DT-2", "2026-09-06T09:15:00+00:00", "2026-09-06T09:30:00+00:00", (1, 3, 7), (2, 6, 10), (7, 1, 3), (4, 5, 11), "2026-09-06T09:14:00+00:00", "CONSOLATION")
    # Loss: first leg does not match.
    add_event(conn, "TEST-DT-3", "2026-09-06T10:15:00+00:00", "2026-09-06T10:30:00+00:00", (2, 4, 6), (1, 7, 9), (3, 5, 8), (1, 7, 9), "2026-09-06T10:14:00+00:00", "LOSS")
    # Time inversion: excluded before settlement.
    add_event(conn, "TEST-DT-4", "2026-09-06T11:15:00+00:00", "2026-09-06T11:30:00+00:00", (1, 2, 3), (4, 5, 6), (1, 2, 3), (4, 5, 6), "2026-09-06T11:16:00+00:00", "MAIN")
    conn.commit()
    conn.close()
    print(f"Created synthetic Double Trio batch fixture: {DB} and {CANDIDATE_ROOT}")


if __name__ == "__main__":
    main()
