#!/usr/bin/env python3
import json
import sqlite3
from pathlib import Path

DB = Path('/home/ubuntu/hkjc_v10_database/runtime/oncc_extension/oncc_extension.sqlite')
required = ['race_date', 'race_no', 'horse_no', 'horse_name']
with sqlite3.connect(f'file:{DB}?mode=ro', uri=True) as con:
    integrity = con.execute('PRAGMA integrity_check').fetchone()[0]
    columns = [r[1] for r in con.execute('PRAGMA table_info(oncc_qualitative_features)')]
    count = con.execute('SELECT COUNT(*) FROM oncc_qualitative_features').fetchone()[0]
    distinct_races = con.execute('SELECT COUNT(DISTINCT race_date || ":" || race_no) FROM oncc_qualitative_features').fetchone()[0]
    nulls = {c: con.execute(f'SELECT COUNT(*) FROM oncc_qualitative_features WHERE {c} IS NULL').fetchone()[0] for c in required}
    duplicate_keys = con.execute('''SELECT COUNT(*) FROM (
        SELECT race_date, race_no, horse_no, COUNT(*) AS n
        FROM oncc_qualitative_features
        GROUP BY race_date, race_no, horse_no HAVING n > 1
    )''').fetchone()[0]
print(json.dumps({
    'status': 'ok' if integrity == 'ok' and not any(nulls.values()) and duplicate_keys == 0 else 'fail',
    'db': str(DB),
    'integrity_check': integrity,
    'columns': columns,
    'rows': count,
    'distinct_races': distinct_races,
    'required_null_counts': nulls,
    'duplicate_primary_key_groups': duplicate_keys,
}, ensure_ascii=False))
