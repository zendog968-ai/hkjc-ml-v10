#!/usr/bin/env python3
"""Monthly deterministic refresh: fetch new official results, rebuild features, retrain model.

Run this after the end of a month or on a selected date range. It preserves the existing
SQLite database and relies on the ETL's own rate-limit and resume behaviour.
"""
from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def database_latest_date(db_path: Path) -> date:
    conn = sqlite3.connect(db_path)
    value = conn.execute("SELECT MAX(race_date) FROM races").fetchone()[0]
    conn.close()
    if not value:
        raise ValueError("資料庫沒有既有賽日，請先完成初始建置。")
    return date.fromisoformat(value)


def run(command: list[str]) -> None:
    print("執行：", " ".join(command))
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="每月更新 HKJC 資料庫、ELO 特徵與 LightGBM 模型")
    parser.add_argument("--db", default="hkjc_last_season.sqlite")
    parser.add_argument("--csv", default="hkjc_last_season.csv")
    parser.add_argument("--end-date", default=date.today().isoformat(), help="更新至 YYYY-MM-DD")
    parser.add_argument("--start-date", help="可選：指定 YYYY-MM-DD；預設為資料庫最後賽日的翌日")
    parser.add_argument("--skip-fetch", action="store_true", help="只重建特徵及重訓模型")
    args = parser.parse_args()
    db_path = Path(args.db)
    end_date = date.fromisoformat(args.end_date)
    start_date = date.fromisoformat(args.start_date) if args.start_date else database_latest_date(db_path) + timedelta(days=1)
    if end_date < start_date and not args.skip_fetch:
        print("沒有較新日期需要抓取；將只重建特徵及重訓模型。")
        args.skip_fetch = True

    if not args.skip_fetch:
        run([
            sys.executable, "hkjc_last_season_etl.py", "--db", args.db, "--csv", args.csv,
            "--start-date", start_date.isoformat(), "--end-date", end_date.isoformat(),
            "--delay-min", "1.5", "--delay-max", "2.3", "--cooldown-every", "20", "--cooldown-seconds", "20",
        ])
    run([sys.executable, "normalize_results.py", "--db", args.db, "--csv", args.csv])
    run([sys.executable, "build_elo_features.py", "--db", args.db, "--report", "elo_feature_report.json"])
    run([
        sys.executable, "train_lightgbm.py", "--db", args.db, "--model", "horse_model.pkl",
        "--report", "lightgbm_training_report.json", "--predictions", "lightgbm_backtest_predictions.csv",
    ])
    print("月度更新與重訓完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
