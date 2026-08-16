# V10.2 海外回刷、無條件歸檔、覆盤與風險提示運行手冊

**版本：V10.2 Advanced Feature & Ensemble Edition**

**狀態：程式與隔離測試已完成；歷史官方來源回刷採可恢復的缺口稽核模式。**

## 1. 本次升級摘要

本次升級把海外 S1/S2 管線由單純的冷啟動預測，擴充為可稽核的官方來源資料閉環。海外賽果解析器現在只會按可見標頭提取欄位，並保存名次、馬位差、完成時間、最後獨贏／位置賠率，以及賽果頁實際披露的騎師、練馬師、負磅與檔位。沒有足夠可解析名次列的賽事會保留為 `partial`，而非被誤報為已完成。

賽後覆盤現會在有賽前預測時輸出 Top 1、Top 3、熱門穩攻／冷門突襲命中、落飛標記追蹤、場內多馬勝率 Brier Score，及以**官方最後獨贏賠率**結算的一注一單位研究籃子 ROI。位置策略不會以臨場位置賠率取代官方結算派彩；尚未正規化官方位置派彩時會維持 `N/A`。無賽前預測時，系統仍會儲存官方賽果並寫入 `archived_only` 稽核紀錄，不生成虛構的模型表現。

| 功能 | 現行行為 | 安全閘門 |
|---|---|---|
| 2023–2026 海外發現 | 以 HKJC 官方 fixture 依賽季及 `YYYY-MM-DD` 範圍建立海外轉播群組清單。 | 只把官方解析到的群組列入；空來源或未解析賽果會保留缺口。 |
| 回刷與續跑 | `--resume` 只重試 `discovered`、`partial`、`source_unavailable` 群組；寫入具冪等性。 | 不以第三方資料、推斷名次或替代賠率補洞。 |
| 無條件歸檔 | 本地與海外已完成賽事均獨立歸檔，和是否曾產生貼士無關。 | 無賽前預測只入庫、不發覆盤訊息。 |
| 覆盤 | 使用預測生成時已存檔的海外預測批次，或明確指定的預測 JSON。 | 不得用賽後欄位回填或重建賽前預測。 |
| Telegram | 只有已設定 `TELEGRAM_BOT_TOKEN` 與 `TELEGRAM_CHAT_ID` 才會推送有預測的覆盤摘要。 | 缺少設定時回傳 `telegram_not_configured`，不外洩憑證。 |

## 2. 官方來源覆蓋預檢結果

已對 `2023-01-01` 至 `2026-08-17` 執行 HKJC 官方 fixture 發現預檢。官方頁共發現 268 個海外轉播群組：2023/24 賽季代碼 `2324` 有 73 個、2024/25 的 `2425` 有 81 個、2025/26 的 `2526` 有 100 個、2026/27 的 `2627` 有 14 個；代碼 `2223` 在該次官方頁回應中沒有可解析群組。此結果表示系統已建立可恢復的官方發現清單，**並不代表 2023–2026 全部賽果已成功寫入**。

單一 2023-07-23 S1 歷史案例驗證後，已修正官方路由與等待條件：舊式頁實際標示為 `Meeting Summary`，而其 Results 導航指向新版 `/en-us/overseas/results?RaceDate=...&Racecourse=...&RaceNo=...` 端點。結果頁若未有可解析的名次列，系統會保留 `partial`，並令群組保留在可續跑清單。這是刻意的完整性控制，不可把「已讀到 HTTP 頁面」當作「官方賽果已完整歸檔」。[1] [2]

> **資料完整性結論：** 目前可確認的是 268 個官方轉播群組的發現清單，而不是已完成的三年賽果資料庫。正式全量回刷應在可長時間持續執行的主機分批完成，並以輸出的覆蓋報告確認 `strict_status`；不得聲稱資料已全量完成，直至每個官方群組均有可稽核的完成或明確缺口記錄。

## 3. 正式回刷與續跑

在專案目錄下執行下列命令。所有日期參數均強制採用 `YYYY-MM-DD`。第一次先建立或更新官方發現清單；之後以 `--resume` 只重試尚未完成的群組。保留每次 `report-dir`，可比較缺口而不覆蓋前次報告。

```bash
cd /home/ubuntu/hkjc_v10_database

# 1. 重新取得官方 fixture 並建立群組清單
python3 backfill_overseas_2023_2026.py \
  --db hkjc_last_season.sqlite \
  --schema schema_overseas_racing.sql \
  --start-date 2023-01-01 \
  --end-date 2026-08-17 \
  --discovery-only \
  --report-dir overseas_backfill_reports/discovery_2026-08-17

# 2. 正式分批回刷；以低頻率、冷卻及可續跑方式讀取官方來源
python3 backfill_overseas_2023_2026.py \
  --db hkjc_last_season.sqlite \
  --schema schema_overseas_racing.sql \
  --start-date 2023-01-01 \
  --end-date 2026-08-17 \
  --resume \
  --delay-min 3.0 --delay-max 6.0 \
  --cooldown-every 20 --cooldown-seconds 60 \
  --report-dir overseas_backfill_reports/run_$(date +%F)
```

正式批次結束後，檢視 `overseas_meeting_coverage.csv`、`overseas_backfill_summary.json` 與 `overseas_backfill_attempts.json`。只有當 `strict_status` 為 `complete`，並且審查官方 fixture 缺口後，才可把該範圍描述為已成功解析的官方資料。任何 `partial` 或 `source_unavailable` 都應保留，待 HKJC 原始頁可用時再以 `--resume` 重試。

## 4. 實時 S1/S2 預測與特徵

`fetch_hkjc_s1s2.py` 讀取公開 HKJC 排位及可用賠率；`predict_s1s2.py` 使用海外分層冷啟動先驗，而不會把香港馬匹／騎師 ELO 硬套到海外馬匹。可驗證時，模型會納入 RPR／IFHA、久休天數、場地適應、練馬師 G1 紀錄、T-15/T-5 落飛、場內相對負磅及預測時點前近期前四縮減訊號。賠率不可用時只輸出場內相對機率，不計算 EV 或 Kelly。

```bash
# 建立海外賽卡與實際賽前快照
python3 fetch_hkjc_s1s2.py \
  --date 2026-08-17 --simulcast-code S1 --race-no 9 \
  --snapshot-label T_MINUS_15 \
  --scheduled-start-utc 2026-08-17T12:00:00+00:00 \
  --output runtime/s1_9_t15.json

# 預測與 Markdown 報告；T-15/T-5 必須為同場、身份匹配且時間偏差合格的快照
python3 predict_s1s2.py \
  --db hkjc_last_season.sqlite \
  --race-card runtime/s1_9_t15.json \
  --output-json runtime/s1_9_prediction.json \
  --output-md runtime/s1_9_prediction.md
```

## 5. 高爆冷、價值冷門與投注結構提示

賽前報告現在使用明確規則處理場內分佈。若有效參戰馬數為 14 匹或以上，且首選勝出率低於 20%，報告會在頂部標註 **⚠️【高爆冷風險亂局】**，並直接指出該場不適合作單膽。輸出的結構文字是研究性組合提示，不會執行投注，也不表示可保證回報。

高賠率冷門候選需同時有獨贏賠率高於 15 倍，並有可驗證的輕磅（不多於 129 磅）或內檔（1–4 檔）優勢。只有模型獨贏 EV 為正時，標籤才會顯示 **💣 高 EV 冷門**；EV 缺少或非正時會降級為「高賠率冷門候選（EV 未確認）」。這避免把單靠高賠、輕磅或內檔的馬匹錯稱為正期望值。

| 場內條件 | 報告輸出 | 解讀限制 |
|---|---|---|
| 14 匹或以上，首選勝率 < 20% | ⚠️ 高爆冷風險亂局；不適合作單膽。 | 場內分佈訊號，不是賽果預言。 |
| 首選勝率 ≥ 28% | 相對集中候選的研究性組合提示。 | 仍須覆核臨場賠率、撤回及官方資訊。 |
| 賠率 > 15、輕磅或內檔、模型 EV > 0 | 💣 高 EV 冷門。 | EV 依賴同時點可用賠率及模型校準。 |
| 賠率 > 15、輕磅或內檔、EV 缺失或 ≤ 0 | 高賠率冷門候選（EV 未確認）。 | 不得作正期望值主張。 |

## 6. 每日無條件歸檔與覆盤

`auto_archive_results.py` 不依賴任何對話、貼士或賽前預測。它會對指定日期分別嘗試本地 ETL 和海外官方發現／歸檔，然後只對已完成賽事呼叫覆盤引擎。沒有賽前預測的賽事只會進入 archive；有預測的賽事則生成報告。

```bash
# 只要官方公布資料即執行；不傳 --telegram 時僅產生本地稽核報告
python3 auto_archive_results.py \
  --date 2026-08-17 \
  --db hkjc_last_season.sqlite \
  --schema schema_overseas_racing.sql \
  --archive-dir archive/result_archive_runs

# 僅在主機已安全設定 Telegram 環境變數時，才可要求覆盤摘要推送
python3 auto_archive_results.py \
  --date 2026-08-17 \
  --db hkjc_last_season.sqlite \
  --schema schema_overseas_racing.sql \
  --telegram
```

## 7. 持續執行與 Telegram 的兩個可部署選項

| 方案 | 執行方式與優點 | 取捨 | 設定複雜度 |
|---|---|---|---|
| 使用者現有 Linux 主機 | 以每分鐘賽前排程處理 T-15/T-5；以固定時段執行賽果歸檔。適合已有資料庫、模型和 Python 環境。 | 主機必須保持在線；需由使用者管理 OS 更新、日誌與環境變數。 | 中等。 |
| 受管理的背景服務 | 建立受管理的後端工作，把賽程、回刷進度及報告集中管理。適合日後要調整收件人、閾值或查看覆蓋儀表板。 | 需要一次性部署與 Telegram 憑證設定；長時回刷仍應受官方速率限制。 | 較高。 |

目前工作階段沒有可用的 Telegram 連接設定。程式已採安全降級：若沒有 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID`，不會嘗試傳送，並在覆盤輸出 `telegram_not_configured`。若要啟用 Telegram，請在選定的長期主機以受保護環境變數提供 Bot Token 與 Chat ID；憑證不得寫入 `.py`、SQLite、報告或 Git。

## 8. 驗證範圍

已通過兩個隔離契約測試。`verify_s1s2_feature_enrichment.py` 驗證海外 RPR、久休、場地、G1、T-15/T-5、負磅及近期前四的時間閘門與中性回退。`verify_overseas_archive_audit_guidance.py` 驗證按標頭解析官方賽果欄位、官方最終獨贏結算、Brier Score 寫入、策略結算報告、高爆冷提示及價值冷門報告渲染。測試資料均為標示的隔離合成 fixture，不能用作模型表現、盈利或 ROI 證據。

## References

[1] [HKJC Simulcast Overseas Race — 2023-07-23 S1-8 Meeting Summary](https://racing.hkjc.com/racing/overseas/english/20230723/S1/8/index.aspx?para=/20230723/S1/8)

[2] [HKJC Simulcast Overseas Race — Results Route](https://racing.hkjc.com/en-us/overseas/results?RaceDate=20230723&Racecourse=S1&RaceNo=8)
