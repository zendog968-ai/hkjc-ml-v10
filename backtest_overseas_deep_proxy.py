#!/usr/bin/env python3
"""Strict, cohort-aware evaluation for the overseas deep-score research proxy.

No retrospective racecard is treated as a pre-race decision.  Only immutable
pre-race decision documents plus separately verified official results qualify.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None


def event_key(item: dict[str, Any]) -> str | None:
    race = item.get('race') if isinstance(item.get('race'), dict) else item
    date = race.get('meeting_date')
    code = race.get('simulcast_code')
    number = race.get('race_no')
    return f'{date}|{code}|{number}' if date and code and number else None


def result_order(item: dict[str, Any]) -> list[int] | None:
    order = item.get('finish_order') or item.get('official_finish_order')
    if not isinstance(order, list) or not order:
        return None
    numbers: list[int] = []
    for value in order:
        try:
            numbers.append(int(value))
        except (TypeError, ValueError):
            return None
    return numbers if len(numbers) == len(set(numbers)) else None


def evaluate(decisions_dir: Path, results_dir: Path) -> dict[str, Any]:
    decisions = {event_key(item): item for path in decisions_dir.glob('*.json') if (item := read_json(path)) and event_key(item)}
    results = {event_key(item): item for path in results_dir.glob('*.json') if (item := read_json(path)) and event_key(item)}
    valid, excluded = [], []
    for key, decision in decisions.items():
        result = results.get(key)
        if result is None:
            excluded.append({'event_key': key, 'reason': 'official_result_missing'})
            continue
        capture = parse_time(decision.get('captured_at_utc'))
        start = parse_time(decision.get('scheduled_start_utc'))
        published = parse_time(result.get('published_at_utc'))
        runners = decision.get('runners')
        finish = result_order(result)
        hashes = decision.get('source_hashes')
        version = decision.get('proxy_version')
        if not capture or not start or not published or not isinstance(runners, list) or not isinstance(hashes, dict) or not version:
            excluded.append({'event_key': key, 'reason': 'missing_immutable_pre_race_contract'})
            continue
        if not (capture < start < published) or not finish:
            excluded.append({'event_key': key, 'reason': 'timestamp_or_finish_order_invalid'})
            continue
        probabilities = []
        for runner in runners:
            try:
                number, probability = int(runner['runner_no']), float(runner['research_win_probability'])
            except (KeyError, TypeError, ValueError):
                probabilities = []
                break
            if not math.isfinite(probability) or probability < 0:
                probabilities = []
                break
            probabilities.append((number, probability))
        if not probabilities or not math.isclose(sum(probability for _, probability in probabilities), 1.0, abs_tol=1e-9):
            excluded.append({'event_key': key, 'reason': 'research_probability_contract_invalid'})
            continue
        valid.append({'event_key': key, 'version': version, 'probabilities': probabilities, 'finish': finish})
    cohorts: dict[str, list[dict[str, Any]]] = {}
    for event in valid:
        cohorts.setdefault(event['version'], []).append(event)
    summary = []
    for version, events in sorted(cohorts.items()):
        brier, log_loss, top1, top3 = [], [], 0, 0
        for event in events:
            probs = dict(event['probabilities'])
            winner = event['finish'][0]
            brier.append(sum((probability - (1.0 if number == winner else 0.0)) ** 2 for number, probability in probs.items()))
            log_loss.append(-math.log(max(probs.get(winner, 0.0), 1e-15)))
            ordered = [number for number, _ in sorted(event['probabilities'], key=lambda value: (-value[1], value[0]))]
            top1 += int(ordered[0] == winner)
            top3 += int(winner in ordered[:3])
        n = len(events)
        summary.append({'proxy_version': version, 'events': n, 'sample_status': 'exploratory' if n < 15 else 'sufficient', 'top1_win_rate': top1 / n, 'top3_contains_winner_rate': top3 / n, 'multi_class_brier': sum(brier) / n, 'log_loss': sum(log_loss) / n})
    return {'status': 'ready' if summary else 'not_available', 'strict_event_count': len(valid), 'excluded_events': excluded, 'cohorts': summary, 'minimum_exploratory_events': 15, 'warning': '歷史賽卡或賽後重新抓取的RPR／TS不可作賽前特徵；僅不可變賽前決策配對官方賽果才會進入回測。'}


def main() -> int:
    parser = argparse.ArgumentParser(description='評估海外深度研究代理；嚴格拒絕未有賽前不可變證據的回測。')
    parser.add_argument('--decisions-dir', default='archive/overseas_deep_backtest/decisions')
    parser.add_argument('--results-dir', default='archive/overseas_deep_backtest/results')
    parser.add_argument('--output', default='reports/overseas_deep/OVERSEAS_DEEP_PROXY_BACKTEST.json')
    args = parser.parse_args()
    result = evaluate(Path(args.decisions_dir), Path(args.results_dir))
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': result['status'], 'strict_event_count': result['strict_event_count'], 'cohort_count': len(result['cohorts']), 'output': args.output}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
