#!/usr/bin/env python3
"""Candidate-only HKJC T-15/T-5 snapshot scheduler.

The scheduler runs only after an independently reviewed, local schedule file exists.
It archives raw public pages, enforces a 60-second cross-stage request interval, and
stops the entire meeting on an official 403/429 response.  It never calls predict.py,
loads V10 model/database artifacts, imports N6, or changes any formal prediction.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

HK_TZ = ZoneInfo("Asia/Hong_Kong")
VALID_OFFSETS = (15, 5)
MIN_INTERVAL_SECONDS = 60


@dataclass(frozen=True)
class RaceJob:
    race_date: str
    racecourse: str
    race_no: int
    start_at: datetime

    @property
    def key(self) -> str:
        return f"{self.race_date.replace('/', '-')}_{self.racecourse}_R{self.race_no:02d}"


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = handle.name
    os.replace(temporary, path)


def read_json(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else fallback
    except (OSError, json.JSONDecodeError):
        return fallback


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iso_hkt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return (parsed.replace(tzinfo=HK_TZ) if parsed.tzinfo is None else parsed).astimezone(HK_TZ)


def parse_start(race_date: str, time_text: str) -> datetime:
    return datetime.strptime(f"{race_date.replace('/', '-')} {time_text}", "%Y-%m-%d %H:%M").replace(tzinfo=HK_TZ)


def require_fixture(config: dict[str, Any]) -> None:
    fixture = config.get("fixture_source")
    if not isinstance(fixture, dict):
        raise ValueError("missing fixture_source")
    for field in ("official_url", "retrieved_at_utc", "raw_html_path", "sha256"):
        if not str(fixture.get(field) or "").strip():
            raise ValueError(f"fixture_source missing {field}")
    raw = Path(str(fixture["raw_html_path"]))
    if not raw.is_file() or sha256_file(raw) != str(fixture["sha256"]):
        raise ValueError("fixture source raw HTML is missing or SHA-256 does not match")
    captured = datetime.fromisoformat(str(fixture["retrieved_at_utc"]).replace("Z", "+00:00"))
    if captured.tzinfo is None:
        raise ValueError("fixture_source.retrieved_at_utc must be timezone-aware")


def load_jobs(config_path: Path) -> tuple[dict[str, Any], list[RaceJob]]:
    config = read_json(config_path, {})
    if config.get("schema_version") != "v1_candidate_prerace_snapshot_schedule":
        raise ValueError("unsupported or missing candidate schedule schema_version")
    if config.get("enabled") is not True:
        raise ValueError("candidate schedule is not explicitly enabled")
    approval = config.get("approval")
    if not isinstance(approval, dict) or not str(approval.get("approved_by") or "").strip() or not str(approval.get("approved_at_utc") or "").strip():
        raise ValueError("missing explicit approval")
    require_fixture(config)
    meeting = config.get("meeting")
    if not isinstance(meeting, dict):
        raise ValueError("missing meeting")
    race_date = datetime.strptime(str(meeting.get("race_date") or "").replace("/", "-"), "%Y-%m-%d").strftime("%Y/%m/%d")
    course = str(meeting.get("racecourse") or "").upper()
    if course not in {"ST", "HV"}:
        raise ValueError("meeting.racecourse must be ST or HV")
    starts = meeting.get("race_start_times")
    if not isinstance(starts, dict) or not starts:
        raise ValueError("meeting.race_start_times must be a nonempty map")
    jobs = [RaceJob(race_date, course, int(no), parse_start(race_date, str(value).strip())) for no, value in starts.items()]
    if any(job.race_no < 1 for job in jobs):
        raise ValueError("race numbers must be positive")
    fixture_captured = datetime.fromisoformat(str(config["fixture_source"]["retrieved_at_utc"]).replace("Z", "+00:00")).astimezone(timezone.utc)
    earliest_start = min(job.start_at.astimezone(timezone.utc) for job in jobs)
    if fixture_captured >= earliest_start:
        raise ValueError("fixture_source must be captured before the earliest scheduled race start")
    preload = int(config.get("racecard_capture_minutes_before", 20))
    if preload <= VALID_OFFSETS[0] or preload > 120:
        raise ValueError("racecard_capture_minutes_before must be between 16 and 120")
    if int(config.get("minimum_request_interval_seconds", MIN_INTERVAL_SECONDS)) < MIN_INTERVAL_SECONDS:
        raise ValueError("minimum_request_interval_seconds cannot be below 60")
    return config, sorted(jobs, key=lambda job: job.race_no)


def due_stages(config: dict[str, Any], jobs: list[RaceJob], now: datetime) -> list[tuple[RaceJob, str, int]]:
    current = now.replace(second=0, microsecond=0)
    preload = int(config.get("racecard_capture_minutes_before", 20))
    candidates: list[tuple[RaceJob, str, int]] = []
    for job in jobs:
        stages = [("RACECARD", preload), ("T_MINUS_15", 15), ("T_MINUS_5", 5)]
        for stage, offset in stages:
            target = (job.start_at - timedelta(minutes=offset)).replace(second=0, microsecond=0)
            if target <= current < target + timedelta(minutes=1):
                candidates.append((job, stage, offset))
    return candidates


def command_result(command: list[str], cwd: Path, log_path: Path, timeout_seconds: int) -> dict[str, Any]:
    completed = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout_seconds, check=False)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "$ " + " ".join(command) + "\n\n--- stdout ---\n" + (completed.stdout or "") + "\n--- stderr ---\n" + (completed.stderr or ""),
        encoding="utf-8",
    )
    return {"returncode": completed.returncode, "log": str(log_path), "stdout": completed.stdout or "", "stderr": completed.stderr or ""}


def request_guard(state: dict[str, Any], now_epoch: float, minimum: int) -> None:
    last = float((state.get("request_guard") or {}).get("last_request_epoch", 0) or 0)
    elapsed = now_epoch - last
    if elapsed < minimum:
        raise RuntimeError(f"global HKJC request interval has only elapsed {elapsed:.1f}s; requires {minimum}s")


def mark_request(state: dict[str, Any], now_epoch: float, source: str) -> None:
    state["request_guard"] = {"last_request_epoch": now_epoch, "source": source}


def official_limit_detected(result: dict[str, Any]) -> int | None:
    text = (str(result.get("stdout") or "") + "\n" + str(result.get("stderr") or "")).lower()
    return 403 if "403" in text else 429 if "429" in text else None


def candidate_paths(root: Path, job: RaceJob) -> dict[str, Path]:
    base = root / job.key
    return {
        "base": base,
        "racecard": base / "race_card.json",
        "racecard_raw": base / "raw" / "race_card.html",
        "odds_raw_t15": base / "raw" / "odds_t_minus_15.html",
        "odds_raw_t5": base / "raw" / "odds_t_minus_5.html",
        "snapshot_t15": base / "snapshots" / "odds_t_minus_15.json",
        "snapshot_t5": base / "snapshots" / "odds_t_minus_5.json",
        "manifest": base / "candidate_source_manifest.json",
        "odds_meta": base / "odds_overlay.meta.json",
    }


def append_source_manifest(paths: dict[str, Path], job: RaceJob, stage: str, now: datetime) -> None:
    payload = read_json(paths["manifest"], {"schema_version": "v1", "race": {"race_date": job.race_date.replace("/", "-"), "racecourse": job.racecourse, "race_no": job.race_no}, "events": []})
    events = payload.setdefault("events", [])
    if stage == "RACECARD":
        card = read_json(paths["racecard"], {})
        source = card.get("source") if isinstance(card.get("source"), dict) else {}
        if not paths["racecard_raw"].is_file() or source.get("raw_html_sha256") != sha256_file(paths["racecard_raw"]):
            raise ValueError("racecard archive evidence failed SHA-256 verification")
        events.append({"stage": stage, "captured_at_utc": now.astimezone(timezone.utc).isoformat(timespec="seconds"), "source": source})
    else:
        snapshot = read_json(paths["snapshot_t15"] if stage == "T_MINUS_15" else paths["snapshot_t5"], {})
        raw = paths["odds_raw_t15"] if stage == "T_MINUS_15" else paths["odds_raw_t5"]
        archive = snapshot.get("raw_html_archive") if isinstance(snapshot.get("raw_html_archive"), dict) else {}
        if snapshot.get("status") != "complete" or not raw.is_file() or archive.get("sha256") != sha256_file(raw):
            raise ValueError("odds snapshot is incomplete or raw HTML archive verification failed")
        events.append({"stage": stage, "captured_at_utc": now.astimezone(timezone.utc).isoformat(timespec="seconds"), "source_url": snapshot.get("source_url"), "raw_html_archive": archive, "snapshot_sha256": sha256_file(paths["snapshot_t15"] if stage == "T_MINUS_15" else paths["snapshot_t5"])})
    payload["updated_at_utc"] = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    atomic_write(paths["manifest"], payload)


def import_snapshots(project: Path, root: Path, report_path: Path) -> dict[str, Any]:
    archive = root / "pre_race_odds_snapshots.sqlite"
    command = [sys.executable, str(project / "import_prerace_odds_snapshots.py"), "--db", str(archive), "--schema", str(project / "schema_prerace_odds_snapshots.sql"), "--input-root", str(root), "--report", str(report_path)]
    return command_result(command, project, report_path.with_suffix(".log"), 90)


def send_limit_alert(code: int, project: Path) -> dict[str, Any]:
    command = ["sudo", "/usr/local/sbin/hkjc-v10-smtp-dispatch", "--candidate-snapshot-stop"]
    return command_result(command, project, project / "runtime" / "pre_race_snapshot_candidate" / "candidate_alert_dispatch.log", 30) | {"http_status": code}


def execute_stage(config: dict[str, Any], job: RaceJob, stage: str, offset: int, state: dict[str, Any], project: Path, root: Path, now: datetime, dry_run: bool) -> dict[str, Any]:
    paths = candidate_paths(root, job)
    paths["base"].mkdir(parents=True, exist_ok=True)
    if dry_run:
        return {"status": "dry_run_due", "stage": stage, "offset_minutes": offset, "output_dir": str(paths["base"])}
    minimum = int(config.get("minimum_request_interval_seconds", MIN_INTERVAL_SECONDS))
    request_guard(state, time.time(), minimum)
    if stage == "RACECARD":
        mark_request(state, time.time(), "official_racecard")
        command = [sys.executable, str(project / "fetch_hkjc_racecard.py"), "--date", job.race_date, "--racecourse", job.racecourse, "--race-no", str(job.race_no), "--output", str(paths["racecard"]), "--raw-html-output", str(paths["racecard_raw"])]
        result = command_result(command, project, paths["base"] / "racecard.log", 90)
    else:
        if not paths["racecard"].is_file():
            raise ValueError("racecard evidence is absent; odds capture refused")
        snapshot = paths["snapshot_t15"] if stage == "T_MINUS_15" else paths["snapshot_t5"]
        raw_html = paths["odds_raw_t15"] if stage == "T_MINUS_15" else paths["odds_raw_t5"]
        mark_request(state, time.time(), "official_odds")
        command = [sys.executable, str(project / "fetch_hkjc_live_odds.py"), "--race-card", str(paths["racecard"]), "--output", str(paths["base"] / "odds_overlay.json"), "--place-output", str(paths["base"] / "place_odds_overlay.json"), "--combined-output", str(paths["base"] / "odds_overlay_combined.json"), "--metadata-output", str(paths["odds_meta"]), "--raw-html-output", str(raw_html), "--snapshot-output", str(snapshot), "--snapshot-label", stage, "--race-date", job.race_date, "--racecourse", job.racecourse, "--race-no", str(job.race_no), "--min-interval", str(minimum), "--state-file", str(root / "candidate_request_state.json")]
        result = command_result(command, project, paths["base"] / f"{stage.lower()}.log", 120)
    code = official_limit_detected(result)
    if code is not None:
        state["stopped"] = {"status": "stopped_official_limit", "http_status": code, "at_utc": now.astimezone(timezone.utc).isoformat(timespec="seconds"), "job": job.key, "stage": stage, "alert": {"status": "pending_persisted_dispatch"}}
        return {"status": "stopped_official_limit", "http_status": code, "stage": stage, "output_dir": str(paths["base"]), "step": {key: value for key, value in result.items() if key not in {"stdout", "stderr"}}}
    if result["returncode"] != 0:
        return {"status": "failed", "stage": stage, "output_dir": str(paths["base"]), "step": {key: value for key, value in result.items() if key not in {"stdout", "stderr"}}}
    try:
        append_source_manifest(paths, job, stage, now)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"status": "rejected_provenance", "stage": stage, "output_dir": str(paths["base"]), "reason": f"{type(exc).__name__}: {exc}"}
    import_result = None
    if stage in {"T_MINUS_15", "T_MINUS_5"}:
        import_result = import_snapshots(project, root, root / "import_report.json")
    return {"status": "captured", "stage": stage, "offset_minutes": offset, "output_dir": str(paths["base"]), "import": {key: value for key, value in (import_result or {}).items() if key not in {"stdout", "stderr"}}}


def process(config_path: Path, project: Path, root: Path, state_path: Path, report_path: Path, now: datetime, dry_run: bool) -> dict[str, Any]:
    config, jobs = load_jobs(config_path)
    state = read_json(state_path, {"runs": {}})
    result: dict[str, Any] = {"checked_at_hkt": now.isoformat(), "configured_jobs": len(jobs), "dry_run": dry_run, "processed": [], "network_requests": 0, "production_contract": {"v10_probability_ev_kelly_modified": False, "n6_imported": False, "automatic_training_enabled": False}}
    if state.get("stopped"):
        result["status"] = "stopped_requires_manual_review"
        result["stop_detail"] = state["stopped"]
        atomic_write(report_path, result)
        return result
    due = due_stages(config, jobs, now)
    result["due_stages"] = [{"job": job.key, "stage": stage, "offset_minutes": offset} for job, stage, offset in due]
    runs = state.setdefault("runs", {})
    for job, stage, offset in due:
        prior = ((runs.get(job.key) or {}).get("stages") or {}).get(stage) or {}
        if prior.get("status") == "captured":
            result["processed"].append({"job": job.key, "stage": stage, "status": "already_captured"})
            continue
        try:
            outcome = execute_stage(config, job, stage, offset, state, project, root, now, dry_run)
        except (OSError, ValueError, subprocess.TimeoutExpired, RuntimeError) as exc:
            outcome = {"status": "failed", "stage": stage, "reason": f"{type(exc).__name__}: {exc}"}
        runs.setdefault(job.key, {"stages": {}})["stages"][stage] = {"executed_at_hkt": now.isoformat(), **outcome}
        result["processed"].append({"job": job.key, **outcome})
        if outcome.get("status") == "stopped_official_limit":
            # Persist the validated stop event before asking the root-only dispatcher
            # to read it. No source payload or credentials are passed to sudo.
            state["updated_at_hkt"] = now.isoformat()
            atomic_write(state_path, state)
            state["stopped"]["alert"] = send_limit_alert(int(outcome["http_status"]), project)
            atomic_write(state_path, state)
            break
    if not dry_run:
        state["updated_at_hkt"] = now.isoformat()
        atomic_write(state_path, state)
    result["status"] = result.get("status", "ok")
    atomic_write(report_path, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Candidate-only HKJC pre-race snapshot scheduler")
    parser.add_argument("--config", required=True)
    parser.add_argument("--project-dir", default=".")
    parser.add_argument("--candidate-root", default="runtime/pre_race_snapshot_candidate")
    parser.add_argument("--state-file", default="runtime/pre_race_snapshot_candidate/state.json")
    parser.add_argument("--report", default="runtime/pre_race_snapshot_candidate/latest_scheduler_report.json")
    parser.add_argument("--now")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    project = Path(args.project_dir).resolve()
    root = Path(args.candidate_root).resolve()
    state_path = Path(args.state_file).resolve()
    report_path = Path(args.report).resolve()
    now = iso_hkt(args.now) if args.now else datetime.now(HK_TZ)
    lock_path = state_path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "skipped_locked"}, ensure_ascii=False))
            return 0
        try:
            result = process(Path(args.config).resolve(), project, root, state_path, report_path, now, args.dry_run)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            result = {"status": "rejected_configuration", "reason": f"{type(exc).__name__}: {exc}", "checked_at_hkt": now.isoformat(), "network_requests": 0}
            atomic_write(report_path, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
