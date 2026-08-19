#!/usr/bin/env python3
"""Validate the soft-going hyperprior plus condition-ELO candidate."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("reports/candidates/bayesian_soft_going_hyperprior_covariate_v1")
OOF_PATH = ROOT / "soft_going_hyperprior_covariate_oof_predictions.csv"
FOLDS_PATH = ROOT / "soft_going_hyperprior_covariate_folds.csv"
OUTPUT_PATH = ROOT / "soft_going_hyperprior_covariate_validation.json"
COLUMN = "soft_going_hyperprior_covariate_rank_protected_probability"


def main() -> int:
    oof = pd.read_csv(OOF_PATH)
    folds = pd.read_csv(FOLDS_PATH)
    matches = []
    for _, group in oof.groupby("race_group", sort=False):
        base = int(np.argmax(group["baseline_probability"].to_numpy(dtype=float)))
        candidate = int(np.argmax(group[COLUMN].to_numpy(dtype=float)))
        matches.append(base == candidate)
    report = {
        "oof_rows": int(len(oof)), "oof_races": int(len(matches)), "same_top_races": int(sum(matches)),
        "all_rank_protected": bool(all(matches)), "max_rhat": float(folds["mcmc_max_rhat"].max()),
        "min_ess_bulk": float(folds["mcmc_min_ess_bulk"].min()), "total_divergences": int(folds["mcmc_divergences"].sum()),
        "target_accept": 0.995, "tune": 1200, "soft_sigma_prior": "HalfNormal(0.08)",
        "covariate": "horse_condition_elo_pre, standardized using each fold calibration period only",
        "diagnostic_pass": bool((folds["mcmc_max_rhat"] <= 1.05).all() and (folds["mcmc_min_ess_bulk"] >= 200).all() and (folds["mcmc_divergences"] == 0).all()),
        "diagnostic_reason": "Deployment requires R-hat <= 1.05, bulk ESS >= 200, zero divergences, and exact baseline top-pick preservation for every OOF race.",
    }
    if not report["all_rank_protected"]:
        raise AssertionError("Rank protection changed at least one N6 baseline top pick.")
    OUTPUT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
