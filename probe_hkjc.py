#!/usr/bin/env python3
"""Probe public HKJC result-page structure without bulk downloading."""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URL = (
    "https://racing.hkjc.com/racing/information/Chinese/Racing/"
    "LocalResults.aspx?RaceDate=2026/05/09&Racecourse=ST&RaceNo=5"
)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; HKJCV10Research/1.0; +local-research)",
    "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8",
}


def main() -> None:
    response = requests.get(URL, headers=HEADERS, timeout=30)
    response.raise_for_status()
    Path("probe_localresults.html").write_text(response.text, encoding="utf-8")
    soup = BeautifulSoup(response.text, "html.parser")

    print(f"HTTP: {response.status_code}; bytes: {len(response.content)}")
    print("\n[Select elements]")
    for select in soup.find_all("select"):
        options = select.find_all("option")
        print(f"id={select.get('id')!r}, name={select.get('name')!r}, options={len(options)}")
        for option in options[:5]:
            print(f"  value={option.get('value')!r} text={option.get_text(' ', strip=True)!r}")

    print("\n[Result-table anchor and rows]")
    header_cell = soup.find(lambda tag: tag.name == "td" and tag.get_text(" ", strip=True) == "名次")
    if header_cell:
        parent_table = header_cell.find_parent("table")
        print(f"header parent id={parent_table.get('id')!r}, class={parent_table.get('class')!r}")
        rows = parent_table.find_all("tr")
        print(f"recursive row count={len(rows)}")
        for row in rows[:8]:
            direct_cells = row.find_all("td", recursive=False)
            all_cells = row.find_all("td")
            print("  direct=", [cell.get_text(' ', strip=True) for cell in direct_cells])
            print("  all=   ", [cell.get_text(' ', strip=True) for cell in all_cells])
    else:
        print("No 名次 header cell found")

    print("\n[All table descriptors containing result-related text]")
    for index, table in enumerate(soup.find_all("table")):
        compact_text = table.get_text(" ", strip=True)
        if all(token in compact_text for token in ("名次", "馬名", "騎師", "獨贏")):
            print(f"table={index}, id={table.get('id')!r}, class={table.get('class')!r}, chars={len(compact_text)}")
            first_rows = table.find_all("tr", recursive=False)[:3]
            for row in first_rows:
                print("  row=", [cell.get_text(' ', strip=True) for cell in row.find_all("td", recursive=False)])
            break

    print("\n[Links with race date parameters]")
    counter = Counter()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if "RaceDate" in href or "racedate" in href:
            counter[href] += 1
    for href in list(counter)[:15]:
        print(href)


if __name__ == "__main__":
    main()
