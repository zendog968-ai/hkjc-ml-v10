#!/usr/bin/env python3
"""Monthly deterministic refresh: fetch new official results, rebuild features, retrain model.

All input and output paths are resolved before subprocess execution so the job behaves
identically when invoked by cron, systemd, or an interactive shell.  A caller may direct
all mutable artefacts to a candidate staging directory and atomically promote them only
after independent validation.
"""
from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def resolve_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def database_latest_date(db_path: Path) -> date:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        value = connection.execute("SELECT MAX(race_date) FROM races").fetchone()[0]
    finally:
        connection.close()
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
    parser.add_argument("--model", default="horse_model.pkl")
    parser.add_argument("--training-report", default="lightgbm_training_report.json")
    parser.add_argument("--predictions", default="lightgbm_backtest_predictions.csv")
    parser.add_argument("--elo-report", default="elo_feature_report.json")
    parser.add_argument("--quality-report", default="v101_quality_report.json")
    parser.add_argument("--end-date", default=date.today().isoformat(), help="更新至 YYYY-MM-DD")
    parser.add_argument("--start-date", help="可選：指定 YYYY-MM-DD；預設為資料庫最後賽日的翌日")
    parser.add_argument("--skip-fetch", action="store_true", help="只重建特徵及重訓模型")
    parser.add_argument("--skip-equipment", action="store_true", help="略過官方馬匹近績配備回填（不建議；裝備特徵會降級）")
    args = parser.parse_args()

    db_path = resolve_path(args.db)
    csv_path = resolve_path(args.csv)
    model_path = resolve_path(args.model)
    training_report_path = resolve_path(args.training_report)
    predictions_path = resolve_path(args.predictions)
    elo_report_path = resolve_path(args.elo_report)
    quality_report_path = resolve_path(args.quality_report)
    for output_path in (csv_path, model_path, training_report_path, predictions_path, elo_report_path, quality_report_path):
        output_path.parent.mkdir(parents=True, exist_ok=True)

    end_date = date.fromisoformat(args.end_date)
    start_date = date.fromisoformat(args.start_date) if args.start_date else database_latest_date(db_path) + timedelta(days=1)
    if end_date < start_date and not args.skip_fetch:
        print("沒有較新日期需要抓取；將只重建特徵及重訓模型。")
        args.skip_fetch = True

    if not args.skip_fetch:
        run([
            sys.executable, "hkjc_last_season_etl.py", "--db", str(db_path), "--csv", str(csv_path),
            "--start-date", start_date.isoformat(), "--end-date", end_date.isoformat(),
            "--delay-min", "1.5", "--delay-max", "2.3", "--cooldown-every", "20", "--cooldown-seconds", "20",
        ])
    if not args.skip_equipment:
        run([sys.executable, "enrich_hkjc_equipment.py", "--db", str(db_path), "--delay-seconds", "2.5", "--force"])
    run([sys.executable, "normalize_results.py", "--db", str(db_path), "--csv", str(csv_path)])
    run([sys.executable, "build_elo_features.py", "--db", str(db_path), "--report", str(elo_report_path)])
    run([
        sys.executable, "train_lightgbm.py", "--db", str(db_path), "--model", str(model_path),
        "--report", str(training_report_path), "--predictions", str(predictions_path),
    ])
    run([
        sys.executable, "generate_v101_quality_report.py", "--db", str(db_path),
        "--training-report", str(training_report_path), "--output", str(quality_report_path),
    ])
    print("月度更新、特徵重建、模型重訓與品質報告完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
