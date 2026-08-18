# HKJC ML V10 Web Dashboard 部署指南

## 架構

Dashboard 為純靜態前端，位於 `frontend/`：

```text
frontend/
├── index.html    # Bootstrap 5 + Marked.js + DOMPurify CDN
├── style.css     # Dashboard 視覺樣式
└── app.js        # 同源唯讀 API 查詢與報告渲染
```

Nginx 同時提供靜態 Dashboard 與 API。兩者使用相同的 Basic Auth，因此前端以相對路徑請求 `/api/...`，沒有跨來源請求與 CORS 問題。

| 網址 | Nginx 行為 | 存取保護 |
|---|---|---|
| `/` | 從 `/home/ubuntu/hkjc_v10_database/frontend/index.html` 提供 Dashboard。 | Basic Auth。 |
| `/style.css`、`/app.js` | 提供前端靜態資產。 | Basic Auth。 |
| `/api/...` | 反向代理至 `127.0.0.1:8000` 的 Uvicorn。 | Basic Auth + API 唯讀 GET／OPTIONS 限制。 |
| `/health`、`/docs`、`/openapi.json` | 反向代理至 Uvicorn。 | Basic Auth。 |

> Dashboard 本身不會執行模型、抓取賠率、改寫 runtime 或觸發排程。它只讀取 API 已公開的已保存預測與報告。

## 部署或更新

先取得已包含 Dashboard 的功能分支：

```bash
cd /home/ubuntu/hkjc_v10_database
git fetch origin
git switch feature/v10-readonly-web-api
git pull --ff-only origin feature/v10-readonly-web-api
```

若尚未執行過 API 部署器，請先執行：

```bash
chmod +x deploy/install_hkjc_web_api.sh deploy/verify_hkjc_web_api_deployment.sh
./deploy/install_hkjc_web_api.sh
```

部署器會安裝 `acl`，並只為 Nginx worker 使用者 `www-data` 加入：

1. `/home/ubuntu` 與專案根目錄的 traversal 權限；
2. `frontend/` 及其檔案的 read／execute 權限；
3. 不會把 SQLite、模型、runtime prediction 或其他專案檔案開放給 Nginx 靜態檔案服務。

已安裝 API 時，只需要套用更新後的 Nginx 範本：

```bash
cd /home/ubuntu/hkjc_v10_database
sudo install -m 0644 deploy/nginx/hkjc-api /etc/nginx/sites-available/hkjc-api
sudo nginx -t
sudo systemctl reload nginx
sudo systemctl restart hkjc-web-api.service
```

確認服務與 Dashboard 均受密碼保護：

```bash
./deploy/verify_hkjc_web_api_deployment.sh
curl -u hkjcapi http://127.0.0.1/
curl -u hkjcapi http://127.0.0.1/api/races/2026-08-18
```

然後在瀏覽器開啟：

```text
http://主機IP/
```

如服務已配置 HTTPS，應使用：

```text
https://你的網域/
```

## Nginx 套用檢查

最重要的靜態檔案與 API 路由設定如下：

```nginx
root /home/ubuntu/hkjc_v10_database/frontend;
index index.html;

auth_basic "HKJC ML V10 Dashboard";
auth_basic_user_file /etc/nginx/hkjc-api.htpasswd;

location ^~ /api/ {
    proxy_pass http://127.0.0.1:8000;
}

location / {
    try_files $uri $uri/ /index.html;
}
```

請勿將 `127.0.0.1:8000` 改為 `0.0.0.0:8000`，亦不要在 UFW 開放 port 8000。外部流量應只經由 Nginx 的 HTTP/80 或 HTTPS/443 進入。

## 開發期預覽

若只想在本機不經 Nginx 預覽靜態版面，可執行：

```bash
cd /home/ubuntu/hkjc_v10_database
python3 -m http.server 8011 --directory frontend
```

這個預覽器不會帶有 Nginx Basic Auth，也無法在不同 port 與 API 進行同源整合測試。完整功能請透過正式 Nginx 站點測試。

## 依賴說明

Bootstrap、Marked.js 和 DOMPurify 使用 CDN。若主機或使用者瀏覽器無法存取 CDN，版面仍會載入，但 Bootstrap 樣式、Markdown 渲染或 HTML sanitization 可能不可用。對於完全封閉網路的正式環境，應把這些固定版本的前端檔案下載並由同一 Nginx root 託管，再更新 `index.html` 的 script／stylesheet URL。
