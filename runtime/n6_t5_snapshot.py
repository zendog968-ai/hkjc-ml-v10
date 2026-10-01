#!/usr/bin/env python3
"""Write a fail-closed frozen N6 score snapshot for a validated T-5 race.

The script loads N6 feature state from a meeting-level SQLite snapshot, not the
mutable live V10 database.  It accepts only a complete, identity-aligned T-5
snapshot.  A gate failure writes a ``suppressed`` sidecar with no scores and
returns zero so it cannot interrupt V10's separate baseline pipeline.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def normalized_name(value: Any) -> str:
    return "".join(str(value or "").split())


def normalized_date(value: str) -> str:
    return str(value).replace("/", "-")


def base_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "schema_version": "n6_t5_prerace_snapshot_v1",
        "generated_at_utc": utc_now(),
        "race": {"race_date": normalized_date(args.race_date), "racecourse": args.course.upper(), "race_no": args.race_no},
        "stage": "T_MINUS_5",
        "purpose": "oot_calibration_evidence_only",
        "n6_service_changed": False,
        "v10_prediction_changed": False,
    }


def suppress(args: argparse.Namespace, reason: str, context: dict[str, Any] | None = None) -> int:
    payload = base_payload(args)
    payload.update({
        "status": "suppressed",
        "decision_eligibility": "not_available",
        "suppression_reason": reason,
        "scores": [],
    })
    if context:
        payload["context"] = context
    atomic_write_json(args.output, payload)
    print(json.dumps({"status": "suppressed", "output": str(args.output), "reason": reason}, ensure_ascii=False))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a frozen N6 T-5 score snapshot from immutable meeting state.")
    parser.add_argument("--race-card", type=Path, required=True)
    parser.add_argument("--odds-snapshot", type=Path, required=True)
    parser.add_argument("--odds-meta", type=Path, required=True)
    parser.add_argument("--db-snapshot-manifest", type=Path, required=True)
    parser.add_argument("--p0-gate", type=Path, required=True)
    parser.add_argument("--n6-root", type=Path, default=Path("/home/ubuntu/n6_engine"))
    parser.add_argument("--race-date", required=True)
    parser.add_argument("--course", required=True)
    parser.add_argument("--race-no", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def validate_db_snapshot(args: argparse.Namespace) -> tuple[Path, dict[str, Any]]:
    try:
        manifest = read_json(args.db_snapshot_manifest)
    except FileNotFoundError as error:
        raise ValueError("db_snapshot_manifest_missing") from error
    if manifest.get("schema_version") != "n6_meeting_db_snapshot_v1":
        raise ValueError("db_snapshot_manifest_schema_invalid")
    meeting = manifest.get("meeting")
    expected_date = normalized_date(args.race_date)
    if not isinstance(meeting, dict) or meeting.get("race_date") != expected_date or meeting.get("racecourse") != args.course.upper():
        raise ValueError("db_snapshot_meeting_identity_mismatch")
    snapshot = Path(str(manifest.get("snapshot_path", ""))).resolve(strict=True)
    if manifest.get("snapshot_sha256") != sha256(snapshot):
        raise ValueError("db_snapshot_sha256_mismatch")
    return snapshot, manifest


def validate_inputs(args: argparse.Namespace) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, float], dict[str, Any], dict[str, Any], dict[str, Any]]:
    expected_date, expected_course, expected_no = normalized_date(args.race_date), args.course.upper(), args.race_no
    card = read_json(args.race_card)
    race = card.get("race")
    runners = card.get("runners")
    if not isinstance(race, dict) or normalized_date(race.get("race_date", "")) != expected_date or str(race.get("racecourse", "")).upper() != expected_course or int(race.get("race_no", 0)) != expected_no:
        raise ValueError("race_card_identity_mismatch")
    if not isinstance(runners, list) or len(runners) < 2:
        raise ValueError("race_card_runner_list_invalid")
    names: set[str] = set(); numbers: set[int] = set()
    for runner in runners:
        name = normalized_name(runner.get("horse_name"))
        try:
            number = int(runner.get("horse_no", runner.get("horse_number")))
        except (TypeError, ValueError) as error:
            raise ValueError("race_card_runner_number_invalid") from error
        if not name or number <= 0 or name in names or number in numbers:
            raise ValueError("race_card_runner_identity_not_1_to_1")
        names.add(name); numbers.add(number)

    snapshot = read_json(args.odds_snapshot)
    if snapshot.get("status") != "complete" or snapshot.get("snapshot_label") != "T_MINUS_5":
        raise ValueError("t5_snapshot_not_complete")
    odds_race = snapshot.get("race")
    if not isinstance(odds_race, dict) or normalized_date(odds_race.get("race_date", "")) != expected_date or str(odds_race.get("racecourse", "")).upper() != expected_course or int(odds_race.get("race_no", 0)) != expected_no:
        raise ValueError("t5_snapshot_identity_mismatch")
    odds = snapshot.get("odds")
    if not isinstance(odds, dict) or not isinstance(odds.get("win"), dict) or not isinstance(odds.get("place"), dict):
        raise ValueError("t5_snapshot_odds_missing")
    win = {normalized_name(name): value for name, value in odds["win"].items()}
    place = {normalized_name(name): value for name, value in odds["place"].items()}
    if set(win) != names or set(place) != names:
        raise ValueError("race_card_t5_identity_not_1_to_1")
    win_odds: dict[str, float] = {}
    for name in names:
        try:
            win_value, place_value = float(win[name]), float(place[name])
        except (TypeError, ValueError) as error:
            raise ValueError("t5_odds_value_invalid") from error
        if not math.isfinite(win_value) or not math.isfinite(place_value) or win_value <= 1.0 or place_value <= 0.0:
            raise ValueError("t5_odds_value_invalid")
        win_odds[name] = win_value

    meta = read_json(args.odds_meta)
    if meta.get("status") != "complete" or meta.get("complete_win_place_pairs") != len(runners):
        raise ValueError("odds_meta_not_complete")
    adapter = meta.get("adapter") if isinstance(meta.get("adapter"), dict) else {}
    if adapter.get("race_card_identity_1_to_1") is not True:
        raise ValueError("odds_meta_identity_not_1_to_1")

    p0 = read_json(args.p0_gate)
    if p0.get("fail_closed") is True:
        raise ValueError("p0_gate_fail_closed")
    if p0.get("status") not in {"passed", "restricted_high_uncertainty"}:
        raise ValueError("p0_gate_not_permissive")
    p0_race = p0.get("race")
    if not isinstance(p0_race, dict) or normalized_date(p0_race.get("race_date", "")) != expected_date or str(p0_race.get("racecourse", "")).upper() != expected_course or int(p0_race.get("race_no", 0)) != expected_no:
        raise ValueError("p0_gate_identity_mismatch")
    return race, runners, win_odds, snapshot, meta, p0


def score(args: argparse.Namespace) -> dict[str, Any]:
    snapshot, db_manifest = validate_db_snapshot(args)
    race, runners, win_odds, odds_snapshot, odds_meta, p0 = validate_inputs(args)
    os.environ["N6_V10_DB_PATH"] = str(snapshot)
    n6_root = args.n6_root.resolve(strict=True)
    sys.path.insert(0, str(n6_root))
    import numpy as np  # pylint: disable=import-outside-toplevel
    import torch  # pylint: disable=import-outside-toplevel
    from n6.config import PREPROCESSOR_PATH, V10_DB_PATH  # pylint: disable=import-outside-toplevel
    from n6.feature_engineering import build_live_feature_frame, score_to_race_probabilities  # pylint: disable=import-outside-toplevel
    from n6.model import load_model_bundle  # pylint: disable=import-outside-toplevel

    if V10_DB_PATH.resolve() != snapshot:
        raise ValueError("n6_db_snapshot_environment_mismatch")
    race_payload = {
        "race_date": normalized_date(args.race_date), "racecourse": args.course.upper(), "race_no": args.race_no,
        "race_class": race.get("race_class"), "distance_m": race.get("distance_m"), "surface": race.get("surface"),
        "course_config": race.get("course_config"), "going": race.get("going"),
    }
    runner_payloads = []
    horse_no_by_name: dict[str, int] = {}
    for runner in runners:
        name = normalized_name(runner.get("horse_name"))
        horse_no_by_name[name] = int(runner.get("horse_no", runner.get("horse_number")))
        runner_payloads.append({
            "horse_name": runner.get("horse_name"), "horse_code": runner.get("horse_code"), "jockey": runner.get("jockey"),
            "trainer": runner.get("trainer"), "draw": runner.get("draw"), "weight_lbs": runner.get("weight_lbs", runner.get("weight")),
            "win_odds": win_odds[name],
        })
    frame = build_live_feature_frame(race_payload, runner_payloads)
    bundle = load_model_bundle(n6_root / "models" / "n6_mlp_model.pt", PREPROCESSOR_PATH)
    values = np.asarray(bundle.preprocessor.transform(frame), dtype=np.float32)
    with torch.no_grad():
        logits = bundle.model(torch.tensor(values, dtype=torch.float32)).cpu().numpy()
    probabilities = score_to_race_probabilities(logits / float(bundle.metadata.get("temperature", 1.0)), frame["race_group"])
    rows = []
    for name, probability in zip(frame["horse_name"].tolist(), probabilities.tolist()):
        normalized = normalized_name(name)
        rows.append({"horse_no": horse_no_by_name[normalized], "horse_name": str(name), "neural_win_probability": float(probability), "neural_score": float(probability) * 100.0})
    rows.sort(key=lambda row: (-row["neural_win_probability"], row["horse_no"]))
    for rank, row in enumerate(rows, start=1):
        row["neural_rank"] = rank
        row["neural_win_probability"] = round(row["neural_win_probability"], 8)
        row["neural_score"] = round(row["neural_score"], 6)
    probability_sum = sum(row["neural_win_probability"] for row in rows)
    if not math.isclose(probability_sum, 1.0, abs_tol=2e-7):
        raise ValueError("n6_probability_sum_invalid")

    payload = base_payload(args)
    payload.update({
        "status": "complete",
        "decision_eligibility": "restricted" if p0.get("status") == "restricted_high_uncertainty" else "observation_only",
        "scores": rows,
        "probability_sum": probability_sum,
        "field_size": len(rows),
        "identity_gate": {"race_card_t5_1_to_1": True, "race_card_horse_number_1_to_1": True, "complete_win_place_pairs": len(rows)},
        "p0_gate": {"path": str(args.p0_gate), "sha256": sha256(args.p0_gate), "status": p0.get("status"), "fail_closed": p0.get("fail_closed")},
        "input_provenance": {
            "race_card_path": str(args.race_card), "race_card_sha256": sha256(args.race_card),
            "odds_snapshot_path": str(args.odds_snapshot), "odds_snapshot_sha256": sha256(args.odds_snapshot),
            "odds_meta_path": str(args.odds_meta), "odds_meta_sha256": sha256(args.odds_meta),
            "db_snapshot_manifest_path": str(args.db_snapshot_manifest), "db_snapshot_manifest_sha256": sha256(args.db_snapshot_manifest),
            "db_snapshot_path": str(snapshot), "db_snapshot_sha256": db_manifest.get("snapshot_sha256"),
        },
        "model": {
            "model_path": str(n6_root / "models" / "n6_mlp_model.pt"), "model_sha256": sha256(n6_root / "models" / "n6_mlp_model.pt"),
            "preprocessor_path": str(PREPROCESSOR_PATH), "preprocessor_sha256": sha256(PREPROCESSOR_PATH),
            "production_release": bundle.metadata.get("production_release"), "experiment_id": bundle.metadata.get("experiment_id"),
            "variant": bundle.metadata.get("variant"), "input_dim": bundle.metadata.get("input_dim"),
            "temperature": bundle.metadata.get("temperature"), "feature_contract": bundle.metadata.get("feature_contract"),
        },
        "data_contract": "saved race card + complete T-5 odds + immutable meeting DB snapshot; no post-race labels",
        "post_race_labels_included": False,
    })
    return payload


def main() -> int:
    args = parse_args()
    try:
        result = score(args)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, FileNotFoundError) as error:
        return suppress(args, str(error))
    except Exception as error:  # Defensive boundary: preserve V10 pipeline and leave an auditable sidecar.
        return suppress(args, f"unexpected_{type(error).__name__}")
    atomic_write_json(args.output, result)
    print(json.dumps({"status": "complete", "output": str(args.output), "field_size": result["field_size"], "probability_sum": result["probability_sum"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
