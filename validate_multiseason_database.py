#!/usr/bin/env python3
"""Validate multi-season HKJC official-results coverage for V10.2 research."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def season_label(date_text: str) -> str:
    year, month, _ = map(int, date_text.split("-"))
    start_year = year if month >= 8 else year - 1
    return f"{start_year}/{str(start_year + 1)[-2:]}"


def scalar(conn: sqlite3.Connection, query: str, params: tuple = ()):
    return conn.execute(query, params).fetchone()[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="V10.2 multi-season HKJC database validation")
    parser.add_argument("--db", default="hkjc_last_season.sqlite")
    parser.add_argument("--report", default="v102_multiseason_quality_report.json")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    per_season = {}
    dates = [row["race_date"] for row in conn.execute("SELECT DISTINCT race_date FROM races ORDER BY race_date")]
    for label in sorted({season_label(value) for value in dates}):
        start_year = int(label[:4])
        start_date = f"{start_year}-08-01"
        end_date = f"{start_year + 1}-07-31"
        per_season[label] = dict(
            meetings=scalar(conn, "SELECT COUNT(*) FROM meetings WHERE race_date BETWEEN ? AND ?", (start_date, end_date)),
            races=scalar(conn, "SELECT COUNT(*) FROM races WHERE race_date BETWEEN ? AND ?", (start_date, end_date)),
            completed_races=scalar(conn, "SELECT COUNT(*) FROM races WHERE race_status='completed' AND race_date BETWEEN ? AND ?", (start_date, end_date)),
            cancelled_or_void=scalar(conn, "SELECT COUNT(*) FROM races WHERE race_status IN ('cancelled','void') AND race_date BETWEEN ? AND ?", (start_date, end_date)),
            starters=scalar(conn, "SELECT COUNT(*) FROM starters WHERE race_date BETWEEN ? AND ?", (start_date, end_date)),
            distinct_race_dates=scalar(conn, "SELECT COUNT(DISTINCT race_date) FROM races WHERE race_date BETWEEN ? AND ?", (start_date, end_date)),
        )

    missing = conn.execute(
        """
        SELECT
          SUM(CASE WHEN horse_name IS NULL OR trim(horse_name)='' THEN 1 ELSE 0 END) AS horse_name,
          SUM(CASE WHEN draw IS NULL THEN 1 ELSE 0 END) AS draw,
          SUM(CASE WHEN weight_lbs IS NULL THEN 1 ELSE 0 END) AS weight_lbs,
          SUM(CASE WHEN declared_weight_kg IS NULL THEN 1 ELSE 0 END) AS declared_weight_kg,
          SUM(CASE WHEN jockey IS NULL OR trim(jockey)='' THEN 1 ELSE 0 END) AS jockey,
          SUM(CASE WHEN trainer IS NULL OR trim(trainer)='' THEN 1 ELSE 0 END) AS trainer,
          SUM(CASE WHEN win_odds IS NULL THEN 1 ELSE 0 END) AS win_odds,
          SUM(CASE WHEN finish_pos IS NULL THEN 1 ELSE 0 END) AS finish_pos
        FROM starters s JOIN races r USING(race_date,racecourse,race_no)
        WHERE r.race_status='completed'
        """
    ).fetchone()
    duplicate_count = scalar(
        conn,
        """
        SELECT COUNT(*) FROM (
          SELECT race_date,racecourse,race_no,horse_name,COUNT(*) n
          FROM starters GROUP BY race_date,racecourse,race_no,horse_name HAVING n > 1
        )
        """,
    )
    orphan_starters = scalar(
        conn,
        """
        SELECT COUNT(*) FROM starters s
        LEFT JOIN races r USING(race_date,racecourse,race_no)
        WHERE r.race_id IS NULL
        """,
    )
    field_rows = conn.execute(
        """
        SELECT COUNT(*) AS n FROM starters s JOIN races r USING(race_date,racecourse,race_no)
        WHERE r.race_status='completed'
        GROUP BY s.race_date,s.racecourse,s.race_no
        """
    ).fetchall()
    no_result_log = scalar(conn, "SELECT COUNT(*) FROM crawl_log WHERE outcome='no_official_local_results'")

    report = {
        "scope": "HKJC local official results, 2023/24 through 2025/26",
        "coverage": {
            "first_race_date": scalar(conn, "SELECT MIN(race_date) FROM races"),
            "last_race_date": scalar(conn, "SELECT MAX(race_date) FROM races"),
            "season_breakdown": per_season,
        },
        "totals": {
            "meetings": scalar(conn, "SELECT COUNT(*) FROM meetings"),
            "races": scalar(conn, "SELECT COUNT(*) FROM races"),
            "completed_races": scalar(conn, "SELECT COUNT(*) FROM races WHERE race_status='completed'"),
            "cancelled_or_void": scalar(conn, "SELECT COUNT(*) FROM races WHERE race_status IN ('cancelled','void')"),
            "starters": scalar(conn, "SELECT COUNT(*) FROM starters"),
        },
        "integrity": {
            "duplicate_race_horse_rows": duplicate_count,
            "orphan_starters": orphan_starters,
            "completed_starter_missing_fields": dict(missing),
            "field_size_min": min(row["n"] for row in field_rows),
            "field_size_max": max(row["n"] for row in field_rows),
            "field_size_average": round(sum(row["n"] for row in field_rows) / len(field_rows), 3),
            "official_no_results_dates_skipped": no_result_log,
        },
    }
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
