#!/usr/bin/env python3
"""Fold-2 ablation: quantify the marginal impact of market-log-odds residual term."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression

from evaluate_prerace_residual_correction import (
    C_VALUE, CANDIDATE_COLUMN, enrich_scored, feature_frame, temporal_folds,
)
from evaluate_bayesian_hierarchical_calibration import normalise, race_metrics, rank_protect

ROOT = Path("reports/candidates/prerace_residual_correction_v1")
OUT = ROOT / "fold2_market_odds_ablation.json"


def corrected_probability(train, test, include_market: bool):
    x_train, stats = feature_frame(train)
    x_test, _ = feature_frame(test, stats)
    x_test = x_test.reindex(columns=x_train.columns, fill_value=0.0)
    if not include_market:
        x_train = x_train.drop(columns=["market_log_odds_z"])
        x_test = x_test.drop(columns=["market_log_odds_z"])
    model = LogisticRegression(C=C_VALUE, penalty="l2", solver="lbfgs", max_iter=1000, random_state=20260819)
    model.fit(x_train, train["target_win"].to_numpy(dtype=int))
    raw = model.predict_proba(x_test)[:, 1]
    protected = rank_protect(raw, test["baseline_probability"].to_numpy(float), test["race_group"])
    return normalise(protected, test["race_group"]), {name: float(value) for name, value in zip(x_train.columns, model.coef_[0], strict=True)}


def main() -> int:
    scored = enrich_scored()
    fold2 = temporal_folds(scored)[1]
    train = scored[np.asarray(scored["race_date"].to_numpy(dtype="datetime64[ns]") < np.datetime64(fold2["calibration_end"]))].copy()
    test = scored[scored["race_group"].isin(fold2["test_races"])].copy()
    full, full_coefficients = corrected_probability(train, test, True)
    no_market, no_market_coefficients = corrected_probability(train, test, False)
    test["full"] = full
    test["no_market"] = no_market
    base = race_metrics(test, "baseline_probability")
    full_metrics = race_metrics(test, "full")
    no_market_metrics = race_metrics(test, "no_market")
    result = {"test_races": int(test["race_group"].nunique()), "baseline": base, "full": full_metrics, "without_market_log_odds": no_market_metrics, "full_minus_baseline_brier": full_metrics["race_brier"] - base["race_brier"], "without_market_minus_baseline_brier": no_market_metrics["race_brier"] - base["race_brier"], "full_market_log_odds_coefficient": full_coefficients.get("market_log_odds_z"), "full_coefficients": full_coefficients, "without_market_coefficients": no_market_coefficients}
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
