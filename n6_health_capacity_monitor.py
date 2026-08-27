#!/usr/bin/env python3
"""Read-only daily observability probe for the N6 internal inference service.

This program intentionally measures a small, bounded series of loopback
requests against one labelled historical race.  The resulting percentiles are
synthetic probe latency, not a substitute for real request telemetry.  It does
not read a future racecard, write V10/N6 data, change service configuration, or
transmit data externally.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import statistics
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "http://127.0.0.1:5001"
DEFAULT_TEST_CSV = Path("/home/ubuntu/n6_engine/reports/n6_test_predictions.csv")
DEFAULT_REPORT_DIR = Path("/home/ubuntu/hkjc_v10_database/runtime/n6_health_reports")
UNIT = "n6-engine.service"


def _percentile_nearest_rank(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, int((percentile / 100.0) * len(ordered) + 0.999999999))
    return ordered[min(rank, len(ordered)) - 1]


def _summary(values: list[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "min_ms": round(min(values), 3) if values else None,
        "p50_ms": round(_percentile_nearest_rank(values, 50.0), 3) if values else None,
        "p95_ms": round(_percentile_nearest_rank(values, 95.0), 3) if values else None,
        "p99_ms": round(_percentile_nearest_rank(values, 99.0), 3) if values else None,
        "mean_ms": round(statistics.fmean(values), 3) if values else None,
        "max_ms": round(max(values), 3) if values else None,
    }


def _request_json(url: str, method: str, timeout_seconds: float) -> tuple[int, float, dict[str, Any]]:
    started = time.perf_counter()
    request = urllib.request.Request(url, data=b"" if method == "POST" else None, method=method)
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # nosec B310: fixed loopback URL
        payload = json.loads(response.read().decode("utf-8"))
        status = int(response.status)
    return status, (time.perf_counter() - started) * 1000.0, payload


def _service_properties() -> dict[str, str]:
    command = [
        "systemctl", "show", UNIT,
        "-p", "ActiveState", "-p", "SubState", "-p", "NRestarts",
        "-p", "MemoryCurrent", "-p", "MemoryPeak", "-p", "CPUUsageNSec",
        "-p", "TasksCurrent", "--no-pager",
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=10)
    data: dict[str, str] = {"systemctl_returncode": str(completed.returncode)}
    for line in completed.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            data[key] = value
    return data


def _journal_counts() -> dict[str, int | bool]:
    command = ["journalctl", "-u", UNIT, "--since", "24 hours ago", "--no-pager", "-o", "cat"]
    completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=15)
    text = completed.stdout.lower()
    return {
        "accessible": completed.returncode == 0,
        "lines_24h": len(completed.stdout.splitlines()),
        "errors_24h": len(re.findall(r"\b(error|exception|traceback|critical|fatal|segmentation|killed)\b", text)),
        "warnings_24h": len(re.findall(r"\b(warn|warning|deprecat|oom|timeout|latency|queue|reject)\b", text)),
        "http_5xx_24h": len(re.findall(r"\b5\d\d\b", text)),
    }


def _meminfo() -> dict[str, int | None]:
    wanted = {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}
    result: dict[str, int | None] = {key: None for key in wanted}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        key, _, rest = line.partition(":")
        if key not in wanted:
            continue
        tokens = rest.split()
        if tokens:
            result[key] = int(tokens[0]) * 1024
    return result


def _pressure() -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for resource in ("cpu", "memory", "io"):
        content = Path(f"/proc/pressure/{resource}").read_text(encoding="utf-8")
        parsed: dict[str, float] = {}
        for line in content.splitlines():
            kind, *pairs = line.split()
            for pair in pairs:
                key, _, raw = pair.partition("=")
                if key in {"avg10", "avg60", "avg300"}:
                    parsed[f"{kind}_{key}"] = float(raw)
        result[resource] = parsed
    return result


def _select_historical_race(test_csv: Path) -> tuple[str, str, int, int]:
    with test_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        first = next(reader, None)
        if first is None:
            raise ValueError("N6 test-prediction CSV is empty")
        race_date = first["race_date"]
        racecourse = first["racecourse"]
        race_no = int(first["race_no"])
        count = 1
        for row in reader:
            if (row["race_date"], row["racecourse"], int(row["race_no"])) == (race_date, racecourse, race_no):
                count += 1
    return race_date, racecourse, race_no, count


def _run_probe(url: str, method: str, attempts: int, timeout_seconds: float) -> dict[str, Any]:
    latencies: list[float] = []
    statuses: list[int | str] = []
    errors: list[str] = []
    for _ in range(attempts):
        try:
            status, latency_ms, _payload = _request_json(url, method, timeout_seconds)
            statuses.append(status)
            latencies.append(latency_ms)
        except urllib.error.HTTPError as error:
            statuses.append(error.code)
            errors.append(f"http_{error.code}")
        except Exception as error:  # defensive monitoring boundary
            statuses.append("request_error")
            errors.append(type(error).__name__)
    return {
        "attempts": attempts,
        "status_counts": {str(key): statuses.count(key) for key in sorted(set(statuses), key=str)},
        "errors": sorted(set(errors)),
        "latency_samples_ms": [round(value, 3) for value in latencies],
        "latency": _summary(latencies),
    }


def _status(inference: dict[str, Any], service: dict[str, str], journal: dict[str, int | bool]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if service.get("ActiveState") != "active":
        reasons.append("service_not_active")
    if service.get("NRestarts", "0") != "0":
        reasons.append("unexpected_restart_count")
    if journal["errors_24h"] or journal["http_5xx_24h"]:
        reasons.append("journal_error_or_5xx")
    if inference["status_counts"].get("200", 0) != inference["attempts"]:
        reasons.append("probe_non_200")
    p95 = inference["latency"]["p95_ms"]
    p99 = inference["latency"]["p99_ms"]
    if p95 is not None and p95 > 750.0:
        reasons.append("synthetic_probe_p95_over_750ms")
    if p99 is not None and p99 > 900.0:
        reasons.append("synthetic_probe_p99_over_900ms")
    return ("warning" if reasons else "healthy"), reasons


def _markdown(report: dict[str, Any]) -> str:
    infer = report["synthetic_inference_probe"]
    health = report["synthetic_health_probe"]
    service = report["service"]
    capacity = report["capacity"]
    lines = [
        "# N6 每日健康與容量報告",
        "",
        f"- 產生時間（UTC）：`{report['created_at_utc']}`",
        f"- 狀態：**{report['status'].upper()}**",
        f"- 原因：`{', '.join(report['status_reasons']) if report['status_reasons'] else 'none'}`",
        "- 測量性質：小型、順序、loopback 合成探針；不是實際流量遙測。",
        "",
        "## 延遲探針",
        "",
        "| 項目 | 健康端點 GET | 歷史推論 POST |",
        "|---|---:|---:|",
        f"| 嘗試次數 | {health['attempts']} | {infer['attempts']} |",
        f"| HTTP 200 | {health['status_counts'].get('200', 0)} | {infer['status_counts'].get('200', 0)} |",
        f"| p50（ms） | {health['latency']['p50_ms']} | {infer['latency']['p50_ms']} |",
        f"| p95（ms） | {health['latency']['p95_ms']} | {infer['latency']['p95_ms']} |",
        f"| p99（ms） | {health['latency']['p99_ms']} | {infer['latency']['p99_ms']} |",
        f"| 最大值（ms） | {health['latency']['max_ms']} | {infer['latency']['max_ms']} |",
        "",
        "## 服務與容量",
        "",
        "| 指標 | 數值 |",
        "|---|---:|",
        f"| 服務狀態 | {service.get('ActiveState', 'unknown')} / {service.get('SubState', 'unknown')} |",
        f"| 非預期重啟 | {service.get('NRestarts', 'unknown')} |",
        f"| N6目前記憶體（bytes） | {service.get('MemoryCurrent', 'unknown')} |",
        f"| N6記憶體峰值（bytes） | {service.get('MemoryPeak', 'unknown')} |",
        f"| 主機可用記憶體（bytes） | {capacity['memory']['MemAvailable']} |",
        f"| 最近24小時應用錯誤 | {report['journal']['errors_24h']} |",
        f"| 最近24小時HTTP 5xx | {report['journal']['http_5xx_24h']} |",
        "",
        "> 此報告不會改變 N6 worker、模型、V10 SQLite 或預測輸出。若出現 warning，先檢視連續多日趨勢與服務日誌；不得由此自動重啟或擴容。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempts", type=int, default=20, choices=range(5, 51))
    parser.add_argument("--health-attempts", type=int, default=8, choices=range(3, 31))
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--test-csv", type=Path, default=DEFAULT_TEST_CSV)
    args = parser.parse_args()

    if not args.test_csv.is_file():
        raise SystemExit(f"controlled test labels unavailable: {args.test_csv}")
    if not args.base_url.startswith("http://127.0.0.1:"):
        raise SystemExit("refusing non-loopback base URL")

    race_date, racecourse, race_no, runner_count = _select_historical_race(args.test_csv)
    created_at = datetime.now(timezone.utc)
    service_before = _service_properties()
    journal = _journal_counts()
    health_probe = _run_probe(f"{args.base_url}/health", "GET", args.health_attempts, args.timeout_seconds)
    inference_probe = _run_probe(
        f"{args.base_url}/v1/inference/historical/{race_date}/{racecourse}/{race_no}",
        "POST", args.attempts, args.timeout_seconds,
    )
    service_after = _service_properties()
    status, reasons = _status(inference_probe, service_after, journal)
    report = {
        "schema_version": 1,
        "created_at_utc": created_at.isoformat(timespec="seconds"),
        "status": status,
        "status_reasons": reasons,
        "probe_contract": {
            "scope": "loopback_only",
            "target": "labelled_historical_race",
            "race": {"race_date": race_date, "racecourse": racecourse, "race_no": race_no, "runner_count": runner_count},
            "real_traffic_telemetry": False,
            "writes_performed_to_v10_or_n6": 0,
            "service_configuration_changed": False,
        },
        "synthetic_health_probe": health_probe,
        "synthetic_inference_probe": inference_probe,
        "service_before": service_before,
        "service": service_after,
        "journal": journal,
        "capacity": {"memory": _meminfo(), "pressure": _pressure(), "cpu_count": os.cpu_count()},
        "thresholds_ms": {"p95_warning": 750.0, "p99_warning": 900.0},
    }
    args.report_dir.mkdir(parents=True, exist_ok=True)
    stamp = created_at.strftime("%Y%m%dT%H%M%SZ")
    json_path = args.report_dir / f"n6_health_{stamp}.json"
    markdown_path = args.report_dir / f"n6_health_{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(_markdown(report), encoding="utf-8")
    print(json.dumps({"status": status, "json_report": str(json_path), "markdown_report": str(markdown_path), "reasons": reasons}, ensure_ascii=False))
    return 0 if status in {"healthy", "warning"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
