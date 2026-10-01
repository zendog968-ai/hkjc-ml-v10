#!/usr/bin/env python3
from __future__ import annotations

import copy
import unittest

from p0_decision_projection import (
    CONSERVATIVE_PAPER_CAP,
    PAPER_MIN_STAKE_FRACTION,
    SUPPRESSED_FAIL_CLOSED,
    SUPPRESSED_ODDS_INCOMPLETE,
    project_prediction,
)


def raw(*, ev: float = 0.04, kelly: float = 0.008) -> dict:
    return {
        "race": {"race_date": "2026-10-04", "racecourse": "ST", "race_no": 1},
        "predictions": [{
            "horse_no": 3, "horse_name": "測試馬", "predicted_win_probability": 0.18,
            "win_odds": 6.0, "ev_per_unit": ev, "place_ev_per_unit": 0.02,
            "kelly_full_fraction": kelly * 4, "kelly_quarter_fraction_capped": kelly,
            "place_kelly_quarter_fraction_capped": 0.002,
        }],
    }


def gate(status: str = "passed", *, formal: bool = True, single: bool = True, failed: bool = False) -> dict:
    return {
        "status": status, "fail_closed": failed, "errors": [],
        "uncertainty": {"formal_ev_allowed": formal, "single_anchor_allowed": single, "flags": ["high_normalized_entropy"] if status.startswith("restricted") else []},
    }


class DecisionProjectionV2Tests(unittest.TestCase):
    def test_standard_positive_ev_preserves_quarter_kelly(self):
        result = project_prediction(raw(ev=0.06, kelly=0.008), gate())
        row, record = result["predictions"][0], result["decision_diagnostics"]["records"][0]
        self.assertEqual(result["risk_control"]["decision_mode"], "standard")
        self.assertEqual(row["decision_action"], "approved")
        self.assertAlmostEqual(row["recommended_paper_stake_fraction"], 0.008)
        self.assertEqual(record["rule_id"], "RISK_STANDARD_001")

    def test_complete_high_uncertainty_uses_tighter_cap_not_full_block(self):
        result = project_prediction(raw(ev=0.08, kelly=0.015), gate("restricted_high_uncertainty", formal=False, single=False))
        row, record = result["predictions"][0], result["decision_diagnostics"]["records"][0]
        self.assertEqual(result["effective_projection"]["status"], "conservative_paper_only")
        self.assertEqual(row["decision_action"], "approved_conservative_paper")
        self.assertAlmostEqual(row["recommended_paper_stake_fraction"], CONSERVATIVE_PAPER_CAP)
        self.assertFalse(row["decision_eligible"])
        self.assertTrue(row["paper_trial_eligible"])
        self.assertEqual(record["rule_id"], "RISK_CONSERVATIVE_001")

    def test_positive_boundary_rounds_to_paper_minimum_only_after_ev_threshold(self):
        result = project_prediction(raw(ev=0.02, kelly=0.0004), gate("restricted_high_uncertainty", formal=False, single=False))
        row, record = result["predictions"][0], result["decision_diagnostics"]["records"][0]
        self.assertEqual(row["decision_action"], "approved_conservative_paper")
        self.assertAlmostEqual(row["recommended_paper_stake_fraction"], PAPER_MIN_STAKE_FRACTION)
        self.assertEqual(record["rule_id"], "RISK_MIN_STAKE_002")

    def test_micro_ev_does_not_round_up_and_enters_buffer(self):
        result = project_prediction(raw(ev=0.005, kelly=0.0004), gate("restricted_high_uncertainty", formal=False, single=False))
        row, record = result["predictions"][0], result["decision_diagnostics"]["records"][0]
        self.assertEqual(row["decision_action"], "buffer_only")
        self.assertEqual(row["recommended_paper_stake_fraction"], 0.0)
        self.assertGreater(row["paper_buffer_required_fraction"], 0.0)
        self.assertEqual(record["reason_code"], "stake_below_minimum_ev_too_small")

    def test_high_uncertainty_allows_only_one_paper_trial_per_race(self):
        fixture = raw(ev=0.03, kelly=0.004)
        fixture["predictions"].append({
            "horse_no": 4, "horse_name": "次選馬", "predicted_win_probability": 0.14,
            "win_odds": 7.0, "ev_per_unit": 0.06, "place_ev_per_unit": 0.02,
            "kelly_full_fraction": 0.024, "kelly_quarter_fraction_capped": 0.006,
        })
        result = project_prediction(fixture, gate("restricted_high_uncertainty", formal=False, single=False))
        rows = result["predictions"]
        approved = [row for row in rows if row["decision_action"] == "approved_conservative_paper"]
        watch = [row for row in rows if row["decision_action"] == "watchlist"]
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["horse_no"], 3)
        self.assertEqual(len(watch), 1)
        self.assertEqual(watch[0]["recommended_paper_stake_fraction"], 0.0)

    def test_fatal_gate_never_degrades(self):
        source = raw(ev=0.20, kelly=0.02)
        preserved = copy.deepcopy(source)
        result = project_prediction(source, gate("fail_closed", formal=False, single=False, failed=True))
        row, record = result["predictions"][0], result["decision_diagnostics"]["records"][0]
        self.assertEqual(result["risk_control"]["decision_mode"], "blocked_fatal")
        self.assertEqual(row["recommended_paper_stake_fraction"], 0.0)
        self.assertEqual(row["ev_status"], SUPPRESSED_FAIL_CLOSED)
        self.assertEqual(record["rule_id"], "P0_FATAL_001")
        self.assertEqual(source, preserved)

    def test_missing_ev_remains_row_level_block(self):
        fixture = raw()
        fixture["predictions"][0]["ev_per_unit"] = None
        result = project_prediction(fixture, gate())
        row, record = result["predictions"][0], result["decision_diagnostics"]["records"][0]
        self.assertEqual(row["ev_status"], SUPPRESSED_ODDS_INCOMPLETE)
        self.assertEqual(row["decision_action"], "blocked")
        self.assertEqual(record["reason_code"], SUPPRESSED_ODDS_INCOMPLETE)


if __name__ == "__main__":
    unittest.main()
