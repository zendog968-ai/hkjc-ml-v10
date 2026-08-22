#!/usr/bin/env python3
"""Offline-only helper to inspect archived HKJC trial-table layout for P1 parser design."""
from __future__ import annotations

import argparse
from bs4 import BeautifulSoup


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("raw")
    args = parser.parse_args()
    raw = open(args.raw, "rb").read().decode("utf-8", errors="replace")
    soup = BeautifulSoup(raw, "html.parser")
    for idx, table in enumerate(soup.find_all("table")):
        text = " ".join(table.get_text(" ", strip=True).split())
        if "Batch" not in text and "CALA DEI MORI" not in text:
            continue
        print(f"TABLE={idx} TEXT={text[:900]}")
        rows = table.find_all("tr")
        for row_idx, row in enumerate(rows[:12]):
            cells = [" ".join(cell.get_text(" ", strip=True).split()) for cell in row.find_all(["th", "td"])]
            if cells:
                print(f"ROW={row_idx} CELLS={cells}")
        print("---")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
