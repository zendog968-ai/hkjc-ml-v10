"""Read-only T-15/T-5 odds movement monitor for Double Trio selections.

The monitor consumes fields already persisted by ``predict.py`` from public HKJC
odds snapshots. It never fetches, writes, recalculates, or modifies V10/N6 output.
"""
from __future__ import annotations

import math
from typing import Any

LARGE_MOVE_RATIO = 0.20
RATIO_TOLERANCE = 1e-9


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _odds(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and number > 1.0 else None


def classify_movement(ratio: float | None) -> tuple[str, str]:
    """Return a non-prescriptive monitoring status from a win-odds ratio."""
    if ratio is None:
        return "snapshot_unavailable", "賠率快照未齊"
    if ratio <= -LARGE_MOVE_RATIO + RATIO_TOLERANCE:
        return "large_shortening", "大幅落飛"
    if ratio >= LARGE_MOVE_RATIO - RATIO_TOLERANCE:
        return "large_drift", "大幅升賠"
    return "stable", "變動未達大幅門檻"


def build_leg_odds_monitor(prediction: dict[str, Any], selections: list[dict[str, Any]]) -> dict[str, Any]:
    """Build transparent movement rows for one official Double Trio leg."""
    prediction_rows = prediction.get("predictions") if isinstance(prediction.get("predictions"), list) else []
    by_number: dict[int, dict[str, Any]] = {}
    for row in prediction_rows:
        if not isinstance(row, dict):
            continue
        number = _number(row.get("horse_no", row.get("runner_no")))
        if number is not None and number > 0 and int(number) == number:
            by_number[int(number)] = row

    movement = prediction.get("market_movement") if isinstance(prediction.get("market_movement"), dict) else {}
    rows: list[dict[str, Any]] = []
    for selected in selections:
        horse_no = int(selected["horse_no"])
        source = by_number.get(horse_no, {})
        early = _odds(source.get("odds_t_minus_15"))
        late = _odds(source.get("odds_t_minus_5"))
        stored_ratio = _number(source.get("odds_drop_ratio"))
        calculated_ratio = (late - early) / early if early is not None and late is not None else None
        ratio = calculated_ratio if calculated_ratio is not None else stored_ratio
        status, label = classify_movement(ratio)
        rows.append(
            {
                "horse_no": horse_no,
                "horse_name": selected.get("horse_name"),
                "odds_t_minus_15": early,
                "odds_t_minus_5": late,
                "odds_change_ratio": ratio,
                "latest_win_odds": _odds(source.get("market_odds")) or late,
                "movement_status": status,
                "movement_label": label,
                "large_movement": status in {"large_shortening", "large_drift"},
                "source_ratio_consistent": (
                    None if calculated_ratio is None or stored_ratio is None else abs(calculated_ratio - stored_ratio) <= RATIO_TOLERANCE
                ),
            }
        )
    large_rows = [row for row in rows if row["large_movement"]]
    available = [row for row in rows if row["odds_change_ratio"] is not None]
    return {
        "status": "available" if available else "snapshot_unavailable",
        "source": "HKJC public Win odds snapshots captured by existing T_MINUS_15 and T_MINUS_5 scheduler stages",
        "formula": "(T_MINUS_5 win odds - T_MINUS_15 win odds) / T_MINUS_15 win odds",
        "large_movement_threshold_ratio": LARGE_MOVE_RATIO,
        "early_snapshot": movement.get("early"),
        "late_snapshot": movement.get("late"),
        "selection_count": len(rows),
        "available_selection_count": len(available),
        "large_movement_count": len(large_rows),
        "large_shortening_count": sum(row["movement_status"] == "large_shortening" for row in rows),
        "large_drift_count": sum(row["movement_status"] == "large_drift" for row in rows),
        "selections": rows,
        "notice": "賠率變動只作公開市場監控；不代表資金來源、內幕資訊、勝出保證或自動投注訊號。",
    }
