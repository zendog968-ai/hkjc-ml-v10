# V10.2 `post_race_audit.py` 與 Brier Score 程式碼審核

**審核日期：2026-08-17**

## 結論摘要

`post_race_audit.py` 的 `field_brier_score()` 對於「**場內已正規化、頭馬確實存在於預測名單、馬號資料型別一致**」的正常輸入，計算的是 V10.2 已在訓練回測採用的**單場多分類 Brier 總和**：

\[
B_r=\sum_{i=1}^{N}(p_i-y_i)^2
\]

其中，\(p_i\) 是馬匹 \(i\) 的場內勝出率，\(y_i\) 為頭馬的 one-hot 標記。此尺度與 `train_lightgbm.py` 的 `race_level_metrics()` 完全一致；後者也是每場先正規化，再對每匹馬的平方誤差求和，最後跨場取平均。因此，現行 `0.78` fixture 結果的算術與 V10.2 主要的 `mean_race_brier_score` 口徑均是正確的。

不過，程式目前**沒有驗證評分場的完整性**。若官方頭馬不在預測資料、馬號型別不同、機率未正規化、或預測在賽後才生成，仍可能產生數學上可算但研究上無效的 Brier 值。這是影響歷史評估可信度的高優先級缺口。測試目前證明正常路徑可通過，**不代表**已防止這些邊界條件或可用作模型效能／ROI 證據。

| 審核面向 | 結論 | 優先級 |
|---|---|---|
| 正常案例公式 | 正確，且與訓練階段的場內 Brier 總和一致。 | 通過 |
| fixture 的 `0.78` | 算術正確，但比三匹馬等機率基準 `0.6667` 高 `0.1133`；該 fixture 的模型分配較等機率差。 | 解讀必須修正 |
| Brier 尺度命名 | 應明確稱為 `race_brier_sum` 或 `single_race_multiclass_brier`，避免與逐馬平均 Brier 混淆。 | 中 |
| 頭馬／預測名單匹配 | 未驗證；缺頭馬時可錯算。 | 高 |
| 機率守恆與有限值 | 未驗證 `sum(p)=1`、非負及有限值。 | 高 |
| 時間閘門 | 自動載入最新海外預測時未驗證產生時間早於開跑；有賽後重跑污染風險。 | 高 |
| 測試覆蓋 | 正常成功路徑已測，失敗與拒絕路徑不足。 | 高 |

## 1. 審核的程式控制流程

主程式先讀取官方 archive 的本地或海外賽果，再從明確指定 JSON 或海外 `overseas_prerace_predictions` 表載入賽前預測。若找不到預測，`audit_predictions()` 會維持 `archived_only` 而不製造命中率；這一點符合無資料不補值的設計。

| 程式位置 | 現行行為 | 審核判斷 |
|---|---|---|
| `official_overseas()` 第 66–74 行 | 從 `overseas_starters` 取官方名次、時間、最後 Win／Place odds。 | 正確使用 archive 官方賽果；仍應在評分前確認結果場次為 `completed`。 |
| `official_local()` 第 77–83 行 | 從本地 `starters` 取名次及 `win_odds`。 | 合理，但位置派彩仍未正規化，故位置 ROI 保持 N/A 是正確的。 |
| `load_latest_overseas_prediction()` 第 50–63 行 | 以 `MAX(generated_at_utc)` 取最新的海外預測批次。 | 有時間污染缺口；最新不必然是賽前。 |
| `audit_predictions()` 第 122–139 行 | 排序、計算 Top 1／Top 3、策略命中、Brier、官方最終 Win 研究籃子 ROI。 | 正常資料下合理；Brier 的輸入驗證不足。 |
| 第 226–229 行 | 寫入 `post_race_audits`，含 `brier_score` 與完整 `detail_json`。 | 良好；建議另存 `brier_status`、預測生成時間及 field-match 狀態。 |

## 2. Brier Score 計算的詳細檢視

### 2.1 現行程式

```python
def field_brier_score(ordered, winner_no):
    probabilities = [num(row, "predicted_win_probability", "win_probability")
                     for row in ordered]
    if winner_no is None or not probabilities or any(value is None for value in probabilities):
        return None
    return sum(
        (probability - (1.0 if row["horse_no"] == winner_no else 0.0)) ** 2
        for row, probability in zip(ordered, probabilities)
    )
```

這段程式把各馬的預測勝率與 one-hot 結果相減、平方、加總。它沒有除以馬數，故範圍與馬場大小有關；在機率向量合計為 1 的情況下，最佳值為 0，最差可接近 2。不同出賽馬數的單場分數不可只看絕對值，必須同時看等機率基準或按馬數分層。

`train_lightgbm.py` 第 87–109 行採取相同做法：先對模型輸出按 `race_group` 正規化，之後以 `np.sum((p-y)**2)` 取得每場分數，再報告 `mean_race_brier_score`。所以**賽後覆盤與主要訓練回測的場內口徑是一致的**。

### 2.2 fixture 的 `0.78` 如何得出

隔離測試在第 61–66 行建立三匹馬的預測：#1 的勝率為 0.50、#2（官方頭馬）為 0.30、#3 為 0.20。故：

| 馬號 | 預測 \(p_i\) | 結果 \(y_i\) | 平方誤差 \((p_i-y_i)^2\) |
|---:|---:|---:|---:|
| 1 | 0.50 | 0 | 0.25 |
| 2（頭馬） | 0.30 | 1 | 0.49 |
| 3 | 0.20 | 0 | 0.04 |
| **合計** | **1.00** | **1** | **0.78** |

隔離測試第 80 行以 `abs(audit[7] - 0.78) < 1e-12` 驗證該值，最近一次驗證輸出亦寫入 `brier_score: 0.7799999999999999`。後者只是 IEEE 浮點表示，與 `0.78` 等值，不是數學誤差。

但三匹馬等機率的單場基準是 \(1-1/3=0.6667\)。此 fixture 的 `0.78` 因而比等機率基準高 `0.1133`。所以它只證明程式計算、寫庫與報告渲染正確；**絕不表示這個示例的預測校準較基準好**。

## 3. 已通過的測試內容

最近一次 `verify_overseas_archive_audit_guidance.py` 輸出為 `passed`。它以標示的合成 HTML fixture 驗證海外表格欄位解析、寫入 SQLite、覆盤命中、官方最後獨贏研究性 ROI、Brier 寫入和 Markdown 產出。其測試來源明確聲明為 isolated synthetic fixture，不是歷史回測。

| 已驗證項目 | 證據 | 審核結論 |
|---|---|---|
| 標頭式賽果解析 | 3 匹馬及 2 個派彩項目成功解析。 | 通過。 |
| 官方欄位寫入 | 頭馬的馬名、騎師、練馬師、負磅、檔位、名次、時間、Win／Place odds 均相符。 | 通過。 |
| 完整賽果狀態 | 三匹有名次 fixture 寫為 `completed`。 | 正常成功案例通過。 |
| Top 1／Top 3 | 結果為 `0`／`1`，與 fixture 相符。 | 通過。 |
| Win 研究籃子結算 | 合併 stake `4.0`、net return `6.0`、ROI `1.5`。 | 通過；只限獨贏研究籃子。 |
| 場內 Brier | `0.78` 寫入 `post_race_audits` 並出現於報告。 | 通過。 |
| 高爆冷報告 | 高爆冷、正 EV 的價值冷門標籤可渲染。 | 與 Brier 無直接關係，另行通過。 |

## 4. 需修正的高優先級問題

### 4.1 頭馬不在預測名單時會產生錯誤的有效分數

`field_brier_score()` 只檢查 `winner_no is not None`，沒有檢查 `winner_no` 是否存在於 `ordered`。實際邊界審核中，使用兩匹預測馬 #1、#3（各 0.50），而官方頭馬是 #2，函式仍回傳 `0.50`。這代表函式把所有馬都當成落敗馬，沒有 `y=1` 項，該分數不能代表該場 Brier Score。

**必須修正為 N/A：** 頭馬不在預測 field 時，應回傳 `None` 並寫入 `brier_status='winner_missing_from_prerace_field'`。不得自行把其他馬的機率或結果重分配。

### 4.2 馬號資料型別不同會令真實頭馬失配

現行比較為 `row.get("horse_no") == winner_no`。若賽前 JSON 的馬號是字串 `"2"`，而 archive 的頭馬是整數 `2`，結果會全部視為非頭馬。實測三匹馬的機率 0.50／0.30／0.20 在此情況回傳 `0.38`，即 \(0.25+0.09+0.04\)，而不是正確的 `0.78`。

**必須修正為：** 在讀入時以嚴格整數轉換正規化馬號；轉換失敗、重複馬號或預測／官方場次馬號集合不一致時，寫 `N/A` 及原因碼。

### 4.3 機率沒有守恆或包含非有限值時仍會評分

函式沒有檢查全部機率非負、有限、或總和是否在容差內等於 1。實測 0.80／0.80／0.20（總和 1.80）仍回傳 `0.72`。這個值不能與正規化場內 Brier 或等機率基準比較。`num()` 亦接受 `float('nan')` 或正負無限值，可能令審計值無法可靠比較。

**必須修正為：** 僅接受有限且介乎 `[0,1]` 的值，並要求 `abs(sum(p)-1) <= 1e-6`。不符合時應列為 `not_scored_probability_vector_invalid`，保留原始資料作稽核但不寫可比較分數。

### 4.4 最新預測不等於賽前預測

`load_latest_overseas_prediction()` 僅選取 `MAX(generated_at_utc)`，沒有與 `scheduled_start_utc` 比較。若賽後才手動重跑 `predict_s1s2.py`，自動覆盤可能採用該賽後批次，造成嚴重的前視偏誤。明確傳入的 `--prediction-json` 同樣沒有強制檢查生成時間。

**必須修正為：** 保存並核對預測產生時間、計劃開跑時間、同場的馬號集合及 snapshot provenance。自動載入時只取 `generated_at_utc < scheduled_start_utc` 的最新完整批次；若排位／撤回導致 field 不一致，保留 `N/A`，不可在賽後重正規化舊預測。

## 5. 測試缺口

測試輸出中的 `incomplete_results_not_silently_completed: true` 是成果宣告，但目前 `verify_overseas_archive_audit_guidance.py` 並沒有建立一個少於兩個正式名次列的 fixture，然後斷言其狀態為 `partial`。它只驗證一個三匹完整 fixture 的 `race_status == 'completed'`。因此，該名稱所表達的拒絕路徑尚未真正覆蓋。

下列案例應新增為固定契約測試，並在未通過時阻止將任何 Brier 或 ROI 描述為可比較指標。

| 新增案例 | 預期結果 |
|---|---|
| 頭馬不在預測名單 | `brier_score=None`，原因為 `winner_missing_from_prerace_field`。 |
| 預測馬號為字串、官方馬號為整數 | 經正規化後正確得分；無法轉換時 N/A。 |
| 機率合計不為 1 | N/A，原因為 `probability_vector_invalid`。 |
| 有 `NaN`／`inf`／負機率 | N/A，且不寫入正常數值欄。 |
| 有撤回或實際 field 不同 | N/A，原因為 `prerace_field_mismatch`，不可賽後重正規化。 |
| 最新預測為賽後生成 | 不得被自動載入；應挑選最後一批賽前預測。 |
| 不完整官方賽果 | `race_status='partial'`；不產生 Brier、ROI 或覆盤報告。 |

## 6. 建議的安全修正方向

建議把評分函式改為接收官方已結算參戰馬號集合、勝馬號及預測生成時間的驗證結果，而不只接收排序後的預測列。函式應先建立 `brier_status`，通過所有完整性條件才輸出分數。建議欄位為：

```text
brier_metric = race_brier_sum
brier_score = <float or null>
brier_status = scored | winner_missing_from_prerace_field |
               probability_vector_invalid | prerace_field_mismatch |
               official_result_incomplete
brier_field_size = <integer or null>
brier_uniform_baseline = 1 - 1 / field_size
prediction_generated_at_utc = <timestamp>
```

同時，批量分析應以 `AVG(brier_score)` 報告同一 `brier_metric` 的平均場內分數，並另外報告平均等機率基準、有效場數、排除場數及每種 `brier_status` 的數量。不可將單場 Brier、逐馬 `brier_score_loss`、不同 field size 的未分層分數，或賽後生成預測混合成同一成效結論。

## 最終判定

目前程式的**正常路徑公式與 V10.2 訓練的場內 Brier 口徑一致**，fixture 的 `0.78` 寫入與顯示亦正確。但它仍是「可計算」而非「已完全可稽核」的版本。完成頭馬匹配、馬號正規化、機率守恆、撤回／field-mismatch、預測時間閘門及相應拒絕路徑測試前，不應把 `post_race_audits.brier_score` 當成完整的海外模型校準或長期投資表現證據。
