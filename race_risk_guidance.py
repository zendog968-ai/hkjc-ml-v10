"""Transparent V10.2 pre-race dispersion and value-candidate guidance.

The functions return research labels only.  They do not place bets, infer missing
odds, or convert a price movement into a claim of inside information.
"""
from __future__ import annotations

import math
from typing import Any


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
    if high_dispersion:
        recommendation = "⚠️【高爆冷風險亂局】本場 14 匹或以上且首選勝率低於 20%；不適合作單膽。若仍作研究性組合規劃，宜以 3–4 匹候選複式單T或連贏互拖，避免集中於單一熱門。"
    elif top_probability is not None and top_probability >= 0.28:
        recommendation = "勝率較集中：首選屬相對機會較高候選，但仍非保證。研究性組合可用首選配 2–3 匹候選；賠率、撤回馬及臨場消息須以官方最後公布覆核。"
    else:
        recommendation = "勝率分佈未達單膽門檻：宜採分散候選的研究性組合，避免因單一模型排序作重注或保證式判斷。"
    bombs = [item for row in valid if (item := candidate_value_bomb(row)) is not None]
    bombs.sort(key=lambda item: (not item["is_positive_model_ev"], -(item["win_ev"] or -math.inf), -item["win_odds"]))
    return {
        "field_size": field_size,
        "top1_win_probability": top_probability,
        "dispersion_warning": high_dispersion,
        "dispersion_label": "⚠️ 高爆冷風險亂局" if high_dispersion else None,
        "bet_recommendation": recommendation,
        "value_bomb_candidates": bombs,
        "notice": "所有提示僅供賽前模型研究；不會自動投注。高賠率、輕磅或內檔本身不代表正期望值，缺少可用賠率或模型 EV 時必須標示為未確認。",
    }
