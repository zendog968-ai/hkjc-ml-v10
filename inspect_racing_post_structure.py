#!/usr/bin/env python3
"""Inspect saved public Racing Post HTML only; does not fetch or bypass access controls."""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from bs4 import BeautifulSoup


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("html")
    args = parser.parse_args()
    soup = BeautifulSoup(Path(args.html).read_text(encoding="utf-8"), "html.parser")
    printed = 0
    for text in soup.find_all(string=re.compile(r"\bRPR\b", re.I)):
        parent = text.parent
        lineage = []
        for _ in range(18):
            if parent is None:
                break
            classes = ".".join(parent.get("class", []))
            lineage.append(f"{parent.name}:{classes}")
            snippet = " ".join(parent.get_text(" ", strip=True).split())
            if re.search(r"\bOR\b.*\bTS\b.*\bRPR\b", snippet) and any(token in snippet for token in ("J:", "T:", "yo")) and len(snippet) < 6000:
                print("LINEAGE", " > ".join(lineage))
                print("SNIPPET", snippet[:3000])
                print("ANCHORS", [(anchor.get_text(" ", strip=True), anchor.get("href")) for anchor in parent.find_all("a", href=True)][:12])
                print("---")
                printed += 1
                break
            parent = parent.parent
        if printed >= 5:
            break
    print(f"MATCHED_CONTAINERS={printed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
