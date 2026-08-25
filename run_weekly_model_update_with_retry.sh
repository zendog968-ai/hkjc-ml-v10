#!/usr/bin/env bash
# V10 weekly model-update wrapper: one primary run and at most two safe retries.
# Designed for Monday 02:00, 06:00 and 12:00 HKT cron entries.
set -euo pipefail

ROOT="/home/ubuntu/hkjc_v10_database"
PYTHON="${ROOT}/.venv/bin/python"
LOCK_FILE="${ROOT}/runtime/monthly_update.lock"
STATUS_FILE="${ROOT}/runtime/weekly_update_status.env"
LOG_DIR="${ROOT}/archive/monthly_update_logs"
DISPATCHER="/usr/local/sbin/hkjc-v10-smtp-dispatch"
PROVENANCE_SCRIPT="${ROOT}/training_provenance.py"
MODE=""
SIMULATE_FAILURE=0

usage() {
  echo "Usage: $0 --mode primary|retry [--simulate-failure]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      MODE=${2:-}
      shift 2
      ;;
    --simulate-failure)
      SIMULATE_FAILURE=1
      shift
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

if [[ "$MODE" != "primary" && "$MODE" != "retry" ]]; then
  usage
  exit 2
fi
if [[ ! -x "$PYTHON" ]]; then
  echo "Missing project Python: $PYTHON" >&2
  exit 2
fi

export TZ=Asia/Hong_Kong
RUN_DATE=$(date +%F)
LOG_PATH="${LOG_DIR}/weekly_update_${RUN_DATE}.log"
mkdir -p "$LOG_DIR" "${ROOT}/runtime"
touch "$LOG_PATH"
chmod 0640 "$LOG_PATH"

read_status() {
  [[ -f "$STATUS_FILE" ]] || return 1
  # shellcheck disable=SC1090
  source "$STATUS_FILE"
  [[ "${RUN_DATE:-}" == "$1" ]]
}

write_status() {
  local status=$1 attempts=$2 exit_code=$3 notification=$4
  local tmp
  tmp=$(mktemp "${ROOT}/runtime/.weekly_update_status.XXXXXX")
  umask 027
  cat > "$tmp" <<EOF
RUN_DATE=${RUN_DATE}
STATUS=${status}
ATTEMPTS=${attempts}
EXIT_CODE=${exit_code}
LOG_PATH=${LOG_PATH}
NOTIFICATION=${notification}
UPDATED_AT_HKT=$(date '+%Y-%m-%dT%H:%M:%S%z')
EOF
  chmod 0640 "$tmp"
  mv -f "$tmp" "$STATUS_FILE"
}

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$*" | tee -a "$LOG_PATH"
}

notify_failure() {
  if [[ "$SIMULATE_FAILURE" -eq 1 ]]; then
    log "Simulation mode: email notification intentionally not dispatched."
    return 0
  fi
  if sudo -n "$DISPATCHER" --weekly-failure >> "$LOG_PATH" 2>&1; then
    log "Failure notification dispatcher completed."
    return 0
  fi
  log "ERROR: failure notification dispatcher did not complete."
  return 1
}

if [[ "$MODE" == "primary" ]]; then
  ATTEMPTS=1
  log "Weekly update primary attempt 1/3 started."
elif ! read_status "$RUN_DATE"; then
  log "Retry skipped: no pending failure status for ${RUN_DATE}."
  exit 0
elif [[ "${STATUS:-}" != "retry_pending" ]]; then
  log "Retry skipped: current status is ${STATUS:-unknown}."
  exit 0
else
  ATTEMPTS=$(( ${ATTEMPTS:-0} + 1 ))
  if (( ATTEMPTS < 2 || ATTEMPTS > 3 )); then
    log "Retry skipped: invalid persisted attempt state."
    exit 1
  fi
  log "Weekly update retry attempt ${ATTEMPTS}/3 started."
fi

set +e
if [[ "$SIMULATE_FAILURE" -eq 1 ]]; then
  false >> "$LOG_PATH" 2>&1
  EXIT_CODE=$?
else
  flock -E 75 -n "$LOCK_FILE" "$PYTHON" "${ROOT}/monthly_update.py" >> "$LOG_PATH" 2>&1
  EXIT_CODE=$?
fi
set -e

if [[ "$EXIT_CODE" -eq 0 ]]; then
  if "$PYTHON" "$PROVENANCE_SCRIPT" >> "$LOG_PATH" 2>&1; then
    write_status "success" "$ATTEMPTS" "0" "not_required"
    log "Weekly update and provenance manifest completed successfully on attempt ${ATTEMPTS}/3."
  else
    # The model update has already succeeded. Do not re-run model training merely
    # because a read-only audit artifact failed; retain an explicit status for review.
    write_status "success_with_manifest_error" "$ATTEMPTS" "0" "audit_manifest_failed"
    log "ERROR: weekly update succeeded but provenance manifest generation failed."
  fi
  exit 0
fi

if [[ "$ATTEMPTS" -lt 3 ]]; then
  write_status "retry_pending" "$ATTEMPTS" "$EXIT_CODE" "deferred"
  if [[ "$EXIT_CODE" -eq 75 ]]; then
    log "Update attempt ${ATTEMPTS}/3 skipped because the existing lock is occupied; next scheduled retry retained."
  else
    log "Update attempt ${ATTEMPTS}/3 failed with exit code ${EXIT_CODE}; next scheduled retry retained."
  fi
  exit 0
fi

write_status "failed" "$ATTEMPTS" "$EXIT_CODE" "pending"
if [[ "$SIMULATE_FAILURE" -eq 1 ]]; then
  write_status "failed" "$ATTEMPTS" "$EXIT_CODE" "simulated_not_sent"
elif notify_failure; then
  write_status "failed" "$ATTEMPTS" "$EXIT_CODE" "sent"
else
  write_status "failed" "$ATTEMPTS" "$EXIT_CODE" "dispatcher_failed"
fi
log "Weekly update reached its retry limit after attempt ${ATTEMPTS}/3."
exit 1
