#!/usr/bin/env python3
"""Read-only N6 historical inference stress test.

The test drives only N6's loopback historical endpoint.  N6 itself opens the V10
SQLite source with immutable read-only settings; this script additionally records
the V10 database SHA-256 before and after to prove no file mutation occurred.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

N6_ROOT = Path(__file__).resolve().parent
DEFAULT_CSV = N6_ROOT / "reports" / "n6_test_predictions.csv"
DEFAULT_DB = Path("/home/ubuntu/hkjc_v10_database/hkjc_last_season.sqlite")
DEFAULT_REPORT_DIR = N6_ROOT / "reports" / "stress"


@dataclass(frozen=True)
class RaceCase:
    race_date: str
    racecourse: str
    race_no: int
    expected_runners: int

    @property
    def key(self) -> str:
        return f"{self.race_date}/{self.racecourse}/{self.race_no}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_race_cases(csv_path: Path) -> list[RaceCase]:
    counts: dict[tuple[str, str, int], int] = defaultdict(int)
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"race_date", "racecourse", "race_no"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Test predictions CSV lacks required fields: {sorted(missing)}")
        for row in reader:
            key = (str(row["race_date"]), str(row["racecourse"]), int(row["race_no"]))
            counts[key] += 1
    if not counts:
        raise ValueError("Test predictions CSV contains no historical race cases.")
    return [RaceCase(date, course, no, count) for (date, course, no), count in sorted(counts.items())]


def request_case(base_url: str, case: RaceCase, timeout_seconds: float) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/v1/inference/historical/{case.race_date}/{case.racecourse}/{case.race_no}"
    request = urllib.request.Request(url=url, method="POST", headers={"Accept": "application/json"})
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8")
            http_status = response.status
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        http_status = error.code
    except Exception as error:  # pragma: no cover - exercised in operational failure only
        return {
            "case": case,
            "ok": False,
            "http_status": None,
            "elapsed_ms": (time.perf_counter() - started) * 1000.0,
            "error": f"{type(error).__name__}: {error}",
        }

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as error:
        return {
            "case": case,
            "ok": False,
            "http_status": http_status,
            "elapsed_ms": elapsed_ms,
            "error": f"Invalid JSON: {error}",
        }

    if http_status != 200:
        return {
            "case": case,
            "ok": False,
            "http_status": http_status,
            "elapsed_ms": elapsed_ms,
            "error": payload.get("detail", body[:500]) if isinstance(payload, dict) else body[:500],
        }

    scores = payload.get("scores") if isinstance(payload, dict) else None
    if not isinstance(scores, list):
        return {
            "case": case,
            "ok": False,
            "http_status": http_status,
            "elapsed_ms": elapsed_ms,
            "error": "Response lacks scores list.",
        }

    probabilities: list[float] = []
    ranks: list[int] = []
    names: list[str] = []
    try:
        for row in scores:
            probabilities.append(float(row["neural_win_probability"]))
            ranks.append(int(row["neural_rank"]))
            names.append(str(row["horse_name"]))
    except (KeyError, TypeError, ValueError) as error:
        return {
            "case": case,
            "ok": False,
            "http_status": http_status,
            "elapsed_ms": elapsed_ms,
            "error": f"Invalid score row: {error}",
        }

    probability_sum = sum(probabilities)
    rank_ok = sorted(ranks) == list(range(1, len(scores) + 1))
    row_count_ok = len(scores) == case.expected_runners
    probability_ok = math.isfinite(probability_sum) and abs(probability_sum - 1.0) <= 1e-5
    signature = json.dumps(scores, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "case": case,
        "ok": row_count_ok and rank_ok and probability_ok,
        "http_status": http_status,
        "elapsed_ms": elapsed_ms,
        "error": None if (row_count_ok and rank_ok and probability_ok) else (
            f"row_count_ok={row_count_ok}; rank_ok={rank_ok}; probability_sum={probability_sum:.12f}"
        ),
        "probability_sum": probability_sum,
        "rank_ok": rank_ok,
        "row_count_ok": row_count_ok,
        "signature": signature,
        "top_horse": names[0] if names else None,
    }


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    index = (len(ordered) - 1) * fraction
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def markdown_report(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    validation = report["validation"]
    return f"""# N6 關閉 MKL-DNN 後歷史推論壓力測試

**測試時間（UTC）：** {report['generated_at_utc']}
**N6 端點：** `{report['base_url']}`
**資料集：** `{report['test_predictions_csv']}`
**模式：** 僅以 loopback `POST /v1/inference/historical/...` 進行測試；不寫入 V10 SQLite。

## 工作量

| 指標 | 結果 |
|---|---:|
| 唯一歷史賽事 | {report['workload']['unique_races']} |
| 每場重複 passes | {report['workload']['passes']} |
| 併發客戶端 | {report['workload']['concurrency']} |
| 總 HTTP 推論請求 | {report['workload']['total_requests']} |
| 成功請求 | {metrics['successful_requests']} |
| 失敗請求 | {metrics['failed_requests']} |

## 效能

| 指標 | 結果 |
|---|---:|
| 牆鐘時間 | {metrics['wall_seconds']:.3f} 秒 |
| 吞吐量 | {metrics['throughput_requests_per_second']:.2f} requests/s |
| 平均延遲 | {metrics['latency_mean_ms']:.3f} ms |
| P50 延遲 | {metrics['latency_p50_ms']:.3f} ms |
| P95 延遲 | {metrics['latency_p95_ms']:.3f} ms |
| P99 延遲 | {metrics['latency_p99_ms']:.3f} ms |
| 最大延遲 | {metrics['latency_max_ms']:.3f} ms |

## 正確性與隔離

| 檢查 | 結果 |
|---|---|
| 場內機率守恆 | {validation['probability_conservation_passed']} |
| 名次為 1..N 且無重複 | {validation['rank_integrity_passed']} |
| 回應馬匹數與保存測試集相符 | {validation['runner_count_passed']} |
| 重複推論輸出一致 | {validation['determinism_passed']} |
| V10 SQLite SHA-256 不變 | {validation['v10_sqlite_unchanged']} |
| 最大場內機率誤差 | {validation['max_probability_sum_error']:.12g} |

## 結論

本測試的整體判定為 **{report['overall_status']}**。關閉 MKL-DNN 是為了與 systemd `MemoryDenyWriteExecute=true` 的安全硬化相容；此測試可證明現行 N6 在真實歷史賽事工作量下的推論穩定性和 V10 SQLite 唯讀隔離，但不代表 N6 分數取代 V10.2 正式機率、EV 或 Kelly。
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Stress-test N6 historical inference through its loopback API.")
    parser.add_argument("--base-url", default="http://127.0.0.1:5001")
    parser.add_argument("--test-predictions-csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--v10-db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--passes", type=int, default=3, help="Repeat every unique historical race this many times.")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_REPORT_DIR)
    args = parser.parse_args()

    if args.passes < 1 or args.concurrency < 1:
        raise SystemExit("--passes and --concurrency must be positive integers.")
    if not args.test_predictions_csv.is_file() or not args.v10_db.is_file():
        raise SystemExit("Historical test CSV or V10 SQLite source file is missing.")

    cases = load_race_cases(args.test_predictions_csv)
    db_sha_before = sha256_file(args.v10_db)
    db_mtime_before = args.v10_db.stat().st_mtime_ns
    tasks = [(pass_index, case) for pass_index in range(1, args.passes + 1) for case in cases]
    started = time.perf_counter()
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        future_map = {pool.submit(request_case, args.base_url, case, args.timeout_seconds): (pass_index, case) for pass_index, case in tasks}
        for future in as_completed(future_map):
            pass_index, case = future_map[future]
            result = future.result()
            result["pass_index"] = pass_index
            result["case_key"] = case.key
            results.append(result)
    wall_seconds = time.perf_counter() - started
    db_sha_after = sha256_file(args.v10_db)
    db_mtime_after = args.v10_db.stat().st_mtime_ns

    successes = [result for result in results if result["ok"]]
    failures = [result for result in results if not result["ok"]]
    latencies = [float(result["elapsed_ms"]) for result in results]
    grouped_signatures: dict[str, set[str]] = defaultdict(set)
    for result in successes:
        grouped_signatures[result["case_key"]].add(str(result["signature"]))
    determinism_passed = all(len(signatures) == 1 for signatures in grouped_signatures.values()) and len(grouped_signatures) == len(cases)
    probability_errors = [abs(float(result.get("probability_sum", 0.0)) - 1.0) for result in successes]
    runner_count_passed = all(bool(result.get("row_count_ok")) for result in successes) and not failures
    rank_integrity_passed = all(bool(result.get("rank_ok")) for result in successes) and not failures
    probability_conservation_passed = all(error <= 1e-5 for error in probability_errors) and not failures
    v10_unchanged = db_sha_before == db_sha_after and db_mtime_before == db_mtime_after
    overall = all([not failures, determinism_passed, runner_count_passed, rank_integrity_passed, probability_conservation_passed, v10_unchanged])

    report = {
        "schema_version": "n6_historical_inference_stress_v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "base_url": args.base_url,
        "test_predictions_csv": str(args.test_predictions_csv),
        "v10_database": str(args.v10_db),
        "workload": {
            "unique_races": len(cases),
            "passes": args.passes,
            "concurrency": args.concurrency,
            "total_requests": len(tasks),
        },
        "metrics": {
            "wall_seconds": wall_seconds,
            "throughput_requests_per_second": len(tasks) / wall_seconds if wall_seconds else 0.0,
            "successful_requests": len(successes),
            "failed_requests": len(failures),
            "latency_mean_ms": statistics.mean(latencies),
            "latency_p50_ms": percentile(latencies, 0.50),
            "latency_p95_ms": percentile(latencies, 0.95),
            "latency_p99_ms": percentile(latencies, 0.99),
            "latency_max_ms": max(latencies),
        },
        "validation": {
            "probability_conservation_passed": probability_conservation_passed,
            "rank_integrity_passed": rank_integrity_passed,
            "runner_count_passed": runner_count_passed,
            "determinism_passed": determinism_passed,
            "v10_sqlite_unchanged": v10_unchanged,
            "v10_sqlite_sha256_before": db_sha_before,
            "v10_sqlite_sha256_after": db_sha_after,
            "max_probability_sum_error": max(probability_errors, default=float("nan")),
            "failure_examples": [
                {"race": result["case_key"], "pass": result["pass_index"], "status": result["http_status"], "error": result["error"]}
                for result in failures[:20]
            ],
        },
        "overall_status": "PASS" if overall else "FAIL",
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = args.output_dir / f"n6_historical_stress_{stamp}.json"
    md_path = args.output_dir / f"n6_historical_stress_{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(markdown_report(report), encoding="utf-8")
    print(json.dumps({"status": report["overall_status"], "report_json": str(json_path), "report_markdown": str(md_path), "metrics": report["metrics"], "validation": report["validation"]}, ensure_ascii=False))
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
