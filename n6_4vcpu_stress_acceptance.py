#!/usr/bin/env python3
"""Bounded N6 target-host stress and acceptance test.

This script intentionally does not change N6 workers, semaphore settings, models,
SQLite, network bindings, or systemd configuration. It sends only loopback POST
requests to an existing historical-inference endpoint and writes a Git-ignored
local report. It refuses a host with fewer than four logical CPUs unless --dry-run
is explicitly used.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import math
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "http://127.0.0.1:5001"
DEFAULT_ENDPOINT = "/v1/inference/historical/2026-02-25/HV/1"
OUTPUT_DIR = Path("runtime/n6_target_host_acceptance")
SAMPLE_INTERVAL_SECONDS = 0.20

ACCEPTANCE = {
    "p95_ms_max": 750.0,
    "p99_ms_max": 900.0,
    "min_available_memory_bytes": 1_073_741_824,  # 1 GiB
    "max_memory_growth_bytes": 67_108_864,  # 64 MiB across one test level
    "max_cpu_pressure_some_avg10": 20.0,
}


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * q / 100.0) - 1))
    return round(ordered[index], 3)


def parse_csv_ints(value: str) -> list[int]:
    values = [int(part.strip()) for part in value.split(",") if part.strip()]
    if not values or any(item < 1 or item > 16 for item in values):
        raise argparse.ArgumentTypeError("concurrency values must be between 1 and 16")
    return values


def mem_available_bytes() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        return None
    return None


def pressure_some_avg10(kind: str) -> float | None:
    try:
        for line in Path(f"/proc/pressure/{kind}").read_text(encoding="utf-8").splitlines():
            if line.startswith("some "):
                return float(next(part.split("=", 1)[1] for part in line.split() if part.startswith("avg10=")))
    except (FileNotFoundError, OSError, StopIteration, ValueError):
        return None
    return None


def n6_cgroup() -> Path:
    group = subprocess.check_output(
        ["systemctl", "show", "n6-engine.service", "-p", "ControlGroup", "--value"], text=True
    ).strip()
    if not group.startswith("/"):
        raise RuntimeError("n6-engine.service has no readable control group")
    return Path("/sys/fs/cgroup") / group.lstrip("/")


def read_int(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def cgroup_cpu_usage_usec(group: Path) -> int | None:
    try:
        for line in (group / "cpu.stat").read_text(encoding="utf-8").splitlines():
            if line.startswith("usage_usec "):
                return int(line.split()[1])
    except OSError:
        pass
    return None


def systemd_state() -> dict[str, str]:
    props = ("ActiveState", "NRestarts", "MemoryCurrent", "MemoryPeak")
    command = ["systemctl", "show", "n6-engine.service"] + [f"-p{item}" for item in props]
    output = subprocess.check_output(command, text=True)
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def parse_server_timing(value: str) -> dict[str, float]:
    result: dict[str, float] = {}
    for item in value.split(","):
        parts = [part.strip() for part in item.split(";")]
        if not parts or not parts[0].startswith("n6_"):
            continue
        for part in parts[1:]:
            if part.startswith("dur="):
                result[parts[0].removeprefix("n6_")] = float(part.split("=", 1)[1])
    return result


def request_inference(url: str) -> dict[str, Any]:
    started = time.perf_counter()
    request = urllib.request.Request(url, data=b"", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # nosec B310: fixed local URL only
            response.read()
            return {
                "status": int(response.status),
                "client_latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
                "worker_pid": response.headers.get("X-N6-Worker-Pid"),
                "segments_ms": parse_server_timing(response.headers.get("Server-Timing", "")),
            }
    except urllib.error.HTTPError as exc:
        return {"status": int(exc.code), "client_latency_ms": round((time.perf_counter() - started) * 1000.0, 3), "error": "http_error"}
    except (urllib.error.URLError, TimeoutError) as exc:
        return {"status": None, "client_latency_ms": round((time.perf_counter() - started) * 1000.0, 3), "error": type(exc).__name__}


def snapshot(group: Path) -> dict[str, Any]:
    return {
        "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "memory_available_bytes": mem_available_bytes(),
        "n6_memory_current_bytes": read_int(group / "memory.current"),
        "n6_memory_peak_bytes": read_int(group / "memory.peak"),
        "n6_cpu_usage_usec": cgroup_cpu_usage_usec(group),
        "cpu_pressure_some_avg10": pressure_some_avg10("cpu"),
        "memory_pressure_some_avg10": pressure_some_avg10("memory"),
        "io_pressure_some_avg10": pressure_some_avg10("io"),
    }


def numeric_range(samples: list[dict[str, Any]], field: str) -> dict[str, float | None]:
    values = [float(sample[field]) for sample in samples if sample.get(field) is not None]
    if not values:
        return {"min": None, "max": None, "delta": None}
    return {"min": min(values), "max": max(values), "delta": max(values) - min(values)}


def run_level(url: str, concurrency: int, waves: int, group: Path) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    stop = threading.Event()

    def sampler() -> None:
        while not stop.is_set():
            samples.append(snapshot(group))
            time.sleep(SAMPLE_INTERVAL_SECONDS)
        samples.append(snapshot(group))

    thread = threading.Thread(target=sampler, daemon=True)
    thread.start()
    started = time.perf_counter()
    records: list[dict[str, Any]] = []
    try:
        for _ in range(waves):
            with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = [executor.submit(request_inference, url) for _ in range(concurrency)]
                records.extend(future.result() for future in futures)
    finally:
        stop.set()
        thread.join(timeout=2)

    client = [float(row["client_latency_ms"]) for row in records]
    gate = [float(row.get("segments_ms", {}).get("gate", 0.0)) for row in records]
    status_counts = Counter(str(row.get("status")) for row in records)
    worker_counts = Counter(str(row.get("worker_pid")) for row in records if row.get("worker_pid"))
    resources = {
        field: numeric_range(samples, field)
        for field in (
            "memory_available_bytes",
            "n6_memory_current_bytes",
            "n6_memory_peak_bytes",
            "n6_cpu_usage_usec",
            "cpu_pressure_some_avg10",
            "memory_pressure_some_avg10",
            "io_pressure_some_avg10",
        )
    }
    checks = {
        "all_http_200": all(row.get("status") == 200 for row in records),
        "p95_under_limit": percentile(client, 95) is not None and percentile(client, 95) <= ACCEPTANCE["p95_ms_max"],
        "p99_under_limit": percentile(client, 99) is not None and percentile(client, 99) <= ACCEPTANCE["p99_ms_max"],
        "memory_available_floor": resources["memory_available_bytes"]["min"] is not None and resources["memory_available_bytes"]["min"] >= ACCEPTANCE["min_available_memory_bytes"],
        "memory_growth_bounded": resources["n6_memory_current_bytes"]["delta"] is not None and resources["n6_memory_current_bytes"]["delta"] <= ACCEPTANCE["max_memory_growth_bytes"],
        "cpu_pressure_bounded": resources["cpu_pressure_some_avg10"]["max"] is None or resources["cpu_pressure_some_avg10"]["max"] <= ACCEPTANCE["max_cpu_pressure_some_avg10"],
    }
    return {
        "concurrency": concurrency,
        "waves": waves,
        "request_count": len(records),
        "wall_clock_seconds": round(time.perf_counter() - started, 3),
        "status_counts": dict(sorted(status_counts.items())),
        "worker_request_distribution": dict(sorted(worker_counts.items())),
        "client_latency_ms": {f"p{q}": percentile(client, q) for q in (50, 95, 99)},
        "gate_wait_ms": {f"p{q}": percentile(gate, q) for q in (50, 95, 99)},
        "resource_ranges": resources,
        "checks": checks,
        "passed": all(checks.values()),
        "records": records,
        "samples": samples,
    }


def write_markdown(report: dict[str, Any], path: Path) -> None:
    lines = [
        "# N6 4 vCPU 目標主機壓力測試與驗收報告",
        "",
        "> 這是 loopback 歷史推論的受控測試；不修改 N6、V10、模型、SQLite 或網路設定。",
        "",
        f"- 執行時間（UTC）：{report['started_at_utc']}",
        f"- 邏輯CPU：{report['host']['logical_cpus']}",
        f"- N6 初始服務狀態：{report['service_before']}",
        f"- 整體結果：**{report['overall_status']}**",
        "",
        "| 併發 | 請求 | p95 | p99 | gate p95 | HTTP | worker分佈 | 結果 |",
        "|---:|---:|---:|---:|---:|---|---|---|",
    ]
    for level in report.get("levels", []):
        client = level["client_latency_ms"]
        gate = level["gate_wait_ms"]
        lines.append(
            f"| {level['concurrency']} | {level['request_count']} | {client['p95']}ms | {client['p99']}ms | {gate['p95']}ms | {level['status_counts']} | {level['worker_request_distribution']} | {'PASS' if level['passed'] else 'FAIL'} |"
        )
    lines.extend([
        "",
        "## 驗收門檻",
        "",
        "- 每個負載級別所有請求必須HTTP 200。",
        "- client p95 <= 750ms，p99 <= 900ms。",
        "- 可用記憶體最低值 >= 1GiB，N6 current memory 在單一負載級別的變化 <= 64MiB。",
        "- CPU pressure `some avg10` <= 20%。",
        "- N6 service 在測試前後必須active，`NRestarts`不得增加。",
        "",
        "## 使用限制",
        "",
        "- 僅可在已改用4 vCPU、4 worker、每worker gate=2的目標主機上以 `--execute-on-4vcpu` 執行。",
        "- 結果不會自動修改任何worker數、gate、模型或資料庫設定。",
        "- 若任一門檻失敗，應停止擴容決策並保存此runtime報告作排查依據。",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-on-4vcpu", action="store_true", help="required to issue bounded loopback probes")
    parser.add_argument("--concurrencies", type=parse_csv_ints, default=[2, 4, 8])
    parser.add_argument("--waves", type=int, default=20)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--historical-endpoint", default=DEFAULT_ENDPOINT)
    args = parser.parse_args()

    if args.waves < 1 or args.waves > 60:
        parser.error("waves must be between 1 and 60")
    cpu_count = os.cpu_count() or 0
    if not args.execute_on_4vcpu:
        print(json.dumps({"status": "dry_run", "logical_cpus": cpu_count, "would_test": args.concurrencies, "requires_flag": "--execute-on-4vcpu"}))
        return 0
    if cpu_count < 4:
        raise SystemExit(f"REFUSED: target has {cpu_count} logical CPUs; requires at least 4")
    if not args.base_url.startswith("http://127.0.0.1"):
        raise SystemExit("REFUSED: base URL must remain loopback-only")

    health = urllib.request.urlopen(f"{args.base_url}/health", timeout=3)  # nosec B310: validated loopback URL
    if health.status != 200:
        raise SystemExit(f"REFUSED: health endpoint returned {health.status}")
    health.read()

    group = n6_cgroup()
    service_before = systemd_state()
    output_stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report: dict[str, Any] = {
        "schema_version": 1,
        "scope": "target_host_4vcpu_bounded_loopback_historical_inference",
        "started_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "host": {"logical_cpus": cpu_count},
        "service_before": service_before,
        "configuration_changed": False,
        "writes_performed": 0,
        "acceptance_thresholds": ACCEPTANCE,
        "levels": [],
    }
    url = f"{args.base_url}{args.historical_endpoint}"
    for concurrency in args.concurrencies:
        report["levels"].append(run_level(url, concurrency, args.waves, group))
    report["service_after"] = systemd_state()
    report["checks"] = {
        "service_active_after": report["service_after"].get("ActiveState") == "active",
        "no_service_restart": report["service_after"].get("NRestarts") == service_before.get("NRestarts"),
        "all_levels_passed": all(level["passed"] for level in report["levels"]),
    }
    report["overall_status"] = "PASS" if all(report["checks"].values()) else "FAIL"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUTPUT_DIR / f"n6_4vcpu_acceptance_{output_stamp}.json"
    md_path = OUTPUT_DIR / f"n6_4vcpu_acceptance_{output_stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_markdown(report, md_path)
    print(json.dumps({"status": report["overall_status"], "json_report": str(json_path), "markdown_report": str(md_path), "levels": [{"concurrency": item["concurrency"], "passed": item["passed"]} for item in report["levels"]]}, ensure_ascii=False))
    return 0 if report["overall_status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
