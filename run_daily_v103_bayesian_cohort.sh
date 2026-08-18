#!/usr/bin/env bash
# V10.3 daily unseen-cohort collection and Bayesian walk-forward gate.
# Collects only immutable T-5 pre-race snapshots with matching official results.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
RUN_DATE="$(TZ=Asia/Hong_Kong date +%F)"
LOG_DIR="${V103_COHORT_LOG_DIR:-$ROOT_DIR/archive/v103_bayesian_cohort/logs}"
COHORT_ROOT="${V103_COHORT_ROOT:-$ROOT_DIR/archive/v103_bayesian_cohort}"
LOCK_FILE="${V103_COHORT_LOCK_FILE:-$ROOT_DIR/runtime/v103_bayesian_cohort.lock}"
ARCHIVE_LOCK_FILE="${HKJC_DAILY_LOCK_FILE:-$ROOT_DIR/runtime/daily_archive_and_backfill.lock}"
MIN_UNSEEN_RACES="${V103_MIN_UNSEEN_RACES:-150}"

mkdir -p "$LOG_DIR" "$COHORT_ROOT" "$(dirname "$LOCK_FILE")"
LOG_FILE="$LOG_DIR/${RUN_DATE}.log"
MANIFEST_FILE="$COHORT_ROOT/manifest_latest.json"

if ! command -v flock >/dev/null 2>&1; then
  echo "$(TZ=Asia/Hong_Kong date '+%F %T %Z') ERROR: flock is required for safe V10.3 cohort collection." >> "$LOG_FILE"
  exit 1
fi

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "$(TZ=Asia/Hong_Kong date '+%F %T %Z') INFO: V10.3 cohort job already running; skipped." >> "$LOG_FILE"
  exit 0
fi

# Do not read a partially updated official-result database.
if ! flock -n "$ARCHIVE_LOCK_FILE" true; then
  echo "$(TZ=Asia/Hong_Kong date '+%F %T %Z') INFO: daily archive/backfill lock is held; V10.3 collection deferred." >> "$LOG_FILE"
  exit 0
fi

{
  echo "$(TZ=Asia/Hong_Kong date '+%F %T %Z') START v103_unseen_cohort min_unseen_races=$MIN_UNSEEN_RACES"
  "$PYTHON_BIN" "$ROOT_DIR/collect_v103_unseen_cohort.py" \
    --project-dir "$ROOT_DIR" \
    --db "$ROOT_DIR/hkjc_last_season.sqlite" \
    --snapshot-root "$ROOT_DIR/runtime/pre_race" \
    --cohort-root "$COHORT_ROOT" \
    --min-unseen-races "$MIN_UNSEEN_RACES" \
    --run-evaluation
  echo "$(TZ=Asia/Hong_Kong date '+%F %T %Z') END v103_unseen_cohort manifest=$MANIFEST_FILE"
} >> "$LOG_FILE" 2>&1
