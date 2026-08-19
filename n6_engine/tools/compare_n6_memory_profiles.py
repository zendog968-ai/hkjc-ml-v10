#!/usr/bin/env python3
"""Compare controlled N6 memory warm-up profiles without modifying production data."""
from __future__ import annotations

import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any

MIB = 1024 * 1024
WINDOWS = ((0, 300), (300, 600), (600, 900), (900, 1201))
FIELDS = (
    "cgroup_current_bytes",
    "cgroup_anon_bytes",
    "cgroup_file_bytes",
    "sqlite_fds_total",
    "socket_fds_total",
)


def percentile(values: list[float], probability: float) -> float:
    if not values:
        return math.nan
    values = sorted(values)
    index = (len(values) - 1) * probability
    low = math.floor(index)
    high = math.ceil(index)
    if low == high:
        return values[low]
    return values[low] + (values[high] - values[low]) * (index - low)


def profile_summary(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    telemetry = payload["telemetry"]
    requests = payload["requests"]
    if not telemetry:
        raise ValueError(f"No telemetry in {path}")
    output: dict[str, Any] = {
        "source": str(path),
        "samples": len(telemetry),
        "requests": len(requests),
        "successful_requests": sum(bool(request.get("ok")) for request in requests),
        "metrics": {},
        "windows": [],
    }
    for field in FIELDS:
        values = [float(point[field]) for point in telemetry if point.get(field) is not None]
        output["metrics"][field] = {
            "first": values[0],
            "last": values[-1],
            "delta": values[-1] - values[0],
            "minimum": min(values),
            "maximum": max(values),
            "mean": statistics.fmean(values),
        }
    for start, end in WINDOWS:
        points = [point for point in telemetry if start <= point["elapsed_seconds"] < end]
        if len(points) < 2:
            continue
        duration = points[-1]["elapsed_seconds"] - points[0]["elapsed_seconds"]
        anon_delta = points[-1]["cgroup_anon_bytes"] - points[0]["cgroup_anon_bytes"]
        output["windows"].append(
            {
                "window_seconds": [start, end],
                "samples": len(points),
                "anon_delta_bytes": anon_delta,
                "anon_slope_mib_per_hour": (anon_delta / MIB) / (duration / 3600),
                "sqlite_fd_min": min(point["sqlite_fds_total"] for point in points),
                "sqlite_fd_max": max(point["sqlite_fds_total"] for point in points),
                "sqlite_fd_mean": statistics.fmean(point["sqlite_fds_total"] for point in points),
            }
        )
    latencies = [float(request["latency_ms"]) for request in requests if request.get("ok")]
    output["latency_ms"] = {
        "mean": statistics.fmean(latencies),
        "p95": percentile(latencies, 0.95),
        "maximum": max(latencies),
    }
    return output


def as_mib(value: float) -> float:
    return round(value / MIB, 3)


def main() -> None:
    before = profile_summary(Path(sys.argv[1]))
    after = profile_summary(Path(sys.argv[2]))
    comparison = {"before": before, "after": after}
    print(json.dumps(comparison, ensure_ascii=False, indent=2))
    print("\n# N6 controlled warm-up comparison\n")
    print("| 指標 | 修正前 | 明確 close() 後 | 差異（後－前） |")
    print("|---|---:|---:|---:|")
    for field, label, unit in (
        ("cgroup_current_bytes", "cgroup 總記憶體增量", "MiB"),
        ("cgroup_anon_bytes", "匿名記憶體增量", "MiB"),
        ("cgroup_file_bytes", "檔案頁增量", "MiB"),
        ("sqlite_fds_total", "SQLite FD 淨變動", "個"),
        ("socket_fds_total", "Socket FD 淨變動", "個"),
    ):
        b = before["metrics"][field]["delta"]
        a = after["metrics"][field]["delta"]
        if unit == "MiB":
            b, a = as_mib(b), as_mib(a)
        else:
            b, a = round(b), round(a)
        print(f"| {label} | {b} | {a} | {round(a - b, 3)} |")
    print("\n| 期間 | 修正前匿名記憶體斜率（MiB/h） | 修正後匿名記憶體斜率（MiB/h） | 修正前 SQLite FD 範圍 | 修正後 SQLite FD 範圍 |")
    print("|---|---:|---:|---:|---:|")
    for before_window, after_window in zip(before["windows"], after["windows"]):
        label = f"{before_window['window_seconds'][0] // 60}–{before_window['window_seconds'][1] // 60} 分鐘"
        b_range = f"{before_window['sqlite_fd_min']}–{before_window['sqlite_fd_max']}"
        a_range = f"{after_window['sqlite_fd_min']}–{after_window['sqlite_fd_max']}"
        print(
            f"| {label} | {before_window['anon_slope_mib_per_hour']:.2f} | "
            f"{after_window['anon_slope_mib_per_hour']:.2f} | {b_range} | {a_range} |"
        )
    print("\n| 推論品質 | 修正前 | 修正後 |")
    print("|---|---:|---:|")
    print(f"| 成功請求 | {before['successful_requests']}/{before['requests']} | {after['successful_requests']}/{after['requests']} |")
    print(f"| 平均延遲（ms） | {before['latency_ms']['mean']:.2f} | {after['latency_ms']['mean']:.2f} |")
    print(f"| P95 延遲（ms） | {before['latency_ms']['p95']:.2f} | {after['latency_ms']['p95']:.2f} |")
    print(f"| 最大延遲（ms） | {before['latency_ms']['maximum']:.2f} | {after['latency_ms']['maximum']:.2f} |")


if __name__ == "__main__":
    main()
