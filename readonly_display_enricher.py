"""Read-only display enrichment for saved V10 prediction artifacts.

This module never writes prediction.json, never calls a model, and never derives
values when the required source fields are absent or invalid. It adds three
nested display-only objects so the frontend can use a stable presentation shape.
"""
from __future__ import annotations

from copy import deepcopy
import math
from typing import Any


def _finite_positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None:
            return value
    return None


def _odds_drift(row: dict[str, Any]) -> dict[str, Any]:
    existing = row.get("odds_drift") if isinstance(row.get("odds_drift"), dict) else {}
    t15 = _finite_positive(_first(row, "win_odds_t15", "odds_t_minus_15"))
    t5 = _finite_positive(_first(row, "win_odds_t5", "odds_t_minus_5"))
    ratio = _finite(_first(row, "win_drift_ratio", "odds_drop_ratio"))
    if ratio is None and t15 is not None and t5 is not None:
        ratio = (t5 - t15) / t15
    divergence = _finite(_first(row, "win_place_divergence"))
    if divergence is None:
        divergence = _finite(existing.get("win_place_divergence"))
    valid_signal = ratio is not None or divergence is not None
    smart_money = _first(row, "smart_money_flag") if valid_signal else None
    if smart_money is None and valid_signal:
        smart_money = bool((ratio is not None and ratio <= -0.15) or (divergence is not None and abs(divergence) >= 0.15))
    return {
        "win_odds_t15": t15,
        "win_odds_t5": t5,
        "win_drift_ratio": ratio,
        "win_place_divergence": divergence,
        "smart_money_flag": smart_money if isinstance(smart_money, bool) else None,
        "status": "available" if valid_signal else "not_available",
    }


def _kelly(row: dict[str, Any]) -> dict[str, Any]:
    existing = row.get("kelly_staking") if isinstance(row.get("kelly_staking"), dict) else {}
    ev = _finite(_first(row, "ev_per_unit", "win_ev_per_unit", "win_ev"))
    fraction = _finite(_first(row, "kelly_quarter_fraction_capped", "fractional_kelly_stake_fraction"))
    status = _first(row, "kelly_status", "risk_status")
    if status is None:
        status = existing.get("status")
    return {
        "fraction": 0.25 if fraction is not None else None,
        "stake_fraction": fraction,
        "ev": ev,
        "risk_status": status,
        "status": "available" if ev is not None and fraction is not None else "not_available",
    }


def _qualitative(row: dict[str, Any]) -> dict[str, Any]:
    existing = row.get("qualitative_intel") if isinstance(row.get("qualitative_intel"), dict) else {}
    values = {
        "condition_score": _finite(_first(row, "condition_score", "trackwork_condition_score")),
        "trackwork_comment": _first(row, "trackwork_comment"),
        "effort_level": _first(row, "effort_level", "barrier_trial_effort_level"),
        "surge_rating": _first(row, "surge_rating", "barrier_trial_surge_rating"),
        "ambition_flag": _first(row, "ambition_flag", "stable_ambition_flag"),
    }
    for key, value in list(values.items()):
        if value is None:
            values[key] = existing.get(key)
    available = any(value is not None for value in values.values())
    values["status"] = "available" if available else "not_available"
    return values


def enrich_prediction_for_display(prediction: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with display-only nested extensions; source remains untouched."""
    output = deepcopy(prediction)
    rows = output.get("predictions")
    if not isinstance(rows, list):
        return output
    enriched_rows: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        row["odds_drift"] = _odds_drift(row)
        row["kelly_staking"] = _kelly(row)
        row["qualitative_intel"] = _qualitative(row)
        enriched_rows.append(row)
    output["predictions"] = enriched_rows
    output["display_extensions"] = {
        "status": "read_only",
        "source_unchanged": True,
        "fail_closed": True,
        "extensions": ["odds_drift", "kelly_staking", "qualitative_intel"],
    }
    return output
