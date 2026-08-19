#!/usr/bin/env python3
"""Regression check for N6 SQLite descriptor cleanup in historical inference reads."""
from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path

from n6.feature_engineering import load_historical_race

ROOT = Path(__file__).resolve().parent
SOURCE_CSV = ROOT / "reports" / "n6_test_predictions.csv"
SQLITE_NAME = "hkjc_last_season.sqlite"


def sqlite_fd_count() -> int:
    count = 0
    for entry in Path(f"/proc/{os.getpid()}/fd").iterdir():
        try:
            if SQLITE_NAME in os.readlink(entry):
                count += 1
        except FileNotFoundError:
            continue
    return count


def sample_race() -> tuple[str, str, int]:
    with SOURCE_CSV.open(encoding="utf-8-sig", newline="") as handle:
        row = next(csv.DictReader(handle))
    return row["race_date"], row["racecourse"], int(row["race_no"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=100)
    args = parser.parse_args()
    if args.iterations < 1:
        raise ValueError("iterations must be positive")

    race = sample_race()
    retained_counts: list[int] = []
    row_counts: list[int] = []
    for _ in range(args.iterations):
        frame = load_historical_race(*race)
        row_counts.append(len(frame))
        retained_counts.append(sqlite_fd_count())

    payload = {
        "race": {"race_date": race[0], "racecourse": race[1], "race_no": race[2]},
        "iterations": args.iterations,
        "row_count_min": min(row_counts),
        "row_count_max": max(row_counts),
        "sqlite_fd_before": 0,
        "sqlite_fd_after_each_query_min": min(retained_counts),
        "sqlite_fd_after_each_query_max": max(retained_counts),
        "passed": max(retained_counts) == 0,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
