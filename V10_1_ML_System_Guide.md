# V10.1 賽馬機器學習後台：操作與更新指南

**版本：V10.1**
**作者：Manus AI**

V10.1 以香港賽馬會公開官方賽果與排位資料為基礎，採用「**馬匹／騎師 ELO + 末段走勢代理 + 跑道偏差 + 班磅變化 + LightGBM**」流程，對每場出賽馬建立場內相對勝出率。系統分為一次性資料庫與模型建置，以及日後的單場預測與月度增量重訓兩部分。

> **重要限制：** 香港賽馬會公開賽果頁包含官方分段時間、沿途走位、完成時間與頭馬距離，但並無可直接下載的**個別馬匹實測最後 400 米時間**。`closing400_proxy` 只根據最後一個沿途走位、名次及頭馬距離建立末段走勢代理，絕不等同儀器量度的馬匹末段分段時間。[1]

## 系統組成

| 元件 | 檔案 | 作用 |
|---|---|---|
| 官方賽果資料庫 | `hkjc_last_season.sqlite` | 保存 2025/26 馬季的賽日、賽事及馬匹出賽紀錄。 |
| 限速更新器 | `hkjc_last_season_etl.py` | 依官方賽期表及單場賽果增量下載；支援指定日期、延遲、冷卻、暫停後續跑。 |
| V10.1 特徵工程 | `build_elo_features.py` | 建立無未來資料的 ELO、近績、末段代理、跑道偏差、班次與負磅特徵。 |
| 模型訓練 | `train_lightgbm.py` | 以時間序列切分訓練 LightGBM、校準機率並輸出 `horse_model.pkl`。 |
| 單場預測 | `predict.py` | 輸出獨贏勝率、模型推導的位置機率、雙市場賠率比較、EV、保守 Kelly 及樣本警示。 |
| 官方排位轉換 | `fetch_hkjc_racecard.py` | 將已公布官方排位轉為模型輸入 JSON。 |
| 裝備回填器 | `enrich_hkjc_equipment.py` | 限速讀取官方馬匹近績的逐場配備欄，回填可稽核的歷史裝備資料。 |
| 月度工作流程 | `monthly_update.py` | 順序執行增量抓取、官方裝備回填、結果清理、特徵重建及模型重訓。 |

## V10.1 新增特徵

| 特徵 | 定義 | 防止誤用的規則 |
|---|---|---|
| `track_bias_pre` | 同一馬場、跑道配置、場地狀況、路程組別與內／中／外檔的**歷史平滑勝率指數**。 | 僅使用該場之前賽果；以 48 匹有效出賽馬為先驗，按樣本可靠度再收縮至中性值 1.0，並限制於 0.75–1.25。它是歷史先驗，並非「C+3 必定內檔有利」的硬規則。 |
| `track_bias_sample_pre` | 上述偏差的有效歷史出賽樣本數。 | 樣本過少會顯示警示，不能當作單一選馬理由。 |
| `class_drop_from_last_pre` | 目前班次級別減去上仗班次級別；正數表示由較高班轉至較低班。 | 新馬及非標準班次採中性處理。 |
| `weight_delta` | 今仗實際負磅減上仗負磅。 | 單獨減磅不必然有利，必須與班次及馬匹能力一併解讀。 |
| `class_weight_interaction_pre` | 班次變化與負磅變化的乘積。 | 讓模型自行學習「降班／升班」與「加磅／減磅」的交互，而非預設方向。 |
| `is_first_time_blinker` | 當場官方配備為 `B1` 或 `BO1` 時為 1。 | 只使用已公布排位表配備；不以事後資料推斷。 |
| `is_equip_added` / `equipment_changed` | 相對同馬上一正式出賽的新增配備／有效配備集合變動。 | 上仗配備未知時均為 0，不把未知誤判為變動。 |
| `trainer_equip_change_roi_pre` | 同馬房近兩年裝備變動馬的平滑勝率相對馬房基準。 | 每列只使用較早賽日資料，並限制在 0.5–1.5；名稱相容但非字面投注 ROI。 |

## 香港獨贏賠率、EV 與凱利公式

香港本地獨贏為**平分彩金彩池**，獨贏／位置／連贏／位置Q 的彩金佔彩池百分比為 82.5%，即彩池層面的扣除比例為 17.5%。[2] 因此，臨場顯示的獨贏賠率會隨彩池變動，而非真正鎖定的單場固定賠率。

V10.1 將輸入的香港賽馬會獨贏賠率 `O` 視為**連本帶利的派彩倍數**：固定賠率產品的官方規則也明確以「下注額 × 賠率 = 派彩」表示。[3] 因此，若使用已顯示的官方獨贏賠率：

```text
市場隱含機率 = 1 / O
每 $1 的期望淨回報（EV）= 模型勝率 p × O − 1
淨賠率 b = O − 1
完整 Kelly 比例 = max(0, (p × O − 1) / (O − 1))
```

**不可在 `p × O − 1` 後再次扣除 17.5%**。原因是顯示的官方派彩倍數本身已反映獨贏彩池的扣除和投注分布；重複扣除會把抽水計算兩次。17.5% 只適合用於從「尚未扣除抽水的假設公平彩池」自行推導理論派彩，而不適用於已提供官方獨贏賠率的 EV 比較。[2]

系統另輸出 `kelly_quarter_fraction_capped`：它為完整 Kelly 的四分之一，且上限為模型研究用途的 5%。該欄位是風險尺度，不是下注指令；EV 正數並不代表必然獲利，尤其在賠率快速變動、樣本不足或出現臨場狀況時。

### 位置機率、位置 EV 與限制

位置市場沿用同一個派彩倍數概念，因此 `place_ev_per_unit = predicted_place_probability × place_market_odds − 1`，並以同一個 Kelly 公式計算 `place_kelly_full_fraction` 與上限 5% 的四分之一 Kelly。香港本地賽中，4 至 6 匹宣布出賽馬設前兩名位置派彩；7 匹或以上設前三名。指定轉播賽則可能在 21 匹或以上設四個位置派彩。[4]

現有 LightGBM 只直接訓練頭馬事件，故 `predicted_place_probability` 是把場內已校準的獨贏強度作 **Plackett-Luce 名次模擬** 所得的代理值，不是獨立訓練或獨立校準的位置模型。系統以固定隨機種子及預設 100,000 次模擬，令相同輸入可重現，並輸出其 Monte Carlo 標準誤。模擬採批次化 NumPy Gumbel-top-k 與 `argpartition`，不會逐次或逐馬使用 Python 迴圈。位置 EV 只宜視為模型與市場的研究性比較，不能視為回報保證。

## 已建置資料與 V10.1 驗證

資料庫涵蓋 2025/26 馬季 **88 個賽日、868 場賽事紀錄與 10,947 筆馬匹出賽紀錄**。取消、宣布無效及撤回狀態會保留作稽核，但不會進入勝率訓練樣本。官方馬匹近績配備回填覆蓋 **10,934 筆**出賽紀錄；V10.1 特徵庫含 **10,381 筆**無未來資料的可訓練列，覆蓋 1,427 匹馬與 41 名騎師。

模型先以 70% 賽日訓練、15% 賽日作調校、最後 15% 未觸碰賽日作時間外測試。新增跑道／班磅特徵在驗證期的場內 Brier score 為 **0.8692**，略低於基本 V10 的 **0.8705**，故納入 V10.1。其後加入小樣本跑道偏差收縮保護並重新訓練，最終未觸碰測試期的結果如下；Brier score 越低，代表場內相對機率與實際頭馬的吻合度越好。

| 指標 | V10.1 時間外測試結果 | 直觀解讀 |
|---|---:|---|
| 首選（Top 1）勝出率 | 18.1% | 在 138 場測試賽事中，模型排名第 1 的馬有約 1/6 勝出；不代表單場保證。 |
| 首三名（Top 3）包含頭馬比率 | 44.9% | 頭馬有約 45% 機會出現在模型頭三名之內。 |
| 場內正規化平均 Brier score | 0.8908 | 低於等機會基準 0.9192，代表校準較「每匹馬一律同機會」為佳。 |
| Row-level ROC-AUC | 0.6813 | 模型對頭馬與非頭馬有一定排序辨識力，但絕非可保證的預測能力。 |

> 上述均為歷史研究回測，不能視為對未來賽果、回報或投注結果的承諾。

## 日常單場預測

所有馬名、騎師與練馬師必須使用香港賽馬會最後公布的中文名稱，才能連接歷史資料。排位表公佈後：

```bash
# 1. 讀取官方排位表
python3 fetch_hkjc_racecard.py \
  --date 2026/09/06 --racecourse ST --race-no 3 \
  --output race_card.json

# 2. 讀取公開獨贏及位置賠率；以排位表馬名核對
python3 fetch_hkjc_live_odds.py \
  --race-card race_card.json \
  --output odds_overlay.json \
  --place-output place_odds_overlay.json \
  --combined-output odds_overlay_combined.json

# 3. 預測及雙市場比較
python3 predict.py \
  --db hkjc_last_season.sqlite \
  --model horse_model.pkl \
  --race-card race_card.json \
  --win-odds-overlay odds_overlay.json \
  --place-odds-overlay place_odds_overlay.json \
  --output-json prediction.json \
  --output-csv prediction.csv
```

`new_race_template.json` 與 `odds_overlay_template.json` 提供輸入格式。未取得最新獨贏或位置賠率時，系統仍會輸出獨贏勝率與位置機率代理，但相應市場的 EV 及 Kelly 會留空；這比採用舊賠率計算更可靠。

### 即時賠率降級規則

`fetch_hkjc_live_odds.py` 對賠率為 `0`、空值、`SCR`／退出馬、頁面結構暫變或網絡逾時均採**降級輸出**：相關馬匹在獨贏／位置 overlay 會保留為 `null`，而 `odds_overlay.meta.json` 的 `status` 會標示為 `degraded` 並列出警示。`predict.py` 會把這些 `null`、`0`、`SCR` 或不可讀取的 overlay 視為沒有市場賠率，故相應 EV 會留空、Kelly 為 0，預測本身仍會繼續產生。使用者應於開跑前查看 metadata 的 `status`、`warnings` 與馬名對應結果。

## 每月更新與重訓

每月最後一個賽日後，執行：

```bash
python3 monthly_update.py \
  --db hkjc_last_season.sqlite \
  --csv hkjc_last_season.csv \
  --end-date 2026-09-30
```

程式會由資料庫最後已記錄賽日的翌日開始抓取，沿用單線程、隨機延遲與定期冷卻。遇到 HTTP 403 或 429 會停止而不規避限制，之後可重跑續接。完成後會先以保守限速重查官方馬匹逐場配備，再自動重建 ELO／跑道／班磅／裝備特徵並重訓 `horse_model.pkl`。裝備回填較耗時，但可中斷後續跑；除非只是本機快速測試，不建議使用 `--skip-equipment`。

## 參考資料

[1] [香港賽馬會：本地賽果](https://racing.hkjc.com/zh-hk/local/information/localresults)

[2] [香港賽馬會：平分彩金本地彩池](https://special.hkjc.com/e-win/zh-HK/betting-info/racing/beginners-guide/local-pools/)

[3] [Hong Kong Jockey Club: Fixed Odds Bet Types](https://special.hkjc.com/e-win/en-US/betting-info/racing/beginners-guide/fixed-odds/)

[4] [香港賽馬會：賽馬投注指南－位置派彩資格](https://is.hkjc.com/AOSBS/help/en/HR_Guide.html)

[5] [香港賽馬會：官方馬匹近績及配備欄示例](https://racing.hkjc.com/zh-hk/local/information/horse?horseid=HK_2025_L083)
