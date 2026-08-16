# HKJC 海外轉播賽官方資料來源研究筆記

研究日期：2026-08-16（香港時間）

## 已核實的官方公開頁面

| 用途 | 官方入口／樣例 | 已核實的資訊 |
|---|---|---|
| 賽期與轉播賽程 | `https://racing.hkjc.com/en-us/overseas/simulcast_fixture` | 顯示海外轉播賽日期、賽名、地點與賽事概覽連結；頁面提供賽季參數樣例 `?y=2526`。目前官方頁面明示賽程與排位可更改或取消，回刷器必須保存抓取時間與頁面狀態。 |
| 海外賽事概覽／排位 | `https://racing.hkjc.com/en-us/overseas/race-summary?RaceDate=YYYYMMDD&Racecourse=S1&redirect=Y` | 頁面可列出 S1/S2 等海外轉播群組、賽程、場地、途程、馬號、馬名、負磅、騎師、檔位、練馬師、出賽總績及近績等。 |
| 單場海外賽果 | `https://racing.hkjc.com/racing/overseas/english/results.aspx?para=/YYYYMMDD/S1/RACE_NO` | 搜尋結果與官方頁面顯示海外賽果、HKJC 派彩與「資料由相應海外賽事主辦機構提供，馬會按相關賽果派彩」的資料來源聲明。中文對應頁使用 `/racing/overseas/chinese/results.aspx?para=/YYYYMMDD/S1/RACE_NO`。 |
| 全賽事派彩總覽 | `https://racing.hkjc.com/en-us/local/information/resultsall?racedate=YYYY/MM/DD&Racecourse=ST` | 同一官方結果總覽可列出香港本地及 `Overseas Races(S1 ...)`／`Overseas Races(S2 ...)` 的賽果及派彩。海外重播編號使用 `s0101`、`s0201` 等，證實同日可有多個海外轉播群組。 |

## 資料工程結論

1. `S1`／`S2` 是同一日期的海外轉播群組識別，不等同固定海外賽場。因此主鍵必須至少包含 `meeting_date`、`simulcast_code`、`race_no`，不可只用日期與場次。
2. 賽程頁公開顯示目前與指定賽季資料，但回刷程式必須針對每季逐日建立發現清單，並把「未發現」「取消」「頁面錯誤」「成功」分開記錄；不能把日期掃描失敗視作沒有轉播。
3. 結果總覽可提供香港本地和海外 S1/S2 派彩，但完整馬匹完賽時間、負於頭馬距離、沿途等欄位未必在總覽出現。回刷器需要優先抓取單場結果頁；若某欄位官方頁未公布，應存 `NULL` 和原因碼，不能發明或從最終派彩反推。
4. 最終派彩可以作賽後 ROI 結算，但不可代替賽前 T-15/T-5 組合／獨贏價格，亦不可作賽前特徵。
5. 所有原始頁面、URL、HTTP 狀態、抓取 UTC、內容 SHA-256 及解析版本均應寫入來源稽核表，以支持重跑與缺口覆核。

## 來源

1. https://racing.hkjc.com/en-us/overseas/simulcast_fixture
2. https://racing.hkjc.com/racing/overseas/english/results.aspx?para=/20251011/S1/1
3. https://racing.hkjc.com/racing/overseas/chinese/results.aspx?para=/20260124/S2/5
4. https://racing.hkjc.com/en-us/local/information/resultsall?racedate=2026/02/14&Racecourse=ST

## 動態頁面探測（2023-07-29 S1-5）

以官方舊版 `results.aspx?para=/20230729/S1/5` 連結開啟後，網站重導至現代 `en-us/overseas/results?RaceDate=20230729&Racecourse=S1&RaceNo=5` 路徑；在隔離瀏覽環境持續顯示載入指示且未呈現可解析賽果。直接 HTTP 回應亦沒有賽事日期或賽名可用標記。此結果不代表賽事不存在，只表示此回刷環境不能把該頁的空／持續載入內容當作已取得結果。

回刷器要求：若指定結果頁未呈現至少一張可辨識的名次或派彩表，必須將該場記錄為 `source_unavailable`／`partial` 並輸出到缺口報告；不可把 HTTP 200 或空 HTML 誤記為完整賽果。

## Fixture 動態渲染差異（2023/24）

`requests` 直接讀取 `simulcast_fixture?y=2324` 時只取得 React 載入容器，未含任何 2023 賽期列，因而不能以 HTTP 空殼解讀為「沒有賽事」。在官方網頁完成瀏覽器動態載入後，頁面實際呈現包含 `23/07/2023 Singapore Derby Day`、`29/07/2023 King George VI & Queen Elizabeth Stakes Day` 等 2023/24 賽程列。故 fixture 發現器必須使用保守的瀏覽器渲染模式，並在沒有表格或仍顯示 loading 時明確記錄 `source_unavailable`；不能使用 requests 原始 HTML 的空內容判定 `empty`。

## 官方前端資料服務探測

海外賽果頁公開全域設定 `https://racing.hkjc.com/simulcast/informationAsset/config/globalConfig.js` 宣告 `QIDS_URI = https://info.cld.hkjc.com/graphql/base/`。被動分析頁面載入的公開 JavaScript 顯示海外賽事頁會以 `bizDate`、`venCode`、`raceNumber` 向 QIDS 資料層請求 `GET_RACE_INFO_LIST`，並使用 `simulcastHorse`、`raceMeetingProfile`、`raceResults`、`pmPools` 等結果物件。這是官方公開前端的資料層線索，但在實作前仍須以有明確 HTTP／GraphQL 請求紀錄的受控測試驗證請求格式；不可猜測或捏造 GraphQL 查詢。

現有回刷器目前把無法由官方渲染頁取得資料的事件標為 `partial`／`source_unavailable`。日後若 QIDS 公開查詢格式獲驗證，應新增為替代官方來源，並把實際端點、HTTP 狀態、原始回應雜湊及解析版本存入 `overseas_source_documents`。

## 瀏覽器資源請求檢查

在已渲染的 2023/24 fixture 頁中，瀏覽器 `performance` 資源清單沒有觀察到 `info.cld.hkjc.com` 或 `graphql` 的可見資源 URL；頁面可能經由伺服器元件、非資源計時 API 或其他封裝路徑取得資料。故不能只依據全域設定檔便直接向 QIDS 猜測 GraphQL payload。回刷系統維持以已成功渲染的官方 fixture HTML 作為賽期發現來源，賽果部分若沒有可解析官方來源則標記缺口。

## 近期結果摘要頁探測（2026-07-28 S1）

對官方現代 `race-summary?RaceDate=20260728&Racecourse=S1` 路徑的隔離瀏覽測試同樣持續顯示載入指示，未取得可解析的場次或賽果內容。此情況出現在近期已過賽日，證實不是單純舊版 2023 URL 相容性問題。回刷器須保留 fixture 發現，並把結果層記為 `source_unavailable`／`partial`，直至存在實際可驗證的官方結果資料端點；HTTP 200、空殼或長時間 spinner 不可視作賽果完整。

## QIDS GraphQL 最小探測

對 `https://info.cld.hkjc.com/graphql/base/` 的最小 `__typename` 查詢得到官方服務的 `Query` 回應，確認該公開基底可接受 GraphQL POST。嘗試讀取根 schema 欄位（introspection）時，官方服務回覆 `Your query doesn't match the schema`，故本系統不能使用 introspection 推測賽果欄位。除非能由官方公開前端取得並驗證實際查詢文件／payload，回刷器不得直接以猜測的 GraphQL query 取得或寫入資料。
