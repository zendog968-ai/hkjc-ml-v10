#!/usr/bin/env python3
"""Manual-only HKJC archive capture for the isolated P1 candidate pipeline.

This helper is intentionally separate from p1_ingest.py. It is not a scheduler,
does not access V10/N6, and only creates immutable raw archives plus a manifest
for later offline parsing. Invoke it only for an explicitly approved manual run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen


CAPTURE_VERSION = "preseason_p1_manual_capture_v1"
ALLOWED_SOURCE_KINDS = {"hkjc_pp_list", "hkjc_pp_form", "hkjc_barrier_trial"}


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def validate_hkjc_url(url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "hkjc.com" or host.endswith(".hkjc.com")):
        raise ValueError("manual capture accepts HTTPS HKJC official URLs only")


def read_state(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Manual HKJC archival capture for P1 candidate sources")
    parser.add_argument("--capture-plan", required=True, help="JSON plan with manual_capture_confirmed=true and source list")
    parser.add_argument("--archive-dir", required=True)
    parser.add_argument("--manifest-output", required=True)
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--minimum-interval-seconds", type=int, default=60)
    args = parser.parse_args()
    if args.minimum_interval_seconds < 60:
        raise ValueError("HKJC minimum request interval must be at least 60 seconds")
    plan_path = Path(args.capture_plan).resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("manual_capture_confirmed") is not True or plan.get("live_automation_enabled") is not False:
        raise ValueError("capture plan must explicitly confirm a manual run and disable live automation")
    sources = plan.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("capture plan must contain sources")
    as_of = parse_utc(args.as_of_utc)
    archive_dir = Path(args.archive_dir).resolve()
    project_root = Path(args.project_root).resolve()
    state_path = Path(args.state_file).resolve()
    state = read_state(state_path)
    last_request = parse_utc(state["last_hkjc_request_utc"]) if state.get("last_hkjc_request_utc") else None
    captured: list[dict[str, str]] = []
    for item in sources:
        source_kind = item.get("source_kind")
        source_url = item.get("source_url")
        filename = item.get("archive_filename")
        if source_kind not in ALLOWED_SOURCE_KINDS or not isinstance(source_url, str) or not isinstance(filename, str):
            raise ValueError("each capture source needs permitted source_kind, source_url, archive_filename")
        validate_hkjc_url(source_url)
        target = (archive_dir / filename).resolve()
        if archive_dir not in target.parents:
            raise ValueError("archive filename must remain inside archive-dir")
        if target.exists():
            raise FileExistsError(f"refuse overwrite of immutable source archive: {target}")
        if last_request is not None:
            elapsed = (datetime.now(UTC) - last_request).total_seconds()
            if elapsed < args.minimum_interval_seconds:
                time.sleep(args.minimum_interval_seconds - elapsed)
        request = Request(source_url, headers={"User-Agent": "HKJC-P1-Candidate-Archive/1.0 (manual; contact owner)"})
        with urlopen(request, timeout=30) as response:
            content = response.read()
        retrieved_at = utc_now()
        if parse_utc(retrieved_at) >= as_of:
            raise ValueError("capture completed at or after as_of_utc; archive not eligible for this candidate snapshot")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        last_request = parse_utc(retrieved_at)
        state = {"last_hkjc_request_utc": retrieved_at, "capture_version": CAPTURE_VERSION}
        write_json(state_path, state)
        captured.append({
            "source_kind": source_kind,
            "source_url": source_url,
            "retrieved_at_utc": retrieved_at,
            "local_relative_path": str(target.relative_to(project_root)),
            "content_sha256": sha256_bytes(content),
        })
    manifest = {
        "manifest_version": "preseason_p1_archive_manifest_v1",
        "candidate_schema_version": "n6_preseason_candidate_p1_v1",
        "mode": "offline_archive_only",
        "production_write_forbidden": True,
        "n6_service_integration": "forbidden",
        "as_of_utc": args.as_of_utc,
        "capture_policy": {
            "capture_component": CAPTURE_VERSION,
            "minimum_seconds_between_hkjc_requests": args.minimum_interval_seconds,
            "live_automation_enabled": False,
            "parser_network_access": False,
        },
        "sources": captured,
    }
    write_json(Path(args.manifest_output).resolve(), manifest)
    print(json.dumps({"status": "captured", "source_count": len(captured), "manifest": str(Path(args.manifest_output).resolve())}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
