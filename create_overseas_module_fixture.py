#!/usr/bin/env python3
"""Create a clearly synthetic offline fixture for module-contract tests only."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "overseas_module_fixture"
DB = OUT / "fixture.sqlite"


def main() -> int:
    OUT.mkdir(exist_ok=True)
    if DB.exists():
        DB.unlink()
    conn = sqlite3.connect(DB)
    conn.executescript((ROOT / "schema_overseas_racing.sql").read_text(encoding="utf-8"))
    conn.execute("INSERT INTO overseas_meetings(meeting_date,simulcast_code,meeting_name,location,fixture_url,summary_url,discovery_status,discovered_at_utc) VALUES(?,?,?,?,?,?,?,?)", ("2026-01-10", "S1", "SYNTHETIC MODULE TEST ONLY", "Test", "fixture://test", "summary://test", "race_count_verified", "2026-01-10T00:00:00+00:00"))
    meeting_id = conn.execute("SELECT meeting_id FROM overseas_meetings").fetchone()[0]
    conn.execute("INSERT INTO overseas_races(meeting_id,race_no,official_race_key,race_status,racecard_url,result_url) VALUES(?,?,?,?,?,?)", (meeting_id, 1, "2026-01-10:S1:1", "completed", "fixture://card", "fixture://result"))
    race_id = conn.execute("SELECT overseas_race_id FROM overseas_races").fetchone()[0]
    runners = [
        (1, "TEST ALPHA", 20, 5, 10, 1), (2, "TEST BRAVO", 10, 1, 3, 2), (3, "TEST CHARLIE", 5, 0, 1, 3),
        (4, "TEST DELTA", None, None, None, 4), (5, "TEST ECHO", 16, 2, 7, 5), (6, "TEST FOXTROT", 8, 0, 2, 6),
    ]
    for horse_no, horse_name, starts, wins, places, finish in runners:
        conn.execute("INSERT INTO overseas_starters(overseas_race_id,horse_no,horse_name,career_starts,career_wins,career_places,finish_pos,finish_pos_text,margin_text,finish_time) VALUES(?,?,?,?,?,?,?,?,?,?)", (race_id, horse_no, horse_name, starts, wins, places, finish, str(finish), "0" if finish == 1 else f"{finish - 1}.0", "1:10.00"))
    conn.execute("INSERT INTO overseas_dividends(overseas_race_id,pool_name,winning_combination,dividend_hkd,source_url) VALUES(?,?,?,?,?)", (race_id, "WIN", "1", 36.5, "fixture://dividend"))
    conn.commit()
    conn.close()
    card = {"schema_version": "fixture_only", "label": "🌍 海外轉播賽 (S1/S2) — SYNTHETIC TEST", "race": {"meeting_date": "2026-01-10", "simulcast_code": "S1", "race_no": 1, "overseas_race_id": race_id}, "status": "complete", "runners": [{"horse_no": h, "horse_name": name, "career_starts": s, "career_wins": w, "career_places": p, "win_odds": odds, "place_odds": place} for (h, name, s, w, p, _), odds, place in zip(runners, [3.6, 8.0, 16.0, None, 5.2, 30.0], [1.4, 2.5, 4.0, None, 1.8, 8.0])], "warnings": ["SYNTHETIC TEST DATA ONLY"]}
    (OUT / "s1s2_race_card_fixture.json").write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"db": str(DB), "race_card": str(OUT / "s1s2_race_card_fixture.json"), "notice": "synthetic fixture only"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
