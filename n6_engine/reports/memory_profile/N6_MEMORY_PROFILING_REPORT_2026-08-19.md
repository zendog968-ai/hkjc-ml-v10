# N6 Neural Calculation Engine 記憶體 Root-Cause Profiling 報告

**日期：** 2026-08-19（HKT）
**範圍：** N6 兩個 Uvicorn worker 的 loopback-only 歷史推論路徑；不修改 V10.2 正式勝率、EV、Kelly、資料庫或公開網路設定。
**判定：** 已修正的 **SQLite 連線／cursor 生命週期缺口** 是原先 warm-up 匿名記憶體與 SQLite FD 累積的主要可驗證來源；修正後在相同 20 分鐘真實歷史負載下，匿名記憶體增量由 **35.39 MiB** 降至 **4.57 MiB**，且 SQLite FD 在全程維持 **0**。

> 本報告所述「改善」是對 N6 sidecar 的資源穩定性改善；它不代表、也不嘗試令 N6 取代 V10.2 的 LightGBM + CatBoost 正式機率、EV 或 Kelly。

## 1. 問題與調查範圍

先前的三小時耐久測試在 10,800 次歷史推論中錄得零失敗、零服務重啟，並在尾段呈現平台；不過 N6 cgroup 記憶體在初段增加約 43.92 MiB。因此，本輪針對 **實際 historical inference API** 而非合成資料，量測 worker PSS、cgroup 匿名／檔案頁、SQLite FD、socket FD、延遲與 V10 SQLite 雜湊。

所有 20 分鐘對照均使用相同條件：兩個發送端併發、每秒一個循環、每 5 秒量測一次，以及 385 場已保存的真實歷史賽事輪替。每輪均完成 **2,400 次** loopback `POST /v1/inference/historical/...` 請求。

| 控制項 | 措施 | 結果 |
|---|---|---|
| N6 網路邊界 | 僅呼叫 `127.0.0.1:5001` | 保持 loopback-only |
| V10 資料邊界 | `mode=ro&immutable=1` 與 `PRAGMA query_only=ON` | 無寫入路徑 |
| 負載優先級 | 暫態剖析單元以 `Nice=10` 執行 | 不搶佔日常服務 |
| 服務設定 | 維持兩個 worker、每 worker 最多兩個 inference | 未變更 worker／安全硬化 |
| 資料庫完整性 | 測試前後 SHA-256 比對 | `5f638f…bb9d6dd0` 不變 |

## 2. 根因與修正

Python 官方文件明確指出，`sqlite3.Connection` 的 `with` context manager 只處理交易提交／回滾，**不會關閉 connection**；如需要 closing context manager，應使用 `contextlib.closing()`。[1]  原 N6 的歷史推論函式 `load_historical_race()` 使用 `with connect_v10_read_only() as connection:`，再交由 `pandas.read_sql_query()` 建立內部 cursor。於高頻 historical API 負載下，這讓 connection 與 cursor 的釋放依賴較晚的物件生命週期，實測可見資料庫 FD 在無持續使用者流量後仍殘留。

修正在 `n6/feature_engineering.py` 完成，且只改變資源釋放時點，沒有改變 SQL、特徵值、模型、輸出機率或資料庫存取權限。所有四個唯讀 connection 作用域均改用 `closing(connect_v10_read_only())`；另外新增 `_read_sql_frame()`，以 `connection.execute()` 取得 cursor、`fetchall()` 後在 `finally` 明確 `cursor.close()`，再建立完全相同欄名與列內容的 `DataFrame`。其中 historical inference 路徑已改用此 helper。

| 檢查 | 修正前 | 修正後 | 判定 |
|---|---:|---:|---|
| 單一進程 100 次重複 historical read 後 SQLite FD | 未保證為 0 | 最小 0、最大 0 | 通過 |
| 兩 worker 重啟後閒置 SQLite FD | 可見殘留 | 0 | 通過 |
| 最終 20 分鐘 profiler SQLite FD 範圍 | 0–130 | 0–0 | 通過 |
| 385 場歷史回歸壓力測試後 SQLite FD | 不適用 | 所有子程序 0 | 通過 |

## 3. 受控前後比較

下表以修正前完整 20 分鐘剖析，對照套用 connection + cursor 明確關閉後的完整 20 分鐘剖析。數字是同一測量器、同一資料來源與同一並發條件下所得；因此可用於資源行為比較，但不應把單次延遲差異解讀為模型預測能力變動。

| 指標 | 修正前 | 修正後 | 差異（修正後－修正前） |
|---|---:|---:|---:|
| 成功請求 | 2,400 / 2,400 | 2,400 / 2,400 | 0 |
| cgroup 總記憶體增量 | 35.60 MiB | 4.23 MiB | -31.37 MiB |
| cgroup 匿名記憶體增量 | 35.39 MiB | 4.57 MiB | -30.82 MiB |
| 匿名記憶體增量降幅 | — | 87.08% | — |
| 檔案頁增量 | 0.00 MiB | 0.00 MiB | 0.00 MiB |
| SQLite FD 淨變動 | +46 | 0 | -46 |
| Socket FD 淨變動 | 0 | 0 | 0 |
| 平均延遲 | 78.08 ms | 81.34 ms | +3.26 ms |
| P95 延遲 | 109.44 ms | 121.04 ms | +11.60 ms |
| 最大延遲 | 367.20 ms | 343.62 ms | -23.58 ms |

修正前的匿名記憶體斜率由首五分鐘的 256.28 MiB/h 降至最後五分鐘的 53.29 MiB/h，但同時 SQLite FD 範圍曾達 0–130。修正後，四個五分鐘區段分別為 39.43、8.36、4.40 與 **3.19 MiB/h**，SQLite FD 全程均為零。這個後段斜率及總量屬合理的 Python／pandas／PyTorch allocator warm-up 級別，沒有顯示持續性累積。

| 時段 | 修正前匿名記憶體斜率 | 修正後匿名記憶體斜率 | 修正前 SQLite FD | 修正後 SQLite FD |
|---|---:|---:|---:|---:|
| 0–5 分鐘 | 256.28 MiB/h | 39.43 MiB/h | 0–70 | 0–0 |
| 5–10 分鐘 | 89.24 MiB/h | 8.36 MiB/h | 23–87 | 0–0 |
| 10–15 分鐘 | 25.22 MiB/h | 4.40 MiB/h | 54–108 | 0–0 |
| 15–20 分鐘 | 53.29 MiB/h | 3.19 MiB/h | 15–130 | 0–0 |

## 4. 最終回歸與安全驗證

最終修正後，另以 385 場唯一歷史賽事、單次 pass、四個客戶端進行低優先權回歸壓力測試。該測試驗證每場的機率和、排名完整性、回應馬匹數、重複輸出一致性，以及 V10 SQLite 雜湊與 mtime 不變。

| 驗證項目 | 結果 |
|---|---:|
| 成功／失敗請求 | 385 / 0 |
| 牆鐘時間／吞吐 | 13.61 秒／28.29 req/s |
| P95／最大延遲 | 241.04 ms／364.09 ms |
| 場內機率守恆 | 通過；最大誤差 `3.0e-06` |
| 名次、馬匹數、輸出確定性 | 全部通過 |
| V10 SQLite SHA-256 | 前後相同：`5f638f…bb9d6dd0` |
| N6 journal error／restart | 0／0 |
| 5001 與 8000 listener | 均維持 `127.0.0.1`，無公開端口變更 |

因此，本次修正符合 N6 作為 failure-tolerant、唯讀 sidecar 的部署契約；V10.2 的正式結果檔與計算邏輯未被接觸。

## 5. 結論與風險判定

根因不是 PyTorch oneDNN 或檔案頁快取：檔案頁與 socket FD 在所有受控測試中均無增長，而修正 SQLite connection + cursor 生命週期後，SQLite FD 由最高 130 降至零，同時匿名記憶體增量減少 30.82 MiB。該前後對照足以把 **歷史 SQLite 讀取的延後釋放** 判定為本次初段異常增長的主要可修正因素。

剩餘的 4.57 MiB 匿名記憶體增量在 20 分鐘內已明顯降速，且 3 小時既有耐久測試尾段曾平台化。它目前應列作 **正常 warm-up／allocator 保留，監控但不重啟**，而不是已確認的記憶體洩漏。20 分鐘測試不能數學上排除所有長週期問題，因此以下門檻應納入日常監控。

## 6. 建議監控門檻與處置

| 優先級 | 量測條件 | 門檻 | 處置 |
|---|---|---|---|
| P1 | 閒置至少 15 秒後的 N6 worker SQLite FD | 任一 worker `> 0`，連續兩次 | 立即保存 `/proc/<pid>/fd` 清單與 journal；停止擴大負載，檢查是否有新讀取路徑繞過 `closing()`。 |
| P1 | 唯讀隔離 | V10 SQLite SHA-256 或 mtime 有變化 | 立即停止 N6，保留證據並調查；不得重跑會覆蓋原始檔的工作。 |
| P1 | 服務可靠性 | 任何 HTTP 5xx、worker restart、機率和不在 `1 ± 1e-5` | 保留請求樣本與 journal；先恢復 N6 sidecar 可用性，不更動 V10.2 正式輸出。 |
| P2 | steady-state 匿名記憶體 | 略過服務啟動後首 30 分鐘；其後連續兩個 15 分鐘窗口皆 `> 30 MiB/h` | 執行本 profiler，分開記錄 PSS anon／file、SQLite FD 與 socket FD；尚未確認前不自動重啟。 |
| P2 | cgroup 總記憶體 | `> 1.20 GiB` 且持續 30 分鐘 | 在無臨場賽前任務的時段，先做 health、SHA-256、FD 快照；確認異常後才安排受控 N6 restart。 |
| P3 | 端點效能 | 10 分鐘滾動 P95 `> 300 ms` 或失敗率 `> 0.5%` | 檢查 CPU、排隊與 worker gate；以既有壓力測試重新驗證後才調整 worker／併發設定。 |

## 7. 相關產物與可重現性

| 產物 | 用途 |
|---|---|
| `n6/feature_engineering.py` | 已套用明確 connection／cursor 關閉修正 |
| `verify_sqlite_fd_lifecycle.py` | 100 次重複 historical read 的 FD 回歸測試 |
| `profile_n6_memory_warmup.py` | 高頻 worker／cgroup／FD profiler |
| `reports/memory_profile/n6_memory_profile_warmup_20m.json` | 修正前完整 20 分鐘原始量測 |
| `reports/memory_profile/n6_memory_profile_after_cursor_close_20m.json` | 修正後完整 20 分鐘原始量測 |
| `reports/memory_profile/compare_n6_memory_profiles.py` | 可重現前後比較程式 |
| `reports/memory_profile/n6_memory_profile_final_comparison.txt` | 最終比較輸出 |
| `reports/stress/final_sqlite_cleanup_regression/` | 最終 385 場回歸壓力測試報告 |

## References

[1]: https://docs.python.org/3/library/sqlite3.html#how-to-use-the-connection-context-manager "Python sqlite3 — How to use the connection context manager"
