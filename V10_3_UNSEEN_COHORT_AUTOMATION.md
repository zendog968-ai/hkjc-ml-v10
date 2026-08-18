# V10.3 未見賽事 Cohort 自動收集與 150 場門檻排程

**作者：Manus AI**
**版本：V10.3 研究性 Bayesian calibration pipeline**

## 目的與狀態

本流程會將本地 V10.2 的 **T-5 賽前預測快照**，在官方賽果已完成入庫後，收集成 V10.3 Bayesian calibration 的未見賽事 cohort。它的目的不是賽後重建預測，也不會直接修改 V10.2 的正式勝率；它只建立一條可稽核資料鏈，為 V10.3 平行研究累積樣本。

> **未見賽事**在此指：預測 JSON 已在排定開跑時間前產生、來源檔雜湊仍相符、來源不含賽後欄位、官方結果已歸檔、以及賽前 prediction field 與官方 field 完整一對一對應的本地 ST／HV 賽事。

| 階段 | 門檻 | 行為 | 對 V10.2 正式機率的影響 |
|---|---:|---|---|
| 收集期 | 0–149 場 | 每日收集、去重、顯示完整性拒絕原因。 | 無。 |
| 監測期 | ≥150 場 | 產生「150 場門檻已達」狀態，持續累積。 | 無。 |
| 完整走步期 | ≥325 場（預設） | 以 100 場初始 train、3 個 25 場 validation＋50 場 test fold 執行一次未見 walk-forward。 | 僅研究報告。 |
| 採納審核 | 符合 V10.3 gate | 人工審核預先登記的 Brier、log score、校準及覆蓋門檻。 | 未通過前，**不得替代** V10.2。 |

150 場是監測門檻，並不是預設完整 expanding-window 設計的充分條件。若採用 `initial_train=100`、3 個 `(validation=25,test=50)` fold，完整無重疊評估需要至少 **325 場**同一 base-model cohort。這個較高門檻用來避免把重疊或短樣本結果誤當成正式改進。

## 不可變資料鏈與安全閘門

T-5 排程器完成 `predict.py` 後，會在同一場次資料夾寫入 `v103_snapshot_provenance.json`。此 provenance 包含排定開跑 HKT、預測生成 HKT、模型 SHA-256、預測 JSON SHA-256、來源種類與賽後欄位排除聲明。Cohort 收集器只接受 `prediction_generated_hkt < scheduled_start_hkt` 的來源。

收集器再以本地 SQLite 的官方 `starters` 賽果驗證唯一頭馬與完整 field。只有在預測馬號、官方馬號、機率向量與頭馬標籤均完整時，才封存一份 V10.3 cohort record。封存 record 可含 `actual_win`，但其原始 source prediction 永遠保持 `post_race_labels_in_source=false`；兩者均保留 SHA-256 連結，避免把賽後資料偽裝為賽前資料。

| 拒絕原因範例 | 收集器行為 |
|---|---|
| 預測在排定開跑後才生成 | 拒絕，記錄 `prediction_not_strictly_prerace`。 |
| 預測檔內容與 provenance SHA-256 不同 | 拒絕，記錄 `prediction_snapshot_hash_mismatch`。 |
| 預測 rows 內含名次、派彩或 target 欄位 | 拒絕，記錄 `prediction_rows_contain_post_race_field`。 |
| 官方賽果未入庫、無唯一頭馬或 field 不匹配 | 拒絕並保留原因；不補猜。 |
| 同一場次以不同 source snapshot 重跑 | 不覆蓋既有 cohort record，記錄衝突。 |

每次月度重訓後，`horse_model.pkl` SHA-256 會改變。收集器會按完整 SHA-256 切分 cohort，**不得跨模型版本合併**以湊足 150 或 325 場。

## 每日執行順序

每日 05:10 HKT 的工作會執行 `run_daily_v103_bayesian_cohort.sh`。它先檢查既有 03:15 HKT 官方賽果 archive／海外回刷鎖；如仍在執行，當日安全跳過，翌日重試。工作取得自身 `flock` 後才會掃描 `runtime/pre_race/*/v103_snapshot_provenance.json`，並寫入以下路徑：

| 工件 | 路徑 |
|---|---|
| 封存未見 records | `archive/v103_bayesian_cohort/records/<model_sha前16位>/` |
| 最新累積狀態 | `archive/v103_bayesian_cohort/manifest_latest.json` |
| 每日收集日誌 | `archive/v103_bayesian_cohort/logs/YYYY-MM-DD.log` |
| 完整走步輸入與報告 | `archive/v103_bayesian_cohort/evaluations/<model_sha前16位>/` |
| 成功評估指紋 | `latest_successful_evaluation.json` |

成功評估後，cohort fingerprint 會寫入 marker。若翌日沒有新增有效賽事，狀態為 `evaluation_unchanged_cohort`，不會重跑相同 Bayesian walk-forward。若有新增有效 snapshot，才建立新 fingerprint 並重新評估。

## 主機部署

本流程需要現有、持續運行並保存 SQLite／模型／T-5 snapshot 的 Linux 主機；短暫工作區不應安裝 Cron。請先將更新同步至主機，然後預覽與安裝：

```bash
cd /home/ubuntu/hkjc_v10_database
git pull --ff-only origin main
chmod +x \
  run_daily_v103_bayesian_cohort.sh \
  install_daily_v103_bayesian_cohort_cron.sh
./install_daily_v103_bayesian_cohort_cron.sh --show
./install_daily_v103_bayesian_cohort_cron.sh --install
crontab -l
```

安裝器只更新 `# BEGIN HKJC_V10_3_BAYESIAN_COHORT` 至 `# END HKJC_V10_3_BAYESIAN_COHORT` 之間的區塊，不會覆蓋既有 03:15 archive／backfill、04:30 repository review 或每月重訓排程。

## 驗收與日常檢查

可手動執行一次，不會從網絡重新抓取資料：

```bash
cd /home/ubuntu/hkjc_v10_database
./run_daily_v103_bayesian_cohort.sh
tail -n 80 archive/v103_bayesian_cohort/logs/"$(TZ=Asia/Hong_Kong date +%F)".log
cat archive/v103_bayesian_cohort/manifest_latest.json
```

驗收時應確認 `discovered_provenance_files` 只計算正式 T-5 來源；每個 `model_cohorts` 記錄都有完整 SHA-256、race count、最早／最晚開跑時間與狀態。初始狀態為空 cohort 是正常結果，代表當前主機尚未留存符合規格的 T-5 快照，而不是評估失敗。

> 本流程只量化模型研究所需的不確定性與校準表現；它不會下注、傳送投注指示、調高投注額，亦不會在未通過 V10.3 採納閘門前改寫 V10.2 正式輸出。
