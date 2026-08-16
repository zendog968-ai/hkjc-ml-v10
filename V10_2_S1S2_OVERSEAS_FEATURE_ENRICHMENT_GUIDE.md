# V10.2 S1/S2 Overseas Feature Enrichment Guide

> **定位：** 此功能為海外 S1/S2 的冷啟動先驗增加可驗證的賽前訊號，而非將香港 ELO、最終賠率或賽後資料移植到海外模型。任何欄位缺失或來源／時間未驗證時，對應訊號必須回到中性。HKJC 海外賽卡可公開顯示馬匹生涯、近績、場地狀況、騎師及練馬師等內容；可顯示欄位會因賽區和賽日而異。[1]

## 1. 特徵契約與啟用條件

| 特徵 | V10.2 欄位 | 啟用條件 | 強度處理 | 缺失行為 |
|---|---|---|---|---|
| 國際能力先驗 | `international_rating`、`rating_type` | 只有 RPR、IFHA、World Rating 或 International Rating，且具 `rating_source_url` 與 `rating_as_of_utc`。 | 相對同場已驗證評分的 log-strength，截斷於 ±0.18。 | 不使用；不以手動／不明評分補值。 |
| 久休天數 | `days_since_last_run` | `last_run_date` 為可解析日期且早於賽日。 | 先記錄，現時方向性權重為 0，待時間外校準。 | `null`。 |
| 場地適應 | `going_suitability` | 預測時間前已歸檔海外結果，馬匹有相同官方 going、結算名次及開跑時間。 | Beta 等效 16 起縮減；log-strength 截斷於 ±0.12。 | 0 相對訊號。 |
| 練馬師 G1 | `trainer_g1_win_rate` | 來源明確標示 Group 1／G1，具 starts、wins 與不晚於預測時間的 timestamp。 | Beta 等效 20 起；log-strength 截斷於 ±0.08。 | `null` 與 0 相對訊號。 |
| 閘前落飛 | `odds_drop_ratio`、`odds_drop_weight` | 同一馬、同一場、完整 T-15及T-5 official snapshot；捕捉標籤距離預定開跑均不超過 180 秒，模型在 T-5 後 300 秒內生成。 | 若 `ratio ≤ -0.20`，使用實驗性 `+0.20` log-strength；可透過參數關閉。 | `false` 與 0 權重。 |

海外 HKJC 賽卡的示例頁可顯示賽事路程、草／泥地及場地狀況、馬匹 Career、Last 5 Run、Win Rate 和 Top 3 Rate；該頁不保證每場均提供 RPR／IFHA、上仗日期或按場地統計。[1] 因此，解析器只讀取實際的 RPR／IFHA／World／International Rating 欄，不會將普通 rating、新聞文字或專家意見誤作國際評分。

## 2. 新資料表與審計欄位

`schema_overseas_racing.sql` 已新增 `overseas_odds_snapshots` 和 `overseas_odds_snapshot_runners`，以保存逐馬的 T-15／T-5 Win／Place 價格；每個 snapshot 包含捕捉時間、狀態、來源 URL 與可選原始文件關聯。`overseas_prerace_predictions` 亦保存特徵值、`feature_detail_json` 及實際落飛權重，讓日後覆盤能重建當時可用的訊號。

> `fetch_hkjc_s1s2.py` 只有在同時提供 `--snapshot-label` 及 `--scheduled-start-utc` 後，才會把 T-15／T-5 標籤判定為時間合格。若偏差大於 180 秒，快照仍可保留作稽核，但狀態為 `degraded`，預測器不會使用它計算落飛。

## 3. 賽前工作流程

以下示例以真實即將開跑的海外轉播賽為前提；日期、代碼、場次及開跑時間都必須取自 HKJC 官方排位／賽期資料。[2]

```bash
cd /home/ubuntu/hkjc_v10_database

# T-15：必須以官方 UTC 開跑時間核對捕捉時點
python3 fetch_hkjc_s1s2.py \
  --date 2026-09-01 --simulcast-code S1 --race-no 3 \
  --scheduled-start-utc 2026-09-01T12:00:00+00:00 \
  --snapshot-label T_MINUS_15 \
  --db hkjc_last_season.sqlite --output s1s2_card_t15.json

# T-5：同一 official race identity
python3 fetch_hkjc_s1s2.py \
  --date 2026-09-01 --simulcast-code S1 --race-no 3 \
  --scheduled-start-utc 2026-09-01T12:00:00+00:00 \
  --snapshot-label T_MINUS_5 \
  --db hkjc_last_season.sqlite --output s1s2_card_t5.json

# 只在完整 T-15/T-5 對與模型時序合格時使用落飛訊號
python3 predict_s1s2.py \
  --race-card s1s2_card_t5.json --db hkjc_last_season.sqlite \
  --odds-drop-log-weight 0.20 \
  --output-json s1s2_prediction.json --output-md s1s2_prediction.md
```

`--odds-drop-log-weight 0.20` 是海外研究用的初始敏感度，不是已證明的最優權重。若尚未有至少 100 場完整、時間對齊及已結算的海外樣本，應改用 `--odds-drop-log-weight 0`，僅保存快照與 ratio，待走步驗證後才比較 0、0.05、0.10、0.20 等候選權重。

## 4. 校準與回測限制

每次模型參數調整均須採用時間順序：以前期資料估計或選擇權重，固定套用至未見的下一期，再報告 Brier score、log loss、Top 1／Top 3 及包含賽前價格的 ROI／回撤。不能從最終賠率、已知派彩、專家推薦、賽後評分或賽後更新的馬匹資料反推先驗。

HKJC 部分海外 jockey／trainer ranking 頁展示的是賽區當季總計 starts、wins 和 strike rate，且頁面本身說明相關內容為第三方提供並未獲 HKJC 驗證；它不是 Group 1 勝率。因此，系統不會將該一般季績欄直接填入 `trainer_g1_win_rate`。[3]

### References

[1] [HKJC Overseas Racing Information and Race Card](https://racing.hkjc.com/en-us/overseas/)

[2] [HKJC Simulcast Fixture](https://racing.hkjc.com/en-us/overseas/simulcast_fixture)

[3] [HKJC overseas jockey and trainer ranking sample](https://racing.hkjc.com/racing/overseas/english/20240224/S1/1/jockey-trainer-ranking.aspx?para=/20240224/S1/1)
