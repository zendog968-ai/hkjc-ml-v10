#!/usr/bin/env python3
"""Candidate-only low-degree residual correction using pre-race V10 fields.

The frozen 72D N6 score is transformed to a logit and corrected by a strongly
regularized logistic layer using only race-day pre-race fields: Going, market
log odds, normalized draw, and racecourse. The calibration layer is fit only
on history preceding each expanding temporal test fold, then projected back to
the original N6 rank order and race-normalized.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

from evaluate_bayesian_hierarchical_calibration import (
    expit, logit, normalise, race_metrics, rank_protect, score_all,
)
from n6.config import REPORTS_DIR
from n6.feature_engineering import load_training_frame

EXPERIMENT_ID = "prerace_residual_correction_v1"
OUTPUT_DIR = REPORTS_DIR / "candidates" / EXPERIMENT_ID
SUMMARY_PATH = OUTPUT_DIR / "prerace_residual_correction_summary.json"
FOLDS_PATH = OUTPUT_DIR / "prerace_residual_correction_folds.csv"
OOF_PATH = OUTPUT_DIR / "prerace_residual_correction_oof_predictions.csv"
COEFFICIENTS_PATH = OUTPUT_DIR / "prerace_residual_correction_coefficients.csv"
REPORT_PATH = OUTPUT_DIR / "N6_PRERACE_RESIDUAL_CORRECTION_CV.md"
TARGET = "target_win"
C_VALUE = 0.10
CANDIDATE_COLUMN = "prerace_residual_rank_protected_probability"


def temporal_folds(scored: pd.DataFrame) -> list[dict[str, Any]]:
    races = scored[["race_date", "race_group"]].drop_duplicates().sort_values(["race_date", "race_group"]).reset_index(drop=True)
    # Expanding calibration. The final 40% is split into three non-overlapping chronological folds.
    start = int(len(races) * 0.60)
    blocks = np.array_split(races.iloc[start:], 3)
    folds: list[dict[str, Any]] = []
    for fold_id, block in enumerate(blocks, start=1):
        cutoff = block["race_date"].min()
        folds.append({"fold_id": fold_id, "calibration_end": cutoff, "test_races": set(block["race_group"]), "test_start": cutoff, "test_end": block["race_date"].max()})
    return folds


def feature_frame(frame: pd.DataFrame, stats: dict[str, float] | None = None) -> tuple[pd.DataFrame, dict[str, float]]:
    data = frame.copy()
    if stats is None:
        odds_median = float(data["market_log_odds"].median())
        odds_mean = float(data["market_log_odds"].fillna(odds_median).mean())
        odds_std = max(float(data["market_log_odds"].fillna(odds_median).std(ddof=0)), 1e-6)
        draw_mean = float(data["draw_pct"].mean())
        draw_std = max(float(data["draw_pct"].std(ddof=0)), 1e-6)
        stats = {"odds_median": odds_median, "odds_mean": odds_mean, "odds_std": odds_std, "draw_mean": draw_mean, "draw_std": draw_std}
    odds = data["market_log_odds"].fillna(stats["odds_median"])
    output = pd.DataFrame({
        "base_logit": logit(data["baseline_probability"].to_numpy(dtype=float)),
        "market_log_odds_z": (odds - stats["odds_mean"]) / stats["odds_std"],
        "draw_pct_z": (data["draw_pct"] - stats["draw_mean"]) / stats["draw_std"],
        "racecourse_hv": (data["racecourse"] == "HV").astype(float),
    }, index=data.index)
    going = pd.get_dummies(data["going"], prefix="going", dtype=float)
    for column in ["going_好地", "going_好地至快地", "going_好地至黏地", "going_黏地", "going_軟地", "going_封地", "going_濕慢地", "going_濕快地"]:
        if column not in going:
            going[column] = 0.0
    output = pd.concat([output, going.reindex(sorted(going.columns), axis=1)], axis=1)
    # Good going is the reference category to avoid an unnecessary dummy intercept dependency.
    if "going_好地" in output:
        output = output.drop(columns=["going_好地"])
    return output.astype(float), stats


def enrich_scored() -> pd.DataFrame:
    raw = load_training_frame()
    scored = score_all(raw)
    lookup = raw[["race_group", "horse_name", "market_log_odds", "draw_pct"]].drop_duplicates(subset=["race_group", "horse_name"])
    merged = scored.merge(lookup, on=["race_group", "horse_name"], how="left", validate="one_to_one")
    if merged[["draw_pct"]].isna().any().any():
        raise ValueError("Missing required pre-race draw_pct after merge.")
    return merged


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    scored = enrich_scored()
    predictions: list[pd.DataFrame] = []
    fold_rows: list[dict[str, Any]] = []
    coef_rows: list[dict[str, Any]] = []
    for fold in temporal_folds(scored):
        calibration = scored[scored["race_date"] < fold["calibration_end"]].copy()
        test = scored[scored["race_group"].isin(fold["test_races"])].copy()
        x_train, stats = feature_frame(calibration)
        x_test, _ = feature_frame(test, stats)
        x_test = x_test.reindex(columns=x_train.columns, fill_value=0.0)
        model = LogisticRegression(C=C_VALUE, penalty="l2", solver="lbfgs", max_iter=1000, random_state=20260819)
        model.fit(x_train, calibration[TARGET].to_numpy(dtype=int))
        raw = model.predict_proba(x_test)[:, 1]
        protected = rank_protect(raw, test["baseline_probability"].to_numpy(dtype=float), test["race_group"])
        test[CANDIDATE_COLUMN] = normalise(protected, test["race_group"])
        base = race_metrics(test, "baseline_probability")
        candidate = race_metrics(test, CANDIDATE_COLUMN)
        if not np.isclose(base["top_pick_win_rate"], candidate["top_pick_win_rate"]):
            raise AssertionError("Rank protection changed a fold top-pick win rate.")
        fold_rows.append({"fold_id": fold["fold_id"], "calibration_end_exclusive": fold["calibration_end"].isoformat(), "test_start": fold["test_start"].isoformat(), "test_end": fold["test_end"].isoformat(), "calibration_rows": int(len(calibration)), "calibration_races": int(calibration["race_group"].nunique()), "test_rows": int(len(test)), "test_races": int(test["race_group"].nunique()), "baseline_race_brier": base["race_brier"], "candidate_race_brier": candidate["race_brier"], "brier_delta": candidate["race_brier"] - base["race_brier"], "baseline_ece": base["ece"], "candidate_ece": candidate["ece"], "baseline_top_pick_win_rate": base["top_pick_win_rate"], "candidate_top_pick_win_rate": candidate["top_pick_win_rate"]})
        coef_rows.extend({"fold_id": fold["fold_id"], "feature": feature, "coefficient": float(value)} for feature, value in zip(x_train.columns, model.coef_[0], strict=True))
        predictions.append(test)
    oof = pd.concat(predictions, ignore_index=True)
    folds = pd.DataFrame(fold_rows)
    coefficients = pd.DataFrame(coef_rows)
    base = race_metrics(oof, "baseline_probability")
    candidate = race_metrics(oof, CANDIDATE_COLUMN)
    if not np.isclose(base["top_pick_win_rate"], candidate["top_pick_win_rate"]):
        raise AssertionError("Rank protection changed OOF top-pick win rate.")
    summary = {"engine": "N6 Neural Calculation Engine", "experiment_id": EXPERIMENT_ID, "generated_at_utc": datetime.now(UTC).isoformat(), "candidate_only_guarantee": "No production model, API, service, calibration layer, or V10 data was modified.", "method": {"model": "L2-regularized logistic residual correction", "C": C_VALUE, "inputs": ["frozen N6 base logit", "market_log_odds", "draw_pct", "racecourse", "going"], "all_inputs_pre_race": True, "rank_protection": "non-increasing projection in frozen N6 order"}, "oof": {"folds": int(len(folds)), "races": int(oof["race_group"].nunique()), "baseline_race_brier": base["race_brier"], "candidate_race_brier": candidate["race_brier"], "brier_delta": candidate["race_brier"] - base["race_brier"], "baseline_ece": base["ece"], "candidate_ece": candidate["ece"], "baseline_top_pick_win_rate": base["top_pick_win_rate"], "candidate_top_pick_win_rate": candidate["top_pick_win_rate"]}, "folds": fold_rows}
    SUMMARY_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    folds.to_csv(FOLDS_PATH, index=False, encoding="utf-8-sig")
    oof.to_csv(OOF_PATH, index=False, encoding="utf-8-sig")
    coefficients.to_csv(COEFFICIENTS_PATH, index=False, encoding="utf-8-sig")
    REPORT_PATH.write_text("# N6：既有賽前特徵殘差修正候選\n\n> 此候選只使用既有 V10 SQLite 的賽前 Going、賠率、排位和馬場欄位。每個時間折以此前資料擬合強正則化 logistic 殘差層，輸出再進行場內正規化及原始 N6 排名保護。\n\n| OOF 賽事 | 基準 Brier | 候選 Brier | 差異 | 基準 ECE | 候選 ECE | 基準首選 | 候選首選 |\n| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |\n" + f"| {summary['oof']['races']} | {base['race_brier']:.6f} | {candidate['race_brier']:.6f} | {summary['oof']['brier_delta']:+.6f} | {base['ece']:.6f} | {candidate['ece']:.6f} | {base['top_pick_win_rate']:.2%} | {candidate['top_pick_win_rate']:.2%} |\n\n候選只有在所有時間折次均可接受、並有獨立保留資料支持時才可考慮升級。\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
