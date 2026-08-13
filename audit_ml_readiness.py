#!/usr/bin/env python3
"""Inspect HKJC database fields required by the ELO and ML feature pipeline."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def main() -> int:
    conn = sqlite3.connect("hkjc_last_season.sqlite")
    row = conn.execute(
        """
        SELECT
          COUNT(*) AS completed_starters,
          SUM(CASE WHEN s.finish_time IS NOT NULL AND s.finish_time NOT IN ('---','-','') THEN 1 ELSE 0 END) AS finish_time_rows,
          SUM(CASE WHEN s.running_positions IS NOT NULL AND trim(s.running_positions)<>'' THEN 1 ELSE 0 END) AS running_position_rows,
          SUM(CASE WHEN s.win_odds IS NOT NULL THEN 1 ELSE 0 END) AS odds_rows
        FROM starters AS s
        JOIN races AS r
          ON r.race_date=s.race_date AND r.racecourse=s.racecourse AND r.race_no=s.race_no
        WHERE r.race_status='completed'
        """
    ).fetchone()
    report = {
        "completed_starters": row[0],
        "finish_time_rows": row[1],
        "running_position_rows": row[2],
        "odds_rows": row[3],
    }
    Path("ml_readiness_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
