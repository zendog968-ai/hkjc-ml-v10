# V10.2 深位頭馬與低分離度場次：模型優化方案

**目的：** 將近期錯誤分析的兩項後驗診斷，轉為可在賽前運作、可追溯及可經時間外驗證的 V10.2 改善計畫。

> **兩項診斷的正確定位：** 「真正頭馬模型排名 7+」是賽後才能知道的**漏辨識結果**，絕不能成為賽前特徵；「首二機率差距小於 1 個百分點」則是預測完成後即可觀察的**場內不確定性量度**。因此前者用來定義訓練／驗證目標，後者只可先用於風險揭示，不能事後改寫同一場的模型機率。

## 一、現況與不可直接調參的原因

最近 50 場已保存預測中，真正頭馬排在模型第 7 名或之後的 9 場全部落入高 Brier 組，平均場內 Brier 為 1.0299，較同組均勻基準高 0.1092。首二差距低於 1 個百分點的 20 場，平均 Brier 為 0.8983、Top-1 勝出率為 15.00%。兩者均是有用的診斷線索，但前者只有 9 場，後者同時混合馬場、場地、路程和班次，不能用來直接增加任一現有特徵的權重。

V10.2 目前已具備無洩漏的馬匹／騎師 ELO、近期名次與馬位、末段走勢代理、班磅、裝備、體重、賽道偏差縮減、新馬先驗與 LightGBM＋CatBoost 集成。賽前輸出亦已正規化成場內機率；目前的 `race_risk_guidance.py` 已對「14 匹或以上且首選低於 20%」顯示高爆冷提示，但尚未明確披露首二差距。

## 二、優化原則

| 原則 | 強制規則 |
|---|---|
| 賽前可得性 | 特徵只能使用當場開跑前已存在的官方排位、官方歷史賽果、已存檔試閘／裝備或歷史賽前快照。 |
| 時間切點 | 每一特徵列僅可查詢嚴格早於該列 `race_date/race_no` 的資料；不可用同場賽果、後續賽日或事後人工標註。 |
| 不把結果當訊號 | `winner_rank_7_plus` 只可作評估 target／切片，不能進入 `predict.py` 或訓練表。 |
| 機率守恆 | 任何賽後校準改動必須令每場機率總和為 1，並通過現有 Brier field 完整性檢查。 |
| 漸進採納 | 先加診斷與提示，再進行 feature ablation，最後才考慮替換正式模型；不以本 50 場選參。 |

## 三、優先改善一：低分離度風險層（可立即實作，不改模型）

### 3.1 首二差距與機率熵審計欄位

在 `predict.py` 完成機率正規化與排序後，新增以下**輸出／報告欄位**，但不餵入目前模型：

```text
top1_probability                 = p_(1)
top2_probability                 = p_(2)
top2_gap                         = p_(1) - p_(2)
normalized_entropy               = -Σ p_i log(p_i) / log(field_size)
low_separation_warning           = top2_gap < 0.01
high_entropy_warning             = normalized_entropy >= validation_threshold
ensemble_disagreement_top1       = |p_lgb_norm(top1) - p_cat_norm(top1)|
```

`low_separation_warning` 應與既有 `dispersion_warning` 合併為可稽核的 `race_guidance.uncertainty` 區塊。例如：

```json
{
  "top2_gap": 0.0064,
  "normalized_entropy": 0.93,
  "low_separation_warning": true,
  "label": "⚠️ 首二機率差距不足 1 個百分點：場內分散，不適合作單膽",
  "model_probability_changed": false
}
```

這是**報告層處理**，不調低任何馬匹機率，不把輸出後訊號反饋至同一場模型，也不取代原有的 14 匹／20% 高爆冷條件。它直接處理「首二差距小於 1%」的營運含義：模型在排序上沒有足夠分離度，故應降低對 Top-1 的確定性解讀。

### 3.2 分離度的校準研究（只作離線實驗）

在正式改動模型前，建立一個 `evaluate_v102_race_uncertainty.py` 實驗器，按 `top2_gap`、熵及 LightGBM／CatBoost 分歧分箱，分別報告 Brier、Top-1、Top-3、頭馬排名 7+ 比率和校準誤差。首輪只比較固定分箱：

| 指標 | 固定分箱 |
|---|---|
| 首二差距 | `<1pp`、`1–<4pp`、`4–<8pp`、`≥8pp` |
| 正規化熵 | `<0.75`、`0.75–<0.85`、`0.85–<0.92`、`≥0.92` |
| 集成分歧 | 依早期 validation 段四分位數固定，並原樣套用後續測試段 |

不得以最近 50 場重新尋找最有利的切點。

## 四、優先改善二：深位頭馬的賽前可得候選特徵

頭馬排 7+ 的問題表示目前特徵組合可能未能在某些場次呈現真正頭馬的相對優勢。以下候選均可由官方歷史賽果、已公布排位或既有資料庫取得；每項都需要 availability flag、縮減與賽前時間閘門。

| 候選特徵組 | 具體欄位／算法 | 賽前資料來源與時間閘門 | 風險控制 |
|---|---|---|---|
| **跑法與步速形態** | `early_position_pct_pre`（近 3–5 仗首個已公布 call 的名次／field size）、`pace_gain_pre`、`pace_style_pre`（領放／跟前／中置／後上）、`pace_style_starts_pre`。 | 該馬先前官方 running positions；僅取早於當場的已結算起步。 | 少於 2 個可辨識 call 時中性值＋`pace_history_known_pre=0`；不可把代理稱為實測 sectional。 |
| **場內步速壓力** | `field_front_runner_count_pre`、`field_mean_early_position_pct_pre`、`early_pace_congestion_pre`。 | 同場每匹馬的上述歷史跑法；同場排位表。 | 只作場內相對特徵；未知馬採中性分布並保存 unknown count。 |
| **檔位×跑法×縮減賽道偏差** | `draw_pace_interaction_pre = f(draw_pct, pace_style, track_bias_pre)`、`draw_pace_context_sample_pre`。 | 當場 draw、歷史跑法、既有 `track_bias_pre`。 | 延續 `TRACK_BIAS_PRIOR_RUNNERS=48`、偏差上限 ±0.25；樣本不足時回歸中性。 |
| **休後與回復狀態** | `days_since_last_run_pre`、`layoff_bucket_pre`、`layoff_known_pre`。 | 該馬最後一場早於當場的官方賽日。 | 對日期缺失採中性；不把未來賽果或賽後獸醫消息納入。 |
| **細化條件適應** | `horse_course_going_distance_win_rate_pre`、`horse_course_going_distance_starts_pre`、`horse_context_strength_pre`。 | 同馬此前相同馬場／場地／路程桶的官方賽果。 | Beta 縮減至全馬／全場基準；建議 prior starts ≥ 12，並加 sample 欄。 |
| **模型分歧審計** | `lgb_cat_probability_gap`、race-level mean/max disagreement。 | 同一場既有集成輸出。 | 初期只作報告與校準切片，避免把一個模型的輸出循環用回自身訓練。 |

### 4.1 實作順序

首輪只實作「跑法與場內步速壓力」以及「休後狀態」兩組，因為它們最直接針對深位頭馬可能漏掉的賽事形態，並可由現有 `starters.running_positions`、`races.race_date` 和賽前排位表建立。細化條件適應與檔位×跑法交互留作第二輪，避免一次加入太多彼此相關的稀疏特徵。

每個新欄位須同時加入 `build_elo_features.py`、`predict.py` 的賽前重建、`train_lightgbm.py` 特徵清單、schema migration、availability audit 及 fixture contract test。缺失值不得靜默填成表面上有資訊的數值。

## 五、優先改善三：以場內機率為核心的校準實驗

目前流程以驗證段的逐馬 isotonic 校準，然後在每場作正規化。這能產生有效場內總和，但正規化後不一定仍具原來的逐馬 calibration 形狀。建議保留目前版本作 control，另建候選校準器：

```text
p'_i = p_i^α / Σ_j p_j^α
```

其中 α 只可用 validation 段的**場內 Brier**選取，且在之後的 test fold 固定。`α < 1` 代表將過度尖銳的機率向均勻分布縮減；`α > 1` 代表在有足夠驗證證據時增加分離。首輪僅搜索預先固定網格 `{0.70, 0.80, 0.90, 1.00, 1.10, 1.20, 1.30}`，不可在最終 test 上選 α。

這一實驗可降低極端失誤的場內 Brier，但不保證提升 Top-1。若低分離度是資訊不足而非校準不足，正確處理可能只是揭示不確定性，而不是強行拉開首二差距。

## 六、走步時間外驗證設計

### 6.1 固定 control 與候選組

| 組別 | 內容 |
|---|---|
| C0 | 現行 V10.2 集成、現行特徵、現行 calibration。 |
| C1 | C0 ＋ 跑法／場內步速壓力／休後特徵。 |
| C2 | C1 ＋ 場內 power calibration（預先固定 α 網格、只用 validation 選取）。 |
| C3 | C2 的輸出加上低分離度／熵／模型分歧**報告標籤**；機率不再改動。 |

C3 是營運版候選；它必須與 C2 具有完全相同的機率，藉此驗證風險提示並沒有偷改模型。

### 6.2 時序切分

對每個 fold 採 expanding window：早期賽日訓練、中段賽日 validation、後段連續賽日 test。每個 fold 的 train／validation／test 日期不得重疊；特徵建立、normalizer、模型、calibrator 和集成權重均要在該 fold 內重建。至少做 3 個完整 test fold，並確保合計最少 100 場已評估 test races；不足時只產生 `N/A—樣本未達採納門檻`。

### 6.3 預先登記採納門檻

正式升級模型前，C1 或 C2 必須在**合併時間外 test**及至少 2/3 fold 同時滿足以下全部條件：

| 指標 | 最低採納要求 |
|---|---|
| 整體場內 Brier | 相較 C0 改善至少 0.005，且不出現任一 fold 惡化超過 0.005。 |
| 均勻基準優勢 | `uniform_brier - model_brier` 不得較 C0 降低。 |
| 頭馬排名 7+ 比率 | 相較 C0 降低至少 15% 相對比例；同時該切片至少有 20 場。 |
| 深位頭馬 Brier | 在頭馬 7+ 切片，平均 Brier 不得較 C0 上升；切片少於 20 場則 N/A。 |
| Top-1／Top-3 | Top-1 不得下降超過 1.0 個百分點，Top-3 不得下降超過 1.5 個百分點。 |
| 校準 | 預先固定分箱的平均絕對校準差不惡化；所有場內機率和保持 `1 ± 1e-6`。 |
| 完整性 | 不因排除失敗賽事、缺失賽前資料或賽後可得欄位而人為改善 coverage。 |

若 C1/C2 未達標，保持 C0 模型，僅保留 C3 的不確定性報告層，並繼續收集具時間閘門的資料。這比將單一 50 場窗口的錯誤模式硬編碼進模型更安全。

## 七、交付物與優先級

| 優先級 | 交付物 | 預計變動位置 | 是否改正式機率 |
|---|---|---|---|
| P0 | `top2_gap`、entropy、LGB/Cat 分歧、低分離度提示及每日報告切片。 | `predict.py`、`race_risk_guidance.py`、近期回測／錯誤分析器。 | 否。 |
| P1 | 跑法、場內步速壓力、休後特徵及 availability flags。 | `build_elo_features.py`、`predict.py`、schema、`train_lightgbm.py`、fixtures。 | 僅實驗模型。 |
| P2 | C1/C2 走步訓練、消融與採納 gate。 | 新增 `walk_forward_v102_tail_rank_experiment.py`。 | 僅在 gate 全數通過後。 |
| P3 | 細化條件適應、檔位×跑法交互與獨立賽前快照校準。 | 第二輪 feature bundle。 | 僅在 P2 成功後。 |

## 八、明確不建議的做法

1. 不根據 9 場頭馬 7+ 直接提高任何馬匹、體重、檔位或「冷門」權重。
2. 不把 `winner_model_rank`、賽後 Brier 或賽後名次寫入賽前特徵表。
3. 不用缺乏完整歷史標籤的 T-15/T-5 賠率快照訓練本地模型；繼續將其限定為市場覆核與未來累積資料。
4. 不把 `top2_gap < 1%` 自動轉成反向選馬或投注動作；它只表示模型排序缺乏足夠分離度。
5. 不以單一馬場、單一場地或少於 15 場的切片替換全局 ensemble 權重。
