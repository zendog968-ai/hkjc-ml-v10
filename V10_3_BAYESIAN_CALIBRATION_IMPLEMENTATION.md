# V10.3 Bayesian Calibration／不確定性覆蓋層：實作與運行規範

**版本：** V10.3-P1 research overlay
**分支：** `feature/v10.3-bayesian-calibration`
**定位：** 並列的風險披露與離線研究，不是 V10.2 主模型替代品。

## 範圍與保護契約

本實作在 V10.2 LightGBM＋CatBoost 集成之外新增一個獨立 NumPyro 模組。它只讀取已保存、已場內正規化的 V10.2 賽前機率作為 offset；正式 V10.2 的特徵工程、ensemble 機率、排序、Win／Place EV、Kelly、排位與賠率流程均不會被重算或覆寫。

> V10.3 sidecar 即使成功輸出後驗均值，也只代表研究性的校準敏感度與不確定性摘要。它不構成投注訊號、保證，或對 V10.2 排名的取代。

| 保護項目 | 實作方式 |
|---|---|
| V10.2 正式機率 | 原始 `prediction.json` 只讀；sidecar 保存 `v102_predicted_win_probability` 鏡像以供比較。 |
| 排名、EV 與 Kelly | `bayesian_calibration.py` 不載入或呼叫 V10.2 的 EV／Kelly 函式，也不寫入上述欄位。 |
| 賽前／賽後隔離 | `fit` 僅能讀取已結算歷史 CSV 的 `target_win`；`predict` 不接受或使用賽後標籤。 |
| 場內機率守恆 | 每一 posterior draw 在 sidecar 和回測中均驗證總和為 `1 ± 1e-6`。 |
| 生產故障降級 | T-5 排程中的 overlay 為非阻斷步驟；未有模型、資料契約失敗或推論錯誤時，V10.2 仍照常產生原有報告。 |

## 機率模型

V10.3 使用 NumPyro AutoNormal SVI 建立一個輕量、部分池化的 categorical calibration overlay。對每場 `r`、馬匹 `i`，其 posterior draw 使用：

```text
s_ri = α × log(p_v102,ri) + β_course[r] × δ_component,ri
q_ri = softmax(s_ri)
```

`p_v102` 是保存的 V10.2 場內機率。`δ_component` 是場內正規化後的 LightGBM 與 CatBoost 機率差；它是 V10.2 已有的賽前 component 審計欄位，而不是賽果、最終賠率或派彩。`β_course` 以全局係數為中心作 hierarchical shrinkage；未知馬場只退回全局係數，不會創造臆測的馬場優勢。

| 輸出欄位 | 意義 | 可否替代 V10.2 |
|---|---|---|
| `posterior_win_mean` | posterior draws 的邊際均值。 | 否。 |
| `posterior_win_p05`／`posterior_win_p95` | 5%／95% 邊際分位數。分位數不需橫向相加為 1。 | 否。 |
| `top1_rank_stability` | V10.2 首選在 posterior draws 仍居第一的比例。 | 否，只作混亂度提示。 |
| `posterior_entropy_mean` | 每個 posterior draw 的場內正規化熵平均。 | 否。 |
| `posterior_component_disagreement` | component 差異經 posterior slope 傳遞後的敏感度。 | 否。 |

## 模組與工件

| 檔案 | 用途 |
|---|---|
| `bayesian_calibration.py` | NumPyro SVI 離線 fit，以及由 V10.2 `prediction.json` 產生不確定性 sidecar。 |
| `backtest_v10_3.py` | 以完整賽日、expanding-window 進行離線三 fold 回測與採納閘門檢查。 |
| `verify_v103_bayesian_calibration.py` | 使用真實保存 V10.2 歷史工件的小型探索性契約測試；不是效能證據。 |
| `filter_high_probability.py` | 僅在提供 sidecar 時，在既有 Markdown／WhatsApp 預覽文字旁加入 V10.3 披露。無 sidecar 時維持原有行為。 |
| `pre_race_scheduler.py` | T-5 先完成 V10.2 預測；其後以非阻斷方式呼叫 sidecar，成功才傳予 Markdown filter。 |

## 離線 fit 與賽前 sidecar

離線 fit 必須使用已保存的、具唯一 `target_win=1` 的 V10.2 歷史預測 CSV。它不會重訓或修改 `horse_model.pkl`。

```bash
cd /home/ubuntu/hkjc_v10_database
python3 bayesian_calibration.py fit \
  --predictions v102_multiseason_backtest_predictions.csv \
  --output-model models/v103_bayesian_calibration.npz \
  --advi-steps 10000 \
  --posterior-draws 400 \
  --seed 10301
```

賽前產生 sidecar 時，必須保留 V10.2 原始預測 JSON，並由排程或呼叫端傳入已知的官方賽日、馬場與場次。若模型檔不存在，程式會寫出 `unavailable_model_artifact` sidecar，而非錯誤地使用後驗均值取代 V10.2。

```bash
python3 bayesian_calibration.py predict \
  --model models/v103_bayesian_calibration.npz \
  --prediction runtime/pre_race/2026-08-19_ST_R01/prediction.json \
  --output runtime/pre_race/2026-08-19_ST_R01/v103_bayesian_uncertainty.json \
  --race-date 2026-08-19 --racecourse ST --race-no 1 \
  --posterior-draws 200 --seed 10301
```

## 盲測回測與採納閘門

正式評估需要三個 expanding-window folds。每個 fold 按**完整、不交疊賽日**依序建立 train、forward validation 與最後未見 test；V10.3 NumPyro fit 只使用該 fold 的 train。validation 只作監測與透明報告，不會用 test 設定參數或回填 posterior。

```bash
python3 backtest_v10_3.py \
  --predictions v102_multiseason_backtest_predictions.csv \
  --initial-train-races 100 \
  --validation-races 25 \
  --test-races 50 \
  --max-folds 3 \
  --svi-steps 10000 \
  --posterior-draws 400 \
  --output-dir archive/v103_bayesian_backtest/formal
```

| 閘門 | 最低要求 | 不達標行為 |
|---|---|---|
| 未見 test 覆蓋 | 至少 3 folds、合計至少 150 場。 | `insufficient_data`／`NOT_ELIGIBLE`。 |
| 整體 Brier | overlay 相對 control 改善至少 `0.005`。 | 禁止概率替換。 |
| 分 fold Brier | 至少 2/3 folds 改善至少 `0.005`。 | 禁止概率替換。 |
| Log score | 至少 2/3 folds 不惡化。 | 禁止概率替換。 |
| 機率守恆 | 所有 posterior draws 均通過。 | 禁止概率替換。 |
| 最終決策 | 即使全部達標也只輸出 `REVIEW_REQUIRED`。 | 必須人工審核並建立獨立 V10.3-B 版本；不得自動覆寫 V10.2。 |

## 報告呈現

`filter_high_probability.py` 新增可選 `--bayesian-overlay`。在 sidecar 狀態為 `available_research_only` 時，Markdown 會增加一個「V10.3 貝氏校準／不確定性披露（研究性）」區段，列出首選穩定度、熵、場內守恆誤差及各馬 P05／P95。它會明確展示「V10.2 勝率（保留）」欄，而不是以 posterior mean 覆寫選馬表、EV 或 Kelly。

當 sidecar 不可用時，報告只披露原因與「V10.2 正式輸出維持不變」。T-5 自動化亦以相同原則處理，讓研究模組永遠不成為生產預測的單點故障。
