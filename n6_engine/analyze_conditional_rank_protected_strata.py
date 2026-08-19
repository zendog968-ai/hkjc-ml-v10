#!/usr/bin/env python3
"""Racecourse and going diagnostics for the conditional_rank_protected candidate."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from n6.config import RANDOM_SEED, REPORTS_DIR, TARGET_COLUMN
from n6.feature_engineering import load_training_frame
from train import chronological_split, race_metrics

EXPERIMENT = "conditional_two_stage_calibration_v1"
CANDIDATE_PATH = REPORTS_DIR / "candidates" / EXPERIMENT / "n6_test_predictions_conditional_calibrated.csv"
OUTPUT_DIR = REPORTS_DIR / "candidates" / EXPERIMENT
SUMMARY_JSON = OUTPUT_DIR / "conditional_rank_protected_racecourse_going_summary.json"
SUMMARY_CSV = OUTPUT_DIR / "conditional_rank_protected_racecourse_going_metrics.csv"
RESIDUAL_CSV = OUTPUT_DIR / "conditional_rank_protected_racecourse_going_residuals.csv"
REPORT_MD = OUTPUT_DIR / "N6_CONDITIONAL_RANK_PROTECTED_RACECOURSE_GOING.md"
METHODS = {"baseline": "baseline_probability", "conditional_rank_protected": "conditional_rank_protected_probability"}
BOOTSTRAP_REPLICATES = 3000
MIN_ROWS_FOR_ECE = 80


def calibration_points(frame: pd.DataFrame, probability_column: str, bins: int = 5) -> list[dict[str, Any]]:
    data = frame[[probability_column, TARGET_COLUMN]].dropna().copy()
    if len(data) < MIN_ROWS_FOR_ECE or data[probability_column].nunique() < 2:
        return []
    try:
        data["bin"] = pd.qcut(data[probability_column], q=min(bins, data[probability_column].nunique()), duplicates="drop")
    except ValueError:
        return []
    points: list[dict[str, Any]] = []
    for _, group in data.groupby("bin", observed=True):
        predicted = float(group[probability_column].mean())
        observed = float(group[TARGET_COLUMN].mean())
        points.append({"n": int(len(group)), "gap": observed - predicted})
    return points


def ece(frame: pd.DataFrame, probability_column: str) -> float | None:
    points = calibration_points(frame, probability_column)
    if not points:
        return None
    total = sum(point["n"] for point in points)
    return float(sum(point["n"] * abs(point["gap"]) for point in points) / total)


def per_race_brier(frame: pd.DataFrame, probability_column: str) -> pd.Series:
    return frame.assign(squared_error=(frame[TARGET_COLUMN] - frame[probability_column]) ** 2).groupby("race_group", sort=False)["squared_error"].sum()


def bootstrap_brier_delta(frame: pd.DataFrame, candidate_column: str) -> dict[str, Any] | None:
    baseline = per_race_brier(frame, METHODS["baseline"])
    candidate = per_race_brier(frame, candidate_column).reindex(baseline.index)
    if len(baseline) < 20:
        return None
    delta = candidate.to_numpy() - baseline.to_numpy()
    rng = np.random.default_rng(RANDOM_SEED)
    samples = rng.integers(0, len(delta), size=(BOOTSTRAP_REPLICATES, len(delta)))
    values = delta[samples].mean(axis=1)
    return {"candidate_minus_baseline": float(delta.mean()), "ci95": [float(np.quantile(values, 0.025)), float(np.quantile(values, 0.975))], "probability_candidate_better": float(np.mean(values < 0.0))}


def metrics_for_group(frame: pd.DataFrame, stratum: str, value: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    residuals: list[dict[str, Any]] = []
    for method, column in METHODS.items():
        residual = frame[TARGET_COLUMN] - frame[column]
        race = race_metrics(frame[["race_group", TARGET_COLUMN, column]].rename(columns={column: "probability"}), "probability")
        bootstrap = None if method == "baseline" else bootstrap_brier_delta(frame, column)
        rows.append({
            "stratum": stratum,
            "group": value,
            "method": method,
            "rows": int(len(frame)),
            "races": int(frame["race_group"].nunique()),
            "winners": int(frame[TARGET_COLUMN].sum()),
            "race_brier": float(race["mean_race_brier_score"]),
            "row_brier": float((residual ** 2).mean()),
            "top_pick_win_rate": float(race["top_pick_win_rate"]),
            "ece": ece(frame, column),
            "mean_residual": float(residual.mean()),
            "residual_std": float(residual.std(ddof=1)),
            "residual_p01": float(residual.quantile(0.01)),
            "residual_p50": float(residual.quantile(0.50)),
            "residual_p99": float(residual.quantile(0.99)),
            "brier_delta_bootstrap": bootstrap,
        })
        residuals.append({
            "stratum": stratum,
            "group": value,
            "method": method,
            "rows": int(len(frame)),
            "mean_residual": float(residual.mean()),
            "residual_std": float(residual.std(ddof=1)),
            "p01": float(residual.quantile(0.01)),
            "median": float(residual.quantile(0.50)),
            "p99": float(residual.quantile(0.99)),
        })
    return rows, residuals


def report_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# conditional_rank_protected：馬場與場地狀況分層診斷",
        "",
        "> 本報告以保留測試期比較未校準 72 維基準與 `conditional_rank_protected` 候選。殘差定義為 `target_win − predicted_probability`。所有資料均為唯讀；候選沒有接入生產 API。小樣本分組的 ECE 不應單獨作決策依據。",
        "",
        "## 馬場",
        "",
        "| 馬場 | 方法 | 列數／場數 | Race Brier | ECE | 平均殘差 | 殘差中位數 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in summary["racecourse_metrics"]:
        ece_text = "—" if row["ece"] is None else f"{row['ece']:.4f}"
        lines.append(f"| {row['group']} | {row['method']} | {row['rows']:,}／{row['races']:,} | {row['race_brier']:.6f} | {ece_text} | {row['mean_residual']:+.4f} | {row['residual_p50']:+.4f} |")
    lines.extend(["", "## 場地狀況", "", "| Going | 方法 | 列數／場數 | Race Brier | ECE | 平均殘差 | 殘差中位數 |", "| --- | --- | ---: | ---: | ---: | ---: | ---: |"])
    for row in summary["going_metrics"]:
        ece_text = "—" if row["ece"] is None else f"{row['ece']:.4f}"
        lines.append(f"| {row['group']} | {row['method']} | {row['rows']:,}／{row['races']:,} | {row['race_brier']:.6f} | {ece_text} | {row['mean_residual']:+.4f} | {row['residual_p50']:+.4f} |")
    lines.extend(["", "## 判讀", "", "全體平均殘差在同場機率和為 1 的結構下常接近零；因此本報告以 ECE、race-level Brier、殘差分位數和候選相對基準的 Brier 差異共同判讀。候選若在某組的 bootstrap Brier CI 完全為負，代表在該組中對機率品質的改善具穩健證據；若分組場數有限，則應標記為探索性結果。", ""])
    return "\n".join(lines)


def main() -> int:
    candidate = pd.read_csv(CANDIDATE_PATH)
    _, _, source_test = chronological_split(load_training_frame())
    source = source_test[["race_group", "racecourse", "going"]].drop_duplicates("race_group")
    data = candidate.merge(source[["race_group", "going"]], on="race_group", how="left", validate="many_to_one")
    if data[["racecourse", "going"]].isna().any().any():
        raise ValueError("Could not match candidate predictions to racecourse/going source fields.")
    data["going"] = data["going"].fillna("未提供").astype(str)
    racecourse_metrics: list[dict[str, Any]] = []
    going_metrics: list[dict[str, Any]] = []
    residual_summary: list[dict[str, Any]] = []
    for value, group in data.groupby("racecourse", sort=True):
        metrics, residuals = metrics_for_group(group, "racecourse", str(value))
        racecourse_metrics.extend(metrics)
        residual_summary.extend(residuals)
    for value, group in data.groupby("going", sort=True):
        metrics, residuals = metrics_for_group(group, "going", str(value))
        going_metrics.extend(metrics)
        residual_summary.extend(residuals)
    summary = {
        "engine": "N6 Neural Calculation Engine",
        "candidate": "conditional_rank_protected",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "scope": "Read-only test-period stratified diagnostics. Production model/API/V10 were not modified.",
        "test_rows": int(len(data)),
        "test_races": int(data["race_group"].nunique()),
        "racecourse_metrics": racecourse_metrics,
        "going_metrics": going_metrics,
    }
    SUMMARY_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    metric_rows = racecourse_metrics + going_metrics
    flattened = []
    for row in metric_rows:
        row = dict(row)
        bootstrap = row.pop("brier_delta_bootstrap")
        if bootstrap:
            row["candidate_brier_delta"] = bootstrap["candidate_minus_baseline"]
            row["candidate_brier_delta_ci95_low"] = bootstrap["ci95"][0]
            row["candidate_brier_delta_ci95_high"] = bootstrap["ci95"][1]
        flattened.append(row)
    pd.DataFrame(flattened).to_csv(SUMMARY_CSV, index=False, encoding="utf-8-sig")
    pd.DataFrame(residual_summary).to_csv(RESIDUAL_CSV, index=False, encoding="utf-8-sig")
    REPORT_MD.write_text(report_markdown(summary), encoding="utf-8")
    print(json.dumps({"test_rows": summary["test_rows"], "test_races": summary["test_races"], "racecourse_metrics": racecourse_metrics, "going_metrics": going_metrics}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
