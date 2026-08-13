# 香港賽馬會獨贏賠率與 V10.1 EV 核實

## 官方資料結論

香港本地獨贏屬於**平分彩金彩池（pari-mutuel）**，並非單一賽事的固定賠率產品。香港賽馬會說明，獨贏／位置／連贏／位置Q 彩池的彩金佔彩池百分比為 **82.5%**，即彩池層面的扣除比例為 17.5%。[1]

就另外的固定賠率產品，馬會明確定義：

> `Bet Amount x Odds Fixed at Time of Placing Bet = PAYOUT` [2]

這確認官方展示的「賠率」在此語境下是**連本帶利的派彩倍數（decimal payout multiple）**。而本地獨贏為平分彩金彩池，臨場顯示的獨贏賠率只是最終派彩倍數的估計值，會隨彩池變動。

## V10.1 計算規則

若輸入的是香港賽馬會顯示的獨贏賠率 `O`（連本帶利倍數），每 $1 投注的期望淨回報為：

`EV = p × O − 1`

其中 `p` 為模型勝率。市場隱含機率為：

`市場隱含機率 = 1 / O`

使用實際／臨場顯示的香港獨贏賠率時，**不可再把 17.5% 另行乘扣於 EV**；因為該扣除已反映在獨贏彩池派彩倍率之內。重複扣除會導致雙重計算抽水。

17.5% 可用於以下不同情況：

1. 把尚未扣除抽水的「原始彩池公平概率」轉為理論派彩估計時；或
2. 在沒有官方獨贏賠率、而要構建完全假設性的市場基準時。

兩種情況均不適用於 `predict.py` 已接收官方獨贏賠率的 EV 計算。

## 凱利比例

以 decimal payout multiple `O` 表示時，淨賠率 `b = O − 1`。完整 Kelly 比例為：

`f* = (p × O − 1) / (O − 1)`

V10.1 只會輸出「理論 Kelly 比例」與保守的四分之一 Kelly 上限，並在資料樣本不足、EV 非正或概率未達門檻時輸出 0。此為模型風險尺度，不是投注指令。

## 參考資料

[1] [香港賽馬會：平分彩金本地彩池](https://special.hkjc.com/e-win/zh-HK/betting-info/racing/beginners-guide/local-pools/)

[2] [Hong Kong Jockey Club: Fixed Odds Bet Types](https://special.hkjc.com/e-win/en-US/betting-info/racing/beginners-guide/fixed-odds/)
