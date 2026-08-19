# York S1-1 海外深度資料來源查核

日期：2026-08-19；目標：英國 York 13:50（香港時間 20:50）香港賽馬會 S1-1。

## 已核實公開內容

Racing Post 的 York 13:50 racecard 公開顯示賽事為 5f 89y、Good、22 匹馬，並在卡片列出每匹馬的 OR、Top Speed（TS）、Racing Post Rating（RPR）、檔位、騎師、練馬師及公開賠率。這可作為本原型的 RPR／TS 主來源。[1]

At The Races 的同一場公開 racecard 顯示馬匹血統（父、母、外祖父），以及距離、相近場地、馬場、班次與騎／練連結的歷史 runs、wins、places 和 win rate。這可作為場地／路程適應性及公開騎練歷史表現資料來源。[2]

Timeform 公開頁確認場次、路程、Going、出賽馬及部分歷史資料；但 TFR、Hints、完整 Pace／Race Pass 等內容明確受登入或付費牆保護。本模組不得規避登入、付費牆、反爬機制或抓取受限內容；受限欄位一律保存為 `unavailable_paid_or_restricted`，不以推測數值填補。[3]

HKJC 的公開 resultsall 頁確認 2026-08-19 是 S1 United Kingdom 的七場海外轉播賽日；但該 results 端點於開賽前尚未發布場次資料。因此香港獨贏賠率必須由既有 `fetch_hkjc_s1s2.py`／HKJC 賽前頁後續取得，缺值時保持 N/A。[4]

## 原型資料政策

本次只擷取公開可見、每個欄位帶來源 URL、擷取時間及來源狀態的資料。初步綜合評分僅使用已驗證的公開 RPR、TS、At The Races 評分與可明確解析的相近場地／路程勝率；它不是 V10.2 正式機率、EV、Kelly 或 N6 Neural Score。S1/S2 將顯式標記 `n6_status=disabled_non_hk`。

## References

[1]: https://www.racingpost.com/racecards/107/york/2026-08-19/924986/ "Racing Post：York 13:50 racecard"
[2]: https://www.attheraces.com/racecards/York/19-August-2026 "At The Races：York 19 August 2026 racecards"
[3]: https://www.timeform.com/horse-racing/racecards/york/2026-08-19/1350/62/1 "Timeform：York 13:50 racecard"
[4]: https://racing.hkjc.com/en-us/local/information/resultsall "HKJC：S1 United Kingdom results information"
