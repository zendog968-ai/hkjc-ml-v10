# V10.2 複合彩池 Odds Snapshot：SQLite Schema 擴充設計

**作者：** Manus AI
**目的：** 將現有的 V10.2 單馬 Win／Place 賽前快照庫擴充至三重彩、單T、四重彩、四連環及六環彩，同時維持 T-15／T-5 時間完整性、資料可稽核性與無資料洩漏的回測標準。
**規則基礎：** 香港賽馬會把三重彩、四重彩視為有順序組合；單T及四連環屬無順序組合；六環彩涵蓋指定六關，每關命中第一或第二名，而六寶獎要求六關均命中第一名。[1]

## 1. 現行 schema 的優點與結構缺口

目前 `pre_race_odds_snapshots` 以「一場賽事的一次捕捉」為粒度，而 `pre_race_odds_runner_prices` 以 `(snapshot_id, horse_name)` 保存一匹馬的 Win／Place 價格。這對單馬市場是正確的：每一個價格都只屬於一匹馬，且可直接連接模型首選。

但複合彩池的市場標的是**一張組合票**，不是單一馬匹。三重彩 `2-7-4` 與 `7-2-4` 是兩個不同的有順序組合；單T的 `{2,4,7}` 則是無順序集合；六環彩是六個不同場次、每關一個選擇的向量。因此，若把組合價硬塞進 `runner_prices`，便會遺失排序、關次、馬號、合併彩池及派彩層級，並令回測無法可靠重建。

| 現行單馬層 | 對 Win／Place 的適用性 | 對複合彩池的限制 |
|---|---|---|
| `pre_race_odds_snapshots` | 一場、一次 T-15／T-5 捕捉。 | 缺少「同一個彩池有多關」與「相同會議有多個彩池」的事件層。 |
| `pre_race_odds_runner_prices` | 一匹馬一個獨贏／位置價格。 | 不能表示順序、無順序集合、六關組合或一個組合的多個成員。 |
| 原始 JSON 與 SHA-256 | 保留來源與防止重複匯入。 | 仍必須保留；複合彩池應採同一稽核策略。 |
| 結果連接 | 可把單馬選擇標記為勝／負。 | 需分離「各關名次」與「MAIN／SIX_WIN_BONUS 等派彩層」。 |

## 2. 建議的六層正規化模型

新增檔案 `schema_prerace_complex_pool_snapshots.sql` 以附加方式擴充現有 schema，不修改或取代 Win／Place 表。架構把原始市場、組合、結果與派彩分開保存。

| 層次 | 資料表 | 粒度 | 功能 |
|---|---|---|---|
| 彩池事件 | `pre_race_pool_events` | 一個官方彩池 | 保存彩池類型、會議、官方彩池代碼與預期關數。 |
| 關次 | `pre_race_pool_event_legs` | 一個彩池的一關 | 對應確切賽日、馬場、場次與預定開跑 UTC 時間。 |
| 賽前快照 | `pre_race_pool_snapshots` | 一次 T-15／T-5 抓取 | 保存時間錨點、偏差、完整性、總池額、多寶、原始 JSON及雜湊。 |
| 可觀察組合報價 | `pre_race_pool_selection_quotes` | 一個顯示的組合 | 保存 canonical key、順序性、報價種類、金額與顯示名次。 |
| 組合成員 | `pre_race_pool_selection_members` | 組合內一匹馬 | 保存關次、名次位置、馬號及可選官方馬名。 |
| 賽後事實 | `official_pool_result_members`、`official_pool_payouts` | 名次／派彩層 | 保存結果與正式派彩；不得反向用作賽前特徵或報價。 |

> 這個設計的核心是「**市場快照與模型決策分離，市場快照與賽後派彩亦分離**」。只有先前可見的組合報價才可用於賽前 EV；正式派彩只用於實際收益標記。

## 3. 彩池語義及 canonical key

| 用戶常用名稱 | V10.2 `pool_type` | 關數 | `selection_ordering` | Key 示例 |
|---|---|---:|---|---|
| 三重彩（若「三疊彩」指有順序版本） | `TRIFECTA_ORDERED` | 1 | `ORDERED` | `L1:P1=2|L1:P2=7|L1:P3=4` |
| 單T（若「三疊彩」指無順序頭三名） | `TRIO_UNORDERED` | 1 | `UNORDERED` | `L1:P1=2|L1:P2=4|L1:P3=7` |
| 四重彩 | `QUARTET_ORDERED` | 1 | `ORDERED` | `L1:P1=2|L1:P2=7|L1:P3=4|L1:P4=9` |
| 四連環 | `FIRST_4_UNORDERED` | 1 | `UNORDERED` | `L1:P1=2|L1:P2=4|L1:P3=7|L1:P4=9` |
| 四重彩／四連環合併 | `QUARTET_FIRST_4_COMBINED` | 1 | 兩條結果軌道 | 同一事件中保留 ordered 與 unordered key。 |
| 六環彩 | `SIX_UP` | 6 | `LEGGED` | `L1:P1=2|L2:P1=7|L3:P1=4|L4:P1=8|L5:P1=1|L6:P1=3` |

canonical key 一律以**賽事中的馬號**生成，以避免中文名稱異體、空格與改名造成對應錯誤。對無順序彩池，組合成員要按馬號遞增固定排列；對有順序彩池，`position_no` 等同選擇的名次。六環彩的 `leg_no` 是不可省略的主鍵部分。

## 4. T-15／T-5 的時間設計

單場三重彩／四重彩以該場預定開跑時間為時間錨點。六環彩則以指定的**首關**為預設時間錨點：T-15 或 T-5 的捕捉必須在第一關開跑前完成，並以 `anchor_leg_no`、`scheduled_anchor_start_utc` 和 `capture_delta_seconds` 保存實際偏差。

| 欄位 | 用途 | 回測用途 |
|---|---|---|
| `captured_at_utc` | 真實抓取時間。 | 驗證未在第一關關閉後取得資料。 |
| `scheduled_anchor_start_utc` | 用作 T-15／T-5 的那一關預定開跑時間。 | 重算目標時點。 |
| `capture_delta_seconds` | 真實捕捉時間與目標時點的秒數差。 | 把遲到或過早快照排除或分層分析。 |
| `quote_completeness` | `full`、`market_summary_only`、`partial`、`unavailable`。 | 防止把只含總池額／熱門表的頁面誤稱全組合市場。 |

六環彩後續關次的開跑時間也保存於 `pre_race_pool_event_legs`。這使系統能事後檢查所有關次身分，但不可因後續關次尚未開跑而在第一關後補抓更新價格，再冒充為首關 T-15／T-5。

## 5. 報價、估計派彩與真實 ROI 的區別

複合彩池是平分彩金結構，官方頁面不一定會列出全部可能組合的完整即時價格。對三重彩、四重彩與六環彩，系統應把不同可見程度如實保存，而不是推導不存在的市場價格。

| `quote_kind`／完整性 | 可保存內容 | 可否計算賽前組合 EV | 可否計算賽後實際 ROI |
|---|---|---|---|
| `ESTIMATED_DIVIDEND` 或 `DISPLAYED_ODDS`，且候選組合可一對一匹配 | 特定組合的賽前顯示值。 | 可以，但必須標為「基於估計／顯示派彩的指標 EV」。 | 可以，用賽後正式派彩作實際收益。 |
| `POOL_SHARE` 或 `market_summary_only` | 總池、累積多寶、熱門／局部資料。 | 不可以對未顯示組合計算精確 EV。 | 只有在模型票和官方結果已固定後，才可計算事後 ROI；不能聲稱有賽前 EV。 |
| `partial` | 少量顯示組合或部分關次。 | 只可在模型候選組合本身被觀察到時計算；同列覆蓋率。 | 可以對完全對應的票計算，但不能外推到所有候選。 |
| 賽後 `official_pool_payouts` | `MAIN`、`CONSOLATION`、`SIX_WIN_BONUS` 等正式派彩。 | **不可以**。 | 可以作收益標記及實現 ROI。 |

香港賽馬會的本地彩池規則把三重彩、四重彩及六環彩列為平分彩金結構，六環彩另有普通勝出組合與六寶獎組合。因此，六環彩必須把 `MAIN` 與 `SIX_WIN_BONUS` 分開保存，不得把兩者混成單一賠率。[1]

## 6. 回測閘門與模型連接

真正的複合彩池回測應新增**模型候選票層**，但不能寫入原始市場快照表。建議日後建立 `model_pool_candidates` 與 `model_pool_candidate_members`，保存模型版本、生成 UTC 時間、所依據的 `pool_snapshot_id`、canonical key、模型命中機率、被引用的市場報價、EV、票額與生成理由。

| 閘門 | 必要條件 |
|---|---|
| 模型時間 | 候選組合在對應 T-15／T-5 快照前或同一工作流程內固定。 |
| 市場時間 | 快照捕捉於錨點關開跑前，且偏差在預先定義的容忍區間。 |
| 組合對應 | 模型 key、報價 key、關次及馬號完全相同。 |
| 可見價格 | 組合具有其本身的 `DISPLAYED_ODDS` 或 `ESTIMATED_DIVIDEND`；不可用總池額推導。 |
| 賽後分離 | 名次及派彩只可從官方結果表取得，不能改寫模型候選或賽前價。 |
| 報告完整性 | 同列候選數、具價候選數、覆蓋率、平均指標 EV、實現 ROI、最大回撤與被排除原因。 |

這些條件下，「賽前 EV」應被稱為**指標 EV**，因為平分彩金的最終派彩仍會在關閉前變動；「實現 ROI」則使用賽後正式派彩。兩者均不能保證未來回報。

## 7. 遷移步驟

第一步維持既有 `schema_prerace_odds_snapshots.sql` 不變。第二步在同一獨立的快照 archive 資料庫中套用擴充 schema；第三步擴展抓取器，使其為每次已知彩池事件寫出原始 JSON、關次清單、捕捉時間與每個可觀察組合；第四步建立獨立模型候選票表與回測器。不要在未有真實組合報價前直接產生複合彩池 EV。

```bash
cd /home/ubuntu/hkjc_v10_database
# 先建立既有單馬快照結構，然後追加複合彩池結構
# 匯入／遷移程式應以 SQLite executescript 依序執行兩個 SQL 檔
python3 verify_complex_pool_schema.py
```

擴充 schema 已通過隔離 SQLite 驗證：測試成功連接一個有順序三重彩事件、關次、T-15 快照、組合報價、三個組合成員、三個正式名次成員、正式 MAIN 派彩和完整檢視表列。

## 8. 已更新的可重複使用技能

`hkjc-prerace-odds-snapshots` 現已包含：

| 技能資源 | 新用途 |
|---|---|
| `templates/schema_prerace_complex_pool_snapshots.sql` | 複合彩池核心 schema。 |
| `references/complex-pool-model.md` | 香港彩池語義、canonical key、時點與 EV 閘門。 |
| 更新的 `SKILL.md` | 在涉及三重彩、單T、四重彩、四連環或六環彩時，自動引導載入複合彩池規格。 |

## References

[1] [香港賽馬會：平分彩金本地彩池](https://special.hkjc.com/e-win/zh-HK/betting-info/racing/beginners-guide/local-pools/)
