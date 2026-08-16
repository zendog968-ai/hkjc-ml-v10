# V10.2 海外轉播賽、全賽果歸檔與覆盤操作手冊

> **版本：V10.2 Overseas Simulcast & Post-Race Audit Extension**
> **作者：Manus AI**
> **資料原則：只以香港賽馬會（HKJC）官方公開頁面、其已渲染內容或經驗證的官方資料回應寫入資料庫；空白頁、載入頁、最終賠率替代值及第三方資料一律不可補成賽果。**

## 1. 已加入的系統能力

V10.2 現在將海外轉播賽與本地賽事分層處理。`schema_overseas_racing.sql` 會在現有 SQLite 庫中以附加資料表保存海外賽期、S1/S2 群組、單場、出賽馬匹、派彩、原始來源文件、賽前預測及覆盤帳本；它不會改寫現有香港本地 `races` 或 `starters` 表。

| 模組 | 主要職責 | 不能做的事情 |
|---|---|---|
| `backfill_overseas_2023_2026.py` | 由 HKJC 官方 fixture 發現海外群組、限速保存來源、可續跑抓取及輸出嚴格覆蓋報告。 | 不可把 fixture 缺口、HTTP 空殼或未解析結果標為完成。 |
| `fetch_hkjc_s1s2.py` | 讀取官方 S1/S2 賽卡及可用公開 Win／Place 賠率。 | 賠率不可用時不得填零或使用過期價格。 |
| `predict_s1s2.py` | 以公開海外生涯記錄建立冷啟動先驗，並以向量化 Plackett–Luce 輸出 Win／Place 相對機率。 | 不會把香港馬匹／騎師 ELO 硬套於海外馬匹。 |
| `auto_archive_results.py` | 不依賴賽前對話或貼士，協調本地與海外官方賽果歸檔；完成後呼叫覆盤。 | 不會在官方結果尚不可解析時虛構名次、派彩或完成時間。 |
| `post_race_audit.py` | 比對已保存的預測、Top 1、Top 3、熱門穩攻、冷門突襲及落飛標籤。 | 沒有賽前預測時不會生成虛假命中率或推送訊息。 |

## 2. 官方來源與資料覆蓋現況

海外 fixture 使用 HKJC 的 Simulcast Fixture 頁作**群組發現層**。官方頁面會以動態內容呈現部分歷史資料，因此發現器使用受控、單一、限速的瀏覽器渲染方式；所有回應會保存 URL、抓取時間、SHA-256、解析版本及結果狀態。

2026-08-16 執行的官方 fixture 發現範圍為 `2023-01-01` 至 `2026-08-16`，得到以下結果。這是**已發現轉播群組數**，不是已完成單場賽果數。

| HKJC fixture 賽季代碼 | 已發現海外群組 | 狀態 |
|---|---:|---|
| 2223 | 0 | 官方 fixture 現頁未提供可解析 2023 年 1–6 月群組；列為未驗證缺口。 |
| 2324 | 73 | 已發現。 |
| 2425 | 81 | 已發現。 |
| 2526 | 100 | 已發現。 |
| 2627（截至 2026-08-16） | 14 | 已發現。 |
| **合計** | **268** | 僅為已發現群組。 |

目前官方單場結果摘要頁在此隔離環境對歷史及近期樣本都持續載入、未呈現可解析名次／派彩資料。因此主資料庫現況為 `overseas_races=0`、`overseas_starters=0`、`overseas_dividends=0`，嚴格覆蓋狀態為 `incomplete_or_unverifiable`。這不是「零場海外賽」，而是**不可用官方結果來源的明確缺口**。系統故意拒絕將這 268 個群組聲稱為全量完成。

> 要達成「零缺漏」歷史回刷，必須取得 HKJC 可驗證的歷史海外單場賽果／派彩來源，或在具正常官方資料存取的持久主機重跑並以覆蓋報告核實每個已發現群組。不得使用最終市場截圖、第三方結果或人工猜測補值。

## 3. 海外回刷操作

先進行發現與覆蓋稽核；這一步不抓取所有單場結果。

```bash
cd /home/ubuntu/hkjc_v10_database

python3 backfill_overseas_2023_2026.py \
  --db hkjc_last_season.sqlite \
  --start-date 2023-01-01 \
  --end-date 2026-08-16 \
  --seasons 2223,2324,2425,2526,2627 \
  --discovery-only \
  --report-dir overseas_backfill_reports
```

只有當覆蓋報告確認可解析官方結果頁後，才使用以下受控、可續跑命令；預設每次請求間隔為 3–6 秒，並每 20 次請求冷卻，防止對 HKJC 公開頁造成壓力。

```bash
python3 backfill_overseas_2023_2026.py \
  --db hkjc_last_season.sqlite \
  --start-date 2023-01-01 \
  --end-date 2026-08-16 \
  --resume \
  --report-dir overseas_backfill_reports
```

輸出包括 `overseas_meeting_coverage.csv`、`overseas_backfill_summary.json` 與 `overseas_backfill_attempts.json`。只有當 `strict_status` 為 `complete`、每個已發現群組均已核實場次、且每場均有可解析官方結果列時，才可把回刷稱為完整。

## 4. S1/S2 賽前流程

海外轉播賽報告會標註 **「🌍 海外轉播賽 (S1/S2)」**。以下命令以官方日期、群組及場次建立賽卡；若即時公開賠率頁失敗，程序仍會輸出馬匹與先驗勝率，但 EV 及 Kelly 會保留空白。

```bash
python3 fetch_hkjc_s1s2.py \
  --date 2026-09-01 --simulcast-code S1 --race-no 3 \
  --db hkjc_last_season.sqlite \
  --output s1s2_race_card.json

python3 predict_s1s2.py \
  --race-card s1s2_race_card.json \
  --db hkjc_last_season.sqlite \
  --output-json s1s2_prediction.json \
  --output-md s1s2_prediction.md
```

預測器使用公開的生涯場數、勝場及位置表現作 Beta 平滑先驗。沒有可用海外生涯資料的馬匹使用中性場內先驗；此冷啟動做法比把香港 ELO 視作海外能力更保守。位置率使用 100,000 次向量化 Plackett–Luce 模擬；公開 Win／Place 賠率存在時，計算 `EV = p × odds − 1`，而 Kelly 只對可用獨贏價輸出並以 5% 上限截斷。

## 5. 無條件賽果歸檔與賽後覆盤

以下命令無論是否曾生成賽前預測，均會嘗試歸檔指定日的本地及海外官方結果。所有執行摘要會寫到 `archive/result_archive_runs/YYYY-MM-DD/`。海外官方來源不可用時，摘要會是 `partial_or_error` 或 `no_official_simulcast_found`，而不是成功。

```bash
python3 auto_archive_results.py \
  --date 2026-09-01 \
  --db hkjc_last_season.sqlite \
  --archive-dir archive/result_archive_runs
```

自動覆盤只對已完成且有官方賽果的場次執行。海外場次會優先從 `overseas_prerace_predictions` 讀取最新的賽前預測批次；本地場次可傳入賽前 prediction JSON。若沒有預測，覆盤表 `post_race_audits` 寫入 `archived_only`，不會推送預測檢討報告。若有預測，報告比較官方前四名、Top 1、Top 3、雙策略與落飛標記。

Telegram 是可選的降級整合。只有持久主機已安全設定 `TELEGRAM_BOT_TOKEN` 及 `TELEGRAM_CHAT_ID` 時才加入 `--telegram`；憑證絕不可寫入 Git、SQLite、報告或聊天訊息。

## 6. 測試與限制

已完成的離線模組契約測試涵蓋：海外 schema、S1/S2 冷啟動機率、無賠率降級、賽前預測寫庫、官方前四名覆盤、最新預測批次自動讀取，以及未設定 Telegram 的安全跳過。測試 fixture 均明確標示為合成資料，只驗證程式契約，**不能解讀為真實海外賽事模型績效或 ROI**。

全量海外單場結果回刷尚未完成，原因是官方結果層在當前隔離環境無法產生可解析內容；覆蓋報告已正確保留此缺口。後續應先解決可驗證的官方歷史結果資料存取，再用 `--resume` 分批執行，並以 coverage CSV 逐日核對。

## References

[1] [Hong Kong Jockey Club — Simulcast Fixture](https://racing.hkjc.com/en-us/overseas/simulcast_fixture)

[2] [Hong Kong Jockey Club — Overseas Simulcast Racing](https://racing.hkjc.com/en-us/overseas/)

[3] [Hong Kong Jockey Club — Betting Guide](https://special.hkjc.com/e-win/en-US/betting-info/racing/beginners-guide/guide/)
