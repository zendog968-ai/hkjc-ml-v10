#!/usr/bin/env python3
"""Validate candidate rank protection and flag Bayesian sampler quality issues."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("reports/candidates/bayesian_hierarchical_calibration_v1")
OOF = ROOT / "bayesian_hierarchical_cv_oof_predictions.csv"
FOLDS = ROOT / "bayesian_hierarchical_cv_folds.csv"
OUTPUT = ROOT / "bayesian_hierarchical_cv_validation.json"


def main() -> int:
    oof = pd.read_csv(OOF)
    folds = pd.read_csv(FOLDS)
    same_top = []
    for race_group, group in oof.groupby("race_group", sort=False):
        base_index = int(np.argmax(group["baseline_probability"].to_numpy(dtype=float)))
        candidate_index = int(np.argmax(group["bayesian_hierarchical_rank_protected_probability"].to_numpy(dtype=float)))
        same_top.append({"race_group": race_group, "same_top": base_index == candidate_index})
    comparison = pd.DataFrame(same_top)
    report = {
        "oof_rows": int(len(oof)),
        "oof_races": int(len(comparison)),
        "all_rank_protected": bool(comparison["same_top"].all()),
        "same_top_races": int(comparison["same_top"].sum()),
        "max_rhat": float(folds["mcmc_max_rhat"].max()),
        "min_ess_bulk": float(folds["mcmc_min_ess_bulk"].min()),
        "total_divergences": int(folds["mcmc_divergences"].sum()),
        "diagnostic_pass": bool((folds["mcmc_max_rhat"] <= 1.05).all() and (folds["mcmc_min_ess_bulk"] >= 200).all() and (folds["mcmc_divergences"] == 0).all()),
        "diagnostic_reason": "Deployment blocked when any fold has divergent transitions, ESS bulk below 200, or R-hat above 1.05.",
    }
    if not report["all_rank_protected"]:
        raise AssertionError("Bayesian rank-protected candidate changed at least one top pick.")
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
