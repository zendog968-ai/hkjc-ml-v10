# HKJC 上季賽果資料來源核實

## 馬季定義

本資料庫將「上一季」定義為 **2025/26 香港馬季**，資料期間為 **2025-09-07 至 2026-07-15**。香港賽馬會的官方賽果頁於單場檢視提供完整參賽馬紀錄；賽日總覽則提供各場頭四名及派彩資訊。

## 官方資料入口

| 用途 | 官方 URL 模式 | 可取得內容 |
|---|---|---|
| 單場完整賽果 | `https://racing.hkjc.com/zh-hk/local/information/localresults?racedate=YYYY/MM/DD&Racecourse=ST|HV&RaceNo=N` | 班次、路程、場地狀況、跑道、完成時間、全馬名次、馬號、馬名、騎師、練馬師、實際負磅、排位體重、檔位、頭馬距離、沿途走位、獨贏賠率。 |
| 賽日總覽 | `https://racing.hkjc.com/zh-hk/local/information/resultsall?racedate=YYYY/MM/DD&Racecourse=ST|HV` | 全日各場賽事概覽、頭四名與派彩，用作完整性核對與識別有效賽日。 |

## 欄位映射

| 本地資料庫欄位 | 官方欄位 |
|---|---|
| `race_date` | 賽事日期 |
| `racecourse` | 沙田／跑馬地 |
| `race_no` | 第 N 場 |
| `horse_name`, `horse_code` | 馬名／括號內馬匹編號 |
| `race_class`, `distance_m`, `surface`, `course_config`, `going` | 賽事標題及賽道、場地狀況文字 |
| `draw`, `weight_lbs` | 檔位、實際負磅 |
| `finish_pos`, `finish_time`, `margin_behind_winner` | 名次、完成時間、頭馬距離 |
| `jockey`, `trainer`, `win_odds` | 騎師、練馬師、獨贏賠率 |

## 限速規則

抓取器預設逐請求延遲 1.5–2.5 秒、單一工作執行緒、每 20 次成功請求額外冷卻 20 秒；每完成一場即寫入 SQLite，並以賽日／場次唯一鍵支援中斷續跑。遇到 429、403 或暫時錯誤時以遞增退避重試，絕不繞過存取控制。

## 來源

1. HKJC 單場完整賽果樣本：https://racing.hkjc.com/zh-hk/local/information/localresults?racedate=2026/05/09&Racecourse=ST&RaceNo=5
2. HKJC 賽日全場賽果樣本：https://racing.hkjc.com/zh-hk/local/information/resultsall?racedate=2026/07/15&Racecourse=HV
3. HKJC 賽期表：https://racing.hkjc.com/zh-hk/local/information/fixture

> 資料僅用於研究與分析。賽事最終結果以香港賽馬會公布為準。
