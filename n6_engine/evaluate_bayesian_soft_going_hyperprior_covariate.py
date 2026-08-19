#!/usr/bin/env python3
"""Candidate-only Bayesian calibration with soft-going hyperprior and condition-ELO covariate.

The model uses only pre-race fields. Each expanding time-series fold estimates the
condition-ELO standardization statistics and posterior exclusively from races before
the held-out fold. Soft-going receives a tighter random-intercept hyperprior and all
post-calibration scores are projected back to the frozen N6 baseline ranking.
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
    GOINGS, PARENTS, TARGET_COLUMN, expit, folds_for_going, logit,
    normalise, parent_group, race_metrics, rank_protect, score_all,
)
from n6.config import RANDOM_SEED, REPORTS_DIR
from n6.feature_engineering import load_training_frame

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("PYTENSOR_FLAGS", "base_compiledir=/home/ubuntu/n6_engine/.pytensor_cache,compiledir_format=compiledir_%(platform)s-%(python_version)s")

EXPERIMENT_ID = "bayesian_soft_going_hyperprior_covariate_v1"
OUTPUT_DIR = REPORTS_DIR / "candidates" / EXPERIMENT_ID
SUMMARY_PATH = OUTPUT_DIR / "soft_going_hyperprior_covariate_summary.json"
FOLDS_PATH = OUTPUT_DIR / "soft_going_hyperprior_covariate_folds.csv"
OOF_PATH = OUTPUT_DIR / "soft_going_hyperprior_covariate_oof_predictions.csv"
POSTERIOR_PATH = OUTPUT_DIR / "soft_going_hyperprior_covariate_posterior_summary.csv"
REPORT_PATH = OUTPUT_DIR / "N6_SOFT_GOING_HYPERPRIOR_COVARIATE_CV.md"
SOFT_GOING = "軟地"
DRAWS, TUNE, CHAINS, TARGET_ACCEPT = 600, 1200, 2, 0.995


def fit_calibrator(calibration: pd.DataFrame, seed: int) -> tuple[dict[str, Any], pd.DataFrame, dict[str, float]]:
    cal = calibration.copy()
    elo_mean = float(cal["horse_condition_elo_pre"].mean())
    elo_std = max(float(cal["horse_condition_elo_pre"].std(ddof=0)), 1.0)
    cal["condition_elo_z"] = (cal["horse_condition_elo_pre"] - elo_mean) / elo_std
    # Aggregate calibration observations in two dimensions. This preserves real wins and
    # exposure counts while reducing an otherwise slow 20k+ row Bernoulli likelihood.
    cal["base_logit"] = logit(cal["baseline_probability"].to_numpy(dtype=float))
    p_bins = min(12, int(cal["base_logit"].nunique()))
    e_bins = min(8, int(cal["condition_elo_z"].nunique()))
    cal["p_bin"] = pd.qcut(cal["base_logit"], q=p_bins, duplicates="drop")
    cal["e_bin"] = pd.qcut(cal["condition_elo_z"], q=e_bins, duplicates="drop")
    aggregate = cal.groupby(["going", "p_bin", "e_bin"], observed=True).agg(
        n=(TARGET_COLUMN, "size"), wins=(TARGET_COLUMN, "sum"),
        base_logit=("base_logit", "mean"), condition_elo_z=("condition_elo_z", "mean"),
    ).reset_index()
    aggregate["going_idx"] = aggregate["going"].map({going: idx for idx, going in enumerate(GOINGS)}).astype(int)
    parent_index = np.asarray([PARENTS.index(parent_group(going)) for going in GOINGS], dtype=int)
    going_index = cal["going"].map({going: idx for idx, going in enumerate(GOINGS)}).to_numpy(dtype=int)
    soft_index = GOINGS.index(SOFT_GOING)
    with pm.Model(coords={"going": list(GOINGS), "parent": list(PARENTS), "obs": np.arange(len(aggregate))}) as model:
        parent_idx = pm.Data("parent_idx", parent_index, dims="going")
        obs_going = pm.Data("obs_going", aggregate["going_idx"].to_numpy(dtype=int), dims="obs")
        base_logit = pm.Data("base_logit", aggregate["base_logit"].to_numpy(dtype=float), dims="obs")
        elo_z = pm.Data("elo_z", aggregate["condition_elo_z"].to_numpy(dtype=float), dims="obs")
        parent_alpha = pm.Normal("parent_alpha", mu=0.0, sigma=0.75, dims="parent")
        sigma_parent = pm.HalfNormal("sigma_parent", sigma=0.15, dims="parent")
        sigma_soft = pm.HalfNormal("sigma_soft", sigma=0.08)
        sigma_base = sigma_parent[parent_idx]
        sigma_going = pt.set_subtensor(sigma_base[soft_index], sigma_soft)
        alpha_z = pm.Normal("alpha_z", mu=0.0, sigma=1.0, dims="going")
        alpha = pm.Deterministic("alpha", parent_alpha[parent_idx] + alpha_z * sigma_going, dims="going")
        shared_log_beta = pm.Normal("shared_log_beta", mu=0.0, sigma=0.30)
        shared_beta = pm.Deterministic("shared_beta", pt.exp(shared_log_beta))
        beta_condition_elo = pm.Normal("beta_condition_elo", mu=0.0, sigma=0.07)
        eta = alpha[obs_going] + shared_beta * base_logit + beta_condition_elo * elo_z
        pm.Binomial("outcome", n=aggregate["n"].to_numpy(dtype=int), p=pm.math.sigmoid(eta), observed=aggregate["wins"].to_numpy(dtype=int), dims="obs")
        trace = pm.sample(draws=DRAWS, tune=TUNE, chains=CHAINS, cores=1, target_accept=TARGET_ACCEPT,
                          nuts={"max_treedepth": 14}, random_seed=seed, progressbar=False,
                          compute_convergence_checks=False)
    alpha_values = trace.posterior["alpha"].stack(sample=("chain", "draw")).values
    beta_values = trace.posterior["shared_beta"].stack(sample=("chain", "draw")).values
    condition_values = trace.posterior["beta_condition_elo"].stack(sample=("chain", "draw")).values
    params: dict[str, Any] = {"alpha": alpha_values.mean(axis=1), "shared_beta": float(beta_values.mean()),
                              "beta_condition_elo": float(condition_values.mean()), "elo_mean": elo_mean, "elo_std": elo_std}
    posterior = pd.DataFrame({"going": GOINGS, "parent_group": [parent_group(item) for item in GOINGS],
        "alpha_mean": params["alpha"], "alpha_hdi_5": np.quantile(alpha_values, .05, axis=1),
        "alpha_hdi_95": np.quantile(alpha_values, .95, axis=1), "shared_beta_mean": params["shared_beta"],
        "beta_condition_elo_mean": params["beta_condition_elo"], "beta_condition_elo_hdi_5": float(np.quantile(condition_values, .05)),
        "beta_condition_elo_hdi_95": float(np.quantile(condition_values, .95)), "elo_mean": elo_mean, "elo_std": elo_std})
    summary = az.summary(trace, var_names=["alpha", "shared_beta", "beta_condition_elo", "sigma_parent", "sigma_soft"], round_to=None)
    diagnostics = {"mcmc_max_rhat": float(summary["r_hat"].dropna().max()), "mcmc_min_ess_bulk": float(summary["ess_bulk"].dropna().min()),
                   "mcmc_divergences": int(trace.sample_stats["diverging"].sum().item())}
    return params, posterior, diagnostics


def apply_fold(scored: pd.DataFrame, fold: Any) -> tuple[pd.DataFrame, dict[str, Any], pd.DataFrame]:
    calibration = scored[scored["race_date"] < fold.calibration_end].copy()
    test = scored[scored["race_group"].isin(fold.test_races)].copy()
    params, posterior, diagnostics = fit_calibrator(calibration, RANDOM_SEED + fold.fold_id + 700)
    indices = test["going"].map({going: idx for idx, going in enumerate(GOINGS)}).to_numpy(dtype=int)
    elo_z = (test["horse_condition_elo_pre"].to_numpy(dtype=float) - params["elo_mean"]) / params["elo_std"]
    raw = expit(np.asarray(params["alpha"])[indices] + float(params["shared_beta"]) * logit(test["baseline_probability"].to_numpy(dtype=float)) + float(params["beta_condition_elo"]) * elo_z)
    protected = rank_protect(raw, test["baseline_probability"].to_numpy(dtype=float), test["race_group"])
    test["soft_going_hyperprior_covariate_rank_protected_probability"] = normalise(protected, test["race_group"])
    info = {"going": fold.going, "fold_id": fold.fold_id, "calibration_end_exclusive": fold.calibration_end.isoformat(),
            "calibration_rows": int(len(calibration)), "calibration_races": int(calibration["race_group"].nunique()),
            "test_rows": int(len(test)), "test_races": int(test["race_group"].nunique()), **diagnostics}
    return test, info, posterior


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    raw_training = load_training_frame()
    scored = score_all(raw_training)
    condition_elo_lookup = raw_training[["race_group", "horse_name", "horse_condition_elo_pre"]].drop_duplicates(
        subset=["race_group", "horse_name"]
    )
    scored = scored.merge(condition_elo_lookup, on=["race_group", "horse_name"], how="left", validate="one_to_one")
    if scored["horse_condition_elo_pre"].isna().any():
        raise ValueError("Missing pre-race condition ELO after race-group and horse-name merge.")
    predictions: list[pd.DataFrame] = []; rows: list[dict[str, Any]] = []; posteriors: list[pd.DataFrame] = []
    for fold in folds_for_going(scored, SOFT_GOING):
        test, info, posterior = apply_fold(scored, fold)
        baseline = race_metrics(test, "baseline_probability")
        candidate = race_metrics(test, "soft_going_hyperprior_covariate_rank_protected_probability")
        if not np.isclose(baseline["top_pick_win_rate"], candidate["top_pick_win_rate"]):
            raise AssertionError("Rank protection changed a fold top-pick win rate.")
        info.update({"baseline_race_brier": baseline["race_brier"], "candidate_race_brier": candidate["race_brier"],
                     "brier_delta": candidate["race_brier"] - baseline["race_brier"], "baseline_top_pick_win_rate": baseline["top_pick_win_rate"],
                     "candidate_top_pick_win_rate": candidate["top_pick_win_rate"], "baseline_ece": baseline["ece"], "candidate_ece": candidate["ece"]})
        posterior["fold_id"] = fold.fold_id; predictions.append(test); rows.append(info); posteriors.append(posterior)
    oof = pd.concat(predictions, ignore_index=True); folds = pd.DataFrame(rows); posterior = pd.concat(posteriors, ignore_index=True)
    baseline = race_metrics(oof, "baseline_probability"); candidate = race_metrics(oof, "soft_going_hyperprior_covariate_rank_protected_probability")
    if not np.isclose(baseline["top_pick_win_rate"], candidate["top_pick_win_rate"]):
        raise AssertionError("Rank protection changed OOF top-pick win rate.")
    result = {"engine": "N6 Neural Calculation Engine", "experiment_id": EXPERIMENT_ID, "generated_at_utc": datetime.now(UTC).isoformat(),
              "candidate_only_guarantee": "No production model, API, service, calibration layer, or V10 data was modified.",
              "method": {"going_effect": "hierarchical random intercept with soft-going specific hyperprior", "soft_sigma_prior": "HalfNormal(0.08)",
                         "covariate": "horse_condition_elo_pre, fold-standardized", "covariate_prior": "Normal(0, 0.07)", "target_accept": TARGET_ACCEPT, "draws": DRAWS, "tune": TUNE, "chains": CHAINS, "rank_protection": "non-increasing projection in baseline N6 order"},
              "soft_going": {"folds": int(folds["fold_id"].nunique()), "races": int(oof["race_group"].nunique()), "baseline_race_brier": baseline["race_brier"], "candidate_race_brier": candidate["race_brier"], "brier_delta": candidate["race_brier"]-baseline["race_brier"], "baseline_top_pick_win_rate": baseline["top_pick_win_rate"], "candidate_top_pick_win_rate": candidate["top_pick_win_rate"], "baseline_ece": baseline["ece"], "candidate_ece": candidate["ece"]}, "folds": rows}
    SUMMARY_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    folds.to_csv(FOLDS_PATH, index=False, encoding="utf-8-sig"); oof.to_csv(OOF_PATH, index=False, encoding="utf-8-sig"); posterior.to_csv(POSTERIOR_PATH, index=False, encoding="utf-8-sig")
    REPORT_PATH.write_text("# N6：軟地 Going 超先驗與條件 ELO 校準候選\n\n> 僅以每折校準期的賽前資料估計。軟地截距採更強的 `HalfNormal(0.08)` 超先驗收縮，並加入折內標準化的條件 ELO 校正係數；所有輸出均保護原始 N6 首選。\n\n| Going | 折數／OOF 場數 | 基準 Brier | 候選 Brier | 差異 | 基準 ECE | 候選 ECE |\n| --- | ---: | ---: | ---: | ---: | ---: | ---: |\n" + f"| 軟地 | {result['soft_going']['folds']}／{result['soft_going']['races']} | {baseline['race_brier']:.6f} | {candidate['race_brier']:.6f} | {result['soft_going']['brier_delta']:+.6f} | {baseline['ece']:.6f} | {candidate['ece']:.6f} |\n\n升級要求：每折 R-hat ≤ 1.05、bulk ESS ≥ 200、零發散，且 OOF 首選完整保護。\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2)); return 0

if __name__ == "__main__":
    raise SystemExit(main())
