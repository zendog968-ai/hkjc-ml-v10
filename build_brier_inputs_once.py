import csv
import glob
import json
import re
from pathlib import Path

ROOT = Path('/home/ubuntu/hkjc_v10_database')
DATE = '2026-09-06'
COURSE = 'ST'
PRED_GLOB = ROOT / 'runtime/pre_race/2026/09/06_ST_R*/prediction.json'
RESULT_CSV = ROOT / 'archive/result_archive_runs/2026-09-06/local_results_export.csv'
OUT_PRED = Path('/tmp/hkjc_brier_20260906_predictions.json')
OUT_RES = Path('/tmp/hkjc_brier_20260906_results.json')

predictions = []
for path in sorted(glob.glob(str(PRED_GLOB))):
    match = re.search(r'_R(\d{2})/prediction\.json$', path)
    if not match:
        continue
    race_no = int(match.group(1))
    payload = json.loads(Path(path).read_text(encoding='utf-8'))
    rows = payload.get('predictions', []) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        continue
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = dict(row)
        item['race_date'] = DATE
        item['race_no'] = race_no
        item['racecourse'] = COURSE
        predictions.append(item)

results = []
with RESULT_CSV.open(encoding='utf-8-sig', newline='') as handle:
    for row in csv.DictReader(handle):
        if row.get('race_date') != DATE or row.get('racecourse') != COURSE:
            continue
        try:
            race_no = int(row['race_no'])
            horse_no = int(row['horse_no'])
            finish_pos = int(row['finish_pos'])
        except (TypeError, ValueError):
            continue
        results.append({'race_date': DATE, 'race_no': race_no, 'racecourse': COURSE, 'horse_no': horse_no, 'finish_pos': finish_pos})

OUT_PRED.write_text(json.dumps({'predictions': predictions}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
OUT_RES.write_text(json.dumps({'results': results}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps({'prediction_rows': len(predictions), 'result_rows': len(results), 'prediction_races': sorted({r['race_no'] for r in predictions}), 'result_races': sorted({r['race_no'] for r in results})}, ensure_ascii=False))
