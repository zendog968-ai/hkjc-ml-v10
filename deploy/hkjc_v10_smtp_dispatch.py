#!/usr/bin/env python3
"""Root-only Gmail SMTP dispatcher for V10 operational alerts.

The weekly scheduler can request only a fixed failure notification via a narrow
sudo rule. Credentials remain in /etc/hkjc-v10/smtp.env (root:root 0600).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import sys
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

CONFIG_PATH = Path("/etc/hkjc-v10/smtp.env")
STATUS_PATH = Path("/home/ubuntu/hkjc_v10_database/runtime/weekly_update_status.env")
CANDIDATE_STATE_PATH = Path("/home/ubuntu/hkjc_v10_database/runtime/pre_race_snapshot_candidate/state.json")
PROJECT_ROOT = Path("/home/ubuntu/hkjc_v10_database")
LOG_ROOT = PROJECT_ROOT / "archive" / "monthly_update_logs"


def load_config(path: Path) -> dict[str, str]:
    required = {
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USE_SSL",
        "SENDER_EMAIL",
        "RECEIVER_EMAIL",
        "SMTP_PASSWORD",
    }
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if sep and key in required:
            values[key] = value
    missing = required - values.keys()
    if missing:
        raise ValueError(f"missing SMTP config fields: {', '.join(sorted(missing))}")
    return values


def safe_status() -> tuple[str, str, str]:
    """Return validated attempt count, exit code and local log path only."""
    values: dict[str, str] = {}
    for line in STATUS_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[:24]:
        key, sep, value = line.partition("=")
        if sep and key in {"ATTEMPTS", "EXIT_CODE", "LOG_PATH", "STATUS"}:
            values[key] = value
    if values.get("STATUS") != "failed":
        raise ValueError("weekly failure status is absent or not marked failed")
    attempts = values.get("ATTEMPTS", "")
    exit_code = values.get("EXIT_CODE", "")
    log_path = values.get("LOG_PATH", "")
    if not re.fullmatch(r"[1-3]", attempts):
        raise ValueError("invalid attempt count in status")
    if not re.fullmatch(r"[0-9]{1,3}", exit_code):
        raise ValueError("invalid exit code in status")
    resolved_log = Path(log_path).resolve()
    if LOG_ROOT not in resolved_log.parents or resolved_log.suffix != ".log":
        raise ValueError("invalid log path in status")
    return attempts, exit_code, str(resolved_log)


def safe_candidate_snapshot_stop() -> tuple[str, str, str]:
    """Return only validated public stop metadata; never expose source payloads."""
    try:
        payload = json.loads(CANDIDATE_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("candidate snapshot stop status is absent") from exc
    stopped = payload.get("stopped") if isinstance(payload.get("stopped"), dict) else {}
    if stopped.get("status") != "stopped_official_limit":
        raise ValueError("candidate snapshot stop status is absent")
    http_status = str(stopped.get("http_status") or "")
    stage = str(stopped.get("stage") or "")
    at_utc = str(stopped.get("at_utc") or "")
    if http_status not in {"403", "429"} or stage not in {"RACECARD", "T_MINUS_15", "T_MINUS_5"}:
        raise ValueError("invalid candidate snapshot stop payload")
    return http_status, stage, at_utc


def send(subject: str, body: str) -> None:
    config = load_config(CONFIG_PATH)
    message = EmailMessage()
    message["From"] = config["SENDER_EMAIL"]
    message["To"] = config["RECEIVER_EMAIL"]
    message["Subject"] = subject
    message.set_content(body)
    with smtplib.SMTP_SSL(
        config["SMTP_HOST"], int(config["SMTP_PORT"]), timeout=20
    ) as smtp:
        smtp.login(config["SENDER_EMAIL"], config["SMTP_PASSWORD"])
        smtp.send_message(message)


def main() -> int:
    if os.geteuid() != 0:
        print("This dispatcher must run as root.", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description="Send fixed V10 operational email alerts.")
    command = parser.add_mutually_exclusive_group(required=True)
    command.add_argument("--test", action="store_true")
    command.add_argument("--weekly-failure", action="store_true")
    command.add_argument("--candidate-snapshot-stop", action="store_true")
    args = parser.parse_args()
    now = datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d %H:%M:%S HKT")
    if args.test:
        send(
            "[V10] Weekly update email alert test",
            "This is a configuration test for V10 weekly-update failure alerts.\n"
            f"Sent at: {now}\n\n"
            "No model, database, scheduler, or runtime data was changed by this test.\n",
        )
        print("test_email_sent")
        return 0
    if args.candidate_snapshot_stop:
        http_status, stage, at_utc = safe_candidate_snapshot_stop()
        send(
            "[V10][Action required] Candidate snapshot capture stopped by HKJC limit",
            "Candidate-only T-15/T-5 pre-race snapshot capture has stopped immediately.\n\n"
            f"Official response: HTTP {http_status}\n"
            f"Stage: {stage}\n"
            f"Recorded at: {at_utc}\n\n"
            "No bypass or retry was attempted. V10 probabilities, EV, Kelly, model artifacts and N6 were not modified. "
            "Review the candidate scheduler state before explicitly enabling a later meeting.\n",
        )
        print("candidate_snapshot_stop_email_sent")
        return 0
    attempts, exit_code, log_path = safe_status()
    send(
        "[V10][Action required] Weekly update failed after retry limit",
        "V10 weekly model/data update did not complete successfully.\n\n"
        f"Time: {now}\n"
        f"Attempts: {attempts}/3\n"
        f"Final exit code: {exit_code}\n"
        f"Local log: {log_path}\n\n"
        "The scheduler retained the non-overlap lock and did not change V10 inference or N6. "
        "Please inspect the local log before any manual rerun.\n",
    )
    print("failure_email_sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
