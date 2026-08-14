# V10.2 Advanced Feature & Ensemble Edition

**作者：Manus AI**
**版本：V10.2**
**用途：香港賽馬公開資料的研究性特徵工程、賽前模型排序與市場比較。**

> **重要聲明：** V10.2 僅供資料研究與娛樂參考。它不會自動投注、發送訊息或保證任何賽果／回報。所有賠率、撤回馬、場地及出賽狀態必須以香港賽馬會最後公布資料為準。

## 1. V10.2 的升級重點

V10.2 在 V10.1 的 ELO、近績、末段走勢代理、檔位跑道偏差、班磅與裝備特徵上，新增**馬體重變幅**、**雙時點賠率落飛審計**、**新馬血統／試閘先驗接口**及 **LightGBM + CatBoost** 時間序列集成。模型輸出仍是場內相對勝出機率；位置機率由集成勝出強度透過 Plackett–Luce 名次模擬推導。

| 模組 | V10.2 實作 | 使用界限 |
|---|---|---|
| 馬體重 | `horse_body_weight_pre`、相對上仗的 `body_weight_delta_pre`、絕對變幅超過 15 磅標記。 | 缺少官方排位體重時保留未知旗標，不以 0 磅解讀。 |
| 賠率快照 | 開跑前 15 分鐘與 5 分鐘的公開 Win／Place 賠率快照。 | 只作市場審計，因歷史標籤化快照不足而**不進入目前模型訓練**。 |
| 落飛標記 | `odds_drop_ratio=(T-5 獨贏賠率 − T-15 獨贏賠率) / T-15 獨贏賠率`；小於或等於 -20% 顯示 `🔥 閘前資金落飛`。 | 僅表示公開賠率變動；不能證明大戶身份、內幕消息或賽果。 |
| 新馬先驗 | `is_new_horse`、血統距離相符度、最近結構化試閘結果及 `cold_start_prior_pre`。 | 試閘官方頁是日期型資料；未有明確結構化資料時採中性 0.5，不從評語臆測。 |
| 集成模型 | LightGBM Ranker 及 CatBoost Ranker 分別在時間序列資料上訓練、校準及用驗證期 race-Brier score 倒數加權。 | 權重是驗證期統計結果，而非對未來性能的保證。 |

## 2. 資料來源及可得性

本系統讀取香港賽馬會公開排位、賽果、馬匹近績、認可配備及試閘結果頁。排位表中可顯示負磅、檔位、排位體重及配備；馬匹資料及新馬頁可補充血統文字；試閘完整結果按日期提供名次、沿途走位、時間與評語。[1] [2] [3]

> 香港賽馬會頁面結構或公開欄位如有變化，抓取器會保留 `null` 與警示，而不是以推測值填補。新馬試閘在沒有日期型結構化來源時會維持中性先驗。

## 3. 特徵工程

### 3.1 馬體重與極端變幅

歷史賽果的 `declared_weight_kg` 欄位在系統內沿用為官方顯示的馬體重磅數。`horse_body_weight_pre` 是今仗體重；`body_weight_delta_pre` 為今仗減上仗。只有今仗及上仗體重均存在時，`body_weight_delta_known_pre=1`；絕對變幅大於 15 磅時，`is_extreme_body_weight_change_pre=1`。

這項設計將「資料未知」與「變動為零」分開，避免缺失資料被錯當穩定體重。所有歷史列只使用該場之前的體重，故不使用同場或未來資訊。

### 3.2 新馬冷啟動

新馬定義為本地正式賽事歷史出賽為零。當 `is_new_horse=1`，系統可由 `horse_new_horse_priors` 讀取截至該日的官方血統／試閘資料：

| 欄位 | 意義 | 未知時處理 |
|---|---|---|
| `pedigree_distance_match_pre` | 官方明確「合適路程」文字是否覆蓋當前路程（含 200 米緩衝）。 | 0.5 中性值；`pedigree_prior_known_pre=0`。 |
| `latest_trial_position_pre` | 最近已結構化的試閘名次。 | 0；`trial_prior_known_pre=0`。 |
| `latest_trial_margin_pre` | 最近試閘負距。 | 0；配合未知旗標。 |
| `latest_trial_qualified_pre` | 官方「及格」標記。 | 0；配合未知旗標。 |
| `cold_start_prior_pre` | 血統與試閘資訊的保守合成先驗。 | 0.5 中性值。 |

執行新馬回填：

```bash
python3 enrich_hkjc_new_horse_priors.py \
  --db hkjc_last_season.sqlite \
  --race-card race_card.json \
  --report new_horse_priors_report.json
```

可選的 `--trial-json` 必須是已由官方日期型試閘頁整理的結構化資料。腳本不會把自然語言試閘評語轉換成虛構分數。

### 3.3 賠率落飛雙快照

在持續 Linux 伺服器上，`pre_race_scheduler.py` 每分鐘由 Cron 啟動。它在 T-15 保存 `odds_t_minus_15.json`，於 T-5 保存 `odds_t_minus_5.json`，然後才執行 `predict.py` 與 `filter_high_probability.py`。

```bash
# 手動重播／研究用
python3 fetch_hkjc_live_odds.py \
  --race-card race_card.json \
  --snapshot-output odds_t_minus_15.json \
  --snapshot-label T_MINUS_15 \
  --race-date 2026/09/06 --racecourse ST --race-no 3

# 五分鐘後，以同一 race_card 再取一次，標記 T_MINUS_5；再傳入 predict.py。
python3 predict.py \
  --db hkjc_last_season.sqlite --model horse_model.pkl \
  --race-card race_card.json \
  --win-odds-overlay odds_overlay.json \
  --place-odds-overlay place_odds_overlay.json \
  --odds-snapshot-early odds_t_minus_15.json \
  --odds-snapshot-late odds_t_minus_5.json \
  --output-json prediction.json --output-csv prediction.csv
```

`fetch_hkjc_live_odds.py` 對 `0`、空值、SCR、逾時或公開頁變動採降級處理，輸出合法 JSON 及 `metadata.status=degraded`。在此情況，預測仍完成，但落飛率、EV 或 Kelly 可為空值／零，不能視為市場訊號。

## 4. 模型訓練與驗證

先重建無未來資料特徵，再訓練集成模型：

```bash
python3 build_elo_features.py \
  --db hkjc_last_season.sqlite \
  --report v102_feature_report.json

python3 train_lightgbm.py \
  --db hkjc_last_season.sqlite \
  --model horse_model.pkl \
  --report lightgbm_training_report.json \
  --predictions lightgbm_backtest_predictions.csv
```

訓練器以賽日時間排序切分 70% 訓練、15% 驗證及最後 15% 測試。LightGBM 和 CatBoost 均採 ranking objective；驗證期各自校準後，按驗證期場內 Brier score 的倒數正規化成集成權重。V10.2 當前測試期為 138 場，Top 1 勝出率 18.8%、Top 3 包含頭馬率 46.4%、場內 Brier score 0.8877，低於等機會基準 0.9192；這些是歷史研究指標，不構成未來績效保證。

## 5. 預測與雙策略報告

`predict.py` 會輸出 LightGBM、CatBoost 和集成的校準前／後欄位、Win／Place 機率、EV、保守 quarter-Kelly、體重、新馬、裝備、跑道與賠率落飛審計欄。`filter_high_probability.py` 保留兩個策略：

| 策略 | 門檻 |
|---|---|
| 熱門穩攻 | 獨贏勝率 ≥ 10% **或** 位置勝率 ≥ 85%；位置勝率 ≥ 90% 為「超級焦點」。 |
| 冷門突襲 / Value Bomb | 獨贏賠率 ≥ 10、位置賠率 ≥ 3.5、獨贏勝率 ≥ 8% 及位置勝率 ≥ 80%。 |

篩選器只會生成 `https://api.whatsapp.com/send?...` 的預覽連結，目標 `85296896832`；它**不會自動發送**。Telegram 不能以電話號碼作 Direct Link，如要增加 Telegram 發送，需另行提供 `@username`、頻道名稱或 bot chat ID。

## 6. 自動化與部署

`pre_race_schedule.example.json` 使用：

```json
{
  "timezone": "Asia/Hong_Kong",
  "snapshot_minutes_before": [15, 5],
  "meeting": {
    "race_date": "YYYY/MM/DD",
    "racecourse": "ST",
    "race_start_times": {"1": "13:00"}
  }
}
```

持續 Linux 主機可每分鐘執行：

```cron
* * * * * cd /opt/hkjc-ml-v10 && /usr/bin/python3 pre_race_scheduler.py \
  --config pre_race_schedule.json --project-dir /opt/hkjc-ml-v10 \
  --output-root /var/lib/hkjc-v10/pre_race \
  --state-file /var/lib/hkjc-v10/pre_race_state.json \
  >> /var/log/hkjc-v10/pre-race.log 2>&1
```

GitHub Actions 的 `race_day_scan.yml` 是賽前約 60 分鐘的報告／備援掃描，並非 T-15／T-5 的可靠替代品；GitHub 排程最短為五分鐘且可能延遲。[4] 請以持續 Linux Cron 執行正式雙快照流程。

## 7. 測試與版本控制

```bash
python3 test_v102_advanced.py
python3 test_equipment_features.py
python3 test_predict_without_odds.py
python3 validate_place_prediction.py --prediction v102_prediction_test.json
python3 test_pre_race_automation.py
python3 test_github_actions_workflow.py
```

大型 SQLite、模型、日誌、即時快照與日常預測輸出應受 `.gitignore` 保護。程式碼、設定範本及指南可提交至私人儲存庫；新的 `horse_model.pkl` 和 `hkjc_last_season.sqlite` 應上傳至私人 Release `v10-assets`，供 GitHub Actions 下載。

## References

[1] [香港賽馬會：排位表](https://racing.hkjc.com/zh-hk/local/information/racecard)
[2] [香港賽馬會：馬匹資料與試閘結果](https://racing.hkjc.com/zh-hk/local/information/horse)
[3] [香港賽馬會：認可配備](https://racing.hkjc.com/racing/chinese/racing-info/reg_approved_gear.aspx)
[4] [GitHub Docs：Scheduled workflows](https://docs.github.com/actions/using-workflows/events-that-trigger-workflows#schedule)
