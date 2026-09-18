# S3-3 正式 Manifest 啟用與來源記錄

- 核實時間：2026-08-21 13:31–13:48 UTC。
- HKJC 官方賽期表顯示 2026-08-21 為 Nunthorpe Stakes Day，代碼 S3，英國 York。[1]
- HKJC 官方 Win／Place 頁顯示 S3-3 為 Gimcrack Stakes（G2，1200m Turf，York Racecourse），開跑時間為 2026-08-21 22:00 HKT，即 14:00 UTC；市場快照有 10 匹有效馬與兩匹退出（Great Oak、Mrair）。[2]
- Racing Post 公開 compact-view 對應 York 15:00 Gimcrack Stakes，顯示 12 原始條目、兩匹 NR，並含可公開讀取的 TS／RPR 欄位。[3]
- At The Races York 2026-08-21 公開賽卡是本場公開form來源。[4]
- 正式 `runtime/overseas_blindtest/active_manifest.json` 已在賽前建立，SHA-256：`e23baf42975d385d73275fd1a97074beafe4a2bb09d62403dea22c46f759181f`，模式 0600。
- 離線驗證結果為 `valid_offline`，並明確記錄 `network_access: none`。
- 啟用後首次計時器執行在 13:48:07 UTC 因官方市場與公開深度名單未完成全場一對一身份匹配而拒絕封存，`captured_count` 維持 0；此為預期的安全閘門，沒有建立賽前決策。

## References

[1]: https://racing.hkjc.com/en-us/overseas/simulcast_fixture?y=2627
[2]: https://bet.hkjc.com/en/racing/wp/2026-08-21/S3/3
[3]: https://www.racingpost.com/racecards/107/york/2026-08-21/923373/compact-view/
[4]: https://www.attheraces.com/racecards/York/21-August-2026
