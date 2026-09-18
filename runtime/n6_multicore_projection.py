#!/usr/bin/env python3
"""Compute transparent, non-deployment N6 capacity projections from measured load."""
from __future__ import annotations

import json
from pathlib import Path

SOURCE = Path("runtime/n6_backpressure_resource_profile.json")
OUTPUT = Path("runtime/n6_multicore_projection.json")


def main() -> int:
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    resources = source["resource_summary"]
    current_cores = 2
    target_cores = 4
    workers_current = 2
    workers_target = 4
    gate_per_worker = 2
    wall = float(source["wall_clock_seconds"])
    n6_cpu_delta_seconds = float(resources["n6_cpu_usage_usec"]["delta"]) / 1_000_000.0
    host_busy_percent = float(resources["host_cpu_busy_percent"])
    requests = int(source["requests"])
    measured_gate_p95 = float(source["gate_wait_ms"]["p95"])
    measured_client_p95 = float(source["client_latency_ms"]["p95"])

    n6_average_cores = n6_cpu_delta_seconds / wall
    host_busy_cores = current_cores * host_busy_percent / 100.0
    projected_host_busy_percent = host_busy_cores / target_cores * 100.0
    current_aggregate_gate = workers_current * gate_per_worker
    target_aggregate_gate = workers_target * gate_per_worker
    per_request_n6_cpu_ms = (n6_cpu_delta_seconds * 1000.0) / requests

    report = {
        "scope": "analytical_projection_only",
        "server_configuration_changed": False,
        "source_profile": str(SOURCE),
        "assumptions": [
            "The same 96-request, eight-client historical-probe workload is used.",
            "CPU work per request and non-N6 background load remain approximately constant.",
            "Worker scaling is bounded by database, memory bandwidth, Python scheduling and the Linux load balancer; it is not assumed to be perfectly linear.",
            "The projection estimates capacity ratios only. It does not claim a future p95 or p99 latency without measuring the target host.",
        ],
        "measured_2vcpu": {
            "clients": source["clients"],
            "requests": requests,
            "wall_clock_seconds": wall,
            "host_cpu_busy_percent": host_busy_percent,
            "n6_cpu_delta_seconds": n6_cpu_delta_seconds,
            "n6_average_cores": n6_average_cores,
            "n6_cpu_ms_per_request": per_request_n6_cpu_ms,
            "aggregate_gate_capacity": current_aggregate_gate,
            "client_p95_ms": measured_client_p95,
            "gate_wait_p95_ms": measured_gate_p95,
        },
        "candidate_4vcpu_4worker_gate2": {
            "workers": workers_target,
            "gate_per_worker": gate_per_worker,
            "aggregate_gate_capacity": target_aggregate_gate,
            "projected_host_busy_percent_if_workload_identical": projected_host_busy_percent,
            "queueing_interpretation": "Eight concurrent clients would match the proposed aggregate gate capacity instead of exceeding the current aggregate capacity of four. The measured 237.673ms p95 gate wait is therefore expected to be materially reduced, but target-host latency must be measured rather than inferred.",
            "not_projected": ["p95 latency", "p99 latency", "memory use", "database contention"],
        },
        "risk_note_per_worker_gate4": {
            "workers": workers_target,
            "gate_per_worker": 4,
            "aggregate_gate_capacity": workers_target * 4,
            "interpretation": "This would permit up to sixteen protected inference sections and is not a four-request setting. It should not be adopted without a bounded target-host soak test and a shared aggregate ingress gate.",
        },
    }
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
