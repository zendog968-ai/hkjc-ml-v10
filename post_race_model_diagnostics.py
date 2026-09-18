#!/usr/bin/env python3
"""Read-only post-race diagnostics for V10/N6.

This script never changes predictions, model weights, EV, Kelly, or databases.
It joins an existing Brier audit to the corresponding pre-race prediction
snapshots and writes an explainable diagnostic JSON.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def as_float(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def find_prediction(root: Path, race_date: str, course: str, race_no: int):
    candidates = [
        root / f"{race_date.replace('-', '/')}_{course}_R{race_no:02d}" / "prediction.json",
        root / f"{race_date.replace('-', '/')}_{course}_R{race_no:02d}" / "prediction.json",
        root / f"{race_date.replace('-', '')}_{course}_R{race_no:02d}" / "prediction.json",
    ]
    for path in candidates:
        if path.exists():
            return path
    # Fallback is deliberately constrained to the exact course and race suffix.
    for path in root.glob(f"*_{course}_R{race_no:02d}/prediction.json"):
        try:
            payload = load_json(path)
            if payload.get("race", {}).get("race_date", "").replace("/", "-") == race_date:
                return path
        except Exception:
            continue
    return None


def diagnose_race(audit_row, prediction_path: Path | None):
    race_no = int(audit_row["race_no"])
    brier = as_float(audit_row.get("brier_score"))
    field_size = int(audit_row.get("field_size") or 0)
    uniform = as_float(audit_row.get("uniform_baseline"))
    result = {
        "race_no": race_no,
        "brier_score": brier,
        "uniform_baseline": uniform,
        "brier_worse_than_uniform": bool(brier is not None and uniform is not None and brier > uniform),
        "model_top_horse_no": audit_row.get("model_top_horse_no"),
        "model_top_horse_name": audit_row.get("model_top_horse_name"),
        "winner_horse_no": audit_row.get("winner_horse_no"),
        "winner_horse_name": audit_row.get("winner_horse_name"),
        "model_top3_hit": audit_row.get("model_top3_hit"),
        "model_place_top3_hit": audit_row.get("model_place_top3_hit"),
        "place_brier_score": as_float(audit_row.get("place_brier_score")),
        "prediction_snapshot": str(prediction_path) if prediction_path else None,
        "prediction_join_status": "missing",
        "winner_probability": None,
        "winner_probability_rank": None,
        "top1_probability": None,
        "top2_gap": None,
        "normalized_entropy": None,
        "market_late_status": None,
        "matched_win_odds": None,
        "qualitative_coverage": None,
        "flags": [],
    }
    if prediction_path is None:
        result["flags"].append("prediction_snapshot_missing")
        return result

    payload = load_json(prediction_path)
    predictions = payload.get("predictions") or []
    winner_no = audit_row.get("winner_horse_no")
    ordered = sorted(
        predictions,
        key=lambda row: as_float(row.get("predicted_win_probability")) or -1.0,
        reverse=True,
    )
    winner_row = next((row for row in predictions if row.get("horse_no") == winner_no), None)
    result["prediction_join_status"] = "matched" if winner_row else "winner_not_in_snapshot"
    if winner_row:
        result["winner_probability"] = as_float(winner_row.get("predicted_win_probability"))
        result["winner_probability_rank"] = next(
            (idx for idx, row in enumerate(ordered, start=1) if row.get("horse_no") == winner_no),
            None,
        )
    if ordered:
        result["top1_probability"] = as_float(ordered[0].get("predicted_win_probability"))
        if len(ordered) > 1:
            p1 = as_float(ordered[0].get("predicted_win_probability")) or 0.0
            p2 = as_float(ordered[1].get("predicted_win_probability")) or 0.0
            result["top2_gap"] = p1 - p2
    uncertainty = (payload.get("race_guidance") or {}).get("uncertainty") or {}
    result["normalized_entropy"] = as_float(uncertainty.get("normalized_entropy"))
    result["market_late_status"] = ((payload.get("market_movement") or {}).get("late") or {}).get("status")
    result["matched_win_odds"] = (payload.get("market_overlays") or {}).get("matched_win_odds")
    result["qualitative_coverage"] = payload.get("qualitative_coverage")

    if result["brier_worse_than_uniform"]:
        result["flags"].append("brier_worse_than_uniform")
    if result["model_top3_hit"] is False:
        result["flags"].append("model_top3_miss")
    if result["winner_probability_rank"] and result["winner_probability_rank"] > 3:
        result["flags"].append("winner_outside_model_top3")
    if result["market_late_status"] != "complete":
        result["flags"].append("odds_degraded_or_missing")
    if result["qualitative_coverage"] is not None and as_float(result["qualitative_coverage"]) < 0.5:
        result["flags"].append("qualitative_low_coverage")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--predictions-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    audit = load_json(args.audit)
    race_date = str(audit.get("race_date", "2026-09-13")).replace("/", "-")
    course = str(audit.get("racecourse", "ST"))
    rows = []
    for row in audit.get("races", []):
        path = find_prediction(args.predictions_root, race_date, course, int(row["race_no"]))
        rows.append(diagnose_race(row, path))
    scored = [r for r in rows if r["prediction_join_status"] == "matched"]
    out = {
        "report_type": "v10_post_race_model_diagnostics",
        "read_only": True,
        "race_date": race_date,
        "racecourse": course,
        "race_count": len(rows),
        "prediction_joined_count": len(scored),
        "brier_worse_than_uniform_races": [r["race_no"] for r in rows if r["brier_worse_than_uniform"]],
        "top3_miss_races": [r["race_no"] for r in rows if r["model_top3_hit"] is False],
        "odds_degraded_races": [r["race_no"] for r in rows if "odds_degraded_or_missing" in r["flags"]],
        "mean_winner_probability": mean([r["winner_probability"] for r in scored if r["winner_probability"] is not None]) if any(r["winner_probability"] is not None for r in scored) else None,
        "races": rows,
        "safety_note": "Diagnostic only; does not alter N6, EV, Kelly, predictions, or betting decisions.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("race_count", "prediction_joined_count", "brier_worse_than_uniform_races", "top3_miss_races", "odds_degraded_races")}, ensure_ascii=False))


if __name__ == "__main__":
    main()

## end
