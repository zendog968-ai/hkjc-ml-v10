#!/usr/bin/env python3
"""Read-only high-frequency N6 worker memory warm-up / plateau profiler."""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
SOURCE_CSV = ROOT / "reports" / "n6_test_predictions.csv"
CGROUP = Path("/sys/fs/cgroup/system.slice/n6-engine.service")


def read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (FileNotFoundError, PermissionError, ValueError):
        return None


def worker_pids() -> list[int]:
    main = subprocess.run(["systemctl", "show", "n6-engine.service", "-p", "MainPID", "--value"], text=True, capture_output=True, check=False)
    try:
        master = int(main.stdout.strip())
    except ValueError:
        return []
    children = subprocess.run(["pgrep", "-P", str(master)], text=True, capture_output=True, check=False)
    return [int(line) for line in children.stdout.split() if line.strip().isdigit()]


def smaps(pid: int) -> dict[str, int]:
    wanted = {"Pss", "Pss_Anon", "Pss_File", "Private_Dirty", "Private_Clean", "Shared_Clean"}
    output: dict[str, int] = {}
    try:
        for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
            match = re.match(r"^(\w+):\s+(\d+)\s+kB$", line)
            if match and match.group(1) in wanted:
                output[match.group(1)] = int(match.group(2)) * 1024
    except (FileNotFoundError, PermissionError):
        pass
    return output


def fd_counts(pid: int) -> tuple[int, int]:
    try:
        links = [os.readlink(f"/proc/{pid}/fd/{entry}") for entry in os.listdir(f"/proc/{pid}/fd")]
    except (FileNotFoundError, PermissionError):
        return 0, 0
    return sum("hkjc_last_season.sqlite" in item for item in links), sum(item.startswith("socket:") for item in links)


def snapshot(elapsed: float) -> dict:
    workers = []
    for pid in worker_pids():
        values = smaps(pid)
        sqlite_fds, socket_fds = fd_counts(pid)
        workers.append({"pid": pid, **values, "sqlite_fds": sqlite_fds, "socket_fds": socket_fds})
    fields = ["Pss", "Pss_Anon", "Pss_File", "Private_Dirty", "Private_Clean", "Shared_Clean"]
    return {
        "elapsed_seconds": elapsed,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "cgroup_current_bytes": read_int(CGROUP / "memory.current"),
        "cgroup_anon_bytes": next((int(line.split()[1]) for line in (CGROUP / "memory.stat").read_text().splitlines() if line.startswith("anon ")), None),
        "cgroup_file_bytes": next((int(line.split()[1]) for line in (CGROUP / "memory.stat").read_text().splitlines() if line.startswith("file ")), None),
        "workers": workers,
        "worker_totals": {field: sum(worker.get(field, 0) for worker in workers) for field in fields},
        "sqlite_fds_total": sum(worker["sqlite_fds"] for worker in workers),
        "socket_fds_total": sum(worker["socket_fds"] for worker in workers),
    }


def races() -> list[tuple[str, str, int]]:
    result = set()
    with SOURCE_CSV.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            result.add((row["race_date"], row["racecourse"], int(row["race_no"])))
    return sorted(result)


def request(race: tuple[str, str, int]) -> tuple[bool, float]:
    date, course, race_no = race
    started = time.perf_counter()
    try:
        with urlopen(Request(f"http://127.0.0.1:5001/v1/inference/historical/{date}/{course}/{race_no}", data=b"", method="POST"), timeout=15) as response:  # nosec B310
            payload = json.loads(response.read())
        values = payload.get("scores", [])
        ok = response.status == 200 and values and abs(sum(float(item.get("neural_win_probability", 0.0)) for item in values) - 1.0) <= 1e-5
        return bool(ok), (time.perf_counter() - started) * 1000
    except HTTPError:
        return False, (time.perf_counter() - started) * 1000


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration-seconds", type=int, default=1200)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--sample-seconds", type=float, default=5.0)
    parser.add_argument("--cycle-seconds", type=float, default=1.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.concurrency < 1 or args.concurrency > 2:
        raise ValueError("concurrency must be 1 or 2")
    source = races()
    started = time.monotonic()
    next_sample = started
    records: list[dict] = []
    telemetry: list[dict] = []
    index = 0
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        while time.monotonic() - started < args.duration_seconds:
            cycle_started = time.monotonic()
            batch = [source[(index + offset) % len(source)] for offset in range(args.concurrency)]
            index += args.concurrency
            for future in as_completed([pool.submit(request, race) for race in batch]):
                ok, latency = future.result()
                records.append({"elapsed_seconds": time.monotonic() - started, "ok": ok, "latency_ms": latency})
            now = time.monotonic()
            if now >= next_sample:
                telemetry.append(snapshot(now - started))
                next_sample = now + args.sample_seconds
            sleep_for = args.cycle_seconds - (time.monotonic() - cycle_started)
            if sleep_for > 0:
                time.sleep(sleep_for)
    args.output.write_text(json.dumps({"schema_version": "n6_memory_warmup_profile_v1", "workload": {"duration_seconds": args.duration_seconds, "concurrency": args.concurrency, "real_races": len(source)}, "requests": records, "telemetry": telemetry}, ensure_ascii=False, indent=2) + "\n")
    print(args.output)


if __name__ == "__main__":
    main()
