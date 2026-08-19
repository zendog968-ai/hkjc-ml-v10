# 孖T四匹複式歷史回測：官方資料來源盤點

日期：2026-08-19

香港賽馬會的本地賽果頁 `LocalResults.aspx` 可按賽日及馬場呈現每場的完成名次、馬號與派彩表；派彩表以彩池名稱、勝出組合及港元派彩呈現，因此可用於賽後核對兩關的官方頭三及孖T派彩。[1]

香港賽馬會的孖T頁會明示第一關與第二關，並列出各關馬號及獨贏賠率。官方投注示例確認孖T為兩關各選三匹馬的組合；四匹複式在每關有 C(4,3)=4 個三馬組合，兩關交叉即 16 注。[2] [3]

目前 Cloud Computer 的 `hkjc_last_season.sqlite` 尚未建立複合彩池 archive tables，且 `runtime/pre_race/` 沒有任何非 fixture 的 N6 已富化歷史 `prediction.json`。這意味著過往賽季尚無法為「本次 V10+N6 嚴格四匹複式」提供無未來資料的真實回測；不得以其後重新運算的模型排名代替當時賽前決策，亦不得只以賽後結果填補 ROI。

後續實作將採取資料充足性閘門：只有同時具備官方兩關、當時賽前不可變的 N6 已富化四匹選擇、官方兩關頭三與官方 MAIN／適用 CONSOLATION 派彩的事件才會結算。少於 15 個完整結算事件一律標示「探索性」，0 個則顯示 N/A。

## References

[1]: https://racing.hkjc.com/racing/information/Chinese/Racing/LocalResults.aspx "香港賽馬會本地賽果及派彩頁"
[2]: https://bet.hkjc.com/en/racing/dt/ "香港賽馬會 Double Trio 即時頁"
[3]: https://www.hkjc.com/ENGLISH/betting/ticket_doublet.asp "香港賽馬會 Double Trio 投注示例"
