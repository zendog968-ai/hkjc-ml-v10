# HKJC 馬匹裝備資料來源核實

## 官方來源與欄位可用性

香港賽馬會官方馬匹資料頁的「馬匹近三季往績紀錄」表，逐場提供 **配備** 欄位。此欄可用於回溯已完成賽事的實際裝備，並與同馬上一場比較。示例中，「應龍飛影 (L083)」25/26 馬季顯示 `TT1`、`TT` 與 `--`；「嘉應駿昇 (K247)」顯示 `BO1/TT`、`BO/TT`、`BO2/TT`、`BO-/TT` 等紀錄。

官方頁面同時說明：`1` 代表首次使用、`2` 代表重戴、`-` 代表除去。官方代號包括 `B`、`BO`、`V`、`VO`、`P`、`PC`、`PS`、`CP`、`CO`、`SB`、`SR`、`H`、`E`、`XB`、`CC`、`TT` 等。[1] [2]

歷史 `LocalResults` 賽果表的既有擷取欄位沒有逐馬配備，故要建立無未來資料的裝備特徵，需以 `horse_code` 批次讀取官方馬匹資料頁並依賽日合併配備紀錄。最新排位表的「配備」欄則應由官方排位表直接擷取。

## 特徵定義建議

- **is_first_time_blinker**：當前配備字串中，`B1` 或 `BO1` 等眼罩類代號帶有首次標記時為 1。
- **is_equip_added**：當前裝備相對上一正式出賽新增至少一種基礎裝備時為 1；首次出賽或未知歷史回傳 0 並標記樣本不足。
- **equipment_changed**：當前基礎裝備集合與上一正式出賽不同時為 1。
- **trainer_equip_change_roi**：按賽前可得的近兩年正式賽事，計算同練馬師裝備變動馬的平滑勝率相對於全體基準之比；名稱保留相容性，但此數據庫沒有每注派彩回報，不能宣稱為字面投資回報率。

## 來源

[1] [HKJC：應龍飛影（L083）馬匹資料及逐場配備](https://racing.hkjc.com/zh-hk/local/information/horse?horseid=HK_2025_L083)

[2] [HKJC：嘉應駿昇（K247）馬匹資料及逐場配備](https://racing.hkjc.com/zh-hk/local/information/horse?horseid=HK_2024_K247)

[3] [HKJC：認可馬匹配備登記冊](https://racing.hkjc.com/racing/english/racing-info/reg_approved_gear.aspx)
