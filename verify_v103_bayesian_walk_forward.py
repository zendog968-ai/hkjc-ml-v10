#!/usr/bin/env python3
"""Contract checks for the V10.3 Bayesian overlay walk-forward scaffold.

The test intentionally uses the project's saved V10.2 pre-race artifact rather
than a synthetic race dataset.  It verifies temporal boundaries and output
invariants without treating the small available test window as performance proof.
"""
from __future__ import annotations

import json
import shutil
from argparse import Namespace
from pathlib import Path

from walk_forward_v103_bayesian_uncertainty import (
    build_report,
    load_frozen_races,
    make_folds,
)

ROOT = Path(__file__).resolve().parent
PREDICTIONS = ROOT / "v102_multiseason_backtest_predictions.csv"
OUTPUT_DIR = ROOT / "v103_walk_forward_contract_fixture"


def main() -> int:
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    races, exclusions = load_frozen_races(PREDICTIONS, "race_normalized_probability", 1e-6)
    assert len(races) >= 150, f"預期實際工件至少 150 場，實得 {len(races)} 場"
    assert not exclusions, f"實際工件不應有完整性排除：{dict(exclusions)}"
    folds = make_folds(races, min_train=50, validation=25, test=25, max_folds=3)
    assert len(folds) == 3, f"預期 3 個實際 fold，實得 {len(folds)}"
    for fold_id, fold in enumerate(folds, start=1):
        train_dates = {race.meta.race_date for race in fold["train"]}
        validation_dates = {race.meta.race_date for race in fold["validation"]}
        test_dates = {race.meta.race_date for race in fold["test"]}
        assert not (train_dates & validation_dates), f"fold {fold_id} train/validation 賽日交疊"
        assert not (train_dates & test_dates), f"fold {fold_id} train/test 賽日交疊"
        assert not (validation_dates & test_dates), f"fold {fold_id} validation/test 賽日交疊"
        assert max(train_dates) < min(validation_dates) < min(test_dates), f"fold {fold_id} 時間順序錯誤"

    args = Namespace(
        predictions=str(PREDICTIONS),
        probability_column="race_normalized_probability",
        min_train_races=50,
        validation_races=25,
        test_races=25,
        max_folds=3,
        prior_sds="0.15,0.30,0.50",
        posterior_draws=100,
        seed=10301,
        probability_tolerance=1e-6,
        output_json=str(OUTPUT_DIR / "report.json"),
        output_md=str(OUTPUT_DIR / "report.md"),
        output_race_csv=str(OUTPUT_DIR / "race_metrics.csv"),
    )
    report = build_report(args)
    assert report["configuration"]["temporal_split_unit"] == "race_date_complete_non_overlapping"
    assert report["input"]["formal_probability_replacement"] is False
    assert len(report["folds"]) == 3
    assert report["test_metrics"]["races"] > 0
    assert report["pre_registered_gate"]["probability_sum_ok"] is True
    assert report["pre_registered_gate"]["rank_change_evaluable"] is False
    assert report["pre_registered_gate"]["adoption_decision"] == "NOT_ELIGIBLE"
    for item in report["test_race_metrics"]:
        assert item["posterior_probability_sum_max_abs_error"] <= 1e-6
        assert item["rank_stability_semantics"] == "not_informative_rank_preserving_temperature"
        assert item["control_top1_won"] == item["overlay_top1_won"]
        assert item["control_top3_contains_winner"] == item["overlay_top3_contains_winner"]
        assert item["control_winner_rank"] == item["overlay_winner_rank"]
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    summary = {
        "status": "ok",
        "real_artifact_usable_races": len(races),
        "folds": len(report["folds"]),
        "test_races": report["test_metrics"]["races"],
        "gate_status": report["pre_registered_gate"]["status"],
        "temporal_split_unit": report["configuration"]["temporal_split_unit"],
    }
    (OUTPUT_DIR / "validation.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
