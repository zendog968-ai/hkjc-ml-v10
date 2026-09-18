#!/usr/bin/env python3
"""Read-only safety gates for V10 pre-race artifacts.

This module never changes N6 probabilities, model weights, feature vectors, or
odds. It validates artifact identity and writes only additive safety metadata.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ENTROPY_THRESHOLD = 0.92
TOP2_GAP_THRESHOLD = 0.02


def load(path: Path, fallback: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def sha256_file(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def norm_date(value: Any) -> str:
    return str(value or "").replace("/", "-")


def race_identity(payload: dict[str, Any]) -> tuple[str, str, int | None]:
    race = payload.get("race") if isinstance(payload.get("race"), dict) else payload
    try:
        no = int(race.get("race_no")) if race.get("race_no") is not None else None
    except (TypeError, ValueError):
        no = None
    return norm_date(race.get("race_date")), str(race.get("racecourse", "")).upper(), no


def validate_identity(card: dict[str, Any], expected_date: str, expected_course: str, expected_no: int) -> dict[str, Any]:
    date, course, no = race_identity(card)
    field_size = len(card.get("runners", [])) if isinstance(card.get("runners"), list) else 0
    errors = []
    if date != norm_date(expected_date): errors.append("race_date_mismatch")
    if course != str(expected_course).upper(): errors.append("racecourse_mismatch")
    if no != int(expected_no): errors.append("race_no_mismatch")
    if field_size <= 0: errors.append("empty_race_card")
    horse_nos = []
    for runner in card.get("runners", []):
        try: horse_nos.append(int(runner.get("horse_no")))
        except (TypeError, ValueError): errors.append("invalid_horse_no")
    if len(horse_nos) != len(set(horse_nos)): errors.append("duplicate_horse_no")
    return {"ok": not errors, "errors": errors, "field_size": field_size, "horse_nos": sorted(horse_nos)}


def validate_odds(meta: dict[str, Any], expected_date: str, expected_course: str, expected_no: int, field_size: int) -> dict[str, Any]:
    errors = []
    if meta.get("status") != "complete": errors.append("odds_status_not_complete")
    if norm_date(meta.get("race_date")) != norm_date(expected_date): errors.append("odds_date_mismatch")
    if str(meta.get("racecourse", "")).upper() != str(expected_course).upper(): errors.append("odds_course_mismatch")
    try:
        if int(meta.get("race_no")) != int(expected_no): errors.append("odds_race_no_mismatch")
    except (TypeError, ValueError): errors.append("odds_race_no_missing")
    if int(meta.get("complete_win_place_pairs") or 0) != int(field_size): errors.append("odds_pair_count_mismatch")
    if int(meta.get("runners_written") or 0) != int(field_size): errors.append("odds_runner_count_mismatch")
    return {"ok": not errors, "errors": errors, "complete_win_place_pairs": meta.get("complete_win_place_pairs"), "field_size": field_size}


def uncertainty_flags(prediction: dict[str, Any]) -> dict[str, Any]:
    guidance = prediction.get("race_guidance") if isinstance(prediction.get("race_guidance"), dict) else {}
    uncertainty = guidance.get("uncertainty") if isinstance(guidance.get("uncertainty"), dict) else {}
    entropy = uncertainty.get("normalized_entropy")
    gap = uncertainty.get("top2_gap")
    try: entropy = float(entropy) if entropy is not None else None
    except (TypeError, ValueError): entropy = None
    try: gap = float(gap) if gap is not None else None
    except (TypeError, ValueError): gap = None
    flags = []
    if entropy is not None and entropy >= ENTROPY_THRESHOLD: flags.append("high_normalized_entropy")
    if gap is not None and gap <= TOP2_GAP_THRESHOLD: flags.append("low_top2_separation")
    if guidance.get("dispersion_warning") is True: flags.append("model_dispersion_warning")
    restricted = bool(flags)
    return {
        "status": "high_uncertainty" if restricted else "normal_uncertainty",
        "flags": flags,
        "normalized_entropy": entropy,
        "top2_gap": gap,
        "ev_display_mode": "restricted" if restricted else "standard",
        "formal_ev_allowed": not restricted,
        "single_anchor_allowed": not restricted,
        "policy": "p0_high_uncertainty_guard_v1",
    }


def validate_prediction(prediction: dict[str, Any], expected_date: str, expected_course: str, expected_no: int, field_size: int) -> dict[str, Any]:
    date, course, no = race_identity(prediction)
    rows = prediction.get("predictions") if isinstance(prediction.get("predictions"), list) else []
    errors = []
    if date != norm_date(expected_date): errors.append("prediction_date_mismatch")
    if course != str(expected_course).upper(): errors.append("prediction_course_mismatch")
    if no not in (None, int(expected_no)): errors.append("prediction_race_no_mismatch")
    if len(rows) != int(field_size): errors.append("prediction_field_size_mismatch")
    horse_nos = []
    probabilities = []
    for row in rows:
        try: horse_nos.append(int(row.get("horse_no")))
        except (TypeError, ValueError): errors.append("prediction_invalid_horse_no")
        try: probabilities.append(float(row.get("predicted_win_probability")))
        except (TypeError, ValueError): errors.append("prediction_invalid_probability")
    if len(horse_nos) != len(set(horse_nos)): errors.append("prediction_duplicate_horse_no")
    if probabilities and abs(sum(probabilities) - 1.0) > 1e-5: errors.append("prediction_probability_sum_failure")
    return {"ok": not errors, "errors": errors, "field_size": len(rows), "horse_nos": sorted(horse_nos)}


def build_safety(card_path: Path, meta_path: Path, prediction_path: Path, expected_date: str, expected_course: str, expected_no: int) -> dict[str, Any]:
    card = load(card_path, {})
    meta = load(meta_path, {})
    prediction = load(prediction_path, {})
    card_gate = validate_identity(card, expected_date, expected_course, expected_no)
    odds_gate = validate_odds(meta, expected_date, expected_course, expected_no, card_gate["field_size"])
    prediction_gate = validate_prediction(prediction, expected_date, expected_course, expected_no, card_gate["field_size"])
    uncertainty = uncertainty_flags(prediction)
    errors = card_gate["errors"] + odds_gate["errors"] + prediction_gate["errors"]
    status = "passed" if not errors else "fail_closed"
    if uncertainty["status"] == "high_uncertainty" and status == "passed": status = "restricted_high_uncertainty"
    return {
        "schema_version": "v10_p0_safety_gate_v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": status,
        "race": {"race_date": norm_date(expected_date), "racecourse": str(expected_course).upper(), "race_no": int(expected_no)},
        "card_gate": card_gate,
        "odds_gate": odds_gate,
        "prediction_gate": prediction_gate,
        "uncertainty": uncertainty,
        "errors": errors,
        "input_hashes": {"race_card_sha256": sha256_file(card_path), "odds_meta_sha256": sha256_file(meta_path), "prediction_sha256": sha256_file(prediction_path)},
        "fail_closed": bool(errors),
        "formal_ev_note": "High-uncertainty races are marked restricted; this additive gate does not alter N6 probabilities.",
    }


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="V10 P0 artifact safety gate")
    parser.add_argument("--card", required=True, type=Path)
    parser.add_argument("--meta", required=True, type=Path)
    parser.add_argument("--prediction", type=Path)
    parser.add_argument("--date", required=True)
    parser.add_argument("--course", required=True)
    parser.add_argument("--race-no", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--phase", choices=("pre", "post"), default="post")
    args = parser.parse_args()
    card = load(args.card, {})
    meta = load(args.meta, {})
    card_gate = validate_identity(card, args.date, args.course, args.race_no)
    odds_gate = validate_odds(meta, args.date, args.course, args.race_no, card_gate["field_size"])
    if args.phase == "pre":
        gate_ok = card_gate["ok"] and odds_gate["ok"]
        result = {"schema_version": "v10_p0_safety_gate_v1", "phase": "pre", "status": "passed" if gate_ok else "fail_closed", "race": {"race_date": norm_date(args.date), "racecourse": args.course.upper(), "race_no": args.race_no}, "card_gate": card_gate, "odds_gate": odds_gate, "errors": card_gate["errors"] + odds_gate["errors"], "input_hashes": {"race_card_sha256": sha256_file(args.card), "odds_meta_sha256": sha256_file(args.meta)}, "fail_closed": not gate_ok}
    else:
        if args.prediction is None:
            raise SystemExit("--prediction is required for post phase")
        result = build_safety(args.card, args.meta, args.prediction, args.date, args.course, args.race_no)
        result["phase"] = "post"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "fail_closed": result["fail_closed"], "errors": result.get("errors", [])}, ensure_ascii=False))
    return 1 if result["fail_closed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
