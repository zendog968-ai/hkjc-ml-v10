#!/usr/bin/env python3
"""Read-only query of P1 candidate trial batch for Cala Dei Mori (L428)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from statistics import mean, median

ROOT = Path('/home/ubuntu/hkjc_v10_database')
DB = ROOT / 'runtime/preseason_candidate/p1_preseason_candidate.sqlite'

with sqlite3.connect(f'file:{DB}?mode=ro', uri=True) as conn:
    conn.row_factory = sqlite3.Row
    batch = conn.execute(
        """
        SELECT b.*
        FROM trial_batches b
        JOIN trial_entries e ON e.trial_batch_id = b.trial_batch_id
        WHERE e.horse_code = ?
        """,
        ('L428',),
    ).fetchone()
    entries = conn.execute(
        """
        SELECT horse_code, horse_name, finish_rank, finish_time_seconds,
               time_vs_batch_seconds, finish_rank_pct, lbw, lbw_raw,
               running_position_json, final_sectional_vs_batch_seconds, comment
        FROM trial_entries
        WHERE trial_batch_id = ?
        ORDER BY finish_rank, finish_time_seconds, horse_code
        """,
        (batch['trial_batch_id'],),
    ).fetchall()

rows = [dict(row) for row in entries]
times = [float(row['finish_time_seconds']) for row in rows]
target = next(row for row in rows if row['horse_code'] == 'L428')
sectionals = json.loads(batch['sectional_json'])

print(json.dumps({
    'database_open_mode': 'read_only_candidate_sqlite',
    'batch': {
        'trial_date': batch['trial_date'],
        'venue': batch['venue'],
        'surface': batch['surface'],
        'going': batch['going'],
        'distance_m': batch['distance_m'],
        'batch_label': batch['batch_label'],
        'batch_time_seconds': batch['batch_time_seconds'],
        'sectionals': sectionals,
        'sectionals_sum_seconds': round(sum(sectionals), 3),
    },
    'cala_dei_mori': target,
    'batch_time_distribution_seconds': {
        'count': len(times),
        'fastest': min(times),
        'median': median(times),
        'mean': round(mean(times), 3),
        'slowest': max(times),
        'cala_vs_median': round(float(target['finish_time_seconds']) - median(times), 3),
        'cala_vs_mean': round(float(target['finish_time_seconds']) - mean(times), 3),
    },
    'entries': rows,
}, ensure_ascii=False, indent=2))
