#!/usr/bin/env python3
"""Regression checks for strict four-horse Double Trio backtesting."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from double_trio_four_horse_backtest import main


def decision(decision_id: str, sha: str, generated: str = "2026-09-06T04:55:00+00:00") -> dict:
    return {
        "decision_id": decision_id,
        "meeting": {"race_date": "2026-09-06", "racecourse": "ST"},
        "provenance": {
            "base_model_sha256": sha,
            "n6_release": "n6-production-72d-market-implied-v1",
            "generated_at_utc": generated,
            "first_leg_start_at_utc": "2026-09-06T05:10:00+00:00",
            "post_race_labels_included": False,
        },
        "strategy": {
            "legs": [
                {"leg_no": 1, "selections": [{"horse_no": value} for value in (1, 2, 3, 4)]},
                {"leg_no": 2, "selections": [{"horse_no": value} for value in (11, 12, 13, 14)]},
            ]
        },
        "combination_plan": {"total_suggested_capital_hkd": 160.0},
    }


def settlement(top1: list[int], top2: list[int], payout: float = 1000.0) -> dict:
    return {
        "official": {
            "leg1_top3": top1,
            "leg2_top3": top2,
            "main_payout_per_unit": payout,
            "payout_unit": 10.0,
            "source_url": "https://racing.hkjc.com/example-official-results",
        }
    }


def main_test() -> int:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        decisions = root / "decisions"; settlements = root / "settlements"; output = root / "summary.json"
        decisions.mkdir(); settlements.mkdir()
        fixtures = {
            "hit-a": (decision("hit-a", "a" * 64), settlement([1, 2, 3], [11, 12, 13])),
            "loss-a": (decision("loss-a", "a" * 64), settlement([1, 2, 3], [11, 12, 15])),
            "hit-b": (decision("hit-b", "b" * 64), settlement([1, 2, 3], [11, 12, 13], 500.0)),
            "late": (decision("late", "c" * 64, "2026-09-06T05:10:00+00:00"), settlement([1, 2, 3], [11, 12, 13])),
        }
        for key, (record, result) in fixtures.items():
            (decisions / f"{key}.json").write_text(json.dumps(record), encoding="utf-8")
            (settlements / f"{key}.json").write_text(json.dumps(result), encoding="utf-8")
        import sys
        old_argv = sys.argv
        try:
            sys.argv = ["double_trio_four_horse_backtest.py", "--decision-root", str(decisions), "--settlement-root", str(settlements), "--output", str(output)]
            assert main() == 0
        finally:
            sys.argv = old_argv
        summary = json.loads(output.read_text(encoding="utf-8"))
        assert summary["settled_record_count"] == 3, summary
        assert summary["excluded_record_count"] == 1, summary
        cohort_a = summary["cohorts"]["a" * 64]
        cohort_b = summary["cohorts"]["b" * 64]
        assert cohort_a["settled_event_count"] == 2 and cohort_a["hit_count"] == 1
        assert cohort_a["total_stake_hkd"] == 320.0 and cohort_a["gross_return_hkd"] == 1000.0
        assert abs(cohort_a["roi"] - (680.0 / 320.0)) < 1e-12
        assert cohort_b["settled_event_count"] == 1 and cohort_b["hit_count"] == 1
        assert summary["cohort_policy"].startswith("Results are segregated")
        assert all(cohort["status"] == "exploratory" for cohort in summary["cohorts"].values())
        assert any("決策生成時間" in reason for reason in summary["exclusion_reason_counts"]), summary
    print(json.dumps({"status": "PASS", "main_hit": "PASS", "loss": "PASS", "model_sha_isolation": "PASS", "exploratory_gate": "PASS", "pre_race_time_gate": "PASS"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main_test())
