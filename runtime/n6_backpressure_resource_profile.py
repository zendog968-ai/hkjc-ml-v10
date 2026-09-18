#!/usr/bin/env python3
"""Short, loopback-only N6 resource profile under a bounded eight-client load."""
from __future__ import annotations

import concurrent.futures
import json
import math
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

BASE_URL = "http://127.0.0.1:5001"
ENDPOINT = f"{BASE_URL}/v1/inference/historical/2026-02-25/HV/1"
OUTPUT = Path("runtime/n6_backpressure_resource_profile.json")
SAMPLE_INTERVAL_SECONDS = 0.10
CLIENTS = 8
WAVES = 12


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, PermissionError, ValueError):
        return None


def _read_mem_available() -> int | None:
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    return None


def _read_cpu_total() -> tuple[int, int]:
    fields = [int(value) for value in Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:]]
    total = sum(fields)
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
    return total, idle


def _read_pressure(resource: str) -> float | None:
    try:
        for line in Path(f"/proc/pressure/{resource}").read_text(encoding="utf-8").splitlines():
            if line.startswith("some "):
                for part in line.split():
                    if part.startswith("avg10="):
                        return float(part.split("=", 1)[1])
    except FileNotFoundError:
        return None
    return None


def _control_group() -> Path:
    group = subprocess.check_output(
        ["systemctl", "show", "n6-engine.service", "-p", "ControlGroup", "--value"], text=True
    ).strip()
    if not group.startswith("/"):
        raise RuntimeError("N6 cgroup unavailable")
    return Path("/sys/fs/cgroup") / group.lstrip("/")


def _cgroup_cpu_usage_usec(group: Path) -> int | None:
    try:
        for line in (group / "cpu.stat").read_text(encoding="utf-8").splitlines():
            if line.startswith("usage_usec "):
                return int(line.split()[1])
    except (FileNotFoundError, PermissionError, ValueError):
        pass
    return None


def _sample(group: Path) -> dict[str, Any]:
    total, idle = _read_cpu_total()
    return {
        "t_monotonic_seconds": round(time.monotonic(), 6),
        "host_cpu_total_ticks": total,
        "host_cpu_idle_ticks": idle,
        "host_mem_available_bytes": _read_mem_available(),
        "n6_memory_current_bytes": _read_int(group / "memory.current"),
        "n6_memory_peak_bytes": _read_int(group / "memory.peak"),
        "n6_cpu_usage_usec": _cgroup_cpu_usage_usec(group),
        "cpu_pressure_some_avg10": _read_pressure("cpu"),
        "memory_pressure_some_avg10": _read_pressure("memory"),
        "io_pressure_some_avg10": _read_pressure("io"),
    }


def _parse_timing(header: str) -> dict[str, float]:
    values: dict[str, float] = {}
    for item in header.split(","):
        parts = [part.strip() for part in item.split(";")]
        if not parts or not parts[0].startswith("n6_"):
            continue
        for part in parts[1:]:
            if part.startswith("dur="):
                values[parts[0].removeprefix("n6_")] = float(part.split("=", 1)[1])
    return values


def _request() -> dict[str, Any]:
    started = time.perf_counter()
    request = urllib.request.Request(ENDPOINT, data=b"", method="POST")
    with urllib.request.urlopen(request, timeout=5) as response:  # nosec B310: fixed loopback URL
        response.read()
        result = {
            "status": int(response.status),
            "worker_pid": response.headers.get("X-N6-Worker-Pid"),
            "segments_ms": _parse_timing(response.headers.get("Server-Timing", "")),
        }
    result["client_latency_ms"] = (time.perf_counter() - started) * 1000.0
    return result


def _percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * percent / 100.0) - 1))
    return round(ordered[index], 3)


def _range(values: list[int | float | None]) -> dict[str, float | None]:
    numeric = [float(value) for value in values if value is not None]
    if not numeric:
        return {"min": None, "max": None, "delta": None}
    return {"min": min(numeric), "max": max(numeric), "delta": max(numeric) - min(numeric)}


def _cpu_busy_percent(samples: list[dict[str, Any]]) -> float | None:
    if len(samples) < 2:
        return None
    total_delta = samples[-1]["host_cpu_total_ticks"] - samples[0]["host_cpu_total_ticks"]
    idle_delta = samples[-1]["host_cpu_idle_ticks"] - samples[0]["host_cpu_idle_ticks"]
    if not total_delta:
        return None
    return round((1.0 - idle_delta / total_delta) * 100.0, 3)


def main() -> int:
    group = _control_group()
    samples: list[dict[str, Any]] = []
    stop = threading.Event()

    def sampler() -> None:
        while not stop.is_set():
            samples.append(_sample(group))
            time.sleep(SAMPLE_INTERVAL_SECONDS)
        samples.append(_sample(group))

    thread = threading.Thread(target=sampler, daemon=True)
    thread.start()
    started = time.perf_counter()
    records: list[dict[str, Any]] = []
    try:
        for _ in range(WAVES):
            with concurrent.futures.ThreadPoolExecutor(max_workers=CLIENTS) as pool:
                records.extend(future.result() for future in [pool.submit(_request) for _ in range(CLIENTS)])
    finally:
        stop.set()
        thread.join(timeout=2)

    latencies = [float(item["client_latency_ms"]) for item in records]
    gate = [float(dict(item["segments_ms"]).get("gate", 0.0)) for item in records]
    worker_counts: dict[str, int] = {}
    for item in records:
        worker = str(item.get("worker_pid"))
        worker_counts[worker] = worker_counts.get(worker, 0) + 1

    report = {
        "schema_version": 1,
        "scope": "loopback_only_historical_probe",
        "writes_performed": 0,
        "server_configuration_changed": False,
        "clients": CLIENTS,
        "waves": WAVES,
        "requests": len(records),
        "wall_clock_seconds": round(time.perf_counter() - started, 3),
        "all_http_200": all(int(item["status"]) == 200 for item in records),
        "worker_request_distribution": dict(sorted(worker_counts.items())),
        "client_latency_ms": {f"p{percent}": _percentile(latencies, percent) for percent in (50, 95, 99)},
        "gate_wait_ms": {f"p{percent}": _percentile(gate, percent) for percent in (50, 95, 99)},
        "resource_summary": {
            "host_cpu_busy_percent": _cpu_busy_percent(samples),
            "host_mem_available_bytes": _range([item["host_mem_available_bytes"] for item in samples]),
            "n6_memory_current_bytes": _range([item["n6_memory_current_bytes"] for item in samples]),
            "n6_memory_peak_bytes": _range([item["n6_memory_peak_bytes"] for item in samples]),
            "n6_cpu_usage_usec": _range([item["n6_cpu_usage_usec"] for item in samples]),
            "cpu_pressure_some_avg10": _range([item["cpu_pressure_some_avg10"] for item in samples]),
            "memory_pressure_some_avg10": _range([item["memory_pressure_some_avg10"] for item in samples]),
            "io_pressure_some_avg10": _range([item["io_pressure_some_avg10"] for item in samples]),
        },
        "sample_count": len(samples),
        "records": records,
        "samples": samples,
    }
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("requests", "all_http_200", "client_latency_ms", "gate_wait_ms", "worker_request_distribution", "resource_summary", "sample_count")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
