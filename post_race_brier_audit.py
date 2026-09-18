"""Offline official-result alignment and strict WIN/PLACE Brier audit."""
from __future__ import annotations
import argparse
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

TOL = 1e-6

def load(path: Path, names: tuple[str, ...]):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    rows = value if isinstance(value, list) else next((value.get(name) for name in names if isinstance(value.get(name), list)), None)
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError(f'{path}: invalid rows')
    return rows

def intval(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer() and value > 0:
        return int(value)
    if isinstance(value, str) and value.strip().isdigit() and int(value.strip()) > 0:
        return int(value.strip())
    return None

def key(row):
    day = row.get('race_date') or row.get('date')
    number = intval(row.get('race_no') or row.get('race_number'))
    return (day, number) if isinstance(day, str) and number else None

def numeric_probability(row, names):
    for name in names:
        value = row.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = float(value)
            if math.isfinite(value) and 0 <= value <= 1:
                return value
    return None

def row_name(row):
    return row.get('horse_name') or row.get('name') or None

def one(prediction_rows, result_rows):
    official = {}
    names = {}
    positions = {}
    for row in result_rows:
        horse = intval(row.get('horse_no'))
        finish = row.get('finish_pos') or row.get('finish_position')
        finish = intval(finish)
        if horse is None or finish is None:
            return {'status': 'not_scored_invalid_official_row', 'brier_score': None}
        if horse in official:
            return {'status': 'not_scored_duplicate_official_horse_no', 'brier_score': None}
        official[horse] = finish
        names[horse] = row_name(row)
        positions[finish] = horse
    if not official:
        return {'status': 'not_scored_no_official_field', 'brier_score': None}
    winner = positions.get(1)
    if winner is None:
        return {'status': 'not_scored_missing_winner', 'field_size': len(official), 'brier_score': None}
    official_top3 = [positions[pos] for pos in (1, 2, 3) if pos in positions]
    prediction = {}
    for row in prediction_rows:
        horse = intval(row.get('horse_no'))
        win = numeric_probability(row, ('predicted_win_probability', 'win_probability', 'probability'))
        place = numeric_probability(row, ('predicted_place_probability', 'place_probability'))
        if horse is None or win is None:
            return {'status': 'not_scored_invalid_prediction_row', 'field_size': len(official), 'brier_score': None}
        if horse in prediction:
            return {'status': 'not_scored_duplicate_prediction_horse_no', 'field_size': len(official), 'brier_score': None}
        prediction[horse] = {'win': win, 'place': place, 'name': row_name(row)}
    if set(prediction) != set(official):
        return {'status': 'not_scored_field_mismatch', 'field_size': len(official), 'brier_score': None}
    win_total = sum(item['win'] for item in prediction.values())
    if abs(win_total - 1) > TOL:
        return {'status': 'not_scored_probability_sum_not_one', 'field_size': len(official), 'probability_sum': win_total, 'brier_score': None}
    top_win = sorted(prediction, key=lambda horse: (-prediction[horse]['win'], horse))[:3]
    model_top = top_win[0]
    win_score = sum((item['win'] - (1 if horse == winner else 0)) ** 2 for horse, item in prediction.items())
    place_rows = [horse for horse, item in prediction.items() if item['place'] is not None]
    place_score = None
    place_probability_sum = None
    place_status = 'not_scored_place_missing'
    if len(place_rows) == len(prediction):
        place_probability_sum = sum(prediction[horse]['place'] for horse in place_rows)
        place_score = sum((prediction[horse]['place'] - (1 if official[horse] <= 3 else 0)) ** 2 for horse in place_rows) / len(place_rows)
        place_status = 'scored'
    model_place_top3 = sorted(place_rows, key=lambda horse: (-prediction[horse]['place'], horse))[:3] if place_rows else []
    return {
        'status': 'scored',
        'field_size': len(official),
        'winner_horse_no': winner,
        'winner_horse_name': names.get(winner),
        'official_top3_horse_nos': official_top3,
        'official_top3_horse_names': [names.get(horse) for horse in official_top3],
        'model_top_horse_no': model_top,
        'model_top_horse_name': prediction[model_top]['name'],
        'model_top3_horse_nos': top_win,
        'model_top3_hit': bool(set(top_win) & set(official_top3)),
        'probability_sum': win_total,
        'brier_score': win_score,
        'uniform_baseline': 1 - 1 / len(official),
        'place_status': place_status,
        'place_probability_rows': len(place_rows),
        'place_probability_sum': place_probability_sum,
        'place_brier_score': place_score,
        'model_place_top3_horse_nos': model_place_top3,
        'model_place_top3_hit': bool(set(model_place_top3) & set(official_top3)) if model_place_top3 else None,
    }

def audit(prediction_path: Path, result_path: Path, output_path: Path, log_path: Path):
    predictions = load(prediction_path, ('predictions', 'results', 'races'))
    results = load(result_path, ('results', 'official_results', 'starters'))
    grouped_predictions = defaultdict(list)
    grouped_results = defaultdict(list)
    for row in predictions:
        if key(row):
            grouped_predictions[key(row)].append(row)
    for row in results:
        if key(row):
            grouped_results[key(row)].append(row)
    races = []
    for day, number in sorted(set(grouped_predictions) | set(grouped_results)):
        item = {'race_date': day, 'race_no': number}
        item.update(one(grouped_predictions[(day, number)], grouped_results[(day, number)]))
        races.append(item)
    scored_win = [item['brier_score'] for item in races if item.get('status') == 'scored' and item.get('brier_score') is not None]
    scored_place = [item['place_brier_score'] for item in races if item.get('place_brier_score') is not None]
    place_hits = [item['model_place_top3_hit'] for item in races if item.get('model_place_top3_hit') is not None]
    top3_hits = [item['model_top3_hit'] for item in races if item.get('status') == 'scored']
    report = {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'prediction_source': str(prediction_path),
        'official_result_source': str(result_path),
        'race_count': len(races),
        'scored_race_count': len(scored_win),
        'not_scored_race_count': len(races) - len(scored_win),
        'daily_brier_score': sum(scored_win) / len(scored_win) if scored_win else None,
        'place_brier_score': sum(scored_place) / len(scored_place) if scored_place else None,
        'place_scored_race_count': len(scored_place),
        'model_top3_hit_rate': sum(top3_hits) / len(top3_hits) if top3_hits else None,
        'model_place_top3_hit_rate': sum(place_hits) / len(place_hits) if place_hits else None,
        'races': races,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    with log_path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(report, ensure_ascii=False, separators=(',', ':')) + '\n')
    return report

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--predictions', required=True, type=Path)
    parser.add_argument('--results', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--log', required=True, type=Path)
    args = parser.parse_args()
    report = audit(args.predictions, args.results, args.output, args.log)
    print(json.dumps({key: report[key] for key in ('race_count', 'scored_race_count', 'daily_brier_score', 'place_brier_score', 'model_top3_hit_rate', 'model_place_top3_hit_rate')}, ensure_ascii=False))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
