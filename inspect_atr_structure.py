#!/usr/bin/env python3
"""Inspect saved public At The Races HTML only; no network access."""
from __future__ import annotations

import argparse
from pathlib import Path

from bs4 import BeautifulSoup


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("html")
    args = parser.parse_args()
    soup = BeautifulSoup(Path(args.html).read_text(encoding="utf-8"), "html.parser")
    printed = 0
    for anchor in soup.find_all("a", href=True):
        if "/form/horse/" not in str(anchor.get("href")):
            continue
        name = anchor.get_text(" ", strip=True)
        if not name:
            continue
        parent = anchor.parent
        for _ in range(16):
            if parent is None:
                break
            text = " ".join(parent.get_text(" ", strip=True).split())
            if "Distance:" in text and "Similar Going:" in text and len(text) < 12000:
                print("HORSE", name)
                print("LINEAGE", parent.name, ".".join(parent.get("class", [])))
                print("TEXT", text[:4000])
                print("---")
                printed += 1
                break
            parent = parent.parent
        if printed >= 3:
            break
    print(f"MATCHED_CONTAINERS={printed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
