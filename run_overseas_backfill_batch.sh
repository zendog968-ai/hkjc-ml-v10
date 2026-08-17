#!/usr/bin/env bash
# V10.2 resumable HKJC overseas S1/S2 backfill batch.
# It archives only official public sources and never retries around access limits.
# Feature work is a leakage-safe readiness audit; it does not recreate historic
# pre-race predictions after results have become known.
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
DB_PATH="${OVERSEAS_DB_PATH:-$ROOT_DIR/hkjc_last_season.sqlite}"
SCHEMA_PATH="${OVERSEAS_SCHEMA_PATH:-$ROOT_DIR/schema_overseas_racing.sql}"
START_DATE="${OVERSEAS_BACKFILL_START_DATE:-2023-01-01}"
END_DATE="${OVERSEAS_BACKFILL_END_DATE:-$(date +%F)}"
SEASONS="${OVERSEAS_BACKFILL_SEASONS:-2223,2324,2425,2526,2627}"
BATCH_SIZE="${OVERSEAS_BACKFILL_BATCH_SIZE:-6}"
DELAY_MIN="${OVERSEAS_BACKFILL_DELAY_MIN:-3.0}"
DELAY_MAX="${OVERSEAS_BACKFILL_DELAY_MAX:-6.0}"
COOLDOWN_EVERY="${OVERSEAS_BACKFILL_COOLDOWN_EVERY:-20}"
COOLDOWN_SECONDS="${OVERSEAS_BACKFILL_COOLDOWN_SECONDS:-60}"
DISCOVER_IF_EMPTY="${OVERSEAS_BACKFILL_DISCOVER_IF_EMPTY:-0}"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
ARCHIVE_ROOT="${OVERSEAS_BACKFILL_ARCHIVE_ROOT:-$ROOT_DIR/archive/overseas_backfill_batches}"
RUN_DIR="$ARCHIVE_ROOT/$RUN_ID"
RAW_DIR="${OVERSEAS_BACKFILL_RAW_DIR:-$ROOT_DIR/archive/overseas_hkjc_raw}"
LOCK_FILE="${OVERSEAS_BACKFILL_LOCK_FILE:-$ROOT_DIR/runtime/overseas_backfill_batch.lock}"
LOG_FILE="$RUN_DIR/run.log"

mkdir -p "$RUN_DIR" "$RAW_DIR" "$(dirname "$LOCK_FILE")"
if ! command -v flock >/dev/null 2>&1; then
  echo "缺少 flock；為避免並行寫入 SQLite，批次已停止。" >&2
  exit 2
fi
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "已有海外回刷批次正在執行；本次安全跳過。" >&2
  exit 0
fi

log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$LOG_FILE"
}

cd "$ROOT_DIR"
log "開始海外回刷批次：range=$START_DATE..$END_DATE batch_size=$BATCH_SIZE seasons=$SEASONS"

if [[ "$DISCOVER_IF_EMPTY" == "1" ]]; then
  log "執行官方 fixture 發現（DISCOVER_IF_EMPTY=1）。"
  "$PYTHON_BIN" backfill_overseas_2023_2026.py \
    --db "$DB_PATH" --schema "$SCHEMA_PATH" --raw-dir "$RAW_DIR" \
    --report-dir "$RUN_DIR/discovery" --start-date "$START_DATE" --end-date "$END_DATE" \
    --seasons "$SEASONS" --discovery-only \
    --delay-min "$DELAY_MIN" --delay-max "$DELAY_MAX" \
    --cooldown-every "$COOLDOWN_EVERY" --cooldown-seconds "$COOLDOWN_SECONDS" \
    >>"$LOG_FILE" 2>&1
  discovery_code=$?
  if [[ $discovery_code -ne 0 ]]; then
    log "fixture 發現失敗，exit=$discovery_code；不會進入 archive。"
    exit "$discovery_code"
  fi
fi

log "以 --resume 處理最多 $BATCH_SIZE 個未完成／partial／source_unavailable 群組。"
"$PYTHON_BIN" backfill_overseas_2023_2026.py \
  --db "$DB_PATH" --schema "$SCHEMA_PATH" --raw-dir "$RAW_DIR" \
  --report-dir "$RUN_DIR/backfill" --start-date "$START_DATE" --end-date "$END_DATE" \
  --seasons "$SEASONS" --resume --max-meetings "$BATCH_SIZE" \
  --delay-min "$DELAY_MIN" --delay-max "$DELAY_MAX" \
  --cooldown-every "$COOLDOWN_EVERY" --cooldown-seconds "$COOLDOWN_SECONDS" \
  >>"$LOG_FILE" 2>&1
backfill_code=$?

log "建立無資料洩漏的特徵工程可用性報告；不賽後重建預測特徵。"
"$PYTHON_BIN" audit_overseas_feature_readiness.py \
  --db "$DB_PATH" --output "$RUN_DIR/feature_readiness.json" \
  >>"$LOG_FILE" 2>&1
feature_code=$?

cat > "$RUN_DIR/run_manifest.json" <<EOF
{
  "schema_version": "v10.2_overseas_backfill_batch_v1",
  "run_id": "$RUN_ID",
  "started_range": {"start": "$START_DATE", "end": "$END_DATE"},
  "batch_size": $BATCH_SIZE,
  "archive_status": $backfill_code,
  "feature_readiness_status": $feature_code,
  "backfill_summary": "$RUN_DIR/backfill/overseas_backfill_summary.json",
  "feature_readiness": "$RUN_DIR/feature_readiness.json",
  "log": "$LOG_FILE"
}
EOF

if [[ $backfill_code -ne 0 || $feature_code -ne 0 ]]; then
  log "批次未完全成功：archive=$backfill_code feature_audit=$feature_code。"
  exit 1
fi
log "批次完成；請查看 $RUN_DIR/backfill/overseas_backfill_summary.json 的 strict_status，而非以程序 exit code 判定資料完整性。"
