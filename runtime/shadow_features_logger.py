#!/usr/bin/env python3
"""Append-only, read-only shadow feature logger for offline calibration research.

It never mutates predictions, model features, odds, or databases. Rows are only
written after an upstream T-5 safety gate has passed.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FIELDS = [
    "logged_at_utc", "race_date", "racecourse", "race_no", "horse_no", "horse_name", "horse_code",
    "field_size", "draw", "weight_lbs", "jockey", "trainer", "running_style_proxy", "morning_work_score",
    "trial_score", "qualitative_coverage", "win_odds_t5", "place_odds_t5", "predicted_win_probability",
    "predicted_place_probability", "ev_per_unit", "place_ev_per_unit", "kelly_quarter_fraction_capped",
    "track_bias", "track_bias_sample", "model_heat_index", "p0_safety_status", "odds_meta_sha256",
    "prediction_sha256", "source_kind"
]


def load(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if row.get(key) not in (None, ""):
            return row[key]
    return None


def sha256(path: Path) -> str | None:
    import hashlib
    try:
        h = hashlib.sha256(); h.update(path.read_bytes()); return h.hexdigest()
    except OSError:
        return None


def odds_map(path: Path) -> dict[int, dict[str, Any]]:
    payload = load(path, {})
    rows = payload.get("pairs") or payload.get("runners") or payload.get("odds") or []
    if isinstance(rows, dict):
        rows = [{"horse_no": k, **(v if isinstance(v, dict) else {})} for k, v in rows.items()]
    out = {}
    for row in rows if isinstance(rows, list) else []:
        try: no = int(first(row, "horse_no", "horse_number", "number"))
        except (TypeError, ValueError): continue
        out[no] = row
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--race-card", required=True, type=Path)
    parser.add_argument("--prediction", required=True, type=Path)
    parser.add_argument("--odds-snapshot", required=True, type=Path)
    parser.add_argument("--odds-meta", required=True, type=Path)
    parser.add_argument("--safety-gate", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    card = load(args.race_card, {})
    prediction = load(args.prediction, {})
    meta = load(args.odds_meta, {})
    safety = load(args.safety_gate, {})
    if safety.get("status") not in {"passed", "restricted_high_uncertainty"}:
        print(json.dumps({"status": "skipped_safety_gate", "safety_status": safety.get("status")}, ensure_ascii=False)); return 0
    race = card.get("race", {}) if isinstance(card.get("race"), dict) else {}
    rows = prediction.get("predictions", [])
    runners = card.get("runners", [])
    runner_by_no = {}
    for row in runners:
        try: runner_by_no[int(first(row, "horse_no", "horse_number"))] = row
        except (TypeError, ValueError): pass
    pred_by_no = {}
    for row in rows:
        try: pred_by_no[int(first(row, "horse_no", "horse_number"))] = row
        except (TypeError, ValueError): pass
    winplace = odds_map(args.odds_snapshot)
    if not runner_by_no or set(runner_by_no) != set(pred_by_no):
        print(json.dumps({"status": "skipped_field_mismatch", "card_count": len(runner_by_no), "prediction_count": len(pred_by_no)}, ensure_ascii=False)); return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    existing = args.output.exists() and args.output.stat().st_size > 0
    with args.output.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        if not existing: writer.writeheader()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for no, pred in sorted(pred_by_no.items()):
            runner = runner_by_no[no]; odds = winplace.get(no, {})
            writer.writerow({
                "logged_at_utc": now, "race_date": race.get("race_date"), "racecourse": race.get("racecourse"), "race_no": race.get("race_no"),
                "horse_no": no, "horse_name": pred.get("horse_name") or runner.get("horse_name"), "horse_code": pred.get("horse_code"),
                "field_size": len(pred_by_no), "draw": first(pred, "draw") or first(runner, "draw"), "weight_lbs": first(pred, "weight_lbs") or first(runner, "weight_lbs"),
                "jockey": first(pred, "jockey") or first(runner, "jockey"), "trainer": first(pred, "trainer") or first(runner, "trainer"),
                "running_style_proxy": first(pred, "running_style", "running_style_proxy", "run_style", "early_speed_profile"),
                "morning_work_score": first(pred, "morning_work_score", "trackwork_score", "morning_score"),
                "trial_score": first(pred, "trial_score", "barrier_trial_score", "trial_prior"),
                "qualitative_coverage": first(pred, "qualitative_coverage", "oncc_coverage"),
                "win_odds_t5": first(odds, "win_odds", "win", "odds") or first(pred, "odds_t_minus_5"),
                "place_odds_t5": first(odds, "place_odds", "place", "pla_odds"),
                "predicted_win_probability": first(pred, "predicted_win_probability"), "predicted_place_probability": first(pred, "predicted_place_probability"),
                "ev_per_unit": first(pred, "ev_per_unit"), "place_ev_per_unit": first(pred, "place_ev_per_unit"),
                "kelly_quarter_fraction_capped": first(pred, "kelly_quarter_fraction_capped"), "track_bias": first(pred, "track_bias"),
                "track_bias_sample": first(pred, "track_bias_sample"), "model_heat_index": first(pred, "model_heat_index"),
                "p0_safety_status": safety.get("status"), "odds_meta_sha256": sha256(args.odds_meta), "prediction_sha256": sha256(args.prediction),
                "source_kind": "t5_shadow_features_after_safety_gate"
            })
    print(json.dumps({"status": "appended", "rows": len(pred_by_no), "output": str(args.output), "elapsed_budget_seconds": 30}, ensure_ascii=False)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
