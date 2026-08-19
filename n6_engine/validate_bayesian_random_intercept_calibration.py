#!/usr/bin/env python3
"""Validate the random-intercept Bayesian candidate before any promotion."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("reports/candidates/bayesian_random_intercept_calibration_v1")
OOF = ROOT / "bayesian_random_intercept_cv_oof_predictions.csv"
FOLDS = ROOT / "bayesian_random_intercept_cv_folds.csv"
OUTPUT = ROOT / "bayesian_random_intercept_cv_validation.json"


def main() -> int:
    oof = pd.read_csv(OOF)
    folds = pd.read_csv(FOLDS)
    comparisons = []
    for race_group, group in oof.groupby("race_group", sort=False):
        baseline = int(np.argmax(group["baseline_probability"].to_numpy(dtype=float)))
        candidate = int(np.argmax(group["bayesian_random_intercept_rank_protected_probability"].to_numpy(dtype=float)))
        comparisons.append(baseline == candidate)
    report = {
        "oof_rows": int(len(oof)),
        "oof_races": int(len(comparisons)),
        "same_top_races": int(sum(comparisons)),
        "all_rank_protected": bool(all(comparisons)),
        "max_rhat": float(folds["mcmc_max_rhat"].max()),
        "min_ess_bulk": float(folds["mcmc_min_ess_bulk"].min()),
        "total_divergences": int(folds["mcmc_divergences"].sum()),
        "target_accept": 0.99,
        "diagnostic_pass": bool((folds["mcmc_max_rhat"] <= 1.05).all() and (folds["mcmc_min_ess_bulk"] >= 200).all() and (folds["mcmc_divergences"] == 0).all()),
        "diagnostic_reason": "Deployment requires R-hat <= 1.05, bulk ESS >= 200, zero divergences, and exact rank protection for every OOF race.",
    }
    if not report["all_rank_protected"]:
        raise AssertionError("At least one OOF race changed the N6 baseline top pick.")
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
