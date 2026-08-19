#!/usr/bin/env python3
"""Analyse one completed N6 soak-test report without changing production state."""
from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
    return ordered[position]


def linear_fit(xs: list[float], ys: list[float]) -> tuple[float | None, float | None]:
    if len(xs) < 2:
        return None, None
    x_mean, y_mean = statistics.mean(xs), statistics.mean(ys)
    denominator = sum((x - x_mean) ** 2 for x in xs)
    if denominator == 0:
        return None, None
    slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denominator
    fitted = [y_mean + slope * (x - x_mean) for x in xs]
    residual = sum((y - estimate) ** 2 for y, estimate in zip(ys, fitted))
    total = sum((y - y_mean) ** 2 for y in ys)
    r_squared = 1.0 - residual / total if total else 1.0
    return slope, r_squared


def bucket_metrics(samples: list[dict], start: float, end: float) -> dict:
    values = [float(sample["elapsed_ms"]) for sample in samples if start <= float(sample["elapsed_since_start_seconds"]) < end and sample["ok"]]
    return {
        "start_seconds": start,
        "end_seconds": end,
        "request_count": len(values),
        "mean_ms": statistics.mean(values) if values else None,
        "p50_ms": percentile(values, 0.50),
        "p95_ms": percentile(values, 0.95),
        "p99_ms": percentile(values, 0.99),
        "max_ms": max(values) if values else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("requests", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    requests = json.loads(args.requests.read_text(encoding="utf-8"))
    telemetry = report["telemetry"]
    xs = [float(item["elapsed_since_start_seconds"]) for item in telemetry]
    ys = [float(item["n6_cgroup_memory_bytes"]) for item in telemetry]
    slope_bytes_per_sec, r2 = linear_fit(xs, ys)
    quarters = max(1, len(ys) // 4)
    first_quarter_mean = statistics.mean(ys[:quarters])
    last_quarter_mean = statistics.mean(ys[-quarters:])
    duration = float(report["workload"]["duration_seconds_observed"])
    buckets = [bucket_metrics(requests, start, min(start + 3600.0, duration + 0.001)) for start in (0.0, 3600.0, 7200.0)]
    memory_delta = ys[-1] - ys[0]
    slope_mib_hour = slope_bytes_per_sec * 3600 / (1024 * 1024) if slope_bytes_per_sec is not None else None
    peak_after_last_quarter = max(ys[-quarters:])
    conclusion = {
        "memory_growth_mib_total": memory_delta / (1024 * 1024),
        "memory_slope_mib_per_hour": slope_mib_hour,
        "linear_fit_r_squared": r2,
        "first_quarter_mean_mib": first_quarter_mean / (1024 * 1024),
        "last_quarter_mean_mib": last_quarter_mean / (1024 * 1024),
        "last_quarter_peak_mib": peak_after_last_quarter / (1024 * 1024),
        "interpretation": "investigate" if (slope_mib_hour or 0) > 30 or report["results"]["failed_requests"] else "monitor_only",
    }
    payload = {
        "schema_version": "n6_soak_analysis_v1",
        "source_report": str(args.report),
        "source_requests": str(args.requests),
        "results": report["results"],
        "integrity": report["integrity"],
        "memory_analysis": conclusion,
        "latency_by_hour": buckets,
    }
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
