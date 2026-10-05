#!/usr/bin/env python3
"""Transparent V10 decision projection for paper trading.

Raw ``prediction.json`` and the P0 gate remain immutable evidence. This additive
projection never alters V10/N6 probabilities, features, model weights, or places
a bet. Fatal identity/odds/prediction failures stay blocked; only a complete but
high-uncertainty P0 gate may create a tightly capped paper-only WIN trial.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

SCHEMA_VERSION = "v10_effective_decision_projection_v3"
MIN_WIN_MARGIN_P = 0.05
HK_TZ = ZoneInfo("Asia/Hong_Kong")
WIN_KELLY_HARD_CAP = 0.02
CONSERVATIVE_PAPER_CAP = 0.005
PAPER_MIN_STAKE_FRACTION = 0.001
MIN_EV_FOR_PAPER_FLOOR = 0.01
CONSERVATIVE_MIN_WIN_PROBABILITY = 0.10
MODE_STANDARD = "standard"
MODE_CONSERVATIVE = "conservative_paper"
MODE_BLOCKED = "blocked_fatal"

# Retained exports for compatibility with the prior projection tests/consumers.
SUPPRESSED_FAIL_CLOSED = "suppressed_fail_closed"
SUPPRESSED_HIGH_UNCERTAINTY = "suppressed_high_uncertainty"
SUPPRESSED_ODDS_INCOMPLETE = "suppressed_odds_incomplete"


class ProjectionError(ValueError):
    pass


def finite(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def first_number(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = finite(row.get(key))
        if value is not None:
            return value
    return None


def sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ProjectionError(f"cannot read input file: {path}") from exc


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectionError(f"invalid JSON input: {path}") from exc
    if not isinstance(value, dict):
        raise ProjectionError(f"JSON top level must be an object: {path}")
    return value


def reasons(gate: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for value in gate.get("errors", []) if isinstance(gate.get("errors"), list) else []:
        if value is not None and str(value) not in result:
            result.append(str(value))
    uncertainty = gate.get("uncertainty") if isinstance(gate.get("uncertainty"), dict) else {}
    for value in uncertainty.get("flags", []) if isinstance(uncertainty.get("flags"), list) else []:
        if value is not None and str(value) not in result:
            result.append(str(value))
    return result or ["p0_policy"]


def gate_mode(gate: dict[str, Any]) -> tuple[str, list[str]]:
    """Fatal P0 integrity failures are never degraded or bypassed."""
    status = str(gate.get("status") or "fail_closed")
    uncertainty = gate.get("uncertainty") if isinstance(gate.get("uncertainty"), dict) else {}
    if gate.get("fail_closed") is True or status in {"fail_closed", "error", "not_ready"}:
        return MODE_BLOCKED, reasons(gate)
    if status == "restricted_high_uncertainty" or uncertainty.get("formal_ev_allowed") is False or uncertainty.get("single_anchor_allowed") is False:
        return MODE_CONSERVATIVE, reasons(gate)
    if status == "passed" and uncertainty.get("formal_ev_allowed") is True and uncertainty.get("single_anchor_allowed") is True:
        return MODE_STANDARD, []
    return MODE_BLOCKED, ["gate_permission_missing"]


def inputs(row: dict[str, Any]) -> dict[str, float | None]:
    return {
        "win_probability": first_number(row, "calibrated_win_probability", "predicted_win_probability", "win_probability"),
        "win_odds": first_number(row, "win_odds", "market_odds", "odds"),
        "ev_per_unit": first_number(row, "ev_per_unit", "win_ev_per_unit", "win_ev"),
        "kelly_full_fraction": first_number(row, "kelly_full_fraction"),
        "kelly_quarter_fraction": first_number(row, "kelly_quarter_fraction_capped", "fractional_kelly_stake_fraction", "kelly_fraction"),
        "place_ev_per_unit": first_number(row, "place_ev_per_unit"),
        "place_kelly_quarter_fraction": first_number(row, "place_kelly_quarter_fraction_capped"),
    }


def input_errors(values: dict[str, float | None]) -> list[str]:
    errors: list[str] = []
    if values["win_probability"] is None or not 0.0 <= float(values["win_probability"]) <= 1.0:
        errors.append("invalid_win_probability")
    if values["win_odds"] is None or float(values["win_odds"]) <= 1.0:
        errors.append("invalid_win_odds")
    if values["ev_per_unit"] is None:
        errors.append("missing_ev")
    if values["kelly_quarter_fraction"] is None or float(values["kelly_quarter_fraction"] or 0.0) < 0.0:
        errors.append("invalid_kelly_fraction")
    return errors


def set_canonical(row: dict[str, Any], values: dict[str, float | None]) -> None:
    for field in ("win_ev_per_unit", "win_ev", "fractional_kelly_stake_fraction", "kelly_fraction"):
        row.pop(field, None)
    row.update({
        "ev_per_unit": values["ev_per_unit"],
        "place_ev_per_unit": values["place_ev_per_unit"],
        "kelly_full_fraction": values["kelly_full_fraction"] or 0.0,
        "kelly_quarter_fraction_capped": values["kelly_quarter_fraction"] or 0.0,
        "place_kelly_quarter_fraction_capped": values["place_kelly_quarter_fraction"] or 0.0,
    })


def block_row(row: dict[str, Any], *, code: str, rule: str, message: str, **extra: Any) -> dict[str, Any]:
    for field in ("ev_per_unit", "win_ev_per_unit", "win_ev", "place_ev_per_unit", "kelly_full_fraction", "kelly_quarter_fraction_capped", "fractional_kelly_stake_fraction", "kelly_fraction", "place_kelly_quarter_fraction_capped"):
        row.pop(field, None)
    row.update({
        "ev_per_unit": None, "place_ev_per_unit": None,
        "kelly_full_fraction": 0.0, "kelly_quarter_fraction_capped": 0.0,
        "place_kelly_quarter_fraction_capped": 0.0,
        "ev_status": code, "place_ev_status": code, "kelly_status": code, "place_kelly_status": code,
        "decision_eligible": False, "paper_trial_eligible": False,
        "decision_action": "blocked", "recommended_paper_stake_fraction": 0.0,
        "stake_policy": "none",
    })
    return diagnostic(row, "blocked", rule, code, message, {}, 0.0, **extra)


def diagnostic(row: dict[str, Any], action: str, rule: str, code: str, message: str, values: dict[str, float | None], stake: float, **extra: Any) -> dict[str, Any]:
    return {
        "horse_no": row.get("horse_no"), "horse_name": row.get("horse_name"),
        "decision_action": action, "rule_id": rule, "reason_code": code,
        "message": message, "inputs": values,
        "recommended_paper_stake_fraction": stake, **extra,
    }


def contender_pool(source_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return an independent top-three WIN-probability observation pool."""
    ranked: list[tuple[float, int, int, dict[str, Any]]] = []
    for index, row in enumerate(source_rows):
        probability = first_number(row, "calibrated_win_probability", "predicted_win_probability", "win_probability")
        if probability is None or not 0.0 <= probability <= 1.0:
            continue
        rank = int(finite(row.get("rank")) or 10**9)
        ranked.append((-probability, rank, index, row))
    ranked.sort(key=lambda item: item[:3])
    return [
        {
            "contender_rank": position,
            "horse_no": row.get("horse_no"),
            "horse_name": row.get("horse_name"),
            "predicted_win_probability": first_number(row, "calibrated_win_probability", "predicted_win_probability", "win_probability"),
            "predicted_place_probability": first_number(row, "predicted_place_probability", "place_probability"),
            "source_rank": row.get("rank"),
            "role": "contender_pool_place_observation",
        }
        for position, (_, _, _, row) in enumerate(ranked[:3], start=1)
    ]


def win_margin(source_rows: list[dict[str, Any]]) -> tuple[float | None, float | None, float | None]:
    values = sorted(
        [p for row in source_rows if (p := first_number(row, "calibrated_win_probability", "predicted_win_probability", "win_probability")) is not None],
        reverse=True,
    )
    if len(values) < 2:
        return values[0] if values else None, values[1] if len(values) > 1 else None, None
    return values[0], values[1], values[0] - values[1]


def low_margin_decision(row: dict[str, Any], values: dict[str, float | None], delta_p: float | None, threshold: float) -> dict[str, Any]:
    code = "BLOCKED_LOW_MARGIN" if delta_p is not None and delta_p < threshold else "SUPPRESSED_HIGH_ENTROPY"
    message = (f"WIN 阻斷：前兩名勝率差 ΔP={delta_p:.6f} 低於門檻 {threshold:.6f}；保留三甲候選池，不分配 WIN 倉位。" if delta_p is not None else "WIN 阻斷：前兩名有效勝率不足，無法通過 margin check；保留三甲候選池。")
    return block_row(row, code=code, rule="RULE_WIN_MARGIN_CHECK", message=message, win_margin_delta_p=delta_p, min_delta_p=threshold, decision_scope="WIN")


def standard_decision(row: dict[str, Any], values: dict[str, float | None]) -> dict[str, Any]:
    set_canonical(row, values)
    ev, kelly = float(values["ev_per_unit"] or 0.0), float(values["kelly_quarter_fraction"] or 0.0)
    cap = min(kelly, WIN_KELLY_HARD_CAP)
    if ev <= 0.0:
        action, rule, code, message, stake = "declined", "RISK_EV_001", "non_positive_ev", "EV 非正值；不提供紙上倉位。", 0.0
    elif cap <= 0.0:
        action, rule, code, message, stake = "declined", "RISK_KELLY_001", "kelly_nonpositive", "Kelly 非正；不向上補造倉位。", 0.0
    else:
        action, rule, code, message, stake = "approved", "RISK_STANDARD_001", "standard_quarter_kelly", "完整 P0 gate、正 EV 與正 Kelly 已通過。", cap
    row.update({
        "ev_status": "available", "place_ev_status": "available" if values["place_ev_per_unit"] is not None else "unavailable_place_input",
        "kelly_status": "available", "place_kelly_status": "available" if values["place_kelly_quarter_fraction"] is not None else "unavailable_place_input",
        "decision_eligible": action == "approved", "paper_trial_eligible": action == "approved",
        "decision_action": action, "recommended_paper_stake_fraction": stake,
        "stake_policy": "standard_quarter_kelly_capped" if stake else "none",
        "kelly_cap_triggered": kelly > WIN_KELLY_HARD_CAP,
    })
    return diagnostic(row, action, rule, code, message, values, stake, hard_cap_fraction=WIN_KELLY_HARD_CAP, cap_triggered=kelly > WIN_KELLY_HARD_CAP)


def conservative_decision(row: dict[str, Any], values: dict[str, float | None], gate_reasons: list[str]) -> dict[str, Any]:
    """Complete high-uncertainty data: one capped WIN paper trial, never Q/QP."""
    set_canonical(row, values)
    probability = float(values["win_probability"] or 0.0)
    ev, kelly = float(values["ev_per_unit"] or 0.0), float(values["kelly_quarter_fraction"] or 0.0)
    reduced = min(kelly, CONSERVATIVE_PAPER_CAP)
    buffer_required, stake = 0.0, 0.0
    if probability < CONSERVATIVE_MIN_WIN_PROBABILITY:
        action, rule, code, message, policy = "watchlist", "RISK_CONSERVATIVE_000", "win_probability_below_conservative_minimum", "高不確定性紙上測試要求勝率至少 10%；僅保留觀察，不分配倉位。", "none"
    elif ev <= 0.0:
        action, rule, code, message, policy = "declined", "RISK_EV_001", "non_positive_ev", "高不確定性下 EV 非正；不提供紙上倉位。", "none"
    elif reduced <= 0.0:
        action, rule, code, message, policy = "buffer_only", "RISK_KELLY_001", "kelly_nonpositive", "正 EV 但 Kelly 非正；僅記錄緩衝需求，不向上補造倉位。", "paper_buffer_only"
        buffer_required = PAPER_MIN_STAKE_FRACTION
    elif reduced < PAPER_MIN_STAKE_FRACTION and ev < MIN_EV_FOR_PAPER_FLOOR:
        action, rule, code, message, policy = "buffer_only", "RISK_MIN_STAKE_001", "stake_below_minimum_ev_too_small", "倉位低於紙上最小單位且 EV 未達門檻；累積緩衝，不向上對齊。", "paper_buffer_only"
        buffer_required = PAPER_MIN_STAKE_FRACTION - reduced
    elif reduced < PAPER_MIN_STAKE_FRACTION:
        action, rule, code, message, policy, stake = "approved_conservative_paper", "RISK_MIN_STAKE_002", "rounded_to_paper_minimum", "完整資料、正 EV 與正 Kelly 已通過；僅紙上測試向上對齊至最小單位。", "conservative_paper_minimum", PAPER_MIN_STAKE_FRACTION
    else:
        action, rule, code, message, policy, stake = "approved_conservative_paper", "RISK_CONSERVATIVE_001", "conservative_quarter_kelly", "資料完整但高不確定性；只保留單一保守紙上 WIN 測試，不生成組合。", "conservative_quarter_kelly", reduced
    row.update({
        "ev_status": "available_conservative_paper" if stake else code,
        "place_ev_status": SUPPRESSED_HIGH_UNCERTAINTY,
        "kelly_status": "available_conservative_paper" if stake else code,
        "place_kelly_status": SUPPRESSED_HIGH_UNCERTAINTY,
        "decision_eligible": False, "paper_trial_eligible": stake > 0.0,
        "decision_action": action, "recommended_paper_stake_fraction": stake,
        "stake_policy": policy, "paper_buffer_required_fraction": buffer_required,
        "kelly_cap_triggered": kelly > CONSERVATIVE_PAPER_CAP,
        "p0_gate_reason_codes": gate_reasons,
    })
    return diagnostic(row, action, rule, code, message, values, stake,
                      paper_buffer_required_fraction=buffer_required,
                      conservative_cap_fraction=CONSERVATIVE_PAPER_CAP,
                      minimum_paper_stake_fraction=PAPER_MIN_STAKE_FRACTION,
                      cap_triggered=kelly > CONSERVATIVE_PAPER_CAP,
                      p0_gate_reason_codes=gate_reasons)


def limit_conservative_exposure(rows: list[dict[str, Any]], records: list[dict[str, Any]]) -> None:
    """Permit at most one high-uncertainty paper test per race.

    Candidates have already passed P0 completeness, positive-EV/Kelly and the
    10% win-probability floor.  Selecting by model win probability first is a
    capital-preservation rule; it avoids turning a noisy field into many small
    correlated exposures.
    """
    eligible = [i for i, record in enumerate(records) if record["decision_action"] == "approved_conservative_paper"]
    if len(eligible) <= 1:
        return
    chosen = max(eligible, key=lambda i: (
        float(records[i]["inputs"].get("win_probability") or 0.0),
        float(records[i]["inputs"].get("ev_per_unit") or 0.0),
        float(records[i]["recommended_paper_stake_fraction"] or 0.0),
    ))
    for index in eligible:
        if index == chosen:
            continue
        row, record = rows[index], records[index]
        row.update({
            "paper_trial_eligible": False,
            "decision_action": "watchlist",
            "recommended_paper_stake_fraction": 0.0,
            "stake_policy": "none",
            "paper_buffer_required_fraction": 0.0,
        })
        record.update({
            "decision_action": "watchlist",
            "rule_id": "RISK_CONSERVATIVE_002",
            "reason_code": "one_conservative_paper_trial_per_race",
            "message": "本場高不確定性只容許一個紙上測試；此馬保留觀察，不分配倉位。",
            "recommended_paper_stake_fraction": 0.0,
        })


def project_prediction(raw_prediction: dict[str, Any], safety_gate: dict[str, Any], *, prediction_sha256: str | None = None, gate_sha256: str | None = None, generated_at_hkt: str | None = None) -> dict[str, Any]:
    if not isinstance(raw_prediction, dict) or not isinstance(safety_gate, dict):
        raise ProjectionError("raw_prediction and safety_gate must be objects")
    source_rows = raw_prediction.get("predictions")
    if not isinstance(source_rows, list) or not all(isinstance(row, dict) for row in source_rows):
        raise ProjectionError("prediction.predictions must be a list of objects")
    mode, gate_reasons = gate_mode(safety_gate)
    rows = deepcopy(source_rows)
    pool = contender_pool(source_rows)
    top1_probability, top2_probability, margin_delta_p = win_margin(source_rows)
    records: list[dict[str, Any]] = []
    for row in rows:
        if mode == MODE_BLOCKED:
            records.append(block_row(row, code=SUPPRESSED_FAIL_CLOSED, rule="P0_FATAL_001", message="P0 身份、賠率或 prediction 完整性未通過；完全阻斷決策。"))
            continue
        values = inputs(row)
        invalid = input_errors(values)
        if invalid:
            records.append(block_row(row, code=SUPPRESSED_ODDS_INCOMPLETE, rule="P0_ROW_001", message="決策輸入不完整或無效：" + ",".join(invalid)))
        elif mode == MODE_STANDARD:
            records.append(standard_decision(row, values))
        elif margin_delta_p is None or margin_delta_p < MIN_WIN_MARGIN_P:
            records.append(low_margin_decision(row, values, margin_delta_p, MIN_WIN_MARGIN_P))
        else:
            records.append(conservative_decision(row, values, gate_reasons))
    if mode == MODE_CONSERVATIVE:
        limit_conservative_exposure(rows, records)
    output = deepcopy(raw_prediction)
    output["predictions"] = rows
    output["risk_control"] = {
        "schema_version": SCHEMA_VERSION, "source_prediction_sha256": prediction_sha256, "source_gate_sha256": gate_sha256,
        "decision_mode": mode, "formal_ev_allowed": mode == MODE_STANDARD,
        "paper_trial_allowed": mode in {MODE_STANDARD, MODE_CONSERVATIVE}, "single_anchor_allowed": mode == MODE_STANDARD,
        "reason_codes": gate_reasons, "hard_win_cap_fraction": WIN_KELLY_HARD_CAP,
        "conservative_paper_cap_fraction": CONSERVATIVE_PAPER_CAP, "paper_min_stake_fraction": PAPER_MIN_STAKE_FRACTION,
        "paper_floor_min_ev": MIN_EV_FOR_PAPER_FLOOR, "conservative_min_win_probability": CONSERVATIVE_MIN_WIN_PROBABILITY,
        "min_win_margin_p": MIN_WIN_MARGIN_P, "win_margin_delta_p": margin_delta_p,
        "win_margin_top1_probability": top1_probability, "win_margin_top2_probability": top2_probability,
        "generated_at_hkt": generated_at_hkt or datetime.now(timezone.utc).astimezone(HK_TZ).isoformat(timespec="seconds"),
        "policy_notice": "僅 paper trading；不會下單或改寫模型機率、EV 原值、N6/V10 特徵。",
    }
    output["decision_diagnostics"] = {
        "schema_version": "v10_decision_diagnostics_v2", "records": records,
        "counts": {"approved": sum(r["decision_action"] in {"approved", "approved_conservative_paper"} for r in records), "blocked": sum(r["decision_action"] == "blocked" for r in records), "buffer_only": sum(r["decision_action"] == "buffer_only" for r in records), "margin_blocked": sum(r["reason_code"] in {"BLOCKED_LOW_MARGIN", "SUPPRESSED_HIGH_ENTROPY"} for r in records), "total": len(records)},
        "fatal_gate": mode == MODE_BLOCKED,
        "win_margin_check": {"rule_id": "RULE_WIN_MARGIN_CHECK", "min_delta_p": MIN_WIN_MARGIN_P, "delta_p": margin_delta_p, "status": "passed" if margin_delta_p is not None and margin_delta_p >= MIN_WIN_MARGIN_P else "blocked"},
    }
    output["contender_pool"] = pool
    output["effective_projection"] = {"status": "ready" if mode == MODE_STANDARD else ("conservative_paper_only" if mode == MODE_CONSERVATIVE else "not_ready"), "source_layer": "prediction.json", "decision_layer": "p0_safety_gate.json", "raw_prediction_unchanged": True}
    return output


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2); handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try: os.unlink(temporary)
        except FileNotFoundError: pass
        raise


def build_from_files(prediction_path: Path, gate_path: Path, output_path: Path) -> dict[str, Any]:
    result = project_prediction(load_json(prediction_path), load_json(gate_path), prediction_sha256=sha256_file(prediction_path), gate_sha256=sha256_file(gate_path))
    atomic_write_json(output_path, result)
    return result


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build V10 risk-controlled effective decision projection")
    parser.add_argument("--prediction", required=True, type=Path); parser.add_argument("--safety-gate", required=True, type=Path); parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try: result = build_from_files(args.prediction, args.safety_gate, args.output)
    except ProjectionError as exc:
        print(json.dumps({"status": MODE_BLOCKED, "error": str(exc)}, ensure_ascii=False)); return 2
    counts = result["decision_diagnostics"]["counts"]
    print(json.dumps({"status": result["effective_projection"]["status"], "decision_mode": result["risk_control"]["decision_mode"], **counts, "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
