# V10.2 海外候選特徵與校準框架操作手冊

**版本：** `overseas_candidate_features_v1`／`overseas_candidate_logistic_brier_v1`

**作者：** Manus AI

**狀態：** 候選研究層；預設拒絕訓練；不接入 V10.2 或 N6 生產服務。

## 1. 目的與隔離邊界

本框架旨在為未來海外公開研究增加可選的賽前分段步速及騎師／練馬師 ELO 欄位，並在有足夠、不可變的盲測證據時，提供一個離線 Logistic Regression／Brier 比較研究入口。它不是現行 N6 的擴充、替代模型或部署計畫。海外賽事的 `n6_status` 必須保持 `disabled_non_hk`，而 V10.2 的正式機率、EV、Kelly、模型檔與 SQLite 主資料庫均不得被候選層讀寫或改變。

| 元件 | 路徑 | 允許行為 | 禁止行為 |
|---|---|---|---|
| 候選特徵接口 | `overseas_candidate_feature_interface.py` | 建立附加的賽前特徵 annex。 | 修改已封存決策、補入賽後資料或呼叫 N6。 |
| 資格與校準框架 | `overseas_candidate_calibration.py` | 審計15場資格、產生狀態報告、提供受核准的離線函數。 | 計時器自動訓練、寫入模型檔或改寫 V10.2。 |
| 計時器包裝器 | `run_overseas_blindtest_with_candidate_audit.sh` | 在既有盲測工作後更新資格狀態。 | 使用 `--mode train` 或建立核准檔。 |
| 狀態目錄 | `runtime/overseas_candidate_calibration/` | 儲存可重建的資格報告。 | 將其加入 Git 或視為預測／模型輸出。 |

## 2. 可選特徵契約

分段步速及騎師／練馬師 ELO 全部為可選欄位；缺值保持 `null` 並以 `*_available = 0.0` 標示，絕不以賽後數據或中性常數假裝資料存在。每個候選特徵來源必須有開跑前的明確 UTC 擷取時間、來源檔案及 SHA-256。若 `source_captured_at_utc >= scheduled_start_utc`，接口立即拒絕建立該列。

| 類別 | 欄位 |
|---|---|
| 分段步速 | `sectional_early_pace_rating_pre`、`sectional_mid_race_pace_rating_pre`、`sectional_final_600_seconds_pre`、`sectional_final_400_seconds_pre`、`sectional_source_quality_pre` |
| 人員能力 | `jockey_elo_pre`、`trainer_elo_pre` |
| 共同溯源 | `feature_contract_sha256`、`source_captured_at_utc`、`source_path`、`source_sha256`、`n6_status=disabled_non_hk` |

## 3. 預設拒絕訓練閘門

候選校準器不會在樣本累積時自動擬合。既有每分鐘盲測計時器只運行 `--mode audit`，將狀態更新為 `denied_by_default` 或 `candidate_report_ready_requires_independent_approval`。即使後者出現，仍須由使用者／研究治理程序建立獨立核准檔，且核准檔不得由任何計時器或程式自動生成。

| 閘門 | 必須條件 |
|---|---|
| 樣本量 | **正好至少15場**已結算、同一註冊 study 的事件。 |
| 時間順序 | 每場 `captured_at_utc < scheduled_start_utc`；事件必須嚴格時間排序。 |
| 來源完整性 | 決策及官方結果檔案 SHA-256 完整；官方 field 與賽前 field 一致。 |
| 機率契約 | 每場賽前勝率總和為1；同一 `proxy_version`；來源保持研究性。 |
| 隔離 | 每場 `n6_status=disabled_non_hk`；候選層不接觸 V10.2。 |
| 獨立覆盤 | 已有官方結果結算，而非使用使用者暫定名次或賽後重抓決策。 |
| 人工核准 | 有獨立 `overseas_candidate_calibration_approval_v1` 核准檔；範圍只能是 `offline_candidate_only`。 |

> 在所有閘門通過前，`--mode train` 一律拒絕。即使全部通過，離線函數的 in-sample Brier 比較也不是啟用標準；仍須進行獨立、時間排序的 out-of-sample 評估，且不得改寫海外 N6 停用規則。

## 4. 監控與驗證

現有 `hkjc-overseas-blindtest.service` 已以 `20-candidate-audit.conf` 加入候選資格審計後置步驟。這不更改其既有封存、鎖定、60秒HKJC節流或15場上限。服務每次完成後更新：

```bash
cat runtime/overseas_candidate_calibration/eligibility_status.json
sudo systemctl status hkjc-overseas-blindtest.timer --no-pager
```

安全測試：

```bash
.venv/bin/python test_overseas_candidate_calibration_framework.py
.venv/bin/python overseas_candidate_calibration.py --mode audit
.venv/bin/python overseas_candidate_calibration.py --mode train  # 預期拒絕
```

## 5. 現時狀態與下一步

目前仍未有15場符合不可變賽前封存與官方結算的同一海外 RPR／TS study 事件，因此候選校準狀態必然是 `denied_by_default`。今晚收集到的手動／單場研究工件不會被回填為15場盲測樣本，除非它們符合原始決策、結果與時間順序契約。

下一步是維持海外盲測管線的官方 manifest 驗證與賽前封存，逐場累積同一版本的有效事件。樣本達15場後，系統只會提示可以準備候選報告；是否運行任何離線擬合仍需獨立核准與時間序列評估。
