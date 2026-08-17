# V10.2 Brier Score 與高爆冷提示：程式碼實作審核

**審核日期：2026-08-17**
**範圍：** `post_race_audit.py`、`race_risk_guidance.py`、`predict.py`、`predict_s1s2.py`、`filter_high_probability.py`

## 1. 實作架構結論

`post_race_audit.py` **負責賽後評分與結算**，其中包含場內多馬 Brier Score、Top 1／Top 3、策略籃子結算及閘前落飛標記的賽後計數。它**不會自行產生高爆冷提示**。

高爆冷與高 EV 冷門是 `race_risk_guidance.py` 的**賽前共用提示邏輯**；由 `predict.py`、`predict_s1s2.py` 及 `filter_high_probability.py` 載入並輸出至預測 JSON／Markdown／WhatsApp 預覽。這個職責分離是正確的，因為高爆冷警告必須在賽前產生，不能依賴賽後名次。

| 職責 | 實作檔案 | 主要函式 | 輸出 |
|---|---|---|---|
| 賽後模型誤差 | `post_race_audit.py` | `field_brier_score()` | `brier_score` 寫入 `post_race_audits`。 |
| 賽後命中／ROI | `post_race_audit.py` | `audit_predictions()` | Top 1、Top 3、策略 ROI、落飛結果。 |
| 場內高爆冷判斷 | `race_risk_guidance.py` | `build_race_guidance()` | `dispersion_warning`、建議文字。 |
| 高賠率價值候選 | `race_risk_guidance.py` | `candidate_value_bomb()` | `value_bomb_candidates`。 |
| 本地／海外接入 | `predict.py`、`predict_s1s2.py`、`filter_high_probability.py` | `build_race_guidance(...)` | JSON、Markdown、預覽訊息。 |

## 2. Brier Score 的具體程式碼

`post_race_audit.py` 第 115–119 行：

```python
def field_brier_score(ordered, winner_no):
    probabilities = [num(row, "predicted_win_probability", "win_probability")
                     for row in ordered]
    if winner_no is None or not probabilities or any(value is None for value in probabilities):
        return None
    return sum(
        (probability - (1.0 if row.get("horse_no") == winner_no else 0.0)) ** 2
        for row, probability in zip(ordered, probabilities)
    )
```

它使用的是**單場多分類 Brier 總和**：

\[
B_r=\sum_{i=1}^{N}(p_i-y_i)^2
\]

其中 `p_i` 取 `predicted_win_probability`；若沒有該欄則取 `win_probability`。官方頭馬的 `y_i=1`，其餘馬匹為 0。第 126 行先依預測勝率降序排序，然後第 139 行調用 `field_brier_score(ordered, winner_no)`，把結果放入 audit dict；主程式第 226–229 行再寫入 `post_race_audits.brier_score`。

這個尺度與 V10.2 訓練檔 `train_lightgbm.py` 的 `race_level_metrics()` 一致：後者亦對同一場的正規化機率向量以 `np.sum((p-y)**2)` 計分，最後跨場平均。因此，單場覆盤的 `brier_score` 可以在**相同 field-size 分佈與相同口徑**下與 `mean_race_brier_score` 比較。

### 2.1 正確的保護

| 程式保護 | 行號 | 行為 |
|---|---:|---|
| 沒有頭馬 | 117–118 | 回傳 `None`。 |
| 沒有預測列 | 117–118 | 回傳 `None`。 |
| 任何機率欄位不是 Python 數字 | 116–118 | 回傳 `None`。 |
| 沒有賽前預測或官方頭馬 | 123–125 | 覆盤維持 N/A／`archived_only`，不偽造命中率。 |

### 2.2 現有高優先級缺口

| 缺口 | 具體原因 | 風險 | 建議 |
|---|---|---|---|
| 頭馬不在預測 field | 函式只檢查 `winner_no is not None`，沒有驗證 `winner_no in predicted_horse_nos`。 | 所有 `y_i=0`，仍產生貌似正常的分數。 | 回傳 N/A，理由 `winner_missing_from_prerace_field`。 |
| 馬號型別不一致 | 比較為 `row.get("horse_no") == winner_no`。 | `"2"` 與 `2` 失配，真頭馬被視作落敗。 | 讀入時嚴格轉為整數；失敗則 N/A。 |
| 未驗證機率向量 | 未檢查有限值、非負、每項 ≤1、或合計等於 1。 | 非正規化輸入的分數不可與模型回測比較。 | 只在 `abs(sum(p)-1) <= 1e-6` 才計分。 |
| 未驗證完整賽事 field | 未比較 official starters、withdrawals 與 prediction field。 | 賽後 field 差異可能扭曲評分。 | 記錄 `field_match_status`；不賽後重正規化原預測。 |
| 未驗證賽前時間 | 自動海外載入取 `MAX(generated_at_utc)`。 | 賽後重跑預測可能造成前視偏誤。 | 僅取早於 `scheduled_start_utc` 的最新批次。 |
| 只有數值、沒有原因碼 | DB 只寫入 `brier_score`。 | 批量報告難分辨 N/A 原因。 | 加入 `brier_status`、`brier_field_size`、`brier_uniform_baseline`。 |

## 3. 高爆冷提示的具體程式碼

`race_risk_guidance.py` 第 63–85 行是唯一的場內高爆冷判斷來源：

```python
valid = [row for row in predictions
         if first_number(row, "predicted_win_probability", "win_probability") is not None]
field_size = len(valid)
top_probability = max(..., default=None)
high_dispersion = bool(
    field_size >= 14
    and top_probability is not None
    and top_probability < 0.20
)
```

高爆冷標記只會在**有效勝率列不少於 14 匹**且**首選勝率嚴格低於 20%**時成立。成立時輸出：

```text
⚠️【高爆冷風險亂局】本場 14 匹或以上且首選勝率低於 20%；不適合作單膽。
```

這完全符合目前的 V10.2 設定：不是預測哪一匹一定爆冷，而是根據整場機率分散程度禁止把首選視為單一穩膽。`top_probability == 0.20` 不會觸發，因為程式使用嚴格小於 `< 0.20`。

### 3.1 高 EV 冷門的具體程式碼

`candidate_value_bomb()` 第 28–60 行為單馬候選邏輯：

```python
win_odds = first_number(row, "win_odds", "market_odds")
if win_odds is None or win_odds <= 15.0:
    return None

light_weight = weight is not None and weight <= 129.0
inside_draw = draw is not None and 1 <= draw <= 4
if not (light_weight or inside_draw):
    return None

ev = first_number(row, "win_ev", "ev_per_unit", "win_ev_per_unit")
label = "💣 高 EV 冷門" if ev is not None and ev > 0 else "💣 高賠率冷門候選（EV 未確認）"
```

| 條件 | 實作門檻 | 結果 |
|---|---:|---|
| 獨贏賠率 | `> 15.0` | `15.0` 不符合；`15.1` 才可繼續。 |
| 實體／位置優勢 | 負磅 `≤129` **或** 檔位 `1–4` | 至少一項已公開資料存在。 |
| 模型 EV | `>0` | 才可使用「高 EV」；否則僅稱候選。 |
| 缺失賠率、負磅、檔位或 EV | 不猜測 | 不符合或降級為 EV 未確認。 |

`finite()` 第 12–17 行會把可轉成浮點的值取出，並以 `math.isfinite()` 排除 `NaN` 與 `inf`。因此高爆冷／價值候選模組在數值欄位處理上比 Brier 計算更嚴格。

## 4. 實際接入與賽後關係

程式搜尋結果顯示 `build_race_guidance()` 只由 `predict.py`、`predict_s1s2.py` 與 `filter_high_probability.py` 使用；`post_race_audit.py` 沒有 import 它。換言之：

1. 賽前預測器產生 `race_guidance`，包括 `dispersion_warning`、`bet_recommendation` 與 `value_bomb_candidates`。
2. 篩選報告把高爆冷警告與價值候選展示給使用者。
3. 賽後覆盤目前只追蹤 `odds_drop_flag`／`pre_gate_money_drop`，不會判斷當日是否曾發出高爆冷警告，也不會衡量價值冷門候選的實現回報。

因此，「高爆冷提示」目前是**賽前風險溝通功能**，不是可在 `post_race_audit.py` 直接回測的已存檔策略。若要賽後衡量它是否有用，需在賽前把 `race_guidance` 以不變的 JSON 存入獨立表或預測 batch metadata，並在覆盤時讀回原始訊號；不可賽後依最終賽果重建或修改提示。

## 5. 建議的安全修正次序

### 優先級 1：讓 Brier 只在完整可稽核場次計分

為 `field_brier_score()` 建立回傳物件，例如：

```python
{
  "score": None,
  "status": "winner_missing_from_prerace_field",
  "field_size": 14,
  "uniform_baseline": 0.9285714286
}
```

通過馬號、field set、機率守恆與賽前時間檢查後，才寫入真正分數。批量報告同時輸出有效場數、排除原因及等機率基準。

### 優先級 2：保存賽前風險提示以供賽後審計

在預測 batch 寫入：`dispersion_warning`、`top1_win_probability`、`value_bomb_candidates_json`、預測生成時間及 field 馬號集合。覆盤只讀取賽前已存值，計算高爆冷 warning 場的 Top 1／Top 3／Brier 分層表現，避免結果洩漏。

### 優先級 3：完善高爆冷提示的 field 驗證

`build_race_guidance()` 現時以「具有效勝率的列數」作 field size。這合理地排除了缺失模型值，但可再加入：撤回馬排除、重複馬號拒絕、機率向量合計檢查、以及 `input_status='complete'` 閘門。這可避免不完整 JSON 因剛好有 14 列被誤判為亂局。

## 最終判定

高爆冷提示的門檻與資料缺失降級行為清晰、保守，並且確實在本地與 S1/S2 預測輸出層接入；它不會自動投注，也不把高賠率或內檔本身誤稱為正 EV。Brier Score 的主公式則與 V10.2 訓練口徑一致。

不過，Brier 的場次完整性驗證及高爆冷訊號的賽後可追溯性仍未完成。在加上 field／時間閘門及賽前提示保存前，Brier 應只視為單場研究性診斷，而高爆冷警告應只視為賽前風險溝通，不能單獨被宣稱為歷史上已驗證的投注優勢。
