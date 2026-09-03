import os
import re
import json
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta

BASE_URL = "https://racing.on.cc"
HK_TZ = timezone(timedelta(hours=8))

def fetch_html(url):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    resp = requests.get(url, headers=headers, timeout=15)
    resp.encoding = "big5"
    return resp.text if resp.status_code == 200 else None

def get_current_race_day_info():
    url = f"{BASE_URL}/racing/ifo/current/rjifoa0001x0.html"
    html = fetch_html(url)
    if not html:
        return None, 0
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text()
    date_match = re.search(r"賽事日期[：:]\s*(\d{2})/(\d{2})/(\d{4})", text)
    if not date_match:
        return None, 0
    d, m, y = date_match.groups()
    race_date = f"{y}-{m}-{d}"
    return race_date, 10

def scrape_race_horses(race_date, total_races=10):
    records = []
    for race_no in range(1, total_races + 1):
        padded_no = f"{race_no:04d}"
        url = f"{BASE_URL}/racing/ifo/current/rjifoa{padded_no}x0.html"
        html = fetch_html(url)
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        tables = soup.find_all("table")
        if len(tables) < 2:
            continue
        rows = tables[1].find_all("tr")[1:]
        for row in rows:
            cols = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            if len(cols) >= 6 and cols[0].isdigit():
                horse_no = int(cols[0])
                horse_name = cols[1]
                jockey = cols[5]
                trainer = cols[8] if len(cols) > 8 else ""
                record = {
                    "race_date": race_date,
                    "race_no": race_no,
                    "horse_no": horse_no,
                    "horse_name": horse_name,
                    "trainer": trainer,
                    "jockey": jockey,
                    "morning_trackwork": None,
                    "expert_tips": {"recommend_count": 0, "details": []},
                    "barrier_trial": None,
                    "stable_intel": None
                }
                records.append(record)
    return records

def attach_qualitative_data(records, race_date):
    return records

def main():
    race_date, total_races = get_current_race_day_info()
    if not race_date:
        print("未檢測到即將舉行的賽事，跳過本次執行。")
        return
    print(f"開始處理賽事日 {race_date} 共 {total_races} 場資料...")
    records = scrape_race_horses(race_date, total_races)
    records = attach_qualitative_data(records, race_date)
    records.sort(key=lambda x: (x["race_no"], x["horse_no"]))
    os.makedirs("data", exist_ok=True)
    out_path = f"data/v10_racing_sync_{race_date.replace('-', '')}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    print(f"成功輸出 {len(records)} 筆主鍵對齊記錄至 {out_path}")

if __name__ == "__main__":
    main()
