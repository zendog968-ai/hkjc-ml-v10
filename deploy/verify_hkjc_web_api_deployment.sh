#!/usr/bin/env bash
# Verify the deployed HKJC ML V10 read-only API without changing host settings.
set -Eeuo pipefail

SERVICE_NAME="hkjc-web-api.service"

require() {
  if ! "$@"; then
    echo "驗收失敗：$*" >&2
    exit 1
  fi
}

require sudo systemctl is-enabled --quiet "$SERVICE_NAME"
require sudo systemctl is-active --quiet "$SERVICE_NAME"
require sudo nginx -t

if ! sudo ss -ltnp | grep -qE '127\.0\.0\.1:8000'; then
  echo "驗收失敗：沒有偵測到 loopback 127.0.0.1:8000 監聽。" >&2
  exit 1
fi
if sudo ss -ltnp | grep -qE '(^|\s)(0\.0\.0\.0|\[::\]):8000'; then
  echo "驗收失敗：8000 不可對外監聽。" >&2
  exit 1
fi

health="$(curl --silent --show-error --fail http://127.0.0.1:8000/health)"
if ! grep -q '"read_only":true' <<<"$health"; then
  echo "驗收失敗：loopback health 未聲明 read_only。" >&2
  exit 1
fi

status="$(curl --silent --output /dev/null --write-out '%{http_code}' http://127.0.0.1/health)"
if [[ "$status" != "401" ]]; then
  echo "驗收失敗：Nginx /health 未由 Basic Auth 保護（預期 401，實得 $status）。" >&2
  exit 1
fi
dashboard_status="$(curl --silent --output /dev/null --write-out '%{http_code}' http://127.0.0.1/)"
if [[ "$dashboard_status" != "401" ]]; then
  echo "驗收失敗：Nginx Dashboard 根路徑未由 Basic Auth 保護（預期 401，實得 $dashboard_status）。" >&2
  exit 1
fi

printf '%s\n' 'PASS: systemd active/enabled；Uvicorn 僅 loopback:8000；Dashboard 與 API 均受 Nginx Basic Auth 保護；health 為唯讀。'
printf '%s\n' '下一步可測試有效憑證：curl -u 使用者名稱 http://127.0.0.1/health'
