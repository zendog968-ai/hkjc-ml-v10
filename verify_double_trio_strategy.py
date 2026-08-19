#!/usr/bin/env python3
"""Regression checks for the read-only V10+N6 Double Trio strategy builder."""
from __future__ import annotations

import json

from double_trio_strategy import OFFICIAL_EVENT_SCHEMA, build_meeting_strategies


def prediction(race_no: int, base_number: int, available: bool = True) -> dict:
    rows = []
    for offset in range(5):
        probability = 0.31 - offset * 0.04
        rows.append(
            {
                "horse_no": base_number + offset,
                "horse_name": f"測試馬{race_no}-{base_number + offset}",
                "rank": offset + 1,
                "predicted_win_probability": probability,
                "ev_per_unit": 0.03 * (4 - offset),
                "joint_rank": offset + 1,
                "joint_neural_score": probability * 100,
                "joint_neural_probability": probability,
                "n6_neural_score": probability * 100,
                "n6_rank": offset + 1,
            }
        )
    return {"race": {"race_no": race_no}, "predictions": rows, "n6_integration": {"status": "available" if available else "unavailable"}}


def tied_joint_prediction(race_no: int, rows: list[tuple[int, int]]) -> dict:
    return {
        "race": {"race_no": race_no},
        "n6_integration": {"status": "available"},
        "predictions": [
            {
                "horse_no": horse_no,
                "horse_name": f"同分測試馬{race_no}-{horse_no}",
                "rank": joint_rank,
                "predicted_win_probability": 0.2,
                "ev_per_unit": 0.0,
                "joint_rank": joint_rank,
                "joint_neural_score": 25.0,
                "joint_neural_probability": 0.25,
                "n6_neural_score": 25.0,
                "n6_rank": joint_rank,
                "joint_rank_tie_break": "joint_probability_bucket_then_horse_no",
            }
            for horse_no, joint_rank in rows
        ],
    }


def official_payload(status: str = "official_confirmed") -> dict:
    return {
        "schema_version": OFFICIAL_EVENT_SCHEMA,
        "status": status,
        "meeting": {"race_date": "2026-09-06", "racecourse": "ST"},
        "source": {"url": "https://example.invalid/hkjc-official-double-trio", "mode": "offline_fixture"},
        "events": [
            {
                "pool_event_code": "DT-1-R3-R7",
                "display_label": "第1口孖T",
                "legs": [{"leg_no": 1, "race_no": 3}, {"leg_no": 2, "race_no": 7}],
            }
        ],
    }


def main() -> int:
    ready = build_meeting_strategies(official_payload(), {3: prediction(3, 1), 7: prediction(7, 11)})
    assert ready["status"] == "ready", ready
    event = ready["events"][0]
    assert event["status"] == "ready", event
    assert [row["horse_no"] for row in event["legs"][0]["selections"]] == [1, 2, 3, 4]
    assert [row["horse_no"] for row in event["legs"][1]["selections"]] == [11, 12, 13, 14]
    plan = event["combination_plan"]
    assert plan["per_leg_trio_combination_count"] == 4
    assert plan["total_bet_combinations"] == 16
    assert len(plan["cross_combinations"]) == 16
    assert plan["unit_stake_hkd"] == 10.0
    assert plan["total_suggested_capital_hkd"] == 160.0

    # The N6 layer already assigns unique deterministic joint ranks for score ties.
    # The strategy layer must preserve these ranks for both legs despite a shuffled
    # prediction-row order.
    tie_ready = build_meeting_strategies(
        official_payload(),
        {
            3: tied_joint_prediction(3, [(8, 3), (2, 1), (11, 4), (4, 2), (14, 5)]),
            7: tied_joint_prediction(7, [(20, 5), (13, 3), (12, 2), (19, 4), (10, 1)]),
        },
    )
    tied_event = tie_ready["events"][0]
    assert tied_event["status"] == "ready", tied_event
    assert [row["horse_no"] for row in tied_event["legs"][0]["selections"]] == [2, 4, 8, 11]
    assert [row["horse_no"] for row in tied_event["legs"][1]["selections"]] == [10, 12, 13, 19]
    assert tied_event["combination_plan"]["total_bet_combinations"] == 16

    duplicate_rank = tied_joint_prediction(3, [(2, 1), (4, 1), (8, 3), (11, 4), (14, 5)])
    rejected = build_meeting_strategies(official_payload(), {3: duplicate_rank, 7: prediction(7, 11)})
    assert rejected["events"][0]["status"] == "joint_rank_unavailable", rejected

    unavailable = build_meeting_strategies(official_payload(), {3: prediction(3, 1, available=False), 7: prediction(7, 11)})
    assert unavailable["events"][0]["status"] == "joint_rank_unavailable", unavailable

    unconfirmed = build_meeting_strategies(official_payload("pending"), {3: prediction(3, 1), 7: prediction(7, 11)})
    assert unconfirmed["status"] == "official_data_unavailable", unconfirmed
    print(json.dumps({"status": "PASS", "strategy": "four-horse Double Trio", "combinations": 16, "capital_hkd": 160, "tie_order_stable": "PASS", "duplicate_rank_fail_closed": "PASS"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
