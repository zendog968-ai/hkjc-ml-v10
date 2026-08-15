#!/usr/bin/env python3
"""Migrate an existing complex-pool SQLite archive to accept pool_type=DOUBLE_TRIO.

SQLite cannot alter a CHECK constraint in place.  This migration rebuilds only
pre_race_pool_events while preserving its rows and keeping the original table name
referenced by child foreign keys.
"""
from __future__ import annotations

import argparse
import sqlite3

NEW_TABLE_SQL = """
CREATE TABLE pre_race_pool_events_new (
    pool_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_date TEXT NOT NULL,
    meeting_racecourse TEXT NOT NULL CHECK (meeting_racecourse IN ('ST', 'HV')),
    pool_type TEXT NOT NULL CHECK (pool_type IN (
        'TRIFECTA_ORDERED', 'TRIO_UNORDERED',
        'QUARTET_ORDERED', 'FIRST_4_UNORDERED', 'QUARTET_FIRST_4_COMBINED',
        'DOUBLE_TRIO', 'SIX_UP'
    )),
    pool_event_code TEXT NOT NULL,
    expected_leg_count INTEGER NOT NULL CHECK (expected_leg_count BETWEEN 1 AND 6),
    source_url TEXT,
    announced_at_utc TEXT,
    UNIQUE (meeting_date, meeting_racecourse, pool_type, pool_event_code)
)
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="為現有複合彩池 archive 加入 DOUBLE_TRIO CHECK 約束")
    parser.add_argument("--db", required=True)
    args = parser.parse_args()
    conn = sqlite3.connect(args.db)
    table = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='pre_race_pool_events'").fetchone()
    if table is None:
        raise ValueError("找不到 pre_race_pool_events；請先套用複合彩池 schema")
    if "DOUBLE_TRIO" in (table[0] or ""):
        print('{"status":"already_current","migrated":false}')
        conn.close()
        return 0
    required_children = {"pre_race_pool_event_legs", "pre_race_pool_snapshots", "official_pool_result_members", "official_pool_payouts"}
    existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = sorted(required_children - existing)
    if missing:
        raise ValueError("archive 缺少必要子表：" + ", ".join(missing))
    count_before = conn.execute("SELECT COUNT(*) FROM pre_race_pool_events").fetchone()[0]
    dependent_views = conn.execute(
        """SELECT name, sql FROM sqlite_master
        WHERE type='view' AND sql LIKE '%pre_race_pool_events%'"""
    ).fetchall()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        for view_name, _ in dependent_views:
            conn.execute(f'DROP VIEW "{view_name}"')
        conn.execute(NEW_TABLE_SQL)
        conn.execute(
            """INSERT INTO pre_race_pool_events_new
            (pool_event_id,meeting_date,meeting_racecourse,pool_type,pool_event_code,
             expected_leg_count,source_url,announced_at_utc)
            SELECT pool_event_id,meeting_date,meeting_racecourse,pool_type,pool_event_code,
                   expected_leg_count,source_url,announced_at_utc
            FROM pre_race_pool_events"""
        )
        conn.execute("DROP TABLE pre_race_pool_events")
        conn.execute("ALTER TABLE pre_race_pool_events_new RENAME TO pre_race_pool_events")
        for _, view_sql in dependent_views:
            conn.execute(view_sql)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    count_after = conn.execute("SELECT COUNT(*) FROM pre_race_pool_events").fetchone()[0]
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    conn.close()
    if count_before != count_after or violations:
        raise RuntimeError(f"遷移驗證失敗：before={count_before}, after={count_after}, foreign_key_violations={len(violations)}")
    print(f'{{"status":"migrated","migrated":true,"events_preserved":{count_after}}}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
