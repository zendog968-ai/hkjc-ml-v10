#!/usr/bin/env python3
"""Contract tests for V10's in-memory N6 enrichment path and stable joint ranks."""
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


def joint_fixture(rows: list[tuple[int, float]]) -> tuple[dict, list[dict]]:
    prediction_rows = [
        {
            "horse_no": horse_no,
            "horse_name": f"同分馬{horse_no}",
            "rank": index + 1,
            "predicted_win_probability": probability,
            "ev_per_unit": 0.0,
        }
        for index, (horse_no, probability) in enumerate(rows)
    ]
    scores = [
        {
            "horse_name": f"同分馬{horse_no}",
            "neural_rank": index + 1,
            "neural_score": probability * 100.0,
            "neural_win_probability": probability,
        }
        for index, (horse_no, probability) in enumerate(rows)
    ]
    return {"race": {"distance_m": 1200, "surface": "草地"}, "predictions": prediction_rows}, scores


def ranks_by_horse(payload: dict) -> dict[int, int]:
    return {int(row["horse_no"]): int(row["joint_rank"]) for row in payload["predictions"]}


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

        # Exact ties must be independent of the incoming prediction/scores array order.
        tied_rows = [(8, 0.25), (2, 0.25), (11, 0.25), (4, 0.25)]
        tied_prediction, tied_scores = joint_fixture(tied_rows)
        n6_integration.fetch_n6_scores = lambda *_: ("fixture", tied_scores, "available", None)
        tied = n6_integration.enrich_prediction(tied_prediction, "2026-08-18", "ST", 1)
        assert ranks_by_horse(tied) == {2: 1, 4: 2, 8: 3, 11: 4}, tied
        assert {row["joint_rank_tie_break"] for row in tied["predictions"]} == {"joint_probability_bucket_then_horse_no"}

        shuffled_prediction, shuffled_scores = joint_fixture(list(reversed(tied_rows)))
        n6_integration.fetch_n6_scores = lambda *_: ("fixture", shuffled_scores, "available", None)
        shuffled = n6_integration.enrich_prediction(shuffled_prediction, "2026-08-18", "ST", 1)
        assert ranks_by_horse(shuffled) == ranks_by_horse(tied), shuffled

        # Numerical noise below 1e-12 shares a score bucket and resolves by runner number.
        near_rows = [(7, 0.2500000000002), (1, 0.25), (9, 0.2499999999999), (3, 0.25)]
        near_prediction, near_scores = joint_fixture(near_rows)
        n6_integration.fetch_n6_scores = lambda *_: ("fixture", near_scores, "available", None)
        near = n6_integration.enrich_prediction(near_prediction, "2026-08-18", "ST", 1)
        assert ranks_by_horse(near) == {1: 1, 3: 2, 7: 3, 9: 4}, near

        # A materially distinct score remains ahead even if its runner number is larger.
        distinct_rows = [(1, 0.25), (9, 0.25000001), (3, 0.25), (7, 0.24999999)]
        distinct_prediction, distinct_scores = joint_fixture(distinct_rows)
        n6_integration.fetch_n6_scores = lambda *_: ("fixture", distinct_scores, "available", None)
        distinct = n6_integration.enrich_prediction(distinct_prediction, "2026-08-18", "ST", 1)
        assert ranks_by_horse(distinct)[9] == 1, distinct
        assert ranks_by_horse(distinct)[1] == 2, distinct

        duplicate_prediction, duplicate_scores = joint_fixture([(1, 0.5), (1, 0.3), (2, 0.2)])
        n6_integration.fetch_n6_scores = lambda *_: ("fixture", duplicate_scores, "available", None)
        duplicate = n6_integration.enrich_prediction(duplicate_prediction, "2026-08-18", "ST", 1)
        assert duplicate["n6_integration"]["status"] == "unavailable", duplicate
        assert duplicate["predictions"] == duplicate_prediction["predictions"], duplicate

        n6_integration.fetch_n6_scores = lambda *_: ("unavailable", None, "N6 暫不可用", None)
        unavailable = n6_integration.enrich_prediction(original, "2026-08-18", "ST", 1)
        assert unavailable["n6_integration"]["status"] == "unavailable"
        assert unavailable["predictions"] == before["predictions"]
    finally:
        n6_integration.fetch_n6_scores = original_fetch
    print(json.dumps({"status": "PASS", "immutable_v10_fields": "PASS", "joint_score": "PASS", "exact_tie": "PASS", "near_tie": "PASS", "shuffled_input": "PASS", "duplicate_runner_fail_closed": "PASS"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
