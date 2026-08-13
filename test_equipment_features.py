#!/usr/bin/env python3
"""Offline regression tests for HKJC equipment parsing and profile backfill."""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from enrich_hkjc_equipment import init_schema, parse_profile, update_equipment
from equipment_features import equipment_feature_flags, parse_equipment


def main() -> int:
    assert parse_equipment("BO1/TT").active_codes == frozenset({"BO", "TT"})
    assert parse_equipment("BO1/TT").first_time_codes == frozenset({"BO"})
    assert parse_equipment("--").active_codes == frozenset()
    assert equipment_feature_flags("BO1/TT", "TT", True) == {
        "is_first_time_blinker": 1, "is_equip_added": 1, "equipment_changed": 1, "equipment_history_known_pre": 1,
    }
    assert equipment_feature_flags("BO-/TT", "BO/TT", True) == {
        "is_first_time_blinker": 0, "is_equip_added": 0, "equipment_changed": 1, "equipment_history_known_pre": 1,
    }
    assert equipment_feature_flags("TT", None, False)["equipment_changed"] == 0

    sample_html = Path("equipment_horse_sample.html")
    assert sample_html.exists(), "請先保留官方 HTML 測試樣本。"
    records = parse_profile(sample_html.read_text(encoding="utf-8"))
    assert ("2026-07-04", "ST", 9, "TT") in records
    assert ("2026-04-12", "ST", 6, "TT1") in records

    with tempfile.TemporaryDirectory(prefix="v10_equipment_test_") as temp:
        db = sqlite3.connect(Path(temp) / "test.sqlite")
        db.executescript(
            """
            CREATE TABLE starters(
              race_date TEXT, racecourse TEXT, race_no INTEGER, horse_name TEXT,
              horse_code TEXT, equipment TEXT, PRIMARY KEY(race_date,racecourse,race_no,horse_name)
            );
            """
        )
        db.execute("INSERT INTO starters VALUES(?,?,?,?,?,?)", ("2026-07-04", "ST", 9, "應龍飛影", "L083", None))
        init_schema(db)
        updated = update_equipment(db, "L083", "https://example.test/horse", records)
        assert updated == 1
        saved = db.execute("SELECT equipment FROM starters WHERE horse_code='L083'").fetchone()[0]
        assert saved == "TT"
        db.close()
    print('{"result":"PASS","equipment_parser":"PASS","profile_backfill":"PASS"}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
