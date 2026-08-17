# V10.2 海外 2023–2026 回刷批次與特徵工程排程指南

**適用工具：** `run_overseas_backfill_batch.sh`、`audit_overseas_feature_readiness.py`、`backfill_overseas_2023_2026.py`
**資料範圍：** 2023-01-01 至 2026-08-17
**目的：** 以可恢復、單線程、限速及可稽核方式補齊已發現但尚未完成的海外轉播歷史賽事；每批完成後檢查 S1/S2 特徵資料是否足以作無洩漏預測。

## 1. 執行方式選項

| 方式 | 使用情境與取捨 | 成本 | 設定複雜度 |
|---|---|---|---|
| **現有 Linux 主機的每日 Cron** | 適合目前 Python／SQLite／瀏覽器式官方頁解析流程；可直接使用現有資料庫及 Git 專案，主機必須保持開機。**建議用於現有 V10.2。** | 使用既有主機，沒有額外平台費用。 | 低。 |
| **受管背景服務** | 適合日後希望有網頁監控介面、調整批量大小或集中查看報告；現有 Python 官方頁解析需先移植或封裝成支援的受管執行環境。 | 視託管平台的使用量而定。 | 中至高。 |
| **手動分批執行** | 適合先觀察 HKJC 歷史來源穩定性或在短期測試期間控制請求量；需要人工啟動。 | 無額外費用。 | 最低。 |

> 本工具不使用密集輪詢，亦不會嘗試繞過 HKJC 的 403、429、CAPTCHA 或動態頁限制。遇到資料端點缺口時，race 保留 `partial`／`source_unavailable`，讓下一次批次安全重試。

## 2. 安全設計

`run_overseas_backfill_batch.sh` 已包含下列控制：

| 控制 | 行為 |
|---|---|
| 單一執行鎖 | 使用 `flock`；若已有批次執行則本次安全跳過，避免 SQLite 同時寫入。 |
| 有限批量 | 預設一次最多處理 6 個群組，避免對官方來源造成大量連續請求。 |
| 限速與冷卻 | 預設每次官方請求間隔 3–6 秒；每 20 次請求冷卻 60 秒。 |
| 可恢復選取 | 使用 `--resume`，優先處理未 archive 的 `discovered` 群組，再輪替處理最久未嘗試的 `partial`／`source_unavailable` 缺口。 |
| 舊資料庫相容 | 子賽事仍為 `partial`／`source_unavailable` 時，即使舊 meeting 狀態未同步，仍會重新入選。 |
| 原始來源保存 | 官方 HTML 存於 archive raw 路徑；每一批另保存 log、backfill summary、feature readiness 與 manifest。 |
| 完整性判定 | 只可由 `overseas_backfill_summary.json` 的 `strict_status` 判斷，不可只看程式 exit code。 |

## 3. 特徵工程的無洩漏規則

歷史回刷取得賽後結果後，**不能**倒過來把賽後資料偽裝成賽前特徵。`audit_overseas_feature_readiness.py` 會初始化特徵 schema 並統計可用歷史，但刻意不為過去場次重新生成 RPR、久休、場地適應、練馬師 G1、負磅、近期前四或 T-15／T-5 落飛特徵。

只有在下列條件同時成立時，完成的海外結果才可供未來 S1/S2 賽前特徵函式使用：

1. `race_status='completed'`；
2. `scheduled_start_utc` 已知；
3. 該完成賽事的開跑時間嚴格早於當前模型產生時間；
4. 馬匹未被標示為退出，且有正式名次。

如果沒有原始賽前排位、來源時間或模型時間，賽後重建任何「賽前」特徵都會造成未來資料洩漏。因此 readiness 報告會標示 `historical_prediction_feature_rebuild=not_run_without_saved_pre_race_card_and_model_timestamp`，這是安全保護而非失敗。

## 4. 建議的 Cron 設定

先在主機確認專案與腳本位置：

```bash
cd /home/ubuntu/hkjc_v10_database
chmod +x run_overseas_backfill_batch.sh
```

使用 `install_daily_archive_cron.sh --install` 安裝每日 03:15 HKT 排程。它會先執行 `auto_archive_results.py`，再執行有上限的海外回刷批次。每月 1 日的既有模型重訓維持於 02:00 HKT；兩者相隔 75 分鐘，且每日包裝器以 `flock` 防止重疊寫入。

```cron
CRON_TZ=Asia/Hong_Kong
15 3 * * * /home/ubuntu/hkjc_v10_database/run_daily_archive_and_overseas_backfill.sh
```

預設 6 個群組／日，267 個尚未完成群組理論上至少需 45 個成功批次；實際時間取決於 HKJC 官方頁是否提供可解析 results，以及其中 partial／source unavailable 的重試情況。

## 5. 首次手動啟動與 discovery

如果目標資料庫尚未有海外 meeting discovery 記錄，先執行一次 discovery。其後日常 Cron 不應重複 discovery，避免不必要來源請求。

```bash
cd /home/ubuntu/hkjc_v10_database
OVERSEAS_BACKFILL_DISCOVER_IF_EMPTY=1 \
OVERSEAS_BACKFILL_BATCH_SIZE=0 \
./run_overseas_backfill_batch.sh
```

首次發現完成後，以正常批量開始 archive：

```bash
OVERSEAS_BACKFILL_BATCH_SIZE=6 ./run_overseas_backfill_batch.sh
```

## 6. 每批驗收

每次執行會建立一個 UTC 時間戳資料夾：

```text
archive/overseas_backfill_batches/<RUN_ID>/
├── run.log
├── run_manifest.json
├── backfill/overseas_backfill_summary.json
└── feature_readiness.json
```

| 檔案／欄位 | 通過條件 |
|---|---|
| `run_manifest.json` | `archive_status=0` 且 `feature_readiness_status=0`。 |
| `backfill_summary.json` | 可查看 `attempts_this_run`、`races_completed`、`races_partial`、`fixture_discovery_issues`。 |
| `strict_status` | 僅在所有已發現 race 有可解析官方列、且 fixture 無覆蓋缺口時才為 `complete`。 |
| `feature_readiness.json` | 追蹤有 `scheduled_start_utc` 的 completed races、可用 rating／going／G1／odds snapshot 覆蓋及 100 場校準門檻。 |

## 7. 目前資料限制與完成門檻

截至目前測試，官方 fixture 可發現 268 個海外群組，但仍有一個舊季 fixture 空白來源缺口，且已完成賽事的 `scheduled_start_utc` 覆蓋尚不足。這代表 archive 可以持續補齊官方賽果，但 S1/S2 的歷史特徵校準及 Kelly 走步交叉驗證仍必須維持 N/A，直至至少 100 場同時具備：完整官方結果、可驗證開跑時間、賽前預測／快照及通過嚴格 Brier field 校驗的樣本。

## References

[1] [HKJC Simulcast Overseas Fixture](https://racing.hkjc.com/en-us/overseas/simulcast_fixture)

[2] [HKJC Overseas Results](https://racing.hkjc.com/en-us/overseas/results)
