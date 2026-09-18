from __future__ import annotations
import argparse, json, time
from datetime import datetime, timezone
from pathlib import Path
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from candidate_third_party_odds_adapter import parse_third_party_snapshot

BASE_URL='https://www.51saima.com/mobi/odds.jsp'

def atomic(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    tmp.replace(path)

def fetch_html(url: str, timeout: int = 15):
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path='/home/ubuntu/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome', headless=True, args=['--no-sandbox'])
        try:
            page = b.new_page(locale='zh-HK')
            r = page.goto(url, wait_until='domcontentloaded', timeout=timeout * 1000)
            page.wait_for_timeout(5000)
            return (r.status if r else None), page.url, page.content()
        finally:
            b.close()

def snapshot_from_html(html: str, target: str, final_url: str):
    soup = BeautifulSoup(html, 'html.parser')
    tables = []
    for i, table in enumerate(soup.find_all('table')):
        rows = []
        for tr in table.find_all('tr'):
            cells = [c.get_text(' ', strip=True) for c in tr.find_all(['th', 'td'])]
            if cells:
                rows.append(cells)
        tables.append({'index': i, 'rows': rows})
    return {'target': target, 'final_url': final_url, 'tables': tables}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date'); ap.add_argument('--course'); ap.add_argument('--race-no', type=int, required=True)
    ap.add_argument('--race-date'); ap.add_argument('--racecourse'); ap.add_argument('--race-card', required=True)
    ap.add_argument('--out-dir'); ap.add_argument('--output'); ap.add_argument('--place-output'); ap.add_argument('--combined-output'); ap.add_argument('--metadata-output')
    ap.add_argument('--snapshot-output'); ap.add_argument('--snapshot-label', default=''); ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--min-interval', type=int, default=60); ap.add_argument('--state-file', default='.third_party_odds_state.json'); ap.add_argument('--timeout', type=int, default=15)
    args = ap.parse_args()
    args.date = args.date or args.race_date
    args.course = args.course or args.racecourse
    if not args.date or not args.course:
        ap.error('--date/--course or --race-date/--racecourse required')
    base = Path(args.out_dir or (Path(args.output).parent if args.output else '.'))
    output = Path(args.output or base / 'odds_overlay.json')
    place_output = Path(args.place_output or base / 'place_odds_overlay.json')
    combined_output = Path(args.combined_output or base / 'odds_overlay_combined.json')
    metadata_output = Path(args.metadata_output or base / 'odds_overlay.meta.json')
    snapshot_output = Path(args.snapshot_output) if args.snapshot_output else None
    base.mkdir(parents=True, exist_ok=True)
    date_obj = datetime.strptime(args.date, '%Y/%m/%d')
    url = f'{BASE_URL}?date={date_obj.strftime("%d-%m-%Y")}&venue={args.course}&raceno={args.race_no}'
    meta = {'source_mode': 'third_party_public_dom', 'source_url': url, 'race_date': args.date, 'racecourse': args.course, 'race_no': args.race_no, 'status': 'degraded', 'complete_win_place_pairs': 0, 'odds_types': ['WIN', 'PLA'], 'output_files': {}, 'warnings': []}
    runners = []; win = {}; pla = {}; final_url = url
    try:
        state = Path(args.state_file)
        if state.exists():
            prev = json.loads(state.read_text(encoding='utf-8'))
            elapsed = time.time() - float(prev.get('last_request_epoch', 0))
            if elapsed < max(60, args.min_interval):
                raise RuntimeError(f'min_interval_not_elapsed:{int(elapsed)}s')
        status, final_url, html = fetch_html(url, args.timeout)
        if status != 200:
            raise RuntimeError(f'http_status:{status}')
        snap = snapshot_from_html(html, url, final_url)
        adapted = parse_third_party_snapshot(snap, args.race_no, args.date, args.course, args.race_card)
        runners = json.loads(Path(args.race_card).read_text(encoding='utf-8')).get('runners', [])
        win = {str(r.get('horse_name')): None for r in runners}; pla = dict(win)
        for row in adapted.get('odds', []):
            rr = next((r for r in runners if int(r.get('horse_no', r.get('horse_number'))) == row['horse_no']), None)
            if rr:
                win[str(rr['horse_name'])] = row['win_odds']; pla[str(rr['horse_name'])] = row['place_odds']
        meta['adapter'] = adapted.get('metadata', {})
        meta['complete_win_place_pairs'] = adapted.get('metadata', {}).get('complete_win_place_pairs', 0)
        meta['status'] = adapted.get('status', 'degraded')
        meta['runners_written'] = len(runners); meta['final_url'] = final_url
        if meta['status'] != 'ok':
            meta['warnings'].append('strict_third_party_gate_failed; all odds remain null')
    except Exception as exc:
        meta['warnings'].append(f'fail_closed:{type(exc).__name__}:{exc}')
        try:
            runners = json.loads(Path(args.race_card).read_text(encoding='utf-8')).get('runners', [])
            win = {str(r.get('horse_name')): None for r in runners}; pla = dict(win)
        except Exception:
            runners = []; win = {}; pla = {}
    if meta['status'] != 'ok':
        win = {k: None for k in win}; pla = {k: None for k in pla}; meta['complete_win_place_pairs'] = 0
    combined = {'win': win, 'place': pla}
    atomic(output, win); atomic(place_output, pla); atomic(combined_output, combined)
    meta['output_files'] = {'win': str(output), 'place': str(place_output), 'combined': str(combined_output), 'meta': str(metadata_output)}
    meta['fetched_at_utc'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
    meta['status'] = 'complete' if meta['status'] == 'ok' else 'degraded'
    atomic(metadata_output, meta)
    if snapshot_output:
        atomic(snapshot_output, {'schema_version': 'v10.2_odds_snapshot', 'snapshot_label': args.snapshot_label, 'captured_at_utc': meta['fetched_at_utc'], 'race': {'race_date': args.date, 'racecourse': args.course.upper(), 'race_no': args.race_no}, 'status': meta['status'], 'odds': {'win': win, 'place': pla}, 'metadata_file': str(metadata_output), 'source_url': url, 'source_mode': 'third_party_public_dom'})
    atomic(Path(args.state_file), {'last_request_epoch': time.time(), 'url': url})
    print(json.dumps({'status': meta['status'], 'complete_win_place_pairs': meta['complete_win_place_pairs'], 'race_no': args.race_no, 'field_size': len(win), 'out_dir': str(base), 'warnings': meta['warnings']}, ensure_ascii=False))

if __name__ == '__main__':
    main()
