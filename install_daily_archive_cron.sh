#!/usr/bin/env bash
# Install or display the V10.2 daily archive/backfill cron block on a Linux host.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER="$ROOT_DIR/run_daily_archive_and_overseas_backfill.sh"
MARKER_BEGIN="# BEGIN V10.2 DAILY ARCHIVE AND OVERSEAS BACKFILL"
MARKER_END="# END V10.2 DAILY ARCHIVE AND OVERSEAS BACKFILL"

if [[ ! -x "$RUNNER" ]]; then
  echo "請先執行：chmod +x $RUNNER" >&2
  exit 2
fi
block() {
  cat <<EOF
$MARKER_BEGIN
CRON_TZ=Asia/Hong_Kong
15 3 * * * $RUNNER
$MARKER_END
EOF
}

case "${1:---show}" in
  --show)
    echo "將安裝以下每日 03:15 HKT 排程（僅預覽，沒有修改 crontab）："
    block
    ;;
  --install)
    if ! command -v crontab >/dev/null 2>&1; then
      echo "找不到 crontab；請安裝主機的 cron 套件後再執行。" >&2
      exit 2
    fi
    temp="$(mktemp)"
    trap 'rm -f "$temp"' EXIT
    (crontab -l 2>/dev/null || true) | sed "/$MARKER_BEGIN/,/$MARKER_END/d" > "$temp"
    {
      cat "$temp"
      printf '\n'
      block
    } | crontab -
    echo "已安裝／更新每日 03:15 HKT V10.2 排程。"
    echo "請以 crontab -l 驗證；日誌會寫入 $ROOT_DIR/archive/daily_automation_logs/。"
    ;;
  *)
    echo "用法：$0 [--show|--install]" >&2
    exit 2
    ;;
esac
