# HKJC ML V10 唯讀 Web API：Linux 常駐與安全部署

本文件把 `web_api.py` 部署為 Linux 主機上的唯讀常駐服務。架構固定為：外部請求先到 Nginx；Nginx 強制 Basic Auth；Nginx 再轉送至只監聽 `127.0.0.1:8000` 的 Uvicorn。V10.2／V10.3 的 Cron、模型、SQLite 和 runtime 寫入流程均不會由此服務執行。

> **重要安全限制：** Basic Auth 只驗證身分，不會加密密碼。HTTP/80 適合已受 VPN、私有 LAN 或安全 tunnel 保護的初始部署；若要從公共互聯網存取，必須先設定 HTTPS，再容許外部使用。

## 1. 部署前檢查

請先拉取唯讀 API 功能分支：

```bash
cd /home/ubuntu/hkjc_v10_database
git fetch origin
git switch feature/v10-readonly-web-api
git pull --ff-only origin feature/v10-readonly-web-api
```

確認現有網站不會與 HTTP/80 衝突：

```bash
sudo ss -ltnp | grep -E ':(80|443|8000)\b' || true
sudo ls -la /etc/nginx/sites-enabled/ 2>/dev/null || true
```

若主機已有其他網站，請勿直接使用範本內的 `server_name _;` 和 port 80。應先為 API 選擇獨立網域或自訂 Nginx listen port，並按現有 Nginx 架構調整。

## 2. 一鍵安裝

以下指令會安裝 Nginx 與 `htpasswd`、安裝 FastAPI／Uvicorn、提示建立 Basic Auth、安裝 systemd 和 Nginx 設定、啟動服務，並確認 8000 只綁定 loopback。

```bash
cd /home/ubuntu/hkjc_v10_database
chmod +x deploy/install_hkjc_web_api.sh deploy/verify_hkjc_web_api_deployment.sh
./deploy/install_hkjc_web_api.sh
```

部署器若偵測到 Nginx 預設網站，會要求輸入完整的 `yes` 才會停用它，避免意外覆蓋同一主機既有的 HTTP/80 網站。Basic Auth 使用者名稱預設為 `hkjcapi`，密碼由 `htpasswd` 互動讀取，不會寫入 Git、systemd unit 或 shell history。

部署器會建立以下主機檔案：

| 路徑 | 用途 | 權限／安全意義 |
|---|---|---|
| `/etc/systemd/system/hkjc-web-api.service` | Uvicorn 常駐 unit。 | 以 `ubuntu` 身分，僅綁定 `127.0.0.1:8000`。 |
| `/etc/nginx/sites-available/hkjc-api` | Nginx 反向代理站點。 | Basic Auth、方法限制及安全回應標頭。 |
| `/etc/nginx/sites-enabled/hkjc-api` | 站點 symlink。 | 由 Nginx 載入。 |
| `/etc/nginx/hkjc-api.htpasswd` | Basic Auth 雜湊密碼檔。 | `root:www-data`、`0640`；不可提交 Git。 |

## 3. Systemd 設計

服務由 `deploy/systemd/hkjc-web-api.service` 安裝。其核心執行命令是：

```ini
ExecStart=/usr/bin/python3 -m uvicorn web_api:app --host 127.0.0.1 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips=127.0.0.1
```

這個 production unit **不使用 `--reload`**。`--reload` 只適合本機開發，會額外建立監視程序，且不適合常駐生產服務。unit 已啟用 restart-on-failure、`NoNewPrivileges`、唯讀 home／system 保護、空 capability set 與禁止建立可執行記憶體等限制。

常用管理指令：

```bash
sudo systemctl status hkjc-web-api.service --no-pager
sudo journalctl -u hkjc-web-api.service -n 100 --no-pager
sudo systemctl restart hkjc-web-api.service
sudo systemctl stop hkjc-web-api.service
```

## 4. Nginx 與 Basic Auth

Nginx 設定位於 `deploy/nginx/hkjc-api`。它將外部 `/` 請求 proxy 到 `http://127.0.0.1:8000`，並在 proxy 前強制：

```nginx
auth_basic "HKJC ML V10 API";
auth_basic_user_file /etc/nginx/hkjc-api.htpasswd;
```

日後新增或重設帳戶密碼：

```bash
sudo htpasswd /etc/nginx/hkjc-api.htpasswd hkjcapi
sudo chown root:www-data /etc/nginx/hkjc-api.htpasswd
sudo chmod 0640 /etc/nginx/hkjc-api.htpasswd
sudo nginx -t && sudo systemctl reload nginx
```

本機繞過 Nginx 檢查 Uvicorn health：

```bash
curl http://127.0.0.1:8000/health
```

經 Nginx 的 API 請求必須帶入 Basic Auth：

```bash
curl -u hkjcapi http://127.0.0.1/health
curl -u hkjcapi http://127.0.0.1/api/races/2026-08-18
```

## 5. UFW 最小防火牆規則

Uvicorn 的 port 8000 只綁定 loopback，因此 **不應**使用 UFW 開放 8000。若主機目前未使用 UFW，請先確認遠端 SSH 管理規則，再執行：

```bash
sudo ufw allow OpenSSH || sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw --force enable
sudo ufw status verbose
```

如果 SSH 使用自訂 port，請先以實際 port 取代 `22/tcp`。若要把 UFW 併入一鍵部署，可執行：

```bash
cd /home/ubuntu/hkjc_v10_database
./deploy/install_hkjc_web_api.sh --enable-ufw
```

啟用後確認沒有公開 8000 listener：

```bash
sudo ss -ltnp | grep -E ':(80|443|8000)\b'
```

預期只看到 `127.0.0.1:8000`，而 port 80 由 Nginx 監聽。

## 6. 部署驗收

```bash
cd /home/ubuntu/hkjc_v10_database
./deploy/verify_hkjc_web_api_deployment.sh
```

驗證器會檢查 systemd 是否 enabled／active、Nginx syntax、8000 是否僅 loopback、Uvicorn health 是否聲明 `read_only: true`，以及未經 Basic Auth 的 Nginx `/health` 是否返回 HTTP 401。

## 7. 公開互聯網：HTTPS 必要升級

當 API 需要公共互聯網存取時，請先為 server 選定 DNS 網域，再取得 TLS 證書。Basic Auth 憑證不得透過裸 HTTP 公網傳輸。以下是 Nginx 設定好正確 `server_name` 後的典型流程：

```bash
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d api.example.com --redirect
sudo nginx -t && sudo systemctl reload nginx
sudo ufw allow 443/tcp
```

完成 HTTPS 後，應在 Nginx 設定將 `server_name _;` 替換為真實網域，並讓 HTTP/80 僅轉址至 HTTPS。這個步驟取決於實際 DNS 和網域所有權，因此不應在未提供網域的情況下自動執行。

## 8. 停用與移除

```bash
sudo systemctl disable --now hkjc-web-api.service
sudo rm -f /etc/systemd/system/hkjc-web-api.service
sudo rm -f /etc/nginx/sites-enabled/hkjc-api /etc/nginx/sites-available/hkjc-api
sudo rm -f /etc/nginx/hkjc-api.htpasswd
sudo systemctl daemon-reload
sudo nginx -t && sudo systemctl reload nginx
```

移除前請確認沒有其他站點依賴 HTTP/80。
