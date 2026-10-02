#!/usr/bin/env python3
"""Read-only post-race diagnostics for V10/N6.

This script never changes predictions, model weights, EV, Kelly, or databases.
It joins an existing Brier audit to the corresponding pre-race prediction
snapshots and writes an explainable diagnostic JSON.
"""
from __future__ import annotations

import argparse
import json
import re
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


def norm_date(value):
    text = str(value or "").strip().replace("/", "-")
    return text if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) else None


def norm_course(value):
    text = str(value or "").strip().upper()
    return text if re.fullmatch(r"[A-Z]{2,4}", text) else None


def identity_from_prediction_source(value):
    """Extract meeting identity from the immutable prediction-export filename."""
    name = Path(str(value or "")).name
    matched = re.fullmatch(r"predictions_(\d{4}-\d{2}-\d{2})_([A-Za-z]{2,4})\.json", name)
    if not matched:
        return None, None
    return matched.group(1), matched.group(2).upper()


def resolve_identity(audit: dict, cli_date: str | None, cli_course: str | None):
    """Resolve one consistent identity or fail closed on missing/conflicting data."""
    source_date, source_course = identity_from_prediction_source(audit.get("prediction_source"))
    candidates = {
        "cli": (norm_date(cli_date), norm_course(cli_course)),
        "audit": (norm_date(audit.get("race_date")), norm_course(audit.get("racecourse"))),
        "prediction_source": (source_date, source_course),
    }
    dates = {date for date, _ in candidates.values() if date}
    courses = {course for _, course in candidates.values() if course}
    if len(dates) != 1 or len(courses) != 1:
        raise ValueError(
            "fail_closed_identity_missing_or_conflicting: "
            + json.dumps(candidates, ensure_ascii=False, sort_keys=True)
        )
    return dates.pop(), courses.pop(), candidates


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


def diagnose_race(audit_row, prediction_path: Path | None, race_date: str, course: str):
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
        "expected_race_date": race_date,
        "expected_racecourse": course,
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
    race = payload.get("race") if isinstance(payload.get("race"), dict) else {}
    snapshot_date = norm_date(race.get("race_date"))
    snapshot_course = norm_course(race.get("racecourse"))
    if snapshot_date != race_date or snapshot_course != course:
        result["prediction_join_status"] = "identity_mismatch"
        result["flags"].append("prediction_identity_mismatch")
        return result
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
    parser.add_argument("--race-date", help="meeting date override YYYY-MM-DD")
    parser.add_argument("--racecourse", help="meeting course override, e.g. ST")
    args = parser.parse_args()
    audit = load_json(args.audit)
    try:
        race_date, course, identity_sources = resolve_identity(
            audit, args.race_date, args.racecourse
        )
    except ValueError as exc:
        print(json.dumps({"status": str(exc)}, ensure_ascii=False))
        return 2
    rows = []
    for row in audit.get("races", []):
        path = find_prediction(args.predictions_root, race_date, course, int(row["race_no"]))
        rows.append(diagnose_race(row, path, race_date, course))
    scored = [r for r in rows if r["prediction_join_status"] == "matched"]
    out = {
        "report_type": "v10_post_race_model_diagnostics",
        "read_only": True,
        "race_date": race_date,
        "racecourse": course,
        "identity_sources": identity_sources,
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

## end
