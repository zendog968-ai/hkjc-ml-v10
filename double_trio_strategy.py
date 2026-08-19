"""Read-only Double Trio strategy assembly from official legs and V10+N6 output.

This module never calculates, overwrites, or persists V10.2 probabilities, EV or
Kelly. It consumes an HKJC-confirmed two-leg event and already generated, N6-
enriched prediction payloads to display a four-horse-per-leg combination plan.
"""
from __future__ import annotations

from itertools import combinations, product
import math
from typing import Any

DEFAULT_UNIT_STAKE_HKD = 10.0
OFFICIAL_EVENT_SCHEMA = "v10_hkjc_double_trio_official_v1"
STRATEGY_SCHEMA = "v10_n6_double_trio_strategy_v1"


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _positive_value_edge(row: dict[str, Any]) -> float:
    """Return a display-only V10 value edge; no probability or stake is recalculated."""
    for field in ("ev_per_unit", "win_ev_per_unit"):
        value = _number(row.get(field))
        if value is not None:
            return max(0.0, value)
    return 0.0


def _selection_row(row: dict[str, Any]) -> dict[str, Any] | None:
    number = _number(row.get("horse_no", row.get("runner_no")))
    joint_rank = _number(row.get("joint_rank"))
    joint_score = _number(row.get("joint_neural_score"))
    if number is None or number <= 0 or int(number) != number or joint_rank is None or joint_rank <= 0 or joint_score is None:
        return None
    return {
        "horse_no": int(number),
        "horse_name": str(row.get("horse_name") or "未命名"),
        "joint_rank": int(joint_rank),
        "joint_neural_score": joint_score,
        "joint_neural_probability": _number(row.get("joint_neural_probability")),
        "n6_neural_score": _number(row.get("n6_neural_score")),
        "n6_rank": _number(row.get("n6_rank")),
        "v10_rank": _number(row.get("rank")),
        "v10_ev_per_unit": _number(row.get("ev_per_unit", row.get("win_ev_per_unit"))),
        "v10_positive_value_edge": _positive_value_edge(row),
    }


def select_top_four(enriched_prediction: dict[str, Any]) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Strictly select the first four runners by the existing N6+V10 joint rank.

    ``joint_rank`` is produced by ``n6_integration.py`` from equal-weighted,
    race-normalized V10 and N6 probabilities. V10 positive EV is retained for
    transparent value context and used only as a deterministic tie-breaker; this
    function never mutates V10's stored probability, EV or Kelly fields.
    """
    n6 = enriched_prediction.get("n6_integration")
    if not isinstance(n6, dict) or n6.get("status") != "available":
        return None, "N6 聯合排名暫不可用；為避免以局部資料選馬，孖T策略不會產生。"
    rows = enriched_prediction.get("predictions")
    if not isinstance(rows, list):
        return None, "V10 預測列格式無效。"
    candidates = [selected for row in rows if isinstance(row, dict) and (selected := _selection_row(row)) is not None]
    if len(candidates) < 4:
        return None, "有效的 V10＋N6 聯合排名少於四匹，無法建立精選四匹複式。"
    candidates.sort(
        key=lambda row: (
            row["joint_rank"],
            -row["joint_neural_score"],
            -row["v10_positive_value_edge"],
            row["horse_no"],
        )
    )
    top_four = candidates[:4]
    ranks = [row["joint_rank"] for row in top_four]
    if len(set(row["horse_no"] for row in top_four)) != 4 or len(set(ranks)) != 4:
        return None, "聯合排名出現重複馬號或排名，策略已安全停止。"
    return top_four, None


def _validated_legs(event: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    legs = event.get("legs")
    if not isinstance(legs, list) or len(legs) != 2:
        return None
    normalized: list[dict[str, Any]] = []
    for leg in legs:
        if not isinstance(leg, dict):
            return None
        leg_no = _number(leg.get("leg_no"))
        race_no = _number(leg.get("race_no"))
        if leg_no is None or race_no is None or int(leg_no) != leg_no or int(race_no) != race_no:
            return None
        normalized.append({**leg, "leg_no": int(leg_no), "race_no": int(race_no)})
    normalized.sort(key=lambda leg: leg["leg_no"])
    if [leg["leg_no"] for leg in normalized] != [1, 2] or normalized[0]["race_no"] == normalized[1]["race_no"]:
        return None
    return normalized[0], normalized[1]


def build_event_strategy(
    event: dict[str, Any],
    predictions_by_race: dict[int, dict[str, Any]],
    unit_stake_hkd: float = DEFAULT_UNIT_STAKE_HKD,
) -> dict[str, Any]:
    """Build one non-executing four-horse Double Trio display strategy."""
    if not isinstance(event, dict):
        return {"status": "invalid_official_event", "message": "官方孖T事件格式無效。"}
    legs = _validated_legs(event)
    if legs is None:
        return {"status": "invalid_official_event", "message": "官方孖T必須含兩個不同場次的首關與次關。"}
    stake = _number(unit_stake_hkd)
    if stake is None or stake <= 0:
        return {"status": "invalid_configuration", "message": "每注金額設定必須為正數。"}
    first_leg, second_leg = legs
    missing = [leg["race_no"] for leg in (first_leg, second_leg) if leg["race_no"] not in predictions_by_race]
    if missing:
        return {
            "status": "awaiting_predictions",
            "message": "官方孖T場次已確認；仍等待指定場次的最新預測工件。",
            "missing_race_nos": missing,
        }
    leg_one, error_one = select_top_four(predictions_by_race[first_leg["race_no"]])
    leg_two, error_two = select_top_four(predictions_by_race[second_leg["race_no"]])
    if error_one or error_two or leg_one is None or leg_two is None:
        return {
            "status": "joint_rank_unavailable",
            "message": "；".join(message for message in (error_one, error_two) if message),
        }
    leg_one_combinations = [list(combo) for combo in combinations([row["horse_no"] for row in leg_one], 3)]
    leg_two_combinations = [list(combo) for combo in combinations([row["horse_no"] for row in leg_two], 3)]
    cross_combinations = [
        {"leg1": list(first), "leg2": list(second)}
        for first, second in product(leg_one_combinations, leg_two_combinations)
    ]
    if len(leg_one_combinations) != 4 or len(leg_two_combinations) != 4 or len(cross_combinations) != 16:
        return {"status": "combination_integrity_failed", "message": "四匹複式注數檢查失敗，策略不會顯示。"}
    return {
        "status": "ready",
        "strategy_schema": STRATEGY_SCHEMA,
        "pool_type": "DOUBLE_TRIO",
        "pool_event_code": event.get("pool_event_code"),
        "display_label": event.get("display_label") or "官方孖T",
        "selection_method": {
            "primary": "嚴格按既有 V10＋N6 綜合聯合排名（joint_rank）取每關前四匹。",
            "secondary": "同分時以 Joint Neural Score、V10 正 EV 邊際及馬號作可重現排序；不重算或改寫 V10 機率、EV、Kelly。",
            "n6_required": True,
        },
        "legs": [
            {"leg_no": 1, "race_no": first_leg["race_no"], "selections": leg_one, "three_horse_combinations": leg_one_combinations},
            {"leg_no": 2, "race_no": second_leg["race_no"], "selections": leg_two, "three_horse_combinations": leg_two_combinations},
        ],
        "combination_plan": {
            "label": "精選四匹複式",
            "per_leg_selection_count": 4,
            "per_leg_trio_combination_count": 4,
            "total_bet_combinations": 16,
            "unit_stake_hkd": round(stake, 2),
            "total_suggested_capital_hkd": round(stake * len(cross_combinations), 2),
            "cross_combinations": cross_combinations,
        },
        "notice": "此為唯讀的賽前模型研究呈現，不會提交、傳送或執行投注；實際投注與可承受損失由使用者自行決定。",
    }


def build_meeting_strategies(
    official_payload: dict[str, Any],
    predictions_by_race: dict[int, dict[str, Any]],
    unit_stake_hkd: float = DEFAULT_UNIT_STAKE_HKD,
) -> dict[str, Any]:
    """Build strategies only from an official-confirmed event artifact."""
    if not isinstance(official_payload, dict) or official_payload.get("schema_version") != OFFICIAL_EVENT_SCHEMA:
        return {"status": "official_data_unavailable", "message": "尚未找到可驗證的官方孖T場次工件。", "events": []}
    if official_payload.get("status") != "official_confirmed":
        return {
            "status": "official_data_unavailable",
            "message": str(official_payload.get("message") or "官方孖T場次尚未確認；系統不會以固定場次假設代替。"),
            "events": [],
            "source": official_payload.get("source"),
        }
    events = official_payload.get("events")
    if not isinstance(events, list) or not events:
        return {"status": "official_data_unavailable", "message": "官方孖T工件沒有有效事件。", "events": []}
    strategies = [build_event_strategy(event, predictions_by_race, unit_stake_hkd) for event in events]
    overall = "ready" if any(item.get("status") == "ready" for item in strategies) else "awaiting_data"
    return {
        "status": overall,
        "meeting": official_payload.get("meeting"),
        "source": official_payload.get("source"),
        "official_fetched_at_utc": official_payload.get("fetched_at_utc"),
        "events": strategies,
        "notice": "只接受官方確認的首關／次關；未確認或 N6 聯合排名不完整時，策略會安全顯示等待狀態。",
    }
