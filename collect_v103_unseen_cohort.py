#!/usr/bin/env python3
"""Collect immutable V10.3 Bayesian-calibration cohorts from V10.2 T-5 snapshots.

A record becomes eligible only when the source prediction was demonstrably generated
before its scheduled HKT start, its hash still matches the provenance record, and
an official completed local result is available with the same complete field.  The
collector never reconstructs a pre-race vector from post-race data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

HK_TZ = ZoneInfo("Asia/Hong_Kong")
PROBABILITY_TOLERANCE = 1e-6
REQUIRED_SOURCE_KIND = "pre_race_scheduler_t_minus_5"
POST_RACE_KEYS = {"finish_pos", "finish_pos_text", "winner", "actual_win", "target_win", "dividend", "payout"}


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = handle.name
    Path(temporary).replace(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now() -> str:
    return datetime.now(HK_TZ).isoformat(timespec="seconds")


def normalize_horse_no(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value.is_integer() and value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
        return number if number > 0 else None
    return None


def parse_hkt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return (parsed.replace(tzinfo=HK_TZ) if parsed.tzinfo is None else parsed).astimezone(HK_TZ)


def text_probability(row: dict[str, Any]) -> float | None:
    for key in ("predicted_win_probability", "race_normalized_probability", "win_probability"):
        value = row.get(key)
        if isinstance(value, bool) or value is None:
            continue
        try:
            result = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(result):
            return result
    return None


def race_key(date_value: str, course: str, race_no: int) -> str:
    return f"{date_value}:{course.upper()}:R{int(race_no):02d}"


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def official_local_result(conn: sqlite3.Connection, date_value: str, course: str, race_no: int) -> tuple[list[dict[str, Any]] | None, str | None]:
    try:
        rows = conn.execute(
            """SELECT horse_no,horse_name,finish_pos,finish_pos_text
               FROM starters
               WHERE race_date=? AND racecourse=? AND race_no=?
               ORDER BY COALESCE(finish_pos,999),horse_no""",
            (date_value, course, race_no),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        return None, f"official_query_failed:{type(exc).__name__}"
    if not rows:
        return None, "official_result_not_archived"
    official = [dict(item) for item in rows]
    numbers = [normalize_horse_no(item.get("horse_no")) for item in official]
    if any(number is None for number in numbers) or len(set(numbers)) != len(numbers):
        return None, "official_field_invalid"
    winners = [item for item in official if item.get("finish_pos") == 1]
    if len(winners) != 1:
        return None, "official_winner_missing_or_ambiguous"
    return official, None


def validate_snapshot(provenance_path: Path, conn: sqlite3.Connection) -> tuple[dict[str, Any] | None, str | None]:
    provenance = read_json(provenance_path)
    if not provenance:
        return None, "provenance_unreadable"
    if provenance.get("schema_version") != "v10_3_prerace_snapshot_provenance_v1":
        return None, "unsupported_provenance_schema"
    if provenance.get("source_kind") != REQUIRED_SOURCE_KIND:
        return None, "source_not_t_minus_5_scheduler"
    if provenance.get("post_race_labels_included") is not False:
        return None, "provenance_post_race_flag_invalid"
    model_sha = str(provenance.get("model_sha256") or "").strip().lower()
    if len(model_sha) != 64 or any(char not in "0123456789abcdef" for char in model_sha):
        return None, "model_hash_invalid"
    scheduled = parse_hkt(provenance.get("scheduled_start_hkt"))
    generated = parse_hkt(provenance.get("prediction_generated_hkt"))
    if not scheduled or not generated:
        return None, "snapshot_time_invalid"
    if generated >= scheduled:
        return None, "prediction_not_strictly_prerace"
    try:
        date_value = datetime.strptime(str(provenance.get("race_date")), "%Y-%m-%d").strftime("%Y-%m-%d")
        course = str(provenance.get("racecourse") or "").upper()
        race_no = int(provenance.get("race_no"))
    except (TypeError, ValueError):
        return None, "race_identity_invalid"
    if course not in {"ST", "HV"} or race_no < 1:
        return None, "race_identity_invalid"
    prediction_path = Path(str(provenance.get("prediction_path") or ""))
    if not prediction_path.is_file():
        return None, "prediction_snapshot_missing"
    expected_sha = str(provenance.get("prediction_sha256") or "").lower()
    if sha256_file(prediction_path) != expected_sha:
        return None, "prediction_snapshot_hash_mismatch"
    prediction_payload = read_json(prediction_path)
    rows = prediction_payload.get("predictions") if prediction_payload else None
    if not isinstance(rows, list) or len(rows) < 2:
        return None, "prediction_rows_missing"
    if any(any(key in row for key in POST_RACE_KEYS) for row in rows if isinstance(row, dict)):
        return None, "prediction_rows_contain_post_race_field"
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            return None, "prediction_row_invalid"
        horse_no = normalize_horse_no(row.get("horse_no"))
        probability = text_probability(row)
        if horse_no is None or probability is None:
            return None, "prediction_field_or_probability_missing"
        if probability < 0.0 or probability > 1.0:
            return None, "prediction_probability_out_of_range"
        normalized.append({
            "horse_no": horse_no,
            "horse_name": str(row.get("horse_name") or ""),
            "predicted_win_probability": probability,
            "lightgbm_calibrated_probability": row.get("lightgbm_calibrated_probability"),
            "catboost_calibrated_probability": row.get("catboost_calibrated_probability"),
        })
    numbers = [item["horse_no"] for item in normalized]
    if len(set(numbers)) != len(numbers):
        return None, "prediction_duplicate_horse_no"
    probability_sum = float(sum(item["predicted_win_probability"] for item in normalized))
    if not math.isfinite(probability_sum) or abs(probability_sum - 1.0) > PROBABILITY_TOLERANCE:
        return None, "prediction_probability_sum_not_one"
    official, official_reason = official_local_result(conn, date_value, course, race_no)
    if official_reason:
        return None, official_reason
    assert official is not None
    official_numbers = {normalize_horse_no(item["horse_no"]) for item in official}
    if set(numbers) != official_numbers:
        return None, "prediction_official_field_mismatch"
    winner = next(item for item in official if item.get("finish_pos") == 1)
    winner_no = normalize_horse_no(winner.get("horse_no"))
    if winner_no is None:
        return None, "official_winner_invalid"
    enriched = []
    for item in normalized:
        enriched.append({**item, "actual_win": int(item["horse_no"] == winner_no)})
    return {
        "schema_version": "v10_3_unseen_cohort_record_v1",
        "race_key": race_key(date_value, course, race_no),
        "race_date": date_value,
        "racecourse": course,
        "race_no": race_no,
        "scheduled_start_hkt": scheduled.isoformat(),
        "prediction_generated_hkt": generated.isoformat(),
        "model_sha256": model_sha,
        "source_prediction_path": str(prediction_path),
        "source_prediction_sha256": expected_sha,
        "source_provenance_path": str(provenance_path),
        "source_provenance_sha256": sha256_file(provenance_path),
        "post_race_labels_in_source": False,
        "winner_horse_no": winner_no,
        "predictions": enriched,
    }, None


def discover_provenance(snapshot_root: Path) -> list[Path]:
    return sorted(path for path in snapshot_root.rglob("v103_snapshot_provenance.json") if path.is_file())


def model_bucket(model_sha: str) -> str:
    return model_sha[:16]


def write_record(record: dict[str, Any], records_root: Path) -> tuple[bool, str]:
    path = records_root / model_bucket(record["model_sha256"]) / f"{record['race_key'].replace(':', '_')}.json"
    if path.exists():
        existing = read_json(path)
        if existing and existing.get("source_prediction_sha256") == record["source_prediction_sha256"]:
            return False, "already_recorded"
        return False, "record_conflict_existing_source_differs"
    atomic_write_json(path, record)
    return True, "record_written"


def load_records(records_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(records_root.rglob("*.json")):
        payload = read_json(path)
        if payload and payload.get("schema_version") == "v10_3_unseen_cohort_record_v1":
            rows.append(payload)
    return rows


def cohort_fingerprint(records: list[dict[str, Any]]) -> str:
    canonical = [
        {"race_key": row["race_key"], "prediction_sha256": row["source_prediction_sha256"], "model_sha256": row["model_sha256"]}
        for row in sorted(records, key=lambda item: (item["scheduled_start_hkt"], item["race_key"]))
    ]
    return hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def materialize_csv(records: list[dict[str, Any]], path: Path) -> None:
    lines = ["race_date,racecourse,race_no,horse_name,race_normalized_probability,actual_win"]
    for record in sorted(records, key=lambda item: (item["scheduled_start_hkt"], item["race_key"])):
        for row in record["predictions"]:
            horse = str(row["horse_name"]).replace('"', '""')
            lines.append(f'{record["race_date"]},{record["racecourse"]},{record["race_no"]},"{horse}",{float(row["predicted_win_probability"]):.12f},{int(row["actual_win"])}')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_walk_forward(project_dir: Path, csv_path: Path, output_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    script = project_dir / "walk_forward_v103_bayesian_uncertainty.py"
    command = [
        sys.executable, str(script), "--input", str(csv_path), "--output-dir", str(output_dir),
        "--initial-train-races", str(args.initial_train_races), "--validation-races", str(args.validation_races),
        "--test-races", str(args.test_races), "--folds", str(args.required_folds),
    ]
    result = subprocess.run(command, cwd=project_dir, capture_output=True, text=True, check=False, timeout=args.evaluation_timeout_seconds)
    (output_dir / "evaluation_stdout.log").write_text(result.stdout or "", encoding="utf-8")
    (output_dir / "evaluation_stderr.log").write_text(result.stderr or "", encoding="utf-8")
    return {"command": command, "returncode": result.returncode, "report_path": str(output_dir / "report.md")}


def process(args: argparse.Namespace) -> dict[str, Any]:
    project_dir = Path(args.project_dir).resolve()
    snapshot_root = Path(args.snapshot_root).resolve()
    cohort_root = Path(args.cohort_root).resolve()
    records_root = cohort_root / "records"
    manifest_path = cohort_root / "manifest_latest.json"
    conn = sqlite3.connect(str(Path(args.db).resolve()))
    conn.row_factory = sqlite3.Row
    discovered = discover_provenance(snapshot_root)
    reasons: Counter[str] = Counter()
    writes: Counter[str] = Counter()
    for provenance in discovered:
        record, reason = validate_snapshot(provenance, conn)
        if reason:
            reasons[reason] += 1
            continue
        assert record is not None
        _, status = write_record(record, records_root)
        writes[status] += 1
    conn.close()

    records = load_records(records_root)
    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_model[record["model_sha256"]].append(record)
    full_walk_requirement = args.initial_train_races + args.required_folds * (args.validation_races + args.test_races)
    model_summary: dict[str, Any] = {}
    evaluations: list[dict[str, Any]] = []
    for model_sha, cohort in sorted(by_model.items()):
        ordered = sorted(cohort, key=lambda item: (item["scheduled_start_hkt"], item["race_key"]))
        count = len(ordered)
        fingerprint = cohort_fingerprint(ordered)
        status = "collecting"
        evaluation: dict[str, Any] | None = None
        if count >= args.min_unseen_races and count < full_walk_requirement:
            status = "monitoring_threshold_reached_waiting_for_full_walk"
        elif count >= full_walk_requirement:
            status = "full_walk_forward_ready"
            evaluation_marker = cohort_root / "evaluations" / model_bucket(model_sha) / "latest_successful_evaluation.json"
            existing_marker = read_json(evaluation_marker)
            already_evaluated = bool(existing_marker and existing_marker.get("cohort_fingerprint") == fingerprint)
            if args.run_evaluation and already_evaluated:
                status = "evaluation_unchanged_cohort"
            elif args.run_evaluation:
                eval_root = cohort_root / "evaluations" / model_bucket(model_sha) / datetime.now(HK_TZ).strftime("%Y%m%dT%H%M%S")
                csv_path = eval_root / "v103_unseen_cohort.csv"
                materialize_csv(ordered, csv_path)
                evaluation = run_walk_forward(project_dir, csv_path, eval_root, args)
                status = "evaluation_completed" if evaluation["returncode"] == 0 else "evaluation_failed"
                evaluations.append({"model_sha256": model_sha, "status": status, **evaluation})
                if evaluation["returncode"] == 0:
                    atomic_write_json(evaluation_marker, {
                        "model_sha256": model_sha,
                        "cohort_fingerprint": fingerprint,
                        "record_count": count,
                        "completed_at_hkt": utc_now(),
                        "evaluation": evaluation,
                    })
        model_summary[model_sha] = {
            "record_count": count,
            "cohort_fingerprint": fingerprint,
            "first_scheduled_start_hkt": ordered[0]["scheduled_start_hkt"] if ordered else None,
            "last_scheduled_start_hkt": ordered[-1]["scheduled_start_hkt"] if ordered else None,
            "monitoring_threshold": args.min_unseen_races,
            "full_walk_forward_requirement": full_walk_requirement,
            "status": status,
        }
    manifest = {
        "schema_version": "v10_3_unseen_cohort_manifest_v1",
        "generated_at_hkt": utc_now(),
        "snapshot_root": str(snapshot_root),
        "records_root": str(records_root),
        "discovered_provenance_files": len(discovered),
        "new_or_existing_record_status": dict(sorted(writes.items())),
        "rejected_snapshot_reasons": dict(sorted(reasons.items())),
        "monitoring_threshold_unseen_races": args.min_unseen_races,
        "full_walk_forward_requirement": full_walk_requirement,
        "model_cohorts": model_summary,
        "evaluations": evaluations,
        "important_note": "Cohorts are isolated by model_sha256.  Re-training creates a new cohort; records are never combined across base model artifacts.",
    }
    atomic_write_json(manifest_path, manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="收集 V10.3 貝氏校準層的不可變未見賽事 cohort，達門檻後觸發走步驗證。")
    parser.add_argument("--project-dir", default=".")
    parser.add_argument("--db", default="hkjc_last_season.sqlite")
    parser.add_argument("--snapshot-root", default="runtime/pre_race")
    parser.add_argument("--cohort-root", default="archive/v103_bayesian_cohort")
    parser.add_argument("--min-unseen-races", type=int, default=150)
    parser.add_argument("--initial-train-races", type=int, default=100)
    parser.add_argument("--validation-races", type=int, default=25)
    parser.add_argument("--test-races", type=int, default=50)
    parser.add_argument("--required-folds", type=int, default=3)
    parser.add_argument("--run-evaluation", action="store_true")
    parser.add_argument("--evaluation-timeout-seconds", type=int, default=900)
    args = parser.parse_args()
    if args.min_unseen_races < 1 or min(args.initial_train_races, args.validation_races, args.test_races, args.required_folds) < 1:
        raise SystemExit("所有 cohort 與 fold 參數必須為正整數。")
    print(json.dumps(process(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
