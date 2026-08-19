#!/usr/bin/env python3
"""Analyse fold-2 no-market residual correction Brier regressions for interference patterns."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from n6.feature_engineering import load_training_frame

ROOT = Path("reports/candidates/prerace_residual_no_market_v1")
OOF = ROOT / "prerace_no_market_oof_predictions.csv"
PRED = "prerace_no_market_rank_protected_probability"
OUT_SUMMARY = ROOT / "fold2_interference_summary.json"
OUT_RACES = ROOT / "fold2_interference_races.csv"
OUT_GROUPS = ROOT / "fold2_interference_groups.csv"
OUT_REPORT = ROOT / "N6_PRERACE_NO_MARKET_FOLD2_INTERFERENCE.md"


def odds_band(value: float) -> str:
    if value < 5:
        return "1–<5"
    if value < 10:
        return "5–<10"
    if value < 20:
        return "10–<20"
    return "20+"


def brier(group: pd.DataFrame, column: str) -> float:
    return float(np.sum((group[column].to_numpy(float) - group["target_win"].to_numpy(float)) ** 2))


def main() -> int:
    frame = pd.read_csv(OOF)
    test = frame[frame["fold_id"] == 2].copy()
    raw = load_training_frame()
    track_lookup = raw[["race_group", "horse_name", "course_config", "distance_m", "draw"]].drop_duplicates(["race_group", "horse_name"])
    test = test.merge(track_lookup, on=["race_group", "horse_name"], how="left", validate="one_to_one")
    if test[["course_config", "distance_m", "draw"]].isna().any().any():
        raise ValueError("Missing course configuration, distance, or draw after read-only feature merge.")
    rows: list[dict[str, object]] = []
    for race_group, group in test.groupby("race_group", sort=False):
        winner = group[group["target_win"] == 1].iloc[0]
        win_odds = float(np.exp(winner["market_log_odds"]))
        rows.append({"race_group": race_group, "race_date": str(group["race_date"].iloc[0]), "racecourse": group["racecourse"].iloc[0], "course_config": group["course_config"].iloc[0], "going": group["going"].iloc[0], "distance_m": int(group["distance_m"].iloc[0]), "field_size": int(len(group)), "baseline_brier": brier(group, "baseline_probability"), "candidate_brier": brier(group, PRED), "brier_delta": brier(group, PRED) - brier(group, "baseline_probability"), "winner_odds": win_odds, "winner_odds_band": odds_band(win_odds), "winner_draw": int(winner["draw"]), "winner_draw_pct": float(winner["draw_pct"]), "winner_baseline_probability": float(winner["baseline_probability"]), "winner_candidate_probability": float(winner[PRED]), "winner_probability_delta": float(winner[PRED] - winner["baseline_probability"]), "winner_baseline_rank": int((-group["baseline_probability"]).rank(method="min").loc[winner.name]), "winner_candidate_rank": int((-group[PRED]).rank(method="min").loc[winner.name])})
    races = pd.DataFrame(rows).sort_values("brier_delta", ascending=False)
    races["delta_sign"] = np.where(races["brier_delta"] > 0, "regression", "improvement")
    groups: list[dict[str, object]] = []
    for column in ["going", "racecourse", "course_config", "distance_m", "winner_odds_band", "field_size"]:
        for value, group in races.groupby(column, dropna=False):
            groups.append({"group_type": column, "group": str(value), "races": int(len(group)), "mean_brier_delta": float(group["brier_delta"].mean()), "total_brier_delta": float(group["brier_delta"].sum()), "regression_share": float((group["brier_delta"] > 0).mean()), "winner_odds_median": float(group["winner_odds"].median()), "winner_probability_delta_mean": float(group["winner_probability_delta"].mean())})
    group_frame = pd.DataFrame(groups).sort_values(["group_type", "total_brier_delta"], ascending=[True, False])
    positive = races[races["brier_delta"] > 0]
    top20 = races.head(20)
    summary = {"fold": 2, "races": int(len(races)), "baseline_race_brier": float(races["baseline_brier"].mean()), "candidate_race_brier": float(races["candidate_brier"].mean()), "total_brier_delta": float(races["brier_delta"].sum()), "mean_brier_delta": float(races["brier_delta"].mean()), "regression_races": int((races["brier_delta"] > 0).sum()), "regression_race_share": float((races["brier_delta"] > 0).mean()), "top10_delta_share": float(top20.head(10)["brier_delta"].sum() / positive["brier_delta"].sum()), "top20_delta_share": float(top20["brier_delta"].sum() / positive["brier_delta"].sum()), "positive_winner_odds_median": float(positive["winner_odds"].median()), "nonpositive_winner_odds_median": float(races[races["brier_delta"] <= 0]["winner_odds"].median()), "cold_upset_regression_races": int(((positive["winner_odds"] >= 20)).sum()), "cold_upset_regression_delta_share": float(positive.loc[positive["winner_odds"] >= 20, "brier_delta"].sum() / positive["brier_delta"].sum()), "top20": top20.head(20).to_dict(orient="records")}
    OUT_SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    races.to_csv(OUT_RACES, index=False, encoding="utf-8-sig")
    group_frame.to_csv(OUT_GROUPS, index=False, encoding="utf-8-sig")
    report = ["# N6：無市場賠率候選第二折干擾因子診斷", "", f"> 第二折包含 {summary['races']} 場；候選平均 Race Brier 差異為 {summary['mean_brier_delta']:+.6f}。本分析只使用既有賽前 Going、賽道配置、距離、排位與封盤賠率欄位。", "", "## 集中程度", "", f"Brier 惡化賽事有 {summary['regression_races']} 場（{summary['regression_race_share']:.1%}）。最差十場佔正向 Brier 增量 {summary['top10_delta_share']:.1%}；最差二十場佔 {summary['top20_delta_share']:.1%}。", "", "## 最大退化場次", "", "| 日期 | 場地 | 配置 | Going | 路程 | 勝馬賠率 | 勝馬基準機率 | 候選機率 | Brier 差異 |", "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for row in top20.head(12).to_dict(orient="records"):
        report.append(f"| {row['race_date'][:10]} | {row['racecourse']} | {row['course_config']} | {row['going']} | {row['distance_m']} | {row['winner_odds']:.1f} | {row['winner_baseline_probability']:.2%} | {row['winner_candidate_probability']:.2%} | {row['brier_delta']:+.4f} |")
    OUT_REPORT.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
