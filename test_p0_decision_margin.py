#!/usr/bin/env python3
from __future__ import annotations
import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).parent / "runtime" / "p0_decision_projection.py"
spec = importlib.util.spec_from_file_location("formal_p0_decision_projection", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
assert spec.loader is not None
spec.loader.exec_module(module)


def gate():
    return {
        "status": "restricted_high_uncertainty",
        "fail_closed": False,
        "errors": [],
        "uncertainty": {"formal_ev_allowed": False, "single_anchor_allowed": False, "flags": ["high_normalized_entropy"]},
    }


def fixture(probabilities):
    return {
        "race": {"race_date": "2026-10-05", "racecourse": "ST", "race_no": 1},
        "predictions": [
            {
                "horse_no": no,
                "horse_name": f"測試馬{no}",
                "rank": rank,
                "predicted_win_probability": probability,
                "predicted_place_probability": 0.40 - rank * 0.02,
                "win_odds": 6.0,
                "ev_per_unit": 0.08,
                "kelly_quarter_fraction_capped": 0.004,
                "place_ev_per_unit": 0.02,
                "place_kelly_quarter_fraction_capped": 0.002,
            }
            for rank, (no, probability) in enumerate(probabilities, start=1)
        ],
    }


class WinMarginAndContenderPoolTests(unittest.TestCase):
    def test_case_a_margin_passes_and_pool_is_separate(self):
        result = module.project_prediction(fixture([(1, 0.22), (2, 0.14), (3, 0.10)]), gate())
        self.assertAlmostEqual(result["risk_control"]["win_margin_delta_p"], 0.08)
        self.assertEqual(result["decision_diagnostics"]["win_margin_check"]["rule_id"], "RULE_WIN_MARGIN_CHECK")
        self.assertEqual(result["predictions"][0]["decision_action"], "approved_conservative_paper")
        self.assertGreater(result["predictions"][0]["recommended_paper_stake_fraction"], 0.0)
        self.assertEqual([x["horse_no"] for x in result["contender_pool"]], [1, 2, 3])
        self.assertEqual(result["decision_diagnostics"]["counts"]["margin_blocked"], 0)

    def test_case_b_low_margin_blocks_win_but_keeps_three_contenders(self):
        result = module.project_prediction(fixture([(1, 0.18), (2, 0.16), (3, 0.12)]), gate())
        self.assertAlmostEqual(result["risk_control"]["win_margin_delta_p"], 0.02)
        self.assertEqual([x["horse_no"] for x in result["contender_pool"]], [1, 2, 3])
        self.assertTrue(all(row["recommended_paper_stake_fraction"] == 0.0 for row in result["predictions"]))
        self.assertTrue(all(row["decision_action"] == "blocked" for row in result["predictions"]))
        records = result["decision_diagnostics"]["records"]
        self.assertTrue(all(record["rule_id"] == "RULE_WIN_MARGIN_CHECK" for record in records))
        self.assertTrue(all(record["reason_code"] == "BLOCKED_LOW_MARGIN" for record in records))
        self.assertEqual(result["decision_diagnostics"]["counts"]["margin_blocked"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
