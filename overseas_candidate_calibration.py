#!/usr/bin/env python3
"""Candidate-only calibration gate for overseas public research probabilities.

This module cannot alter N6, V10.2, a sealed prediction, or a live overseas
report.  By default it only audits eligibility and writes a report.  A future
offline calibration experiment requires both a 15-event valid cohort and a
separate approval file; no scheduler is allowed to supply that approval.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CALIBRATION_VERSION = "overseas_candidate_logistic_brier_v1"
REQUIRED_EVENTS = 15
REQUIRED_N6_STATUS = "disabled_non_hk"
APPROVAL_SCHEMA = "overseas_candidate_calibration_approval_v1"


class CalibrationGateError(ValueError):
    """Raised whenever a candidate cohort cannot safely be calibrated."""


def sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise CalibrationGateError("timestamp lacks an explicit timezone")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class EventAudit:
    event_key: str
    valid: bool
    reasons: list[str]
    captured_at_utc: str | None
    scheduled_start_utc: str | None
    proxy_version: str | None
    decision_sha256: str | None
    result_sha256: str | None
    runner_count: int


@dataclass(frozen=True)
class EligibilityReport:
    calibration_version: str
    study_id: str
    checked_at_utc: str
    training_status: str
    training_permitted: bool
    required_events: int
    settled_events: int
    valid_events: int
    invalid_events: int
    proxy_versions: list[str]
    n6_status: str
    event_audits: list[dict[str, Any]]
    reasons: list[str]
    v10_2_action: str
    n6_action: str


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CalibrationGateError(f"cannot read immutable artifact {path}") from exc
    if not isinstance(value, dict):
        raise CalibrationGateError(f"artifact is not a JSON object: {path}")
    return value


def _audit_event(row: sqlite3.Row) -> EventAudit:
    reasons: list[str] = []
    decision_path = Path(str(row["decision_path"] or ""))
    result_path = Path(str(row["result_path"] or ""))
    captured_at: str | None = None
    scheduled: str | None = None
    proxy: str | None = None
    runners: list[dict[str, Any]] = []
    try:
        decision = _read_json(decision_path)
        result = _read_json(result_path)
    except CalibrationGateError as exc:
        return EventAudit(str(row["event_key"]), False, [str(exc)], None, None, None, row["decision_sha256"], row["result_sha256"], 0)
    if sha256_path(decision_path) != row["decision_sha256"]:
        reasons.append("decision_sha256_mismatch")
    if sha256_path(result_path) != row["result_sha256"]:
        reasons.append("result_sha256_mismatch")
    captured_at = decision.get("captured_at_utc")
    scheduled = decision.get("scheduled_start_utc")
    proxy = decision.get("proxy_version")
    if decision.get("n6_status") != REQUIRED_N6_STATUS:
        reasons.append("overseas_n6_not_disabled")
    if decision.get("probability_contract", {}).get("status") != "uncalibrated_research_only":
        reasons.append("unexpected_probability_contract")
    try:
        if parse_utc(str(captured_at)) >= parse_utc(str(scheduled)):
            reasons.append("decision_not_strictly_prerace")
        if parse_utc(str(result.get("published_at_utc"))) < parse_utc(str(scheduled)):
            reasons.append("result_published_before_scheduled_start")
    except (CalibrationGateError, TypeError, ValueError):
        reasons.append("invalid_time_ordering")
    raw_runners = decision.get("runners")
    if not isinstance(raw_runners, list) or len(raw_runners) < 2:
        reasons.append("missing_decision_runners")
    else:
        runners = [item for item in raw_runners if isinstance(item, dict)]
        numbers = [item.get("runner_no") for item in runners]
        if len(numbers) != len(set(numbers)) or not all(isinstance(value, int) for value in numbers):
            reasons.append("invalid_decision_runner_numbers")
        try:
            probability_sum = sum(float(item["research_win_probability"]) for item in runners)
            if not math.isclose(probability_sum, 1.0, abs_tol=1e-9):
                reasons.append("win_probability_not_conserved")
        except (KeyError, TypeError, ValueError):
            reasons.append("missing_research_win_probability")
        official_field = set(result.get("field_numbers", []))
        if official_field != set(numbers):
            reasons.append("official_field_mismatch")
        finish = result.get("finish_order")
        if not isinstance(finish, list) or not finish or any(number not in official_field for number in finish):
            reasons.append("invalid_official_finish_order")
    return EventAudit(
        str(row["event_key"]), not reasons, reasons, str(captured_at) if captured_at else None,
        str(scheduled) if scheduled else None, str(proxy) if proxy else None,
        str(row["decision_sha256"]), str(row["result_sha256"]), len(runners),
    )


def audit_eligibility(ledger_path: Path, study_id: str) -> EligibilityReport:
    """Audit only settled, immutable, time-ordered events; never trains."""
    if not ledger_path.is_file():
        return EligibilityReport(CALIBRATION_VERSION, study_id, datetime.now(timezone.utc).isoformat(timespec="seconds"), "denied_missing_ledger", False, REQUIRED_EVENTS, 0, 0, 0, [], REQUIRED_N6_STATUS, [], ["blindtest_ledger_missing"], "not_touched", "disabled_non_hk_unchanged")
    conn = sqlite3.connect(ledger_path)
    conn.row_factory = sqlite3.Row
    try:
        study = conn.execute("SELECT study_id,max_events,pipeline_version,proxy_version FROM blindtest_studies WHERE study_id=?", (study_id,)).fetchone()
        if study is None:
            return EligibilityReport(CALIBRATION_VERSION, study_id, datetime.now(timezone.utc).isoformat(timespec="seconds"), "denied_missing_study", False, REQUIRED_EVENTS, 0, 0, 0, [], REQUIRED_N6_STATUS, [], ["registered_15_event_study_missing"], "not_touched", "disabled_non_hk_unchanged")
        rows = conn.execute("SELECT * FROM blindtest_events WHERE study_id=? AND status='settled' ORDER BY scheduled_start_utc,event_key", (study_id,)).fetchall()
    finally:
        conn.close()
    audits = [_audit_event(row) for row in rows]
    valid = [item for item in audits if item.valid]
    reasons: list[str] = []
    if len(rows) < REQUIRED_EVENTS:
        reasons.append(f"requires_{REQUIRED_EVENTS}_settled_events_has_{len(rows)}")
    if len(valid) != REQUIRED_EVENTS:
        reasons.append(f"requires_{REQUIRED_EVENTS}_fully_valid_events_has_{len(valid)}")
    versions = sorted({item.proxy_version for item in valid if item.proxy_version})
    if len(versions) != 1:
        reasons.append("requires_exactly_one_proxy_version")
    captured = [parse_utc(str(item.captured_at_utc)) for item in valid if item.captured_at_utc]
    scheduled = [parse_utc(str(item.scheduled_start_utc)) for item in valid if item.scheduled_start_utc]
    if len(captured) != len(valid) or any(left >= right for left, right in zip(captured, scheduled)):
        reasons.append("pre_race_ordering_gate_failed")
    if any(left >= right for left, right in zip(scheduled, scheduled[1:])):
        reasons.append("scheduled_events_not_strictly_time_ordered")
    status = "candidate_report_ready_requires_independent_approval" if not reasons else "denied_by_default"
    return EligibilityReport(
        CALIBRATION_VERSION, study_id, datetime.now(timezone.utc).isoformat(timespec="seconds"), status,
        False, REQUIRED_EVENTS, len(rows), len(valid), len(rows) - len(valid), versions, REQUIRED_N6_STATUS,
        [asdict(item) for item in audits], reasons, "not_touched", "disabled_non_hk_unchanged",
    )


def write_report(path: Path, report: EligibilityReport) -> str:
    """Safely replace only a mutable status report, never a decision or result."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(report)
    payload["report_sha256_excluding_self"] = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return str(path)


def require_independent_approval(approval_path: Path, report: EligibilityReport) -> dict[str, Any]:
    """Future offline fitting needs human approval; timers must never create it."""
    if report.training_status != "candidate_report_ready_requires_independent_approval":
        raise CalibrationGateError("candidate cohort has not passed the 15-event eligibility gate")
    approval = _read_json(approval_path)
    if approval.get("schema_version") != APPROVAL_SCHEMA:
        raise CalibrationGateError("approval schema is invalid")
    if approval.get("calibration_version") != CALIBRATION_VERSION:
        raise CalibrationGateError("approval calibration version does not match")
    if approval.get("study_id") != report.study_id or approval.get("approval_scope") != "offline_candidate_only":
        raise CalibrationGateError("approval scope is invalid")
    if approval.get("n6_status") != REQUIRED_N6_STATUS or approval.get("v10_2_action") != "not_touched":
        raise CalibrationGateError("approval cannot change N6 or V10.2")
    return approval


def fit_logistic_candidate_only(
    base_probabilities: list[float],
    outcomes: list[int],
    *,
    approval_path: Path,
    eligibility_report: EligibilityReport,
) -> dict[str, Any]:
    """Fit a *manually approved* offline logistic calibration experiment.

    This function is never called by a timer.  It accepts only the sealed
    pre-race probability vector and official binary outcomes from a cohort that
    already passed the 15-event eligibility audit.  It returns coefficients and
    before/after Brier values in memory; it neither writes a model artifact nor
    changes N6, V10.2, ranking, EV, or Kelly.
    """
    require_independent_approval(approval_path, eligibility_report)
    if len(base_probabilities) != len(outcomes) or not base_probabilities:
        raise CalibrationGateError("probabilities and outcomes must be non-empty and equal length")
    if any(not (0.0 < float(probability) < 1.0) for probability in base_probabilities):
        raise CalibrationGateError("base probabilities must be strictly between zero and one")
    if any(int(outcome) not in {0, 1} for outcome in outcomes):
        raise CalibrationGateError("outcomes must be binary official labels")
    if len(set(int(outcome) for outcome in outcomes)) != 2:
        raise CalibrationGateError("official outcomes must contain both classes for logistic calibration")
    try:
        import numpy as np
        from sklearn.linear_model import LogisticRegression
    except ImportError as exc:
        raise CalibrationGateError("scikit-learn is required only for a manually approved offline fit") from exc
    probabilities = np.asarray(base_probabilities, dtype=float)
    labels = np.asarray(outcomes, dtype=int)
    logits = np.log(probabilities / (1.0 - probabilities)).reshape(-1, 1)
    model = LogisticRegression(C=1.0, solver="lbfgs", max_iter=1000, random_state=0)
    model.fit(logits, labels)
    calibrated = model.predict_proba(logits)[:, 1]
    brier_before = float(np.mean((probabilities - labels) ** 2))
    brier_after = float(np.mean((calibrated - labels) ** 2))
    return {
        "status": "offline_candidate_fit_only",
        "calibration_version": CALIBRATION_VERSION,
        "study_id": eligibility_report.study_id,
        "n6_status": REQUIRED_N6_STATUS,
        "v10_2_action": "not_touched",
        "model_artifact_written": False,
        "feature": "logit_of_sealed_base_probability",
        "intercept": float(model.intercept_[0]),
        "coefficient": float(model.coef_[0][0]),
        "brier_before_in_sample": brier_before,
        "brier_after_in_sample": brier_after,
        "warning": "In-sample values are not an activation criterion; require independent time-ordered out-of-sample evaluation.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="海外候選校準資格審計（預設拒絕訓練）。")
    parser.add_argument("--ledger", default="runtime/overseas_blindtest/overseas_blindtest.sqlite")
    parser.add_argument("--study-id", default="overseas_rpr_ts_exploratory_15_v1")
    parser.add_argument("--report", default="runtime/overseas_candidate_calibration/eligibility_status.json")
    parser.add_argument("--mode", choices=["audit", "train"], default="audit")
    parser.add_argument("--approval")
    args = parser.parse_args()
    report = audit_eligibility(Path(args.ledger), args.study_id)
    report_path = write_report(Path(args.report), report)
    if args.mode == "train":
        if not args.approval:
            raise SystemExit("training is denied: --approval is mandatory and no scheduler may create it")
        require_independent_approval(Path(args.approval), report)
        raise SystemExit("training remains offline-only: invoke fit_logistic_candidate_only() from a separately approved time-ordered evaluation script")
    print(json.dumps({"training_status": report.training_status, "valid_events": report.valid_events, "required_events": report.required_events, "report": report_path, "n6_status": report.n6_status}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
