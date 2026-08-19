#!/usr/bin/env python3
"""Read-only odds calibration analysis for the ST × 1400m × 14-runner exposure group."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from n6.feature_engineering import load_training_frame

OUT = Path("reports/candidates/prerace_residual_no_market_v1")
SUMMARY = OUT / "st1400_field14_market_calibration_summary.json"
DETAIL = OUT / "st1400_field14_market_calibration_groups.csv"
REPORT = OUT / "N6_ST1400_FIELD14_MARKET_CALIBRATION.md"
RNG = np.random.default_rng(20260819)


def odds_band(odds: float) -> str:
    if odds < 5:
        return "1–<5"
    if odds < 10:
        return "5–<10"
    if odds < 20:
        return "10–<20"
    return "20+"


def add_market_probs(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out["market_prob_raw"] = 1.0 / out["win_odds"].astype(float)
    denom = out.groupby("race_group")["market_prob_raw"].transform("sum")
    out["market_prob_norm"] = out["market_prob_raw"] / denom
    out["field_size"] = out.groupby("race_group")["race_group"].transform("size")
    out["odds_band"] = out["win_odds"].astype(float).map(odds_band)
    out["date"] = pd.to_datetime(out["race_date"])
    return out


def metrics(frame: pd.DataFrame) -> dict[str, float | int]:
    actual = frame["target_win"].astype(float)
    raw = frame["market_prob_raw"].astype(float)
    norm = frame["market_prob_norm"].astype(float)
    return {"races": int(frame["race_group"].nunique()), "runners": int(len(frame)), "winners": int(actual.sum()), "actual_win_rate": float(actual.mean()), "mean_raw_implied_probability": float(raw.mean()), "raw_calibration_gap": float(actual.mean() - raw.mean()), "mean_normalized_implied_probability": float(norm.mean()), "normalized_calibration_gap": float(actual.mean() - norm.mean()), "normalized_brier": float(np.mean((norm - actual) ** 2))}


def race_bootstrap_gap(frame: pd.DataFrame, probability_column: str, iterations: int = 3000) -> tuple[float, float]:
    """Vectorized race-cluster bootstrap of runner-weighted calibration gap."""
    race = frame.groupby("race_group", sort=False).agg(
        winners=("target_win", "sum"),
        expected=(probability_column, "sum"),
        runners=("target_win", "size"),
    )
    winners = race["winners"].to_numpy(float)
    expected = race["expected"].to_numpy(float)
    runners = race["runners"].to_numpy(float)
    draws = RNG.integers(0, len(race), size=(iterations, len(race)))
    sampled_runners = runners[draws].sum(axis=1)
    values = winners[draws].sum(axis=1) / sampled_runners - expected[draws].sum(axis=1) / sampled_runners
    return float(np.quantile(values, 0.025)), float(np.quantile(values, 0.975))


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    frame = add_market_probs(load_training_frame())
    target_mask = (frame["racecourse"] == "ST") & (frame["distance_m"] == 1400) & (frame["field_size"] == 14)
    groups = {
        "target_ST_1400_14": target_mask,
        "ST_1400_other_field_sizes": (frame["racecourse"] == "ST") & (frame["distance_m"] == 1400) & (frame["field_size"] != 14),
        "ST_other_distances_14": (frame["racecourse"] == "ST") & (frame["distance_m"] != 1400) & (frame["field_size"] == 14),
        "all_other_races": ~target_mask,
    }
    periods = {
        "through_2025_05_20": frame["date"] < pd.Timestamp("2025-05-21"),
        "fold1_2025_05_21_to_2025_11_14": (frame["date"] >= pd.Timestamp("2025-05-21")) & (frame["date"] < pd.Timestamp("2025-11-15")),
        "fold2_2025_11_15_to_2026_03_17": (frame["date"] >= pd.Timestamp("2025-11-15")) & (frame["date"] < pd.Timestamp("2026-03-18")),
        "fold3_2026_03_18_to_2026_07_14": (frame["date"] >= pd.Timestamp("2026-03-18")) & (frame["date"] < pd.Timestamp("2026-07-15")),
        "all_history": pd.Series(True, index=frame.index),
    }
    records: list[dict[str, object]] = []
    for period_name, period_mask in periods.items():
        for group_name, group_mask in groups.items():
            subset = frame[period_mask & group_mask]
            if subset.empty:
                continue
            record: dict[str, object] = {"period": period_name, "group": group_name, **metrics(subset)}
            if group_name == "target_ST_1400_14" and subset["race_group"].nunique() >= 5:
                raw_low, raw_high = race_bootstrap_gap(subset, "market_prob_raw")
                norm_low, norm_high = race_bootstrap_gap(subset, "market_prob_norm")
                record["raw_gap_ci_low"] = raw_low
                record["raw_gap_ci_high"] = raw_high
                record["normalized_gap_ci_low"] = norm_low
                record["normalized_gap_ci_high"] = norm_high
            records.append(record)
        for comparison_name, comparison_mask in groups.items():
            comparison = frame[period_mask & comparison_mask]
            for band, subset in comparison.groupby("odds_band", sort=False):
                record = {"period": period_name, "group": f"{comparison_name}_odds_{band}", "comparison_group": comparison_name, "odds_band": band, **metrics(subset)}
                records.append(record)
    detail = pd.DataFrame(records)
    detail.to_csv(DETAIL, index=False, encoding="utf-8-sig")
    target_all = detail[(detail["period"] == "all_history") & (detail["group"] == "target_ST_1400_14")].iloc[0].to_dict()
    target_fold2 = detail[(detail["period"] == "fold2_2025_11_15_to_2026_03_17") & (detail["group"] == "target_ST_1400_14")].iloc[0].to_dict()
    summary = {"engine": "N6 Neural Calculation Engine", "candidate_only_guarantee": "Read-only historical analysis; no production model, API, service, or V10 source data was modified.", "definition": "racecourse=ST, distance_m=1400, exactly 14 runners with valid win odds", "raw_implied_probability": "1 / win_odds", "normalized_implied_probability": "raw implied probability normalized within each race; primary comparison against one winner per race", "all_history_target": target_all, "fold2_target": target_fold2, "comparison_rows": detail[(detail["period"] == "all_history") & detail["group"].isin(list(groups))].to_dict(orient="records")}
    SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    report = ["# N6：沙田 × 1400m × 14 匹場的歷史賠率校準", "", "> 本分析採用既有 V10 唯讀歷史資料。原始賠率隱含機率含市場 overround；主要比較使用同場正規化後隱含機率，以符合每場恰有一匹頭馬的結構。", "", "## 目標組合與對照組", "", "| 組合 | 場次 | 馬匹 | 實際頭馬率 | 正規化隱含機率 | 校準差（實際−隱含） | 正規化 Brier |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for _, row in detail[(detail["period"] == "all_history") & detail["group"].isin(list(groups))].iterrows():
        report.append(f"| {row['group']} | {int(row['races'])} | {int(row['runners'])} | {row['actual_win_rate']:.2%} | {row['mean_normalized_implied_probability']:.2%} | {row['normalized_calibration_gap']:+.2%} | {row['normalized_brier']:.6f} |")
    report.extend(["", "## 目標組合的時間窗", "", "> 目標組合每場固定為 14 匹，因此同場正規化後的全體平均機率必然等於 1/14，與全體實際頭馬率相同。故時間窗偏離以未正規化的原始賠率隱含機率為主要量度；正規化機率只在賠率分層內比較。", "", "| 時間窗 | 場次 | 原始賠率校準差 | 95% 叢集 bootstrap CI |", "| --- | ---: | ---: | ---: |"])
    for _, row in detail[detail["group"] == "target_ST_1400_14"].iterrows():
        ci = "—" if pd.isna(row.get("raw_gap_ci_low", np.nan)) else f"[{row['raw_gap_ci_low']:+.2%}, {row['raw_gap_ci_high']:+.2%}]"
        report.append(f"| {row['period']} | {int(row['races'])} | {row['raw_calibration_gap']:+.2%} | {ci} |")
    REPORT.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
