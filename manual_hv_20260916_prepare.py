import json
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

root = Path('/home/ubuntu/hkjc_v10_database')
candidate = root / 'runtime/pre_race_schedule_20260916_HV.candidate.json'
times = {'1':'19:10','2':'19:40','3':'20:10','4':'20:40','5':'21:10','6':'21:45','7':'22:15','8':'22:50'}
payload = {
    'schema_version':'v10_2_pre_race_schedule_v1',
    'timezone':'Asia/Hong_Kong',
    'snapshot_minutes_before':[15,5],
    'meeting':{
        'race_date':'2026/09/16',
        'racecourse':'HV',
        'surface':'Turf',
        'course':'B',
        'race_count':8,
        'source_note':'HKJC official 2026/09/16 Happy Valley race times supplied and verified by user.',
        'race_start_times':times,
    },
}
candidate.parent.mkdir(parents=True, exist_ok=True)
candidate.write_text(json.dumps(payload, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
assert candidate.stat().st_size > 0
for n, t in times.items():
    datetime.strptime(f'2026-09-16 {t}', '%Y-%m-%d %H:%M').replace(tzinfo=ZoneInfo('Asia/Hong_Kong'))
assert payload['meeting']['race_date']=='2026/09/16'
assert payload['meeting']['racecourse']=='HV'
assert len(payload['meeting']['race_start_times'])==8
print('CANDIDATE_MANIFEST_WRITTEN', candidate)
print('CANDIDATE_MANIFEST_VALID')
cmd = [str(root/'.venv/bin/python'), str(root/'pre_race_scheduler.py'), '--config', str(candidate), '--project-dir', str(root), '--output-root', str(root/'runtime/pre_race'), '--state-file', str(root/'runtime/pre_race_state_20260916_HV.candidate.json'), '--now', '2026-09-16T17:00:00+08:00', '--dry-run']
proc = subprocess.run(cmd, cwd=root, text=True, capture_output=True, timeout=120)
print(proc.stdout)
print(proc.stderr)
print('DRY_RUN_RETURN_CODE', proc.returncode)
raise SystemExit(proc.returncode)
