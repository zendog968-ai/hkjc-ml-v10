#!/usr/bin/env bash
# Install HKJC ML V10 Read-Only API behind Nginx Basic Auth.
# Run as: ./deploy/install_hkjc_web_api.sh [--enable-ufw]
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="hkjc-web-api.service"
NGINX_SITE="hkjc-api"
AUTH_FILE="/etc/nginx/hkjc-api.htpasswd"
ENABLE_UFW=0

usage() {
  cat <<'USAGE'
Usage: ./deploy/install_hkjc_web_api.sh [--enable-ufw]

  --enable-ufw  Allow OpenSSH and Nginx HTTP, then enable UFW with its current
                default policy. Omit this option to leave firewall changes to the
                explicit post-install command in the deployment guide.
USAGE
}

for argument in "$@"; do
  case "$argument" in
    --enable-ufw) ENABLE_UFW=1 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "未知參數：$argument" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ "$(id -u)" -eq 0 ]]; then
  echo "請以 ubuntu 使用者執行此腳本；腳本會在必要時個別呼叫 sudo。" >&2
  exit 2
fi
if [[ "$(id -un)" != "ubuntu" ]]; then
  echo "安全限制：此範本為 ubuntu 使用者設計；目前使用者是 $(id -un)。" >&2
  exit 2
fi
if [[ ! -f "$PROJECT_ROOT/web_api.py" ]]; then
  echo "找不到 $PROJECT_ROOT/web_api.py；請從專案根目錄的 deploy 腳本執行。" >&2
  exit 2
fi
if [[ ! -f "$PROJECT_ROOT/deploy/systemd/$SERVICE_NAME" || ! -f "$PROJECT_ROOT/deploy/nginx/$NGINX_SITE" ]]; then
  echo "缺少部署範本檔案。" >&2
  exit 2
fi

sudo apt-get update -y
sudo apt-get install -y nginx apache2-utils
if command -v uv >/dev/null 2>&1; then
  sudo uv pip install --system 'fastapi>=0.110,<1' 'uvicorn[standard]>=0.27,<1'
else
  sudo pip3 install 'fastapi>=0.110,<1' 'uvicorn[standard]>=0.27,<1'
fi

sudo -u ubuntu /usr/bin/python3 -c 'import fastapi, uvicorn; print("FastAPI/Uvicorn import: OK")'

if [[ -e /etc/nginx/sites-enabled/default ]]; then
  cat <<'NOTICE'
偵測到 Nginx 預設網站仍啟用。若這台主機只供 HKJC API 使用，請輸入 yes
以停用預設網站，讓 hkjc-api 成為 HTTP/80 的預設站點。若主機已有其他網站，
請按 Ctrl+C，改用獨立 server_name 或自訂 port 後再部署。
NOTICE
  read -r -p "停用 /etc/nginx/sites-enabled/default？[yes/NO] " confirm_default
  if [[ "$confirm_default" != "yes" ]]; then
    echo "為避免覆蓋既有網站，已取消安裝。" >&2
    exit 1
  fi
fi

read -r -p "Basic Auth 使用者名稱 [hkjcapi]: " auth_user
auth_user="${auth_user:-hkjcapi}"
if ! [[ "$auth_user" =~ ^[A-Za-z0-9._-]{3,64}$ ]]; then
  echo "使用者名稱只可包含英數字、.、_、-，長度 3 至 64。" >&2
  exit 2
fi

sudo install -d -m 0755 /etc/nginx
if [[ -f "$AUTH_FILE" ]]; then
  sudo htpasswd "$AUTH_FILE" "$auth_user"
else
  sudo htpasswd -c "$AUTH_FILE" "$auth_user"
fi
sudo chown root:www-data "$AUTH_FILE"
sudo chmod 0640 "$AUTH_FILE"

sudo install -m 0644 "$PROJECT_ROOT/deploy/systemd/$SERVICE_NAME" "/etc/systemd/system/$SERVICE_NAME"
sudo install -m 0644 "$PROJECT_ROOT/deploy/nginx/$NGINX_SITE" "/etc/nginx/sites-available/$NGINX_SITE"
if [[ -e /etc/nginx/sites-enabled/default ]]; then
  sudo rm -f /etc/nginx/sites-enabled/default
fi
sudo ln -sfn "/etc/nginx/sites-available/$NGINX_SITE" "/etc/nginx/sites-enabled/$NGINX_SITE"

sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE_NAME"
sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl reload nginx

# Uvicorn must never listen on a public interface. Nginx is the only external hop.
if ! sudo ss -ltnp | grep -qE '127\.0\.0\.1:8000'; then
  echo "安全檢查失敗：Uvicorn 未只綁定 127.0.0.1:8000。" >&2
  exit 1
fi
if sudo ss -ltnp | grep -qE '(^|\s)(0\.0\.0\.0|\[::\]):8000'; then
  echo "安全檢查失敗：偵測到公開的 8000 監聽。" >&2
  exit 1
fi

if [[ "$ENABLE_UFW" -eq 1 ]]; then
  if ! command -v ufw >/dev/null 2>&1; then
    sudo apt-get install -y ufw
  fi
  # Prevent remote lockout before enabling a deny-incoming firewall.
  sudo ufw allow OpenSSH || sudo ufw allow 22/tcp
  sudo ufw allow 80/tcp
  sudo ufw --force enable
  sudo ufw status verbose
else
  echo "UFW 尚未變更。確認 SSH 管理規則後，可執行："
  echo "  sudo ufw allow OpenSSH || sudo ufw allow 22/tcp"
  echo "  sudo ufw allow 80/tcp"
  echo "  sudo ufw --force enable"
fi

curl --silent --show-error --fail http://127.0.0.1:8000/health
printf '\n部署成功。外部存取請使用 Nginx 的 HTTP/80 與 Basic Auth；8000 僅限本機。\n'
printf '驗收命令：sudo systemctl status %s --no-pager\n' "$SERVICE_NAME"
