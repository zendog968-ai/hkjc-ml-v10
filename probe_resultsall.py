#!/usr/bin/env python3
"""Probe HKJC ResultsAll handling of racecourse/date parameters."""
from __future__ import annotations

from urllib.parse import urlencode
import time

import requests
from bs4 import BeautifulSoup

BASE = "https://racing.hkjc.com/racing/information/Chinese/Racing/ResultsAll.aspx"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; HKJCV10Research/1.0; +local-research)",
    "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8",
}


def probe(date: str, course: str) -> None:
    query = urlencode({"RaceDate": date, "Racecourse": course})
    response = requests.get(f"{BASE}?{query}", headers=HEADERS, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    text = soup.get_text(" ", strip=True)
    race_links = [
        anchor["href"]
        for anchor in soup.find_all("a", href=True)
        if "localresults" in anchor["href"].lower()
    ]
    print(f"date={date} course={course} http={response.status_code} title={title!r}")
    print(f"race_links={len(race_links)} first={race_links[:2]}")
    for token in ("跑馬地", "沙田", "沒有相關資料", "第 1 場", "第1場"):
        print(f"  {token}: {token in text}")


if __name__ == "__main__":
    for date, course in (("2026/07/15", "ST"), ("2026/07/15", "HV"), ("2026/05/09", "ST"), ("2026/05/09", "HV")):
        probe(date, course)
        time.sleep(2.0)
