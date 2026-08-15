#!/usr/bin/env python3
from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT / "legacy_complex_pool_fixture.sqlite"
if DB.exists():
    DB.unlink()
base = (ROOT / "schema_prerace_odds_snapshots.sql").read_text(encoding="utf-8")
extension = (ROOT / "schema_prerace_complex_pool_snapshots.sql").read_text(encoding="utf-8").replace("        'DOUBLE_TRIO', 'SIX_UP'", "        'SIX_UP'")
conn = sqlite3.connect(DB)
conn.execute("PRAGMA foreign_keys = ON")
conn.executescript(base)
conn.executescript(extension)
cur = conn.execute("""INSERT INTO pre_race_pool_events
(meeting_date,meeting_racecourse,pool_type,pool_event_code,expected_leg_count)
VALUES ('2026-09-06','ST','TRIFECTA_ORDERED','LEGACY-R3',1)""")
event_id = cur.lastrowid
conn.execute("""INSERT INTO pre_race_pool_event_legs
(pool_event_id,leg_no,race_date,racecourse,race_no,scheduled_start_utc)
VALUES (?,1,'2026-09-06','ST',3,'2026-09-06T08:30:00+00:00')""", (event_id,))
conn.commit()
conn.close()
print(DB)
