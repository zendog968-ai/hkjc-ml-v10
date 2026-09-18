#!/usr/bin/env python3
"""Generate read-only V10 research tip text from saved prediction snapshots.

The anchor is always the strongest integrated prediction (rank #1 / highest win
probability). Value legs are selected separately using positive EV and either
win probability above 10% or positive Kelly. No bets are placed and no prediction
artifact is modified.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

WIN_KELLY_CAP = 0.02
Q_TOTAL_KELLY_CAP = 0.04


def num(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value: Any = row
        for part in key.split('.'):
            value = value.get(part) if isinstance(value, dict) else None
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def text(row: dict[str, Any], *keys: str, default: str = "未命名") -> str:
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return str(value)
    return default


def choose_plan(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    prediction = payload.get("prediction", payload)
    rows = prediction.get("predictions", []) if isinstance(prediction, dict) else []
    rows = [dict(row) for row in rows if isinstance(row, dict)]
    if not rows:
        return None, []

    for row in rows:
        row["_probability"] = num(row, "calibrated_win_probability", "predicted_win_probability")
        row["_ev"] = num(row, "ev_per_unit", "win_ev_per_unit", "win_ev")
        row["_kelly"] = num(row, "fractional_kelly_stake_fraction", "kelly_quarter_fraction_capped", "kelly_fraction", "kelly_full_fraction")

    # Anchor: never apply EV/Kelly filtering. Rank #1 is authoritative; if rank
    # is absent, use the highest available integrated win probability.
    ranked = sorted(rows, key=lambda r: (num(r, "rank") if num(r, "rank") is not None else 999999, -(r["_probability"] or 0.0)))
    anchor = ranked[0]

    # Value legs: positive EV and win probability >10% OR positive Kelly.
    value_legs = []
    for row in rows:
        if row is anchor:
            continue
        probability = row["_probability"]
        ev = row["_ev"]
        kelly = row["_kelly"]
        if ev is not None and ev > 0 and ((probability is not None and probability > 0.10) or (kelly is not None and kelly > 0)):
            value_legs.append(row)
    value_legs.sort(key=lambda r: (r["_probability"] if r["_probability"] is not None else 0.0, r["_ev"] if r["_ev"] is not None else 0.0, r["_kelly"] if r["_kelly"] is not None else 0.0), reverse=True)
    return anchor, value_legs[:3]


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.2f}"


def nonnegative(value: float | None) -> float:
    return max(float(value or 0.0), 0.0)


def capped_stakes(anchor: dict[str, Any] | None, legs: list[dict[str, Any]]) -> dict[str, Any]:
    raw_win = nonnegative(anchor.get("_kelly") if anchor else None)
    raw_q = sum(nonnegative(row.get("_kelly")) for row in legs)
    return {
        "raw_win": raw_win,
        "raw_q": raw_q,
        "win": min(raw_win, WIN_KELLY_CAP),
        "q_total": min(raw_q, Q_TOTAL_KELLY_CAP),
        "win_cap_triggered": raw_win > WIN_KELLY_CAP,
        "q_cap_triggered": raw_q > Q_TOTAL_KELLY_CAP,
    }


def cap_note(stakes: dict[str, Any]) -> str:
    triggered = []
    if stakes["win_cap_triggered"]:
        triggered.append("WIN 2%上限")
    if stakes["q_cap_triggered"]:
        triggered.append("Q/QP 4%合計上限")
    if not triggered:
        return "注碼上限：WIN不超過本金2.00%；Q/QP合計不超過本金4.00%。"
    return "注碼上限保護已觸發：" + "、".join(triggered) + "；已按硬性上限截斷。"


def odds_status_label(payload: dict[str, Any]) -> str:
    """Return a conservative label; static market_odds alone is not live lock."""
    prediction = payload.get("prediction", payload)
    overlays = prediction.get("market_overlays", {}) if isinstance(prediction, dict) else {}
    movement = prediction.get("market_movement", {}) if isinstance(prediction, dict) else {}
    late = movement.get("late", {}) if isinstance(movement, dict) else {}
    matched = overlays.get("matched_win_odds")
    try:
        matched_count = int(matched)
    except (TypeError, ValueError):
        matched_count = 0
    if late.get("status") == "complete" and matched_count > 0:
        return "【臨場實戰版 · 賠率已鎖定】"
    return "【賽前研究版 · 賠率未就緒】"


def ref(row: dict[str, Any]) -> tuple[str, str]:
    return (text(row, "horse_no", "horse_number", "runner_no", "number", default="—"), text(row, "horse_name"))


def render(payload: dict[str, Any], label: str) -> str:
    anchor, legs = choose_plan(payload)
    safety = payload.get("_p0_safety_gate") if isinstance(payload.get("_p0_safety_gate"), dict) else {}
    stakes = capped_stakes(anchor, legs)
    lines = [f"下注實戰精選方案（{label}）", odds_status_label(payload), "【Fractional Kelly硬上限】WIN ≤ 2.00%；Q/QP合計 ≤ 4.00%", cap_note(stakes)]
    if safety.get("status") == "fail_closed":
        lines.extend(["【Fail-Closed · 資料閘門未通過】", "資料身份或欄位驗證失敗；不生成任何正式方案。"])
        return "\n".join(lines) + "\n"
    if safety.get("status") == "restricted_high_uncertainty" or safety.get("uncertainty", {}).get("formal_ev_allowed") is False:
        lines.extend(["【高不確定性限制 · 不作單膽】", "本場只保留研究排名；EV 不能解除不確定性，停止單膽、重注及自動注碼提示。"])
        return "\n".join(lines) + "\n"
    lines.append("獨贏（Win）/ 位置（Place）：")
    if anchor is None:
        lines.append("資料不足：沒有可用的綜合預測馬匹；不生成組合。")
        return "\n".join(lines) + "\n"

    anchor_no, anchor_name = ref(anchor)
    anchor_ev = anchor["_ev"]
    anchor_note = "模型首選／實力馬膽"
    if anchor_ev is not None and anchor_ev < 0:
        anchor_note += "；大熱獨贏水位不足，建議專攻 Q / QP 單膽拖配腳"
    elif anchor_ev is not None and anchor_ev > 0:
        anchor_note += f"；具備正 EV {anchor_ev:.3f}"
    else:
        anchor_note += "；EV 未確認"
    lines.append(f"{anchor_no}號「{anchor_name}」（預測勝率 {pct(anchor['_probability'])}%，{anchor_note}；WIN建議注碼上限 {stakes['win'] * 100:.2f}% 本金）。")

    if not legs:
        lines.extend(["", "價值配腳：目前沒有同時通過 EV > 0 及（勝率 > 10% 或 Kelly > 0）的候選。", "不生成連贏／位置Q組合。"])
        return "\n".join(lines) + "\n"

    anchor_ref = (anchor_no, anchor_name)
    leg_refs = [ref(row) for row in legs]
    lines.extend(["連贏（Q）/ 位置Q（QP）核心組合："])
    lines.append(f"核心主單打：{anchor_ref[0]} {anchor_ref[1]} ✕ {leg_refs[0][0]} {leg_refs[0][1]}（{anchor_ref[0]} - {leg_refs[0][0]}，實力馬膽配價值腿）。")
    lines.append("連贏單膽拖配腳：")
    drag = "、".join(f"{no}號「{name}」" for no, name in leg_refs)
    drag_numbers = "、".join(no for no, _ in leg_refs)
    lines.append(f"以 {anchor_ref[0]}號「{anchor_ref[1]}」 為單膽，拖 {drag}（{anchor_ref[0]} 拖 {drag_numbers}；Q/QP合計注碼上限 {stakes['q_total'] * 100:.2f}% 本金）。")
    if len(leg_refs) >= 2:
        lines.append("二重彩單式：")
        lines.append(f"{anchor_ref[0]} ➜ {leg_refs[0][0]}、{anchor_ref[0]} ➜ {leg_refs[1][0]}。")
    if len(leg_refs) < 3:
        lines.append(f"註：目前只有 {len(leg_refs)} 匹價值配腳，未補造第三配腳。")
    lines.extend(["", "註：以上為已保存賽前模型快照的研究展示文字，不會自動下注；臨場賠率、資料完整性及風控閘門仍須另行核對。"])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("prediction_json", type=Path)
    parser.add_argument("--label", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--safety-gate", type=Path)
    args = parser.parse_args()
    payload = json.loads(args.prediction_json.read_text(encoding="utf-8"))
    if args.safety_gate and args.safety_gate.exists():
        payload["_p0_safety_gate"] = json.loads(args.safety_gate.read_text(encoding="utf-8"))
    output = render(payload, args.label)
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
