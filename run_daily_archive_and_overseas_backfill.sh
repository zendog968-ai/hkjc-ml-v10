#!/usr/bin/env bash
# V10.2 daily official-result archive and bounded overseas backfill.
# Intended for a persistent Linux host cron.  Never bypasses HKJC access limits.
set -uo pipefail

export TZ="${TZ:-Asia/Hong_Kong}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
DB_PATH="${HKJC_DB_PATH:-$ROOT_DIR/hkjc_last_season.sqlite}"
SCHEMA_PATH="${HKJC_OVERSEAS_SCHEMA_PATH:-$ROOT_DIR/schema_overseas_racing.sql}"
RUN_DATE="${RUN_DATE:-$(date +%F)}"
LOG_DIR="${HKJC_DAILY_LOG_DIR:-$ROOT_DIR/archive/daily_automation_logs}"
RAW_DIR="${OVERSEAS_BACKFILL_RAW_DIR:-$ROOT_DIR/archive/overseas_hkjc_raw}"
LOCK_FILE="${HKJC_DAILY_LOCK_FILE:-$ROOT_DIR/runtime/daily_archive_and_backfill.lock}"
BATCH_SIZE="${OVERSEAS_BACKFILL_BATCH_SIZE:-6}"
SEASONS="${OVERSEAS_BACKFILL_SEASONS:-2223,2324,2425,2526,2627}"
AUTO_ARCHIVE_TELEGRAM="${AUTO_ARCHIVE_TELEGRAM:-0}"

mkdir -p "$LOG_DIR" "$RAW_DIR" "$(dirname "$LOCK_FILE")"
LOG_FILE="$LOG_DIR/${RUN_DATE}.log"
if ! command -v flock >/dev/null 2>&1; then
  echo "$(date '+%F %T %Z') ERROR: flock 不可用，拒絕在未鎖定狀態執行 SQLite 寫入。" >&2
  exit 2
fi
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "$(date '+%F %T %Z') INFO: 既有每日歸檔／回刷正在執行，本次安全跳過。" >> "$LOG_FILE"
  exit 0
fi

exec >> "$LOG_FILE" 2>&1
printf '%s START run_date=%s db=%s batch_size=%s\n' "$(date '+%F %T %Z')" "$RUN_DATE" "$DB_PATH" "$BATCH_SIZE"

archive_cmd=("$PYTHON_BIN" "$ROOT_DIR/auto_archive_results.py"
  --date "$RUN_DATE"
  --db "$DB_PATH"
  --schema "$SCHEMA_PATH"
  --archive-dir "$ROOT_DIR/archive/result_archive_runs"
  --raw-dir "$RAW_DIR"
  --seasons "$SEASONS")
if [[ "$AUTO_ARCHIVE_TELEGRAM" == "1" ]]; then
  archive_cmd+=(--telegram)
fi
printf '%s STEP auto_archive_results\n' "$(date '+%F %T %Z')"
"${archive_cmd[@]}"
archive_code=$?
printf '%s RESULT auto_archive_results=%s\n' "$(date '+%F %T %Z')" "$archive_code"

# Continue to the bounded overseas archive even if same-day local data is absent;
# both statuses are preserved in this log and the individual JSON reports.
printf '%s STEP overseas_backfill_batch\n' "$(date '+%F %T %Z')"
OVERSEAS_DB_PATH="$DB_PATH" \
OVERSEAS_SCHEMA_PATH="$SCHEMA_PATH" \
OVERSEAS_BACKFILL_RAW_DIR="$RAW_DIR" \
OVERSEAS_BACKFILL_BATCH_SIZE="$BATCH_SIZE" \
"$ROOT_DIR/run_overseas_backfill_batch.sh"
backfill_code=$?
printf '%s RESULT overseas_backfill_batch=%s\n' "$(date '+%F %T %Z')" "$backfill_code"

if [[ $archive_code -ne 0 || $backfill_code -ne 0 ]]; then
  printf '%s END status=partial_or_error archive=%s backfill=%s\n' "$(date '+%F %T %Z')" "$archive_code" "$backfill_code"
  exit 1
fi
printf '%s END status=ok\n' "$(date '+%F %T %Z')"
