#!/usr/bin/env bash
# Install or preview the V10.2 daily repository synchronization/review cron entry.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER="$ROOT_DIR/run_daily_repo_sync_review.sh"
START_MARKER="# BEGIN HKJC_V10_DAILY_REPO_SYNC_REVIEW"
END_MARKER="# END HKJC_V10_DAILY_REPO_SYNC_REVIEW"
CRON_BLOCK="$START_MARKER
CRON_TZ=Asia/Hong_Kong
30 4 * * * $RUNNER
$END_MARKER"

usage() {
  cat <<'EOF'
Usage: ./install_daily_repo_sync_review_cron.sh --show|--install

  --show     Print the exact daily 04:30 HKT crontab block without modifying cron.
  --install  Replace only the marked V10.2 repository-review block in the current user's crontab.
EOF
}

if [[ $# -ne 1 || ( "$1" != "--show" && "$1" != "--install" ) ]]; then
  usage >&2
  exit 2
fi
if [[ ! -x "$RUNNER" ]]; then
  echo "ERROR: runner is not executable: $RUNNER" >&2
  exit 2
fi
if [[ "$1" == "--show" ]]; then
  printf '%s\n' "$CRON_BLOCK"
  exit 0
fi
if ! command -v crontab >/dev/null 2>&1; then
  echo "ERROR: crontab is not installed or unavailable." >&2
  exit 2
fi

existing="$(crontab -l 2>/dev/null || true)"
cleaned="$(printf '%s\n' "$existing" | awk -v begin="$START_MARKER" -v end="$END_MARKER" '
  $0 == begin { skipping=1; next }
  $0 == end { skipping=0; next }
  !skipping { print }
')"
{
  printf '%s\n' "$cleaned" | sed '/^[[:space:]]*$/d'
  printf '\n%s\n' "$CRON_BLOCK"
} | crontab -

echo "Installed daily repository synchronization and code review at 04:30 HKT."
echo "Verify with: crontab -l"
