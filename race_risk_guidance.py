"""Transparent V10.2 pre-race dispersion, uncertainty and value-candidate guidance.

The functions return research labels only.  They do not place bets, infer missing
odds, or alter the model's race-normalized win probabilities.
"""
from __future__ import annotations

import math
from typing import Any


LOW_SEPARATION_GAP = 0.01
PROBABILITY_SUM_TOLERANCE = 1e-6


def finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def first_number(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = finite(row.get(key))
        if value is not None:
            return value
    return None


def _normalized_component_probabilities(valid: list[dict[str, Any]], key: str) -> dict[int, float] | None:
    """Normalize one model component for disagreement diagnostics only.

    The component calibrators can emit strengths rather than already race-normalized
    values.  This helper makes a temporary normalized copy keyed by row identity; it
    never writes to an input row or feeds results back into the ensemble.
    """
    values = [finite(row.get(key)) for row in valid]
    if any(value is None or value < 0.0 for value in values):
        return None
    total = sum(float(value) for value in values if value is not None)
    if total <= 0.0:
        return None
    return {id(row): float(value) / total for row, value in zip(valid, values) if value is not None}


def build_uncertainty_report(predictions: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a race-level, non-mutating uncertainty report.

    ``top2_gap`` and entropy are calculated only when the saved race-normalized
    probabilities are finite, in range and sum to 1 within tolerance.  A failed
    contract is disclosed as unavailable rather than silently normalized.  The
    ``low_separation_warning`` threshold is a reporting rule, not a probability
    adjustment or an empirically adopted calibration threshold.
    """
    valid: list[dict[str, Any]] = []
    for row in predictions:
        probability = first_number(row, "predicted_win_probability", "win_probability")
        if probability is not None:
            valid.append(row)
    field_size = len(valid)
    base = {
        "status": "unavailable",
        "field_size": field_size,
        "probability_sum": None,
        "top1_horse_name": None,
        "top1_probability": None,
        "top2_horse_name": None,
        "top2_probability": None,
        "top2_gap": None,
        "top2_gap_percentage_points": None,
        "low_separation_threshold": LOW_SEPARATION_GAP,
        "low_separation_warning": False,
        "normalized_entropy": None,
        "entropy_warning": None,
        "entropy_threshold_status": "not_calibrated_report_only",
        "ensemble_disagreement_top1": None,
        "ensemble_disagreement_status": "unavailable",
        "label": None,
        "notice": "不確定性欄位只作賽前研究報告，不會改寫模型機率、排序或既有穩膽門檻。",
    }
    if field_size < 2:
        return {**base, "reason": "field_size_lt_2"}

    probabilities = [first_number(row, "predicted_win_probability", "win_probability") for row in valid]
    if any(probability is None or probability < 0.0 or probability > 1.0 for probability in probabilities):
        return {**base, "reason": "probability_out_of_range_or_missing"}
    probability_sum = sum(float(probability) for probability in probabilities if probability is not None)
    if abs(probability_sum - 1.0) > PROBABILITY_SUM_TOLERANCE:
        return {**base, "probability_sum": probability_sum, "reason": "probability_sum_not_one"}

    ordered = sorted(
        zip(valid, probabilities),
        key=lambda item: (-float(item[1]), str(item[0].get("horse_name") or "")),
    )
    top1_row, top1_probability = ordered[0]
    top2_row, top2_probability = ordered[1]
    gap = float(top1_probability) - float(top2_probability)
    entropy = -sum(float(probability) * math.log(float(probability)) for probability in probabilities if float(probability) > 0.0)
    normalized_entropy = entropy / math.log(field_size)
    low_separation = bool(gap < LOW_SEPARATION_GAP)

    lgb = _normalized_component_probabilities(valid, "lightgbm_calibrated_probability")
    cat = _normalized_component_probabilities(valid, "catboost_calibrated_probability")
    disagreement = None
    disagreement_status = "unavailable_missing_component"
    if lgb is not None and cat is not None:
        disagreement = abs(lgb[id(top1_row)] - cat[id(top1_row)])
        disagreement_status = "available"

    label = None
    if low_separation:
        label = (
            "⚠️【低分離度】首二模型勝率只差 "
            f"{gap * 100.0:.2f} 個百分點；場內排序缺乏足夠分離，不適合作單膽。"
        )
    return {
        **base,
        "status": "available",
        "reason": None,
        "probability_sum": probability_sum,
        "top1_horse_name": top1_row.get("horse_name"),
        "top1_probability": float(top1_probability),
        "top2_horse_name": top2_row.get("horse_name"),
        "top2_probability": float(top2_probability),
        "top2_gap": gap,
        "top2_gap_percentage_points": gap * 100.0,
        "low_separation_warning": low_separation,
        "normalized_entropy": normalized_entropy,
        "ensemble_disagreement_top1": disagreement,
        "ensemble_disagreement_status": disagreement_status,
        "label": label,
    }


def candidate_value_bomb(row: dict[str, Any]) -> dict[str, Any] | None:
    """Flag high-priced runners with a disclosed light-weight or inside-draw edge.

    Positive model EV is required before the label says ``高 EV``.  A runner that
    meets only the price/physical heuristic stays a high-priced candidate rather
    than being misrepresented as positive value.
    """
    win_odds = first_number(row, "win_odds", "market_odds")
    if win_odds is None or win_odds <= 15.0:
        return None
    weight = first_number(row, "weight_lbs", "weight")
    draw = first_number(row, "draw")
    light_weight = weight is not None and weight <= 129.0
    inside_draw = draw is not None and 1 <= draw <= 4
    if not (light_weight or inside_draw):
        return None
    ev = first_number(row, "win_ev", "ev_per_unit", "win_ev_per_unit")
    reasons: list[str] = []
    if light_weight:
        reasons.append(f"輕磅 {weight:.0f} 磅")
    if inside_draw:
        reasons.append(f"內檔 {draw:.0f} 檔")
    return {
        "horse_no": row.get("horse_no"),
        "horse_name": row.get("horse_name"),
        "win_odds": win_odds,
        "weight_lbs": weight,
        "draw": int(draw) if draw is not None else None,
        "win_ev": ev,
        "reasons": reasons,
        "label": "💣 高 EV 冷門" if ev is not None and ev > 0 else "💣 高賠率冷門候選（EV 未確認）",
        "is_positive_model_ev": bool(ev is not None and ev > 0),
    }


def build_race_guidance(predictions: list[dict[str, Any]]) -> dict[str, Any]:
    """Build one field-level warning plus candidate labels from pre-race inputs."""
    valid = [row for row in predictions if first_number(row, "predicted_win_probability", "win_probability") is not None]
    field_size = len(valid)
    top_probability = max((first_number(row, "predicted_win_probability", "win_probability") or 0.0 for row in valid), default=None)
    high_dispersion = bool(field_size >= 14 and top_probability is not None and top_probability < 0.20)
    uncertainty = build_uncertainty_report(predictions)
    if high_dispersion:
        recommendation = "⚠️【高爆冷風險亂局】本場 14 匹或以上且首選勝率低於 20%；不適合作單膽。若仍作研究性組合規劃，宜以 3–4 匹候選複式單T或連贏互拖，避免集中於單一熱門。"
    elif top_probability is not None and top_probability >= 0.28:
        recommendation = "勝率較集中：首選屬相對機會較高候選，但仍非保證。研究性組合可用首選配 2–3 匹候選；賠率、撤回馬及臨場消息須以官方最後公布覆核。"
    else:
        recommendation = "勝率分佈未達單膽門檻：宜採分散候選的研究性組合，避免因單一模型排序作重注或保證式判斷。"
    if uncertainty["low_separation_warning"]:
        recommendation += " 首二機率低分離度已觸發；不應把 Top-1 解讀為具足夠優勢的單膽。"
    bombs = [item for row in valid if (item := candidate_value_bomb(row)) is not None]
    bombs.sort(key=lambda item: (not item["is_positive_model_ev"], -(item["win_ev"] or -math.inf), -item["win_odds"]))
    return {
        "field_size": field_size,
        "top1_win_probability": top_probability,
        "dispersion_warning": high_dispersion,
        "dispersion_label": "⚠️ 高爆冷風險亂局" if high_dispersion else None,
        "uncertainty": uncertainty,
        "bet_recommendation": recommendation,
        "value_bomb_candidates": bombs,
        "notice": "所有提示僅供賽前模型研究；不會自動投注。高賠率、輕磅或內檔本身不代表正期望值，缺少可用賠率或模型 EV 時必須標示為未確認。",
    }
