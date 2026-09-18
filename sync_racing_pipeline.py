#!/usr/bin/env python3
"""Formal read-only ONCC dual-source bridge into the isolated qualitative DB.

This bridge never imports or writes the V10/N6 model database. It reads the active
race-day manifest, invokes the candidate-validated ONCC fetcher, then ingests only
into runtime/oncc_extension/oncc_extension.sqlite. Any source/date/table mismatch
fails closed and produces no output artifact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / 'runtime' / 'pre_race_schedule_current.json'
FETCHER = ROOT / 'runtime' / 'candidate_refactor_20260906' / 'qualitative' / 'oncc_dual_fetcher_candidate.py'
DB = ROOT / 'runtime' / 'oncc_extension' / 'oncc_extension.sqlite'
OUT_DIR = ROOT / 'runtime' / 'oncc_extension'
PYTHON = ROOT / '.venv' / 'bin' / 'python'
TZ = ZoneInfo('Asia/Hong_Kong')


def read_identity(date_override: str | None, course_override: str | None, races_override: int | None) -> tuple[str, str, int]:
    data = json.loads(MANIFEST.read_text(encoding='utf-8'))
    meeting = data.get('meeting', {})
    raw_date = date_override or meeting.get('race_date')
    course = (course_override or meeting.get('racecourse') or '').upper()
    races = races_override or int(meeting.get('race_count') or 0)
    if not isinstance(raw_date, str) or not raw_date:
        raise ValueError('manifest race_date missing')
    date_iso = raw_date.replace('/', '-')
    datetime.strptime(date_iso, '%Y-%m-%d')
    if course not in {'ST', 'HV'} or races <= 0:
        raise ValueError('manifest course/race_count invalid')
    return date_iso, course, races


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def run(date_override: str | None, course_override: str | None, races_override: int | None, dry_run: bool) -> int:
    date_iso, course, race_count = read_identity(date_override, course_override, races_override)
    if date_override is None and datetime.now(TZ).date().isoformat() != date_iso:
        print(json.dumps({'status': 'skipped', 'reason': 'manifest_not_current_hkt_date', 'manifest_date': date_iso, 'today_hkt': datetime.now(TZ).date().isoformat()}, ensure_ascii=False))
        return 0
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    target = OUT_DIR / f'oncc_qualitative_{date_iso.replace("-", "")}_{course}.json'
    with tempfile.TemporaryDirectory(prefix='oncc-fetch-', dir=OUT_DIR) as temp_dir:
        temp_output = Path(temp_dir) / target.name
        command = [str(PYTHON), str(FETCHER), '--date', date_iso, '--course', course, '--races', str(race_count), '--output', str(temp_output)]
        fetched = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=600)
        if fetched.returncode != 0 or not temp_output.exists():
            print(fetched.stderr or fetched.stdout, file=sys.stderr)
            print(json.dumps({'status': 'degraded', 'reason': 'candidate_fetch_failed', 'date': date_iso, 'course': course}, ensure_ascii=False), file=sys.stderr)
            return 2
        payload = json.loads(temp_output.read_text(encoding='utf-8'))
        if not isinstance(payload, list) or not payload:
            raise ValueError('empty ONCC payload')
        keys = {(row.get('race_date'), row.get('race_no'), row.get('horse_no')) for row in payload}
        if len(keys) != len(payload) or any(row.get('race_date') != date_iso for row in payload):
            raise ValueError('ONCC primary-key/date alignment failed')
        raw = temp_output.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if dry_run:
            print(json.dumps({'status': 'ok', 'dry_run': True, 'date': date_iso, 'course': course, 'records': len(payload), 'sha256': digest, 'output': str(target)}, ensure_ascii=False))
            return 0
        atomic_write(target, raw)
    ingest = subprocess.run([str(PYTHON), str(ROOT / 'ingest_oncc_json.py'), '--input', str(target), '--db', str(DB)], cwd=ROOT, capture_output=True, text=True, timeout=120)
    if ingest.returncode != 0:
        print(ingest.stderr or ingest.stdout, file=sys.stderr)
        return 2
    print(json.dumps({'status': 'ok', 'date': date_iso, 'course': course, 'records': len(payload), 'sha256': digest, 'json_output': str(target), 'db': str(DB), 'ingest': ingest.stdout.strip(), 'n6_status': 'extension_only', 'formal_v10_changed': False}, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', help='YYYY-MM-DD override for a controlled historical dry-run')
    parser.add_argument('--course', choices=['ST', 'HV'])
    parser.add_argument('--races', type=int)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    try:
        return run(args.date, args.course, args.races, args.dry_run)
    except Exception as exc:
        print(json.dumps({'status': 'degraded', 'error': str(exc), 'formal_v10_changed': False}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
