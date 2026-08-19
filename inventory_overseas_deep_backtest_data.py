#!/usr/bin/env python3
"""Read-only inventory for strict overseas deep-score proxy backtesting."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def count_table(conn: sqlite3.Connection, table: str) -> int | None:
    try:
        return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    except sqlite3.Error:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="盤點海外深度代理無未來資料回測所需資料。")
    parser.add_argument('--db', default='overseas_deep_racing.sqlite')
    parser.add_argument('--runtime-dir', default='runtime/overseas_deep')
    parser.add_argument('--output', default='reports/overseas_deep/OVERSEAS_DEEP_BACKTEST_INVENTORY.json')
    args = parser.parse_args()
    db_path = Path(args.db)
    runtime = Path(args.runtime_dir)
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True) if db_path.exists() else None
    tables: dict[str, int | None] = {}
    if conn is not None:
        names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        tables = {name: count_table(conn, name) for name in names}
        conn.close()
    artifacts = []
    for path in sorted(runtime.glob('*_deep.json')):
        try:
            item = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue
        artifacts.append({
            'path': str(path),
            'date': (item.get('race') or {}).get('meeting_date'),
            'race_no': (item.get('race') or {}).get('race_no'),
            'status': (item.get('scrape_run') or {}).get('status'),
            'has_immutable_pre_race_timestamp': bool((item.get('scrape_run') or {}).get('captured_at_utc')),
            'has_official_result': bool(item.get('official_result')),
            'has_model_or_feature_provenance': bool(item.get('feature_provenance') or item.get('scrape_run')),
        })
    strict_complete = [row for row in artifacts if row['has_immutable_pre_race_timestamp'] and row['has_official_result'] and row['has_model_or_feature_provenance']]
    payload = {
        'status': 'ready' if len(strict_complete) >= 15 else 'not_ready',
        'reason': '需要至少15場具不可變賽前深度工件、來源／特徵版本及官方賽果的同一研究契約事件；否則不得以賽後重抓資料回測。',
        'sqlite_tables': tables,
        'runtime_artifacts': artifacts,
        'strict_complete_event_count': len(strict_complete),
        'exploratory_threshold': 15,
        'next_data_contract': {
            'required': [
                'immutable_pre_race_capture_timestamp',
                'raw_source_hashes_and_urls',
                'deep_score_version_and_feature_availability',
                'official_result_finish_order',
                'same_day_market_snapshot_optional_for_roi',
            ],
            'prohibited': ['post_race_re-scrape_as_pre_race_features', 'mixing_feature_versions_without_cohort_label'],
        },
    }
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': payload['status'], 'strict_complete_event_count': len(strict_complete), 'artifact_count': len(artifacts), 'output': str(out)}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
