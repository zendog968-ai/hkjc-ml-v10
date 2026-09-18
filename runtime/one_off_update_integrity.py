#!/usr/bin/env python3
"""Validate post-update V10 artifacts without modifying them."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = [
    "hkjc_last_season.sqlite",
    "hkjc_last_season.csv",
    "horse_model.pkl",
    "lightgbm_training_report.json",
    "v101_quality_report.json",
]


def main() -> int:
    missing = [name for name in REQUIRED if not (ROOT / name).is_file()]
    conn = sqlite3.connect(ROOT / "hkjc_last_season.sqlite")
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
    conn.close()
    quality = json.loads((ROOT / "v101_quality_report.json").read_text(encoding="utf-8"))
    result = {
        "missing_artifacts": missing,
        "sqlite_integrity_check": integrity,
        "foreign_key_violations": len(foreign_keys),
        "v101_all_checks_passed": quality.get("all_checks_passed"),
        "v101_feature_rows": quality.get("feature_rows"),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not missing and integrity == "ok" and not foreign_keys and quality.get("all_checks_passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
