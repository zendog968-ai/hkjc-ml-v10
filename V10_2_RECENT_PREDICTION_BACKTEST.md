# V10.2 近期預測自動回測

本工具 `backtest_recent_v102_predictions.py` 用於評估**已保存且已結算**的 V10.2 預測機率檔。預設讀取 `v102_multiseason_backtest_predictions.csv` 的最後 50 個賽事群組，並在 `archive/backtest_reports/` 產生 JSON 與 Markdown 報告。

> **資料洩漏保護：** 回測器只讀取既有預測檔中的場內正規化勝率與事後 `target_win` 標籤。它不會重新訓練模型、不會以賽後賽果重建賽前特徵，也不會覆寫原始預測檔。

## 資料契約與完整性閘門

| 項目 | 規則 |
|---|---|
| 預測欄位 | 必須包括 `race_date`、`racecourse`、`race_no`、`horse_name`、`target_win` 及指定機率欄位。 |
| 勝率欄位 | 預設為 `race_normalized_probability`；必須有限、介乎 0 至 1，且每場總和為 `1±1e-6`。 |
| 賽果 | 每場必須恰好有一匹 `target_win=1`。 |
| 馬匹集合 | 馬名不可空白或重複；不完整場次會被排除並記錄原因。 |
| 小樣本 | 少於 15 場已評估賽事時，Markdown 會標示為探索性樣本。 |

## 指標口徑

| 指標 | 定義 |
|---|---|
| Top-1 勝出率 | 每場最高預測機率的馬匹是否為頭馬。 |
| Top-3 包含頭馬率 | 正確頭馬是否位於該場預測前 3 名。 |
| 場內 Brier Score | 每場所有馬匹的 `Σ(p−y)²`，再跨場平均；數值越低越好。 |
| 均勻 Brier 基準 | 同一已評估場次內每匹馬機率設為 `1 / field_size` 的 Brier 分數。 |
| Brier 改善 | 均勻 Brier 減去模型 Brier；正值代表模型優於相同場次的均勻基準。 |
| 校準分箱 | 逐馬將預測機率分為五個區間，比較平均預測機率與實際頭馬率。 |

## 手動執行

```bash
cd /home/ubuntu/hkjc_v10_database

# 最近 50 場賽事群組
python3 backtest_recent_v102_predictions.py \
  --predictions v102_multiseason_backtest_predictions.csv \
  --recent-races 50

# 以預測檔中最新賽日為基準，檢查最近 30 日
python3 backtest_recent_v102_predictions.py \
  --predictions v102_multiseason_backtest_predictions.csv \
  --recent-days 30 \
  --output-json archive/backtest_reports/recent_30d.json \
  --output-md archive/backtest_reports/recent_30d.md
```

`--recent-days` 的時間窗口以**預測檔中最新賽日**計算，而不是使用現在時間；這使離線歷史工件的輸出保持可重現。

## 每日自動執行

`run_daily_repo_sync_review.sh` 已在每日 04:30 HKT 的同步／程式審核程序中加入最近 50 場回測。它會產生：

```text
archive/backtest_reports/daily_recent_v102_backtest.json
archive/backtest_reports/daily_recent_v102_backtest.md
```

可透過下列環境變數調整窗口，而毋須修改程式碼：

```bash
HKJC_RECENT_BACKTEST_RACES=100 ./run_daily_repo_sync_review.sh
```

若回測資料契約不完整，日常審核會記為 `REVIEW_FAILED`，但不會改動模型、SQLite 資料庫或已保存的預測工件。
