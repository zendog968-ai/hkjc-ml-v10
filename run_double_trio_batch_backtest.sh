#!/usr/bin/env bash
# Run strict, separate T-15 and T-5 historical Double Trio backtests.
# Usage:
# ./run_double_trio_batch_backtest.sh \
#   --db hkjc_odds_snapshot_archive.sqlite \
#   --candidate-root archive/model_pool_candidates/double_trio \
#   --output-root v102_double_trio_batch_backtest
set -euo pipefail

DB=""
CANDIDATE_ROOT=""
OUTPUT_ROOT=""
MAX_CAPTURE_DELTA_SECONDS=180
LABELS=("T_MINUS_15" "T_MINUS_5")

while [[ $# -gt 0 ]]; do
  case "$1" in
    --db) DB="$2"; shift 2 ;;
    --candidate-root) CANDIDATE_ROOT="$2"; shift 2 ;;
    --output-root) OUTPUT_ROOT="$2"; shift 2 ;;
    --max-capture-delta-seconds) MAX_CAPTURE_DELTA_SECONDS="$2"; shift 2 ;;
    --snapshot-label) LABELS=("$2"); shift 2 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "未知參數：$1" >&2; exit 2 ;;
  esac
done

if [[ -z "$DB" || -z "$CANDIDATE_ROOT" || -z "$OUTPUT_ROOT" ]]; then
  echo "必須提供 --db、--candidate-root 及 --output-root" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$OUTPUT_ROOT"
for LABEL in "${LABELS[@]}"; do
  TARGET_DIR="$OUTPUT_ROOT/${LABEL}"
  rm -rf "$TARGET_DIR"
  python3 "$SCRIPT_DIR/backtest_complex_pool_double_trio.py" \
    --db "$DB" \
    --candidate-root "$CANDIDATE_ROOT" \
    --snapshot-label "$LABEL" \
    --max-capture-delta-seconds "$MAX_CAPTURE_DELTA_SECONDS" \
    --output-dir "$TARGET_DIR"
done

echo "完成：請分別檢閱 $OUTPUT_ROOT/T_MINUS_15 與 $OUTPUT_ROOT/T_MINUS_5 的 double_trio_batch_summary.json。"
