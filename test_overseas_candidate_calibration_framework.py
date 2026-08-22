#!/usr/bin/env python3
"""Offline contract test for overseas candidate feature/calibration scaffolding.

Uses only temporary fixture documents.  It never opens external URLs, never
starts N6, never changes V10.2, and never calls a calibration fit.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from overseas_candidate_calibration import (
    CalibrationGateError,
    audit_eligibility,
    fit_logistic_candidate_only,
    write_report,
)
from overseas_candidate_feature_interface import (
    CandidateFeatureContractError,
    attach_candidate_feature_block,
    build_optional_feature_row,
    summarize_feature_availability,
    write_feature_annex,
)
from overseas_blindtest_pipeline import DEFAULT_STUDY_ID, ensure_study, init_ledger

ROOT = Path(__file__).resolve().parent


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, payload: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    path.write_bytes(body)
    return hashlib.sha256(body).hexdigest()


def seed_valid_event(conn, root: Path, number: int) -> None:
    scheduled = datetime(2099, 1, 1, 12, 0, tzinfo=timezone.utc) + timedelta(hours=number)
    captured = scheduled - timedelta(minutes=10)
    decision = {
        "captured_at_utc": captured.isoformat(timespec="seconds"),
        "scheduled_start_utc": scheduled.isoformat(timespec="seconds"),
        "proxy_version": "fixture_public_proxy_v1",
        "n6_status": "disabled_non_hk",
        "probability_contract": {"status": "uncalibrated_research_only"},
        "runners": [
            {"runner_no": 1, "research_win_probability": 0.70},
            {"runner_no": 2, "research_win_probability": 0.30},
        ],
    }
    result = {
        "published_at_utc": (scheduled + timedelta(minutes=15)).isoformat(timespec="seconds"),
        "field_numbers": [1, 2], "finish_order": [1, 2],
    }
    decision_path = root / "decisions" / f"fixture_{number}.json"
    result_path = root / "results" / f"fixture_{number}.json"
    decision_hash = write_json(decision_path, decision)
    result_hash = write_json(result_path, result)
    event_key = f"2099-01-01:S1:{number}"
    conn.execute(
        """INSERT INTO blindtest_events(study_id,event_key,scheduled_start_utc,decision_path,decision_sha256,captured_at_utc,result_path,result_sha256,settled_at_utc,status,note)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (DEFAULT_STUDY_ID, event_key, scheduled.isoformat(timespec="seconds"), str(decision_path), decision_hash,
         captured.isoformat(timespec="seconds"), str(result_path), result_hash,
         (scheduled + timedelta(minutes=15)).isoformat(timespec="seconds"), "settled", "fixture_only"),
    )


def main() -> int:
    before = {"v10_sqlite_sha256": sha(ROOT / "hkjc_last_season.sqlite"), "v10_model_sha256": sha(ROOT / "horse_model.pkl")}
    root = Path(tempfile.mkdtemp(prefix="overseas_candidate_contract_", dir=str(ROOT / "reports/overseas_deep")))
    try:
        source = root / "pre_race_sectionals.json"
        source.write_text('{"captured_pre_race":true}\n', encoding="utf-8")
        scheduled = "2099-01-01T12:00:00Z"
        captured = "2099-01-01T11:50:00Z"
        row = build_optional_feature_row(
            runner_no=1, horse_name="Fixture Runner", scheduled_start_utc=scheduled,
            source_captured_at_utc=captured, source_path=source,
        )
        availability = summarize_feature_availability([row])
        annex = attach_candidate_feature_block(
            {"n6_status": "disabled_non_hk", "race": {"event_key": "fixture"}, "captured_at_utc": captured, "scheduled_start_utc": scheduled},
            [row],
        )
        annex_path = root / "annex.json"
        annex_hash = write_feature_annex(annex_path, annex)
        post_start_rejected = False
        try:
            build_optional_feature_row(
                runner_no=1, horse_name="Fixture Runner", scheduled_start_utc=scheduled,
                source_captured_at_utc="2099-01-01T12:00:00Z", source_path=source,
            )
        except CandidateFeatureContractError:
            post_start_rejected = True

        ledger = root / "fixture_ledger.sqlite"
        conn = init_ledger(ledger)
        ensure_study(conn, DEFAULT_STUDY_ID, 15)
        for number in range(1, 16):
            seed_valid_event(conn, root, number)
        conn.commit()
        conn.close()
        report = audit_eligibility(ledger, DEFAULT_STUDY_ID)
        report_path = root / "eligibility.json"
        write_report(report_path, report)
        training_rejected = False
        try:
            fit_logistic_candidate_only([0.70, 0.30], [1, 0], approval_path=root / "missing_approval.json", eligibility_report=report)
        except CalibrationGateError:
            training_rejected = True
        after = {"v10_sqlite_sha256": sha(ROOT / "hkjc_last_season.sqlite"), "v10_model_sha256": sha(ROOT / "horse_model.pkl")}
        result = {
            "status": "passed" if report.training_status == "candidate_report_ready_requires_independent_approval" and not report.training_permitted and post_start_rejected and training_rejected and before == after and availability.total_rows == 1 and availability.sectional_complete_rows == 0 else "failed",
            "fixture_root": str(root),
            "candidate_feature_availability": availability.__dict__,
            "post_start_source_rejected": post_start_rejected,
            "candidate_status": report.training_status,
            "training_permitted": report.training_permitted,
            "direct_training_rejected": training_rejected,
            "n6_status": report.n6_status,
            "v10_integrity_before": before,
            "v10_integrity_after": after,
            "annex_sha256": annex_hash,
        }
        write_json(root / "SIMULATION_REPORT.json", result)
        print(json.dumps({"status": result["status"], "report": str(root / "SIMULATION_REPORT.json")}, ensure_ascii=False))
        return 0 if result["status"] == "passed" else 1
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
