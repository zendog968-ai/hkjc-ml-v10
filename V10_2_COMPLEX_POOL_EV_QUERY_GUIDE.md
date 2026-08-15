# V10.2 複合彩池指標 EV 查詢：六寶獎與三重彩

**作者：** Manus AI
**程式：** `query_complex_pool_ev.py`
**用途：** 對已固定的模型候選組合，僅使用同一 T-15／T-5 快照中、同一組合與同一派彩層的賽前顯示／估計報價，計算**指標預期值（indicator EV）**。它不讀取 `official_pool_payouts`，因此不會以賽後派彩偽裝成賽前價格。

> 三重彩需要依次選中第一、第二及第三名；六環彩的六寶獎則要求指定六關全部命中第一名。這兩種都是聯合事件，模型輸入的機率必須是整張組合票的聯合命中機率，而不是各馬的邊際勝率相加或平均。[1]

## 1. EV 的定義

假設模型對某一張組合票的聯合命中機率為 `p`，該票的賽前顯示／估計派彩為 `quote_value`，其公布投注單位為 `quote_unit`。如果報價已連本金，則：

```text
每 1 元注額的預期回報倍數 = quote_value / quote_unit
指標 EV（每 1 元） = p × 預期回報倍數 − 1
預期淨收益（指定注額 s） = s × 指標 EV
```

若資料源的報價**不連本金**，查詢器會先加回 1 倍本金再套用同一公式。此差異由 `quote_is_return_inclusive` 控制，不能由程式猜測。

| 查詢模式 | `pool_type` | 需要的 `quoted_payout_tier` | 模型輸入機率 |
|---|---|---|---|
| `trifecta` | `TRIFECTA_ORDERED` | `MAIN` | 確切 1-2-3 名次順序的聯合機率。 |
| `six_win_bonus` | `SIX_UP` | `SIX_WIN_BONUS` | 六關均為第一名的聯合機率。 |

香港賽馬會把六環彩的普通勝出組合和六寶獎列為不同派彩層，因此兩者不能共用同一報價或回報計算。[1]

## 2. Schema 前置條件

將 `schema_prerace_odds_snapshots.sql` 及 `schema_prerace_complex_pool_snapshots.sql` 依序套用到獨立 SQLite archive。複合彩池報價表已包含以下 EV 所需欄位。

| 欄位 | 作用 | EV 閘門 |
|---|---|---|
| `selection_key` | 以關次、位置和馬號構成的 canonical key。 | 必須與模型候選票完全一致。 |
| `quoted_payout_tier` | `MAIN`、`SIX_WIN_BONUS` 等報價層。 | 三重彩限定 `MAIN`；六寶獎限定 `SIX_WIN_BONUS`。 |
| `quote_kind` | `ESTIMATED_DIVIDEND`、`DISPLAYED_ODDS` 等。 | 只接受前兩種特定組合價格。 |
| `quote_value`、`quote_unit` | 公布派彩／賠率及相應每注單位。 | 缺一不可。 |
| `quote_is_return_inclusive` | 報價是否已連本金。 | 缺失即拒絕，避免錯算倍率。 |
| `captured_at_utc`、`scheduled_anchor_start_utc` | 真實捕捉與錨點關開跑時間。 | 快照必須在錨點關開跑前。 |
| `model_generated_at_utc` | 模型候選票固定時間。 | 不得晚於快照。 |

## 3. 模型候選 JSON

三重彩範例：

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

六寶獎範例：

```json
{
  "pool_snapshot_id": 202,
  "model_generated_at_utc": "2026-09-06T10:44:00+00:00",
  "candidates": [
    {
      "selection_key": "L1:P1=2|L2:P1=7|L3:P1=4|L4:P1=8|L5:P1=1|L6:P1=3",
      "predicted_hit_probability": 0.000002,
      "stake": 2.0
    }
  ]
}
```

`selection_key` 必須與 `pre_race_pool_selection_members` 的實際關次、位置與馬號重新生成後完全相同。三重彩不可以把 `2-7-4` 與 `7-2-4` 視為同一票；六寶獎也不可刪除任何 `leg_no`。

## 4. 查詢命令

```bash
cd /home/ubuntu/hkjc_v10_database

# 三重彩 MAIN 派彩層
python3 query_complex_pool_ev.py \
  --db hkjc_odds_snapshot_archive.sqlite \
  --mode trifecta \
  --candidate-file trifecta_candidates.json \
  --output-json trifecta_ev.json \
  --output-csv trifecta_ev.csv

# 六環彩六寶獎派彩層
python3 query_complex_pool_ev.py \
  --db hkjc_odds_snapshot_archive.sqlite \
  --mode six_win_bonus \
  --candidate-file six_win_bonus_candidates.json \
  --output-json six_win_bonus_ev.json \
  --output-csv six_win_bonus_ev.csv
```

JSON 輸出包含快照身分、時間閘門、候選數、具價候選覆蓋率、總注額、組合層級指標 EV及排除原因；CSV 則可供審計和後續彙總。

## 5. 已驗證的隔離示例

以下數值只來自程式隔離測試 fixture，**不是香港賽馬會真實賠率、模型預測或投注建議**。

| 模式 | 模型聯合機率 | 賽前估計派彩／單位 | 每元預期回報倍數 | 指標 EV／每元 | 指定注額預期淨收益 |
|---|---:|---:|---:|---:|---:|
| 三重彩 | 1.50% | $850／$10 | 85.0 | 27.50% | $2.75（$10 注額） |
| 六寶獎 | 0.0002% | $2,000,000／$2 | 1,000,000.0 | 100.00% | $2.00（$2 注額） |

程式亦驗證了模型在快照後才生成的情況：當候選生成時間晚於 T-15 捕捉時間，所有候選會被標示 `eligible=false`，且總指標 EV 為 `null`。這個閘門防止用已看到的市場價格／賽後資訊回頭選票。

## 6. 必經閘門及解讀限制

| 情況 | 查詢器行為 | 正確解讀 |
|---|---|---|
| 快照屬 `market_summary_only` | 拒絕計算。 | 總池額不足以代表某一組合的價格。 |
| 候選組合沒有特定報價 | 個別候選排除。 | 報告可定價覆蓋率；不可把它當作零 EV。 |
| 六環彩只有 `MAIN` 報價 | 六寶獎查詢排除。 | `MAIN` 不能取代 `SIX_WIN_BONUS`。 |
| 模型在快照後生成 | 整批拒絕。 | 時間順序不成立，不能聲稱賽前 EV。 |
| 僅有賽後正式派彩 | 不會被查詢器讀取。 | 可用於已實現 ROI，不能倒灌為賽前 EV。 |
| `partial` 快照 | 可對具備特定報價的候選計算。 | 必須同列較低的報價覆蓋率，不可外推整體市場。 |

平分彩金的最終派彩會在停止受注前隨資金變動，故這裡的 EV 只是一個基於**當時顯示／估計組合價格**的指標，而不是保證回報。要進行事後實現 ROI 回測，必須另行以已固定的候選票及 `official_pool_payouts` 結果層計算，並分開報告。[1]

## 7. 新增／更新檔案

| 檔案 | 用途 |
|---|---|
| `query_complex_pool_ev.py` | 三重彩與六寶獎指標 EV 查詢器。 |
| `schema_prerace_complex_pool_snapshots.sql` | 新增報價派彩層、報價單位與連本金欄位。 |
| `create_complex_pool_ev_fixture.py` | 隔離測試 fixture 建立器；不可用於正式回測。 |
| `fixture_*_candidates.json` | 測試候選資料。 |
| `V10_2_COMPLEX_POOL_EV_QUERY_GUIDE.md` | 本使用說明。 |

## References

[1] [香港賽馬會：平分彩金本地彩池](https://special.hkjc.com/e-win/zh-HK/betting-info/racing/beginners-guide/local-pools/)
