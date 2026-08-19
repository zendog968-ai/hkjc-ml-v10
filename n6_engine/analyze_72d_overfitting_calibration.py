#!/usr/bin/env python3
"""Out-of-sample overfitting, residual, and odds-band calibration diagnostics for N6 72D."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import torch
from sklearn.linear_model import LogisticRegression

from n6.config import MODEL_PATH, PREPROCESSOR_PATH, RANDOM_SEED, REPORTS_DIR, TARGET_COLUMN
from n6.feature_engineering import load_training_frame, score_to_race_probabilities
from n6.model import load_model_bundle
from train import chronological_split, race_metrics

OUTPUT_JSON = REPORTS_DIR / "n6_72d_out_of_sample_diagnostics.json"
OUTPUT_MD = REPORTS_DIR / "N6_72D_OUT_OF_SAMPLE_DIAGNOSTICS.md"
OUTPUT_PARTITIONS = REPORTS_DIR / "n6_72d_partition_diagnostics.csv"
OUTPUT_ODDS = REPORTS_DIR / "n6_72d_odds_band_calibration.csv"
OUTPUT_CURVES = REPORTS_DIR / "n6_72d_calibration_curve_points.csv"
OUTPUT_RESIDUALS = REPORTS_DIR / "n6_72d_residual_summary.csv"
OUTPUT_CALIBRATION_SVG = REPORTS_DIR / "n6_72d_calibration_curves.svg"
OUTPUT_RESIDUAL_SVG = REPORTS_DIR / "n6_72d_residual_distributions.svg"

ODDS_BANDS = ["1–<5", "5–<10", "10–<20", "20+", "missing/invalid"]
ODDS_BAND_BOOTSTRAP_REPLICATES = 3000


def odds_band(values: pd.Series) -> pd.Series:
    odds = pd.to_numeric(values, errors="coerce")
    output = pd.Series("missing/invalid", index=values.index, dtype="object")
    output.loc[odds.ge(1.0) & odds.lt(5.0)] = "1–<5"
    output.loc[odds.ge(5.0) & odds.lt(10.0)] = "5–<10"
    output.loc[odds.ge(10.0) & odds.lt(20.0)] = "10–<20"
    output.loc[odds.ge(20.0)] = "20+"
    return output


def calibration_points(frame: pd.DataFrame, partition: str, group_name: str, group_value: str, bins: int = 10) -> list[dict[str, Any]]:
    data = frame[["probability", TARGET_COLUMN]].dropna().copy()
    if len(data) < max(20, bins * 2) or data["probability"].nunique() < 2:
        return []
    effective_bins = min(bins, max(2, data["probability"].nunique()))
    try:
        data["bin"] = pd.qcut(data["probability"], q=effective_bins, duplicates="drop")
    except ValueError:
        return []
    rows: list[dict[str, Any]] = []
    for index, (_, group) in enumerate(data.groupby("bin", observed=True), start=1):
        rows.append({
            "partition": partition,
            "group_name": group_name,
            "group_value": group_value,
            "bin": index,
            "n": int(len(group)),
            "mean_predicted_probability": float(group["probability"].mean()),
            "observed_win_rate": float(group[TARGET_COLUMN].mean()),
            "calibration_gap_observed_minus_predicted": float(group[TARGET_COLUMN].mean() - group["probability"].mean()),
        })
    return rows


def calibration_summary(points: list[dict[str, Any]]) -> tuple[float | None, float | None]:
    if not points:
        return None, None
    count = sum(row["n"] for row in points)
    ece = sum(row["n"] * abs(row["calibration_gap_observed_minus_predicted"]) for row in points) / count
    mce = max(abs(row["calibration_gap_observed_minus_predicted"]) for row in points)
    return float(ece), float(mce)


def calibration_slope_intercept(frame: pd.DataFrame) -> tuple[float | None, float | None]:
    probabilities = np.clip(frame["probability"].to_numpy(dtype=float), 1e-6, 1.0 - 1e-6)
    labels = frame[TARGET_COLUMN].to_numpy(dtype=int)
    if len(np.unique(labels)) < 2:
        return None, None
    logit = np.log(probabilities / (1.0 - probabilities)).reshape(-1, 1)
    model = LogisticRegression(C=1e6, solver="lbfgs", max_iter=2000, random_state=20260819)
    model.fit(logit, labels)
    return float(model.coef_[0, 0]), float(model.intercept_[0])


def evaluate_partition(name: str, frame: pd.DataFrame, model: torch.nn.Module, preprocessor: Any, contract: list[str], temperature: float) -> tuple[pd.DataFrame, dict[str, Any], list[dict[str, Any]]]:
    values = np.asarray(preprocessor.transform(frame[contract]), dtype=np.float32)
    with torch.no_grad():
        logits = model(torch.tensor(values, dtype=torch.float32)).cpu().numpy()
    output = frame[["race_date", "racecourse", "race_no", "horse_name", "race_group", TARGET_COLUMN, "win_odds"]].copy()
    output["partition"] = name
    output["raw_logit"] = logits
    output["probability"] = score_to_race_probabilities(logits / temperature, output["race_group"])
    output["residual_observed_minus_predicted"] = output[TARGET_COLUMN] - output["probability"]
    output["squared_error"] = output["residual_observed_minus_predicted"] ** 2
    output["odds_band"] = odds_band(output["win_odds"])
    metrics = race_metrics(output[["race_group", TARGET_COLUMN, "probability"]], "probability")
    points = calibration_points(output, name, "overall", "all", bins=10)
    ece, mce = calibration_summary(points)
    slope, intercept = calibration_slope_intercept(output)
    residual = output["residual_observed_minus_predicted"]
    summary = {
        "partition": name,
        "rows": int(len(output)),
        "races": int(output["race_group"].nunique()),
        "mean_race_brier_score": float(metrics["mean_race_brier_score"]),
        "top_pick_win_rate": float(metrics["top_pick_win_rate"]),
        "top3_contains_winner_rate": float(metrics["top3_contains_winner_rate"]),
        "row_brier_score": float(output["squared_error"].mean()),
        "mean_residual": float(residual.mean()),
        "residual_std": float(residual.std(ddof=1)),
        "residual_skew": float(residual.skew()),
        "residual_p01": float(residual.quantile(0.01)),
        "residual_p50": float(residual.quantile(0.50)),
        "residual_p99": float(residual.quantile(0.99)),
        "calibration_ece_quantile_bins": ece,
        "calibration_mce_quantile_bins": mce,
        "calibration_slope": slope,
        "calibration_intercept": intercept,
    }
    return output, summary, points


def odds_gap_bootstrap(frame: pd.DataFrame) -> dict[str, dict[str, float | list[float]]]:
    """Estimate race-clustered confidence intervals for odds-band calibration gaps."""
    race_groups = list(frame["race_group"].drop_duplicates())
    n_races = len(race_groups)
    band_count = len(ODDS_BANDS)
    counts = np.zeros((n_races, band_count), dtype=float)
    winners = np.zeros((n_races, band_count), dtype=float)
    predictions = np.zeros((n_races, band_count), dtype=float)
    band_index = {band: index for index, band in enumerate(ODDS_BANDS)}
    for race_index, race_group in enumerate(race_groups):
        group = frame[frame["race_group"] == race_group]
        for band, subset in group.groupby("odds_band", sort=False):
            index = band_index[str(band)]
            counts[race_index, index] = len(subset)
            winners[race_index, index] = subset[TARGET_COLUMN].sum()
            predictions[race_index, index] = subset["probability"].sum()
    rng = np.random.default_rng(RANDOM_SEED)
    draws = rng.integers(0, n_races, size=(ODDS_BAND_BOOTSTRAP_REPLICATES, n_races))
    resample_counts = counts[draws].sum(axis=1)
    resample_winners = winners[draws].sum(axis=1)
    resample_predictions = predictions[draws].sum(axis=1)
    gaps = np.divide(resample_winners - resample_predictions, resample_counts, out=np.full_like(resample_counts, np.nan), where=resample_counts > 0)
    output: dict[str, dict[str, float | list[float]]] = {}
    for band, index in band_index.items():
        values = gaps[:, index]
        values = values[np.isfinite(values)]
        if len(values) == 0:
            continue
        output[band] = {
            "gap_ci95": [float(np.quantile(values, 0.025)), float(np.quantile(values, 0.975))],
            "positive_gap_probability": float(np.mean(values > 0.0)),
        }
    return output


def odds_calibration(frame: pd.DataFrame) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    summaries: list[dict[str, Any]] = []
    all_points: list[dict[str, Any]] = []
    bootstrap = odds_gap_bootstrap(frame)
    for band in ODDS_BANDS:
        group = frame[frame["odds_band"] == band].copy()
        if group.empty:
            continue
        points = calibration_points(group, "test", "odds_band", band, bins=5)
        ece, mce = calibration_summary(points)
        summaries.append({
            "odds_band": band,
            "rows": int(len(group)),
            "winners": int(group[TARGET_COLUMN].sum()),
            "observed_win_rate": float(group[TARGET_COLUMN].mean()),
            "mean_predicted_probability": float(group["probability"].mean()),
            "calibration_gap_observed_minus_predicted": float(group[TARGET_COLUMN].mean() - group["probability"].mean()),
            "row_brier_score": float(group["squared_error"].mean()),
            "mean_residual": float(group["residual_observed_minus_predicted"].mean()),
            "calibration_ece_quantile_bins": ece,
            "calibration_mce_quantile_bins": mce,
            "calibration_gap_ci95": bootstrap.get(band, {}).get("gap_ci95"),
            "positive_calibration_gap_probability": bootstrap.get(band, {}).get("positive_gap_probability"),
        })
        all_points.extend(points)
    return summaries, all_points


def plot_calibration(overall_points: list[dict[str, Any]], odds_points: list[dict[str, Any]]) -> None:
    plt.style.use("seaborn-v0_8-whitegrid")
    fig, axes = plt.subplots(1, 2, figsize=(13, 5.3), constrained_layout=True)
    axes[0].plot([0, 1], [0, 1], linestyle="--", color="#777777", label="Ideal")
    by_partition: dict[str, list[dict[str, Any]]] = {}
    for point in overall_points:
        by_partition.setdefault(point["partition"], []).append(point)
    colors = {"train": "#1f77b4", "validation": "#ff7f0e", "test": "#2ca02c"}
    for partition, points in by_partition.items():
        points = sorted(points, key=lambda item: item["mean_predicted_probability"])
        axes[0].plot([item["mean_predicted_probability"] for item in points], [item["observed_win_rate"] for item in points], marker="o", label=partition.title(), color=colors.get(partition))
    axes[0].set_title("Overall calibration by chronological split")
    axes[0].set_xlabel("Mean predicted race-normalized probability")
    axes[0].set_ylabel("Observed winner rate")
    axes[0].legend()
    axes[0].set_xlim(left=0)
    axes[0].set_ylim(bottom=0)

    axes[1].plot([0, 1], [0, 1], linestyle="--", color="#777777", label="Ideal")
    palette = {"1–<5": "#1f77b4", "5–<10": "#ff7f0e", "10–<20": "#2ca02c", "20+": "#d62728", "missing/invalid": "#9467bd"}
    by_band: dict[str, list[dict[str, Any]]] = {}
    for point in odds_points:
        by_band.setdefault(point["group_value"], []).append(point)
    for band, points in by_band.items():
        points = sorted(points, key=lambda item: item["mean_predicted_probability"])
        axes[1].plot([item["mean_predicted_probability"] for item in points], [item["observed_win_rate"] for item in points], marker="o", label=band, color=palette.get(band))
    axes[1].set_title("Test calibration by historical odds band")
    axes[1].set_xlabel("Mean predicted race-normalized probability")
    axes[1].set_ylabel("Observed winner rate")
    axes[1].legend(title="Odds band", fontsize=8)
    axes[1].set_xlim(left=0)
    axes[1].set_ylim(bottom=0)
    fig.savefig(OUTPUT_CALIBRATION_SVG, format="svg", bbox_inches="tight")
    plt.close(fig)


def plot_residuals(partitions: dict[str, pd.DataFrame], test: pd.DataFrame) -> None:
    plt.style.use("seaborn-v0_8-whitegrid")
    fig, axes = plt.subplots(1, 2, figsize=(13, 5.3), constrained_layout=True)
    colors = {"train": "#1f77b4", "validation": "#ff7f0e", "test": "#2ca02c"}
    bins = np.linspace(-0.35, 1.0, 55)
    for name, frame in partitions.items():
        axes[0].hist(frame["residual_observed_minus_predicted"], bins=bins, density=True, alpha=0.36, label=name.title(), color=colors[name])
    axes[0].axvline(0, color="#333333", linestyle="--", linewidth=1)
    axes[0].set_title("Residual distributions by chronological split")
    axes[0].set_xlabel("Residual = observed winner label − predicted probability")
    axes[0].set_ylabel("Density")
    axes[0].legend()

    box_data = [test.loc[test["odds_band"] == band, "residual_observed_minus_predicted"].to_numpy() for band in ODDS_BANDS if (test["odds_band"] == band).any()]
    labels = [band for band in ODDS_BANDS if (test["odds_band"] == band).any()]
    axes[1].boxplot(box_data, tick_labels=labels, showfliers=False)
    axes[1].axhline(0, color="#333333", linestyle="--", linewidth=1)
    axes[1].set_title("Test residuals by historical odds band")
    axes[1].set_xlabel("Historical odds band")
    axes[1].set_ylabel("Residual")
    fig.savefig(OUTPUT_RESIDUAL_SVG, format="svg", bbox_inches="tight")
    plt.close(fig)


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# N6 72 維生產模型：外樣本過擬合與校準診斷",
        "",
        "> 此報告以目前生產版本 `n6-production-72d-market-implied-v1` 的既有時間序列資料切分，重新載入已部署模型進行唯讀評估。所有賠率分組均來自歷史 `win_odds`；校準結果描述過去樣本的機率品質，不構成未來賽果或回報保證。",
        "",
        "## 跨期間泛化與殘差",
        "",
        "| 分割 | 列數 | 場數 | Race Brier | 首選命中率 | Row Brier | ECE | 校準斜率 | 平均殘差 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in report["partition_diagnostics"]:
        lines.append(f"| {row['partition']} | {row['rows']:,} | {row['races']:,} | {row['mean_race_brier_score']:.6f} | {row['top_pick_win_rate']:.2%} | {row['row_brier_score']:.6f} | {row['calibration_ece_quantile_bins']:.4f} | {row['calibration_slope']:.3f} | {row['mean_residual']:+.5f} |")
    lines.extend([
        "",
        "## 測試期：按歷史賠率區間的校準",
        "",
        "| 區間 | 列數 | 頭馬 | 平均預測機率 | 實際頭馬率 | 校準差（實際−預測） | 95% CI | Row Brier | ECE |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ])
    for row in report["test_odds_band_calibration"]:
        ece = "—" if row["calibration_ece_quantile_bins"] is None else f"{row['calibration_ece_quantile_bins']:.4f}"
        ci = row.get("calibration_gap_ci95")
        ci_text = "—" if ci is None else f"[{ci[0]:+.2%}, {ci[1]:+.2%}]"
        lines.append(f"| {row['odds_band']} | {row['rows']:,} | {row['winners']:,} | {row['mean_predicted_probability']:.2%} | {row['observed_win_rate']:.2%} | {row['calibration_gap_observed_minus_predicted']:+.2%} | {ci_text} | {row['row_brier_score']:.6f} | {ece} |")
    lines.extend([
        "",
        "## 判讀界線",
        "",
        "若測試期 Brier 明顯高於驗證期，或校準斜率顯著低於 1，通常是過擬合或機率過度極端的警訊；反之，測試與驗證表現接近、且殘差平均值接近零，代表未見明顯的整體漂移。本報告的 ECE 採分位數分箱，應同時閱讀樣本數與各區間的校準差，尤其是頭馬極少的長賠率區間。",
        "",
        "![Calibration curves](n6_72d_calibration_curves.svg)",
        "",
        "![Residual distributions](n6_72d_residual_distributions.svg)",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    bundle = load_model_bundle(MODEL_PATH, PREPROCESSOR_PATH)
    if int(bundle.metadata.get("input_dim", -1)) != 72:
        raise ValueError("This diagnostic must only be run against the active 72D production model.")
    frame = load_training_frame()
    train, validation, test = chronological_split(frame)
    contract = list(bundle.metadata["feature_contract"])
    temperature = float(bundle.metadata.get("temperature", 1.0))
    results: dict[str, pd.DataFrame] = {}
    summaries: list[dict[str, Any]] = []
    all_points: list[dict[str, Any]] = []
    for name, partition in (("train", train), ("validation", validation), ("test", test)):
        evaluated, summary, points = evaluate_partition(name, partition, bundle.model, bundle.preprocessor, contract, temperature)
        results[name] = evaluated
        summaries.append(summary)
        all_points.extend(points)
    test_odds, odds_points = odds_calibration(results["test"])
    all_points.extend(odds_points)
    test_summary = next(row for row in summaries if row["partition"] == "test")
    validation_summary = next(row for row in summaries if row["partition"] == "validation")
    report = {
        "engine": "N6 Neural Calculation Engine",
        "production_release": bundle.metadata.get("production_release"),
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "scope": "Read-only diagnostics on the active 72-dimensional production artifact; no model, V10 database, or service configuration modified.",
        "method": {
            "probabilities": "Race-level softmax after production temperature scaling",
            "residual": "target_win minus race-normalized predicted probability",
            "calibration_curve": "Quantile-binned empirical win rate versus mean prediction",
            "odds_bands": ODDS_BANDS,
            "odds_band_uncertainty": f"{ODDS_BAND_BOOTSTRAP_REPLICATES} race-clustered bootstrap replicates for calibration-gap 95% intervals",
        },
        "partition_diagnostics": summaries,
        "test_odds_band_calibration": test_odds,
        "calibration_curve_points": all_points,
        "overfitting_gaps": {
            "test_minus_validation_race_brier": float(test_summary["mean_race_brier_score"] - validation_summary["mean_race_brier_score"]),
            "test_minus_train_race_brier": float(test_summary["mean_race_brier_score"] - next(row for row in summaries if row["partition"] == "train")["mean_race_brier_score"]),
            "test_minus_validation_ece": float((test_summary["calibration_ece_quantile_bins"] or 0.0) - (validation_summary["calibration_ece_quantile_bins"] or 0.0)),
        },
    }
    OUTPUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(summaries).to_csv(OUTPUT_PARTITIONS, index=False, encoding="utf-8-sig")
    pd.DataFrame(test_odds).to_csv(OUTPUT_ODDS, index=False, encoding="utf-8-sig")
    pd.DataFrame(all_points).to_csv(OUTPUT_CURVES, index=False, encoding="utf-8-sig")
    residual_rows = []
    for name, partition in results.items():
        for band, group in partition.groupby("odds_band", sort=False):
            residual_rows.append({"partition": name, "odds_band": band, "rows": int(len(group)), "mean_residual": float(group["residual_observed_minus_predicted"].mean()), "residual_std": float(group["residual_observed_minus_predicted"].std(ddof=1)), "p01": float(group["residual_observed_minus_predicted"].quantile(0.01)), "p99": float(group["residual_observed_minus_predicted"].quantile(0.99))})
    pd.DataFrame(residual_rows).to_csv(OUTPUT_RESIDUALS, index=False, encoding="utf-8-sig")
    plot_calibration([point for point in all_points if point["group_name"] == "overall"], odds_points)
    plot_residuals(results, results["test"])
    OUTPUT_MD.write_text(markdown_report(report), encoding="utf-8")
    print(json.dumps({"production_release": report["production_release"], "partition_diagnostics": summaries, "test_odds_band_calibration": test_odds, "overfitting_gaps": report["overfitting_gaps"], "artifacts": [str(OUTPUT_JSON), str(OUTPUT_MD), str(OUTPUT_CALIBRATION_SVG), str(OUTPUT_RESIDUAL_SVG)]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
