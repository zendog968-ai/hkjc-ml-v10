# V10.2 S1/S2 Odds-Drop Cross-Validation Guide

> **目的：** 在累積至少 100 場具完整 T-15／T-5 快照、已固定賽前預測和官方結果的海外 S1/S2 賽事後，透過跨季度走步驗證選擇下一期要監察的 `odds-drop log weight`。此程序不保證獲利，也不將同一全樣本的最高 ROI 視為最優設定。

## 1. 一鍵自動指令

當 archive 已存在以下兩個持久檔案後，執行：

```bash
cd /home/ubuntu/hkjc_v10_database
./run_s1s2_odds_drop_cross_validation.sh
```

預設路徑與門檻如下。

| 設定 | 預設值 | 用途 |
|---|---|---|
| 預測檔 glob | `archive/overseas_s1s2_predictions/**/*.json` | 已固定的 S1/S2 預測 JSON。 |
| 結果檔 | `archive/overseas_s1s2_results.csv` | 官方結果；必要欄為 `race_key,horse_no,finish_pos`。 |
| 候選權重 | `0,0.05,0.10,0.20,0.30` | 由低至高的落飛 log-strength 敏感度。 |
| 季度 | 香港賽季季度（9–11、12–2、3–5、6–8 月） | 保持賽季節奏。 |
| 完整總樣本 | 100 場 | 未達時輸出 N/A，不進行選擇。 |
| 每個訓練窗 | 100 場 | 確保選擇權重前已有足夠早期資料。 |
| 每個測試季度 | 15 場 | 低於此數的季度標為跳過。 |
| 可提出候選權重的走步 folds | 2 個 | 少於兩個未見季度只屬探索性。 |

可用環境變數改變 archive 位置或網格，不必修改腳本：

```bash
OVERSEAS_PREDICTION_GLOB='archive/overseas_s1s2_predictions/**/*.json' \
OVERSEAS_RESULTS_CSV='archive/overseas_s1s2_results.csv' \
OVERSEAS_ODDS_DROP_WEIGHTS='0,0.05,0.10,0.20,0.30' \
OVERSEAS_CV_OUTPUT_DIR='v102_s1s2_odds_drop_cross_validation' \
./run_s1s2_odds_drop_cross_validation.sh
```

## 2. 嚴格走步規則

每個測試季度只可使用其**之前**的季度選擇權重；本期結果不會參與本期選擇。訓練期內先比較每個候選權重的場內 Brier score，選取最低者；只有 Brier 完全相同時，才以已結算 ROI 作次要排序。這個順序旨在降低少數高派彩結果主導參數的風險。

選定權重隨後固定於下一個未見季度，輸出該季度的 Brier、首選勝率、固定單位 ROI、最大回撤、覆蓋率和被排除紀錄。最終的 `recommended_weight` 是多個走步 fold 中被選取最頻繁的權重，而不是事後最高未見 ROI 的權重。

> 只有同場、同馬、完整且時間合格的 T-15／T-5 快照能啟用落飛訊號。模型生成必須在 T-5 後 300 秒內；缺少快照、時間不符或官方賽果缺失的記錄不能被用來填補 100 場門檻。

## 3. 輸出與判讀

| 輸出 | 用途 |
|---|---|
| `cross_validation_summary.json` | 門檻狀態、推薦狀態、各 fold 與完整排除清單。 |
| `walkforward_fold_summary.csv` | 每個未見季度的選定權重、訓練／測試場數、Brier、ROI與回撤。 |
| `training_weight_grid.csv` | 每個訓練窗內所有權重的 Brier 與 ROI。 |
| `test_details_*.csv` | 個別未見季度的首選與結算明細。 |
| `cross_validation_exclusions.csv` | 不完整快照、缺結果或格式問題。 |

`candidate_for_further_monitoring` 只表示已完成至少兩個走步 fold，且某權重在過往訓練窗獲得較一致選擇；它不是「最優」或收益保證。若 `status` 為 `N/A_insufficient_complete_settled_races`，必須先累積真實 archive，不能用合成 fixture、最終賠率或賽後派彩補足。

## 4. 建議的每週維護

在持久主機完成海外賽果歸檔後，先更新 `archive/overseas_s1s2_results.csv`，再執行此指令並保存輸出目錄。僅在結果由 `N/A` 轉為已完成、且有至少兩個合格未見季度後，才檢視 `recommended_weight`。任何權重變更都應記錄版本、啟用日期、過去 fold 表現及下一次覆核日期。
