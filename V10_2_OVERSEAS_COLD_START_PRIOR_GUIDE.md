# V10.2 海外 S1/S2 分層冷啟動 Prior Fallback

> **目的：** 海外 S1/S2 馬匹不應硬套香港 ELO。此模組以香港賽馬會公開海外賽卡可得的馬匹生涯起數／頭馬數，以及**預測時點前**已成功歸檔的海外騎師與練馬師結果，建立可解釋的場內相對先驗。 [1]
>
> **限制：** 這是低資料量下的保守研究先驗，不是跨賽區能力的真實 ELO，也不代表未來結果或投資回報保證。

## 1. 為何不用固定中性數值

舊版 S1/S2 模式在資料缺失時把所有未知馬賦予固定 strength。這雖可避免程式失敗，但會把「完全沒有資料」與「少量可驗證生涯資料」混為一談，也不能顯示模型對不同馬匹的信心差異。V10.2 新 Prior Fallback 改為將每一個來源拆成**訊號、樣本證據與不確定性**三部分；任何來源不足時，該來源的相對訊號自動變為零，而非被人為放大。

| 層級 | 賽前可用資料 | 基礎平滑 | 相對權重 | 只有何時可用 |
|---|---|---:|---:|---|
| 馬匹生涯 | 公開 `career_starts`、`career_wins` | Beta 等效先驗 20 起 | 65% | 賽卡起數與頭馬數有效。 |
| 練馬師 | 海外 SQLite 中較預測時間早的已完成結果 | Beta 等效先驗 40 起 | 20% | 有 `scheduled_start_utc` 且歷史結果早於預測時間。 |
| 騎師 | 海外 SQLite 中較預測時間早的已完成結果 | Beta 等效先驗 30 起 | 15% | 有 `scheduled_start_utc` 且歷史結果早於預測時間。 |
| 場內中性 | 當場有效馬匹數的 `1 / field_size` | 不適用 | 基準 | 所有來源缺失時。 |

## 2. 分層縮減與不確定性退火

馬匹公開生涯勝率不會直接作為場內機率。若當場有 `F` 匹馬，場內基礎勝率為 `q = 1/F`。對某一來源的 `w` 場頭馬和 `n` 次出賽，平滑後機率為：

> `p_source = (w + q × κ) / (n + κ)`

其中 `κ` 為該層的等效先驗起數。證據強度為：

> `evidence = n / (n + κ)`

系統在 log 相對強度空間組合各層：

> `signal = Σ(weight × evidence × log(p_source / base_rate))`

最後再乘上 `0.25 + 0.75 × confidence` 的退火係數。這表示當資料極少時，任何看似很高或很低的生涯率都會被拉回場內中性強度；只有隨可驗證資料增加，訊號才逐漸保留。最終 relative strength 受限於 `[0.35, 2.85]`，再由 Plackett–Luce 在該場重新正規化為 Win／Place 機率。

| `prior_confidence` | `cold_start_tier` | 預測含義 |
|---:|---|---|
| < 0.15 | `neutral_field_prior` | 沒有可用訊號；所有未知馬以相同場內基準處理。 |
| 0.15–<0.45 | `hierarchical_low_confidence` | 有少量公開生涯或歷史 archive 訊號，但縮減顯著。 |
| 0.45–<0.75 | `hierarchical_medium_confidence` | 多來源或較長樣本支持，仍保留不確定性。 |
| ≥ 0.75 | `hierarchical_high_confidence` | 多層來源均有較足夠的預測時點前樣本；仍不等同 ELO。 |

## 3. 絕對時間完整性規則

練馬師與騎師歷史資料只在 `race_status='completed'`、有 `scheduled_start_utc`、且該時間**早於** `generated_at_utc` 時才被採用。同一賽日稍後才完成的賽果、賽後派彩、最終賠率與抓取時間不明的資料均不會進入先驗。若海外結果來源不足，騎師與練馬師層只是自動不啟用，馬匹回到生涯或中性場內先驗。

每張 S1/S2 預測均會保存下列欄位：

| 欄位 | 審計用途 |
|---|---|
| `cold_start_tier` | 顯示該馬的先驗資料成熟度。 |
| `prior_source` | 區分純中性與預測時點前 archive 支援。 |
| `prior_confidence`、`prior_uncertainty` | 量化縮減程度與資料不足風險。 |
| `prior_detail_json` | 保存每一來源的 starts、wins、posterior、evidence、權重與 `as_of_utc`。 |

## 4. 使用方式

```bash
cd /home/ubuntu/hkjc_v10_database

python3 fetch_hkjc_s1s2.py \
  --date 2026-09-01 --simulcast-code S1 --race-no 3 \
  --db hkjc_last_season.sqlite --output s1s2_race_card.json

python3 predict_s1s2.py \
  --race-card s1s2_race_card.json --db hkjc_last_season.sqlite \
  --model-version V10.2-overseas-hierarchical-prior-v2 \
  --output-json s1s2_prediction.json --output-md s1s2_prediction.md
```

預測 Markdown 會在「先驗／信心」欄顯示 tier 與 confidence；JSON 的 `prior_detail` 和 SQLite 的 `prior_detail_json` 則提供完整審計軌跡。公開海外賠率不可用時，Win／Place 機率仍會輸出，但 EV 與 Kelly 必須維持空白。[2]

## 5. 資料累積後的下一步：校準，而非加重權重

當海外 archive 累積至少 100 個完整、時間對齊、已結算的 S1/S2 賽事後，應按時間順序評估每個 tier 的 Brier score、校準曲線和 log loss。任何 recalibration 都必須只在先前時段擬合，然後套用到下一個完全未見時段。若 `hierarchical_high_confidence` 在時間外樣本仍明顯過度自信，正確修復是增加退火或作單調校準，而不是提高 Kelly 或放寬 EV 門檻。

在資料未達標時，優先改善官方賽卡欄位保存、T-15／T-5 快照覆蓋、結果 archive 完整性與 `scheduled_start_utc`，而不是新增未驗證的血統、第三方評分或跨賽區 ELO 假設。海外 fixture 只用作賽期發現；單場官方排位、價格與賽果仍須逐筆存檔及核對。[1]

## References

[1] [香港賽馬會：海外轉播賽期表](https://racing.hkjc.com/en-us/overseas/simulcast_fixture)

[2] [香港賽馬會：海外轉播賽資訊](https://racing.hkjc.com/en-us/overseas/)
