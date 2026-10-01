#!/usr/bin/env python3
"""Build read-only place-selection symbols from a saved prediction artifact."""
from __future__ import annotations
import argparse, json, os, tempfile
from pathlib import Path
from typing import Any

from candidate_place_selection_20260928.generate_actionable_tips_place_cap import (
    choose_plan, marker_map, render,
)


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("prediction_json", type=Path)
    parser.add_argument("--label", required=True)
    parser.add_argument("--safety-gate", type=Path, required=True)
    parser.add_argument("--text-output", type=Path, required=True)
    parser.add_argument("--metadata-output", type=Path, required=True)
    parser.add_argument("--race-date", required=True)
    parser.add_argument("--course", required=True)
    parser.add_argument("--race-no", type=int, required=True)
    args = parser.parse_args()

    payload: dict[str, Any] = json.loads(args.prediction_json.read_text(encoding="utf-8"))
    payload["_p0_safety_gate"] = json.loads(args.safety_gate.read_text(encoding="utf-8"))
    plan = choose_plan(payload)
    markers = marker_map(plan)
    gate_status = str(payload.get("_p0_safety_gate", {}).get("status", ""))
    if gate_status == "fail_closed":
        markers = {}
        plan = {"status": "fail_closed", "reason": "P0 fail-closed: no actionable symbols", "anchor": None, "legs": []}
    prediction = payload.get("prediction", payload)
    race = prediction.get("race", {}) if isinstance(prediction, dict) else {}
    rows = prediction.get("predictions", []) if isinstance(prediction, dict) else []
    horse_nos = {str(row.get("horse_no", row.get("horse_number", row.get("runner_no", row.get("number"))))) for row in rows if isinstance(row, dict)}
    marker_rows = {str(k): v for k, v in markers.items() if str(k) in horse_nos}
    metadata = {
        "schema_version": "v10_place_selection_projection_v1",
        "read_only": True,
        "race": {"date": args.race_date, "course": args.course, "race_no": args.race_no},
        "status": plan.get("status"),
        "reason": plan.get("reason"),
        "markers": marker_rows,
        "legend": {"★": "位置重心", "◆": "位置EV最高", "◎": "獨贏首選"},
        "anchor_horse_no": plan.get("anchor", {}).get("horse_no") if isinstance(plan.get("anchor"), dict) else None,
        "leg_horse_nos": [row.get("horse_no") for row in plan.get("legs", []) if isinstance(row, dict)],
    }
    atomic_write(args.text_output, render(payload, args.label))
    atomic_write(args.metadata_output, json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
