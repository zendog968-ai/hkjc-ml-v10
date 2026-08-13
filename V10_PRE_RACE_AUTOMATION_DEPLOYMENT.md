# V10.1 賽前 15 分鐘自動化：長期 Linux 伺服器部署指南

本套件在每場香港賽馬開跑前 **15 分鐘**執行一次完整流程：讀取已公布的官方排位、讀取公開獨贏／位置賠率、執行 V10.1 預測、篩選「熱門穩攻」與「冷門突襲」，並在有合格馬匹時產生可點擊預覽的 WhatsApp Direct Link。它**不會自動發送 WhatsApp 訊息，也不會自動投注**。

> 正式部署前，必須在每個賽日根據香港賽馬會官方公布的開跑時間更新 `pre_race_schedule.json`。目前公開排位表解析器沒有可保證穩定的機器可讀開跑時間欄位，因此排程器刻意使用明確、可稽核的賽日設定檔，而非推測開跑時間。

## 套件檔案

| 檔案 | 用途 |
|---|---|
| `pre_race_scheduler.py` | 每分鐘由 Cron 觸發；只在各場開跑前 15 分鐘執行一次。 |
| `pre_race_schedule.example.json` | 賽日、馬場與官方開跑時間範本。 |
| `filter_high_probability.py` | 套用雙策略門檻並建立 WhatsApp 預覽連結。 |
| `cron/v10-pre-race.cron` | 可直接調整路徑後安裝的每分鐘 Cron 範本。 |
| `test_pre_race_automation.py` | 不連網的排程、篩選與連結回歸測試。 |

## 一次性伺服器安裝

以下以 `/opt/hkjc-v10` 為例。請以有適當檔案權限的 Linux 使用者執行；不要將資料庫、模型、日常賠率或預測產物加入 Git。

```bash
sudo mkdir -p /opt/hkjc-v10 /var/log/hkjc-v10
sudo chown -R "$USER":"$USER" /opt/hkjc-v10 /var/log/hkjc-v10

git clone https://github.com/zendog968-ai/hkjc-ml-v10.git /opt/hkjc-v10
cd /opt/hkjc-v10

python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
```

Git 儲存庫會刻意排除 `hkjc_last_season.sqlite` 與 `horse_model.pkl`。請在部署後採用其中一種方式取得它們：

1. 從您可信任的備份安全複製至 `/opt/hkjc-v10/`；或
2. 在伺服器重新執行受限速控制的資料抓取、特徵工程與模型訓練流程。

在啟用 Cron 前，確認兩個檔案存在：

```bash
ls -lh hkjc_last_season.sqlite horse_model.pkl
python test_v101_hardening.py --project-dir . --output runtime/v101_hardening_test_report.json
python test_pre_race_automation.py
```

## 每個賽日：設定官方開跑時間

複製範本並填寫當日實際開跑時間。時間一律採用 **香港時間**的 24 小時格式。只保留當天真正開跑的場次；若有延遲、取消或撤回馬，請先更新設定及／或重抓排位表再使用輸出。

```bash
cd /opt/hkjc-v10
cp pre_race_schedule.example.json pre_race_schedule.json
nano pre_race_schedule.json
```

範例：

```json
{
  "timezone": "Asia/Hong_Kong",
  "trigger_minutes_before": 15,
  "meeting": {
    "race_date": "2026/09/06",
    "racecourse": "ST",
    "race_start_times": {
      "1": "13:00",
      "2": "13:35",
      "3": "14:05"
    }
  }
}
```

## 安裝 Cron

先檢查路徑是否與伺服器一致，然後安裝範本：

```bash
cd /opt/hkjc-v10
crontab cron/v10-pre-race.cron
crontab -l
```

Cron 每分鐘只會呼叫排程器；排程器內部以檔案鎖避免重疊，並以 `runtime/pre_race_state.json` 記錄已完成場次。某場在 15 分鐘觸發窗內完成後，不會因下一分鐘 Cron 再次執行。輸出位置為：

```text
runtime/pre_race/YYYY/MM/DD_ST_R03/
├── race_card.json
├── odds_overlay.json
├── place_odds_overlay.json
├── odds_overlay_combined.json
├── odds_overlay.meta.json
├── prediction.json
├── prediction.csv
├── high_probability_filter.json
└── racecard.log / odds.log / predict.log / filter.log
```

## 模擬檢查與運行狀態

在不發送任何官方請求的情況下，使用模擬時間檢查某一分鐘是否會觸發：

```bash
cd /opt/hkjc-v10
. .venv/bin/activate
python pre_race_scheduler.py \
  --config pre_race_schedule.json \
  --project-dir . \
  --now 2026-09-06T12:45:00+08:00 \
  --dry-run
```

日常監察：

```bash
tail -f /var/log/hkjc-v10/pre_race_scheduler.log
cat runtime/pre_race_state.json
```

若 `odds_overlay.meta.json` 顯示 `status: degraded`，代表公開頁有空值、SCR、頁面載入問題或其他資料缺漏。該場的模型預測仍會完成；受影響馬匹的 EV 會是 `null`、Kelly 為 `0`。不應把空白 EV 當作市場訊號。

## 雙策略篩選規則

| 策略 | 門檻 | 額外標記 |
|---|---|---|
| 熱門穩攻 | 獨贏勝率 ≥ 10% **或** 位置勝率 ≥ 85% | 位置勝率 ≥ 90% 會標示為「超級焦點」。 |
| 冷門突襲 | 獨贏賠率 ≥ 10、位置賠率 ≥ 3.5、獨贏勝率 ≥ 8%、位置勝率 ≥ 80% | 標示為「冷門值博」。 |

WhatsApp 只會產生 `https://api.whatsapp.com/send?...` 預覽連結，目標電話號碼為 `85296896832`。您需自行點擊、檢視訊息及決定是否發送。

## 更新程式與模型

程式更新可由 Git 取得；資料與模型更新仍須按既有月度流程執行。

```bash
cd /opt/hkjc-v10
git pull --ff-only origin main
. .venv/bin/activate
python monthly_update.py --db hkjc_last_season.sqlite --csv hkjc_last_season.csv --end-date YYYY-MM-DD
python test_v101_hardening.py --project-dir . --output runtime/v101_hardening_test_report.json
```

> 賽馬模型、篩選與 EV 僅作研究參考。開跑時間、排位、撤回馬、場地及賠率可能變動；請以香港賽馬會的最後公布為準並量力而為。

## 參考資料

[1] [香港賽馬會：本地賽事排位表及資料](https://racing.hkjc.com/zh-hk/local/information/racecard)

[2] [香港賽馬會：本地彩池及派彩資料](https://special.hkjc.com/e-win/zh-HK/betting-info/racing/beginners-guide/local-pools/)
