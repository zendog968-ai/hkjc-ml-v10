#!/usr/bin/env python3
"""Regression checks for the isolated public overseas deep-data scraper."""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from fetch_overseas_deep_data import parse_atr, parse_racing_post, persist, score_rows

ROOT = Path(__file__).resolve().parent
RP_HTML = """
<html><body><div class="runner"><a href="/profile/horse/1/dubai-bling/">Dubai Bling IRE</a>
1 ( 17 ) Dubai Bling IRE 18 p b g 4 yo J: Oisin Murphy T: Hugo Palmer OR : 104 TS : 91 RPR : 113</div>
<div class="runner"><a href="/profile/horse/2/stratusnine/">Stratusnine IRE</a>
2 ( 16 ) Stratusnine IRE 18 b g 4 yo J: James Doyle T: Hugo Palmer OR : 104 TS : 94 RPR : 111</div>
<h1>Hong Kong Jockey Club World Pool Handicap</h1><p>5½f</p><p>Going: Good</p><p>Runners: 22</p><p>Class 2</p></body></html>
"""
ATR_HTML = """
<html><body><div class="horse"><a href="/form/horse/Dubai-Bling/IRE/1">Dubai Bling (IRE)</a>
Dubai Bling (IRE) 18 b g Dark Angel - Millicent Fawcett (Kingman)
Distance: 4 runs, 1 win, 2 places, 25% Similar Going: 12 runs, 3 wins, 4 places, 25%</div></body></html>
"""


def main() -> int:
    race, rp = parse_racing_post(RP_HTML)
    atr = parse_atr(ATR_HTML)
    assert race["going"] == "Good" and race["declared_runners"] == 22
    assert rp["dubaibling"]["horse_name"] == "Dubai Bling"
    assert rp["dubaibling"]["racing_post_rating"] == 113
    assert rp["stratusnine"]["top_speed_rating"] == 94
    assert atr["dubaibling"]["sire"] == "Dark Angel"
    assert atr["dubaibling"]["distance_wins"] == 1
    rows = [{**rp["dubaibling"], **atr["dubaibling"], "hkjc_win_odds": None, "hkjc_place_odds": None, "data_completeness": "partial", "source_form_url": "https://atr.example"}, {**rp["stratusnine"], "hkjc_win_odds": None, "hkjc_place_odds": None, "data_completeness": "partial", "source_form_url": "https://atr.example"}]
    score_rows(rows)
    assert {row["deep_rank"] for row in rows} == {1, 2}
    with tempfile.TemporaryDirectory() as temp_dir:
        db = Path(temp_dir) / "overseas.sqlite"
        payload = {
            "scrape_run": {"meeting_date": "2026-08-19", "simulcast_code": "S1", "race_no": 1, "venue": "York", "status": "complete", "n6_status": "disabled_non_hk", "fetched_at_utc": "2026-08-19T08:00:00+00:00", "racing_post_url": "https://rp.example", "at_the_races_url": "https://atr.example", "timeform_url": "https://tf.example", "hkjc_odds_source": "https://hkjc.example", "source_notes": "fixture only"},
            "race": {"meeting_date": "2026-08-19", "simulcast_code": "S1", "race_no": 1, "venue": "York", "local_start_time": "13:50 BST", "hkt_start_time": "20:50 HKT", "race_name": "Fixture", "distance_text": "5½f", "going": "Good", "race_class": "Class 2", "declared_runners": 2, "source_status": "complete"},
            "starters": rows,
        }
        persist(db, ROOT / "schema_overseas_deep_racing.sql", payload)
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT COUNT(*) FROM s1_starters").fetchone()[0] == 2
        statuses = dict(conn.execute("SELECT field_name, availability FROM s1_source_field_status WHERE s1_starter_id = 1"))
        assert statuses["racing_post_rating"] == "available_public"
        assert statuses["timeform_pace_setup"] == "unavailable_paid_or_restricted"
        conn.close()
    print("PASS: overseas deep scraper public fields, N6 isolation and SQLite provenance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
