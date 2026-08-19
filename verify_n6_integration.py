#!/usr/bin/env python3
"""Contract tests for V10's in-memory N6 enrichment path."""

from __future__ import annotations

import copy
import json

import n6_integration


def fixture_prediction() -> dict:
    return {
        "race": {"race_date": "2026-08-18", "racecourse": "ST", "race_no": 1, "distance_m": 1200, "surface": "草地"},
        "predictions": [
            {"horse_no": 1, "horse_name": "甲馬", "rank": 1, "predicted_win_probability": 0.50, "ev_per_unit": 0.20, "kelly_quarter_fraction_capped": 0.03},
            {"horse_no": 2, "horse_name": "乙馬", "rank": 2, "predicted_win_probability": 0.30, "ev_per_unit": 0.05, "kelly_quarter_fraction_capped": 0.01},
            {"horse_no": 3, "horse_name": "丙馬", "rank": 3, "predicted_win_probability": 0.20, "ev_per_unit": -0.10, "kelly_quarter_fraction_capped": 0.00},
        ],
    }


def main() -> int:
    original = fixture_prediction()
    before = copy.deepcopy(original)
    original_fetch = n6_integration.fetch_n6_scores
    try:
        n6_integration.fetch_n6_scores = lambda *_: ("historical_pre_race_features", [
            {"horse_name": "甲馬", "neural_rank": 1, "neural_score": 55.0, "neural_win_probability": 0.55},
            {"horse_name": "乙馬", "neural_rank": 3, "neural_score": 15.0, "neural_win_probability": 0.15},
            {"horse_name": "丙馬", "neural_rank": 2, "neural_score": 30.0, "neural_win_probability": 0.30},
        ], "available", {"production_release": "n6-production-72d-market-implied-v1", "input_dim": 72, "market_feature_policy": "market_implied_probability + market_odds_available; market_log_odds excluded"})
        enriched = n6_integration.enrich_prediction(original, "2026-08-18", "ST", 1)
        assert original == before, "N6 enrichment changed the input V10 prediction"
        assert enriched["n6_integration"]["status"] == "available"
        assert enriched["n6_integration"]["model"]["input_dim"] == 72
        assert enriched["predictions"][0]["n6_rank"] == 1
        assert enriched["predictions"][0]["joint_recommendation"] == "綜合聯合推薦"
        assert enriched["predictions"][0]["joint_consensus"] is True
        assert enriched["predictions"][0]["ev_per_unit"] == before["predictions"][0]["ev_per_unit"]
        assert enriched["predictions"][0]["kelly_quarter_fraction_capped"] == before["predictions"][0]["kelly_quarter_fraction_capped"]
        assert abs(sum(row["joint_neural_probability"] for row in enriched["predictions"]) - 1.0) < 1e-9

        n6_integration.fetch_n6_scores = lambda *_: ("unavailable", None, "N6 暫不可用", None)
        unavailable = n6_integration.enrich_prediction(original, "2026-08-18", "ST", 1)
        assert unavailable["n6_integration"]["status"] == "unavailable"
        assert unavailable["predictions"] == before["predictions"]
    finally:
        n6_integration.fetch_n6_scores = original_fetch
    print(json.dumps({"status": "PASS", "immutable_v10_fields": "PASS", "joint_score": "PASS", "n6_model_contract": "PASS", "n6_failure_fallback": "PASS"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
