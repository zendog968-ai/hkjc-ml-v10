#!/usr/bin/env python3
"""Read-only aggregate of current York S1 public market research artifacts."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description='彙總 York S1 官方市場與海外研究性 EV 工件。')
    parser.add_argument('--runtime-dir', default='runtime/overseas_deep')
    parser.add_argument('--output', default='reports/overseas_deep/YORK_S1_MARKET_SUMMARY_2026-08-19.json')
    args = parser.parse_args()
    rows = []
    totals = {'races_complete': 0, 'matched_runners': 0, 'ev_available': 0, 'positive_win_ev': 0, 'positive_place_ev': 0}
    for race_no in range(1, 8):
        path = Path(args.runtime_dir) / f'york_s1_{race_no}_deep.json'
        try:
            item = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            rows.append({'race_no': race_no, 'status': 'artifact_missing'})
            continue
        market = item.get('market_research') or {}
        starters = item.get('starters') or []
        available = [row.get('market_research') or {} for row in starters if (row.get('market_research') or {}).get('ev_kelly_status') == 'available_research_only']
        positive_win = sum(1 for entry in available if isinstance(entry.get('win_ev'), (int, float)) and entry['win_ev'] > 0)
        positive_place = sum(1 for entry in available if isinstance(entry.get('place_ev'), (int, float)) and entry['place_ev'] > 0)
        status = market.get('status', 'unavailable')
        if status == 'complete':
            totals['races_complete'] += 1
            totals['matched_runners'] += int(market.get('matched_runner_count') or 0)
            totals['ev_available'] += len(available)
            totals['positive_win_ev'] += positive_win
            totals['positive_place_ev'] += positive_place
        rows.append({'race_no': race_no, 'status': status, 'matched_runners': market.get('matched_runner_count', 0), 'expected_runners': market.get('expected_runner_count', 0), 'place_dividends': market.get('place_dividends'), 'ev_available': len(available), 'positive_win_ev': positive_win, 'positive_place_ev': positive_place, 'captured_at_utc': market.get('captured_at_utc')})
    payload = {'meeting': 'York S1 2026-08-19', 'research_only': True, 'n6_status': 'disabled_non_hk', 'races': rows, 'totals': totals, 'warning': 'EV／Kelly建基於未校準的海外公開深度分數機率代理；只作研究性市場比較，不是V10.2正式訊號。'}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
