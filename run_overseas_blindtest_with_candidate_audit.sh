#!/usr/bin/env bash
# Run the immutable overseas blind-test tick, then update a candidate-only
# eligibility report.  This wrapper never invokes --mode train and never calls
# N6.  It is intentionally separate from V10.2 prediction paths.
set -uo pipefail

ROOT=/home/ubuntu/hkjc_v10_database
PYTHON="$ROOT/.venv/bin/python"
LOCK="$ROOT/runtime/overseas_blindtest/host.lock"
STATUS_DIR="$ROOT/runtime/overseas_candidate_calibration"

mkdir -p "$STATUS_DIR"
exec 9>"$LOCK"
if ! flock -n 9; then
  exit 0
fi

# Preserve existing blind-test behaviour and run its audit even if a capture
# gate fails.  The tick itself records failures in the immutable status file.
"$PYTHON" "$ROOT/overseas_blindtest_pipeline.py" --mode tick
pipeline_exit=$?

# Audit-only: no --mode train flag, no approval file, no model artifact output.
"$PYTHON" "$ROOT/overseas_candidate_calibration.py" \
  --mode audit \
  --ledger "$ROOT/runtime/overseas_blindtest/overseas_blindtest.sqlite" \
  --report "$STATUS_DIR/eligibility_status.json"
audit_exit=$?

if [ "$pipeline_exit" -ne 0 ]; then
  exit "$pipeline_exit"
fi
exit "$audit_exit"
