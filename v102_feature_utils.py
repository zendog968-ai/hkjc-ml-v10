#!/usr/bin/env python3
"""Shared, conservative helpers for V10.2 body-weight and new-horse priors.

The helpers do not invent missing information: unknown values become neutral values and
carry explicit availability flags for model / report auditability.
"""
from __future__ import annotations

import re
from typing import Any

EXTREME_BODY_WEIGHT_CHANGE_LBS = 15.0


def safe_float(value: object, default: float | None = None) -> float | None:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def body_weight_features(current_lbs: object, previous_lbs: object) -> dict[str, float | int]:
    """Return leakage-safe body-weight values and a >15 lb absolute-change flag."""
    current = safe_float(current_lbs)
    previous = safe_float(previous_lbs)
    known = int(current is not None)
    delta = (current - previous) if current is not None and previous is not None else 0.0
    return {
        "horse_body_weight_pre": current if current is not None else 0.0,
        "horse_body_weight_known_pre": known,
        "body_weight_delta_pre": delta,
        "body_weight_delta_known_pre": int(current is not None and previous is not None),
        "is_extreme_body_weight_change_pre": int(abs(delta) > EXTREME_BODY_WEIGHT_CHANGE_LBS),
    }


def is_new_horse_from_prior_starts(prior_starts: int) -> int:
    """A runner is new to the local model only if it has no earlier official start."""
    return int(prior_starts <= 0)


def distance_match_prior(text: object, distance_m: object) -> tuple[float, int]:
    """Convert explicit pedigree suggested-distance text into a conservative 0..1 prior.

    This function only uses clear numeric kilometre values (for example 1000米-1200米).
    Broad prose without an explicit range is intentionally returned as neutral / unknown.
    """
    source = str(text or "")
    distance = safe_float(distance_m)
    numbers = [float(v) for v in re.findall(r"(\d{3,4})\s*米", source)]
    if distance is None or not numbers:
        return 0.5, 0
    lower, upper = min(numbers), max(numbers)
    # 200m buffer prevents brittle hard boundaries around an advertised range.
    return (1.0 if lower - 200 <= distance <= upper + 200 else 0.0), 1


def trial_prior(
    trial_position: object,
    trial_margin_lengths: object,
    trial_qualified: object,
) -> dict[str, float | int]:
    """Return neutral-safe, structured latest barrier-trial features.

    The trial result is an auxiliary prior for cold starts. It never claims to score an
    unstructured commentary field and uses neutral values when no official trial exists.
    """
    position = safe_float(trial_position)
    margin = safe_float(trial_margin_lengths)
    qualified_text = str(trial_qualified or "").strip()
    known = int(position is not None or margin is not None or bool(qualified_text))
    return {
        "trial_prior_known_pre": known,
        "latest_trial_position_pre": position if position is not None else 0.0,
        "latest_trial_margin_pre": margin if margin is not None else 0.0,
        "latest_trial_qualified_pre": int(qualified_text == "及格"),
    }


def cold_start_prior_score(
    pedigree_distance_match: object,
    pedigree_known: object,
    trial_position: object,
    trial_margin: object,
    trial_qualified: object,
    trial_known: object,
) -> float:
    """Produce an auditable 0..1 prior for new runners, not a target probability.

    It is deliberately conservative: unknown pedigree and trial data return 0.5.
    The result is designed as a model feature / report audit field, not a stand-alone bet.
    """
    components: list[float] = []
    if int(pedigree_known or 0):
        components.append(float(pedigree_distance_match))
    if int(trial_known or 0):
        pos = safe_float(trial_position, 0.0) or 0.0
        margin = safe_float(trial_margin, 0.0) or 0.0
        qualifying = int(trial_qualified or 0)
        positional = max(0.0, min(1.0, 1.0 - max(pos - 1.0, 0.0) / 10.0)) if pos else 0.5
        margin_component = max(0.0, min(1.0, 1.0 - max(margin, 0.0) / 12.0)) if margin else 0.5
        components.append(0.45 * positional + 0.35 * margin_component + 0.20 * qualifying)
    return sum(components) / len(components) if components else 0.5


def extract_sire_name(value: str) -> str | None:
    """Best-effort extraction of a sire label from public HKJC initial-horse text."""
    source = re.sub(r"\s+", " ", value or " ").strip()
    matched = re.search(r"父系\s*[:：]\s*(.+?)(?:\s*(?:毛色|出生年份|產地)\s*[:：]|\||$)", source)
    if not matched:
        return None
    sire = matched.group(1).strip()
    return sire[:160] if sire else None


def normalize_horse_code(value: object) -> str | None:
    matched = re.search(r"([A-Z]\d{3})", str(value or "").upper())
    return matched.group(1) if matched else None
