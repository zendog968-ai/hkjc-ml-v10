# V10.2 2023–2026 海外回刷與 S1/S2 模組整合測試報告

**系統版本：V10.2 Advanced Feature & Ensemble Edition**
**程式版本：Git `10aa75c`**
**報告日期：2026-08-17**
**報告作者：Manus AI**

## 執行摘要

本報告測試 V10.2 的海外 S1/S2 流程，包括 2023-01-01 至 2026-08-17 的 HKJC 官方 fixture 發現、SQLite schema 與續跑控制、海外賽前特徵、Win／Place 機率與 EV／Kelly 輸出、官方結果 archive、賽後覆盤、Brier Score、分佈風險提示及自動歸檔編排。

**程式整合與隔離契約測試均已通過；歷史全量賽果回刷尚未完成，亦不可宣稱為已完成。** 真實官方 fixture 預檢發現 268 個海外轉播群組，但主資料庫現時只有 1 場已嘗試解析的海外賽事，狀態為 `partial`，沒有任何 `completed` 海外賽、出賽馬、派彩、預測批次或海外覆盤紀錄。原因是該歷史 Results 頁在測試時呈現空結果列；系統已正確保留缺口，而非將 HTTP 頁面成功誤列為完整賽果。

> **整體判定：有條件通過（程式與合成整合層）；歷史資料覆蓋未通過。** S1/S2 可在符合輸入契約的情況下生成可稽核的研究性預測及報告，但 2023–2026 全量歷史結果資料庫、正式海外模型校準、歷史 ROI、Brier 趨勢及 Kelly 走步驗證，均仍處於不可驗收狀態。

| 驗收範圍 | 狀態 | 核心結論 |
|---|---|---|
| HKJC 官方 fixture 發現 | **通過但有來源缺口** | 發現 268 個群組；`2223` fixture 回應沒有可解析群組。 |
| 2023–2026 全量賽果回刷 | **未通過** | 主庫為 0 completed overseas races，不能稱為全量完成。 |
| SQLite schema 與回刷狀態控制 | **部分通過** | 可保存 discovered／partial／completed；既有一筆 legacy `race_count_verified` 需修復為可續跑。 |
| S1/S2 特徵與預測整合 | **通過（隔離契約）** | 10 項特徵、時間閘門、機率守恆及 SQLite 審計欄位均通過。 |
| 結果 archive、覆盤及 Brier | **通過（隔離契約）** | 官方欄位、Win 研究籃子、Brier、報告渲染均通過。 |
| 無條件自動 archive 編排 | **程式流程通過；真實日終端對端未通過** | 只覆盤 completed 官方賽事；現時無 completed 海外資料可作真實驗收。 |
| 海外 ROI／Kelly／校準 | **N/A** | 不存在足夠完整已結算海外樣本。 |

## 1. 測試範圍與資料來源

海外資料只以 HKJC 公開的海外轉播 fixture、賽事資料與結果頁為來源。回刷器不會用第三方賽果、推斷名次、其他市場的派彩或賽後補值來填補缺口；任何沒有可解析官方結果列的頁面都必須保留為 `partial` 或 `source_unavailable`。[1] [2]

| 層次 | 被測元件 | 測試方式 | 實際資料／合成資料 |
|---|---|---|---|
| 發現 | `backfill_overseas_2023_2026.py` | 真實 HKJC fixture 預檢 | 真實官方公開頁。 |
| 歸檔 | `overseas_hkjc_core.py` | 一個真實歷史群組嘗試，加上合成官方 HTML 契約 fixture | 混合。 |
| S1/S2 賽前 | `fetch_hkjc_s1s2.py`、`predict_s1s2.py`、`overseas_feature_enrichment.py` | 端對端 SQLite fixture | 隔離合成資料。 |
| 賽後覆盤 | `post_race_audit.py` | 端對端 SQLite fixture 與官方格式結果 HTML | 隔離合成資料。 |
| 無條件編排 | `auto_archive_results.py` | 程式流程及 completed-only 選擇規則檢閱 | 靜態／整合檢視。 |
| 長期評估 | odds-drop、Kelly、Brier、ROI | 資料量閘門檢查 | 主庫實況。 |

## 2. 2023–2026 官方回刷發現測試

### 2.1 執行設定

已對 `2023-01-01` 至 `2026-08-17` 使用回刷器的官方 fixture 發現模式執行預檢。日期輸入為 ISO `YYYY-MM-DD`，賽季代碼為 `2223,2324,2425,2526,2627`。發現模式只建立可稽核的 meeting 清單，不會把 fixture 條目誤當成已完成賽果。

```bash
python3 backfill_overseas_2023_2026.py \
  --db hkjc_last_season.sqlite \
  --schema schema_overseas_racing.sql \
  --start-date 2023-01-01 \
  --end-date 2026-08-17 \
  --discovery-only \
  --report-dir overseas_backfill_preflight
```

### 2.2 真實 fixture 發現結果

| HKJC season code | 發現群組數 | 官方發現狀態 | 判讀 |
|---|---:|---|---|
| `2223` | 0 | `empty` | 官方 fixture 頁在本次預檢沒有可解析群組；此範圍是未驗證缺口。 |
| `2324` | 73 | `complete` | fixture 層發現完成，不等於結果列完成。 |
| `2425` | 81 | `complete` | fixture 層發現完成，不等於結果列完成。 |
| `2526` | 100 | `complete` | fixture 層發現完成，不等於結果列完成。 |
| `2627` | 14 | `complete` | fixture 層發現完成，不等於結果列完成。 |
| **總計** | **268** | `incomplete_or_unverifiable` | `2223` 缺口令全量宣稱不成立。 |

回刷程式的 `strict_status='complete'` 需要至少一場已發現賽事、所有已發現賽事均為 `completed`，並且沒有 fixture discovery issue。因此，發現 268 個群組是**後續逐場 archive 的工作佇列**，不是全量賽果完成證明。

### 2.3 真實結果頁歸檔測試

以 2023-07-23 S1 作單群組測試，回刷輸出顯示 1 場已發現賽事、0 completed、1 partial、完成率 0%。主資料庫當前再核對的結果一致：268 meetings、1 overseas race、0 completed races、1 partial race、0 starters、0 dividends。

| 主資料庫海外項目 | 實際計數 | 測試意義 |
|---|---:|---|
| `overseas_meetings` | 268 | 官方 fixture 群組已存檔。 |
| `overseas_races` | 1 | 只完成一個受限真實 archive 嘗試。 |
| `overseas_races.completed` | 0 | 沒有可用於歷史結果、ROI 或 Brier 評估的實際海外賽事。 |
| `overseas_races.partial` | 1 | 結果頁沒有可解析完成列，缺口被保留。 |
| `overseas_starters` | 0 | 真實官方結果馬匹列尚未成功歸檔。 |
| `overseas_dividends` | 0 | 真實官方派彩尚未成功歸檔。 |
| `overseas_odds_snapshots` | 0 | 主庫沒有可供真實 T-15／T-5 回測的海外快照。 |
| `overseas_prerace_predictions` | 0 | 主庫沒有時間對齊的真實海外預測批次。 |
| `post_race_audits`（overseas） | 0 | 不存在可供真實海外覆盤的 completed race。 |

這項測試**通過了缺口處理要求**：空結果表沒有被寫成 `completed`。但它**沒有通過全量資料覆蓋要求**，因為資料列仍未取得。

### 2.4 已知續跑狀態風險

主庫有一個先前測試遺留的 meeting 仍標示為 `race_count_verified`，而其子賽事是 `partial`。目前 `--resume` 會選取 meeting 狀態 `discovered`、`partial` 或 `source_unavailable`，故該 legacy 狀態需要一次性資料修復為 `partial`，否則不會自動再次選中。現行 `archive_meeting()` 已改為在新執行出現 partial／unavailable race 時將 meeting 標為 `partial`；仍須對既有資料庫執行遷移驗收。

## 3. S1/S2 賽前整合測試

### 3.1 執行的端對端契約測試

```bash
cd /home/ubuntu/hkjc_v10_database
rm -rf s1s2_feature_enrichment_fixture
python3 verify_s1s2_feature_enrichment.py
```

最新執行狀態為 `passed`。測試建立隔離 SQLite、寫入預測時間前完成的海外歷史、建立賽卡及 T-15／T-5 快照，再以 `predict_s1s2.py` 輸出 JSON、Markdown 和 `overseas_prerace_predictions` 審計列。

| 測試契約 | 結果 | 驗證內容 |
|---|---|---|
| 場內勝率守恆 | 通過 | 所有馬匹的 `predicted_win_probability` 合計為 1。 |
| RPR 能力先驗 | 通過 | 已驗證 RPR `118` 被讀入。 |
| 久休 | 通過 | `days_since_last_run=17` 正確產生。 |
| 場地適應 | 通過 | 只使用預測時間切點前完成的場地賽果。 |
| 練馬師 G1 | 通過 | 只使用合格時間閘門內資料。 |
| T-15／T-5 落飛 | 通過 | 完整快照產生 `odds_drop_ratio=-0.25` 及 flag。 |
| 場內相對負磅 | 通過 | `118` 磅相對場均 `124.67`，訊號 `+0.0267`，位於設計上限內。 |
| 近期前四 Beta 縮減 | 通過 | `3/3` 前四仍縮減為 `recent_top4_log_signal≈+0.00305`。 |
| 缺失資料回退 | 通過 | 沒有可用負磅／近績資料時維持中性。 |
| 預測審計欄位 | 通過 | 新增負磅與前四頻率欄位可寫入資料庫。 |

此測試證明功能契約與資料時間閘門可運行。它不證明 RPR、G1、場地或落飛訊號在真實 2023–2026 市場具有可重現的預測增益，因為主庫沒有完整已結算海外樣本。

### 3.2 Win／Place、EV 與 Kelly 的整合行為

S1/S2 預測器已輸出以下介面：場內相對 Win 機率、Plackett–Luce 位置機率代理、可用公開 Win／Place 價格下的 EV、及受限 Kelly 比例。賠率缺失時，對應 EV 和 Kelly 保持空值，不會用舊賠率或猜測價格補值。海外冷啟動層使用公開生涯資料與預測時間前 archive，而不會套用香港馬匹 ELO。

> **測試限制：** 現行主資料庫沒有真實海外 odds snapshots、prediction batches 或 completed results，故「EV 正值」「Kelly > 0」只代表 fixture 的算術和風險上限路徑，不代表真實海外可投資性或長期 ROI。

## 4. 結果 archive、賽後覆盤與高爆冷提示測試

### 4.1 隔離測試結果

```bash
cd /home/ubuntu/hkjc_v10_database
rm -rf overseas_archive_audit_guidance_fixture
python3 verify_overseas_archive_audit_guidance.py
```

最新執行狀態為 `passed`。測試使用標示的合成官方格式 HTML，不使用歷史回刷資料，驗證下列整合鏈：`parse_results()` → `apply_results()` → `post_race_audit.py` → `post_race_audits` → Markdown 報告。

| 測試項目 | 結果 | 證據／解讀 |
|---|---|---|
| 結果欄位解析 | 通過 | 成功解析 3 匹、2 個派彩項目、名次、馬位差、完成時間、最終 Win／Place odds、騎師、練馬師、負磅、檔位。 |
| 完整官方賽果狀態 | 通過 | 3 匹具名次 fixture 寫為 `completed`。 |
| Top 1／Top 3 | 通過 | 頭馬不在首選、在前三，結果為 `0`／`1`。 |
| 官方最後 Win 研究籃子 | 通過 | 合併 stake `4.0`、net return `6.0`、ROI `+150%`。 |
| 場內 Brier 寫入 | 通過 | Brier `0.78` 成功寫入 `post_race_audits`。 |
| 策略結算報告 | 通過 | Markdown 包含 `策略結算`。 |
| 高爆冷提示 | 通過 | 14 匹、首選 19% 時輸出不適合作單膽的警示。 |
| 高 EV 冷門標籤 | 通過 | 賠率 20、128 磅、4 檔、正 EV 時可渲染。 |

### 4.2 Brier Score 的測試判讀

海外覆盤的單場 Brier 使用：

\[
B_r=\sum_{i=1}^{N}(p_i-y_i)^2
\]

fixture 機率為 `0.50, 0.30, 0.20`，官方頭馬為第二匹，故分數為：

\[
(0.50-0)^2+(0.30-1)^2+(0.20-0)^2=0.25+0.49+0.04=0.78
\]

此計算與 V10.2 本地 `train_lightgbm.py` 的 race-level Brier 總和口徑一致。惟三匹馬的等機率基準為 `1-1/3=0.6667`，故 fixture 的 `0.78` 較基準差 `0.1133`。因此它只能證明**計算、寫入與顯示的正確性**，不能作為模型優於市場或等機率的成效證據。

另外，Brier 的完整性驗收仍有高優先級待辦：頭馬必須存在於預測名單、馬號型別必須正規化、機率向量必須有限且合計為 1、以及預測生成時間必須嚴格早於開跑。這些拒絕路徑尚未全部寫入固定契約測試，故真實歷史統計前不能把單場數值直接彙總成校準結論。

## 5. 無條件自動歸檔與覆盤分流

`auto_archive_results.py` 會依日期執行本地 ETL、同日海外 fixture 發現和群組 archive，然後只對 `race_status='completed'` 的賽事呼叫 `post_race_audit.py`。這一控制流程符合兩個關鍵要求：結果 archive 不依賴是否曾生成貼士，而沒有賽前預測的 completed race 會由覆盤器標示 `archived_only`，不生成模型成效報告或 Telegram 通知。

| 條件 | 預期系統行為 | 現時驗收 |
|---|---|---|
| 官方本地結果可用 | 執行本地 ETL，寫入指定 archive 日期目錄。 | 程式流程已檢視；非本報告重跑範圍。 |
| 同日官方海外群組可發現且結果可解析 | archive race，狀態 `completed`，進入覆盤候選。 | 尚未有真實海外 completed case。 |
| 官方海外結果頁空白／不可解析 | `partial` 或 `source_unavailable`，不進入覆盤。 | 真實單群組測試已證明不應偽完成。 |
| 沒有賽前預測 | 官方結果入庫；覆盤寫 `archived_only`。 | 程式分流已檢視；尚未有真實海外 completed case。 |
| 有賽前預測且 Telegram 憑證存在 | 生成報告並嘗試發送摘要。 | 未配置 Telegram，未做傳送測試。 |

## 6. 長期校準與回測閘門

海外落飛、負磅、近期前四、EV 與 Kelly 都必須受歷史資料覆蓋限制。現時 0 completed overseas races 代表下列指標全為 **N/A**：

| 指標 | 現況 | 可啟用條件 |
|---|---|---|
| 真實海外 Top 1／Top 3 | N/A | 至少有時間對齊的賽前預測及官方名次。 |
| 平均場內 Brier／等機率基準差 | N/A | 有完整 field、頭馬及守恆的預測機率。 |
| Win／Place 實現 ROI | N/A | 有同時點價格／官方派彩及已結算策略。 |
| 落飛敏感度 | N/A | 有完整、時間合格的 T-15／T-5 快照及結果。 |
| 動態 Kelly walk-forward | N/A | 至少 100 場完整已結算海外賽，且遵守季度時間順序。 |
| 負磅／前四訊號增權 | 禁止 | 至少 100 場完整、時間對齊海外賽後走步驗證。 |

## 7. 正式驗收與重跑程序

正式回刷必須在持續運行的主機執行，維持低頻率、冷卻、原始 HTML archive 和可恢復報告。以下程序不會繞過 HKJC 限制，若發生 403、429、timeout 或空資料，應保留缺口並以 `--resume` 重試。

```bash
cd /home/ubuntu/hkjc_v10_database

# A. 重新建立官方發現覆蓋報告
python3 backfill_overseas_2023_2026.py \
  --db hkjc_last_season.sqlite \
  --schema schema_overseas_racing.sql \
  --start-date 2023-01-01 \
  --end-date 2026-08-17 \
  --discovery-only \
  --report-dir overseas_backfill_reports/discovery_$(date +%F)

# B. 修復既有 legacy partial meeting 狀態後，以限速方式分批續跑
python3 backfill_overseas_2023_2026.py \
  --db hkjc_last_season.sqlite \
  --schema schema_overseas_racing.sql \
  --start-date 2023-01-01 \
  --end-date 2026-08-17 \
  --resume \
  --delay-min 3.0 --delay-max 6.0 \
  --cooldown-every 20 --cooldown-seconds 60 \
  --report-dir overseas_backfill_reports/resume_$(date +%F)

# C. 每批後重跑兩個不依賴歷史表現的程式契約測試
python3 verify_s1s2_feature_enrichment.py
python3 verify_overseas_archive_audit_guidance.py
```

正式驗收前，最新 `overseas_backfill_summary.json` 必須同時滿足下表。若不滿足，報告必須仍標示為 `incomplete_or_unverifiable`，而非以部分覆蓋推論完整性。

| 驗收條件 | 必須值 |
|---|---|
| `fixture_discovery_issues` | 空陣列，或每一個官方不可用賽季有獨立、明確的範圍排除決議。 |
| `races_discovered` | 大於 0。 |
| `races_completed` | 等於 `races_discovered`。 |
| `races_partial` | 0。 |
| `completion_rate` | `1.0`。 |
| `strict_status` | `complete`。 |
| 出賽馬／派彩列 | 與 completed race 覆蓋一致，並保留原始來源連結。 |
| 預測時間閘門 | 所有用於回測的 prediction batches 均早於開跑。 |

## 8. 最終測試判定與風險登錄

| 風險／限制 | 嚴重度 | 目前控制 | 下一步 |
|---|---|---|---|
| 2023–2026 結果未全量回刷 | 阻塞 | 以 `partial`、coverage JSON 和 `strict_status` 禁止偽完成。 | 在持續主機分批 `--resume`。 |
| `2223` fixture 缺口 | 阻塞 | discovery audit 明確記錄為 `empty`。 | 檢查 HKJC 對應歷史頁可用性並形成範圍決議。 |
| legacy partial meeting 不可自動續跑 | 高 | 新版 archive 對新 partial 會標示 meeting `partial`。 | 對主庫遺留資料作一次狀態遷移並驗證選取。 |
| 真實海外 archive 只有空結果表 | 高 | 禁止將空表視為 completed。 | 監察官方頁渲染／格式，重試並保留 raw 原始頁。 |
| 真實海外 ROI／Brier／Kelly | 高 | 缺資料時輸出 N/A。 | 累積完成結果、賽前預測與快照後才做走步評估。 |
| Brier 評分完整性 | 高 | 正常公式及 fixture 通過。 | 加入 field match、機率守恆、馬號正規化及賽前時間拒絕測試。 |
| Telegram 發送 | 中 | 缺憑證時安全降級為本地報告。 | 在長期主機安全設定環境變數後以測試 chat 驗收。 |

## 結論

V10.2 的 S1/S2 模組、特徵時間閘門、Win／Place／EV／Kelly 輸出、結果 archive schema、覆盤與高爆冷提示，在可控制的隔離整合測試中均已通過。真正的 HKJC fixture 發現亦已運行，並建立 268 個可稽核海外轉播群組。

但是，這個發現清單不是歷史賽果資料集。現時主庫仍是 0 completed overseas races，因此不能聲稱 2023–2026 已完成全量賽果回刷，也不能報告真實海外模型命中率、Brier、ROI、Kelly 回撤或任何投資優勢。正確的下一里程碑是完成官方結果列的限速、可恢復 archive，通過 `strict_status='complete'` 與時間完整性驗收，然後才進入最低 100 場的季度走步校準。[1] [2]

## References

[1] [HKJC Simulcast Overseas Fixture](https://racing.hkjc.com/en-us/overseas/simulcast_fixture)

[2] [HKJC Overseas Racing Information](https://racing.hkjc.com/en-us/overseas/)
