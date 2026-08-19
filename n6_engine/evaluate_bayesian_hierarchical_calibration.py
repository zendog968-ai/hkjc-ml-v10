#!/usr/bin/env python3
"""Candidate-only hierarchical Bayesian probability calibration for N6.

A group-specific logit calibration model is fitted from strictly historical data:
    y ~ Bernoulli(sigmoid(alpha_going + beta_going * logit(p_n6)))
where alpha/beta of each exact Going are non-centred draws around a parent
surface family's parameters.  Posterior pooling replaces a manually selected
tau.  Calibrated scores are normalized within each race and projected back to
the original N6 ranking before metrics are evaluated.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

import arviz as az
import numpy as np
import pandas as pd
import pymc as pm
import pytensor.tensor as pt
import torch

from n6.config import MODEL_PATH, PREPROCESSOR_PATH, RANDOM_SEED, REPORTS_DIR, TARGET_COLUMN
from n6.feature_engineering import load_training_frame, score_to_race_probabilities
from n6.model import load_model_bundle

# Limit Bayesian numerical libraries so this candidate process does not compete with N6 service workers.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("PYTENSOR_FLAGS", "base_compiledir=/home/ubuntu/n6_engine/.pytensor_cache,compiledir_format=compiledir_%(platform)s-%(python_version)s")

EXPERIMENT_ID = "bayesian_hierarchical_calibration_v1"
OUTPUT_DIR = REPORTS_DIR / "candidates" / EXPERIMENT_ID
MODEL_DIR = Path("models") / "candidates" / EXPERIMENT_ID
SUMMARY_PATH = OUTPUT_DIR / "bayesian_hierarchical_cv_summary.json"
FOLDS_PATH = OUTPUT_DIR / "bayesian_hierarchical_cv_folds.csv"
OOF_PATH = OUTPUT_DIR / "bayesian_hierarchical_cv_oof_predictions.csv"
POSTERIOR_PATH = OUTPUT_DIR / "bayesian_hierarchical_posterior_summary.csv"
REPORT_PATH = OUTPUT_DIR / "N6_BAYESIAN_HIERARCHICAL_CALIBRATION_CV.md"
RARE_GOINGS = ("黏地", "軟地")
GOINGS = ("好地", "好地至快地", "好地至黏地", "黏地", "軟地", "濕快地", "濕慢地", "封地")
PARENTS = ("turf_dry", "turf_wet", "all_weather")
MIN_LOCAL_RACES = 5
N_SPLITS = 3
BINS = 12
DRAWS = 500
TUNE = 500
CHAINS = 2
EPSILON = 1e-7


@dataclass(frozen=True)
class Fold:
    going: str
    fold_id: int
    calibration_end: pd.Timestamp
    test_races: tuple[str, ...]


def parent_group(going: str) -> str:
    if str(going) == "封地":
        return "all_weather"
    if str(going) in {"好地", "好地至快地"}:
        return "turf_dry"
    return "turf_wet"


def logit(probability: np.ndarray) -> np.ndarray:
    p = np.clip(np.asarray(probability, dtype=float), EPSILON, 1.0 - EPSILON)
    return np.log(p / (1.0 - p))


def expit(value: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(value, -30.0, 30.0)))


def rank_protect(raw: np.ndarray, baseline: np.ndarray, race_groups: pd.Series) -> np.ndarray:
    adjusted = np.asarray(raw, dtype=float).copy()
    ids = pd.Series(np.arange(len(adjusted)))
    for _, locations in ids.groupby(race_groups.to_numpy(), sort=False):
        idx = locations.to_numpy()
        order = np.argsort(-baseline[idx], kind="stable")
        ordered = idx[order]
        values = adjusted[ordered].copy()
        for position in range(1, len(values)):
            values[position] = min(values[position], values[position - 1] * (1.0 - 1e-10))
        adjusted[ordered] = values
    return adjusted


def normalise(raw: np.ndarray, race_groups: pd.Series) -> np.ndarray:
    raw = np.clip(np.asarray(raw, dtype=float), EPSILON, None)
    result = np.zeros_like(raw)
    ids = pd.Series(np.arange(len(raw)))
    for _, locations in ids.groupby(race_groups.to_numpy(), sort=False):
        idx = locations.to_numpy()
        result[idx] = raw[idx] / raw[idx].sum()
    return result


def ece(frame: pd.DataFrame, column: str, bins: int = 5) -> float | None:
    data = frame[[TARGET_COLUMN, column]].dropna().copy()
    if len(data) < 30 or data[column].nunique() < 2:
        return None
    try:
        data["bin"] = pd.qcut(data[column], q=min(bins, data[column].nunique()), duplicates="drop")
    except ValueError:
        return None
    total = 0.0
    for _, group in data.groupby("bin", observed=True):
        total += len(group) * abs(float(group[TARGET_COLUMN].mean() - group[column].mean()))
    return float(total / len(data))


def race_metrics(frame: pd.DataFrame, column: str) -> dict[str, float | None]:
    brier = []
    top = []
    for _, group in frame.groupby("race_group", sort=False):
        p = group[column].to_numpy(dtype=float)
        y = group[TARGET_COLUMN].to_numpy(dtype=float)
        brier.append(float(np.square(y - p).sum()))
        top.append(float(y[int(np.argmax(p))]))
    return {"race_brier": float(np.mean(brier)), "top_pick_win_rate": float(np.mean(top)), "ece": ece(frame, column)}


def score_all(frame: pd.DataFrame) -> pd.DataFrame:
    bundle = load_model_bundle(MODEL_PATH, PREPROCESSOR_PATH)
    if int(bundle.metadata.get("input_dim", -1)) != 72:
        raise ValueError("This experiment requires the active 72-dimensional N6 production model.")
    features = bundle.metadata["feature_contract"]
    matrix = np.asarray(bundle.preprocessor.transform(frame[features]), dtype=np.float32)
    with torch.no_grad():
        logits = bundle.model(torch.tensor(matrix, dtype=torch.float32)).cpu().numpy()
    output = frame[["race_date", "racecourse", "race_no", "race_group", "horse_name", "going", TARGET_COLUMN]].copy()
    output["parent_group"] = output["going"].map(parent_group)
    output["baseline_probability"] = score_to_race_probabilities(logits / float(bundle.metadata.get("temperature", 1.0)), output["race_group"])
    output["production_release"] = bundle.metadata.get("production_release")
    return output


def folds_for_going(scored: pd.DataFrame, going: str) -> list[Fold]:
    race_table = scored[scored["going"] == going].sort_values(["race_date", "race_group"]).drop_duplicates("race_group")
    race_ids = race_table["race_group"].tolist()
    if len(race_ids) <= MIN_LOCAL_RACES:
        return []
    chunks = [tuple(chunk.tolist()) for chunk in np.array_split(np.asarray(race_ids[MIN_LOCAL_RACES:], dtype=object), min(N_SPLITS, len(race_ids) - MIN_LOCAL_RACES)) if len(chunk)]
    indexed = race_table.set_index("race_group")
    return [Fold(going=going, fold_id=index, calibration_end=pd.Timestamp(indexed.loc[chunk[0], "race_date"]), test_races=chunk) for index, chunk in enumerate(chunks, start=1)]


def aggregate_calibration(calibration: pd.DataFrame) -> pd.DataFrame:
    """Compress binary rows into probability bins while retaining group-specific likelihoods."""
    data = calibration.copy()
    # Quantile bins are calculated only from the fold's historical calibration sample.
    data["p_bin"] = pd.qcut(data["baseline_probability"], q=BINS, labels=False, duplicates="drop")
    grouped = (
        data.groupby(["going", "p_bin"], observed=True)
        .agg(n=(TARGET_COLUMN, "size"), wins=(TARGET_COLUMN, "sum"), mean_p=("baseline_probability", "mean"))
        .reset_index()
    )
    grouped["going"] = pd.Categorical(grouped["going"], categories=GOINGS)
    grouped = grouped.dropna(subset=["going"]).copy()
    grouped["going_idx"] = grouped["going"].cat.codes.astype(int)
    return grouped


def fit_hierarchical_calibrator(calibration: pd.DataFrame, seed: int) -> tuple[dict[str, np.ndarray], pd.DataFrame, dict[str, float]]:
    aggregate = aggregate_calibration(calibration)
    parent_index = np.asarray([PARENTS.index(parent_group(going)) for going in GOINGS], dtype=int)
    with pm.Model(coords={"going": list(GOINGS), "parent": list(PARENTS), "obs": np.arange(len(aggregate))}) as model:
        parent_idx = pm.Data("parent_idx", parent_index, dims="going")
        obs_going = pm.Data("obs_going", aggregate["going_idx"].to_numpy(), dims="obs")
        x = pm.Data("x", logit(aggregate["mean_p"].to_numpy()), dims="obs")
        trials = pm.Data("trials", aggregate["n"].to_numpy(dtype=int), dims="obs")
        parent_alpha = pm.Normal("parent_alpha", mu=0.0, sigma=1.0, dims="parent")
        parent_log_beta = pm.Normal("parent_log_beta", mu=0.0, sigma=0.45, dims="parent")
        sigma_alpha = pm.HalfNormal("sigma_alpha", sigma=0.55, dims="parent")
        sigma_log_beta = pm.HalfNormal("sigma_log_beta", sigma=0.35, dims="parent")
        alpha_z = pm.Normal("alpha_z", mu=0.0, sigma=1.0, dims="going")
        log_beta_z = pm.Normal("log_beta_z", mu=0.0, sigma=1.0, dims="going")
        alpha = pm.Deterministic("alpha", parent_alpha[parent_idx] + alpha_z * sigma_alpha[parent_idx], dims="going")
        log_beta = pm.Deterministic("log_beta", parent_log_beta[parent_idx] + log_beta_z * sigma_log_beta[parent_idx], dims="going")
        beta = pm.Deterministic("beta", pt.exp(log_beta), dims="going")
        probability = pm.math.sigmoid(alpha[obs_going] + beta[obs_going] * x)
        pm.Binomial("outcome", n=trials, p=probability, observed=aggregate["wins"].to_numpy(dtype=int), dims="obs")
        trace = pm.sample(draws=DRAWS, tune=TUNE, chains=CHAINS, cores=1, target_accept=0.92, random_seed=seed, progressbar=False, compute_convergence_checks=False)
    alpha_values = trace.posterior["alpha"].stack(sample=("chain", "draw")).values
    beta_values = trace.posterior["beta"].stack(sample=("chain", "draw")).values
    parameters = {"alpha": alpha_values.mean(axis=1), "beta": beta_values.mean(axis=1)}
    posterior = pd.DataFrame({"going": GOINGS, "parent_group": [parent_group(item) for item in GOINGS], "alpha_mean": parameters["alpha"], "alpha_hdi_5": np.quantile(alpha_values, 0.05, axis=1), "alpha_hdi_95": np.quantile(alpha_values, 0.95, axis=1), "beta_mean": parameters["beta"], "beta_hdi_5": np.quantile(beta_values, 0.05, axis=1), "beta_hdi_95": np.quantile(beta_values, 0.95, axis=1)})
    convergence = az.summary(trace, var_names=["alpha", "beta", "sigma_alpha", "sigma_log_beta"], round_to=None)
    diagnostics = {
        "mcmc_max_rhat": float(convergence["r_hat"].dropna().max()),
        "mcmc_min_ess_bulk": float(convergence["ess_bulk"].dropna().min()),
        "mcmc_divergences": int(trace.sample_stats["diverging"].sum().item()),
    }
    return parameters, posterior, diagnostics


def apply_fold(scored: pd.DataFrame, fold: Fold) -> tuple[pd.DataFrame, dict[str, Any], pd.DataFrame]:
    calibration = scored[scored["race_date"] < fold.calibration_end].copy()
    test = scored[scored["race_group"].isin(fold.test_races)].copy()
    parameters, posterior, diagnostics = fit_hierarchical_calibrator(calibration, RANDOM_SEED + fold.fold_id + (100 if fold.going == "軟地" else 0))
    indices = test["going"].map({going: index for index, going in enumerate(GOINGS)}).to_numpy(dtype=int)
    raw = expit(parameters["alpha"][indices] + parameters["beta"][indices] * logit(test["baseline_probability"].to_numpy()))
    protected = rank_protect(raw, test["baseline_probability"].to_numpy(dtype=float), test["race_group"])
    test["bayesian_hierarchical_rank_protected_probability"] = normalise(protected, test["race_group"])
    test["fold_id"] = fold.fold_id
    info = {"going": fold.going, "fold_id": fold.fold_id, "calibration_end_exclusive": fold.calibration_end.isoformat(), "calibration_rows": int(len(calibration)), "calibration_races": int(calibration["race_group"].nunique()), "test_rows": int(len(test)), "test_races": int(test["race_group"].nunique()), **diagnostics}
    return test, info, posterior


def report_markdown(summary: dict[str, Any]) -> str:
    lines = ["# N6：階層貝氏 Going 校準與排名保護交叉驗證", "", "> 候選模型以 exact Going 的 logit 校準截距與斜率為群組參數，並由父曲面類型的超參數自動部分池化。每一折嚴格使用早於測試折的資料進行貝氏後驗推斷；所有候選輸出均未接入生產服務。", "", "| Going | 折數／OOF 場數 | 基準 Brier | 貝氏候選 Brier | 差異 | 基準首選 | 候選首選 |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for row in summary["per_going"]:
        lines.append(f"| {row['going']} | {row['folds']}／{row['races']} | {row['baseline_race_brier']:.6f} | {row['candidate_race_brier']:.6f} | {row['brier_delta']:+.6f} | {row['baseline_top_pick_win_rate']:.2%} | {row['candidate_top_pick_win_rate']:.2%} |")
    lines.extend(["", "## 限制", "", "此實驗驗證校準層的時間順序、後驗收縮和排名保護。由於基礎 N6 MLP 已在較早資料訓練，不能將此結果解讀為完整端到端重訓的外樣本表現。稀少 Going 的場數仍不足以形成部署結論。", ""])
    return "\n".join(lines)


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    scored = score_all(load_training_frame())
    predictions: list[pd.DataFrame] = []
    fold_rows: list[dict[str, Any]] = []
    posterior_rows: list[pd.DataFrame] = []
    for going in RARE_GOINGS:
        folds = folds_for_going(scored, going)
        for fold in folds:
            test, info, posterior = apply_fold(scored, fold)
            baseline = race_metrics(test, "baseline_probability")
            candidate = race_metrics(test, "bayesian_hierarchical_rank_protected_probability")
            if not np.isclose(baseline["top_pick_win_rate"], candidate["top_pick_win_rate"]):
                raise AssertionError("Rank protection changed a fold's top-pick win rate.")
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
        candidate = race_metrics(group, "bayesian_hierarchical_rank_protected_probability")
        if not np.isclose(baseline["top_pick_win_rate"], candidate["top_pick_win_rate"]):
            raise AssertionError("Rank protection changed an OOF top-pick win rate.")
        per_going.append({"going": going, "folds": int(folds_df[folds_df["going"] == going]["fold_id"].nunique()), "races": int(group["race_group"].nunique()), "baseline_race_brier": baseline["race_brier"], "candidate_race_brier": candidate["race_brier"], "brier_delta": candidate["race_brier"] - baseline["race_brier"], "baseline_top_pick_win_rate": baseline["top_pick_win_rate"], "candidate_top_pick_win_rate": candidate["top_pick_win_rate"], "baseline_ece": baseline["ece"], "candidate_ece": candidate["ece"]})
    summary = {"engine": "N6 Neural Calculation Engine", "experiment_id": EXPERIMENT_ID, "generated_at_utc": datetime.now(UTC).isoformat(), "candidate_only_guarantee": "No production model, production calibration layer, API, service, or V10 data was modified.", "method": {"link": "logit", "exact_going_parameters": "alpha_going and positive beta_going", "partial_pooling": "non-centred exact-going parameters around parent surface groups", "parents": {parent: [going for going in GOINGS if parent_group(going) == parent] for parent in PARENTS}, "posterior": {"draws": DRAWS, "tune": TUNE, "chains": CHAINS}, "rank_protection": "non-increasing projection in original N6 ranking"}, "per_going": per_going, "folds": fold_rows, "artifacts": {"fold_metrics": str(FOLDS_PATH), "oof_predictions": str(OOF_PATH), "posterior_summary": str(POSTERIOR_PATH)}}
    SUMMARY_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    folds_df.to_csv(FOLDS_PATH, index=False, encoding="utf-8-sig")
    oof.to_csv(OOF_PATH, index=False, encoding="utf-8-sig")
    posterior_df.to_csv(POSTERIOR_PATH, index=False, encoding="utf-8-sig")
    REPORT_PATH.write_text(report_markdown(summary), encoding="utf-8")
    print(json.dumps({"per_going": per_going, "folds": fold_rows, "artifacts": summary["artifacts"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
