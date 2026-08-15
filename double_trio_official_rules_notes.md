# 孖T官方規則設計筆記

**核實日期：** 2026-08-15

## 官方來源結論

香港賽馬會將孖T列為多寶彩池。基本勝出條件為：在指定兩場賽事中，**每一關均選中第一、第二及第三名馬匹，毋須順序**。因此，資料模型必須以兩個關次保存，每關三匹無順序組合，而不是以有順序的三重彩模型保存。[1]

在孖T勝出組合無人投注時，第一關的頭三名馬匹（不論次序）可獲淨得彩池的 50%，毋須理會第二關。因此系統須另存 `CONSOLATION` 派彩層和第一關的局部勝出組合；不可將它與正常兩關均中的 `MAIN` 組合合併。[1]

HKJC 注數表說明：孖T總組合數為每關組合數目的相乘。因此模型產生複式候選時，應同時保存每關組合數、交叉積及每組合固定注額；不能把一張複式票誤作單一選擇。[2]

孖T的彩金比例為彩池 75%，另有由馬會收入部分扣除的 0.5% 營辦者儲備扣數。此資訊可作彩池背景描述，但不得用固定抽水率替代特定組合的 T-15／T-5 顯示或估計派彩來計算指標 EV。[1]

## V10.2 設計推論

- `pool_type = DOUBLE_TRIO`，`expected_leg_count = 2`。
- `selection_ordering = LEGGED`；每一關內為無順序頭三，canonical key 按馬號遞增重編 `P1..P3`。
- `MAIN` 需要兩關均命中；`CONSOLATION` 只代表第一關命中且沒有正常勝出組合時的官方派彩層。
- T-15／T-5 時間錨點使用第一關預定開跑時間；模型候選生成時間不得晚於快照。
- 指標 EV只能使用相同 `selection_key`、相同派彩層的 T-15／T-5 特定報價；總池額、賽後派彩及未顯示組合不可補值。
- 實現 ROI 在候選固定後才讀取兩關官方結果及 `official_pool_payouts`；必須分開報告 MAIN 命中與 CONSOLATION 命中。

## References

[1] [香港賽馬會：平分彩金本地彩池](https://special.hkjc.com/e-win/zh-HK/betting-info/racing/beginners-guide/local-pools/)

[2] [香港賽馬會：注數表－平分彩金彩池](https://special.hkjc.com/e-win/zh-HK/betting-info/racing/beginners-guide/chance-table/)
