#!/usr/bin/env python3
"""Paired race-level bootstrap validation for the implied-probability-only variant."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from n6.config import RANDOM_SEED, REPORTS_DIR

BASELINE = REPORTS_DIR / "n6_test_predictions.csv"
CANDIDATE = REPORTS_DIR / "candidates" / "market_feature_variants_v1" / "implied_probability_only" / "n6_test_predictions_candidate.csv"
OUTPUT = REPORTS_DIR / "candidates" / "market_feature_variants_v1" / "implied_probability_only" / "paired_race_bootstrap_validation.json"
BOOTSTRAP_REPLICATES = 5000


def percentile_ci(values: np.ndarray) -> tuple[float, float]:
    return float(np.quantile(values, 0.025)), float(np.quantile(values, 0.975))


def main() -> int:
    keys = ["race_date", "racecourse", "race_no", "horse_name", "race_group", "target_win"]
    baseline = pd.read_csv(BASELINE, encoding="utf-8-sig")
    candidate = pd.read_csv(CANDIDATE, encoding="utf-8-sig")
    merged = baseline[keys + ["neural_score", "neural_rank"]].merge(
        candidate[keys + ["neural_score", "neural_rank"]],
        on=keys,
        suffixes=("_baseline", "_candidate"),
        validate="one_to_one",
    )
    per_race = []
    for race_group, race in merged.groupby("race_group", sort=False):
        labels = race["target_win"].to_numpy(dtype=float)
        if labels.sum() != 1:
            continue
        baseline_probability = race["neural_score_baseline"].to_numpy(dtype=float)
        candidate_probability = race["neural_score_candidate"].to_numpy(dtype=float)
        per_race.append({
            "race_group": race_group,
            "brier_delta_candidate_minus_baseline": float(np.sum((candidate_probability - labels) ** 2) - np.sum((baseline_probability - labels) ** 2)),
            "top_pick_delta_candidate_minus_baseline": int(race.loc[race["neural_rank_candidate"].idxmin(), "target_win"]) - int(race.loc[race["neural_rank_baseline"].idxmin(), "target_win"]),
        })
    race_frame = pd.DataFrame(per_race)
    rng = np.random.default_rng(RANDOM_SEED)
    indexes = rng.integers(0, len(race_frame), size=(BOOTSTRAP_REPLICATES, len(race_frame)))
    brier_bootstrap = race_frame["brier_delta_candidate_minus_baseline"].to_numpy()[indexes].mean(axis=1)
    top_pick_bootstrap = race_frame["top_pick_delta_candidate_minus_baseline"].to_numpy()[indexes].mean(axis=1)
    brier_ci = percentile_ci(brier_bootstrap)
    top_pick_ci = percentile_ci(top_pick_bootstrap)
    result = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "method": {"comparison": "paired per-race candidate-minus-baseline differences", "bootstrap_replicates": BOOTSTRAP_REPLICATES, "resampling_unit": "race_group", "interval": "two-sided 95 percent percentile interval"},
        "sample": {"races": int(len(race_frame)), "rows": int(len(merged)), "aligned_one_to_one": True},
        "brier": {"mean_delta_candidate_minus_baseline": float(race_frame["brier_delta_candidate_minus_baseline"].mean()), "ci95": list(brier_ci), "candidate_improvement_probability": float(np.mean(brier_bootstrap < 0.0))},
        "top_pick_win_rate": {"mean_delta_candidate_minus_baseline": float(race_frame["top_pick_delta_candidate_minus_baseline"].mean()), "ci95": list(top_pick_ci), "candidate_improvement_probability": float(np.mean(top_pick_bootstrap > 0.0))},
        "interpretation": "Negative Brier delta supports the candidate. Positive top-pick delta supports the candidate. This is a stability check on one held-out period, not proof of future performance.",
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
