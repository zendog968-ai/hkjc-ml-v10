# V10.2 2023–2026 海外回刷與 S1/S2 全流程整合測試暨覆盤效能總結

**測試日期：2026-08-17**
**程式版本：Git `83dff22`**
**資料範圍：2023-01-01 至 2026-08-17**
**測試資料庫：`integration_2023_2026_run/overseas_integration.sqlite`**

## 執行摘要

本次已執行 V10.2 海外 S1/S2 的端對端整合測試：全期間 HKJC 官方 fixture 發現、單一真實海外 results archive、無條件 archive 編排、`archived_only` 覆盤分流、S1/S2 特徵與預測契約、以及嚴格 Brier field 校驗。程式路徑可運行，並且在沒有賽前預測時正確避免產生虛構命中率、Brier 或 ROI。

真實官方整合測試目前發現 **268 個**海外轉播群組，並成功 archive 了 **1 場**歷史 S1 賽事（2023-07-23 S1-8）。該場有 4 匹已歸檔出賽馬，官方頭馬為 #2 `Golden Monkey (AUS)`；因測試資料庫沒有賽前預測批次，所以結果正確寫為 `archived_only`，而非產生模型覆盤報告。

> **總結判定：整合流程通過；模型效能尚不可評估。** 真實已完成 archive 覆蓋僅為 1／268 群組，即 **0.373134%**，且有 0 個真實賽前預測、0 個可評分 Brier 及 0 個可結算策略 ROI 觀測。因此，本報告不會把合成 fixture 的結果誤表述為 2023–2026 真實模型表現。

| 測試範圍 | 狀態 | 實際結論 |
|---|---|---|
| 2023–2026 官方 fixture 發現 | 部分通過 | 發現 268 個群組，但 `2223` fixture 為空。 |
| 真實海外 results archive | 通過（單場） | 2023-07-23 S1-8 已完成、4 匹出賽馬已入庫。 |
| 無條件 archive → 覆盤分流 | 通過 | 無賽前預測時產生 `archived_only`，沒有虛構報告。 |
| S1/S2 特徵與預測 | 通過（隔離契約） | 10 項特徵／時間閘門／機率守恆均通過。 |
| 嚴格 Brier 覆盤 | 通過（隔離契約） | 馬號正規化、field match、機率守恆及拒絕路徑通過。 |
| 真實模型 Top 1／Top 3／Brier／ROI | N/A | 沒有時間對齊的真實賽前預測樣本。 |
| 真實海外長期校準／Kelly | N/A | 沒有足夠完整已結算海外資料。 |

## 1. 執行命令與測試設計

### 1.1 官方 2023–2026 fixture 發現與單群組 archive

```bash
python3 backfill_overseas_2023_2026.py \
  --db integration_2023_2026_run/overseas_integration.sqlite \
  --schema schema_overseas_racing.sql \
  --raw-dir integration_2023_2026_run/raw \
  --report-dir integration_2023_2026_run/discovery \
  --start-date 2023-01-01 --end-date 2026-08-17 \
  --discovery-only

python3 backfill_overseas_2023_2026.py \
  --db integration_2023_2026_run/overseas_integration.sqlite \
  --schema schema_overseas_racing.sql \
  --raw-dir integration_2023_2026_run/raw \
  --report-dir integration_2023_2026_run/one_meeting_archive \
  --start-date 2023-01-01 --end-date 2026-08-17 \
  --resume --max-meetings 1
```

此設計把全期間發現與真實 results 解析分開。發現群組不是賽果完成證明；只有 official results 中有足夠可解析名次列的賽事才可標記為 `completed`。[1]

### 1.2 S1/S2 預測、覆盤及無條件 archive 編排

```bash
python3 verify_s1s2_feature_enrichment.py
python3 verify_overseas_archive_audit_guidance.py

python3 auto_archive_results.py \
  --date 2023-07-23 \
  --db integration_2023_2026_run/overseas_integration.sqlite \
  --schema schema_overseas_racing.sql \
  --archive-dir integration_2023_2026_run/auto_archive \
  --raw-dir integration_2023_2026_run/raw \
  --seasons 2324 --skip-local
```

所有測試使用獨立 SQLite 與 archive 路徑；不覆寫主資料庫、模型或先前賽前輸出。

## 2. 真實官方回刷與 archive 結果

### 2.1 全期間 fixture 發現

| 賽季代碼 | 發現海外群組 | 狀態 | 判讀 |
|---|---:|---|---|
| `2223` | 0 | `empty` | 仍是官方來源覆蓋缺口。 |
| `2324` | 73 | `complete` | 只代表 fixture 群組可發現。 |
| `2425` | 81 | `complete` | 只代表 fixture 群組可發現。 |
| `2526` | 100 | `complete` | 只代表 fixture 群組可發現。 |
| `2627` | 14 | `complete` | 只代表 fixture 群組可發現。 |
| **總計** | **268** | `incomplete_or_unverifiable` | `2223` 缺口使全量覆蓋尚未成立。 |

`overseas_backfill_summary.json` 將 discovery 設計為可稽核佇列；`strict_status='complete'` 必須同時滿足所有已發現賽事皆已完成、且沒有 fixture discovery issue。現階段此門檻沒有達成。

### 2.2 真實 S1-8 archive 與無條件覆盤

無條件 archive 編排於 2023-07-23 發現並處理 S1 群組。結果如下：

| 資料庫項目 | 實際值 | 解讀 |
|---|---:|---|
| 已發現 meetings | 268 | 全期間官方 fixture 發現資料。 |
| 已 archive races | 1 | 真實 results 端對端解析案例。 |
| completed races | 1 | 2023-07-23 S1-8。 |
| partial races | 0 | 此隔離測試執行未留下 partial race。 |
| official starters | 4 | 名次與最終 Win odds 已寫入。 |
| official dividends | 0 | 該來源沒有已解析的正式派彩列。 |
| post-race audits | 1 | 正確寫入 `archived_only`。 |
| 真實 prerace predictions | 0 | 不能計算 Top 1／Top 3、Brier 或 ROI。 |

| 名次 | 馬號 | 馬名 | 最終獨贏賠率 |
|---:|---:|---|---:|
| 1 | 2 | Golden Monkey (AUS) | 1.5 |
| 2 | 4 | Cavalry (NZ) | 4.3 |
| 3 | 1 | Super Salute (AUS) | 7.1 |
| 4 | 3 | Invincible Tycoon (AUS) | 19.0 |

覆盤列的實際狀態為：

```json
{
  "had_prerace_prediction": 0,
  "brier_score": null,
  "brier_status": "not_scored_no_prerace_prediction",
  "brier_field_size": 4,
  "brier_uniform_baseline": 0.75,
  "brier_probability_sum": null,
  "status": "archived_only"
}
```

這是預期的安全行為。系統只歸檔官方賽果，並且清楚保留無法評分的理由；不會根據賽後資料重建預測或自行生成成功率。[2]

## 3. S1/S2 預測與特徵整合測試

`verify_s1s2_feature_enrichment.py` 最新執行為 `passed`。該測試建立一個有預測時間切點的隔離 SQLite，注入合法的賽前賽績、RPR、場地資料、練馬師紀錄及 T-15／T-5 快照，再執行 `predict_s1s2.py`。

| 契約 | 結果 | 驗證證據 |
|---|---|---|
| 場內 Win 機率守恆 | 通過 | 所有 runner 勝率合計為 1。 |
| RPR／國際評分 | 通過 | RPR `118` 被用作能力先驗。 |
| 久休 | 通過 | `days_since_last_run=17`。 |
| 場地適應 | 通過 | 僅使用 cutoff 前的完成資料。 |
| 練馬師 G1 | 通過 | 套用時間閘門。 |
| T-15／T-5 落飛 | 通過 | `odds_drop_ratio=-0.25`、flag 為真。 |
| 場內相對負磅 | 通過 | 118 磅相對場均 124.67 磅，訊號受上限保護。 |
| 近期前四縮減 | 通過 | Beta shrinkage、僅使用 cutoff 前歷史。 |
| 缺失資料中性退化 | 通過 | 沒有可用資料不推斷優勢。 |
| 預測審計欄位 | 通過 | 相關 feature audit 值成功寫入。 |

該結果證明功能整合正確，但它是合成契約測試，並非 2023–2026 真實海外歷史模型成績。

## 4. Archive、覆盤、Brier 與報告整合測試

`verify_overseas_archive_audit_guidance.py` 最新執行為 `passed`。它以標示為合成的官方格式 HTML 驗證：官方結果解析 → SQLite 寫入 → 覆盤 → Markdown 報告 → 高爆冷提示渲染。

| 檢查 | 結果 |
|---|---|
| 有標籤官方結果欄位寫入 | 通過 |
| 不完整結果不被靜默標成 completed | 通過 |
| 官方最終 Win odds 的研究籃子結算 | 通過 |
| Brier 寫庫 | 通過 |
| 馬號字串／整數正規化 | 通過 |
| 頭馬與 pre-race field 完全匹配 | 通過 |
| 機率總和守恆 | 通過 |
| 高爆冷單膽警告 | 通過 |
| 高 EV 冷門標籤 | 通過 |

合成覆盤 fixture 的數值為 Top 1 `0`、Top 3 `1`、研究籃子 ROI `+150%`、Brier `0.78`、field size `3`、等機率基準 `0.6667`、機率總和 `1.0`。該 fixture 的 Brier **高於**其等機率基準，所以不能被解讀為模型優勢；它只證明演算法、寫庫、校驗狀態及報告格式可運行。

## 5. 嚴格 Brier 校驗結果

目前 `post_race_audit.py` 只在以下所有條件成立時才輸出分數：

1. 官方 field 有有效且不重複的正整數馬號；
2. 官方結果只有一匹頭馬；
3. 每一匹預測馬可正規化為正整數，且沒有重複；
4. 預測馬號集合與官方 field 完全相同；
5. 頭馬存在於預測 field；
6. 每個勝率均有限、位於 `[0,1]`，且總和為 `1±1e-6`。

不通過時 `brier_score=NULL`，`brier_status` 保存拒絕原因，並保留 field size、等機率基準與可用機率總和。這使批量覆盤可以排除不完整資料，而不是把無效資料與正常 Brier 混合平均。

## 6. 覆盤效能總結

### 6.1 真實可用效能

| 指標 | 真實可用樣本 | 結果 | 判定 |
|---|---:|---|---|
| 已完成官方海外賽事 | 1 | S1-8 已 archive。 | 僅證明 archive 路徑。 |
| 有賽前預測的已完成海外賽 | 0 | N/A | 不可計算命中率。 |
| 可評分的 Brier | 0 | N/A | 不可談校準。 |
| 官方 Win strategy settlement | 0 | N/A | 不可談 ROI。 |
| Place strategy settlement | 0 | N/A | 等待正式 Place dividends 正規化。 |
| 落飛訊號表現 | 0 | N/A | 沒有時間對齊快照與結果。 |
| Kelly 走步評估 | 0 | N/A | 未達最低 100 場門檻。 |

已完成群組覆蓋率為 `1 / 268 = 0.373134%`。這不具備推斷任何海外預測優勢、報酬或風險參數的統計意義。

### 6.2 合成路徑效能

| 指標 | 合成 fixture 結果 | 可解讀內容 | 不可解讀內容 |
|---|---:|---|---|
| Top 1 | 0／1 | 覆盤命中比較可正確運行。 | 真實命中率。 |
| Top 3 | 1／1 | 排名結果可正確運行。 | 真實 Top 3 能力。 |
| Brier | 0.78 | 嚴格 field／機率校驗後正確寫庫。 | 優於基準或市場。 |
| Win ROI | +150% | 官方最終 Win odds 結算算術與輸出可運行。 | 可複製的投資回報。 |
| 高爆冷提示 | 成功渲染 | 14 匹／首選 <20% 的警告機制可用。 | 爆冷預測準確率。 |

## 7. 主要風險與下一步驗收門檻

| 風險／缺口 | 現時控制 | 正式驗收所需條件 |
|---|---|---|
| `2223` fixture 空白 | discovery audit 明確記為 `empty`。 | 調查官方歷史可用性，或正式界定範圍排除。 |
| 267 個群組尚未 archive | `--resume` 與 raw archive 可安全續跑。 | 限速分批完成並取得 `strict_status=complete`。 |
| 沒有真實賽前預測 | `archived_only` 防止虛構覆盤。 | 在開跑前保存 prediction batch 與 time provenance。 |
| Brier／ROI N/A | 嚴格狀態碼防止誤平均。 | 至少有同場官方結果與 pre-race field 完全匹配。 |
| Kelly／落飛未校準 | 固定閘門。 | 至少 100 場完整、時間對齊、已結算海外賽。 |
| 最新預測時間閘門 | 仍待實作。 | 自動載入只允許 `generated_at_utc < scheduled_start_utc`。 |

## 8. 結論

本次完整整合測試證明 V10.2 的海外資料管線可以從官方 fixture 發現到真實單場結果 archive，再由無條件 archive 編排正確進入 `archived_only` 覆盤分流；同時 S1/S2 賽前特徵、Win／Place 預測、嚴格 Brier 校驗及高爆冷報告的契約層均已通過。

不過，2023–2026 的海外全量歷史資料**尚未完成回刷**，目前只確認 1 場真實 completed race，且沒有對應賽前預測。因此，真實模型的 Top 1、Top 3、Brier、ROI、落飛效益及 Kelly 回撤全部保持 N/A。任何將本次整合測試描述為已證實海外模型長期績效的說法均不正確。

下一個可量化里程碑，是先完成官方 historical results 的可恢復 archive 與賽前預測捕捉，然後在至少 100 場完整、時間對齊、經嚴格 field 校驗的海外賽事上執行季度走步評估。[1] [2]

## References

[1] [HKJC Simulcast Overseas Fixture](https://racing.hkjc.com/en-us/overseas/simulcast_fixture)

[2] [HKJC Overseas Results: 2023-07-23 S1-8](https://racing.hkjc.com/en-us/overseas/results?RaceDate=20230723&Racecourse=S1&RaceNo=8)
