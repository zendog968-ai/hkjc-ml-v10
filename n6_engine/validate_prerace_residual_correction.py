#!/usr/bin/env python3
"""Validate low-degree pre-race residual correction as a candidate only."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("reports/candidates/prerace_residual_correction_v1")
OOF_PATH = ROOT / "prerace_residual_correction_oof_predictions.csv"
FOLDS_PATH = ROOT / "prerace_residual_correction_folds.csv"
OUTPUT_PATH = ROOT / "prerace_residual_correction_validation.json"
COLUMN = "prerace_residual_rank_protected_probability"


def race_brier(frame: pd.DataFrame, column: str) -> float:
    values = []
    for _, group in frame.groupby("race_group", sort=False):
        probability = group[column].to_numpy(dtype=float)
        outcome = group["target_win"].to_numpy(dtype=float)
        values.append(float(np.sum((probability - outcome) ** 2)))
    return float(np.mean(values))


def main() -> int:
    oof = pd.read_csv(OOF_PATH)
    folds = pd.read_csv(FOLDS_PATH)
    race_groups = list(oof["race_group"].drop_duplicates())
    matches = []
    base_brier = []
    candidate_brier = []
    by_race = []
    for key in race_groups:
        group = oof[oof["race_group"] == key]
        base = group["baseline_probability"].to_numpy(dtype=float)
        candidate = group[COLUMN].to_numpy(dtype=float)
        target = group["target_win"].to_numpy(dtype=float)
        matches.append(int(np.argmax(base)) == int(np.argmax(candidate)))
        base_brier.append(float(np.sum((base - target) ** 2)))
        candidate_brier.append(float(np.sum((candidate - target) ** 2)))
        by_race.append(candidate_brier[-1] - base_brier[-1])
    differences = np.asarray(by_race, dtype=float)
    rng = np.random.default_rng(20260819)
    draws = rng.choice(differences, size=(5000, len(differences)), replace=True).mean(axis=1)
    report = {"oof_races": len(race_groups), "same_top_races": int(sum(matches)), "all_rank_protected": bool(all(matches)), "baseline_race_brier": float(np.mean(base_brier)), "candidate_race_brier": float(np.mean(candidate_brier)), "brier_delta": float(differences.mean()), "brier_delta_ci_95": [float(np.quantile(draws, 0.025)), float(np.quantile(draws, 0.975))], "bootstrap_candidate_better_share": float(np.mean(draws < 0)), "all_folds_rank_protected": bool(np.isclose(folds["baseline_top_pick_win_rate"], folds["candidate_top_pick_win_rate"]).all())}
    if not report["all_rank_protected"] or not report["all_folds_rank_protected"]:
        raise AssertionError("Rank protection changed at least one top pick.")
    OUTPUT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
