# V10.2 三重彩批量回測：T-15／T-5 指令與 ROI 稽核

**作者：** Manus AI
**主要程式：** `backtest_complex_pool_trifecta.py`
**一鍵命令：** `run_trifecta_batch_backtest.sh`
**目的：** 批次評估歷史三重彩模型候選，將**賽前指標 EV**與**賽後實現 ROI**嚴格分開，並逐一保存覆蓋率、命中率、回撤及排除原因。

三重彩要求依序命中一場賽事的第一、第二及第三名，因此每張候選票必須使用完整且有順序的 `selection_key`。香港賽馬會把三重彩列為平分彩金本地彩池；賽前顯示／估計派彩會在關閉前變動，故指標 EV 不等於保證收益。[1]

## 1. 一鍵批量回測指令

以下命令會**分開**跑 T-15 及 T-5，輸出兩個互不混合的回測目錄：

```bash
cd /home/ubuntu/hkjc_v10_database

./run_trifecta_batch_backtest.sh \
  --db hkjc_odds_snapshot_archive.sqlite \
  --candidate-root archive/model_pool_candidates \
  --output-root v102_trifecta_batch_backtest \
  --max-capture-delta-seconds 180
```

如只需稽核單一時點，加入 `--snapshot-label`：

```bash
./run_trifecta_batch_backtest.sh \
  --db hkjc_odds_snapshot_archive.sqlite \
  --candidate-root archive/model_pool_candidates \
  --output-root v102_trifecta_t15_only \
  --snapshot-label T_MINUS_15 \
  --max-capture-delta-seconds 180
```

`max-capture-delta-seconds` 是預先宣告的時間偏差容忍值；預設為 180 秒。超出此範圍、模型晚於快照、快照在開跑後，或資料缺乏時間錨點的候選都會被排除，而不是補值。

## 2. 候選檔資料契約

`candidate-root` 中每個 JSON 代表一個**在市場快照前已固定**的模型候選批次。最小結構如下：

```json
{
  "pool_snapshot_id": 101,
  "model_generated_at_utc": "2026-09-06T08:14:00+00:00",
  "candidates": [
    {
      "selection_key": "L1:P1=2|L1:P2=7|L1:P3=4",
      "predicted_hit_probability": 0.015,
      "stake": 10.0
    }
  ]
}
```

| 欄位 | 意義 | 不合規時的處理 |
|---|---|---|
| `pool_snapshot_id` | 指向一個正式 T-15 或 T-5 三重彩快照。 | 找不到或彩池不符即整批排除。 |
| `model_generated_at_utc` | 模型候選定稿時間，必須有 UTC 偏移。 | 晚於快照即整批排除。 |
| `selection_key` | 精確 1-2-3 次序，例如 `2 → 7 → 4`。 | 與市場組合成員不一致即個別排除。 |
| `predicted_hit_probability` | 整張有順序三重彩票的聯合命中機率。 | 不是單馬勝率，也不可超過 1。 |
| `stake` | 對該候選票固定的歷史測試注額。 | 必須大於 0。 |

## 3. 回測的兩條計算軌道

| 指標 | 讀取資料 | 公式／方法 | 可回答的問題 |
|---|---|---|---|
| 指標 EV | 同一 T-15／T-5 快照內的 `ESTIMATED_DIVIDEND` 或 `DISPLAYED_ODDS`。 | `p × 賽前每元回報倍數 − 1`。 | 當時顯示／估計價格下，模型的理論邊際如何？ |
| 實現 ROI | 已固定候選票、`official_pool_result_members` 與 `official_pool_payouts`。 | `實現淨收益 ÷ 已結算總注額`。 | 歷史賽果下，這些先前固定候選的實際結果如何？ |
| 最大回撤 | 依快照捕捉時間排序的已結算實現淨收益。 | 高水位至其後累積淨值低點的最大差額。 | 歷史損失路徑有多深？ |

賽後派彩只用於實現 ROI；回測器不會用它去反算指標 EV。正式派彩和賽前報價都按各自的 `*_per_unit / *_unit` 與 `*_is_return_inclusive` 正規化，以避免把每 $10 派彩錯作每 $1 或遺漏本金。

## 4. 輸出檔案

一鍵命令完成後，會建立：

```text
v102_trifecta_batch_backtest/
├── T_MINUS_15/
│   ├── trifecta_batch_summary.json
│   ├── trifecta_batch_details.csv
│   └── trifecta_batch_exclusions.csv
└── T_MINUS_5/
    ├── trifecta_batch_summary.json
    ├── trifecta_batch_details.csv
    └── trifecta_batch_exclusions.csv
```

| 輸出 | 必讀欄位／用途 |
|---|---|
| `trifecta_batch_summary.json` | 可定價覆蓋率、指標組合 EV、已結算覆蓋率、命中率、實現 ROI、最大回撤及排除原因計數。 |
| `trifecta_batch_details.csv` | 每張候選的模型時間、快照時間、派彩層、指標 EV、官方勝出組合、命中、實現淨收益與回撤。 |
| `trifecta_batch_exclusions.csv` | 被排除候選的可稽核原因；不可悄悄刪除。 |

## 5. 必經資料閘門

| 閘門 | 條件 | 若不通過 |
|---|---|---|
| 彩池身分 | `pool_type = TRIFECTA_ORDERED`、單一關次、`payout_tier = MAIN`。 | 不計入三重彩指標。 |
| 時間順序 | 模型生成時間 ≤ 快照時間 < 錨點場開跑時間。 | 排除，避免資料洩漏。 |
| T-minus 偏差 | `capture_delta_seconds` 在預設或指定容忍值內。 | 排除。 |
| 市場可見度 | 快照為 `full` 或 `partial`，且該模型組合有自身具價報價。 | 沒有指標 EV；不以總池額補值。 |
| 結果可用度 | 有完整官方 1-2-3 名次；命中時有相符 MAIN 派彩。 | 不計入實現 ROI。 |

## 6. 隔離測試結果

測試 fixture 包含三張合成候選：一張命中、一張落敗及一張模型在快照後才產生而被排除。下表僅驗證程式邏輯，**不代表香港賽馬會實際歷史表現、可交易策略或未來收益**。

| 指標 | 隔離測試結果 |
|---|---:|
| 候選數 | 3 |
| 可定價候選 | 2（66.67%） |
| 已結算候選 | 2 |
| 命中 | 1（50.00%） |
| 組合指標 EV | 1.25%／每元 |
| 實現 ROI | 4,150.00% |
| 最大回撤 | $10.00 |
| 排除原因 | 1 張模型生成時間晚於賽前快照。 |

上述 ROI 很高是因為 fixture 特意使用小樣本及合成的高派彩資料來測試結算公式，沒有任何預測或商業意義。真實回測必須依靠完整保存的 T-15／T-5 官方組合報價、已固定候選檔與官方賽果；目前 V10.2 archive 尚未有可匹配的歷史快照時，回測器會報告零覆蓋或 `N/A`，而不會代入最終賠率。

## 7. 使用建議

先以 T-15、T-5 **各自**累積足夠完整賽日，再比較兩個時間點的可定價覆蓋率與回測表現。任何細分樣本少於 15 張已結算候選時，必須標示為探索性；在未達 30 張同質、完整、時間外樣本前，不應據此調整模型機率或放大注額。

## References

[1] [香港賽馬會：平分彩金本地彩池](https://special.hkjc.com/e-win/zh-HK/betting-info/racing/beginners-guide/local-pools/)
