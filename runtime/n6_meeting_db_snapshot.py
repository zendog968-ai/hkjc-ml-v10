#!/usr/bin/env python3
"""Create one immutable V10 SQLite snapshot for the active race meeting.

This evidence-only tool uses SQLite's online backup API.  It never changes the
source database, N6 service environment, model files, or model artifacts.  A
meeting snapshot is immutable: a same-manifest retry validates and returns the
first artifact; a different manifest hash for the same meeting refuses overwrite.
"""
from __future__ import annotations

import argparse
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

SCRIPT_VERSION = "n6-meeting-db-snapshot-v1"
UTC = dt.timezone.utc


def utc_now() -> str:
    return dt.datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def readonly_uri(path: Path, immutable: bool = False) -> str:
    suffix = "&immutable=1" if immutable else ""
    return f"file:{quote(str(path), safe='/')}?mode=ro{suffix}"


def atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def normalized_date(value: object) -> str:
    date = str(value or "").replace("/", "-")
    return dt.datetime.strptime(date, "%Y-%m-%d").strftime("%Y-%m-%d")


def sqlite_diagnostics(path: Path, immutable: bool) -> dict[str, object]:
    connection = sqlite3.connect(readonly_uri(path, immutable=immutable), uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        return {
            "integrity_check": integrity,
            "foreign_key_violations": foreign_keys,
            "page_count": int(connection.execute("PRAGMA page_count").fetchone()[0]),
            "page_size": int(connection.execute("PRAGMA page_size").fetchone()[0]),
            "journal_mode": str(connection.execute("PRAGMA journal_mode").fetchone()[0]),
        }
    finally:
        connection.close()


def meeting_from_manifest(manifest_path: Path) -> tuple[str, str, str]:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    meeting = payload.get("meeting") if isinstance(payload, dict) else None
    if not isinstance(meeting, dict):
        raise ValueError("manifest meeting object is required")
    date = normalized_date(meeting.get("race_date"))
    course = str(meeting.get("racecourse", "")).upper()
    if course not in {"ST", "HV"}:
        raise ValueError("manifest racecourse must be ST or HV")
    times = meeting.get("race_start_times")
    if not isinstance(times, dict) or not times:
        raise ValueError("manifest race_start_times is required")
    return date, course, sha256(manifest_path)


def snapshot_dir(root: Path, date: str, course: str) -> Path:
    year, month, day = date.split("-")
    return root / year / month / f"{day}_{course}"


def validate_existing(manifest_path: Path, manifest_sha: str, date: str, course: str) -> dict[str, object]:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "n6_meeting_db_snapshot_v1":
        raise ValueError("existing meeting snapshot has an unknown schema")
    meeting = payload.get("meeting")
    if not isinstance(meeting, dict) or meeting.get("race_date") != date or meeting.get("racecourse") != course:
        raise ValueError("existing snapshot meeting identity mismatch")
    if payload.get("schedule_manifest_sha256") != manifest_sha:
        raise ValueError("refusing to overwrite immutable meeting snapshot after manifest change")
    snap = Path(str(payload.get("snapshot_path", ""))).resolve(strict=True)
    if payload.get("snapshot_sha256") != sha256(snap):
        raise ValueError("existing snapshot SHA-256 mismatch")
    diagnostics = sqlite_diagnostics(snap, immutable=True)
    if diagnostics["integrity_check"] != "ok" or diagnostics["foreign_key_violations"] != 0:
        raise ValueError("existing snapshot fails SQLite integrity validation")
    return {"status": "existing", "snapshot": str(snap), "manifest": str(manifest_path), "snapshot_sha256": payload["snapshot_sha256"]}


def create_snapshot(source: Path, schedule_manifest: Path, root: Path) -> dict[str, object]:
    source = source.resolve(strict=True)
    schedule_manifest = schedule_manifest.resolve(strict=True)
    if source.suffix != ".sqlite":
        raise ValueError("source must be a .sqlite database")
    date, course, manifest_sha = meeting_from_manifest(schedule_manifest)
    directory = snapshot_dir(root.resolve(), date, course)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    manifest_path = directory / "meeting_db_snapshot.manifest.json"
    snapshot_path = directory / "meeting_prerace.sqlite"
    lock_path = directory / ".meeting-snapshot.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if manifest_path.exists() or snapshot_path.exists():
            if not manifest_path.exists() or not snapshot_path.exists():
                raise ValueError("partial meeting snapshot exists; refusing overwrite")
            return validate_existing(manifest_path, manifest_sha, date, course)
        source_info = sqlite_diagnostics(source, immutable=False)
        temporary = directory / f".{snapshot_path.name}.tmp.{os.getpid()}"
        source_connection = sqlite3.connect(readonly_uri(source), uri=True)
        target_connection = sqlite3.connect(temporary)
        try:
            source_connection.execute("PRAGMA query_only=ON")
            source_connection.backup(target_connection)
            target_connection.commit()
        finally:
            target_connection.close()
            source_connection.close()
        os.chmod(temporary, 0o600)
        snapshot_info = sqlite_diagnostics(temporary, immutable=True)
        if snapshot_info["integrity_check"] != "ok" or snapshot_info["foreign_key_violations"] != 0:
            temporary.unlink(missing_ok=True)
            raise RuntimeError("snapshot integrity or foreign-key check failed")
        os.replace(temporary, snapshot_path)
        snapshot_hash = sha256(snapshot_path)
        payload: dict[str, object] = {
            "schema_version": "n6_meeting_db_snapshot_v1",
            "creator_version": SCRIPT_VERSION,
            "created_at_utc": utc_now(),
            "purpose": "pre_race_immutable_evidence_only",
            "meeting": {"race_date": date, "racecourse": course},
            "schedule_manifest_path": str(schedule_manifest),
            "schedule_manifest_sha256": manifest_sha,
            "source_path": str(source),
            "source_sha256": sha256(source),
            "source_sqlite": source_info,
            "snapshot_path": str(snapshot_path),
            "snapshot_sha256": snapshot_hash,
            "snapshot_sqlite": snapshot_info,
            "n6_service_changed": False,
            "v10_database_changed": False,
            "immutable_after_creation": True,
        }
        atomic_write_json(manifest_path, payload)
    return {"status": "created", "snapshot": str(snapshot_path), "manifest": str(manifest_path), "snapshot_sha256": snapshot_hash}


def main() -> int:
    parser = argparse.ArgumentParser(description="Create an immutable evidence-only database snapshot for the active meeting.")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--schedule-manifest", type=Path, required=True)
    parser.add_argument("--snapshot-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(create_snapshot(args.source, args.schedule_manifest, args.snapshot_root), ensure_ascii=False, sort_keys=True))
    except (OSError, ValueError, sqlite3.Error, json.JSONDecodeError, RuntimeError) as error:
        print(f"MEETING_SNAPSHOT_REFUSED: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
