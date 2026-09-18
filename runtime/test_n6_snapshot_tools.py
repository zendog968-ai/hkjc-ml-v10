#!/usr/bin/env python3
"""Isolated smoke test for candidate snapshot creation and atomic candidate-link swap."""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / "runtime" / "n6_snapshot_tool_test"
SOURCE = TEST_ROOT / "source.sqlite"
SNAPSHOTS = TEST_ROOT / "snapshots"


def run(*args: str) -> str:
    completed = subprocess.run(args, cwd=ROOT, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"command failed: {args}\nstdout={completed.stdout}\nstderr={completed.stderr}")
    return completed.stdout.strip()


def main() -> int:
    shutil.rmtree(TEST_ROOT, ignore_errors=True)
    SNAPSHOTS.mkdir(parents=True)
    connection = sqlite3.connect(SOURCE)
    try:
        connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO sample(value) VALUES ('isolated-test')")
        connection.commit()
    finally:
        connection.close()

    snapshot_output = run(
        str(ROOT / ".venv" / "bin" / "python"),
        str(ROOT / "n6_db_snapshot_create.py"),
        "--source", str(SOURCE),
        "--snapshot-dir", str(SNAPSHOTS),
        "--as-of-utc", "2026-08-27T00:00:00Z",
    )
    snapshot_result = json.loads(snapshot_output)
    snapshot = Path(snapshot_result["snapshot"])
    manifest = Path(snapshot_result["manifest"])
    if not snapshot.is_file() or not manifest.is_file():
        raise RuntimeError("snapshot artifact missing")

    swap_output = run(
        str(ROOT / ".venv" / "bin" / "python"),
        str(ROOT / "n6_db_snapshot_atomic_swap.py"),
        "--snapshot", str(snapshot),
        "--snapshot-dir", str(SNAPSHOTS),
        "--approve-candidate-symlink-swap",
    )
    swap_result = json.loads(swap_output)
    candidate_link = Path(swap_result["candidate_link"])
    if not candidate_link.is_symlink() or candidate_link.resolve() != snapshot.resolve():
        raise RuntimeError("candidate link did not point to validated snapshot")
    snapshot_connection = sqlite3.connect(f"file:{snapshot}?mode=ro&immutable=1", uri=True)
    try:
        value = snapshot_connection.execute("SELECT value FROM sample").fetchone()[0]
    finally:
        snapshot_connection.close()
    if value != "isolated-test":
        raise RuntimeError("snapshot content mismatch")
    print(json.dumps({"status": "ok", "candidate_link": str(candidate_link), "n6_service_changed": False}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
