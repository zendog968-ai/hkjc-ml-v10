#!/usr/bin/env bash
# V10.2 S1/S2 odds-drop walk-forward validation.
# Does not fabricate data: the Python runner exits successfully with N/A until
# at least 100 complete T-15/T-5 predictions and official settled results exist.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREDICTION_GLOB="${OVERSEAS_PREDICTION_GLOB:-archive/overseas_s1s2_predictions/**/*.json}"
RESULTS_CSV="${OVERSEAS_RESULTS_CSV:-archive/overseas_s1s2_results.csv}"
OUTPUT_DIR="${OVERSEAS_CV_OUTPUT_DIR:-v102_s1s2_odds_drop_cross_validation}"
WEIGHTS="${OVERSEAS_ODDS_DROP_WEIGHTS:-0,0.05,0.10,0.20,0.30}"
INITIAL_BANKROLL="${OVERSEAS_KELLY_INITIAL_BANKROLL:-100}"
MAX_SINGLE_FRACTION="${OVERSEAS_KELLY_MAX_SINGLE_FRACTION:-0.01}"
MAX_RACE_FRACTION="${OVERSEAS_KELLY_MAX_RACE_FRACTION:-0.02}"
DRAWDOWN_TRIGGER="${OVERSEAS_KELLY_DRAWDOWN_TRIGGER:-0.10}"
DRAWDOWN_MULTIPLIER="${OVERSEAS_KELLY_DRAWDOWN_MULTIPLIER:-0.50}"

cd "$ROOT_DIR"
if [[ ! -f "$RESULTS_CSV" ]]; then
  echo "缺少官方結果 CSV：$RESULTS_CSV" >&2
  echo "需包含 race_key,horse_no,finish_pos；不會以最終賠率或非官方資料代替。" >&2
  exit 2
fi

python3 cross_validate_s1s2_odds_drop_weight.py \
  --prediction-glob "$PREDICTION_GLOB" \
  --results-csv "$RESULTS_CSV" \
  --weights "$WEIGHTS" \
  --season-mode hk_season \
  --min-total-races 100 \
  --min-train-races 100 \
  --min-test-races 15 \
  --min-folds-for-recommendation 2 \
  --enable-dynamic-kelly \
  --initial-bankroll "$INITIAL_BANKROLL" \
  --kelly-max-single-fraction "$MAX_SINGLE_FRACTION" \
  --kelly-max-race-fraction "$MAX_RACE_FRACTION" \
  --kelly-drawdown-trigger "$DRAWDOWN_TRIGGER" \
  --kelly-drawdown-multiplier "$DRAWDOWN_MULTIPLIER" \
  --output-dir "$OUTPUT_DIR"
