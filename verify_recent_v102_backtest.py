#!/usr/bin/env python3
"""Contract test for ``backtest_recent_v102_predictions.py``.

The CSV fixture is isolated test data used only to verify calculations and refusal
paths. It is not evidence of model performance.
"""
from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

from backtest_recent_v102_predictions import build_markdown, build_report, parse_args


ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / "recent_v102_backtest_fixture"


def write_fixture(path: Path) -> None:
    rows = [
        # Valid three-runner race: winner is the second-ranked runner.
        {"race_date": "2026-08-01", "racecourse": "ST", "race_no": "1", "horse_name": "甲", "target_win": "0", "race_group": "2026-08-01|ST|1", "race_normalized_probability": "0.50", "model_rank": "1"},
        {"race_date": "2026-08-01", "racecourse": "ST", "race_no": "1", "horse_name": "乙", "target_win": "1", "race_group": "2026-08-01|ST|1", "race_normalized_probability": "0.30", "model_rank": "2"},
        {"race_date": "2026-08-01", "racecourse": "ST", "race_no": "1", "horse_name": "丙", "target_win": "0", "race_group": "2026-08-01|ST|1", "race_normalized_probability": "0.20", "model_rank": "3"},
        # Invalid probability sum: must be excluded rather than silently normalized.
        {"race_date": "2026-08-02", "racecourse": "HV", "race_no": "2", "horse_name": "丁", "target_win": "1", "race_group": "2026-08-02|HV|2", "race_normalized_probability": "0.60", "model_rank": "1"},
        {"race_date": "2026-08-02", "racecourse": "HV", "race_no": "2", "horse_name": "戊", "target_win": "0", "race_group": "2026-08-02|HV|2", "race_normalized_probability": "0.50", "model_rank": "2"},
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    if FIXTURE.exists():
        shutil.rmtree(FIXTURE)
    FIXTURE.mkdir(parents=True)
    csv_path = FIXTURE / "predictions.csv"
    write_fixture(csv_path)

    # Construct argparse-compatible arguments without modifying process argv.
    import argparse

    args = argparse.Namespace(
        predictions=str(csv_path),
        probability_column="race_normalized_probability",
        recent_races=2,
        recent_days=None,
        calibration_bins=5,
        probability_tolerance=1e-6,
        output_json=str(FIXTURE / "report.json"),
        output_md=str(FIXTURE / "report.md"),
    )
    report = build_report(args)
    assert report["status"] == "ok"
    assert report["coverage"] == {
        "selected_race_groups": 2,
        "evaluated_races": 1,
        "excluded_races": 1,
        "evaluated_runners": 3,
        "date_range": "2026-08-01 至 2026-08-02",
    }
    metrics = report["metrics"]
    assert metrics["top1_win_rate"] == 0.0
    assert metrics["top3_contains_winner_rate"] == 1.0
    assert abs(metrics["mean_race_brier_score"] - 0.78) < 1e-12
    assert abs(metrics["mean_uniform_brier_score"] - (2.0 / 3.0)) < 1e-12
    assert report["exclusions"] == {"probability_sum_not_one": 1}
    assert report["race_results"][0]["status"] == "evaluated"
    assert report["race_results"][1]["reason"] == "probability_sum_not_one"
    markdown = build_markdown(report)
    assert "探索性樣本警告" in markdown
    assert "Brier" in markdown
    assert "probability_sum_not_one" in markdown
    (FIXTURE / "report.md").write_text(markdown, encoding="utf-8")
    (FIXTURE / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "passed", "provenance": "isolated contract fixture; not model-performance evidence", "metrics": metrics}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
