#!/usr/bin/env python3
"""Candidate-only within-race probability calibration for N6 72D.

Calibration models are fitted exclusively on the chronological validation window.
The transformed per-runner values are then re-normalized within each race before
all metrics are computed on the untouched test window. Production N6 artifacts,
API behavior, and V10 are never modified.
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import joblib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import torch
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression

from n6.config import MODEL_PATH, PREPROCESSOR_PATH, RANDOM_SEED, REPORTS_DIR, TARGET_COLUMN
from n6.feature_engineering import load_training_frame, score_to_race_probabilities
from n6.model import load_model_bundle
from train import chronological_split, race_metrics

EXPERIMENT_ID = "probability_calibration_layers_v1"
EXPERIMENT_MODEL_DIR = Path("models") / "candidates" / EXPERIMENT_ID
EXPERIMENT_REPORT_DIR = REPORTS_DIR / "candidates" / EXPERIMENT_ID
SUMMARY_PATH = EXPERIMENT_REPORT_DIR / "calibration_layer_summary.json"
METRICS_PATH = EXPERIMENT_REPORT_DIR / "calibration_layer_metrics.csv"
ODDS_PATH = EXPERIMENT_REPORT_DIR / "calibration_layer_odds_bands.csv"
PREDICTIONS_PATH = EXPERIMENT_REPORT_DIR / "n6_test_predictions_calibrated.csv"
LAYER_PATH = EXPERIMENT_MODEL_DIR / "calibration_layer_candidates.joblib"
PLOT_PATH = EXPERIMENT_REPORT_DIR / "calibration_layer_comparison.svg"
REPORT_PATH = EXPERIMENT_REPORT_DIR / "N6_PROBABILITY_CALIBRATION_LAYER_REPORT.md"
ODDS_BANDS = ["1–<5", "5–<10", "10–<20", "20+", "missing/invalid"]


def odds_band(values: pd.Series) -> pd.Series:
    odds = pd.to_numeric(values, errors="coerce")
    output = pd.Series("missing/invalid", index=values.index, dtype="object")
    output.loc[odds.ge(1.0) & odds.lt(5.0)] = "1–<5"
    output.loc[odds.ge(5.0) & odds.lt(10.0)] = "5–<10"
    output.loc[odds.ge(10.0) & odds.lt(20.0)] = "10–<20"
    output.loc[odds.ge(20.0)] = "20+"
    return output


def evaluate_logits(frame: pd.DataFrame, bundle: Any, temperature: float) -> pd.DataFrame:
    values = np.asarray(bundle.preprocessor.transform(frame[bundle.metadata["feature_contract"]]), dtype=np.float32)
    with torch.no_grad():
        logits = bundle.model(torch.tensor(values, dtype=torch.float32)).cpu().numpy()
    output = frame[["race_date", "racecourse", "race_no", "horse_name", "race_group", TARGET_COLUMN, "win_odds"]].copy()
    output["raw_logit"] = logits
    output["baseline_probability"] = score_to_race_probabilities(logits / temperature, output["race_group"])
    output["odds_band"] = odds_band(output["win_odds"])
    return output


def renormalize(values: np.ndarray, groups: pd.Series) -> np.ndarray:
    raw = np.clip(np.asarray(values, dtype=float), 1e-9, None)
    output = np.zeros_like(raw)
    for _, positions in pd.Series(np.arange(len(raw))).groupby(groups.to_numpy(), sort=False):
        indexes = positions.to_numpy()
        denominator = raw[indexes].sum()
        output[indexes] = raw[indexes] / denominator if denominator > 0 else 1.0 / len(indexes)
    return output


def fit_platt(validation: pd.DataFrame) -> LogisticRegression:
    probability = np.clip(validation["baseline_probability"].to_numpy(dtype=float), 1e-6, 1.0 - 1e-6)
    logit = np.log(probability / (1.0 - probability)).reshape(-1, 1)
    calibrator = LogisticRegression(C=1e6, solver="lbfgs", max_iter=2000, random_state=RANDOM_SEED)
    calibrator.fit(logit, validation[TARGET_COLUMN].to_numpy(dtype=int))
    return calibrator


def apply_platt(calibrator: LogisticRegression, frame: pd.DataFrame) -> np.ndarray:
    probability = np.clip(frame["baseline_probability"].to_numpy(dtype=float), 1e-6, 1.0 - 1e-6)
    logit = np.log(probability / (1.0 - probability)).reshape(-1, 1)
    raw = calibrator.predict_proba(logit)[:, 1]
    return renormalize(raw, frame["race_group"])


def fit_isotonic(validation: pd.DataFrame) -> IsotonicRegression:
    calibrator = IsotonicRegression(y_min=1e-6, y_max=1.0 - 1e-6, out_of_bounds="clip")
    calibrator.fit(validation["baseline_probability"].to_numpy(dtype=float), validation[TARGET_COLUMN].to_numpy(dtype=float))
    return calibrator


def apply_isotonic(calibrator: IsotonicRegression, frame: pd.DataFrame) -> np.ndarray:
    raw = calibrator.predict(frame["baseline_probability"].to_numpy(dtype=float))
    return renormalize(raw, frame["race_group"])


def calibration_points(frame: pd.DataFrame, probability_column: str, bins: int = 10) -> list[dict[str, Any]]:
    data = frame[[probability_column, TARGET_COLUMN]].dropna().copy()
    if len(data) < bins * 2:
        return []
    try:
        data["bin"] = pd.qcut(data[probability_column], q=bins, duplicates="drop")
    except ValueError:
        return []
    rows: list[dict[str, Any]] = []
    for position, (_, group) in enumerate(data.groupby("bin", observed=True), start=1):
        predicted = float(group[probability_column].mean())
        observed = float(group[TARGET_COLUMN].mean())
        rows.append({"bin": position, "n": int(len(group)), "mean_predicted_probability": predicted, "observed_win_rate": observed, "gap": observed - predicted})
    return rows


def ece(points: list[dict[str, Any]]) -> float | None:
    if not points:
        return None
    total = sum(row["n"] for row in points)
    return float(sum(row["n"] * abs(row["gap"]) for row in points) / total)


def evaluate_method(name: str, frame: pd.DataFrame, probability_column: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    metrics = race_metrics(frame[["race_group", TARGET_COLUMN, probability_column]].rename(columns={probability_column: "probability"}), "probability")
    points = calibration_points(frame, probability_column)
    result = {
        "method": name,
        "races": int(metrics["races"]),
        "race_brier": float(metrics["mean_race_brier_score"]),
        "top_pick_win_rate": float(metrics["top_pick_win_rate"]),
        "top3_contains_winner_rate": float(metrics["top3_contains_winner_rate"]),
        "calibration_ece": ece(points),
    }
    return result, points


def odds_summary(frame: pd.DataFrame, method: str, probability_column: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for band in ODDS_BANDS:
        group = frame[frame["odds_band"] == band]
        if group.empty:
            continue
        probability = group[probability_column]
        observed = group[TARGET_COLUMN]
        rows.append({
            "method": method,
            "odds_band": band,
            "rows": int(len(group)),
            "winners": int(observed.sum()),
            "mean_predicted_probability": float(probability.mean()),
            "observed_win_rate": float(observed.mean()),
            "calibration_gap_observed_minus_predicted": float(observed.mean() - probability.mean()),
            "row_brier": float(((observed - probability) ** 2).mean()),
        })
    return rows


def plot_calibration(method_points: dict[str, list[dict[str, Any]]]) -> None:
    plt.style.use("seaborn-v0_8-whitegrid")
    fig, axis = plt.subplots(figsize=(7.2, 5.4), constrained_layout=True)
    axis.plot([0, 1], [0, 1], linestyle="--", color="#555555", label="Ideal")
    colors = {"baseline": "#777777", "platt": "#1f77b4", "isotonic": "#2ca02c"}
    for method, points in method_points.items():
        ordered = sorted(points, key=lambda row: row["mean_predicted_probability"])
        axis.plot([row["mean_predicted_probability"] for row in ordered], [row["observed_win_rate"] for row in ordered], marker="o", color=colors[method], label=method.title())
    axis.set_title("Test calibration after within-race normalization")
    axis.set_xlabel("Mean predicted probability")
    axis.set_ylabel("Observed winner rate")
    axis.set_xlim(left=0)
    axis.set_ylim(bottom=0)
    axis.legend()
    fig.savefig(PLOT_PATH, format="svg", bbox_inches="tight")
    plt.close(fig)


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# N6 72D 場內機率校準層：等張迴歸與 Platt Scaling 對照",
        "",
        "> 校準器僅使用時間序列驗證期擬合，並在未回看測試期評估。每匹馬的校準後分數都會於同一場內重新正規化為機率和 1。此為候選實驗，未修改生產 N6 模型、API 或 V10 資料。",
        "",
        "## 測試期整體比較",
        "",
        "| 方法 | Race Brier | 相對基準 Brier | 首選命中率 | Top-3 含頭馬 | ECE |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    baseline = next(row for row in report["test_metrics"] if row["method"] == "baseline")
    for row in report["test_metrics"]:
        delta = row["race_brier"] - baseline["race_brier"]
        lines.append(f"| {row['method']} | {row['race_brier']:.6f} | {delta:+.6f} | {row['top_pick_win_rate']:.2%} | {row['top3_contains_winner_rate']:.2%} | {row['calibration_ece']:.4f} |")
    lines.extend([
        "",
        "## 測試期賠率區間校準差",
        "",
        "| 方法 | 區間 | 平均預測 | 實際頭馬率 | 校準差（實際−預測） |",
        "| --- | --- | ---: | ---: | ---: |",
    ])
    for row in report["odds_band_summary"]:
        lines.append(f"| {row['method']} | {row['odds_band']} | {row['mean_predicted_probability']:.2%} | {row['observed_win_rate']:.2%} | {row['calibration_gap_observed_minus_predicted']:+.2%} |")
    lines.extend([
        "",
        "## 設計限制",
        "",
        "全域一維等張或 Platt 校準器只能依基準機率變換，無法直接使用賠率區間；場內重新正規化也會改變其邊際校準。因此應以未回看測試期 Brier、ECE 和熱門／長賠率方向性偏差共同判斷，不能只憑驗證期擬合效果升級。",
        "",
        "![Test calibration comparison](calibration_layer_comparison.svg)",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    EXPERIMENT_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    EXPERIMENT_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    bundle = load_model_bundle(MODEL_PATH, PREPROCESSOR_PATH)
    if int(bundle.metadata.get("input_dim", -1)) != 72:
        raise ValueError("Expected active 72D production model.")
    frame = load_training_frame()
    _, validation_source, test_source = chronological_split(frame)
    temperature = float(bundle.metadata.get("temperature", 1.0))
    validation = evaluate_logits(validation_source, bundle, temperature)
    test = evaluate_logits(test_source, bundle, temperature)
    platt = fit_platt(validation)
    isotonic = fit_isotonic(validation)
    validation["platt_probability"] = apply_platt(platt, validation)
    validation["isotonic_probability"] = apply_isotonic(isotonic, validation)
    test["platt_probability"] = apply_platt(platt, test)
    test["isotonic_probability"] = apply_isotonic(isotonic, test)
    methods = {"baseline": "baseline_probability", "platt": "platt_probability", "isotonic": "isotonic_probability"}
    test_metrics: list[dict[str, Any]] = []
    method_points: dict[str, list[dict[str, Any]]] = {}
    odds_rows: list[dict[str, Any]] = []
    validation_metrics: list[dict[str, Any]] = []
    for method, column in methods.items():
        metrics, points = evaluate_method(method, test, column)
        test_metrics.append(metrics)
        method_points[method] = points
        odds_rows.extend(odds_summary(test, method, column))
        validation_metrics.append(evaluate_method(method, validation, column)[0])
    output_predictions = test[["race_date", "racecourse", "race_no", "horse_name", "race_group", TARGET_COLUMN, "win_odds", "odds_band", "baseline_probability", "platt_probability", "isotonic_probability"]].copy()
    output_predictions.to_csv(PREDICTIONS_PATH, index=False, encoding="utf-8-sig")
    pd.DataFrame(test_metrics).to_csv(METRICS_PATH, index=False, encoding="utf-8-sig")
    pd.DataFrame(odds_rows).to_csv(ODDS_PATH, index=False, encoding="utf-8-sig")
    joblib.dump({"experiment_id": EXPERIMENT_ID, "fitted_on": "chronological validation window only", "temperature": temperature, "platt": platt, "isotonic": isotonic, "production_release": bundle.metadata.get("production_release")}, LAYER_PATH)
    plot_calibration(method_points)
    report = {
        "engine": "N6 Neural Calculation Engine",
        "experiment_id": EXPERIMENT_ID,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "candidate_only_guarantee": "Production N6 artifacts, API behavior, V10 database, V10 runtime outputs, and service configuration were not modified.",
        "production_release": bundle.metadata.get("production_release"),
        "fit_window": {"rows": int(len(validation)), "races": int(validation["race_group"].nunique()), "from": str(validation["race_date"].min().date()), "to": str(validation["race_date"].max().date())},
        "test_window": {"rows": int(len(test)), "races": int(test["race_group"].nunique()), "from": str(test["race_date"].min().date()), "to": str(test["race_date"].max().date())},
        "test_metrics": test_metrics,
        "validation_metrics_after_fit": validation_metrics,
        "odds_band_summary": odds_rows,
        "artifacts": {"layers": str(LAYER_PATH), "test_predictions": str(PREDICTIONS_PATH), "metrics": str(METRICS_PATH), "odds_bands": str(ODDS_PATH), "plot": str(PLOT_PATH)},
    }
    SUMMARY_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    REPORT_PATH.write_text(markdown(report), encoding="utf-8")
    print(json.dumps({"fit_window": report["fit_window"], "test_window": report["test_window"], "test_metrics": test_metrics, "artifacts": report["artifacts"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
