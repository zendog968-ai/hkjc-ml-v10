#!/usr/bin/env python3
"""Normalize officially tied finish-position labels in an existing HKJC SQLite database."""
from __future__ import annotations

import argparse
import re
import sqlite3
from pathlib import Path

from hkjc_last_season_etl import export_csv


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="hkjc_last_season.sqlite")
    parser.add_argument("--csv", default="hkjc_last_season.csv")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    rows = conn.execute(
        "SELECT rowid, finish_pos_text FROM starters WHERE finish_pos IS NULL AND finish_pos_text LIKE '%平頭馬%'"
    ).fetchall()
    updates = []
    for rowid, text in rows:
        match = re.match(r"\s*(\d+)\s*平頭馬\s*$", text or "")
        if match:
            updates.append((int(match.group(1)), rowid))
    conn.executemany("UPDATE starters SET finish_pos=? WHERE rowid=?", updates)
    conn.commit()
    exported = export_csv(conn, Path(args.csv))
    print(f"已補正同名次紀錄：{len(updates)} 行；CSV 已重新匯出：{exported} 行。")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
