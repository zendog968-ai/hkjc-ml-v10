# 香港賽馬上季資料庫與 V10 歷史勝率分析模組

**資料季別：2025/26 香港馬季**

本專案從香港賽馬會的公開官方賽果頁整理本地賽事紀錄，建立可重跑的 SQLite／CSV 資料庫，並提供一個透明、可檢視特徵的 V10 歷史勝率模型。官方單場賽果頁載有全體出賽馬的名次、檔位、實際負磅、頭馬距離、完成時間、騎師、練馬師與獨贏賠率等欄位；本專案僅使用這些公開欄位作研究用途。[1]

> **重要說明：** 此工具所輸出的「預估勝率」是根據輸入名單內各馬的相對模型分數正規化而成，並非保證結果或投注指示。樣本偏少、馬匹轉倉、休後狀態、臨場場地、騎師更換、步速與即時市場資訊均可能導致偏差。請以負責任博彩為原則，且勿將模型輸出視為必然回報。

## 專案內容

| 檔案 | 用途 |
|---|---|
| `hkjc_last_season_etl.py` | 單一工作執行緒、限速、可續跑的官方賽果下載與清洗程式。 |
| `hkjc_last_season.sqlite` | SQLite 歷史資料庫；可用任何 SQL 工具查詢。 |
| `hkjc_last_season.csv` | 與資料庫同步輸出的扁平化馬匹出賽紀錄。 |
| `v10_win_probability.py` | 讀取新賽事 JSON，輸出所有參賽馬的預估勝率、冷熱指數與特徵審計。 |
| `sample_race_card.json` | 輸入 JSON 格式範例；僅用於功能驗證，非即時賽事建議。 |
| `sample_prediction.csv` / `sample_prediction.json` | 模組輸出格式樣本。 |
| `source_validation.md` | 官方來源、欄位映射與限速規則的核實紀錄。 |

## 資料庫結構

資料庫以三張核心表儲存資料。`meetings` 代表賽日，`races` 代表場次，`starters` 則為每匹馬每次出賽的一筆紀錄。`races.race_status` 會將官方宣布取消的場次標示為 `cancelled`，將宣布無效的場次標示為 `void`；兩者均不會進入勝率樣本，避免將退款場誤當作正常賽果。官方賽日總覽頁可用以核實每個賽日實際開跑場數及特殊場次。[2]

| 表格 | 主鍵 | 主要欄位 |
|---|---|---|
| `meetings` | `race_date, racecourse` | 賽日、馬場、官方賽期表場數、來源網址。 |
| `races` | `race_date, racecourse, race_no` | 場名、班次、路程、場地、跑道欄位、場地狀況、頭馬完成時間、場次狀態。 |
| `starters` | `race_date, racecourse, race_no, horse_name` | 馬名、馬匹編號、名次、騎師、練馬師、負磅、檔位、頭馬距離、完成時間、獨贏賠率。 |

### CSV 欄位

`hkjc_last_season.csv` 一行代表一次出賽紀錄。最重要的欄位如下。

| 欄位 | 說明 |
|---|---|
| `race_date`, `racecourse`, `race_no`, `race_id` | 賽日、馬場（`ST`／`HV`）、場次與唯一賽事鍵。 |
| `race_class`, `distance_m`, `surface`, `course_config`, `going` | 賽事條件；供同程同場與檔位分析使用。 |
| `horse_name`, `horse_code`, `horse_no` | 馬匹名稱、馬匹編號及當場馬號。 |
| `finish_pos`, `finish_time`, `margin_lengths` | 賽果、完成時間與換算後頭馬距離。原始文字保留在 `margin_text`。 |
| `draw`, `weight_lbs`, `declared_weight_kg` | 檔位、實際負磅及排位體重。 |
| `jockey`, `trainer`, `win_odds` | 騎師、練馬師與官方獨贏賠率。 |
| `race_status` | `completed`、`cancelled` 或 `void`。分析模組只使用 `completed` 場次。 |

## 資料抓取與續跑

下載器先讀取逐月官方賽期表，取得賽日與馬場，再以官方「所有場次賽果」核實實際場數，最後抓取每一場完整賽果。2025/26 馬季設有 88 個賽日；程式將賽期固定為 2025-09-07 至 2026-07-15。[3]

抓取器採取以下保守策略：**單一工作執行緒、請求間隨機延遲、每固定請求數額外冷卻、HTTP 429／403 即停止、網絡與暫時性伺服器錯誤有限重試，以及每完成一場即寫入資料庫。** 因此，如工作中斷，只須以相同指令重跑，已完成場次會被跳過。

```bash
cd /home/ubuntu/hkjc_v10_database
python3 hkjc_last_season_etl.py \
  --db hkjc_last_season.sqlite \
  --csv hkjc_last_season.csv \
  --delay-min 1.5 --delay-max 2.0 \
  --cooldown-every 30 --cooldown-seconds 15
```

若官方網站回傳 429 或 403，程式會停止而不會繞過存取控制。待一段時間後，以相同指令續跑即可。可先核對賽期表解析結果：

```bash
python3 hkjc_last_season_etl.py --discover-only
```

## V10 勝率算法

V10 將每匹參賽馬的歷史資料轉成可審計特徵，再於**同一場輸入名單內**透過溫度化 softmax 正規化為合計 100% 的相對勝率。設計目標是避免只憑單一近績或單一騎師名氣選馬，並保留每個分數的來源。

| 維度 | 量化方式 | 作用 |
|---|---|---|
| 同程同場勝率 | 相同馬場、路程及場地的勝出率；以全體平均勝率作貝葉斯平滑。 | 反映馬匹於相近條件下的實證適性，避免小樣本 100% 勝率誤導。 |
| 近幾仗名次／馬位差 | 回望最近 6 場，以遞減權重計算名次分與頭馬距離分。 | 較近期表現有較高權重，差距過大會拉低分數。 |
| 檔位利弊 | 在相同馬場、路程、場地內，以內／中／外三個檔位帶的歷史勝率平滑計算。 | 將不同跑道與路程的檔位偏差轉為場景化分數。 |
| 負磅變化 | 比較本場實際負磅與該馬最近一仗負磅；變輕為正向、變重為負向，並設上下限。 | 降低極端磅差造成過度放大的風險。 |
| 騎師／練馬師勝率 | 對全季騎師及練馬師勝出率作平滑，再計算相對平均的對數比率。 | 將騎練長期效率納入，但不讓少量出賽產生不合理高分。 |

模型保留 `feature_audit` JSON，當中包括每一匹馬的樣本數、平滑後勝率、近績分、檔位分、磅位變化、騎師與練馬師勝率及原始分數。任何 `same_condition_starts < 2` 或近績樣本過少的馬匹，都會在 `caution` 欄標記，**不應視作可作重注單膽的充分證據**。

原始分數會以 **temperature = 2.0** 進行保守正規化，避免少量同程同場勝仗造成極端機率。專案另附 `backtest_v10.py`；其以每一歷史場次前的資料作推算，杜絕使用同日或後續賽果。最近 50 場的留後切分測試中，模型首選勝出率為 18%，首三名包含頭馬比率為 38%；機率 Brier score 為 0.913，低於等機會基準的 0.918。這只是有限樣本的研究性驗證，不能推論保證回報。

### 冷熱指數

冷熱指數計算為：

```text
模型冷熱指數 = 100 × 預估勝率 ÷ (1 ÷ 場內馬匹數)
```

因此，100 代表與場內平均機會相若；大於 120 屬「偏熱」、大於 160 屬「極熱」、低於 80 屬「偏冷」。此指數描述的是**模型相對熱度**，不是市場賠率。若在輸入提供 `market_odds`，程式會另外產生 `value_index = 預估勝率 ÷ 市場隱含機率`，並以「模型相對看好／市場相對看好／接近市場」標籤呈現；這只是一個比較指標，不代表必然價值。

## 新賽事輸入介面

準備一份 JSON，至少提供馬場、路程、場地以及每匹馬的馬名、檔位、負磅、騎師、練馬師。馬名、騎師及練馬師必須與官方資料的中文名稱完全一致，才可正確回溯歷史紀錄。

```json
{
  "race": {
    "racecourse": "ST",
    "distance_m": 1200,
    "surface": "草地",
    "course_config": "A",
    "going": "好地"
  },
  "runners": [
    {
      "horse_name": "馬匹中文名",
      "draw": 4,
      "weight_lbs": 126,
      "jockey": "騎師中文名",
      "trainer": "練馬師中文名",
      "market_odds": 5.8
    }
  ]
}
```

執行指令如下。輸出 `v10_prediction.csv` 為排序後摘要，`v10_prediction.json` 則包含完整預測與特徵審計。

```bash
python3 v10_win_probability.py \
  --db hkjc_last_season.sqlite \
  --race-card my_new_race.json \
  --output-csv v10_prediction.csv \
  --output-json v10_prediction.json

# 如要更保守或更進取地調整勝率分布，可附加：--temperature 2.2
```

如需要以歷史賽日作回測，務必加入 `--as-of-date YYYY-MM-DD`。該模式只使用指定日期之前的紀錄，可防止資料洩漏。

```bash
python3 v10_win_probability.py \
  --db hkjc_last_season.sqlite \
  --race-card historical_race.json \
  --as-of-date 2026-05-01
```

## 歷史切分回測

```bash
python3 backtest_v10.py \
  --db hkjc_last_season.sqlite \
  --limit 50 \
  --temperature 2.0 \
  --output v10_backtest_summary.json
```

此程式會逐場以 `as_of_date` 作資料截點，輸出首選勝出率、首三名命中頭馬比率、模型 Brier score 與等機會基準 Brier score。Brier score 越低，代表機率校準越佳。

## 資料品質檢查

完成抓取後，可用以下查詢核對覆蓋度與取消場數。

```sql
SELECT COUNT(*) AS completed_races
FROM races
WHERE race_status = 'completed';

SELECT COUNT(*) AS starter_rows
FROM starters;

SELECT race_date, racecourse, race_no
FROM races
WHERE race_status IN ('cancelled', 'void')
ORDER BY race_date, racecourse, race_no;
```

## 參考資料

[1] [香港賽馬會：單場賽果範例](https://racing.hkjc.com/zh-hk/local/information/localresults?racedate=2026/05/09&Racecourse=ST&RaceNo=5)

[2] [香港賽馬會：所有場次賽果範例](https://racing.hkjc.com/zh-hk/local/information/resultsall?racedate=2026/07/15&Racecourse=HV)

[3] [香港賽馬會：賽期表](https://racing.hkjc.com/zh-hk/local/information/fixture)


## V10.1 無即時賠率模擬測試

`test_predict_without_odds.py` 會從資料庫選取一場已完成賽事，建立一份**刻意不含** `market_odds` 的模擬排位表，再調用 `predict.py`。它驗證的是無賠率模式的輸出契約，**不是**無未來資料的歷史準確度回測。

```bash
python3 test_predict_without_odds.py \
  --db hkjc_last_season.sqlite \
  --model horse_model.pkl \
  --date 2026-07-15 \
  --racecourse HV \
  --race-no 1 \
  --output-dir no_odds_test_output
```

測試通過時，腳本會核實：場內 `predicted_win_probability` 合計為 1；預估勝率、模型熱度、ELO、跑道偏差和樣本警示仍完整；`market_odds`、`market_implied_probability` 與 `ev_per_unit` 為空；兩個 Kelly 欄位均為 0；建議欄只會顯示「等待市場賠率後比較」或「樣本不足，僅供觀察」。


## V10.1 公開即時獨贏賠率輔助程式

`fetch_hkjc_live_odds.py` 會以本機無頭瀏覽器讀取香港賽馬會的**公開**獨贏／位置頁，等待可見馬名及獨贏數值載入，再將「官方中文馬名：獨贏派彩倍數」寫入 `odds_overlay.json`。程式不會登入、不會投注、不會直接猜測或探測未公開的資料端點。

```bash
# 已建立 race_card.json 後，讀取公開獨贏賠率並以馬名篩選
python3 fetch_hkjc_live_odds.py \
  --race-card race_card.json \
  --output odds_overlay.json \
  --metadata-output odds_overlay.meta.json
```

工具每次執行只載入一次公開頁面，並以狀態檔強制兩次請求最少相隔 60 秒。頁面未開售、資料未載入、馬名不對應、結構改變或出現存取限制時，程式會以錯誤結束，且**不會寫入部分 overlay**。`odds_overlay.meta.json` 會記錄抓取時間、來源、寫入筆數與任何未對應馬名。

離線測試可使用 `--html public_odds_fixture.html`；此模式不會發送網絡請求。


## V10.1 馬匹裝備變動特徵

香港賽馬會官方馬匹近績表逐場列出「配備」，包括 `TT1`、`BO/TT`、`BO-/TT` 及 `--`。當中 `1` 表示首次使用、`2` 表示重戴、`-` 表示除去。[4] `enrich_hkjc_equipment.py` 會以單線程和保守延遲讀取官方馬匹近績頁，將逐場配備回填至 `starters.equipment` 與可稽核的 `starter_equipment` 表；遇 403／429 會停止而不繞過限制。

```bash
python3 enrich_hkjc_equipment.py \
  --db hkjc_last_season.sqlite \
  --delay-seconds 2.5
```

| 特徵 | 定義 | 無未來資料處理 |
|---|---|---|
| `is_first_time_blinker` | 當場官方配備為 `B1` 或 `BO1` 時為 1。 | 只讀取該場排位表已公布配備。 |
| `is_equip_added` | 相對上一正式出賽新增至少一種基礎裝備。 | 沒有已知上仗配備時為 0。 |
| `equipment_changed` | 當前有效裝備集合與上一正式出賽不同。 | 僅比較開跑前已知的前一場資料。 |
| `trainer_equip_change_roi_pre` | 同馬房近兩年裝備變動馬的平滑勝率相對馬房基準。 | 每列只累積較早賽日資料，並限制在 0.5–1.5。 |

> `trainer_equip_change_roi_pre` 為相容性名稱。由於資料庫並無逐注投注回報，它是**平滑勝率權重**，不是字面投注 ROI，也不代表回報保證。

完成增量賽果抓取後，`monthly_update.py` 會預設重查官方配備、重建特徵及重訓模型。只有本機快速測試才應使用 `--skip-equipment`；否則裝備資料缺漏會被模型降級為中性訊號。

[4] [香港賽馬會：官方馬匹近績配備欄示例](https://racing.hkjc.com/zh-hk/local/information/horse?horseid=HK_2025_L083)
