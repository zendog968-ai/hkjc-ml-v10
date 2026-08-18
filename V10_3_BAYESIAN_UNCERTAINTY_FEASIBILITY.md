# V10.3 貝氏推論不確定性量化：可行性評估與路線圖

**作者：** Manus AI
**範圍：** 本文件評估以貝氏後驗預測分布補充 V10.2 場內勝率研究輸出的可行性。它不構成投注建議、獲利承諾或自動下注規則。

## 結論摘要

> **建議採取「有條件 Go」：** V10.3 應先建立一個**貝氏校準／不確定性覆蓋層**，保留 V10.2 的 LightGBM＋CatBoost 排序集成作為主模型，而不是立即以全貝氏模型取代它。此方案能直接量化參數、條件群組與場內排序的後驗不確定性，同時保持現有特徵工程、官方資料來源與場內機率守恆契約。

V10.2 已有時間序列訓練、isotonic 校準、場內正規化、模型分歧及 P0 `top2_gap` 報告層。它目前缺少的是把「樣本不足、條件轉換、模型成分分歧與首選排序不穩定」合併成**後驗分布**的機制。近期 50 場診斷中的「頭馬排名 7+」應保留為賽後評估切片；「首二差距 <1pp」已正確地作為賽前 P0 報告訊號，兩者都不能被反向寫入同一場模型特徵。

## 一、貝氏推論可補足的內容

| 不確定性類型 | V10.2 現況 | V10.3 貝氏覆蓋層可提供 | 營運用途 |
|---|---|---|---|
| **條件噪音（aleatoric）** | 場內機率、熵與首二差距。 | 每場後驗預測分布、首選保持首位的後驗比例。 | 標示排序是否本質上不易分開。 |
| **知識不足（epistemic）** | 新馬／稀疏樣本採中性先驗；P0 只披露輸出端訊號。 | 群組先驗與部分池化後的後驗寬度。 | 顯示冷門、久休、樣本少的估計不確定性。 |
| **模型結構分歧** | 可輸出 LightGBM／CatBoost 機率差。 | 將成分輸出作為校準覆蓋層的輸入／審計切片。 | 不把分歧誤解為機率優勢；用於檢驗 posterior 是否合理反映不穩定性。 |
| **跨條件資料稀疏** | `track_bias_pre`、條件勝率等已有縮減。 | 馬場／場地／班次／馬匹層級的階層先驗與 partial pooling。 | 避免小樣本條件桶被當成確定訊號。 |

貝氏 Plackett–Luce 模型把多參賽者名次視為同一場內的排序分布，與 V10.2 的場內正規化目標相容。已有運動排名研究以 Bayesian Plackett–Luce 取得參數後驗並進行機率預測，亦有多隊在線排名方法以技能均值與不確定度作為更新狀態。[1] [2] [3]

## 二、候選架構比較

| 方案 | 描述 | 優點 | 主要風險／成本 | 判定 |
|---|---|---|---|---|
| **A．全貝氏階層 Plackett–Luce** | 直接以完整排名與所有 V10.2 特徵擬合層級 posterior。 | 理論一致地處理場內名次、條件群組與後驗抽樣。 | 43+ 特徵、稀疏 horse／trainer 效應與 2,500+ 場次使 MCMC 較重；模型更難除錯。 | **P3 備選，不作首輪。** |
| **B．貝氏序列技能模型** | 把現有 horse ELO 改為 skill mean＋sigma 的遞推模型。 | 對新馬、轉倉、久休與樣本不足的知識不確定性很直觀；可在線更新。 | 多馬賽事、場地與班次交互要額外建模；改動特徵庫核心。 | **P2 候選。** |
| **C．貝氏校準／不確定性覆蓋層** | 將 V10.2 的預測強度當 offset，加入小型階層校準項，抽樣輸出場內 posterior。 | 最小侵入、保留現有主模型、可與 C0 並行、計算量可控。 | 不能取代缺失特徵；若層級過多仍可過擬合。 | **P1 推薦。** |
| **D．共形／bootstrap 基準** | 以時間分割 calibration set 生成非貝氏集合或重抽樣不確定性。 | 可作 V10.3 的方法學對照，避免把任何 interval 都稱為貝氏優勢。 | 不直接提供有意義的階層 posterior；多分類／場內排序實作要小心。 | **必備 benchmark，不是正式主線。** |

## 三、推薦的 V10.3-P1 覆蓋層

### 3.1 訓練模型

對每場 `r` 的每匹馬 `i`，先保留 V10.2 在嚴格賽前時間切點建立的正規化勝率 `p_v102,ri`。令它轉成相對強度 offset：

```text
s_ri = α × log(max(p_v102,ri, 1e-6))
       + βᵀ z_ri
       + u_course,going,class[r]
       + u_data_quality_bucket,ri
```

```text
p_ri(draw) = exp(s_ri(draw)) / Σ_j exp(s_rj(draw))
```

其中 `z_ri` 在第一輪只包括**已於賽前保存且可稽核**的資料可用性／稀疏度欄位，例如 `is_new_horse`、`pace_history_known_pre`（P1 特徵完成後）、`layoff_known_pre`、`track_bias_sample_pre`、裝備歷史可用性與兩個集成 component 的機率差。不要把名次、`winner_model_rank`、賽後 Brier、最終賠率、派彩或結果頁欄位加入。

第一輪 likelihood 只使用官方頭馬作場內 categorical outcome，減低部分名次／退出馬解析不完整的風險。當官方完整名次 coverage 連續達標後，才把全排序 Plackett–Luce likelihood 作為離線比較候選。這保留了 V10.2 目前 field-Brier 的目標定義，亦降低第一輪資料工程範圍。

### 3.2 先驗與部分池化

| 參數群組 | 建議第一輪先驗 | 理由與保護 |
|---|---|---|
| 全局 temperature `α` | `Normal(1, 0.25)`，正值約束。 | 以 V10.2 為中心，不預設強行壓平或拉尖機率。 |
| 特徵係數 `β` | 標準化後 `Normal(0, 0.3)`。 | 使新覆蓋層只作溫和修正。 |
| 馬場×場地×班次群組 `u` | `Normal(0, σ_group)`；`σ_group ~ HalfNormal(0.2)`。 | 稀疏組別自動向零收縮，避免以少量濕地或單一場地改變全局。 |
| 資料可用性群組 | `Normal(0, σ_quality)`；`σ_quality ~ HalfNormal(0.15)`。 | 把未知資料反映為不確定性，不替未知馬創造虛構優勢。 |
| 馬匹隨機效果 | **第一輪不加**；第二輪僅在足夠先前出賽下階層化引入。 | V10.2 已有 ELO；直接疊加大量稀疏 identity effect 會令抽樣和解釋不穩定。 |

### 3.3 預測輸出契約

每場以固定、可配置的 posterior draws 產生場內向量；**每一次 draw 本身**必須總和為 1。個別馬的分位數區間並不要求橫向相加為 1，因為它們是邊際分位數，報告必須明確說明這點。

| 新欄位 | 定義 | 能否改寫 V10.2 正式機率 |
|---|---|---|
| `bayesian_status` | `available`、`unavailable` 或具體降級原因。 | 否。 |
| `posterior_win_mean`／`p05`／`p95` | 場內 posterior draw 的邊際摘要。 | P1 只作平行研究輸出。 |
| `top1_rank_stability` | V10.2 首選在 posterior draws 仍為第一的比例。 | 否；作排序穩定性提示。 |
| `posterior_entropy_mean` | 每個 draw 的場內熵之平均。 | 否。 |
| `posterior_component_disagreement` | 對 LGB／Cat 差異的後驗敏感度審計。 | 否。 |
| `posterior_draws_reference` | 原始 draws 的檔案 hash、cutoff、模型版本及 seed。 | 不適用。 |

對 P1，正式 `predicted_win_probability` 必須繼續是 V10.2 的現行正規化輸出；任何 `posterior_*` 欄位只會強化 P0 不確定性報告。只有通過採納閘門後，才可建立候選 V10.3-B 版本，並將 posterior mean 作為一個**獨立版本**的正式場內機率。

## 四、計算與部署可行性

現有 `requirements.txt` 不含 PyMC、NumPyro、JAX 或 CmdStanPy。首輪研究可選擇 PyMC 的 NUTS 於縮小的歷史切片做 posterior 參考，再用 ADVI／Laplace 或 NumPyro SVI 的近似後驗做月度重訓候選。不可先假定變分近似品質；必須在固定歷史 slice 比較其 posterior summary 與參考抽樣的差異。

| 工作 | 建議方法 | 執行節奏 | 是否允許在賽前 T-15／T-5 執行 |
|---|---|---|---|
| 研究 reference posterior | PyMC NUTS，縮小／固定時間窗。 | 離線、版本升級前。 | 否。 |
| 月度 P1 posterior fit | ADVI、Laplace 或 NumPyro SVI；保存收斂與 seed。 | 每月更新後。 | 否。 |
| 單場 posterior summary | 使用已保存的 fitted approximation 及當場賽前特徵。 | 排位後、預測流程內。 | 可以；不得重新以臨場結果 fit。 |
| T-15／T-5 市場資料 | 只作已驗證的市場覆核；尚無完整標籤 coverage 時不訓練。 | 既有流程。 | 可以。 |

這個安排把最耗時計算移至月度／離線階段，避免影響 T-15／T-5 雙快照與臨場輸出時效。全貝氏階層 Plackett–Luce 可待 P1 取得真實增益後再考慮。

## 五、無未來資料洩漏驗證

### 5.1 時序流程

每個 expanding-window fold 必須依序完成：

1. 僅以早於 fold cutoff 的官方已結算資料建立 ELO、V10.2 特徵與貝氏先驗／posterior。
2. 以其後連續 validation 區段選擇唯一近似方法、temperature 網格及報告閾值；不使用 test。
3. 鎖定所有超參數、先驗尺度與 random seed，對最後連續 test 區段產生一次預測。
4. 對每場保存特徵 cutoff、posterior fit cutoff、component probability、posterior draws reference、機率和與資料 availability flags。

所有模型、scaler、isotonic calibrator、Bayesian approximator、分箱邊界及風險提示閾值都必須在 fold 內重建。不得以 2026 的 posterior 或不確定性切點回填 2025 賽事。

### 5.2 預先登記的採納閘門

至少 3 個完整 test fold、合計至少 150 場已結算 test races，且資料 coverage 不得低於 V10.2 control。若尾部切片（頭馬 V10.2 rank 7+）少於 25 場，相關指標一律 `N/A`，不可聲稱改善。

| 指標 | V10.3-P1 報告層採納要求 | V10.3-B 機率替換額外要求 |
|---|---|---|
| 場內機率守恆 | 每一 posterior draw 與所有輸出場次皆為 `1 ± 1e-6`。 | 相同。 |
| 整體場內 Brier | 不適用於純報告層；只比對 V10.2 機率不變性。 | 相較 C0 改善至少 0.005，且至少 2/3 fold 改善。 |
| 場內 log score | 報告但不選參。 | 不得較 C0 惡化；至少 2/3 fold 不惡化。 |
| Top-1／Top-3 | 低穩定性組必須在未見 test 中呈現較低的經驗 Top-1 或較高 Brier，否則標籤不具辨識力。 | Top-1 不得下降 >1pp，Top-3 不得下降 >1.5pp。 |
| 後驗不確定性校準 | 預先固定穩定性／區間寬度分箱；檢驗其與 test Brier、頭馬 rank 7+ 比率的單調關係。 | 同左，且不得僅靠增寬 intervals 造成表面改善。 |
| 尾部漏辨識 | 只作監測；無足夠 n 不判定。 | rank 7+ 比率相對下降至少 15%，且該切片至少 25 場。 |
| 重現性 | fit cutoff、prior、seed、library version、收斂診斷與原始 posterior reference 全數保存。 | 相同。 |

二元賽果不能以單場「真值是否落在機率 credible interval」作為校準證據；那會誤把結果 0/1 與未知真實條件勝率混為一談。應以未見 test 場的 proper scores、預先固定 bins、posterior rank stability 與後驗寬度對高 Brier／低 Top-1 組的辨識能力進行群組評估。

## 六、實作優先級與交付物

| 階段 | 交付物 | 變動範圍 | 正式輸出影響 |
|---|---|---|---|
| **B0：資料審計** | `audit_v103_bayesian_readiness.py`；檢查每個候選群組的賽前 coverage、稀疏度、cutoff。 | 新增離線稽核。 | 無。 |
| **B1：reference 模型** | 縮小特徵集的 Bayesian calibration-overlay、NUTS 比較報告、後驗收斂記錄。 | 新實驗模組／環境檔。 | 無。 |
| **B2：快速近似** | ADVI／Laplace／SVI 與 reference posterior 的固定 slice 比較。 | 新訓練器。 | 無。 |
| **B3：預測 adapter** | `predict_bayesian_overlay_v103.py`；輸出 `posterior_*`、draw provenance 與 P0 整合。 | 平行 JSON artifact。 | 僅報告。 |
| **B4：走步驗證** | `walk_forward_v103_bayesian_uncertainty.py`、C0／P1／D benchmark 報告。 | 離線回測。 | 無。 |
| **B5：版本決策** | 若所有 gate 通過，發布 V10.3-B；否則保留 V10.2＋P0。 | 受管版本切換。 | 視 gate 決定。 |

## 七、明確不建議的做法

1. 不因最近 50 場、9 個深位頭馬或一個馬場／場地切片直接把 posterior mean 取代 V10.2 機率。
2. 不將最終賠率、派彩、完成時間、賽後名次、`winner_model_rank` 或 post-race Brier 放入任何賽前貝氏特徵。
3. 不在每次 T-15／T-5 快照重新 MCMC fit，也不把未完整標籤的賠率快照當成貝氏訓練資料。
4. 不把寬 posterior interval 當成安全、優勢或自動投注訊號；它只表示模型的估計不確定性。
5. 不只看平均 Brier；必須同時檢查 field probability sum、log score、Top-1／Top-3、資料 coverage、尾部切片與不確定性分箱的時間外行為。

## 結語

V10.3 以貝氏校準／不確定性覆蓋層開始是可行、可審計且與現有 V10.2 兼容的方案。它能把 P0 的首二差距與模型分歧由單一訊號提升為有後驗來源的排序穩定性診斷；但在 150 場以上、至少 3 個時間外 fold 的預先登記測試達標前，V10.2 仍應保留為正式勝率輸出，V10.3 的 posterior 只能作平行研究與不確定性披露。

## References

[1] [Henderson, D. A. & Kirrane, L. J. (2018), *A Comparison of Truncated and Time-Weighted Plackett–Luce Models for Probabilistic Forecasting of Formula One Results*](https://projecteuclid.org/journals/bayesian-analysis/volume-13/issue-2/A-Comparison-of-Truncated-and-Time-Weighted-PlackettLuce-Models-for/10.1214/17-BA1048.short)

[2] [Guiver, J. & Snelson, E. (2009), *Bayesian Inference for Plackett–Luce Ranking Models*](https://dl.acm.org/doi/abs/10.1145/1553374.1553423)

[3] [Weng, R. C. & Lin, C.-J. (2011), *A Bayesian Approximation Method for Online Ranking*](https://www.jmlr.org/papers/volume12/weng11a/weng11a.pdf)
