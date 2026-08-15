#!/usr/bin/env python3
from __future__ import annotations

import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent / "legacy_complex_pool_fixture.sqlite"
conn = sqlite3.connect(DB)
conn.execute("PRAGMA foreign_keys = ON")
legacy_count = conn.execute("SELECT COUNT(*) FROM pre_race_pool_events WHERE pool_type='TRIFECTA_ORDERED'").fetchone()[0]
conn.execute("""INSERT INTO pre_race_pool_events
(meeting_date,meeting_racecourse,pool_type,pool_event_code,expected_leg_count)
VALUES ('2026-09-06','ST','DOUBLE_TRIO','MIGRATION-DT',2)""")
view_exists = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='view' AND name='v_pre_race_complex_quotes_complete'").fetchone()[0]
violations = conn.execute("PRAGMA foreign_key_check").fetchall()
conn.commit()
conn.close()
assert legacy_count == 1
assert view_exists == 1
assert not violations
print("Double Trio migration verification: PASS")
