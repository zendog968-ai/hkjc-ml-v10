#!/usr/bin/env python3
"""Candidate-only residual correction without any market odds feature."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

from evaluate_prerace_residual_correction import (
    C_VALUE, enrich_scored, feature_frame, temporal_folds,
)
from evaluate_bayesian_hierarchical_calibration import normalise, race_metrics, rank_protect
from n6.config import REPORTS_DIR

EXPERIMENT_ID = "prerace_residual_no_market_v1"
OUTPUT_DIR = REPORTS_DIR / "candidates" / EXPERIMENT_ID
PREDICTION_COLUMN = "prerace_no_market_rank_protected_probability"


def features_without_market(frame: pd.DataFrame, stats: dict[str, float] | None = None) -> tuple[pd.DataFrame, dict[str, float]]:
    features, stats = feature_frame(frame, stats)
    return features.drop(columns=["market_log_odds_z"]), stats


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    scored = enrich_scored()
    rows: list[dict[str, object]] = []
    oof: list[pd.DataFrame] = []
    coefficients: list[dict[str, object]] = []
    for fold in temporal_folds(scored):
        calibration = scored[pd.to_datetime(scored["race_date"]) < fold["calibration_end"]].copy()
        test = scored[scored["race_group"].isin(fold["test_races"])].copy()
        x_train, stats = features_without_market(calibration)
        x_test, _ = features_without_market(test, stats)
        x_test = x_test.reindex(columns=x_train.columns, fill_value=0.0)
        model = LogisticRegression(C=C_VALUE, penalty="l2", solver="lbfgs", max_iter=1000, random_state=20260819)
        model.fit(x_train, calibration["target_win"].to_numpy(dtype=int))
        corrected = model.predict_proba(x_test)[:, 1]
        protected = rank_protect(corrected, test["baseline_probability"].to_numpy(float), test["race_group"])
        test[PREDICTION_COLUMN] = normalise(protected, test["race_group"])
        base = race_metrics(test, "baseline_probability")
        candidate = race_metrics(test, PREDICTION_COLUMN)
        if not np.isclose(base["top_pick_win_rate"], candidate["top_pick_win_rate"]):
            raise AssertionError("Rank protection altered a fold top pick.")
        rows.append({"fold_id": fold["fold_id"], "calibration_end_exclusive": fold["calibration_end"].isoformat(), "test_start": fold["test_start"].isoformat(), "test_end": fold["test_end"].isoformat(), "calibration_races": int(calibration["race_group"].nunique()), "test_rows": int(len(test)), "test_races": int(test["race_group"].nunique()), "baseline_race_brier": base["race_brier"], "candidate_race_brier": candidate["race_brier"], "brier_delta": candidate["race_brier"] - base["race_brier"], "baseline_ece": base["ece"], "candidate_ece": candidate["ece"], "baseline_top_pick_win_rate": base["top_pick_win_rate"], "candidate_top_pick_win_rate": candidate["top_pick_win_rate"]})
        coefficients.extend({"fold_id": fold["fold_id"], "feature": name, "coefficient": float(value)} for name, value in zip(x_train.columns, model.coef_[0], strict=True))
        test["fold_id"] = fold["fold_id"]
        oof.append(test)
    oof_frame = pd.concat(oof, ignore_index=True)
    fold_frame = pd.DataFrame(rows)
    coefficient_frame = pd.DataFrame(coefficients)
    base = race_metrics(oof_frame, "baseline_probability")
    candidate = race_metrics(oof_frame, PREDICTION_COLUMN)
    if not np.isclose(base["top_pick_win_rate"], candidate["top_pick_win_rate"]):
        raise AssertionError("Rank protection altered OOF top pick.")
    summary = {"engine": "N6 Neural Calculation Engine", "experiment_id": EXPERIMENT_ID, "generated_at_utc": datetime.now(UTC).isoformat(), "candidate_only_guarantee": "No production model, API, service, calibration layer, or V10 data was modified.", "method": {"model": "L2-regularized logistic residual correction", "C": C_VALUE, "inputs": ["frozen N6 base logit", "draw_pct", "racecourse", "going"], "excluded_inputs": ["market_log_odds", "market_implied_probability"], "all_inputs_pre_race": True, "rank_protection": "non-increasing projection in frozen N6 order"}, "oof": {"folds": len(fold_frame), "races": int(oof_frame["race_group"].nunique()), "baseline_race_brier": base["race_brier"], "candidate_race_brier": candidate["race_brier"], "brier_delta": candidate["race_brier"] - base["race_brier"], "baseline_ece": base["ece"], "candidate_ece": candidate["ece"], "baseline_top_pick_win_rate": base["top_pick_win_rate"], "candidate_top_pick_win_rate": candidate["top_pick_win_rate"]}, "folds": rows}
    (OUTPUT_DIR / "prerace_no_market_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    fold_frame.to_csv(OUTPUT_DIR / "prerace_no_market_folds.csv", index=False, encoding="utf-8-sig")
    coefficient_frame.to_csv(OUTPUT_DIR / "prerace_no_market_coefficients.csv", index=False, encoding="utf-8-sig")
    oof_frame.to_csv(OUTPUT_DIR / "prerace_no_market_oof_predictions.csv", index=False, encoding="utf-8-sig")
    report = ["# N6：無市場賠率的賽前殘差校正候選", "", "> 候選只使用凍結 N6 logit、Going、馬場及相對排位，明確排除市場賠率任何形式。每一時間折以此前資料擬合，並對同場輸出保持原 N6 首選排名。", "", "| 折次 | 測試期 | 賽事 | 基準 Brier | 候選 Brier | 差異 | 基準 ECE | 候選 ECE | 基準首選 | 候選首選 |", "| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for row in rows:
        report.append(f"| {row['fold_id']} | {row['test_start'][:10]} 至 {row['test_end'][:10]} | {row['test_races']} | {row['baseline_race_brier']:.6f} | {row['candidate_race_brier']:.6f} | {row['brier_delta']:+.6f} | {row['baseline_ece']:.6f} | {row['candidate_ece']:.6f} | {row['baseline_top_pick_win_rate']:.2%} | {row['candidate_top_pick_win_rate']:.2%} |")
    report.extend(["", "## OOF 合計", "", f"共 {summary['oof']['races']} 場。基準 Brier 為 {base['race_brier']:.6f}；候選 Brier 為 {candidate['race_brier']:.6f}；差異為 {summary['oof']['brier_delta']:+.6f}。基準 ECE 為 {base['ece']:.6f}；候選 ECE 為 {candidate['ece']:.6f}。", ""])
    (OUTPUT_DIR / "N6_PRERACE_NO_MARKET_CV.md").write_text("\n".join(report), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
