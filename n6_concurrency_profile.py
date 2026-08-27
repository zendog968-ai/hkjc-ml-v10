#!/usr/bin/env python3
"""Bounded loopback-only N6 concurrency profile; reports no race payloads."""
from __future__ import annotations

import concurrent.futures
import json
import math
import time
import urllib.request
from collections import Counter
from pathlib import Path

BASE_URL = "http://127.0.0.1:5001"
ENDPOINT = f"{BASE_URL}/v1/inference/historical/2026-02-25/HV/1"
OUTPUT = Path("runtime/n6_concurrency_profile.json")


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(len(ordered) * p / 100) - 1))]


def parse_server_timing(value: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for item in value.split(","):
        name, *params = [part.strip() for part in item.split(";")]
        if not name.startswith("n6_"):
            continue
        for param in params:
            if param.startswith("dur="):
                out[name.removeprefix("n6_")] = float(param.split("=", 1)[1])
    return out


def one_request() -> dict[str, object]:
    started = time.perf_counter()
    request = urllib.request.Request(ENDPOINT, data=b"", method="POST")
    with urllib.request.urlopen(request, timeout=5) as response:  # nosec B310: fixed loopback
        response.read()
        worker = response.headers.get("X-N6-Worker-Pid")
        timing = parse_server_timing(response.headers.get("Server-Timing", ""))
        status = int(response.status)
    return {
        "status": status,
        "client_latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "worker_pid": worker,
        "segments_ms": {key: round(value, 3) for key, value in sorted(timing.items())},
    }


def summarize(records: list[dict[str, object]], clients: int) -> dict[str, object]:
    latencies = [float(row["client_latency_ms"]) for row in records]
    workers = Counter(str(row.get("worker_pid")) for row in records)
    segment_names = ("gate", "feature", "preprocess", "forward", "postprocess", "total")
    segments: dict[str, dict[str, float | None]] = {}
    for name in segment_names:
        values = [float(dict(row["segments_ms"]).get(name, 0.0)) for row in records]
        segments[name] = {
            "p50_ms": round(percentile(values, 50) or 0.0, 3),
            "p95_ms": round(percentile(values, 95) or 0.0, 3),
            "p99_ms": round(percentile(values, 99) or 0.0, 3),
            "max_ms": round(max(values) if values else 0.0, 3),
        }
    return {
        "client_concurrency": clients,
        "requests": len(records),
        "all_http_200": all(int(row["status"]) == 200 for row in records),
        "client_latency_ms": {
            "p50_ms": round(percentile(latencies, 50) or 0.0, 3),
            "p95_ms": round(percentile(latencies, 95) or 0.0, 3),
            "p99_ms": round(percentile(latencies, 99) or 0.0, 3),
            "max_ms": round(max(latencies) if latencies else 0.0, 3),
        },
        "worker_request_distribution": dict(sorted(workers.items())),
        "segments_ms": segments,
    }


def run_level(clients: int, waves: int) -> dict[str, object]:
    all_records: list[dict[str, object]] = []
    started = time.perf_counter()
    for _ in range(waves):
        with concurrent.futures.ThreadPoolExecutor(max_workers=clients) as pool:
            futures = [pool.submit(one_request) for _ in range(clients)]
            all_records.extend(future.result() for future in futures)
    summary = summarize(all_records, clients)
    summary["wall_clock_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    return summary


def main() -> int:
    levels = {str(clients): run_level(clients, waves=4) for clients in (2, 4, 8)}
    report = {
        "schema_version": 1,
        "scope": "loopback_only_historical_probe",
        "writes_performed": 0,
        "server_configuration_changed": False,
        "configured_per_worker_gate": 2,
        "uvicorn_workers": 2,
        "interpretation": "Client concurrency 4 measures current aggregate load; it does not change the per-worker gate. Client concurrency 8 demonstrates queueing pressure under the current gate.",
        "levels": levels,
    }
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT), "levels": {key: value["client_latency_ms"] for key, value in levels.items()}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
