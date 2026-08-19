#!/usr/bin/env python3
"""Candidate-only odds-conditional two-stage probability calibration for N6 72D.

Stage 1 fits one isotonic mapping per historical odds band on validation data only.
Stage 2 maps those scores back to within-race probabilities.  The rank-protected
variant projects calibrated scores to the original within-race ordering before
normalization, so the baseline first choice is retained exactly.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
import torch
from sklearn.isotonic import IsotonicRegression

from n6.config import MODEL_PATH, PREPROCESSOR_PATH, RANDOM_SEED, REPORTS_DIR, TARGET_COLUMN
from n6.feature_engineering import load_training_frame, score_to_race_probabilities
from n6.model import load_model_bundle
from train import chronological_split, race_metrics

EXPERIMENT_ID = "conditional_two_stage_calibration_v1"
MODEL_DIR = Path("models") / "candidates" / EXPERIMENT_ID
REPORT_DIR = REPORTS_DIR / "candidates" / EXPERIMENT_ID
SUMMARY_PATH = REPORT_DIR / "conditional_calibration_summary.json"
METRICS_PATH = REPORT_DIR / "conditional_calibration_metrics.csv"
ODDS_PATH = REPORT_DIR / "conditional_calibration_odds_bands.csv"
PREDICTIONS_PATH = REPORT_DIR / "n6_test_predictions_conditional_calibrated.csv"
LAYER_PATH = MODEL_DIR / "conditional_two_stage_calibrators.joblib"
BOOTSTRAP_PATH = REPORT_DIR / "paired_race_bootstrap_validation.json"
REPORT_PATH = REPORT_DIR / "N6_CONDITIONAL_TWO_STAGE_CALIBRATION_REPORT.md"
ODDS_BANDS = ["1–<5", "5–<10", "10–<20", "20+", "missing/invalid"]
BOOTSTRAP_REPLICATES = 5000
MIN_BAND_ROWS = 100


def odds_band(values: pd.Series) -> pd.Series:
    odds = pd.to_numeric(values, errors="coerce")
    result = pd.Series("missing/invalid", index=values.index, dtype="object")
    result.loc[odds.ge(1.0) & odds.lt(5.0)] = "1–<5"
    result.loc[odds.ge(5.0) & odds.lt(10.0)] = "5–<10"
    result.loc[odds.ge(10.0) & odds.lt(20.0)] = "10–<20"
    result.loc[odds.ge(20.0)] = "20+"
    return result


def evaluate_base(frame: pd.DataFrame, bundle: Any, temperature: float) -> pd.DataFrame:
    values = np.asarray(bundle.preprocessor.transform(frame[bundle.metadata["feature_contract"]]), dtype=np.float32)
    with torch.no_grad():
        logits = bundle.model(torch.tensor(values, dtype=torch.float32)).cpu().numpy()
    output = frame[["race_date", "racecourse", "race_no", "horse_name", "race_group", TARGET_COLUMN, "win_odds"]].copy()
    output["baseline_probability"] = score_to_race_probabilities(logits / temperature, output["race_group"])
    output["odds_band"] = odds_band(output["win_odds"])
    return output


def renormalize(raw: np.ndarray, groups: pd.Series) -> np.ndarray:
    raw = np.clip(np.asarray(raw, dtype=float), 1e-9, None)
    output = np.zeros_like(raw)
    for _, locations in pd.Series(np.arange(len(raw))).groupby(groups.to_numpy(), sort=False):
        idx = locations.to_numpy()
        total = raw[idx].sum()
        output[idx] = raw[idx] / total if total > 0 else 1.0 / len(idx)
    return output


def fit_calibrators(validation: pd.DataFrame) -> tuple[dict[str, IsotonicRegression], dict[str, Any]]:
    global_calibrator = IsotonicRegression(y_min=1e-6, y_max=1.0 - 1e-6, out_of_bounds="clip")
    global_calibrator.fit(validation["baseline_probability"], validation[TARGET_COLUMN])
    calibrators: dict[str, IsotonicRegression] = {"__global__": global_calibrator}
    metadata: dict[str, Any] = {"global_rows": int(len(validation)), "bands": {}}
    for band in ODDS_BANDS:
        group = validation[validation["odds_band"] == band]
        usable = len(group) >= MIN_BAND_ROWS and group[TARGET_COLUMN].nunique() == 2 and group["baseline_probability"].nunique() >= 2
        if usable:
            calibrator = IsotonicRegression(y_min=1e-6, y_max=1.0 - 1e-6, out_of_bounds="clip")
            calibrator.fit(group["baseline_probability"], group[TARGET_COLUMN])
            calibrators[band] = calibrator
            source = "band"
        else:
            source = "global_fallback"
        metadata["bands"][band] = {"rows": int(len(group)), "winners": int(group[TARGET_COLUMN].sum()), "source": source}
    return calibrators, metadata


def conditional_raw(frame: pd.DataFrame, calibrators: dict[str, IsotonicRegression]) -> np.ndarray:
    output = np.zeros(len(frame), dtype=float)
    probability = frame["baseline_probability"].to_numpy(dtype=float)
    for band in ODDS_BANDS:
        mask = (frame["odds_band"] == band).to_numpy()
        if not mask.any():
            continue
        calibrator = calibrators.get(band, calibrators["__global__"])
        output[mask] = calibrator.predict(probability[mask])
    return np.clip(output, 1e-9, None)


def rank_preserve(raw: np.ndarray, baseline: np.ndarray, groups: pd.Series) -> np.ndarray:
    """Project stage-one scores to the original within-race order without rank changes."""
    adjusted = np.asarray(raw, dtype=float).copy()
    for _, locations in pd.Series(np.arange(len(adjusted))).groupby(groups.to_numpy(), sort=False):
        idx = locations.to_numpy()
        order = np.argsort(-baseline[idx], kind="stable")
        ordered_idx = idx[order]
        values = adjusted[ordered_idx].copy()
        # A non-increasing projection that preserves the baseline winner and all ordering ties.
        for position in range(1, len(values)):
            values[position] = min(values[position], values[position - 1] * (1.0 - 1e-10))
        adjusted[ordered_idx] = values
    return adjusted


def calibration_points(frame: pd.DataFrame, column: str, bins: int = 10) -> list[dict[str, Any]]:
    data = frame[[column, TARGET_COLUMN]].dropna().copy()
    try:
        data["bin"] = pd.qcut(data[column], q=bins, duplicates="drop")
    except ValueError:
        return []
    points = []
    for position, (_, group) in enumerate(data.groupby("bin", observed=True), start=1):
        predicted = float(group[column].mean())
        observed = float(group[TARGET_COLUMN].mean())
        points.append({"bin": position, "n": int(len(group)), "predicted": predicted, "observed": observed, "gap": observed - predicted})
    return points


def ece(points: list[dict[str, Any]]) -> float | None:
    if not points:
        return None
    total = sum(point["n"] for point in points)
    return float(sum(point["n"] * abs(point["gap"]) for point in points) / total)


def method_metrics(frame: pd.DataFrame, method: str, column: str) -> dict[str, Any]:
    race = race_metrics(frame[["race_group", TARGET_COLUMN, column]].rename(columns={column: "probability"}), "probability")
    return {"method": method, "races": int(race["races"]), "race_brier": float(race["mean_race_brier_score"]), "top_pick_win_rate": float(race["top_pick_win_rate"]), "top3_contains_winner_rate": float(race["top3_contains_winner_rate"]), "calibration_ece": ece(calibration_points(frame, column))}


def odds_rows(frame: pd.DataFrame, method: str, column: str) -> list[dict[str, Any]]:
    output = []
    for band in ODDS_BANDS:
        group = frame[frame["odds_band"] == band]
        if group.empty:
            continue
        pred = group[column]
        target = group[TARGET_COLUMN]
        output.append({"method": method, "odds_band": band, "rows": int(len(group)), "winners": int(target.sum()), "mean_predicted_probability": float(pred.mean()), "observed_win_rate": float(target.mean()), "calibration_gap_observed_minus_predicted": float(target.mean() - pred.mean()), "row_brier": float(((target - pred) ** 2).mean())})
    return output


def per_race(frame: pd.DataFrame, column: str) -> pd.DataFrame:
    rows = []
    for race_group, group in frame.groupby("race_group", sort=False):
        target = group[TARGET_COLUMN].to_numpy(dtype=float)
        probability = group[column].to_numpy(dtype=float)
        rows.append({"race_group": race_group, "brier": float(np.square(target - probability).sum()), "top": float(target[int(np.argmax(probability))])})
    return pd.DataFrame(rows).set_index("race_group")


def paired_bootstrap(base: pd.DataFrame, candidate: pd.DataFrame, rng: np.random.Generator) -> dict[str, Any]:
    candidate = candidate.reindex(base.index)
    results: dict[str, Any] = {}
    for name, lower_better in (("brier", True), ("top", False)):
        delta = candidate[name].to_numpy() - base[name].to_numpy()
        indices = rng.integers(0, len(delta), size=(BOOTSTRAP_REPLICATES, len(delta)))
        sample = delta[indices].mean(axis=1)
        results[name] = {"candidate_minus_baseline": float(delta.mean()), "ci95": [float(np.quantile(sample, 0.025)), float(np.quantile(sample, 0.975))], "probability_candidate_better": float(np.mean(sample < 0.0 if lower_better else sample > 0.0))}
    return results


def report_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# N6：賠率條件式雙階段校準器與排名保護實驗",
        "",
        "> 第一階段在驗證期內按歷史賠率區間擬合等張映射；第二階段將結果於同場內重新正規化。`conditional_rank_protected` 額外投影至原基準排名順序，因此首選不會改變。此為候選實驗，並未修改生產模型、API、服務或 V10 資料。",
        "",
        "## 保留測試期整體比較",
        "",
        "| 方法 | Race Brier | 相對基準 | 首選命中率 | ECE |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    baseline = next(row for row in summary["test_metrics"] if row["method"] == "baseline")
    for row in summary["test_metrics"]:
        lines.append(f"| {row['method']} | {row['race_brier']:.6f} | {row['race_brier'] - baseline['race_brier']:+.6f} | {row['top_pick_win_rate']:.2%} | {row['calibration_ece']:.4f} |")
    lines.extend(["", "## 賠率區間校準差", "", "| 方法 | 區間 | 校準差（實際−預測） |", "| --- | --- | ---: |"])
    for row in summary["odds_band_summary"]:
        lines.append(f"| {row['method']} | {row['odds_band']} | {row['calibration_gap_observed_minus_predicted']:+.2%} |")
    lines.extend(["", "## 注意", "", "排名保護可確保首選命中率不因校準器改變，但也限制了校準器對相對排序的修正能力。候選升級應同時要求 Brier 改善的配對 bootstrap 信賴區間為負，且首選命中率不低於生產基準。", ""])
    return "\n".join(lines)


def main() -> int:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    bundle = load_model_bundle(MODEL_PATH, PREPROCESSOR_PATH)
    if int(bundle.metadata.get("input_dim", -1)) != 72:
        raise ValueError("Expected active 72D production model.")
    _, validation_source, test_source = chronological_split(load_training_frame())
    temperature = float(bundle.metadata.get("temperature", 1.0))
    validation = evaluate_base(validation_source, bundle, temperature)
    test = evaluate_base(test_source, bundle, temperature)
    calibrators, calibration_fit = fit_calibrators(validation)
    for data in (validation, test):
        raw = conditional_raw(data, calibrators)
        data["conditional_probability"] = renormalize(raw, data["race_group"])
        protected = rank_preserve(raw, data["baseline_probability"].to_numpy(dtype=float), data["race_group"])
        data["conditional_rank_protected_probability"] = renormalize(protected, data["race_group"])
    methods = {"baseline": "baseline_probability", "conditional": "conditional_probability", "conditional_rank_protected": "conditional_rank_protected_probability"}
    test_metrics = [method_metrics(test, name, column) for name, column in methods.items()]
    validation_metrics = [method_metrics(validation, name, column) for name, column in methods.items()]
    odds = [row for name, column in methods.items() for row in odds_rows(test, name, column)]
    per_race_metrics = {name: per_race(test, column) for name, column in methods.items()}
    rng = np.random.default_rng(RANDOM_SEED)
    bootstrap = {name: paired_bootstrap(per_race_metrics["baseline"], per_race_metrics[name], rng) for name in ("conditional", "conditional_rank_protected")}
    output = test[["race_date", "racecourse", "race_no", "horse_name", "race_group", TARGET_COLUMN, "win_odds", "odds_band", *methods.values()]].copy()
    output.to_csv(PREDICTIONS_PATH, index=False, encoding="utf-8-sig")
    pd.DataFrame(test_metrics).to_csv(METRICS_PATH, index=False, encoding="utf-8-sig")
    pd.DataFrame(odds).to_csv(ODDS_PATH, index=False, encoding="utf-8-sig")
    joblib.dump({"experiment_id": EXPERIMENT_ID, "fitted_on": "validation only", "production_release": bundle.metadata.get("production_release"), "calibration_fit": calibration_fit, "calibrators": calibrators, "rank_protection": "non-increasing projection in original baseline within-race order"}, LAYER_PATH)
    summary = {"engine": "N6 Neural Calculation Engine", "experiment_id": EXPERIMENT_ID, "generated_at_utc": datetime.now(UTC).isoformat(), "candidate_only_guarantee": "No production artifact, API, service, or V10 data change.", "production_release": bundle.metadata.get("production_release"), "fit_window": {"rows": int(len(validation)), "races": int(validation["race_group"].nunique())}, "test_window": {"rows": int(len(test)), "races": int(test["race_group"].nunique())}, "stage_one_fit": calibration_fit, "test_metrics": test_metrics, "validation_metrics_after_fit": validation_metrics, "odds_band_summary": odds, "paired_race_bootstrap": bootstrap, "artifacts": {"layer": str(LAYER_PATH), "predictions": str(PREDICTIONS_PATH), "metrics": str(METRICS_PATH), "odds": str(ODDS_PATH)}}
    SUMMARY_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    BOOTSTRAP_PATH.write_text(json.dumps(bootstrap, ensure_ascii=False, indent=2), encoding="utf-8")
    REPORT_PATH.write_text(report_markdown(summary), encoding="utf-8")
    print(json.dumps({"test_metrics": test_metrics, "paired_race_bootstrap": bootstrap, "stage_one_fit": calibration_fit}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
