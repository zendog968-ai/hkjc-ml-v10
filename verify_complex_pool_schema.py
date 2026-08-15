#!/usr/bin/env python3
"""Isolated verification for the complex-pool schema, including Double Trio."""
from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
base = (ROOT / 'schema_prerace_odds_snapshots.sql').read_text(encoding='utf-8')
extension = (ROOT / 'schema_prerace_complex_pool_snapshots.sql').read_text(encoding='utf-8')
db = ROOT / 'complex_pool_schema_test.sqlite'
if db.exists():
    db.unlink()
conn = sqlite3.connect(db)
conn.execute('PRAGMA foreign_keys = ON')
conn.executescript(base)
conn.executescript(extension)

# Existing ordered TRIFECTA reference row.
cur = conn.execute("""INSERT INTO pre_race_pool_events(meeting_date,meeting_racecourse,pool_type,pool_event_code,expected_leg_count,source_url)
VALUES ('2026-09-06','ST','TRIFECTA_ORDERED','R03-TRI',1,'https://example.invalid/odds')""")
tri_event_id = cur.lastrowid
conn.execute("INSERT INTO pre_race_pool_event_legs(pool_event_id,leg_no,race_date,racecourse,race_no,scheduled_start_utc) VALUES (?,?,?,?,?,?)", (tri_event_id, 1, '2026-09-06', 'ST', 3, '2026-09-06T08:30:00+00:00'))
cur = conn.execute("""INSERT INTO pre_race_pool_snapshots(pool_event_id,snapshot_label,captured_at_utc,anchor_leg_no,scheduled_anchor_start_utc,capture_delta_seconds,status,quote_completeness,gross_pool_amount,source_file_path,payload_sha256,raw_payload_json,imported_at_utc)
VALUES (?, 'T_MINUS_15', '2026-09-06T08:15:00+00:00', 1, '2026-09-06T08:30:00+00:00', 0, 'complete', 'partial', 300000.0, 'fixture-tri.json', ?, '{}', '2026-09-06T08:15:05+00:00')""", (tri_event_id, 'a' * 64))
tri_snapshot_id = cur.lastrowid
cur = conn.execute("""INSERT INTO pre_race_pool_selection_quotes(pool_snapshot_id,selection_key,selection_ordering,quote_kind,quote_value,quote_unit,quote_is_return_inclusive)
VALUES (?, 'L1:P1=2|L1:P2=7|L1:P3=4', 'ORDERED', 'ESTIMATED_DIVIDEND', 850.0, 10.0, 1)""", (tri_snapshot_id,))
tri_quote_id = cur.lastrowid
conn.executemany("INSERT INTO pre_race_pool_selection_members VALUES (?,?,?,?,?)", [(tri_quote_id,1,1,2,'馬甲'),(tri_quote_id,1,2,7,'馬乙'),(tri_quote_id,1,3,4,'馬丙')])
conn.executemany("INSERT INTO official_pool_result_members VALUES (?,?,?,?,?)", [(tri_event_id,1,1,2,'馬甲'),(tri_event_id,1,2,7,'馬乙'),(tri_event_id,1,3,4,'馬丙')])
conn.execute("""INSERT INTO official_pool_payouts(pool_event_id,payout_tier,winning_selection_key,payout_per_unit,payout_unit,payout_is_return_inclusive,result_source_url)
VALUES (?, 'MAIN', 'L1:P1=2|L1:P2=7|L1:P3=4', 850.0, 10.0, 1, 'https://example.invalid/results')""", (tri_event_id,))

# DOUBLE_TRIO: each leg's three runners are unordered, so canonical key is ascending
# by runner number inside each leg, independent of official finishing order.
cur = conn.execute("""INSERT INTO pre_race_pool_events(meeting_date,meeting_racecourse,pool_type,pool_event_code,expected_leg_count,source_url)
VALUES ('2026-09-06','ST','DOUBLE_TRIO','R06-R07-DT',2,'https://example.invalid/double-trio')""")
dt_event_id = cur.lastrowid
conn.executemany("""INSERT INTO pre_race_pool_event_legs(pool_event_id,leg_no,race_date,racecourse,race_no,scheduled_start_utc)
VALUES (?,?,?,?,?,?)""", [
    (dt_event_id, 1, '2026-09-06', 'ST', 6, '2026-09-06T11:00:00+00:00'),
    (dt_event_id, 2, '2026-09-06', 'ST', 7, '2026-09-06T11:35:00+00:00'),
])
cur = conn.execute("""INSERT INTO pre_race_pool_snapshots(pool_event_id,snapshot_label,captured_at_utc,anchor_leg_no,scheduled_anchor_start_utc,capture_delta_seconds,status,quote_completeness,gross_pool_amount,carryover_amount,source_file_path,payload_sha256,raw_payload_json,imported_at_utc)
VALUES (?, 'T_MINUS_15', '2026-09-06T10:45:00+00:00', 1, '2026-09-06T11:00:00+00:00', 0, 'complete', 'partial', 2000000.0, 500000.0, 'fixture-dt.json', ?, '{}', '2026-09-06T10:45:05+00:00')""", (dt_event_id, 'b' * 64))
dt_snapshot_id = cur.lastrowid
dt_main_key = 'L1:P1=2|L1:P2=5|L1:P3=9|L2:P1=1|L2:P2=4|L2:P3=8'
cur = conn.execute("""INSERT INTO pre_race_pool_selection_quotes(pool_snapshot_id,selection_key,selection_ordering,quote_kind,quoted_payout_tier,quote_value,quote_unit,quote_is_return_inclusive)
VALUES (?, ?, 'LEGGED', 'ESTIMATED_DIVIDEND', 'MAIN', 120000.0, 10.0, 1)""", (dt_snapshot_id, dt_main_key))
dt_main_quote_id = cur.lastrowid
conn.executemany("INSERT INTO pre_race_pool_selection_members VALUES (?,?,?,?,?)", [
    (dt_main_quote_id, 1, 1, 2, '孖T甲'), (dt_main_quote_id, 1, 2, 5, '孖T乙'), (dt_main_quote_id, 1, 3, 9, '孖T丙'),
    (dt_main_quote_id, 2, 1, 1, '孖T丁'), (dt_main_quote_id, 2, 2, 4, '孖T戊'), (dt_main_quote_id, 2, 3, 8, '孖T己'),
])
# Separate first-leg-only consolation selection, as it cannot be treated as MAIN.
dt_consolation_key = 'L1:P1=2|L1:P2=5|L1:P3=9'
cur = conn.execute("""INSERT INTO pre_race_pool_selection_quotes(pool_snapshot_id,selection_key,selection_ordering,quote_kind,quoted_payout_tier,quote_value,quote_unit,quote_is_return_inclusive)
VALUES (?, ?, 'LEGGED', 'ESTIMATED_DIVIDEND', 'CONSOLATION', 5000.0, 10.0, 1)""", (dt_snapshot_id, dt_consolation_key))
dt_consolation_quote_id = cur.lastrowid
conn.executemany("INSERT INTO pre_race_pool_selection_members VALUES (?,?,?,?,?)", [
    (dt_consolation_quote_id, 1, 1, 2, '孖T甲'), (dt_consolation_quote_id, 1, 2, 5, '孖T乙'), (dt_consolation_quote_id, 1, 3, 9, '孖T丙'),
])
# Actual finish order deliberately differs, but same top-three runner sets are represented.
conn.executemany("INSERT INTO official_pool_result_members VALUES (?,?,?,?,?)", [
    (dt_event_id, 1, 1, 9, '孖T丙'), (dt_event_id, 1, 2, 2, '孖T甲'), (dt_event_id, 1, 3, 5, '孖T乙'),
    (dt_event_id, 2, 1, 8, '孖T己'), (dt_event_id, 2, 2, 1, '孖T丁'), (dt_event_id, 2, 3, 4, '孖T戊'),
])
conn.executemany("""INSERT INTO official_pool_payouts(pool_event_id,payout_tier,winning_selection_key,payout_per_unit,payout_unit,payout_is_return_inclusive,result_source_url)
VALUES (?,?,?,?,?,?,?)""", [
    (dt_event_id, 'MAIN', dt_main_key, 120000.0, 10.0, 1, 'https://example.invalid/dt-result'),
    (dt_event_id, 'CONSOLATION', dt_consolation_key, 5000.0, 10.0, 1, 'https://example.invalid/dt-result'),
])

conn.commit()
checks = {
    'tables': conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name LIKE 'pre_race_pool_%'").fetchone()[0],
    'event_legs': conn.execute('SELECT COUNT(*) FROM pre_race_pool_event_legs').fetchone()[0],
    'quotes': conn.execute('SELECT COUNT(*) FROM pre_race_pool_selection_quotes').fetchone()[0],
    'members': conn.execute('SELECT COUNT(*) FROM pre_race_pool_selection_members').fetchone()[0],
    'result_members': conn.execute('SELECT COUNT(*) FROM official_pool_result_members').fetchone()[0],
    'payouts': conn.execute('SELECT COUNT(*) FROM official_pool_payouts').fetchone()[0],
    'view_rows': conn.execute('SELECT COUNT(*) FROM v_pre_race_complex_quotes_complete').fetchone()[0],
}
conn.close()
print(checks)
assert checks == {'tables': 5, 'event_legs': 3, 'quotes': 3, 'members': 12, 'result_members': 9, 'payouts': 3, 'view_rows': 3}
print('Complex-pool schema including Double Trio: PASS')
