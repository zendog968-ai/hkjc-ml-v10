#!/usr/bin/env python3
"""Leakage-safe S1/S2 Plackett–Luce + fractional-Kelly walk-forward backtest.

The program intentionally accepts a *ledger* rather than historical final odds.
A row is eligible only if the model was generated no later than a genuine T-15 or
T-5 snapshot and if settlement is separately present.  It reports N/A instead
of manufacturing ROI when the archive has no eligible rows.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

REQUIRED = {
    "race_key", "race_date", "captured_at_utc", "model_generated_at_utc", "snapshot_label",
    "snapshot_status", "market", "predicted_probability", "captured_payout_multiple",
    "settled_gross_return_per_unit", "settled",
}


@dataclass(frozen=True)
class Config:
    kelly_scale: float
    max_single_fraction: float
    max_race_fraction: float
    min_ev: float


def parse_scales(raw: str) -> list[float]:
    values = sorted(set(float(value.strip()) for value in raw.split(",") if value.strip()))
    if not values or any(value <= 0 or value > 1 for value in values):
        raise ValueError("--kelly-scales 必須為 (0, 1] 的逗號分隔小數，例如 0.125,0.25,0.5。")
    return values


def read_ledger(pattern: str) -> pd.DataFrame:
    paths = sorted(glob.glob(pattern, recursive=True))
    if not paths:
        return pd.DataFrame()
    frames = [pd.read_csv(path) for path in paths]
    ledger = pd.concat(frames, ignore_index=True)
    missing = REQUIRED.difference(ledger.columns)
    if missing:
        raise ValueError(f"ledger 缺少必要欄位：{sorted(missing)}")
    return ledger


def hk_season_quarter(date_series: pd.Series) -> pd.Series:
    dates = pd.to_datetime(date_series, errors="coerce", utc=True)
    if dates.isna().any():
        raise ValueError("race_date 必須可轉換為日期。")
    # HK season quarter: Sep-Nov=Q1; Dec-Feb=Q2; Mar-May=Q3; Jun-Aug=Q4.
    season_start = np.where(dates.dt.month >= 9, dates.dt.year, dates.dt.year - 1)
    quarter = np.select(
        [dates.dt.month.isin([9, 10, 11]), dates.dt.month.isin([12, 1, 2]), dates.dt.month.isin([3, 4, 5])],
        [1, 2, 3],
        default=4,
    )
    return pd.Series([f"{int(year)}/{str(int(year) + 1)[-2:]}-Q{int(q)}" for year, q in zip(season_start, quarter)], index=date_series.index)


def validate_and_filter(ledger: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if ledger.empty:
        return ledger.copy(), pd.DataFrame(columns=["reason", "count"])
    data = ledger.copy()
    for col in ("captured_at_utc", "model_generated_at_utc"):
        data[col] = pd.to_datetime(data[col], errors="coerce", utc=True)
    for col in ("predicted_probability", "captured_payout_multiple", "settled_gross_return_per_unit"):
        data[col] = pd.to_numeric(data[col], errors="coerce")
    data["settled"] = data["settled"].astype(str).str.lower().isin(["1", "true", "yes"])
    reasons: list[pd.Series] = []
    reasons.append(pd.Series(np.where(data["snapshot_label"].isin(["T_MINUS_15", "T_MINUS_5"]), "", "invalid_snapshot_label"), index=data.index))
    reasons.append(pd.Series(np.where(data["snapshot_status"].eq("complete"), "", "snapshot_not_complete"), index=data.index))
    reasons.append(pd.Series(np.where(data["captured_at_utc"].notna() & data["model_generated_at_utc"].notna() & (data["model_generated_at_utc"] <= data["captured_at_utc"]), "", "model_after_snapshot"), index=data.index))
    reasons.append(pd.Series(np.where(data["settled"], "", "unsettled"), index=data.index))
    reasons.append(pd.Series(np.where((data["predicted_probability"] > 0) & (data["predicted_probability"] < 1), "", "invalid_probability"), index=data.index))
    reasons.append(pd.Series(np.where(data["captured_payout_multiple"] > 1, "", "invalid_captured_payout"), index=data.index))
    reasons.append(pd.Series(np.where(data["settled_gross_return_per_unit"] >= 0, "", "invalid_settlement"), index=data.index))
    combined = pd.concat(reasons, axis=1).apply(lambda row: next((value for value in row if value), ""), axis=1)
    data["exclusion_reason"] = combined
    excluded = data.loc[data["exclusion_reason"].ne("")]
    included = data.loc[data["exclusion_reason"].eq("")].copy()
    included["quarter"] = hk_season_quarter(included["race_date"])
    included = included.sort_values(["captured_at_utc", "race_key", "market"]).reset_index(drop=True)
    exclusion_summary = excluded["exclusion_reason"].value_counts().rename_axis("reason").reset_index(name="count")
    return included, exclusion_summary


def apply_config(rows: pd.DataFrame, config: Config, initial_bankroll: float) -> tuple[pd.DataFrame, dict]:
    if rows.empty:
        return rows.copy(), {"settled_candidates": 0, "eligible_bets": 0, "roi": None, "max_drawdown": None}
    data = rows.copy()
    b = data["captured_payout_multiple"] - 1.0
    data["ev_at_capture"] = data["predicted_probability"] * data["captured_payout_multiple"] - 1.0
    data["full_kelly"] = np.maximum(0.0, (data["predicted_probability"] * b - (1.0 - data["predicted_probability"])) / b)
    data["raw_fraction"] = np.minimum(config.max_single_fraction, data["full_kelly"] * config.kelly_scale)
    data["selected"] = data["ev_at_capture"] >= config.min_ev
    data.loc[~data["selected"], "raw_fraction"] = 0.0
    # Maintain a hard portfolio cap for selections exposed to the same race.
    race_sum = data.groupby("race_key")["raw_fraction"].transform("sum")
    data["allocated_fraction"] = np.where(race_sum > config.max_race_fraction, data["raw_fraction"] * config.max_race_fraction / race_sum, data["raw_fraction"])
    bankroll = float(initial_bankroll)
    data["stake"] = 0.0
    data["net_return"] = 0.0
    data["bankroll_after"] = np.nan
    # Candidates in one race share a single pre-race bankroll.  Allocate all
    # same-race stakes first, settle their combined return, then move to the
    # next timestamped race; do not incorrectly compound inside one race.
    for _, group in data.groupby("race_key", sort=False):
        race_index = group.index
        race_stakes = bankroll * group["allocated_fraction"]
        race_nets = race_stakes * (group["settled_gross_return_per_unit"] - 1.0)
        bankroll += float(race_nets.sum())
        data.loc[race_index, "stake"] = race_stakes
        data.loc[race_index, "net_return"] = race_nets
        data.loc[race_index, "bankroll_after"] = bankroll
    data["equity_peak"] = data["bankroll_after"].cummax().clip(lower=initial_bankroll)
    data["drawdown"] = data["bankroll_after"] / data["equity_peak"] - 1.0
    selected = data.loc[data["selected"] & (data["stake"] > 0)]
    total_stake = float(selected["stake"].sum())
    total_net = float(selected["net_return"].sum())
    summary = {
        "settled_candidates": int(len(data)),
        "eligible_bets": int(len(selected)),
        "coverage_rate": float(len(data) / len(rows)) if len(rows) else None,
        "total_stake": total_stake,
        "total_net_return": total_net,
        "roi": (total_net / total_stake) if total_stake > 0 else None,
        "final_bankroll": float(data["bankroll_after"].iloc[-1]),
        "max_drawdown": float(data["drawdown"].min()),
        "max_single_stake_fraction": float(data["allocated_fraction"].max()),
        "mean_ev_at_capture": float(selected["ev_at_capture"].mean()) if not selected.empty else None,
    }
    return data, summary


def choose_walkforward_scale(rows: pd.DataFrame, scales: Iterable[float], config_base: Config, initial_bankroll: float, min_train_bets: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    quarters = list(dict.fromkeys(rows["quarter"].tolist()))
    tests: list[dict] = []
    details: list[pd.DataFrame] = []
    for idx in range(1, len(quarters)):
        train = rows.loc[rows["quarter"].isin(quarters[:idx])]
        candidates = []
        for scale in scales:
            _, metrics = apply_config(train, Config(scale, config_base.max_single_fraction, config_base.max_race_fraction, config_base.min_ev), initial_bankroll)
            if metrics["eligible_bets"] >= min_train_bets and metrics["roi"] is not None:
                # Penalize excessive historical drawdown during training selection.
                score = math.log(max(metrics["final_bankroll"] / initial_bankroll, 1e-12)) + 0.5 * float(metrics["max_drawdown"])
                candidates.append((score, scale, metrics))
        if not candidates:
            tests.append({"test_quarter": quarters[idx], "status": "N/A_insufficient_prior_training", "chosen_scale": None})
            continue
        _, chosen_scale, train_metrics = max(candidates, key=lambda item: item[0])
        test = rows.loc[rows["quarter"].eq(quarters[idx])]
        test_details, test_metrics = apply_config(test, Config(chosen_scale, config_base.max_single_fraction, config_base.max_race_fraction, config_base.min_ev), initial_bankroll)
        test_details["walkforward_chosen_scale"] = chosen_scale
        details.append(test_details)
        tests.append({"test_quarter": quarters[idx], "status": "ok", "chosen_scale": chosen_scale, "train_eligible_bets": train_metrics["eligible_bets"], **{f"test_{key}": value for key, value in test_metrics.items()}})
    return pd.DataFrame(tests), pd.concat(details, ignore_index=True) if details else pd.DataFrame()


def main() -> int:
    parser = argparse.ArgumentParser(description="S1/S2 Plackett–Luce + fractional-Kelly walk-forward backtest.")
    parser.add_argument("--ledger-glob", required=True, help="歷史賽前候選／結算帳本 CSV glob；只能包含真實 T-15/T-5 快照。")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--initial-bankroll", type=float, default=100.0, help="研究用標準化起始資本，非投注指示。")
    parser.add_argument("--kelly-scales", default="0.125,0.25,0.5")
    parser.add_argument("--max-single-fraction", type=float, default=0.01)
    parser.add_argument("--max-race-fraction", type=float, default=0.02)
    parser.add_argument("--min-ev", type=float, default=0.0)
    parser.add_argument("--min-train-bets", type=int, default=30)
    args = parser.parse_args()
    if args.initial_bankroll <= 0 or not (0 < args.max_single_fraction <= args.max_race_fraction <= 1):
        raise ValueError("資本與 fraction 參數不合法。")
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    raw = read_ledger(args.ledger_glob)
    included, exclusions = validate_and_filter(raw)
    config_base = Config(0.0, args.max_single_fraction, args.max_race_fraction, args.min_ev)
    summary: dict = {
        "schema_version": "v10.2_s1s2_pl_kelly_walkforward_v1",
        "input_rows": int(len(raw)),
        "eligible_settled_rows": int(len(included)),
        "strict_status": "N/A_no_eligible_prerace_settled_rows" if included.empty else "exploratory" if len(included) < 30 else "ready_for_walkforward_review",
        "rules": {"snapshot_labels": ["T_MINUS_15", "T_MINUS_5"], "requires_model_at_or_before_snapshot": True, "requires_settlement": True, "quarter_mode": "hk_season"},
    }
    exclusions.to_csv(out / "exclusion_summary.csv", index=False)
    if included.empty:
        (out / "walkforward_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    included.to_csv(out / "eligible_ledger.csv", index=False)
    scale_rows = []
    for scale in parse_scales(args.kelly_scales):
        details, metrics = apply_config(included, Config(scale, args.max_single_fraction, args.max_race_fraction, args.min_ev), args.initial_bankroll)
        metrics["kelly_scale"] = scale
        scale_rows.append(metrics)
        details.to_csv(out / f"kelly_scale_{scale:g}_details.csv", index=False)
    scale_summary = pd.DataFrame(scale_rows)
    scale_summary.to_csv(out / "kelly_parameter_surface.csv", index=False)
    walk_summary, walk_details = choose_walkforward_scale(included, parse_scales(args.kelly_scales), config_base, args.initial_bankroll, args.min_train_bets)
    walk_summary.to_csv(out / "walkforward_quarter_summary.csv", index=False)
    if not walk_details.empty:
        walk_details.to_csv(out / "walkforward_details.csv", index=False)
    summary["quarters"] = sorted(included["quarter"].unique().tolist())
    summary["walkforward_quarters"] = int(len(walk_summary))
    summary["minimum_train_bets"] = args.min_train_bets
    (out / "walkforward_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
