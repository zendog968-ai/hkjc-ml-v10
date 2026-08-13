# V10.1 GitHub Actions：賽前一小時掃描

`.github/workflows/race_day_scan.yml` 會在 `race_day_schedule.json` 設定的場次開跑前約一小時，執行官方排位表、公開獨贏／位置賠率、V10.1 預測、雙策略篩選及 Markdown 報告產生流程。它不會發送 WhatsApp 訊息或執行任何投注；只在工作流摘要與下載工件中提供預覽連結。

> GitHub 的排程工作流最低頻率為每 5 分鐘，而且高負載時可能延遲。因此此工作流適合備援與報告型掃描；如需要接近固定分鐘的賽前執行，應以長期 Linux 伺服器上的 Cron 方案為主。[1]

## 需要的儲存庫資產

資料庫與模型沒有放進 Git，因為它們屬於大型生成檔。工作流會從同一個私人儲存庫的**私有 Release** 下載以下兩個資產：

```text
Release tag: v10-assets
Assets:
- hkjc_last_season.sqlite
- horse_model.pkl
```

請在 GitHub 的 Releases 頁建立一個名為 `v10-assets` 的私人 Release，並上傳這兩個已驗證檔案。之後每次月度重訓產生新模型時，請替換 Release 中的同名資產。不要把資料庫、模型、即時賠率或日常預測結果直接提交到 Git。

## 每個賽日前：更新時間設定

在賽日前，依香港賽馬會最後公布的開跑時間更新並提交 `race_day_schedule.json`：

```json
{
  "timezone": "Asia/Hong_Kong",
  "trigger_minutes_before": 60,
  "trigger_window_minutes": 10,
  "meeting": {
    "race_date": "2026/09/06",
    "racecourse": "ST",
    "race_start_times": {
      "1": "13:00"
    }
  }
}
```

`trigger_window_minutes` 是排程容許視窗。以範例為例，Action 在香港時間 12:00 至 12:09 啟動時會執行第 1 場；其他時間會安全跳過。若排程延遲超過此視窗，請使用 GitHub Actions 頁面的 **Run workflow**，勾選 `Force one scan` 並輸入賽日、馬場與場次。

## 工作流輸出

成功運行後，以下檔案會作為 Actions artifact 上傳，並保存工作流 summary：

| 檔案 | 用途 |
|---|---|
| `race_card.json` | 當場官方排位表模型輸入。 |
| `odds_overlay*.json` | 公開獨贏及位置賠率與資料狀態。 |
| `prediction.json` / `.csv` | V10.1 獨贏與位置機率、EV、Kelly 研究性輸出。 |
| `high_probability_filter.json` | 分開的熱門穩攻及冷門突襲結果。 |
| `pre_race_report.md` | 可讀 Markdown 報告及 WhatsApp 預覽連結。 |

## 雙策略規則

| 策略 | 條件 |
|---|---|
| 熱門穩攻 | 獨贏勝率 ≥ 10% **或** 位置勝率 ≥ 85%。位置勝率 ≥ 90% 額外標示為「超級焦點」。 |
| 冷門突襲 / Value Bomb | 獨贏賠率 ≥ 10.0、位置賠率 ≥ 3.5、獨贏勝率 ≥ 8%、位置勝率 ≥ 80%，四項必須同時成立。 |

當至少有一匹馬符合條件時，`pre_race_report.md` 會列出目標為 `85296896832` 的 WhatsApp Direct Link。該連結只會開啟 WhatsApp 訊息預覽；發送動作由您自行確認。

## 故障處理

公開賠率頁如出現空值、SCR、逾時或結構變化，賠率讀取器會輸出降級狀態，而非令預測程式崩潰。這種情況下，機率模型仍可輸出，但受影響馬匹的 EV 是 `null`、Kelly 是 `0`。請在工件中的 `odds_overlay.meta.json` 檢查 `status` 與 warnings。

> 賽馬結果具有不確定性。篩選、模型機率、EV 與 Kelly 僅供研究和風險比較，不構成獲利保證或自動投注建議。

## 參考資料

[1] [GitHub Docs：排程觸發工作流](https://docs.github.com/actions/using-workflows/events-that-trigger-workflows#schedule)
