# V10.2 每日 03:15 HKT 全賽果歸檔與海外回刷部署

本部署以單一 Cron 入口執行兩個有序工作：先以 `auto_archive_results.py` 將當日已公布的香港本地及海外官方賽果入庫並執行覆盤分流，再以有限批量補處理歷史海外 S1/S2 缺口。流程不會自動投注，也不會以非官方資料補填缺口。

## 部署前條件

| 項目 | 要求 |
|---|---|
| 專案路徑 | `/home/ubuntu/hkjc_v10_database`；若不同，先修改兩個腳本中的 `ROOT_DIR` 或在正確路徑執行。 |
| 執行帳號 | 對專案、SQLite、`archive/` 與 `runtime/` 具讀寫權限的 Linux 使用者。 |
| 系統工具 | `bash`、`python3`、`flock` 及 `crontab`。Ubuntu/Debian 通常可用 `sudo apt-get install -y cron util-linux` 補齊。 |
| 官方資料保護 | 主機需可訪問 HKJC 公開頁面；如遇 403、429、CAPTCHA 或空來源，工具會留存 partial，不會繞過或偽造賽果。 |

## 一次性部署指令

請在 Linux 主機以部署 V10.2 的帳號執行。第一段只會更新程式與設定權限，第二段會先預覽，最後才寫入 crontab。

```bash
cd /home/ubuntu/hkjc_v10_database
git pull --ff-only origin main
chmod +x run_daily_archive_and_overseas_backfill.sh install_daily_archive_cron.sh run_overseas_backfill_batch.sh
./install_daily_archive_cron.sh --show
./install_daily_archive_cron.sh --install
crontab -l
```

安裝器只管理以下標記區塊；重複執行 `--install` 會更新該區塊，不會重複新增，也不會移除其他 crontab 工作。

```cron
# BEGIN V10.2 DAILY ARCHIVE AND OVERSEAS BACKFILL
CRON_TZ=Asia/Hong_Kong
15 3 * * * /home/ubuntu/hkjc_v10_database/run_daily_archive_and_overseas_backfill.sh
# END V10.2 DAILY ARCHIVE AND OVERSEAS BACKFILL
```

## 排程行為與防重疊

每日 03:15 HKT，`run_daily_archive_and_overseas_backfill.sh` 使用 `runtime/daily_archive_and_backfill.lock` 建立整體 `flock`。已有工作仍在運行時，新一輪會安全退出，不會同時寫入 SQLite。此工作與既有每月 1 日 02:00 HKT 模型重訓相隔 75 分鐘。

工作順序如下：

1. 以香港日期呼叫 `auto_archive_results.py`，保存本地與海外官方結果、原始來源及賽後覆盤分流。
2. 啟動最多 6 個未完成海外群組的回刷；回刷器另有自己的鎖、3–6 秒請求間隔和每 20 次請求 60 秒冷卻。
3. 輸出特徵資料可用性稽核，但不會賽後重建歷史預測特徵，以避免未來資料洩漏。

## 驗證與監察

排程安裝後可立即確認內容與日誌位置：

```bash
crontab -l
tail -n 100 /home/ubuntu/hkjc_v10_database/archive/daily_automation_logs/$(TZ=Asia/Hong_Kong date +%F).log
find /home/ubuntu/hkjc_v10_database/archive/overseas_backfill_batches -maxdepth 2 -name run_manifest.json -print | tail
```

每次回刷批次的 `archive/overseas_backfill_batches/<RUN_ID>/backfill/overseas_backfill_summary.json` 中，只有 `strict_status='complete'` 才代表已發現資料覆蓋完整。程式回傳成功或 HTTP 成功不等於所有官方賽果已可解析。

## 暫停或移除

若要暫停每日工作，先執行 `crontab -e`，刪除或註解上述標記區塊。不要刪除每月模型重訓的既有工作。若只想暫停而保留內容，將排程行首加上 `#` 即可。
