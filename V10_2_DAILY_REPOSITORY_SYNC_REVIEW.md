# V10.2 每日儲存庫同步與程式審核

**版本：** V10.2 Advanced Feature & Ensemble Edition
**排程：** 每日 04:30 HKT
**目的：** 在不覆蓋本機研究工作的前提下，將 `hkjc-ml-v10/main` 安全同步至已部署的 Linux 主機，並產出可稽核的程式品質報告。

## 設計原則

此流程只容許**乾淨工作區的 fast-forward 同步**。它不會自動 `commit`、`push`、`stash`、`reset`、`merge` 或 `rebase`。若本機存在未提交或未追蹤檔案、本機提交領先遠端、分支分歧，或每日官方歸檔／海外回刷仍持有鎖定，流程會安全停止並留下 `BLOCKED` 報告。

> 同步保護的目標是保護主機上的模型、SQLite 資料庫、營運筆記與尚未審核的研究，而不是強制令本機工作區與遠端一致。

## 每日審核範圍

| 檢查 | 判定規則 | 失敗處理 |
|---|---|---|
| Git 同步狀態 | 僅容許 `main` fast-forward 至 `origin/main`。 | 產生 `BLOCKED` 報告；不更改分支。 |
| 官方歸檔互斥 | 讀取 `runtime/daily_archive_and_backfill.lock`。 | 日常資料歸檔正在運行時不更新程式碼。 |
| Python 語法 | 對所有已追蹤的 `*.py` 執行 `py_compile`。 | 報告 `REVIEW_FAILED`。 |
| 格式完整性 | 執行 `git diff --check HEAD`。 | 報告 `REVIEW_FAILED`。 |
| 明顯憑證掃描 | 搜尋私鑰、AWS／GitHub／Slack 形式 token 及明文 Telegram bot token。 | 報告 `REVIEW_FAILED`；不顯示完整憑證值。 |
| 核心契約測試 | 執行 S1/S2 特徵與海外 archive／覆盤測試。 | 報告 `REVIEW_FAILED`。 |

這是**確定性程式檢查**，不是新的模型回訓、回測或投注決策流程。它不會抓取新的 HKJC 網頁資料、不會傳送訊息，亦不會下注。

## 部署

在已存放 V10.2 專案的 Linux 主機執行：

```bash
cd /home/ubuntu/hkjc_v10_database
git pull --ff-only origin main
chmod +x run_daily_repo_sync_review.sh install_daily_repo_sync_review_cron.sh
./install_daily_repo_sync_review_cron.sh --show
./install_daily_repo_sync_review_cron.sh --install
crontab -l
```

安裝器只替換由 `BEGIN HKJC_V10_DAILY_REPO_SYNC_REVIEW` 與 `END HKJC_V10_DAILY_REPO_SYNC_REVIEW` 標記包圍的區段，不會刪除既有的月度重訓或每日資料歸檔 Cron。

## 日誌、報告與驗收

日誌保存於：

```text
archive/daily_repo_review_logs/YYYY-MM-DD.log
```

每日 Markdown 報告保存於：

```text
archive/daily_repo_review_reports/YYYY-MM-DD.md
```

手動驗證一次：

```bash
cd /home/ubuntu/hkjc_v10_database
./run_daily_repo_sync_review.sh
cat "archive/daily_repo_review_reports/$(TZ=Asia/Hong_Kong date +%F).md"
```

若狀態為 `OK`，代表同步與所有既定檢查完成。若狀態為 `BLOCKED`，應先處理報告指出的未提交內容、分支分歧或等待日常資料歸檔完成。若狀態為 `REVIEW_FAILED`，應依檢查項目修正程式或測試，再手動重跑；不要以自動 `reset` 取代人工審核。

## 停用或調整

如需暫停每日任務，先執行 `crontab -l`，再刪除整個標記區段。若只需修改時間，保留標記並將 `30 4 * * *` 改為所需的 HKT 時間。每月 1 日的 02:00 HKT 重訓與每日 03:15 HKT 歸檔／海外回刷不應被覆蓋或移除。
