"""Research-only dynamic Kelly policy for chronological S1/S2 walk-forward tests."""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Any

import numpy as np

from analyze_s1s2_odds_drop_sensitivity import RacePrediction, rescore


@dataclass(frozen=True)
class KellyPolicy:
    status: str
    selected_weight: float
    training_races: int
    brier_score: float | None
    equal_brier_score: float | None
    calibration_scale: float
    sample_scale: float
    historical_drawdown_scale: float
    effective_kelly_scale: float
    max_single_fraction: float
    max_race_fraction: float
    drawdown_trigger: float
    drawdown_multiplier: float
    min_ev: float
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _outcomes(race: RacePrediction, results: dict[tuple[str, int], int]) -> list[int] | None:
    values = [results.get((race.race_key, int(row["horse_no"]))) for row in race.rows]
    return values if values and all(value is not None for value in values) else None


def training_diagnostics(train: list[RacePrediction], results: dict[tuple[str, int], int], weight: float) -> dict[str, float] | None:
    briers: list[float] = []
    equal_briers: list[float] = []
    fixed_equity = [100.0]
    for race in sorted(train, key=lambda item: (item.generated_at_utc, item.race_key)):
        outcomes = _outcomes(race, results)
        if outcomes is None:
            continue
        rescored = rescore(race.rows, race.baseline_weight, weight)
        count = len(rescored)
        briers.append(float(sum((float(row["counterfactual_probability"]) - (1.0 if outcome == 1 else 0.0)) ** 2 for row, outcome in zip(rescored, outcomes, strict=True))))
        equal_briers.append(float(sum(((1.0 / count) - (1.0 if outcome == 1 else 0.0)) ** 2 for outcome in outcomes)))
        top = rescored[0]
        top_outcome = results.get((race.race_key, int(top["horse_no"])))
        if isinstance(top.get("win_odds"), (int, float)) and float(top["win_odds"]) > 1.0 and top_outcome is not None:
            fixed_equity.append(fixed_equity[-1] + ((float(top["win_odds"]) - 1.0) if top_outcome == 1 else -1.0))
    if not briers:
        return None
    peak = fixed_equity[0]
    max_drawdown = 0.0
    for value in fixed_equity:
        peak = max(peak, value)
        max_drawdown = min(max_drawdown, (value - peak) / peak if peak else 0.0)
    return {"brier_score": float(np.mean(briers)), "equal_brier_score": float(np.mean(equal_briers)), "fixed_unit_drawdown_fraction": abs(max_drawdown), "diagnostic_races": len(briers)}


def derive_policy(train: list[RacePrediction], results: dict[tuple[str, int], int], selected_weight: float, min_train_races: int, max_single_fraction: float, max_race_fraction: float, drawdown_trigger: float, drawdown_multiplier: float, min_ev: float) -> KellyPolicy:
    diag = training_diagnostics(train, results, selected_weight)
    if diag is None or len(train) < min_train_races:
        return KellyPolicy("disabled_insufficient_training", selected_weight, len(train), None, None, 0.0, 0.0, 0.0, 0.0, max_single_fraction, max_race_fraction, drawdown_trigger, drawdown_multiplier, min_ev, "training data does not meet the chronological minimum")
    equal = diag["equal_brier_score"]
    model = diag["brier_score"]
    calibration_scale = max(0.0, min(1.0, (equal - model) / max(equal * 0.10, 1e-12)))
    sample_scale = min(1.0, math.sqrt(diag["diagnostic_races"] / max(min_train_races * 2, 1)))
    historical_drawdown_scale = max(0.0, min(1.0, 1.0 - diag["fixed_unit_drawdown_fraction"] / max(drawdown_trigger, 1e-9)))
    effective = calibration_scale * sample_scale * historical_drawdown_scale
    status = "enabled" if effective > 0 else "disabled_no_pretraining_edge_or_drawdown_capacity"
    reason = "calibration, evidence volume and prior fixed-unit drawdown jointly determine the scale"
    return KellyPolicy(status, selected_weight, len(train), model, equal, calibration_scale, sample_scale, historical_drawdown_scale, effective, max_single_fraction, max_race_fraction, drawdown_trigger, drawdown_multiplier, min_ev, reason)


def fractional_kelly(probability: float, decimal_odds: float, policy: KellyPolicy, drawdown_fraction: float) -> tuple[float, float, str]:
    if policy.effective_kelly_scale <= 0 or decimal_odds <= 1.0:
        return 0.0, 0.0, "policy_disabled_or_invalid_odds"
    ev = probability * decimal_odds - 1.0
    if ev <= policy.min_ev:
        return 0.0, ev, "ev_not_above_threshold"
    full = ev / (decimal_odds - 1.0)
    guard = policy.drawdown_multiplier if drawdown_fraction <= -policy.drawdown_trigger else 1.0
    fraction = min(policy.max_single_fraction, max(0.0, full * policy.effective_kelly_scale * guard))
    return fraction, ev, "drawdown_guard" if guard < 1.0 else "normal"


def simulate_test_quarter(test: list[RacePrediction], results: dict[tuple[str, int], int], policy: KellyPolicy, initial_bankroll: float) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    bankroll = initial_bankroll
    peak = initial_bankroll
    rows: list[dict[str, Any]] = []
    for race in sorted(test, key=lambda item: (item.generated_at_utc, item.race_key)):
        rescored = rescore(race.rows, race.baseline_weight, policy.selected_weight)
        top = rescored[0]
        before = bankroll
        drawdown = (bankroll - peak) / peak if peak else 0.0
        if isinstance(top.get("win_odds"), (int, float)):
            fraction, ev, reason = fractional_kelly(float(top["counterfactual_probability"]), float(top["win_odds"]), policy, drawdown)
        else:
            fraction, ev, reason = 0.0, None, "missing_prerace_odds"
        fraction = min(fraction, policy.max_race_fraction)
        stake = before * fraction
        result = results.get((race.race_key, int(top["horse_no"])))
        pnl = None
        if result is not None and stake > 0:
            pnl = stake * ((float(top["win_odds"]) - 1.0) if result == 1 else -1.0)
            bankroll += pnl
            peak = max(peak, bankroll)
        rows.append({"race_key": race.race_key, "generated_at_utc": race.generated_at_utc, "horse_no": top["horse_no"], "horse_name": top.get("horse_name"), "finish_pos": result, "win_odds": top.get("win_odds"), "probability": top["counterfactual_probability"], "ev": ev, "kelly_fraction": fraction, "stake": stake, "pnl": pnl, "bankroll_before": before, "bankroll_after": bankroll, "drawdown_before": drawdown, "reason": reason})
    settled = [row for row in rows if row["pnl"] is not None]
    peak_after = initial_bankroll
    max_dd = 0.0
    for row in rows:
        if row["pnl"] is not None:
            peak_after = max(peak_after, row["bankroll_after"])
            max_dd = min(max_dd, (row["bankroll_after"] - peak_after) / peak_after if peak_after else 0.0)
    total_stake = sum(row["stake"] for row in settled)
    return rows, {"settled_bets": len(settled), "total_stake": total_stake, "net_pnl": bankroll - initial_bankroll, "roi_on_staked_capital": (bankroll - initial_bankroll) / total_stake if total_stake else None, "ending_bankroll": bankroll, "max_drawdown_fraction": max_dd}
