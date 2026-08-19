#!/usr/bin/env python3
"""Race-clustered paired bootstrap validation for N6 calibration-layer candidates."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from n6.config import RANDOM_SEED

ROOT = Path("reports/candidates/probability_calibration_layers_v1")
INPUT = ROOT / "n6_test_predictions_calibrated.csv"
OUTPUT = ROOT / "paired_race_bootstrap_validation.json"
REPLICATES = 5000


def per_race_metrics(frame: pd.DataFrame, probability_column: str) -> pd.DataFrame:
    rows = []
    for race_group, group in frame.groupby("race_group", sort=False):
        probability = group[probability_column].to_numpy(dtype=float)
        target = group["target_win"].to_numpy(dtype=float)
        top_index = int(np.argmax(probability))
        rows.append({
            "race_group": race_group,
            "race_brier": float(np.square(target - probability).sum()),
            "top_pick_win": float(target[top_index]),
        })
    return pd.DataFrame(rows).set_index("race_group")


def bootstrap_delta(baseline: np.ndarray, candidate: np.ndarray, rng: np.random.Generator) -> dict[str, float | list[float]]:
    delta = candidate - baseline
    count = len(delta)
    indices = rng.integers(0, count, size=(REPLICATES, count))
    samples = delta[indices].mean(axis=1)
    return {
        "candidate_minus_baseline": float(delta.mean()),
        "ci95": [float(np.quantile(samples, 0.025)), float(np.quantile(samples, 0.975))],
        "probability_candidate_better_for_lower_metric": float(np.mean(samples < 0.0)),
    }


def bootstrap_top_pick(baseline: np.ndarray, candidate: np.ndarray, rng: np.random.Generator) -> dict[str, float | list[float]]:
    delta = candidate - baseline
    count = len(delta)
    indices = rng.integers(0, count, size=(REPLICATES, count))
    samples = delta[indices].mean(axis=1)
    return {
        "candidate_minus_baseline": float(delta.mean()),
        "ci95": [float(np.quantile(samples, 0.025)), float(np.quantile(samples, 0.975))],
        "probability_candidate_higher": float(np.mean(samples > 0.0)),
    }


def main() -> int:
    frame = pd.read_csv(INPUT)
    methods = {"baseline": "baseline_probability", "platt": "platt_probability", "isotonic": "isotonic_probability"}
    metrics = {name: per_race_metrics(frame, column) for name, column in methods.items()}
    baseline = metrics["baseline"]
    rng = np.random.default_rng(RANDOM_SEED)
    results = {"replicates": REPLICATES, "races": int(len(baseline)), "comparisons": {}}
    for name in ("platt", "isotonic"):
        candidate = metrics[name].reindex(baseline.index)
        results["comparisons"][name] = {
            "race_brier": bootstrap_delta(baseline["race_brier"].to_numpy(), candidate["race_brier"].to_numpy(), rng),
            "top_pick_win_rate": bootstrap_top_pick(baseline["top_pick_win"].to_numpy(), candidate["top_pick_win"].to_numpy(), rng),
        }
    OUTPUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
