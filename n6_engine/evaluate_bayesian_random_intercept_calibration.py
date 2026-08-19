#!/usr/bin/env python3
"""Candidate-only Bayesian calibration: hierarchical Going intercepts + shared slope.

This deliberately removes sparse Going-specific slopes.  Exact Going intercepts
are non-centred draws from surface-family parent intercepts; a single positive
slope calibrates the frozen N6 baseline logit for every Going.  NUTS uses
`target_accept=0.99`, after which posterior diagnostics, race normalization and
baseline-rank protection are checked before any result can be considered.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import arviz as az
import numpy as np
import pandas as pd
import pymc as pm
import pytensor.tensor as pt

from evaluate_bayesian_hierarchical_calibration import (
    EPSILON,
    GOINGS,
    PARENTS,
    RARE_GOINGS,
    TARGET_COLUMN,
    aggregate_calibration,
    expit,
    folds_for_going,
    logit,
    normalise,
    parent_group,
    race_metrics,
    rank_protect,
    score_all,
)
from n6.config import RANDOM_SEED, REPORTS_DIR
from n6.feature_engineering import load_training_frame

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("PYTENSOR_FLAGS", "base_compiledir=/home/ubuntu/n6_engine/.pytensor_cache,compiledir_format=compiledir_%(platform)s-%(python_version)s")

EXPERIMENT_ID = "bayesian_random_intercept_calibration_v1"
OUTPUT_DIR = REPORTS_DIR / "candidates" / EXPERIMENT_ID
SUMMARY_PATH = OUTPUT_DIR / "bayesian_random_intercept_cv_summary.json"
FOLDS_PATH = OUTPUT_DIR / "bayesian_random_intercept_cv_folds.csv"
OOF_PATH = OUTPUT_DIR / "bayesian_random_intercept_cv_oof_predictions.csv"
POSTERIOR_PATH = OUTPUT_DIR / "bayesian_random_intercept_posterior_summary.csv"
REPORT_PATH = OUTPUT_DIR / "N6_BAYESIAN_RANDOM_INTERCEPT_CALIBRATION_CV.md"
DRAWS = 600
TUNE = 600
CHAINS = 2
TARGET_ACCEPT = 0.99


def fit_calibrator(calibration: pd.DataFrame, seed: int) -> tuple[dict[str, np.ndarray | float], pd.DataFrame, dict[str, float]]:
    aggregate = aggregate_calibration(calibration)
    parent_index = np.asarray([PARENTS.index(parent_group(going)) for going in GOINGS], dtype=int)
    with pm.Model(coords={"going": list(GOINGS), "parent": list(PARENTS), "obs": np.arange(len(aggregate))}) as model:
        parent_idx = pm.Data("parent_idx", parent_index, dims="going")
        obs_going = pm.Data("obs_going", aggregate["going_idx"].to_numpy(), dims="obs")
        x = pm.Data("x", logit(aggregate["mean_p"].to_numpy()), dims="obs")
        trials = pm.Data("trials", aggregate["n"].to_numpy(dtype=int), dims="obs")
        parent_alpha = pm.Normal("parent_alpha", mu=0.0, sigma=0.75, dims="parent")
        sigma_alpha = pm.HalfNormal("sigma_alpha", sigma=0.25, dims="parent")
        alpha_z = pm.Normal("alpha_z", mu=0.0, sigma=1.0, dims="going")
        alpha = pm.Deterministic("alpha", parent_alpha[parent_idx] + alpha_z * sigma_alpha[parent_idx], dims="going")
        shared_log_beta = pm.Normal("shared_log_beta", mu=0.0, sigma=0.30)
        shared_beta = pm.Deterministic("shared_beta", pt.exp(shared_log_beta))
        probability = pm.math.sigmoid(alpha[obs_going] + shared_beta * x)
        pm.Binomial("outcome", n=trials, p=probability, observed=aggregate["wins"].to_numpy(dtype=int), dims="obs")
        trace = pm.sample(
            draws=DRAWS,
            tune=TUNE,
            chains=CHAINS,
            cores=1,
            target_accept=TARGET_ACCEPT,
            nuts={"max_treedepth": 12},
            random_seed=seed,
            progressbar=False,
            compute_convergence_checks=False,
        )
    alpha_values = trace.posterior["alpha"].stack(sample=("chain", "draw")).values
    beta_values = trace.posterior["shared_beta"].stack(sample=("chain", "draw")).values
    parameters: dict[str, np.ndarray | float] = {"alpha": alpha_values.mean(axis=1), "shared_beta": float(beta_values.mean())}
    posterior = pd.DataFrame(
        {
            "going": GOINGS,
            "parent_group": [parent_group(item) for item in GOINGS],
            "alpha_mean": parameters["alpha"],
            "alpha_hdi_5": np.quantile(alpha_values, 0.05, axis=1),
            "alpha_hdi_95": np.quantile(alpha_values, 0.95, axis=1),
            "shared_beta_mean": float(beta_values.mean()),
            "shared_beta_hdi_5": float(np.quantile(beta_values, 0.05)),
            "shared_beta_hdi_95": float(np.quantile(beta_values, 0.95)),
        }
    )
    summary = az.summary(trace, var_names=["alpha", "shared_beta", "sigma_alpha"], round_to=None)
    diagnostics = {
        "mcmc_max_rhat": float(summary["r_hat"].dropna().max()),
        "mcmc_min_ess_bulk": float(summary["ess_bulk"].dropna().min()),
        "mcmc_divergences": int(trace.sample_stats["diverging"].sum().item()),
    }
    return parameters, posterior, diagnostics


def apply_fold(scored: pd.DataFrame, fold: Any) -> tuple[pd.DataFrame, dict[str, Any], pd.DataFrame]:
    calibration = scored[scored["race_date"] < fold.calibration_end].copy()
    test = scored[scored["race_group"].isin(fold.test_races)].copy()
    parameters, posterior, diagnostics = fit_calibrator(calibration, RANDOM_SEED + fold.fold_id + (100 if fold.going == "軟地" else 0))
    indices = test["going"].map({going: index for index, going in enumerate(GOINGS)}).to_numpy(dtype=int)
    raw = expit(np.asarray(parameters["alpha"])[indices] + float(parameters["shared_beta"]) * logit(test["baseline_probability"].to_numpy()))
    protected = rank_protect(raw, test["baseline_probability"].to_numpy(dtype=float), test["race_group"])
    test["bayesian_random_intercept_rank_protected_probability"] = normalise(protected, test["race_group"])
    test["fold_id"] = fold.fold_id
    info = {
        "going": fold.going,
        "fold_id": fold.fold_id,
        "calibration_end_exclusive": fold.calibration_end.isoformat(),
        "calibration_rows": int(len(calibration)),
        "calibration_races": int(calibration["race_group"].nunique()),
        "test_rows": int(len(test)),
        "test_races": int(test["race_group"].nunique()),
        **diagnostics,
    }
    return test, info, posterior


def write_report(summary: dict[str, Any]) -> None:
    lines = [
        "# N6：簡化階層貝氏校準（Going 隨機截距＋共用斜率）",
        "",
        "> 此候選以 Going 隨機截距向父曲面類型部分池化，並以一個全域正斜率校準 N6 logit。它移除了低樣本 Going 的獨立斜率，以降低識別問題；每折使用 `target_accept=0.99` 的 NUTS。候選不會改動生產模型、API 或 V10 資料。",
        "",
        "| Going | 折數／OOF 場數 | 基準 Brier | 候選 Brier | 差異 | 基準首選 | 候選首選 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in summary["per_going"]:
        lines.append(f"| {row['going']} | {row['folds']}／{row['races']} | {row['baseline_race_brier']:.6f} | {row['candidate_race_brier']:.6f} | {row['brier_delta']:+.6f} | {row['baseline_top_pick_win_rate']:.2%} | {row['candidate_top_pick_win_rate']:.2%} |")
    lines.extend(["", "## 後驗診斷", "", "升級候選必須同時滿足：每折 R-hat 不高於 1.05、bulk ESS 不低於 200、零發散轉換，以及所有 OOF 場次的首選完全與基準一致。未達成任何一項即只保留研究產物。", ""])
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    scored = score_all(load_training_frame())
    predictions: list[pd.DataFrame] = []
    fold_rows: list[dict[str, Any]] = []
    posterior_rows: list[pd.DataFrame] = []
    for going in RARE_GOINGS:
        for fold in folds_for_going(scored, going):
            test, info, posterior = apply_fold(scored, fold)
            baseline = race_metrics(test, "baseline_probability")
            candidate = race_metrics(test, "bayesian_random_intercept_rank_protected_probability")
            if not np.isclose(baseline["top_pick_win_rate"], candidate["top_pick_win_rate"]):
                raise AssertionError("Rank protection changed a fold top-pick win rate.")
            info.update({"baseline_race_brier": baseline["race_brier"], "candidate_race_brier": candidate["race_brier"], "brier_delta": candidate["race_brier"] - baseline["race_brier"], "baseline_top_pick_win_rate": baseline["top_pick_win_rate"], "candidate_top_pick_win_rate": candidate["top_pick_win_rate"], "baseline_ece": baseline["ece"], "candidate_ece": candidate["ece"]})
            posterior["test_going"] = going
            posterior["fold_id"] = fold.fold_id
            predictions.append(test)
            fold_rows.append(info)
            posterior_rows.append(posterior)
    oof = pd.concat(predictions, ignore_index=True)
    folds_df = pd.DataFrame(fold_rows)
    posterior_df = pd.concat(posterior_rows, ignore_index=True)
    per_going = []
    for going, group in oof.groupby("going", sort=True):
        baseline = race_metrics(group, "baseline_probability")
        candidate = race_metrics(group, "bayesian_random_intercept_rank_protected_probability")
        if not np.isclose(baseline["top_pick_win_rate"], candidate["top_pick_win_rate"]):
            raise AssertionError("Rank protection changed an OOF top-pick win rate.")
        per_going.append({"going": going, "folds": int(folds_df[folds_df["going"] == going]["fold_id"].nunique()), "races": int(group["race_group"].nunique()), "baseline_race_brier": baseline["race_brier"], "candidate_race_brier": candidate["race_brier"], "brier_delta": candidate["race_brier"] - baseline["race_brier"], "baseline_top_pick_win_rate": baseline["top_pick_win_rate"], "candidate_top_pick_win_rate": candidate["top_pick_win_rate"], "baseline_ece": baseline["ece"], "candidate_ece": candidate["ece"]})
    summary = {
        "engine": "N6 Neural Calculation Engine",
        "experiment_id": EXPERIMENT_ID,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "candidate_only_guarantee": "No production model, production calibration layer, API, service, or V10 data was modified.",
        "method": {"going_effect": "hierarchical random intercept", "slope": "single shared positive logit slope", "target_accept": TARGET_ACCEPT, "draws": DRAWS, "tune": TUNE, "chains": CHAINS, "rank_protection": "non-increasing projection in baseline N6 order"},
        "per_going": per_going,
        "folds": fold_rows,
        "artifacts": {"fold_metrics": str(FOLDS_PATH), "oof_predictions": str(OOF_PATH), "posterior_summary": str(POSTERIOR_PATH)},
    }
    SUMMARY_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    folds_df.to_csv(FOLDS_PATH, index=False, encoding="utf-8-sig")
    oof.to_csv(OOF_PATH, index=False, encoding="utf-8-sig")
    posterior_df.to_csv(POSTERIOR_PATH, index=False, encoding="utf-8-sig")
    write_report(summary)
    print(json.dumps({"per_going": per_going, "folds": fold_rows, "artifacts": summary["artifacts"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
