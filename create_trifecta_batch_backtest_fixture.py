#!/usr/bin/env python3
"""Create synthetic, isolated data for batch trifecta backtest tests only."""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT / "trifecta_batch_fixture.sqlite"
CANDIDATE_ROOT = ROOT / "trifecta_batch_fixture_candidates"


def create_event(conn: sqlite3.Connection, code: str, snapshot_time: str, start_time: str, key: str, quote: float, winning_key: str, model_time: str) -> int:
    cur = conn.execute(
        """INSERT INTO pre_race_pool_events
        (meeting_date, meeting_racecourse, pool_type, pool_event_code, expected_leg_count)
        VALUES ('2026-09-06','ST','TRIFECTA_ORDERED',?,1)""",
        (code,),
    )
    event_id = int(cur.lastrowid)
    race_no = int(code[-1])
    conn.execute(
        """INSERT INTO pre_race_pool_event_legs
        (pool_event_id,leg_no,race_date,racecourse,race_no,scheduled_start_utc)
        VALUES (?,1,'2026-09-06','ST',?,?)""",
        (event_id, race_no, start_time),
    )
    cur = conn.execute(
        """INSERT INTO pre_race_pool_snapshots
        (pool_event_id,snapshot_label,captured_at_utc,anchor_leg_no,scheduled_anchor_start_utc,
         capture_delta_seconds,status,quote_completeness,source_file_path,payload_sha256,
         raw_payload_json,imported_at_utc)
        VALUES (?,'T_MINUS_15',?,1,?,0,'complete','full','synthetic.json',?, '{}',?)""",
        (event_id, snapshot_time, start_time, str(event_id) * 64, snapshot_time),
    )
    snapshot_id = int(cur.lastrowid)
    runner_values = [int(part.split("=")[1]) for part in key.split("|")]
    cur = conn.execute(
        """INSERT INTO pre_race_pool_selection_quotes
        (pool_snapshot_id,selection_key,selection_ordering,quote_kind,quoted_payout_tier,
         quote_value,quote_unit,quote_is_return_inclusive)
        VALUES (?,?,'ORDERED','ESTIMATED_DIVIDEND','MAIN',?,10.0,1)""",
        (snapshot_id, key, quote),
    )
    quote_id = int(cur.lastrowid)
    conn.executemany(
        """INSERT INTO pre_race_pool_selection_members
        (pool_selection_quote_id,leg_no,position_no,runner_no,horse_name)
        VALUES (?,1,?,?,?)""",
        [(quote_id, pos, runner_no, f"測試馬{runner_no}") for pos, runner_no in enumerate(runner_values, start=1)],
    )
    winning_values = [int(part.split("=")[1]) for part in winning_key.split("|")]
    conn.executemany(
        """INSERT INTO official_pool_result_members
        (pool_event_id,leg_no,finish_position,runner_no,horse_name)
        VALUES (?,1,?,?,?)""",
        [(event_id, pos, runner_no, f"結果馬{runner_no}") for pos, runner_no in enumerate(winning_values, start=1)],
    )
    conn.execute(
        """INSERT INTO official_pool_payouts
        (pool_event_id,payout_tier,winning_selection_key,payout_per_unit,payout_unit,
         payout_is_return_inclusive,result_source_url)
        VALUES (?,'MAIN',?,850.0,10.0,1,'https://example.invalid/result')""",
        (event_id, winning_key),
    )
    (CANDIDATE_ROOT / f"{code}.json").write_text(json.dumps({
        "pool_snapshot_id": snapshot_id,
        "model_generated_at_utc": model_time,
        "candidates": [{"selection_key": key, "predicted_hit_probability": 0.015, "stake": 10.0}],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return snapshot_id


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
    winning = "L1:P1=2|L1:P2=7|L1:P3=4"
    create_event(conn, "TEST-TRI-R3", "2026-09-06T08:15:00+00:00", "2026-09-06T08:30:00+00:00", winning, 850.0, winning, "2026-09-06T08:14:00+00:00")
    create_event(conn, "TEST-TRI-R4", "2026-09-06T09:15:00+00:00", "2026-09-06T09:30:00+00:00", "L1:P1=3|L1:P2=5|L1:P3=8", 500.0, "L1:P1=1|L1:P2=6|L1:P3=9", "2026-09-06T09:14:00+00:00")
    create_event(conn, "TEST-TRI-R5", "2026-09-06T10:15:00+00:00", "2026-09-06T10:30:00+00:00", "L1:P1=4|L1:P2=2|L1:P3=1", 600.0, "L1:P1=4|L1:P2=2|L1:P3=1", "2026-09-06T10:16:00+00:00")
    conn.commit()
    conn.close()
    print(f"Created synthetic batch fixture: {DB} and {CANDIDATE_ROOT}")


if __name__ == "__main__":
    main()
