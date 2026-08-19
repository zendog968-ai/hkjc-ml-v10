#!/usr/bin/env python3
"""Regression tests for read-only Double Trio T-15/T-5 odds monitoring."""
from __future__ import annotations

import json

from double_trio_odds_monitor import LARGE_MOVE_RATIO, build_leg_odds_monitor


def prediction() -> dict:
    return {
        "market_movement": {
            "early": {"status": "complete", "label": "T_MINUS_15", "captured_at_utc": "2026-09-06T05:00:00+00:00"},
            "late": {"status": "complete", "label": "T_MINUS_5", "captured_at_utc": "2026-09-06T05:10:00+00:00"},
        },
        "predictions": [
            {"horse_no": 1, "horse_name": "落飛馬", "market_odds": 8.0, "odds_t_minus_15": 10.0, "odds_t_minus_5": 8.0, "odds_drop_ratio": -0.2},
            {"horse_no": 2, "horse_name": "升賠馬", "market_odds": 6.0, "odds_t_minus_15": 5.0, "odds_t_minus_5": 6.0, "odds_drop_ratio": 0.2},
            {"horse_no": 3, "horse_name": "平穩馬", "market_odds": 7.5, "odds_t_minus_15": 7.0, "odds_t_minus_5": 7.5, "odds_drop_ratio": 0.07142857142857142},
            {"horse_no": 4, "horse_name": "缺快照馬", "market_odds": 9.0, "odds_t_minus_15": None, "odds_t_minus_5": 9.0, "odds_drop_ratio": None},
        ],
    }


def main() -> int:
    selections = [{"horse_no": number, "horse_name": name} for number, name in ((1, "落飛馬"), (2, "升賠馬"), (3, "平穩馬"), (4, "缺快照馬"))]
    monitor = build_leg_odds_monitor(prediction(), selections)
    assert monitor["status"] == "available", monitor
    assert monitor["large_movement_threshold_ratio"] == LARGE_MOVE_RATIO
    assert monitor["available_selection_count"] == 3
    assert monitor["large_movement_count"] == 2
    assert monitor["large_shortening_count"] == 1
    assert monitor["large_drift_count"] == 1
    rows = {row["horse_no"]: row for row in monitor["selections"]}
    assert rows[1]["movement_status"] == "large_shortening" and rows[1]["source_ratio_consistent"] is True
    assert rows[2]["movement_status"] == "large_drift" and rows[2]["source_ratio_consistent"] is True
    assert rows[3]["movement_status"] == "stable"
    assert rows[4]["movement_status"] == "snapshot_unavailable"

    unavailable = build_leg_odds_monitor({"predictions": [{"horse_no": 1, "horse_name": "無快照馬"}]}, [{"horse_no": 1, "horse_name": "無快照馬"}])
    assert unavailable["status"] == "snapshot_unavailable", unavailable
    print(json.dumps({"status": "PASS", "large_shortening": "PASS", "large_drift": "PASS", "stable": "PASS", "snapshot_unavailable": "PASS"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
