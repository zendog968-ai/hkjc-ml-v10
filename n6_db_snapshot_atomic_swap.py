#!/usr/bin/env python3
"""Atomically repoint a candidate-only N6 database snapshot symlink.

The tool never changes N6_V10_DB_PATH, systemd settings, models, or the production
source database. It validates a snapshot manifest before atomically replacing only
runtime/n6_db_snapshots/current_candidate.sqlite.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

UTC = dt.timezone.utc
SCRIPT_VERSION = "n6-db-snapshot-atomic-swap-v1"


def utc_now() -> str:
    return dt.datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def immutable_uri(path: Path) -> str:
    return f"file:{quote(str(path), safe='/')}?mode=ro&immutable=1"


def validate_snapshot(snapshot: Path, snapshot_dir: Path) -> dict[str, object]:
    snapshot = snapshot.resolve(strict=True)
    snapshot_dir = snapshot_dir.resolve(strict=True)
    if snapshot.parent != snapshot_dir or snapshot.suffix != ".sqlite":
        raise ValueError("snapshot must be a direct .sqlite child of snapshot-dir")
    manifest_path = snapshot.with_suffix(".manifest.json")
    if not manifest_path.is_file():
        raise ValueError("snapshot manifest is required")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "n6_candidate_snapshot_manifest_v1" or manifest.get("candidate_only") is not True:
        raise ValueError("manifest is not a candidate snapshot manifest")
    if Path(str(manifest.get("snapshot_path", ""))).resolve() != snapshot:
        raise ValueError("manifest snapshot path mismatch")
    if manifest.get("snapshot_sha256") != sha256(snapshot):
        raise ValueError("snapshot SHA-256 mismatch")
    connection = sqlite3.connect(immutable_uri(snapshot), uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_count = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    finally:
        connection.close()
    if integrity != "ok" or foreign_key_count != 0:
        raise ValueError("snapshot fails integrity or foreign-key validation")
    return {"manifest_path": str(manifest_path), "snapshot_sha256": manifest["snapshot_sha256"]}


def load_ledger(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"schema": "n6_candidate_snapshot_link_ledger_v1", "events": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != "n6_candidate_snapshot_link_ledger_v1" or not isinstance(payload.get("events"), list):
        raise ValueError("existing link ledger has invalid schema")
    return payload


def write_atomic_json(path: Path, payload: dict[str, object]) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def atomic_swap(snapshot: Path, snapshot_dir: Path, link_name: str) -> dict[str, object]:
    if link_name != "current_candidate.sqlite":
        raise ValueError("only the fixed candidate link name is allowed")
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(snapshot_dir, 0o700)
    snapshot_dir = snapshot_dir.resolve(strict=True)
    validation = validate_snapshot(snapshot, snapshot_dir)
    link = snapshot_dir / link_name
    prior_target = os.readlink(link) if link.is_symlink() else None
    if link.exists() and not link.is_symlink():
        raise ValueError("candidate link path exists but is not a symlink")
    relative_target = os.path.relpath(snapshot.resolve(strict=True), start=snapshot_dir)
    temporary_link = snapshot_dir / f".{link_name}.new.{os.getpid()}"
    os.symlink(relative_target, temporary_link)
    os.replace(temporary_link, link)
    ledger_path = snapshot_dir / "candidate_link_ledger.json"
    ledger = load_ledger(ledger_path)
    ledger["events"].append({
        "at_utc": utc_now(),
        "event": "candidate_atomic_symlink_swap",
        "prior_target": prior_target,
        "new_target": relative_target,
        "snapshot_sha256": validation["snapshot_sha256"],
        "manifest_path": validation["manifest_path"],
        "n6_service_changed": False,
        "script_version": SCRIPT_VERSION,
    })
    write_atomic_json(ledger_path, ledger)
    return {"candidate_link": str(link), "target": relative_target, "prior_target": prior_target, "n6_service_changed": False}


def main() -> int:
    parser = argparse.ArgumentParser(description="Atomically swap only the candidate N6 SQLite snapshot link.")
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--snapshot-dir", type=Path, required=True)
    parser.add_argument("--approve-candidate-symlink-swap", action="store_true")
    args = parser.parse_args()
    if not args.approve_candidate_symlink_swap:
        print("SWAP_REFUSED: explicit --approve-candidate-symlink-swap is required", file=sys.stderr)
        return 2
    try:
        result = atomic_swap(args.snapshot, args.snapshot_dir, "current_candidate.sqlite")
    except (OSError, ValueError, sqlite3.Error, json.JSONDecodeError) as error:
        print(f"SWAP_REFUSED: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
