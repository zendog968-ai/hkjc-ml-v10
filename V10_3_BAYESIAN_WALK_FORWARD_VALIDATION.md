# V10.3 貝氏校準覆蓋層：Expanding-Window 時間外驗證器

**作者：** Manus AI

## 用途與範圍

`walk_forward_v103_bayesian_uncertainty.py` 評估 V10.3-P1 的**平行研究性 Bayesian temperature overlay**。它以已保存、已場內正規化的 V10.2 賽前勝率工件為唯一模型輸入，按時間作 expanding-window 切分，並在未見 test 賽事上比較 Control V10.2 與 posterior mean overlay 的 proper scores。

> 此工具不會重建 ELO、賽前特徵、LightGBM、CatBoost 或 isotonic calibrator；也不會改寫正式 `predicted_win_probability`、排序、EV、Kelly 或 P0 風險規則。`target_win` 只在每個 partition 的賽後評估使用。

## 模型與重要限制

覆蓋層以每場已保存機率 `p_i` 構造：

```text
q_i(T) = softmax(T × log(p_i))
log(T) ~ Normal(0, prior_sd²)
```

它以一維 MAP 加 Laplace 後驗近似產生固定數量的 posterior draws。每一次 draw 的場內機率向量均會檢查為 `1 ± 1e-6`。

因為 `T > 0` 時是嚴格單調轉換，這個 P1 scaffold **保留每場全部馬匹的排序**。所以 Top-1、Top-3、頭馬模型排名 7+ 與 `top1_rank_stability` 的排名變化在此實驗中不具辨識意義；它可以研究校準、Brier、log score、posterior 分位數寬度與熵，但不能聲稱修復深位頭馬。只有日後導入非單調的階層特徵覆蓋層，才可將 rank 7+ 作為候選改善目標。

## 資料切分與無洩漏保護

來源多馬季預測工件目前保存 `race_date`，但未保存每場精確 scheduled start。因此驗證器以**完整、不交疊的賽日**作最小時間單位：任何同一賽日的賽事均不會被拆開至 train、validation 和 test 的不同 partition。

每一 fold 依序執行以下步驟：

1. 以過去完整賽日的 train 賽事擬合各 `prior_sd` 的 posterior。
2. 只在其後完整賽日 validation 區段，依 overlay Brier、再依 log score 選擇唯一 `prior_sd`。
3. 以鎖定的設定重用同一 train 資料重新擬合 posterior。
4. 對最後完整賽日 test 區段只評估一次，保存 posterior draws 摘要、機率和與所有指標。
5. 下一個 fold 才可把先前 validation／test 視為歷史資料擴大 train。

## 執行範例

以下是示範性小窗口；它只用於驗證管線，未達 V10.3 採納門檻：

```bash
cd /home/ubuntu/hkjc_v10_database
python3 walk_forward_v103_bayesian_uncertainty.py \
  --predictions v102_multiseason_backtest_predictions.csv \
  --min-train-races 50 \
  --validation-races 25 \
  --test-races 25 \
  --max-folds 3 \
  --posterior-draws 200 \
  --seed 10301 \
  --output-json archive/v103_bayesian_validation/demo/report.json \
  --output-md archive/v103_bayesian_validation/demo/report.md \
  --output-race-csv archive/v103_bayesian_validation/demo/race_metrics.csv
```

若可用完整、保存的賽前預測工件足夠，正式研究配置應至少以 200 場 expanding train、50 場 validation、50 場 test 及最多 3 folds 執行。若沒有形成 fold 或未見 test 少於 150 場，報告必須維持 `insufficient_data`。

## 輸出工件

| 檔案 | 內容 |
|---|---|
| `report.json` | 完整 input hash、配置、fold 邊界、prior 選擇、posterior 摘要、逐場 test 指標與採納 gate。 |
| `report.md` | 人類可讀的 fold 表、Control／Overlay 指標與資料限制。 |
| `race_metrics.csv` | 每個未見 test 場的 Brier、log score、posterior 抽樣摘要、守恆誤差與排序語義。 |

## 採納判讀

預先登記的正式 V10.3-B 機率替換條件為至少 3 folds、150 場未見 test、每 draw 守恆，整體場內 Brier 改善至少 0.005、至少 2/3 fold 改善、至少 2/3 fold 的 log score 不惡化，以及完整資料 coverage。此 P1 保序實驗不可以 `winner_rank_7_plus` 或 Top-1／Top-3 變化判定成敗；這些欄位必須被標示為 `rank_change_evaluable: false`。

在所有採納條件達標以前，V10.2 仍是唯一正式場內勝率；V10.3 posterior 只可作平行研究、P0 不確定性披露與模型監測。
