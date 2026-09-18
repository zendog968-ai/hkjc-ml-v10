# HKJC V10 + N6 最新系統狀態

**報告時間：** 2026-09-17 20:17 HKT  
**系統用途：** HKJC 賽馬賽前紙上模擬、資料完整性驗證、模型推論與賽後審計  
**報告性質：** 只讀狀態總覽；本報告沒有修改 N6 核心模型、72 維特徵、模型權重或正式賽果資料庫。

## 1. 國師結論

V10/N6 現時可列為 **Paper Trading Ready（紙上交易就緒）**。賽前排程、資料身份鎖定、T-15/T-5 快照、第三方 DOM 賠率監控、Fractional Kelly 風控、賽後審計及影子資料記錄均已納入正式運行邊界。

系統目前採取 **Fail-Closed** 原則：日期、馬場、場次或馬號配對不一致時，不會用不完整資料計算 EV 或 Kelly。沒有通過 WIN／PLACE 完整配對的場次，會保留研究版或輸出空值，而不是以估算賠率解除安全鎖。

需要特別注意的是，正式 Manifest 目前仍指向上一個已完成的賽日 **2026-09-16 HV**。這不是模型故障，而是下一個賽日尚未完成官方時間核對及原子切換；在切換前，排程器不應被視為已接管下一場賽事。

## 2. 正式 Manifest 與排程狀態

| 項目 | 目前狀態 |
|---|---|
| Manifest | `runtime/pre_race_schedule_current.json` 存在 |
| Manifest 日期 | 2026/09/16 |
| 馬場／場地 | HV／Turf |
| Course | B |
| 場次 | 8 場 |
| 時區 | Asia/Hong_Kong |
| 快照窗口 | T-15、T-5 |
| Manifest SHA-256 | `f726b1075a6877cc020ec461b4df63ec54caa3b9ce72c156e949c08097ecfc1b` |
| T-15/T-5 Scheduler Timer | enabled、active；每分鐘檢查已配置的官方賽程 |
| DOM 賠率監控 Timer | enabled、active；每分鐘執行 DOM 賠率與 1:1 配對檢查 |
| ONCC 晨操 Timer | enabled、active；每日 08:00 HKT 執行 |
| Post-Race Audit Timer | enabled、active；每日 23:45 HKT 執行 |

Post-Race Audit Timer 的實際設定為 `OnCalendar=*-*-* 23:45:00 Asia/Hong_Kong`，並啟用 `Persistent=true`。因此主機錯過觸發時，systemd 仍具備補觸發語義，但審計腳本本身會對日期、賽果及預測資料執行安全檢查。

## 3. 已部署的安全與資料管線

### 3.1 資料身份與 1:1 配對

每場推論必須先通過 Race Card 身份檢查。核心比對包括賽事日期、馬場、場次、馬號及馬名。賠率資料則必須與正式 Race Card 逐匹對齊。任一馬匹缺失、重複、日期錯位或欄位不完整，都會觸發 Fail-Closed。

### 3.2 T-15／T-5 賠率流程

排程器在 T-15 及 T-5 執行快照。當 T-5 賠率工件通過完整性閘門後，管線會觸發 N6 推論重跑、原子覆蓋 Prediction，以及重新生成實戰文字方案。若賠率狀態不是 `complete`，Prediction 會保留研究版，EV 與 Kelly 不會被強行恢復。

離線 R2 fixture 的端到端測試已證明：在 WIN／PLACE 完整且 1:1 對齊時，Prediction、篩選報告及臨場方案可於 30 秒內完成。該測試證明的是本地工件落地後的處理能力，不代表外部網絡抓取必然在 30 秒內完成。

### 3.3 Fractional Kelly 硬上限

`generate_actionable_tips_p0.py` 已使用 0.25x Fractional Kelly 邏輯，並設置不可逾越的 P0 上限：

| 投注類型 | 單場總上限 |
|---|---:|
| WIN | 本金 2% |
| Q／QP 組合 | 本金合計 4% |

公式即使產生更高比例，最終輸出仍會被硬上限攔截，並保留觸發保護的狀態。這個限制優先於任何單場 EV，不會因高賠率或高模型信心而放寬。

### 3.4 影子特徵日誌

`runtime/shadow_features_logger.py` 只在上游 T-5 安全閘門通過後追加資料。日誌包含檔位、負磅、騎師、練馬師、跑法代理值、晨操／試閘欄位、質性覆蓋率、T-5 WIN／PLACE 賠率、N6 機率、EV、Kelly、Track Bias、來源雜湊及安全狀態。

這些資料目前只用於離線研究，不會修改正式 N6 的 72 維向量，也不會直接解除 EV／Kelly 風控。至少需要 5 個未見 HV 賽日的 OOT 樣本，才可評估受限校準層。

## 4. 最近一次完整復盤：2026-09-16 HV

官方 8 場賽果、賽前 Prediction 及前三名資料已完成 1:1 Join。審計報告位置為 `runtime/post_race/2026/09/16/`。

| 指標 | 結果 |
|---|---:|
| 已計分場次 | 8/8 |
| 官方出賽紀錄 | 96 匹 |
| Win Daily Brier | 0.858776 |
| 12 匹均勻分佈基準 | 0.916667 |
| PLACE Brier | 0.183541 |
| 模型 Top-3 命中率 | 75.0% |
| PLACE Top-3 命中率 | 75.0% |

整晚 Win Brier 低於均勻基準，因此不能判定整套模型全晚失效。然而失準集中於特定場次：R6 及 R7 的 Brier 高於均勻基準；R1 及 R8 的模型 Top-3 沒有覆蓋官方三甲。R6 的官方冠軍模型勝率約 8.14%，排序第 7；R7 冠軍排序第 5，顯示高熵場次存在排序不穩定。

HV 當晚官方三甲的跑法分佈為後上 11、前置 9、放頭 4。這是一個需要監控的領域偏移訊號，但單一賽日不足以證明檔位或跑法權重具有因果失效。現階段仍禁止直接改動 N6 72 維特徵。

## 5. T-5 覆蓋狀態與已知歷史問題

2026-09-16 HV 的復盤顯示，R1 的 Prediction 在新 Hook 部署前已產生，因此仍保留研究版；R2 至 R8 均有 T-5 provenance，EV 非空，並切換為「臨場實戰版 · 賠率已鎖定」。這表示已知斷鏈主要是 R1 的部署時序窗口，而不是 R2 至 R8 全晚失效。

目前的正確安全語義如下：

```text
odds_status = complete
+ complete_win_place_pairs = field_size
+ race_card_identity_1_to_1 = true
+ source_time_valid = true
=> 才可計算 EV／Kelly 及產出臨場版
```

任何條件不成立時，系統應輸出 `ev=null` 或 `kelly_stake=0`，並保留「賽前研究版 · 賠率未就緒」。

## 6. 晨操及質性資料的安全定位

ONCC／其他質性資料目前屬候選擴充層。缺失資料必須保留為 `null` 並帶有 `missing_flag`、`source_quality`、`asof_hkt`、`qualitative_coverage` 及版本資訊，不可把缺失值直接填成 0 或中性分數。

正式 N6 目前維持 baseline。候選層可以並列產生質性 shadow probability，但不得直接改變正式 Prediction、EV 或 Kelly。只有在多個未見賽日中證明低概率組假正 EV 下降，且整體 Brier、Log Loss、ECE 與校準斜率沒有惡化後，才可考慮小範圍 canary。

## 7. 目前風險清單

| 風險 | 嚴重度 | 目前處置 |
|---|---|---|
| 下一賽日 Manifest 尚未切換 | 高 | 等官方時間核對；未切換前不接管新賽日 |
| HV 樣本數不足以證明 Domain Shift | 中 | 累積至少 5 個未見 HV 賽日，保持 shadow |
| 外部賠率頁面或 DOM 結構變更 | 高 | 1:1 配對失敗即 Fail-Closed |
| 晨操資料覆蓋率偏低 | 中 | 缺失保留 null，不直接餵入 N6 |
| 高熵場次低概率排序失準 | 中 | 監控 entropy、Top-3、低概率假正 EV 率；不單日改權重 |
| 審計資料 Join 錯位 | 高 | 日期、馬場、場次、馬號及馬名共同核對 |

## 8. 下一賽日的自動驗收門檻

下一個正式賽日開始前，應按以下順序完成：

1. 以官方 Fixture 核對日期、馬場、場次及開跑時間。
2. 在候選 Manifest 執行 schema、時區、時間排序及場次數量檢查。
3. 以原子操作切換正式 Manifest，並記錄 SHA-256。
4. 讓 Race Card Fetcher 逐場產出 JSON，驗證馬號、馬名、負磅、騎師、練馬師及環境欄位。
5. 執行 N6 baseline 推論，確認每場 Prediction 的欄位數、機率和為 1，以及不含錯誤馬號。
6. 由 Scheduler 自動接管 T-15。不要以手動補跑替代正式排程驗證。
7. 由 T-5 賠率閘門決定是否進入臨場版；不完整資料必須維持研究版。
8. 每場完成後由 shadow logger 追加可追溯資料，但不得阻塞正式推論。
9. 23:45 HKT 由 Post-Race Audit Timer 執行結果抓取、Brier、PLACE、Top-3 及 Join 審計。
10. 賽後只以跨日 OOT 結果評估校準，不以單場命中或單日盈利直接改動 N6。

## 9. 重要正式工件

- `pre_race_scheduler.py`：T-15/T-5 排程、安全閘門及 Prediction Hook。
- `generate_actionable_tips_p0.py`：P0 Fractional Kelly 上限及研究版／臨場版輸出。
- `post_race_auto_audit.py`：賽果、Brier 及診斷的自動化包裝器。
- `runtime/shadow_features_logger.py`：T-5 後影子特徵追加器。
- `runtime/p0_safety_gate.py`：日期、馬場、場次及身份完整性檢查。
- `runtime/pre_race_schedule_current.json`：目前正式賽程 Manifest。
- `runtime/post_race/2026/09/16/brier_audit.json`：最近一次 Brier 審計。
- `runtime/post_race/2026/09/16/model_diagnostics.json`：最近一次逐場診斷。
- `runtime/post_race/2026/09/16/hv_domain_shift_analysis.json`：HV 跑法及檔位觀察。
- `runtime/post_race/2026/09/16/test_t5_hook_offline_e2e.py`：T-5 離線端到端回歸測試。

## 10. 版本追蹤雜湊

| 工件 | SHA-256 |
|---|---|
| `runtime/pre_race_schedule_current.json` | `f726b1075a6877cc020ec461b4df63ec54caa3b9ce72c156e949c08097ecfc1b` |
| `generate_actionable_tips_p0.py` | `ef82502c49f04ac77d827635fa41a4fd13ef01370b772389446276400e6554a3` |
| `pre_race_scheduler.py` | `9e69f09b8a7019fbecb4c91ba1b83b41696f9e026d9ec0b1b8c095618f967f22` |
| `post_race_auto_audit.py` | `a48f08137f7111044634a94eb40ae51a8c80d8b69ffd7ca78edf885a9b66bc6b` |
| `runtime/shadow_features_logger.py` | `1173fa8a753ae81eae796f5ba21d5bfe847fb57b83c320ba300c88a070a1bee6` |

## 11. 最終狀態

**正式 N6 核心：保持不變。**  
**EV／Kelly：只有在賠率完整及身份 1:1 通過時解除。**  
**風控：WIN 2%、Q／QP 合計 4% 硬上限有效。**  
**賽後審計：23:45 HKT Timer 已啟用並在等待下一次觸發。**  
**影子資料：持續追加，未進入正式模型。**  
**下一個必要動作：完成下一賽日官方 Manifest 核對及原子切換。**

> 本系統報告描述的是工程狀態，不是博彩收益保證或個人化投注建議。賽馬投注可能導致全部本金損失。

## References

[1]: https://racing.hkjc.com/ "Hong Kong Jockey Club Racing Information"

[2]: https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html "systemd.timer Manual"
