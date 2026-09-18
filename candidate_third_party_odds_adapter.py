"""Candidate-only adapter for a public third-party odds DOM snapshot.

This module never fetches, authenticates, or writes production data. It parses a
previously captured, sanitized table snapshot and fails closed on ambiguity.
"""
from __future__ import annotations
import json
import math
from datetime import datetime
import re
import sys
from pathlib import Path
from typing import Any


def positive_number(value: Any) -> float | None:
    try:
        x = float(str(value).replace('\u00a0', '').strip())
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x > 0 else None


def parse_third_party_snapshot(snapshot: dict[str, Any], expected_race_no: int, expected_date: str = '2026/09/13', expected_course: str = 'ST', race_card_path: str | None = None) -> dict[str, Any]:
    rows = []
    tables = snapshot.get('tables', []) if isinstance(snapshot, dict) else []
    target_text = ' '.join(str(snapshot.get(k, '')) for k in ('target', 'final_url', 'page_url'))
    try:
        dt = datetime.strptime(expected_date, '%Y/%m/%d')
        date_tokens = {dt.strftime('%Y-%m-%d'), dt.strftime('%d-%m-%Y'), expected_date}
    except ValueError:
        date_tokens = {expected_date}
    source_identity_ok = any(token in target_text for token in date_tokens) and expected_course.upper() in target_text.upper() and f'raceno={expected_race_no}' in target_text.lower()
    formal_identity_ok = True
    formal_pairs = []
    if race_card_path:
        card = json.loads(Path(race_card_path).read_text(encoding='utf-8'))
        for r in card.get('runners', []):
            try: n = int(r.get('horse_no', r.get('horse_number')))
            except (TypeError, ValueError): continue
            name = str(r.get('horse_name', '')).strip()
            if name: formal_pairs.append((n, name))
        formal_pairs = sorted(formal_pairs)
        formal_identity_ok = bool(formal_pairs)

    # The mobile page may render nested/duplicate tables. Choose the table with
    # the largest number of complete horse rows, not the first matching wrapper.
    candidates = []
    for table in tables:
        table_rows = table.get('rows', []) if isinstance(table, dict) else []
        if not any(isinstance(row, list) and [str(x).strip() for x in row[:6]] == ['馬號', '膽', '腳', '馬名', '獨贏', '位置'] for row in table_rows):
            continue
        score = 0
        for row in table_rows:
            if not isinstance(row, list) or len(row) != 6:
                continue
            try:
                int(str(row[0]).strip())
            except (TypeError, ValueError):
                continue
            if positive_number(row[4]) is not None and positive_number(row[5]) is not None and str(row[3]).strip():
                score += 1
        candidates.append((score, table_rows))
    selected_table = max(candidates, key=lambda x: (x[0], -len(x[1])))[1] if candidates else []
    for row in selected_table:
        if not isinstance(row, list) or len(row) < 6:
            continue
        header = [str(x).strip() for x in row[:6]]
        if header == ['馬號', '膽', '腳', '馬名', '獨贏', '位置']:
            continue
        try:
            horse_no = int(str(row[0]).strip())
        except (TypeError, ValueError):
            continue
        win = positive_number(row[4])
        pla = positive_number(row[5])
        horse_name = str(row[3]).strip()
        if horse_no <= 0 or win is None or pla is None or not horse_name:
            continue
        rows.append({'horse_no': horse_no, 'horse_name': horse_name, 'win_odds': win, 'place_odds': pla})
    by_no = {}
    duplicate = False
    for row in rows:
        if row['horse_no'] in by_no:
            duplicate = True
        by_no[row['horse_no']] = row
    rows = [by_no[k] for k in sorted(by_no)]
    third_pairs = [(r['horse_no'], r['horse_name']) for r in rows]
    if formal_pairs:
        formal_identity_ok = third_pairs == formal_pairs
    checks = {
        'source_snapshot_present': bool(tables),
        'source_manifest_identity': source_identity_ok,
        'race_no_bound': int(expected_race_no) > 0,
        'race_card_identity_1_to_1': formal_identity_ok,
        'no_duplicate_horse_no': not duplicate,
        'all_rows_complete': bool(rows) and all(r['horse_name'] and r['win_odds'] > 0 and r['place_odds'] > 0 for r in rows),
        'horse_numbers_contiguous': bool(rows) and [r['horse_no'] for r in rows] == list(range(1, len(rows) + 1)),
    }
    ok = all(checks.values())
    return {
        'status': 'ok' if ok else 'degraded',
        'odds': rows if ok else [],
        'metadata': {
            'source': 'third_party_public_dom',
            'race_no': int(expected_race_no),
            'field_size': len(rows),
            'source_manifest_identity': source_identity_ok,
            'race_card_identity_1_to_1': formal_identity_ok,
            'complete_win_place_pairs': len(rows) if ok else 0,
            'checks': checks,
        },
    }


if __name__ == '__main__':
    source = Path(sys.argv[1])
    race_no = int(sys.argv[2])
    expected_date = sys.argv[3] if len(sys.argv) > 3 else '2026/09/13'
    expected_course = sys.argv[4] if len(sys.argv) > 4 else 'ST'
    race_card = sys.argv[5] if len(sys.argv) > 5 else None
    result = parse_third_party_snapshot(json.loads(source.read_text(encoding='utf-8')), race_no, expected_date, expected_course, race_card)
    print(json.dumps(result, ensure_ascii=False, indent=2))
