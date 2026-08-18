#!/usr/bin/env bash
# Install or preview V10.3 unseen-cohort collection at 05:10 HKT.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER="$ROOT_DIR/run_daily_v103_bayesian_cohort.sh"
START_MARKER="# BEGIN HKJC_V10_3_BAYESIAN_COHORT"
END_MARKER="# END HKJC_V10_3_BAYESIAN_COHORT"
CRON_BLOCK="$START_MARKER
CRON_TZ=Asia/Hong_Kong
10 5 * * * $RUNNER
$END_MARKER"

usage() {
  cat <<'EOF'
Usage: ./install_daily_v103_bayesian_cohort_cron.sh --show|--install

  --show     顯示每日 05:10 HKT V10.3 cohort 排程，不修改 crontab。
  --install  只取代本安裝器的標記區段，保留其他 crontab 工作。
EOF
}

case "${1:-}" in
  --show)
    printf '%s\n' "$CRON_BLOCK"
    exit 0
    ;;
  --install)
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

if [[ ! -x "$RUNNER" ]]; then
  echo "ERROR: runner 不存在或不可執行：$RUNNER" >&2
  exit 1
fi
if ! command -v crontab >/dev/null 2>&1; then
  echo "ERROR: crontab 未安裝或不可用；請在持續運行的 Linux 主機安裝 cron 後重試。" >&2
  exit 1
fi

temp="$(mktemp)"
trap 'rm -f "$temp"' EXIT
(crontab -l 2>/dev/null || true) | sed "/$START_MARKER/,/$END_MARKER/d" > "$temp"
{
  cat "$temp"
  printf '\n%s\n' "$CRON_BLOCK"
} | crontab -

echo "Installed daily V10.3 unseen-cohort collection at 05:10 HKT."
echo "The job only collects immutable T-5 snapshots after official settlement and skips while archive/backfill is active."
echo "Verify with: crontab -l"
