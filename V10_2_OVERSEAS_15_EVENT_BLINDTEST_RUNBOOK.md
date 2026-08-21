# V10.2 海外 RPR／TS 15 場探索性盲測封存手冊

## 目的與範圍

本流程只為海外 S1／S2 的公開 Racing Post RPR／TS 研究代理建立**未來15場**的不可變賽前決策與獨立賽後結算資料。其目的，是累積可作探索性回測的時間序列樣本；它不是 V10.2 的機率替代層，亦不會啟用 N6 或改寫香港主資料庫。

> **研究邊界：**所有海外機率、EV及任何相關欄位仍屬未校準研究代理。封存的作用是保留可日後檢驗的原始決策，而不是產生投注指令。

## 啟用條件

計時器已經每分鐘執行一次，但在沒有 `runtime/overseas_blindtest/active_manifest.json` 時保持惰性，僅輸出 `awaiting_official_manifest`。因此，系統不會自行猜測未來賽程、來源網址、開跑時間或位置彩派彩數。

每個事件加入正式 manifest 前，必須由已核實的 HKJC 海外賽程、HKJC Win／Place 頁、HKJC結果頁，以及公開 Racing Post／At The Races 賽卡確認。可版本控制的格式範本位於：

```text
runtime/overseas_blindtest/active_manifest.example.json
```

真正的 `active_manifest.json` 是執行期控制檔，受 `.gitignore` 保護，絕不提交。

## 賽前封存契約

只有下表所有條件均通過，系統才會建立一份一次寫入、模式 `0440` 的決策檔。

| 閘門 | 要求 | 失敗處理 |
|---|---|---|
| 時間 | `captured_at_utc < scheduled_start_utc`，且只在開跑前15分鐘內觸發。 | 不建立決策。 |
| 來源 | Racing Post、At The Races、HKJC市場原文檔案均存在並記錄 SHA-256。 | 不建立決策。 |
| 身份 | 公開深度名單與HKJC市場為全場一對一匹配。 | 不建立決策。 |
| 機率 | 研究 Win 機率為有限值、每匹唯一、合計精確為1。 | 不建立決策。 |
| 海外隔離 | 事件必須為S1或S2，並有 `N6=disabled_non_hk`。 | 不建立決策。 |
| 上限 | Study內已封存或結算事件少於15場。 | 第16場起回報 `cap_reached`。 |

封存的決策包含：來源URL、來源雜湊、賽事與開跑UTC時間、研究代理版本、特徵可用狀態、全場排名與未校準Win／Place機率、HKJC快照賠率及身份匹配結果。決策檔採用 `O_EXCL` 一次建立；已有同一 event key 時只回報 `already_sealed`，不會覆寫。

## 賽後官方結算

開跑後至少10分鐘，計時器只對已封存事件讀取其 manifest 指定的公開 HKJC 結果頁。結算前會再次比較賽前封存馬號與正式結果馬號；不相等、名次不完整或官方頁不可用時，事件標示 `invalid`／`result_unavailable`，絕不自動補造結果。

通過的官方結果寫入獨立的 `overseas_blindtest_official.sqlite` 及 `archive/overseas_deep_backtest/results/`。日後嚴格回測只讀取下列兩個封存目錄：

```text
archive/overseas_deep_backtest/decisions/
archive/overseas_deep_backtest/results/
```

## 執行與可觀察性

| 項目 | 位置／指令 |
|---|---|
| 計時器 | `hkjc-overseas-blindtest.timer`，每分鐘觸發。 |
| 服務 | `hkjc-overseas-blindtest.service`，以 `ubuntu` 執行。 |
| 主要狀態 | `runtime/overseas_blindtest/status.json`。 |
| 應用鎖 | `runtime/overseas_blindtest/overseas_blindtest.lock`。 |
| 主機鎖 | `runtime/overseas_blindtest/host.lock`。 |
| 系統日誌 | `sudo journalctl -u hkjc-overseas-blindtest.service -n 100 --no-pager`。 |
| 計時器狀態 | `sudo systemctl status hkjc-overseas-blindtest.timer --no-pager`。 |

## 安全不變量

本流程沒有匯入 `horse_model.pkl`、`hkjc_last_season.sqlite` 或 N6 API。海外深度資料仍沿用獨立 `overseas_deep_racing.sqlite`；賽後官方結果則只寫入新建的 `overseas_blindtest_official.sqlite`。`post_race_audit.py` 已加上時間門檻，只會讀取明確早於 `scheduled_start_utc` 的海外賽前預測，避免賽後生成項目被誤選。

## 模擬驗證

在正式啟用前，管線已以15場模擬事件驗證：15份決策成功封存、第16場被硬性阻擋、重複事件被拒絕覆寫、結果檔能一次建立，並且 V10 主SQLite與模型 SHA-256 在前後完全相同。模擬檔案位於 `reports/overseas_deep/OVERSEAS_BLINDTEST_PIPELINE_SIMULATION_20260821T073327Z/`，屬執行期測試證據，不納入版本控制。
