#!/usr/bin/env python3
"""Quality checks for the HKJC last-season database and a leakage-free test card."""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path


def scalar(conn: sqlite3.Connection, query: str):
    return conn.execute(query).fetchone()[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="hkjc_last_season.sqlite")
    parser.add_argument("--report", default="database_quality_report.json")
    parser.add_argument("--backtest-card", default="backtest_race_card.json")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    status_rows = conn.execute(
        "SELECT race_status, COUNT(*) AS count FROM races GROUP BY race_status ORDER BY race_status"
    ).fetchall()
    null_rows = conn.execute(
        """
        SELECT
          SUM(CASE WHEN horse_name IS NULL OR trim(horse_name)='' THEN 1 ELSE 0 END) AS missing_horse_name,
          SUM(CASE WHEN draw IS NULL THEN 1 ELSE 0 END) AS missing_draw,
          SUM(CASE WHEN weight_lbs IS NULL THEN 1 ELSE 0 END) AS missing_weight,
          SUM(CASE WHEN jockey IS NULL OR trim(jockey)='' THEN 1 ELSE 0 END) AS missing_jockey,
          SUM(CASE WHEN trainer IS NULL OR trim(trainer)='' THEN 1 ELSE 0 END) AS missing_trainer,
          SUM(CASE WHEN finish_pos IS NULL THEN 1 ELSE 0 END) AS missing_finish_pos
        FROM starters AS s
        JOIN races AS r
          ON r.race_date=s.race_date AND r.racecourse=s.racecourse AND r.race_no=s.race_no
        WHERE r.race_status='completed'
        """
    ).fetchone()
    field_sizes = conn.execute(
        """
        SELECT COUNT(*) AS field_size
        FROM starters AS s
        JOIN races AS r
          ON r.race_date=s.race_date AND r.racecourse=s.racecourse AND r.race_no=s.race_no
        WHERE r.race_status='completed'
        GROUP BY s.race_date,s.racecourse,s.race_no
        """
    ).fetchall()
    duplicate_rows = scalar(
        conn,
        """
        SELECT COUNT(*) FROM (
          SELECT race_date,racecourse,race_no,horse_name,COUNT(*) AS n
          FROM starters GROUP BY race_date,racecourse,race_no,horse_name HAVING n>1
        )
        """,
    )
    nonstandard_statuses = conn.execute(
        """
        SELECT s.finish_pos_text, COUNT(*) AS count
        FROM starters AS s
        JOIN races AS r
          ON r.race_date=s.race_date AND r.racecourse=s.racecourse AND r.race_no=s.race_no
        WHERE r.race_status='completed' AND s.finish_pos IS NULL
        GROUP BY s.finish_pos_text
        ORDER BY count DESC
        """
    ).fetchall()

    test_race = conn.execute(
        """
        SELECT r.*
        FROM races AS r
        WHERE r.race_status='completed'
        ORDER BY r.race_date DESC, r.racecourse DESC, r.race_no ASC
        LIMIT 1
        """
    ).fetchone()
    runners = conn.execute(
        """
        SELECT horse_name, draw, weight_lbs, jockey, trainer, win_odds
        FROM starters
        WHERE race_date=? AND racecourse=? AND race_no=? AND horse_no IS NOT NULL
        ORDER BY horse_no
        """,
        (test_race["race_date"], test_race["racecourse"], test_race["race_no"]),
    ).fetchall()
    test_card = {
        "race": {
            "racecourse": test_race["racecourse"],
            "distance_m": test_race["distance_m"],
            "surface": test_race["surface"],
            "course_config": test_race["course_config"],
            "going": test_race["going"],
            "as_of_date": test_race["race_date"],
            "validation_note": "This historical card is evaluated using only records before as_of_date.",
        },
        "runners": [
            {
                "horse_name": row["horse_name"],
                "draw": row["draw"],
                "weight_lbs": row["weight_lbs"],
                "jockey": row["jockey"],
                "trainer": row["trainer"],
                "market_odds": row["win_odds"],
            }
            for row in runners
        ],
    }
    Path(args.backtest_card).write_text(json.dumps(test_card, ensure_ascii=False, indent=2), encoding="utf-8")

    report = {
        "season": "2025/26",
        "meeting_count": scalar(conn, "SELECT COUNT(*) FROM meetings"),
        "race_count": scalar(conn, "SELECT COUNT(*) FROM races"),
        "completed_race_count": scalar(conn, "SELECT COUNT(*) FROM races WHERE race_status='completed'"),
        "starter_row_count": scalar(conn, "SELECT COUNT(*) FROM starters"),
        "race_status_counts": {row["race_status"]: row["count"] for row in status_rows},
        "coverage": {
            "first_race_date": scalar(conn, "SELECT MIN(race_date) FROM races"),
            "last_race_date": scalar(conn, "SELECT MAX(race_date) FROM races"),
            "distinct_race_dates": scalar(conn, "SELECT COUNT(DISTINCT race_date) FROM races"),
        },
        "data_integrity": {
            "duplicate_race_horse_rows": duplicate_rows,
            "completed_race_missing_fields": dict(null_rows),
            "field_size_min": min(row["field_size"] for row in field_sizes),
            "field_size_max": max(row["field_size"] for row in field_sizes),
            "field_size_average": round(sum(row["field_size"] for row in field_sizes) / len(field_sizes), 3),
            "nonstandard_finish_statuses": {str(row["finish_pos_text"]): row["count"] for row in nonstandard_statuses},
        },
        "leakage_free_backtest_card": {
            "race_date": test_race["race_date"],
            "racecourse": test_race["racecourse"],
            "race_no": test_race["race_no"],
            "runner_count": len(runners),
            "as_of_date": test_race["race_date"],
        },
    }
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
