#!/usr/bin/env python3
"""Profile N6 loopback historical endpoint tail latency using real saved race cases.

This script does not write to V10 or change N6 service configuration.  It compares
one-client and concurrent-client request modes to determine whether tail latency
is primarily caused by queueing/CPU contention rather than a particular race.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_CSV = ROOT / "reports" / "n6_test_predictions.csv"
DEFAULT_OUTPUT = ROOT / "reports" / "stress"


def race_cases(path: Path) -> list[dict[str, Any]]:
    counts: dict[tuple[str, str, int], int] = defaultdict(int)
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source):
            counts[(row["race_date"], row["racecourse"], int(row["race_no"]))] += 1
    return [
        {"race_date": date, "racecourse": course, "race_no": race_no, "runner_count": runners, "race_key": f"{date}/{course}/{race_no}"}
        for (date, course, race_no), runners in sorted(counts.items())
    ]


def invoke(base_url: str, case: dict[str, Any], mode: str, timeout: float) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/v1/inference/historical/{case['race_date']}/{case['racecourse']}/{case['race_no']}"
    started = time.perf_counter()
    try:
        request = urllib.request.Request(url, method="POST", headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
            status = response.status
            error = None
    except urllib.error.HTTPError as exc:
        status = exc.code
        error = exc.read().decode("utf-8", errors="replace")[:300]
        payload = None
    except Exception as exc:  # pragma: no cover - operational transport boundary
        status = None
        error = f"{type(exc).__name__}: {exc}"
        payload = None
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    score_count = len(payload.get("scores", [])) if isinstance(payload, dict) else 0
    return {**case, "mode": mode, "http_status": status, "error": error, "elapsed_ms": elapsed_ms, "score_count": score_count}


def percentile(values: list[float], q: float) -> float:
    values = sorted(values)
    i = (len(values) - 1) * q
    lo, hi = math.floor(i), math.ceil(i)
    return values[lo] if lo == hi else values[lo] + (values[hi] - values[lo]) * (i - lo)


def summarise(results: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [float(row["elapsed_ms"]) for row in results if row["http_status"] == 200 and row["error"] is None]
    slowest = sorted(results, key=lambda row: float(row["elapsed_ms"]), reverse=True)[:15]
    return {
        "requests": len(results),
        "successes": len(valid),
        "failures": len(results) - len(valid),
        "mean_ms": statistics.mean(valid) if valid else None,
        "p50_ms": percentile(valid, 0.50) if valid else None,
        "p95_ms": percentile(valid, 0.95) if valid else None,
        "p99_ms": percentile(valid, 0.99) if valid else None,
        "max_ms": max(valid) if valid else None,
        "slowest": slowest,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Profile N6 historical endpoint tail latency.")
    parser.add_argument("--base-url", default="http://127.0.0.1:5001")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    cases = race_cases(args.csv)
    if not cases:
        raise SystemExit("No historical race cases discovered.")

    sequential = [invoke(args.base_url, case, "sequential", args.timeout) for case in cases]
    concurrent: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(invoke, args.base_url, case, f"concurrent_{args.concurrency}", args.timeout) for case in cases]
        for future in as_completed(futures):
            concurrent.append(future.result())

    report = {
        "schema_version": "n6_tail_latency_profile_v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "workload": {"unique_races": len(cases), "concurrency": args.concurrency, "source_csv": str(args.csv)},
        "sequential": summarise(sequential),
        "concurrent": summarise(concurrent),
        "results": sequential + concurrent,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = args.output_dir / f"n6_tail_latency_profile_{stamp}.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(destination), "sequential": report["sequential"], "concurrent": report["concurrent"]}, ensure_ascii=False))
    return 0 if report["sequential"]["failures"] == 0 and report["concurrent"]["failures"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
