#!/usr/bin/env python3
"""Create a non-mutating V10 uncertainty sidecar and a calibration eligibility gate.

The sidecar reads a saved prediction JSON and writes a separate research artifact.
It does not load model files, open SQLite, call any network service, alter EV/Kelly,
or feed results back into V10/N6.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from race_risk_guidance import build_uncertainty_report


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def create_sidecar(input_path: Path, output_path: Path) -> dict[str, Any]:
    raw = input_path.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    predictions = payload.get("predictions")
    if not isinstance(predictions, list):
        raise ValueError("prediction payload is missing a predictions list")
    uncertainty = build_uncertainty_report(predictions)
    result = {
        "schema_version": "v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "purpose": "candidate-only uncertainty disclosure",
        "input_prediction_path": str(input_path),
        "input_prediction_sha256": sha256_bytes(raw),
        "race": payload.get("race"),
        "uncertainty": uncertainty,
        "production_contract": {
            "prediction_rows_mutated": False,
            "v10_probability_ev_kelly_modified": False,
            "n6_imported": False,
            "network_requests": 0,
            "calibration_applied": False,
        },
    }
    write_json(output_path, result)
    return result


def calibration_gate(snapshot_coverage_path: Path | None, output_path: Path) -> dict[str, Any]:
    coverage: dict[str, Any] = {}
    if snapshot_coverage_path is not None and snapshot_coverage_path.is_file():
        coverage = json.loads(snapshot_coverage_path.read_text(encoding="utf-8"))
    complete = int(coverage.get("complete_races") or 0)
    minimum = int(coverage.get("minimum_complete_races") or 150)
    result = {
        "schema_version": "v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "eligible_for_candidate_evaluation" if complete >= minimum else "blocked_default_deny",
        "complete_snapshot_races": complete,
        "minimum_required_races": minimum,
        "required_before_any_candidate_fit": [
            "complete T_MINUS_15 and T_MINUS_5 snapshots per race",
            "official-results join with pre-result timestamp ordering",
            "three expanding-window evaluations",
            "race-level bootstrap confidence intervals",
            "independent review of calibration, ranking and degradation slices",
        ],
        "production_contract": {
            "training_started": False,
            "v10_probability_ev_kelly_modified": False,
            "n6_model_or_contract_modified": False,
            "automatic_capture_enabled": False,
        },
    }
    write_json(output_path, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Candidate-only uncertainty tools.")
    sub = parser.add_subparsers(dest="command", required=True)
    side = sub.add_parser("sidecar")
    side.add_argument("--input", required=True)
    side.add_argument("--output", required=True)
    gate = sub.add_parser("calibration-gate")
    gate.add_argument("--snapshot-coverage")
    gate.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.command == "sidecar":
        result = create_sidecar(Path(args.input), Path(args.output))
    else:
        result = calibration_gate(Path(args.snapshot_coverage) if args.snapshot_coverage else None, Path(args.output))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
