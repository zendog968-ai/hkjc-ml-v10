#!/usr/bin/env python3
"""Contract test for V10.3 immutable unseen-cohort collection."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from collect_v103_unseen_cohort import process


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_snapshot(root: Path, *, race_date: str, course: str, race_no: int, model_sha: str, probabilities: list[float], include_post_race_key: bool = False) -> Path:
    folder = root / f"{race_date}_{course}_R{race_no:02d}"
    prediction = folder / "prediction.json"
    rows = []
    for horse_no, probability in enumerate(probabilities, 1):
        row = {"horse_no": horse_no, "horse_name": f"馬匹{race_no}-{horse_no}", "predicted_win_probability": probability}
        if include_post_race_key:
            row["finish_pos"] = horse_no
        rows.append(row)
    write_json(prediction, {"race": {"race_date": race_date, "racecourse": course}, "predictions": rows})
    provenance = folder / "v103_snapshot_provenance.json"
    write_json(provenance, {
        "schema_version": "v10_3_prerace_snapshot_provenance_v1",
        "race_key": f"{race_date}_{course}_R{race_no:02d}",
        "race_date": race_date,
        "racecourse": course,
        "race_no": race_no,
        "scheduled_start_hkt": f"{race_date}T20:00:00+08:00",
        "prediction_generated_hkt": f"{race_date}T19:55:00+08:00",
        "stage": "T_MINUS_5",
        "source_kind": "pre_race_scheduler_t_minus_5",
        "model_path": "/fixture/horse_model.pkl",
        "model_sha256": model_sha,
        "prediction_path": str(prediction),
        "prediction_sha256": sha256(prediction),
        "post_race_labels_included": False,
    })
    return provenance


def add_official_result(conn: sqlite3.Connection, race_date: str, course: str, race_no: int) -> None:
    for horse_no in (1, 2):
        conn.execute(
            "INSERT INTO starters VALUES (?,?,?,?,?,?,?)",
            (race_date, course, race_no, horse_no, f"馬匹{race_no}-{horse_no}", 1 if horse_no == 2 else 2, str(1 if horse_no == 2 else 2)),
        )
    conn.commit()


def make_args(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        project_dir=str(root), db=str(root / "fixture.sqlite"), snapshot_root=str(root / "runtime" / "pre_race"),
        cohort_root=str(root / "archive" / "v103_bayesian_cohort"), min_unseen_races=1,
        initial_train_races=1, validation_races=1, test_races=1, required_folds=1,
        run_evaluation=False, evaluation_timeout_seconds=30,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", default="v103_unseen_cohort_contract_fixture")
    args = parser.parse_args()
    root = Path(args.output_root).resolve()
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    conn = sqlite3.connect(root / "fixture.sqlite")
    conn.execute("CREATE TABLE starters (race_date TEXT,racecourse TEXT,race_no INTEGER,horse_no INTEGER,horse_name TEXT,finish_pos INTEGER,finish_pos_text TEXT)")
    add_official_result(conn, "2026-08-01", "ST", 1)
    add_official_result(conn, "2026-08-02", "HV", 2)
    conn.close()

    snapshots = root / "runtime" / "pre_race"
    make_snapshot(snapshots, race_date="2026-08-01", course="ST", race_no=1, model_sha="a" * 64, probabilities=[0.60, 0.40])
    make_snapshot(snapshots, race_date="2026-08-02", course="HV", race_no=2, model_sha="b" * 64, probabilities=[0.51, 0.49])
    # It has an official result but embeds a forbidden post-race key; it must not enter any cohort.
    make_snapshot(snapshots, race_date="2026-08-03", course="ST", race_no=3, model_sha="a" * 64, probabilities=[0.50, 0.50], include_post_race_key=True)

    first = process(make_args(root))
    cohorts = first["model_cohorts"]
    assert len(cohorts) == 2, cohorts
    assert all(item["record_count"] == 1 for item in cohorts.values()), cohorts
    assert all(item["status"] == "monitoring_threshold_reached_waiting_for_full_walk" for item in cohorts.values()), cohorts
    assert first["rejected_snapshot_reasons"].get("prediction_rows_contain_post_race_field") == 1, first
    assert first["new_or_existing_record_status"].get("record_written") == 2, first

    second = process(make_args(root))
    assert second["new_or_existing_record_status"].get("already_recorded") == 2, second
    records = list((root / "archive" / "v103_bayesian_cohort" / "records").rglob("*.json"))
    assert len(records) == 2, records
    # actual_win is present only in the sealed cohort record after official settlement;
    # each source prediction remains separately declared post-race-free.
    assert all("actual_win" in json.loads(Path(item).read_text(encoding="utf-8"))["predictions"][0] for item in records)
    assert all(json.loads(Path(item).read_text(encoding="utf-8"))["post_race_labels_in_source"] is False for item in records)

    validation = {"status": "passed", "first": first, "second": second, "records": [str(item) for item in records]}
    write_json(root / "validation.json", validation)
    print(json.dumps(validation, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
