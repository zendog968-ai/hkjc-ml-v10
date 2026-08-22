#!/usr/bin/env python3
"""Offline validation harness for P1 candidate-only archives and ingestion."""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path


def query_one(connection: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> tuple[object, ...]:
    row = connection.execute(sql, params).fetchone()
    if row is None:
        raise AssertionError(f"expected row not found: {sql}")
    return row


def table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    tables = [
        "p1_ingest_runs", "source_manifests", "pp_identity_map", "pp_external_form",
        "trial_batches", "trial_entries", "candidate_feature_snapshots", "ingest_rejections",
    ]
    return {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}


def run_ingest(project_root: Path, manifest: Path, schema: Path, db: Path, report: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(project_root / "preseason_candidate/p1_ingest.py"),
         "--manifest", str(manifest), "--schema", str(schema), "--db", str(db),
         "--report", str(report), "--project-root", str(project_root)],
        text=True, capture_output=True, check=False,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    project_root = Path(args.project_root).resolve()
    manifest = Path(args.manifest).resolve()
    schema = Path(args.schema).resolve()
    db = Path(args.db).resolve()
    report = Path(args.report).resolve()

    if not db.exists():
        first = run_ingest(project_root, manifest, schema, db, report)
        if first.returncode != 0:
            raise AssertionError(f"initial P1 ingest failed: {first.stderr}")
    with sqlite3.connect(db) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        initial_counts = table_counts(connection)
        integrity = query_one(connection, "PRAGMA integrity_check")[0]
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        trial_counts = connection.execute(
            "SELECT batch_label, COUNT(*) FROM trial_batches b JOIN trial_entries e ON b.trial_batch_id=e.trial_batch_id GROUP BY batch_label ORDER BY batch_label"
        ).fetchall()
        alpha = query_one(
            connection,
            "SELECT i.hk_horse_name, i.original_horse_name, i.source_country, f.form_date, f.race_distance_m, f.finishing_position, f.field_size "
            "FROM pp_identity_map i JOIN pp_external_form f ON f.pp_identity_id=i.pp_identity_id WHERE i.hk_horse_code='L019'",
        )
        cala = query_one(
            connection,
            "SELECT b.batch_label, b.venue, b.surface, b.distance_m, b.going, b.batch_time_seconds, e.horse_name, e.draw, e.finish_rank, e.time_vs_batch_seconds, e.finish_rank_pct "
            "FROM trial_entries e JOIN trial_batches b ON b.trial_batch_id=e.trial_batch_id WHERE e.horse_code='L428'",
        )
        try:
            connection.execute("UPDATE source_manifests SET source_url='forbidden' LIMIT 1")
            raise AssertionError("source manifest immutability trigger did not reject UPDATE")
        except sqlite3.DatabaseError as error:
            immutability_rejection = str(error)

    second = run_ingest(project_root, manifest, schema, db, report)
    if second.returncode != 0:
        raise AssertionError(f"idempotent second P1 ingest failed: {second.stderr}")
    with sqlite3.connect(db) as connection:
        after_counts = table_counts(connection)
    if initial_counts != after_counts:
        raise AssertionError(f"second run changed immutable record counts: {initial_counts} != {after_counts}")

    invalid_manifest = json.loads(manifest.read_text(encoding="utf-8"))
    invalid_manifest["as_of_utc"] = invalid_manifest["sources"][0]["retrieved_at_utc"]
    invalid_path = report.parent / "p1_invalid_timestamp_manifest.json"
    invalid_db = report.parent / "p1_invalid_timestamp_should_not_exist.sqlite"
    invalid_report = report.parent / "p1_invalid_timestamp_should_not_exist.json"
    for path in (invalid_db, invalid_report):
        path.unlink(missing_ok=True)
    invalid_path.write_text(json.dumps(invalid_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    invalid = run_ingest(project_root, invalid_path, schema, invalid_db, invalid_report)
    invalid_path.unlink(missing_ok=True)
    if invalid.returncode == 0 or invalid_db.exists() or invalid_report.exists():
        raise AssertionError("strict pre-as-of rejection did not fail before database/report creation")

    source_code = (project_root / "preseason_candidate/p1_ingest.py").read_text(encoding="utf-8")
    prohibited_tokens = [
        "import requests", "from requests", "import urllib.request", "from urllib",
        "hkjc_last" + "_season.sqlite", "n6" + "_engine",
    ]
    prohibited_hits = [token for token in prohibited_tokens if token in source_code]
    if prohibited_hits:
        raise AssertionError(f"candidate parser contains prohibited production/network reference(s): {prohibited_hits}")

    result = {
        "status": "pass",
        "validation": {
            "trial_batch_entry_counts": [{"batch_label": row[0], "entries": row[1]} for row in trial_counts],
            "pp_alpha_strike": {
                "hk_horse_name": alpha[0], "original_horse_name": alpha[1], "source_country": alpha[2],
                "form_date": alpha[3], "race_distance_m": alpha[4], "finishing_position": alpha[5], "field_size": alpha[6],
            },
            "trial_cala_dei_mori": {
                "batch_label": cala[0], "venue": cala[1], "surface": cala[2], "distance_m": cala[3], "going": cala[4],
                "batch_time_seconds": cala[5], "horse_name": cala[6], "draw": cala[7], "finish_rank": cala[8],
                "time_vs_batch_seconds": cala[9], "finish_rank_pct": cala[10],
            },
            "initial_counts": initial_counts,
            "second_run_counts": after_counts,
            "idempotent_second_run": True,
            "strict_pre_as_of_rejection_before_db_creation": True,
            "sqlite_integrity_check": integrity,
            "foreign_key_errors": len(foreign_key_errors),
            "immutability_update_rejected": immutability_rejection,
            "production_access": {"v10_sqlite_opened": False, "n6_imported": False, "network_requests": 0},
        },
    }
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "trial_batches": len(trial_counts), "idempotent": True}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
