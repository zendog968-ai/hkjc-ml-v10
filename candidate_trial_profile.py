#!/usr/bin/env python3
"""Candidate-only trial sectional profile disclosure.

The P1 schema stores a Batch-level sectional display, not per-horse splits.
This module therefore describes successive displayed-time deltas as a profile
proxy, never as a physical acceleration measurement or a cross-track rating.
It reads only the isolated P1 candidate database and makes no V10/N6 calls.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROFILE_VERSION = "candidate_trial_sectional_profile_v1"


class CandidateTrialProfileError(ValueError):
    """Raised when no verified candidate trial record can be safely profiled."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def build_trial_profile(db_path: Path, horse_code: str) -> dict[str, Any]:
    if not db_path.is_file():
        raise CandidateTrialProfileError(f"candidate database missing: {db_path}")
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT e.horse_code, e.horse_name, e.draw, e.lbw, e.lbw_raw,
                   e.running_position_json, e.finish_rank, e.finish_time_seconds,
                   e.time_vs_batch_seconds, e.finish_rank_pct, e.comment,
                   b.trial_date, b.venue, b.surface, b.going, b.distance_m,
                   b.batch_label, b.batch_time_seconds, b.sectional_json,
                   s.source_url, s.retrieved_at_utc, s.content_sha256
            FROM trial_entries e
            JOIN trial_batches b ON b.trial_batch_id = e.trial_batch_id
            JOIN source_manifests s ON s.source_id = b.source_id
            WHERE e.horse_code = ?
            """,
            (horse_code.strip().upper(),),
        ).fetchone()
    if row is None:
        raise CandidateTrialProfileError("no candidate trial entry for horse code")

    data = dict(row)
    sectionals = json.loads(data["sectional_json"])
    if not isinstance(sectionals, list) or len(sectionals) < 2 or any(not isinstance(value, (int, float)) for value in sectionals):
        raise CandidateTrialProfileError("batch sectional display is incomplete")
    deltas = [round(float(sectionals[index + 1]) - float(sectionals[index]), 3) for index in range(len(sectionals) - 1)]
    split_sum = round(sum(float(value) for value in sectionals), 3)
    official_time = float(data["batch_time_seconds"])

    report: dict[str, Any] = {
        "profile_version": PROFILE_VERSION,
        "mode": "candidate_only_read_only",
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "database_open_mode": "sqlite_ro_candidate_p1",
        "database_sha256": _sha256(db_path),
        "trial_evidence": {
            "horse_code": data["horse_code"],
            "horse_name": data["horse_name"],
            "trial_date": data["trial_date"],
            "venue": data["venue"],
            "surface": data["surface"],
            "going": data["going"],
            "distance_m": data["distance_m"],
            "batch_label": data["batch_label"],
            "draw": data["draw"],
            "running_position": json.loads(data["running_position_json"]),
            "finish_rank": data["finish_rank"],
            "finish_time_seconds": data["finish_time_seconds"],
            "time_vs_batch_seconds": data["time_vs_batch_seconds"],
            "finish_rank_pct": data["finish_rank_pct"],
            "lbw": data["lbw"],
            "lbw_raw": data["lbw_raw"],
            "comment": data["comment"],
        },
        "batch_display_sectional_profile": {
            "displayed_sectionals_seconds": sectionals,
            "successive_time_deltas_seconds": deltas,
            "displayed_sum_seconds": split_sum,
            "official_finish_time_seconds": official_time,
            "displayed_sum_minus_official_seconds": round(split_sum - official_time, 3),
            "scope": "batch-level displayed sectionals aligned to this winning entry; P1 has no per-horse sectional table",
            "interpretation_rule": "A negative successive time delta means the next displayed split was shorter, not a measured physical acceleration estimate.",
        },
        "surface_transfer_disclosure": {
            "direct_supported_context": "One trial only: Sha Tin All Weather Track, Wet Slow, 1200m.",
            "sha_tin_all_weather": "limited_direct_candidate_evidence_one_trial_only",
            "sha_tin_turf": "not_assessed_no_verified_turf_trial_or_race_record",
            "happy_valley_turf": "not_assessed_no_verified_happy_valley_trial_or_race_record",
            "cross_surface_or_cross_course_score": None,
            "reason": "AWT and turf use different official going schemes and course geometries; one AWT trial cannot establish turf adaptability.",
            "official_course_context": {
                "sha_tin_awt_home_straight_m": 365,
                "sha_tin_turf_home_straight_m": 430,
                "happy_valley_turf_home_straight_m_range": [310, 338],
            },
        },
        "explicitly_not_produced": {
            "physical_acceleration": None,
            "sha_tin_turf_suitability_score": None,
            "happy_valley_suitability_score": None,
            "formal_win_probability": None,
            "hong_kong_elo": None,
            "expected_value": None,
            "kelly_fraction": None,
        },
        "v10_action": "not_touched",
        "n6_action": "not_imported_not_touched",
        "network_requests": 0,
        "training_status": "not_trained_default_deny",
        "source_provenance": {
            "source_url": data["source_url"],
            "retrieved_at_utc": data["retrieved_at_utc"],
            "content_sha256": data["content_sha256"],
        },
    }
    report["report_sha256_excluding_self"] = _hash(report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a candidate trial profile without predicting.")
    parser.add_argument("--db", default="runtime/preseason_candidate/p1_preseason_candidate.sqlite")
    parser.add_argument("--horse-code", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    if output.exists():
        raise SystemExit(f"refusing to overwrite candidate report: {output}")
    payload = build_trial_profile(Path(args.db), args.horse_code)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": "candidate_report_written", "output": str(output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
