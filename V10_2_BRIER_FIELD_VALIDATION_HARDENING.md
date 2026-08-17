# V10.2 賽後 Brier Field 校驗強化

**版本：** V10.2
**適用元件：** `post_race_audit.py`、`schema_overseas_racing.sql`、`verify_overseas_archive_audit_guidance.py`

## 目的

本次更新使 `post_race_audit.py` 只會在官方結果與賽前預測形成一個**完整、可對應、機率守恆**的場內 field 時寫入 Brier Score。若任一必要條件不成立，系統保留原始預測與官方結果供稽核，但將 `brier_score` 寫為 `NULL`，並以明確狀態碼說明拒絕原因。

這防止頭馬不在預測名單、馬號型別不一致、重複馬號、撤回導致 field 改變、非有限機率或未正規化向量被誤解為可比較的模型校準指標。

## 計分條件

通過所有下列條件後，系統使用單場多分類 Brier 總和：

\[
B_r=\sum_{i=1}^{N}(p_i-y_i)^2
\]

| 校驗項目 | 規則 |
|---|---|
| 官方 field | 至少一匹有可正規化的正整數馬號；馬號不能重複。 |
| 官方頭馬 | field 中必須剛好一匹 `finish_pos=1`。 |
| 賽前 field | 每列馬號必須能嚴格正規化為正整數，且不能重複。 |
| field 匹配 | 賽前馬號集合必須與官方馬號集合完全相同。 |
| 頭馬匹配 | 官方頭馬必須在賽前 field 之內。 |
| 勝率 | 每項必須有限、介乎 0 至 1，且總和與 1 的差距不超過 `1e-6`。 |

`"2"`、`2` 與 `2.0` 會正規化為馬號 `2`；`2.5`、`0`、負數、布林值、空值或非數字字串則拒絕。此正規化只用於身份比對，**不會**賽後改寫賽前機率。

## 審計欄位

`post_race_audits` 新增下列可查詢欄位；舊資料庫由 `ensure_audit_schema()` 加性遷移，不會覆寫歷史紀錄。

| 欄位 | 說明 |
|---|---|
| `brier_score` | 通過所有校驗後的單場 Brier 總和；否則 `NULL`。 |
| `brier_status` | `scored` 或 `not_scored_*` 原因碼。 |
| `brier_field_size` | 用於 field 比對的官方馬匹數。 |
| `brier_uniform_baseline` | 等機率單場基準 `1 - 1/N`。 |
| `brier_probability_sum` | 通過 field 比對後的賽前勝率總和；總和不為 1 時可用於診斷。 |

主要拒絕狀態包括：

```text
not_scored_no_official_field
not_scored_duplicate_official_horse_no
not_scored_missing_or_ambiguous_official_winner
not_scored_no_prerace_prediction
not_scored_invalid_prediction_horse_no
not_scored_missing_probability
not_scored_nonfinite_probability
not_scored_probability_out_of_range
not_scored_duplicate_prediction_horse_no
not_scored_winner_missing_from_prerace_field
not_scored_prerace_field_mismatch
not_scored_probability_sum_not_one
```

## 測試結果

以下隔離契約測試已通過：

```bash
cd /home/ubuntu/hkjc_v10_database
rm -rf overseas_archive_audit_guidance_fixture
python3 -m py_compile post_race_audit.py verify_overseas_archive_audit_guidance.py
python3 verify_overseas_archive_audit_guidance.py
```

| 案例 | 預期結果 | 已驗證 |
|---|---|---|
| 正常三匹 field | Brier `0.78`、`brier_status=scored`、`p_sum=1.0`。 | 是 |
| 賽前馬號為字串 | 與官方整數馬號正確匹配並計分。 | 是 |
| 頭馬缺失 | `NULL` 與 `not_scored_winner_missing_from_prerace_field`。 | 是 |
| field 不一致 | `NULL` 與 `not_scored_prerace_field_mismatch`。 | 是 |
| 重複預測馬號 | `NULL` 與 `not_scored_duplicate_prediction_horse_no`。 | 是 |
| 機率總和不為 1 | `NULL` 與 `not_scored_probability_sum_not_one`。 | 是 |

## 仍待實作的獨立閘門

這次只處理**場內 identity 與機率完整性**。自動海外覆盤目前仍須補上「只選取 `generated_at_utc < scheduled_start_utc` 的預測批次」的時間閘門。完成前，任何由 `load_latest_overseas_prediction()` 自動載入的預測仍應維持研究性標記，不應用作正式歷史模型校準結論。
