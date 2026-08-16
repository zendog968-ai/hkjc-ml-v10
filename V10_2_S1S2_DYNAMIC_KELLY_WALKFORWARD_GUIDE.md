# V10.2 S1/S2 Dynamic Kelly Walk-Forward Guide

> **研究目的：** 在 S1/S2 海外落飛權重的跨季度走步驗證中，讓 Kelly 資金比例只由**過去訓練季度**的校準、證據量和回撤承受度決定，再固定用於下一個未見季度。此工具用於風險診斷及歷史研究，並不保證回報或代表實際下注指示。

## 1. 啟用方式

一鍵指令已預設啟用動態 Kelly：

```bash
cd /home/ubuntu/hkjc_v10_database
./run_s1s2_odds_drop_cross_validation.sh
```

以下環境變數可調整研究假設，但應在某次走步驗證開始前固定，不能看過測試季度後回調：

| 變數 | 預設值 | 定義 |
|---|---:|---|
| `OVERSEAS_KELLY_INITIAL_BANKROLL` | 100 | 回測初始研究資本單位。 |
| `OVERSEAS_KELLY_MAX_SINGLE_FRACTION` | 0.01 | 單一首選可使用的最高場前資本比例。 |
| `OVERSEAS_KELLY_MAX_RACE_FRACTION` | 0.02 | 同一場所有候選的最高合計比例；目前 top-pick 評估下為額外硬上限。 |
| `OVERSEAS_KELLY_DRAWDOWN_TRIGGER` | 0.10 | 未見季度資本曲線回撤達 10% 時啟用防守模式。 |
| `OVERSEAS_KELLY_DRAWDOWN_MULTIPLIER` | 0.50 | 防守模式將縮減後 Kelly 再乘以 0.50。 |

## 2. 僅由訓練期決定的 Kelly scale

對每個未見季度，系統先用之前所有合格季度選出 odds-drop weight，再用同一訓練集推導以下三個縮減因子：

\[
\text{effective scale}=S_{calibration}\times S_{sample}\times S_{drawdown}
\]

其中校準縮減取決於模型場內 Brier 是否比等機會基準更好；若沒有改善，`S_calibration=0`。樣本縮減隨完整訓練場數增加而逐步提高，但不超過 1。回撤縮減由訓練期固定單位首選的歷史最大回撤推導；若該回撤已超過指定的 10% 承受門檻，`S_drawdown=0`。這三項全部是測試季度開始前已知的量。

單一候選的完整 Kelly 為：

\[
f_{full}=\max\left(0,\frac{pO-1}{O-1}\right)
\]

系統最後採用：

\[
f=\min(f_{single\ cap}, f_{full}\times \text{effective scale}\times G_{drawdown})
\]

`G_drawdown` 在未見季度的資本曲線由峰值下跌至少 10% 後變為 0.50，否則為 1。只有 `EV > 0` 且賽前 displayed odds 完整時才會產生正 stake；缺價格、不完整快照或 policy disabled 均為 0 stake。

## 3. 無資料與防洩漏規則

以下任一情況會令動態 Kelly 回退為 0 或整份報告為 N/A：

| 情況 | 行為 |
|---|---|
| 總完整已結算海外場次少於 100 | `N/A_insufficient_complete_settled_races`，不選權重、不建立資本曲線。 |
| 訓練窗少於 100 場或模型校準沒有勝過等機會 | policy disabled，未見季度所有 Kelly stake 為 0。 |
| T-15／T-5 快照不完整、時間不合格或無官方結果 | 排除，不作門檻補值。 |
| 未見季度內出現 10% 資本回撤 | 只把後續 stake 乘以 0.50；不會反向使用後續賽果重新選權重或重新調 policy。 |

## 4. 讀取結果

開啟動態 Kelly 後，`walkforward_fold_summary.csv` 增加 `dynamic_kelly_status`、`dynamic_kelly_scale`、訓練期 Brier／等機會 Brier、未見季度 ROI、最大回撤及淨損益。每個未見季度另有 `dynamic_kelly_details_*.csv`，保存場前資本、機率、EV、縮減後比例、stake、場後資本、回撤及 `reason`。

`enabled` 僅代表過去訓練資料滿足當前保守規則；它不是最優資本比例或未來收益承諾。應把 dynamic Kelly 結果與固定單位 ROI、Brier、覆蓋率及移除最大正回報後的壓力測試並列檢視。若不同未見季度結果差異很大，應保留零 stake 或使用較低硬上限，而不是增加 Kelly scale。
