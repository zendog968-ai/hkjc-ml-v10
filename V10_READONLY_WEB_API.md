# HKJC ML V10 唯讀 Web API

## 目的與邊界

`web_api.py` 是 HKJC ML V10 的輕量級本機查詢服務。它只讀取賽前自動化已生成的 `runtime/pre_race/` 工件，讓日後的網頁前端可以查閱賽日、prediction、雙策略篩選結果及 Markdown 報告。

> 此服務**不會**執行 `predict.py`、修改 `hkjc_last_season.sqlite`、寫入 runtime、觸發 Cron、重訓模型，或改動 V10.2／V10.3 的正式機率、EV、Kelly 與排程。

## 啟動

在專案根目錄執行：

```bash
cd /home/ubuntu/hkjc_v10_database
uvicorn web_api:app --host 0.0.0.0 --port 8000 --reload
```

啟動後可先開啟 `http://127.0.0.1:8000/docs` 查看 OpenAPI 文件，或以：

```bash
curl http://127.0.0.1:8000/health
```

本命令適用於本機開發與測試。長期公開部署前，應透過受控反向代理、TLS、網路存取限制及明確 CORS 網域設定保護服務；不得把開發用 `--reload` 作為公開生產程序。

## API 端點

| 方法 | 端點 | 回應 | 行為 |
|---|---|---|---|
| GET | `/health` | JSON | 顯示 API 狀態、runtime 是否存在及唯讀標記。 |
| GET | `/api/races/{date}` | JSON | 列出該日期已生成 `prediction.json` 的 ST／HV 場次。日期格式為 `YYYY-MM-DD`。 |
| GET | `/api/prediction/{date}/{course}/{race_no}` | JSON | 回傳 `prediction.json` 與可選的 `high_probability_filter.json`。 |
| GET | `/api/report/{date}/{course}/{race_no}` | `text/markdown` | 回傳 `pre_race_report.md` 原文，供前端 Markdown renderer 顯示。 |

例如：

```bash
curl http://127.0.0.1:8000/api/races/2026-08-18
curl http://127.0.0.1:8000/api/prediction/2026-08-18/ST/1
curl http://127.0.0.1:8000/api/report/2026-08-18/ST/1
```

## Runtime 檔案契約

賽前 scheduler 現行工作目錄以 `runtime/pre_race/YYYY/MM/DD_ST_Rnn/` 形式產生。例如：

```text
runtime/pre_race/2026/08/18_ST_R01/
├── prediction.json
├── high_probability_filter.json
├── pre_race_report.md
└── v103_bayesian_uncertainty.json  # 如有研究 sidecar
```

API 只接受嚴格的日期、`ST`／`HV` 及 1 至 20 場次，並只從固定 runtime root 下發現 `prediction.json` 的工作目錄。它不把任何使用者輸入直接拼接為檔案路徑，且會拒絕超過安全讀取上限或無效 JSON 的工件。

## CORS 設定

預設只允許以下前端來源：

```text
http://localhost:3000
http://127.0.0.1:3000
```

如前端使用其他指定網域，請在啟動前設定以逗號分隔的 allow-list：

```bash
export HKJC_API_CORS_ORIGINS="https://your-frontend.example,http://localhost:5173"
uvicorn web_api:app --host 0.0.0.0 --port 8000 --reload
```

不建議對包含賽前預測資料的服務設定萬用 `*` CORS。

## 測試

```bash
cd /home/ubuntu/hkjc_v10_database
python3 verify_web_api.py
```

測試會在 temporary runtime fixture 驗證四個端點、CORS、輸入拒絕與 HTTP `POST` 禁止，並以檔案 SHA-256 對照確認 API 沒有修改任何 runtime 工件。
