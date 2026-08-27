#!/usr/bin/env python3
"""Create a candidate-only immutable SQLite snapshot for future N6 data handoff.

This tool never changes the source database, N6 environment, service unit, model, or
symlinks. It uses SQLite's online backup API so a source in WAL mode is copied through
SQLite rather than by a blind filesystem copy.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

SCRIPT_VERSION = "n6-db-snapshot-create-v1"
UTC = dt.timezone.utc


def utc_now() -> str:
    return dt.datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> str:
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected ISO-8601 UTC timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise argparse.ArgumentTypeError("timestamp must be explicit UTC, for example 2026-08-28T00:00:00Z")
    if parsed > dt.datetime.now(UTC):
        raise argparse.ArgumentTypeError("as-of timestamp cannot be in the future")
    return parsed.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_only_uri(path: Path) -> str:
    return f"file:{quote(str(path), safe='/')}?mode=ro"


def sqlite_diagnostics(path: Path, immutable: bool) -> dict[str, object]:
    suffix = "&immutable=1" if immutable else ""
    connection = sqlite3.connect(read_only_uri(path) + suffix, uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
    finally:
        connection.close()
    return {
        "sqlite_integrity_check": integrity,
        "foreign_key_violations": len(foreign_keys),
        "page_count": page_count,
        "page_size": page_size,
        "user_version": user_version,
        "journal_mode": journal_mode,
    }


def write_atomic_json(path: Path, payload: dict[str, object]) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def create_snapshot(source: Path, snapshot_dir: Path, as_of_utc: str, creator_version: str) -> dict[str, object]:
    source = source.resolve(strict=True)
    if not source.is_file() or source.suffix != ".sqlite":
        raise ValueError("source must be an existing .sqlite file")
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(snapshot_dir, 0o700)
    snapshot_dir = snapshot_dir.resolve(strict=True)
    stamp = utc_now().replace(":", "").replace("-", "")
    snapshot_id = f"v10_{stamp}"
    snapshot = snapshot_dir / f"{snapshot_id}.sqlite"
    manifest = snapshot_dir / f"{snapshot_id}.manifest.json"
    if snapshot.exists() or manifest.exists():
        raise ValueError("refusing to overwrite existing snapshot or manifest")

    lock_path = snapshot_dir / ".snapshot-create.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        source_info = sqlite_diagnostics(source, immutable=False)
        temporary = snapshot_dir / f".{snapshot.name}.tmp.{os.getpid()}"
        source_conn = sqlite3.connect(read_only_uri(source), uri=True)
        target_conn = sqlite3.connect(temporary)
        try:
            source_conn.execute("PRAGMA query_only=ON")
            source_conn.backup(target_conn)
            target_conn.commit()
        finally:
            target_conn.close()
            source_conn.close()
        os.chmod(temporary, 0o600)
        snapshot_info = sqlite_diagnostics(temporary, immutable=True)
        if snapshot_info["sqlite_integrity_check"] != "ok" or snapshot_info["foreign_key_violations"] != 0:
            temporary.unlink(missing_ok=True)
            raise RuntimeError("refusing snapshot that fails SQLite integrity or foreign-key checks")
        os.replace(temporary, snapshot)
        payload: dict[str, object] = {
            "schema": "n6_candidate_snapshot_manifest_v1",
            "snapshot_id": snapshot_id,
            "created_at_utc": utc_now(),
            "as_of_utc": as_of_utc,
            "creator_version": creator_version,
            "source_path": str(source),
            "source_sha256": sha256(source),
            "source_sqlite": source_info,
            "snapshot_path": str(snapshot),
            "snapshot_sha256": sha256(snapshot),
            "snapshot_sqlite": snapshot_info,
            "n6_service_changed": False,
            "candidate_only": True,
        }
        write_atomic_json(manifest, payload)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return {"snapshot": str(snapshot), "manifest": str(manifest), "snapshot_sha256": payload["snapshot_sha256"]}


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a candidate-only consistent SQLite snapshot for N6 handoff.")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--snapshot-dir", type=Path, required=True)
    parser.add_argument("--as-of-utc", type=parse_utc, required=True)
    parser.add_argument("--creator-version", default=SCRIPT_VERSION)
    args = parser.parse_args()
    try:
        result = create_snapshot(args.source, args.snapshot_dir, args.as_of_utc, args.creator_version)
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as error:
        print(f"SNAPSHOT_REFUSED: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
