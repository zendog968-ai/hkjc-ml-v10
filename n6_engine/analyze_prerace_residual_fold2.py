#!/usr/bin/env python3
"""Diagnose why fold 2 of pre-race residual correction worsened race Brier."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from evaluate_prerace_residual_correction import enrich_scored, temporal_folds

ROOT = Path("reports/candidates/prerace_residual_correction_v1")
OOF_PATH = ROOT / "prerace_residual_correction_oof_predictions.csv"
COEF_PATH = ROOT / "prerace_residual_correction_coefficients.csv"
OUT_JSON = ROOT / "fold2_root_cause_summary.json"
OUT_GROUPS = ROOT / "fold2_root_cause_groups.csv"
OUT_RACES = ROOT / "fold2_largest_brier_regressions.csv"
OUT_REPORT = ROOT / "N6_PRERACE_RESIDUAL_FOLD2_ROOT_CAUSE.md"
CANDIDATE = "prerace_residual_rank_protected_probability"


def odds_band(value: float) -> str:
    if value < 5:
        return "1–<5"
    if value < 10:
        return "5–<10"
    if value < 20:
        return "10–<20"
    return "20+"


def draw_band(value: float) -> str:
    if value <= 1 / 3:
        return "low-third"
    if value <= 2 / 3:
        return "mid-third"
    return "high-third"


def race_brier(group: pd.DataFrame, column: str) -> float:
    return float(np.sum((group[column].to_numpy(float) - group["target_win"].to_numpy(float)) ** 2))


def distribution(frame: pd.DataFrame, column: str) -> pd.Series:
    return frame[column].value_counts(normalize=True, dropna=False)


def psi(train: pd.Series, test: pd.Series) -> float:
    all_idx = train.index.union(test.index)
    a = train.reindex(all_idx, fill_value=0.0).clip(1e-6)
    b = test.reindex(all_idx, fill_value=0.0).clip(1e-6)
    return float(((b - a) * np.log(b / a)).sum())


def main() -> int:
    oof = pd.read_csv(OOF_PATH)
    all_scored = enrich_scored()
    fold2 = temporal_folds(all_scored)[1]
    # Use the original race-group block, not inclusive dates, because a date can span folds.
    test = oof[oof["race_group"].isin(fold2["test_races"])].copy()
    train = all_scored[pd.to_datetime(all_scored["race_date"]) < fold2["calibration_end"]].copy()
    for frame in (train, test):
        frame["odds_band"] = frame["market_log_odds"].fillna(np.log(99)).map(lambda x: odds_band(float(np.exp(x))))
        frame["draw_band"] = frame["draw_pct"].map(draw_band)
    category_specs = [("going", "going"), ("racecourse", "racecourse"), ("odds_band", "odds_band"), ("draw_band", "draw_band")]
    group_rows: list[dict[str, object]] = []
    drifts: list[dict[str, object]] = []
    for source, label in category_specs:
        train_dist = distribution(train, source)
        test_dist = distribution(test, source)
        drifts.append({"feature": label, "psi": psi(train_dist, test_dist), "train_categories": int(len(train_dist)), "test_categories": int(len(test_dist))})
        for value, group in test.groupby(source, dropna=False):
            base = np.sum((group["baseline_probability"].to_numpy(float) - group["target_win"].to_numpy(float)) ** 2)
            candidate = np.sum((group[CANDIDATE].to_numpy(float) - group["target_win"].to_numpy(float)) ** 2)
            winners = group[group["target_win"] == 1]
            group_rows.append({"group_type": label, "group": str(value), "rows": int(len(group)), "races": int(group["race_group"].nunique()), "test_share": float(len(group) / len(test)), "train_share": float(train_dist.get(value, 0.0)), "share_shift_pp": float(100 * (len(group) / len(test) - train_dist.get(value, 0.0))), "baseline_row_brier_sum": float(base), "candidate_row_brier_sum": float(candidate), "brier_delta_sum": float(candidate - base), "brier_delta_per_row": float((candidate - base) / len(group)), "winner_base_probability": float(winners["baseline_probability"].mean()), "winner_candidate_probability": float(winners[CANDIDATE].mean()), "actual_win_rate": float(group["target_win"].mean()), "baseline_mean_probability": float(group["baseline_probability"].mean()), "candidate_mean_probability": float(group[CANDIDATE].mean()), "baseline_calibration_gap": float(group["target_win"].mean() - group["baseline_probability"].mean()), "candidate_calibration_gap": float(group["target_win"].mean() - group[CANDIDATE].mean())})
    race_rows: list[dict[str, object]] = []
    for key, group in test.groupby("race_group", sort=False):
        base = race_brier(group, "baseline_probability")
        candidate = race_brier(group, CANDIDATE)
        winner = group[group["target_win"] == 1].iloc[0]
        race_rows.append({"race_group": key, "race_date": str(group["race_date"].iloc[0]), "racecourse": group["racecourse"].iloc[0], "going": group["going"].iloc[0], "field_size": int(len(group)), "baseline_brier": base, "candidate_brier": candidate, "brier_delta": candidate - base, "winner_odds": float(np.exp(winner["market_log_odds"])), "winner_draw_pct": float(winner["draw_pct"]), "winner_baseline_probability": float(winner["baseline_probability"]), "winner_candidate_probability": float(winner[CANDIDATE]), "winner_probability_delta": float(winner[CANDIDATE] - winner["baseline_probability"])})
    races = pd.DataFrame(race_rows).sort_values("brier_delta", ascending=False)
    groups = pd.DataFrame(group_rows).sort_values("brier_delta_sum", ascending=False)
    coefficients = pd.read_csv(COEF_PATH)
    coef2 = coefficients[coefficients["fold_id"] == 2].sort_values("coefficient", key=lambda s: s.abs(), ascending=False)
    summary = {"fold": 2, "test_period": {"start": str(test["race_date"].min()), "end": str(test["race_date"].max())}, "test_rows": int(len(test)), "test_races": int(test["race_group"].nunique()), "baseline_race_brier": float(np.mean([row["baseline_brier"] for row in race_rows])), "candidate_race_brier": float(np.mean([row["candidate_brier"] for row in race_rows])), "brier_delta": float(races["brier_delta"].mean()), "largest_regression_races_share": float(races.head(20)["brier_delta"].sum() / races["brier_delta"].sum()), "distribution_drift": drifts, "fold2_top_abs_coefficients": coef2.head(10).to_dict(orient="records")}
    OUT_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    groups.to_csv(OUT_GROUPS, index=False, encoding="utf-8-sig")
    races.to_csv(OUT_RACES, index=False, encoding="utf-8-sig")
    lines = ["# N6：賽前殘差修正候選第二折根因診斷", "", f"> 第二折測試期：{summary['test_period']['start']} 至 {summary['test_period']['end']}。分析比較凍結 72 維 N6 基準與排名保護後的賽前殘差修正候選，未改動生產模型或 V10 資料。", "", "## 摘要", "", f"第二折共有 {summary['test_races']} 場、{summary['test_rows']} 匹，候選 Race Brier 差異為 {summary['brier_delta']:+.6f}。正值代表候選惡化。", "", "## 分布漂移", "", "| 特徵 | PSI |", "| --- | ---: |"]
    for row in drifts:
        lines.append(f"| {row['feature']} | {row['psi']:.4f} |")
    lines.extend(["", "## 最大絕對係數（第二折）", "", "| 特徵 | 係數 |", "| --- | ---: |"])
    for row in coef2.head(10).to_dict(orient="records"):
        lines.append(f"| {row['feature']} | {row['coefficient']:+.5f} |")
    OUT_REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
