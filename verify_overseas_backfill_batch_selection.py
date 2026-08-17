#!/usr/bin/env python3
"""Contract test for resumable overseas backfill batch selection."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from overseas_hkjc_core import select_meetings

ROOT = Path(__file__).resolve().parent
DB = ROOT / "overseas_backfill_selection_fixture.sqlite"


def main() -> int:
    DB.unlink(missing_ok=True)
    conn = sqlite3.connect(DB)
    conn.executescript("""
        CREATE TABLE overseas_meetings (
          meeting_id INTEGER PRIMARY KEY,
          meeting_date TEXT NOT NULL,
          simulcast_code TEXT NOT NULL,
          meeting_name TEXT,
          location TEXT,
          fixture_url TEXT,
          summary_url TEXT,
          discovery_status TEXT NOT NULL,
          discovered_at_utc TEXT
        );
        CREATE TABLE overseas_races (
          overseas_race_id INTEGER PRIMARY KEY,
          meeting_id INTEGER NOT NULL,
          race_status TEXT NOT NULL,
          fetched_at_utc TEXT
        );
    """)
    conn.executemany(
        "INSERT INTO overseas_meetings VALUES(?,?,?,?,?,?,?,?,?)",
        [
            (1, "2024-01-10", "S1", "New discovery", None, "fixture", "summary", "discovered", "2024-01-10T00:00:00+00:00"),
            (2, "2023-07-23", "S1", "Legacy stale status", None, "fixture", "summary", "race_count_verified", "2023-07-23T00:00:00+00:00"),
            (3, "2023-08-01", "S2", "Recent partial", None, "fixture", "summary", "partial", "2023-08-01T00:00:00+00:00"),
        ],
    )
    conn.executemany(
        "INSERT INTO overseas_races VALUES(?,?,?,?)",
        [
            (1, 2, "source_unavailable", "2023-08-02T00:00:00+00:00"),
            (2, 3, "partial", "2024-01-01T00:00:00+00:00"),
        ],
    )
    selected = select_meetings(conn, "2023-01-01", "2024-12-31")
    keys = [(item.meeting_date, item.simulcast_code) for item in selected]
    assert keys == [("2024-01-10", "S1"), ("2023-07-23", "S1"), ("2023-08-01", "S2")], keys
    conn.close()
    DB.unlink(missing_ok=True)
    print("passed: discovered-first, stale-partial-resume, oldest-partial-rotation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
