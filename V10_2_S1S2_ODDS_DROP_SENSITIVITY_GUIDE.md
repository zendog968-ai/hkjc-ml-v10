# V10.2 S1/S2 Odds-Drop Sensitivity Guide

> **目的：** 量化 `--odds-drop-log-weight` 對海外 S1/S2 賽前 Win 機率、場內排序、Win EV，以及在有完整官方結算資料時的 Brier score、固定單位 ROI 與最大回撤的影響。這是**反事實特徵敏感度研究**，不是已證實的收益優化程序。

## 1. 資料門檻

分析器只讀取已保存的 `predict_s1s2.py` JSON。每份輸入必須有 `input_status=complete` 或 `degraded`、`odds_snapshot_status=complete`，且任何標示為落飛的馬匹都必須保留同一匹馬的 T-15／T-5 捕捉時間及 `odds_drop_ratio ≤ -0.20`。無此條件的檔案會列入 `sensitivity_exclusions.csv`。

真實歷史 ROI、命中率、最大回撤與 Brier score還需要官方結果 CSV：

| 欄位 | 含義 | 使用時點 |
|---|---|---|
| `race_key` | 與預測輸出一致的 race identity，例如 `overseas_race_id:123`。 | 事後結算連接。 |
| `horse_no` | 官方馬號。 | 事後結算連接。 |
| `finish_pos` | 官方名次；頭馬為 1。 | 只用於 Brier、命中、ROI 與回撤，絕不回寫賽前機率。 |

若某場所有馬匹未具官方 `finish_pos`，該場的 Brier score 保持 `null`。若首選沒有可驗證 final outcome 或賽前 Win 價格，ROI、命中率與回撤保持 `null`。這樣可防止最終賠率、派彩或缺漏結果冒充賽前可交易資料。

## 2. 權重與反事實重算

對原始預測機率 `p_i`，分析器依下式重算每匹馬的相對強度，再在場內正規化：

\[
\tilde p_i(w)=\frac{\exp(\log(p_i)+(w-w_0)\cdot I_i)}{\sum_j\exp(\log(p_j)+(w-w_0)\cdot I_j)}
\]

其中 `w_0` 是原檔案的 `odds_drop_weight`，`I_i=1` 僅適用於已通過完整落飛閘門的馬匹。這只改變該特徵的 log-strength，保留同場其他先驗與特徵不變。Win EV 依賽前記錄的 displayed odds 計算：

\[
\operatorname{EV}_i(w)=\tilde p_i(w)\times O_i-1
\]

預設網格為 **0、0.05、0.10、0.20、0.30**。它涵蓋禁用訊號、低敏感度、當前研究預設及較高敏感度。不要以同一全樣本中最高 ROI 直接選擇權重；應以前期季度選擇候選，再固定測試下一期。

## 3. 執行方法

### 3.1 使用隔離 fixture 驗證程式流程

```bash
cd /home/ubuntu/hkjc_v10_database

python3 analyze_s1s2_odds_drop_sensitivity.py \
  --prediction-glob 's1s2_feature_enrichment_fixture/prediction.json' \
  --weights 0,0.05,0.10,0.20,0.30 \
  --fixture-mode \
  --output-dir s1s2_odds_drop_sensitivity_fixture
```

`--fixture-mode` 會在圖表及 JSON 標記為隔離資料。它只適合驗證權重對機率及排序的數學反應，**不能**用來解讀 ROI、Brier score 或最優設定。

### 3.2 使用真實 archive

```bash
python3 analyze_s1s2_odds_drop_sensitivity.py \
  --prediction-glob 'archive/overseas_s1s2_predictions/**/*.json' \
  --results-csv archive/overseas_s1s2_results.csv \
  --weights 0,0.05,0.10,0.20,0.30 \
  --output-dir v102_s1s2_odds_drop_sensitivity
```

## 4. 讀取輸出

| 檔案 | 用途 |
|---|---|
| `weight_sensitivity_summary.csv` | 各權重的首選率、排序變動、平均首選 EV、Brier、ROI 與最大回撤。 |
| `top_pick_sensitivity_details.csv` | 每場每個權重的首選、機率、EV、結算名次及單位損益。 |
| `sensitivity_exclusions.csv` | 不完整快照、無效落飛合約或輸入格式問題。 |
| `01_top_probability_sensitivity.png` | 首選平均機率對權重的影響。 |
| `02_roi_sensitivity.png` | 只有已結算官方樣本才生成的固定單位 ROI 圖。 |
| `sensitivity_summary.json` | 可機器讀取的覆蓋率、狀態、權重與警告。 |

判讀時應同時看四件事：第一，權重提高是否只讓極少數賽事的首選翻轉；第二，Brier score 是否在未見樣本改善而非惡化；第三，ROI 是否在移除最大正回報後仍有穩定性；第四，最大回撤是否與資本限制相容。單一賽事、單一季度或少於 15 場已結算首選的結果均屬探索性。

## 5. 目前 V10.2 資料狀態

目前專案只有一份隔離 S1/S2 feature fixture，沒有可用的正式海外 T-15／T-5 歷史預測與官方結果配對。因此正式 archive 測試會輸出 `N/A_no_eligible_complete_t15_t5_predictions`；這是正確的資料品質結果，而不是 0% 表現。日後應先以持久主機保存至少 100 場完整快照與預測，並以滾動季度選取與測試權重，才可討論 0.20 是否比 0、0.05、0.10 或 0.30 更穩健。
