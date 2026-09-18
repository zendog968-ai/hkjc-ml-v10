#!/usr/bin/env python3
"""Run a V10 monthly update in a candidate workspace and atomically promote only validated artefacts.

The wrapper never changes N6 configuration or invokes N6.  Any fetch, parser, training, or
quality-gate failure leaves the five formal artefacts untouched and retains a private log.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime"
ARTEFACTS = {
    "database": "hkjc_last_season.sqlite",
    "csv": "hkjc_last_season.csv",
    "model": "horse_model.pkl",
    "training_report": "lightgbm_training_report.json",
    "quality_report": "v101_quality_report.json",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sqlite_backup(source: Path, destination: Path) -> None:
    source_connection = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
        destination_connection.execute("PRAGMA foreign_keys=ON")
        integrity = destination_connection.execute("PRAGMA integrity_check").fetchone()[0]
        violations = destination_connection.execute("PRAGMA foreign_key_check").fetchall()
        if integrity != "ok" or violations:
            raise RuntimeError(f"candidate database backup validation failed: integrity={integrity}, fk={len(violations)}")
    finally:
        destination_connection.close()
        source_connection.close()


def database_stats(path: Path) -> dict[str, object]:
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        return {
            "last_race_date": connection.execute("SELECT MAX(race_date) FROM races").fetchone()[0],
            "races": int(connection.execute("SELECT COUNT(*) FROM races").fetchone()[0]),
            "starters": int(connection.execute("SELECT COUNT(*) FROM starters").fetchone()[0]),
            "feature_rows": int(connection.execute("SELECT COUNT(*) FROM elo_feature_store").fetchone()[0]),
            "integrity_check": connection.execute("PRAGMA integrity_check").fetchone()[0],
            "foreign_key_violations": len(connection.execute("PRAGMA foreign_key_check").fetchall()),
        }
    finally:
        connection.close()


def copy_with_fsync(source: Path, destination: Path) -> None:
    with source.open("rb") as reader, destination.open("wb") as writer:
        shutil.copyfileobj(reader, writer, length=1024 * 1024)
        writer.flush()
        os.fsync(writer.fileno())
    shutil.copystat(source, destination, follow_symlinks=True)


def promote_atomically(staging: Path, backup_dir: Path) -> None:
    backup_dir.mkdir(parents=True, exist_ok=False)
    for filename in ARTEFACTS.values():
        source = ROOT / filename
        if not source.exists():
            raise RuntimeError(f"formal artefact missing before promotion: {source}")
        copy_with_fsync(source, backup_dir / filename)
    for filename in ARTEFACTS.values():
        staged = staging / filename
        formal = ROOT / filename
        if not staged.exists():
            raise RuntimeError(f"validated staged artefact missing: {staged}")
        pending = ROOT / f".{filename}.monthly-update-pending"
        copy_with_fsync(staged, pending)
        os.replace(pending, formal)
    directory_fd = os.open(ROOT, os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--end-date", required=True, help="Inclusive official fixture/results cutoff (YYYY-MM-DD)")
    parser.add_argument("--skip-fetch", action="store_true", help="Do not issue any official fixture/result requests; use only after an audited incremental fetch finds no new local race day.")
    parser.add_argument("--skip-equipment", action="store_true", help="Do not issue any official equipment-form requests; use only after an audited incremental fetch finds no new local race day.")
    parser.add_argument("--promote", action="store_true", help="Atomically promote only after all candidate checks pass.")
    args = parser.parse_args()

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    staging = RUNTIME / "monthly_update_staging" / run_id
    backup_dir = RUNTIME / "monthly_update_rollbacks" / run_id
    staging.mkdir(parents=True, mode=0o700)
    os.chmod(staging, 0o700)
    log_path = staging / "monthly_update.log"
    original_db = ROOT / ARTEFACTS["database"]
    original_csv = ROOT / ARTEFACTS["csv"]
    sqlite_backup(original_db, staging / ARTEFACTS["database"])
    copy_with_fsync(original_csv, staging / ARTEFACTS["csv"])

    command = [
        sys.executable, str(ROOT / "monthly_update.py"),
        "--db", str(staging / ARTEFACTS["database"]),
        "--csv", str(staging / ARTEFACTS["csv"]),
        "--model", str(staging / ARTEFACTS["model"]),
        "--training-report", str(staging / ARTEFACTS["training_report"]),
        "--predictions", str(staging / "lightgbm_backtest_predictions.csv"),
        "--elo-report", str(staging / "elo_feature_report.json"),
        "--quality-report", str(staging / ARTEFACTS["quality_report"]),
        "--end-date", args.end_date,
    ]
    if args.skip_fetch:
        command.append("--skip-fetch")
    if args.skip_equipment:
        command.append("--skip-equipment")
    before = database_stats(original_db)
    with log_path.open("w", encoding="utf-8") as log:
        log.write("command=" + " ".join(command) + "\n")
        log.flush()
        result = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, text=True)
    if result.returncode != 0:
        summary = {
            "status": "candidate_failed_not_promoted",
            "run_id": run_id,
            "returncode": result.returncode,
            "staging": str(staging),
            "log": str(log_path),
            "before": before,
        }
        (staging / "transaction_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return result.returncode

    staged_db = staging / ARTEFACTS["database"]
    after = database_stats(staged_db)
    quality = json.loads((staging / ARTEFACTS["quality_report"]).read_text(encoding="utf-8"))
    checks = {
        "database_integrity": after["integrity_check"] == "ok",
        "foreign_keys": after["foreign_key_violations"] == 0,
        "quality_report_passed": quality.get("all_checks_passed") is True,
        "all_expected_artefacts": all((staging / filename).is_file() for filename in ARTEFACTS.values()),
    }
    if not all(checks.values()):
        summary = {"status": "candidate_validation_failed_not_promoted", "run_id": run_id, "staging": str(staging), "log": str(log_path), "before": before, "after": after, "checks": checks}
        (staging / "transaction_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 1

    promoted = False
    if args.promote:
        promote_atomically(staging, backup_dir)
        promoted = True
    summary = {
        "status": "promoted" if promoted else "candidate_validated_not_promoted",
        "run_id": run_id,
        "staging": str(staging),
        "rollback": str(backup_dir) if promoted else None,
        "log": str(log_path),
        "before": before,
        "after": after,
        "delta": {key: after[key] - before[key] for key in ("races", "starters", "feature_rows")},
        "checks": checks,
        "artefact_sha256": {filename: sha256((ROOT if promoted else staging) / filename) for filename in ARTEFACTS.values()},
    }
    (staging / "transaction_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(staging / "transaction_summary.json", 0o600)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
