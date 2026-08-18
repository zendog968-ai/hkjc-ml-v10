#!/usr/bin/env python3
"""Contract checks for V10.2 P0 race-uncertainty reporting.

The tests use fixed in-memory examples solely to verify reporting contracts.  They
never fit a model, load race results, or modify prediction probabilities.
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path

from filter_high_probability import build_markdown, build_message
from race_risk_guidance import LOW_SEPARATION_GAP, build_race_guidance, build_uncertainty_report


def assert_close(actual: float | None, expected: float, tolerance: float = 1e-12) -> None:
    if actual is None or abs(actual - expected) > tolerance:
        raise AssertionError(f"expected {expected}, got {actual}")


def low_separation_rows() -> list[dict[str, object]]:
    return [
        {"horse_name": "甲", "predicted_win_probability": 0.306, "lightgbm_calibrated_probability": 0.40, "catboost_calibrated_probability": 0.20, "predicted_place_probability": 0.60},
        {"horse_name": "乙", "predicted_win_probability": 0.299, "lightgbm_calibrated_probability": 0.30, "catboost_calibrated_probability": 0.50, "predicted_place_probability": 0.58},
        {"horse_name": "丙", "predicted_win_probability": 0.200, "lightgbm_calibrated_probability": 0.20, "catboost_calibrated_probability": 0.20, "predicted_place_probability": 0.42},
        {"horse_name": "丁", "predicted_win_probability": 0.195, "lightgbm_calibrated_probability": 0.10, "catboost_calibrated_probability": 0.10, "predicted_place_probability": 0.40},
    ]


def main() -> int:
    rows = low_separation_rows()
    before = copy.deepcopy(rows)
    report = build_uncertainty_report(rows)
    assert report["status"] == "available"
    assert report["low_separation_warning"] is True
    assert_close(report["top2_gap"], 0.007)
    assert_close(report["top2_gap_percentage_points"], 0.7)
    assert report["normalized_entropy"] is not None and 0.0 < report["normalized_entropy"] <= 1.0
    assert report["ensemble_disagreement_status"] == "available"
    assert_close(report["ensemble_disagreement_top1"], 0.20)
    assert rows == before, "P0 reporting must not mutate prediction rows"

    guidance = build_race_guidance(rows)
    assert guidance["uncertainty"] == report
    assert "低分離度" in guidance["bet_recommendation"]
    assert guidance["top1_win_probability"] == 0.306

    exact_threshold = copy.deepcopy(rows)
    exact_threshold[0]["predicted_win_probability"] = 0.305
    exact_threshold[1]["predicted_win_probability"] = 0.295
    exact_threshold[3]["predicted_win_probability"] = 0.200
    exact_report = build_uncertainty_report(exact_threshold)
    assert_close(exact_report["top2_gap"], LOW_SEPARATION_GAP)
    assert exact_report["low_separation_warning"] is False, "threshold must be strict < 1 percentage point"

    invalid_rows = copy.deepcopy(rows)
    invalid_rows[0]["predicted_win_probability"] = 0.70
    invalid_rows[1]["predicted_win_probability"] = 0.20
    invalid_report = build_uncertainty_report(invalid_rows)
    assert invalid_report["status"] == "unavailable"
    assert invalid_report["reason"] == "probability_sum_not_one"
    assert invalid_report["low_separation_warning"] is False

    prediction = {
        "race": {"race_date": "2026-08-18", "racecourse": "ST", "race_no": 1},
        "predictions": rows,
    }
    output = {
        "generated_at_utc": "2026-08-18T00:00:00+00:00",
        "race_label": "2026-08-18 ST 第1場",
        "race_guidance": guidance,
        "strategies": {"熱門穩攻": [], "冷門突襲 / Value Bomb": []},
        "selection_rules": {"熱門穩攻": "test", "冷門突襲 / Value Bomb": "test"},
        "whatsapp": {"direct_link": None},
    }
    markdown = build_markdown(output)
    message = build_message(output["race_label"], output["strategies"], guidance)
    assert "首二差距" in markdown and "低分離度" in markdown
    assert "低分離度" in message

    result = {
        "status": "ok",
        "low_separation_gap": report["top2_gap"],
        "normalized_entropy": report["normalized_entropy"],
        "top1_disagreement": report["ensemble_disagreement_top1"],
        "probabilities_unchanged": rows == before,
        "strict_threshold_verified": True,
        "invalid_vector_degrades_safely": True,
    }
    Path("race_uncertainty_reporting_validation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
