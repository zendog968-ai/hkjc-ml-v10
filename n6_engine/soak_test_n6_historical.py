#!/usr/bin/env python3
"""Read-only N6 historical inference soak test for the 2-worker production service."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
SOURCE_CSV = ROOT / "reports" / "n6_test_predictions.csv"
V10_DB = Path("/home/ubuntu/hkjc_v10_database/hkjc_last_season.sqlite")
CGROUP_MEMORY = Path("/sys/fs/cgroup/system.slice/n6-engine.service/memory.current")


@dataclass(frozen=True)
class RaceKey:
    race_date: str
    racecourse: str
    race_no: int


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_races(path: Path) -> list[RaceKey]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"race_date", "racecourse", "race_no"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError(f"missing historical test columns; got={reader.fieldnames}")
        seen: set[RaceKey] = set()
        for row in reader:
            seen.add(RaceKey(row["race_date"], row["racecourse"], int(row["race_no"])))
    return sorted(seen, key=lambda race: (race.race_date, race.racecourse, race.race_no))


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def service_memory_bytes() -> int | None:
    try:
        return int(CGROUP_MEMORY.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, PermissionError, ValueError):
        return None


def process_rss_kib() -> int | None:
    result = subprocess.run(
        ["ps", "-C", "python", "-o", "rss="], text=True, capture_output=True, check=False
    )
    values: list[int] = []
    for raw in result.stdout.splitlines():
        try:
            values.append(int(raw.strip()))
        except ValueError:
            continue
    return sum(values) if values else None


def n6_restarts() -> int | None:
    result = subprocess.run(
        ["systemctl", "show", "n6-engine.service", "-p", "NRestarts", "--value"],
        text=True,
        capture_output=True,
        check=False,
    )
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def request_race(base_url: str, race: RaceKey, timeout: float) -> dict[str, Any]:
    started = time.perf_counter()
    url = f"{base_url}/v1/inference/historical/{race.race_date}/{race.racecourse}/{race.race_no}"
    try:
        request = Request(url, data=b"", method="POST")
        try:
            with urlopen(request, timeout=timeout) as response:  # nosec B310: fixed loopback URL
                status_code = int(response.status)
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            status_code = int(error.code)
            payload = json.loads(error.read().decode("utf-8")) if error.fp else {}
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        scores = payload.get("scores", []) if isinstance(payload, dict) else []
        probability_sum = sum(float(item.get("neural_win_probability", 0.0)) for item in scores)
        ranks = sorted(int(item.get("neural_rank", 0)) for item in scores)
        valid = (
            status_code == 200
            and len(scores) > 0
            and abs(probability_sum - 1.0) <= 1e-5
            and ranks == list(range(1, len(scores) + 1))
        )
        return {
            "ok": valid,
            "status_code": status_code,
            "elapsed_ms": elapsed_ms,
            "race": asdict(race),
            "probability_sum": probability_sum,
            "score_count": len(scores),
            "detail": None if valid else str(payload)[:500],
        }
    except Exception as error:  # Defensive test boundary
        return {
            "ok": False,
            "status_code": None,
            "elapsed_ms": (time.perf_counter() - started) * 1000.0,
            "race": asdict(race),
            "probability_sum": None,
            "score_count": 0,
            "detail": repr(error),
        }


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-seconds", type=int, default=10800)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--cycle-seconds", type=float, default=2.0)
    parser.add_argument("--telemetry-seconds", type=float, default=60.0)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "reports" / "soak")
    args = parser.parse_args()
    if args.duration_seconds < 60 or args.concurrency < 1 or args.concurrency > 2 or args.cycle_seconds < 0.2:
        raise ValueError("duration >=60, concurrency 1..2, cycle >=0.2 are required")

    races = load_races(SOURCE_CSV)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    started_wall = time.time()
    started_monotonic = time.monotonic()
    started_utc = datetime.now(timezone.utc).isoformat()
    db_hash_before = sha256_file(V10_DB)
    db_mtime_before_ns = V10_DB.stat().st_mtime_ns
    service_restarts_before = n6_restarts()
    samples: list[dict[str, Any]] = []
    telemetry: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    cycle = 0
    next_telemetry = started_monotonic

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        while time.monotonic() - started_monotonic < args.duration_seconds:
            cycle_start = time.monotonic()
            selected = [races[(cycle * args.concurrency + index) % len(races)] for index in range(args.concurrency)]
            futures = [pool.submit(request_race, args.base_url, race, args.timeout_seconds) for race in selected]
            for future in as_completed(futures):
                result = future.result()
                result["elapsed_since_start_seconds"] = time.monotonic() - started_monotonic
                samples.append(result)
                if not result["ok"]:
                    failures.append(result)
            now = time.monotonic()
            if now >= next_telemetry:
                telemetry.append(
                    {
                        "elapsed_since_start_seconds": now - started_monotonic,
                        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                        "n6_cgroup_memory_bytes": service_memory_bytes(),
                        "python_rss_kib_total": process_rss_kib(),
                        "n6_restarts": n6_restarts(),
                        "successful_requests": sum(1 for item in samples if item["ok"]),
                        "failed_requests": len(failures),
                    }
                )
                next_telemetry = now + args.telemetry_seconds
            cycle += 1
            remaining = args.cycle_seconds - (time.monotonic() - cycle_start)
            if remaining > 0:
                time.sleep(remaining)

    elapsed_seconds = time.monotonic() - started_monotonic
    db_hash_after = sha256_file(V10_DB)
    db_mtime_after_ns = V10_DB.stat().st_mtime_ns
    latencies = [float(item["elapsed_ms"]) for item in samples if item["ok"]]
    memory_values = [item["n6_cgroup_memory_bytes"] for item in telemetry if item["n6_cgroup_memory_bytes"] is not None]
    memory_delta = memory_values[-1] - memory_values[0] if len(memory_values) >= 2 else None
    report = {
        "schema_version": "n6_historical_soak_test_v1",
        "started_utc": started_utc,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "workload": {
            "unique_real_historical_races": len(races),
            "duration_seconds_requested": args.duration_seconds,
            "duration_seconds_observed": elapsed_seconds,
            "concurrency": args.concurrency,
            "cycle_seconds": args.cycle_seconds,
            "base_url": args.base_url,
            "source_csv": str(SOURCE_CSV),
        },
        "results": {
            "total_requests": len(samples),
            "successful_requests": len(latencies),
            "failed_requests": len(failures),
            "throughput_requests_per_second": len(samples) / elapsed_seconds if elapsed_seconds else None,
            "latency_mean_ms": statistics.mean(latencies) if latencies else None,
            "latency_p50_ms": percentile(latencies, 0.50),
            "latency_p95_ms": percentile(latencies, 0.95),
            "latency_p99_ms": percentile(latencies, 0.99),
            "latency_max_ms": max(latencies) if latencies else None,
            "probability_conservation_passed": not failures,
            "failures_sample": failures[:25],
        },
        "memory": {
            "telemetry_samples": len(telemetry),
            "n6_cgroup_memory_start_bytes": memory_values[0] if memory_values else None,
            "n6_cgroup_memory_end_bytes": memory_values[-1] if memory_values else None,
            "n6_cgroup_memory_delta_bytes": memory_delta,
            "n6_cgroup_memory_peak_bytes": max(memory_values) if memory_values else None,
        },
        "integrity": {
            "v10_sqlite_sha256_before": db_hash_before,
            "v10_sqlite_sha256_after": db_hash_after,
            "v10_sqlite_mtime_before_ns": db_mtime_before_ns,
            "v10_sqlite_mtime_after_ns": db_mtime_after_ns,
            "v10_sqlite_unchanged": db_hash_before == db_hash_after and db_mtime_before_ns == db_mtime_after_ns,
            "n6_restarts_before": service_restarts_before,
            "n6_restarts_after": n6_restarts(),
        },
        "telemetry": telemetry,
    }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    write_json(args.output_dir / f"n6_soak_{stamp}.json", report)
    write_json(args.output_dir / f"n6_soak_{stamp}_requests.json", samples)
    print(json.dumps({"status": "PASS" if not failures else "FAIL", "report": str(args.output_dir / f"n6_soak_{stamp}.json"), "summary": report["results"]}, ensure_ascii=False))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
