# S2 海外深度資料監控

`monitor_s2_overseas.py` 是 S2 的**安全待命監控器**。它不會假定下一個 S2 的賽日、賽場、場次、Racing Post URL、At The Races URL 或 HKJC 賠率 URL；在沒有經核實的官方 manifest 時，監控器只寫出 `awaiting_official_manifest` 狀態，並且不發出任何外部請求。

> 這項設計避免把錯誤賽場、重複場次或海外轉播編號誤寫入資料庫。它同時保持 N6 的海外停用規則及 V10 本地資料庫隔離。

## 資料隔離

所有 S2 深度資料沿用獨立的 `overseas_deep_racing.sqlite` 與 `schema_overseas_deep_racing.sql`。schema 以 `simulcast_code` 區分 S1 與 S2，並保存 `overseas_meetings`、`overseas_races`、`overseas_starters`、來源狀態及市場研究快照。監控器不讀取或寫入 `hkjc_last_season.sqlite`，也不呼叫 N6。

## 官方 manifest 啟用條件

只有在以下資料全部由 HKJC 海外轉播頁和對應公開賽卡核實後，才可將範本複製為 live manifest：

| 欄位 | 必要條件 |
|---|---|
| `meeting_date`、`venue` | 與 HKJC S2 官方賽程一致。 |
| `race_no` | 介乎 1–20，且為該轉播賽的已公布場次。 |
| `local_start_time`、`hkt_start_time` | 同時可核對，避免跨日或夏令時間混淆。 |
| `racing_post_url` | HTTPS、Racing Post 官方網域、實際場次頁。 |
| `at_the_races_url` | HTTPS、At The Races 官方網域、實際賽日頁。 |
| `hkjc_win_place_url` | 可選；必須為 HKJC 官方 S2 Win／Place 頁。未核實時不抓取市場或計算 EV／Kelly。 |

範本位於 `runtime/s2_monitor/s2_official_manifest.template.json`。live manifest 的目標路徑為 `runtime/s2_monitor/s2_official_manifest.json`；該檔案是 runtime 工件，不會提交至版本庫。

## 執行方法

```bash
cd /home/ubuntu/hkjc_v10_database
.venv/bin/python monitor_s2_overseas.py \
  --manifest runtime/s2_monitor/s2_official_manifest.json \
  --db overseas_deep_racing.sqlite \
  --report reports/overseas_deep/S2_MONITOR_STATUS.json
```

監控器會以非阻塞鎖避免重疊，對公開來源逐場串行處理，預設每個外部請求至少相隔 2 秒。每一場都先執行 HTTPS 網域 allowlist、賽時及 URL 完整性檢查；任何失敗均記錄為 `skipped` 或 `failed`，不會猜測資料。

## 市場與 EV／Kelly

S2 深度抓取只保存公開 RPR、TS、路程／Going／場地特徵。HKJC 市場整合是獨立後續步驟，必須以官方 Win／Place 頁完成全場馬匹身份匹配，並符合至少 60 秒市場請求間隔，才可產生研究性 EV／Kelly。其機率代理未校準，不是 V10.2 正式勝率、EV、Kelly 或投注指令。

## 驗證

```bash
.venv/bin/python verify_s2_overseas_monitor.py
```

此測試確認只有 HTTPS allowlisted URL 才能通過，S1／錯誤 manifest、缺少賽時或未核實來源均會安全停止。
