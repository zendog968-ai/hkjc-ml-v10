#!/usr/bin/env python3
"""Verify V10 forwards real N6 72D model metadata without mutating stored inputs."""

from __future__ import annotations

import copy
import json

import n6_integration


def main() -> int:
    prediction = {
        "race": {"race_date": "2026-08-20", "racecourse": "ST", "race_no": 1, "distance_m": 1200, "surface": "草地"},
        "predictions": [
            {"horse_no": 1, "horse_name": "N6整合測試甲", "jockey": "測試騎師甲", "trainer": "測試練馬師甲", "draw": 1, "weight_lbs": 120, "market_odds": 3.5, "rank": 1, "predicted_win_probability": 0.60, "ev_per_unit": 0.1, "kelly_quarter_fraction_capped": 0.02},
            {"horse_no": 2, "horse_name": "N6整合測試乙", "jockey": "測試騎師乙", "trainer": "測試練馬師乙", "draw": 2, "weight_lbs": 118, "market_odds": 7.0, "rank": 2, "predicted_win_probability": 0.40, "ev_per_unit": 0.0, "kelly_quarter_fraction_capped": 0.01},
        ],
    }
    original = copy.deepcopy(prediction)
    enriched = n6_integration.enrich_prediction(prediction, "2026-08-20", "ST", 1)
    assert prediction == original, "V10 input changed during N6 enrichment"
    assert enriched["n6_integration"]["status"] == "available", enriched["n6_integration"]
    model = enriched["n6_integration"]["model"]
    assert model["production_release"] == "n6-production-72d-market-implied-v1"
    assert model["input_dim"] == 72
    assert "market_log_odds excluded" in model["market_feature_policy"]
    assert len(enriched["predictions"]) == 2
    assert all("n6_neural_score" in row and "joint_neural_score" in row for row in enriched["predictions"])
    assert [row["ev_per_unit"] for row in enriched["predictions"]] == [row["ev_per_unit"] for row in original["predictions"]]
    print(json.dumps({"status": "PASS", "n6_release": model["production_release"], "input_dim": model["input_dim"], "mode": enriched["n6_integration"]["mode"], "readonly_v10_fields": "PASS"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
