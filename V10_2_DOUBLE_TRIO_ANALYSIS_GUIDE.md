# V10.2 孖T彩池分析技能：操作與回測指南

**作者：** Manus AI
**版本：** V10.2 Double Trio Extension
**適用範圍：** 香港賽馬會官方孖T彩池的兩關模型候選、T-15／T-5 組合報價、指標 EV 與歷史回測。

> 孖T的正常勝出條件是指定兩場賽事中，各自命中第一、第二及第三名馬匹，兩關內均不分次序。若沒有正常勝出單位，才按官方規則以第一關頭三作安慰獎。這兩種派彩結果必須嚴格分開。[1]

## 1. 新增能力總覽

| 層次 | 新增項目 | 核心保護 |
|---|---|---|
| 官方資料模型 | `DOUBLE_TRIO`、兩個 `pre_race_pool_event_legs`、`MAIN` 與 `CONSOLATION` 派彩層。 | 以獨立事件、快照、組合成員、結果及派彩表保存。 |
| 兩關組合鍵 | 每關頭三馬號獨立遞增排列，再串接成 canonical key。 | 不會把孖T誤當有順序的三重彩。 |
| V10.2 預測輸出 | `predict.py` 新保留 `horse_no`。 | 候選使用官方馬號，不會用檔位代替馬號。 |
| 候選生成 | `build_double_trio_candidates.py`。 | 先固定模型候選，不看市場報價。 |
| 快照綁定 | `bind_double_trio_candidates.py`。 | 模型時間必須早於相同官方 T-15／T-5 快照。 |
| EV 與回測 | `query_complex_pool_ev.py --mode double_trio`、`backtest_complex_pool_double_trio.py`。 | 賽前 MAIN 指標 EV 與賽後 MAIN／安慰獎 ROI 分離。 |

## 2. 官方語義與資料模型

孖T有兩關，每關均為無順序頭三集合；因此 `expected_leg_count=2`、`selection_ordering=LEGGED`。例如第一關選 9、2、5 號，第二關選 8、1、4 號，固定 key 是：

```text
L1:P1=2|L1:P2=5|L1:P3=9|L2:P1=1|L2:P2=4|L2:P3=8
```

第一關實際名次即使為 9、2、5，仍與上述第一關選擇相符；候選 key 的 `P1` 至 `P3` 只是按馬號遞增的稽核位置，不是跑入名次。香港賽馬會的注數表同時指出，孖T總組合數為兩關各自組合數的乘積，故複式組合須保存每張基本組合及固定研究注額。[2]

| 派彩層 | 正確條件 | 系統處理 |
|---|---|---|
| `MAIN` | 兩關均命中頭三無順序集合。 | 可用相同組合的賽前特定報價計算指標 EV；賽後可按官方 MAIN 派彩結算 ROI。 |
| `CONSOLATION` | 無任何官方 MAIN 派彩時，候選第一關命中官方頭三，並有相符官方安慰獎派彩。 | 只在賽後實現 ROI中結算；不可用 MAIN 報價替代為賽前 EV。 |

## 3. 一個賽日的嚴格工作流程

### 第一步：對兩關分別執行 V10.2 預測

使用已公布的官方排位表對兩關產生 `prediction.json`。預測輸出現在包含 `horse_no`，必須核對它是香港賽馬會官方馬號。

```bash
python3 predict.py --db hkjc_last_season.sqlite --model horse_model.pkl \
  --race-card leg1_race_card.json --output-json leg1_prediction.json --output-csv leg1_prediction.csv

python3 predict.py --db hkjc_last_season.sqlite --model horse_model.pkl \
  --race-card leg2_race_card.json --output-json leg2_prediction.json --output-csv leg2_prediction.csv
```

### 第二步：在市場快照前固定孖T候選

```bash
python3 build_double_trio_candidates.py \
  --leg1-prediction leg1_prediction.json \
  --leg2-prediction leg2_prediction.json \
  --pool-event-code OFFICIAL_DOUBLE_TRIO_CODE \
  --model-generated-at-utc 2026-09-06T10:44:00+00:00 \
  --top-runners-per-leg 5 \
  --max-candidates 30 \
  --research-stake 10 \
  --output double_trio_unbound.json
```

生成器會對每關可選的三匹集合以 Plackett-Luce 排名模型計算**無順序頭三集合機率**，再以兩關集合機率的乘積作聯合機率近似。這是 V10.2 模型研究代理，並非保證命中、保證獲利或個人下注指示。

### 第三步：保存並匯入官方 T-15 或 T-5 快照

只有在官方頁面確實顯示個別孖T組合的估計派彩或顯示價格時，才可對該組合計算 EV。總池額、累積多寶或賽後派彩均不足以替代特定組合的賽前價格。

快照需寫入 `pre_race_pool_events`、`pre_race_pool_event_legs`、`pre_race_pool_snapshots`、`pre_race_pool_selection_quotes` 及 `pre_race_pool_selection_members`；`anchor_leg_no=1`，時間以第一關預定開跑為準。若官方頁僅提供總池額，將 `quote_completeness` 記為 `market_summary_only`，並保留 EV 為 `N/A`。

### 第四步：以時間完整性綁定候選與快照

```bash
python3 bind_double_trio_candidates.py \
  --db hkjc_odds_snapshot_archive.sqlite \
  --unbound-candidates double_trio_unbound.json \
  --pool-snapshot-id 1234 \
  --snapshot-label T_MINUS_15 \
  --output double_trio_t15_bound.json
```

綁定器要求模型候選生成時間不晚於快照，並驗證彩池種類、官方 pool code、兩關結構、第一關時間錨點、快照完成狀態及開跑前捕捉時間。違反任何一項即拒絕輸出候選檔。

### 第五步：查詢賽前 MAIN 指標 EV

```bash
python3 query_complex_pool_ev.py \
  --db hkjc_odds_snapshot_archive.sqlite \
  --mode double_trio \
  --candidate-file double_trio_t15_bound.json \
  --output-json double_trio_t15_ev.json \
  --output-csv double_trio_t15_ev.csv
```

若同一候選 key 沒有同一 T-15／T-5 快照內的 `MAIN` 特定報價，該候選會被排除並明示理由；不可把未顯示報價當作零、不可推算未顯示價格，也不可使用正式派彩代替。

## 4. 批量回測

一鍵同時跑 T-15 與 T-5：

```bash
./run_double_trio_batch_backtest.sh \
  --db hkjc_odds_snapshot_archive.sqlite \
  --candidate-root archive/model_pool_candidates/double_trio \
  --output-root v102_double_trio_batch_backtest \
  --max-capture-delta-seconds 180
```

| 輸出檔案 | 內容 |
|---|---|
| `double_trio_batch_summary.json` | 可定價覆蓋率、指標 EV、MAIN 命中、安慰獎命中、實現 ROI、最大回撤及排除原因。 |
| `double_trio_batch_details.csv` | 每張組合的模型時間、快照時間、兩關 key、報價、結果、MAIN／安慰獎結算及累積回撤。 |
| `double_trio_batch_exclusions.csv` | 時間倒置、快照品質、組合不符、缺失報價或缺失結果等所有排除紀錄。 |

實現 ROI 只在候選已固定後，才讀取兩關的 `official_pool_result_members` 與 `official_pool_payouts`。若有 MAIN 官方派彩，第一關命中的候選不可另行取安慰獎；只有沒有 MAIN 派彩而官方有相符 `CONSOLATION` 派彩時才可計入安慰獎收益。

## 5. 既有 archive 的 schema 遷移

已有複合彩池 archive 需要先遷移其 `pool_type` CHECK 約束：

```bash
python3 migrate_complex_pool_double_trio.py --db hkjc_odds_snapshot_archive.sqlite
```

遷移器會暫存並重建依賴 view，保留既有事件列及外鍵，並可安全重複執行。新建 archive 則直接套用更新後的 `schema_prerace_complex_pool_snapshots.sql`。

## 6. 資料與回測限制

| 限制 | 正確處理 |
|---|---|
| 尚未有真實的歷史 T-15／T-5 孖T組合快照。 | 報告 `N/A`／零合資格覆蓋；不可用最終派彩回填。 |
| 官方頁只顯示總池額或多寶。 | 可保存市場摘要，但不計算組合 EV。 |
| 局部顯示熱門組合。 | 只報可定價候選覆蓋率，不外推所有模型候選。 |
| T-15／T-5 時點不同。 | 分開建立、分開回測、分開報告，不互相補值。 |
| 少於 15 張已結算候選。 | 明確標記為探索性；未達 30 張完整同質樣本前不調整模型機率。 |

## 7. 驗證範圍

隔離測試已驗證：兩關無順序 canonical key、MAIN 命中、沒有 MAIN 時的官方安慰獎、一般落敗、模型時間晚於快照時的拒絕、既有 SQLite archive 無損遷移，以及 V10.2 兩關 prediction → 未綁定候選 → 快照綁定 → MAIN EV 查詢的端到端鏈路。所有測試價格、機率及回報均為合成資料，沒有任何真實歷史收益意義。

## References

[1] [香港賽馬會：平分彩金本地彩池](https://special.hkjc.com/e-win/zh-HK/betting-info/racing/beginners-guide/local-pools/)

[2] [香港賽馬會：注數表－平分彩金彩池](https://special.hkjc.com/e-win/zh-HK/betting-info/racing/beginners-guide/chance-table/)
