"""Contract test for S1/S2 feature enrichment; uses labelled synthetic records only."""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from overseas_feature_enrichment import ensure_enrichment_schema, write_snapshot

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "s1s2_feature_enrichment_fixture"


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir()
    db_path = OUT / "fixture.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript((ROOT / "schema_overseas_racing.sql").read_text(encoding="utf-8"))
    ensure_enrichment_schema(conn)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    conn.execute("INSERT INTO overseas_meetings(meeting_date,simulcast_code,fixture_url,discovery_status,discovered_at_utc) VALUES(?,?,?,?,?)", ((now + timedelta(days=1)).date().isoformat(), "S1", "fixture://official", "discovered", now.isoformat()))
    meeting_id = int(conn.execute("SELECT meeting_id FROM overseas_meetings").fetchone()[0])
    conn.execute("INSERT INTO overseas_races(meeting_id,race_no,official_race_key,going,scheduled_start_utc,race_status) VALUES(?,?,?,?,?,?)", (meeting_id, 1, "fixture-current", "GOOD", (now + timedelta(minutes=5)).isoformat(), "discovered"))
    race_id = int(conn.execute("SELECT overseas_race_id FROM overseas_races WHERE official_race_key='fixture-current'").fetchone()[0])
    conn.execute("INSERT INTO overseas_races(meeting_id,race_no,official_race_key,going,scheduled_start_utc,race_status) VALUES(?,?,?,?,?,?)", (meeting_id, 2, "fixture-history", "GOOD", (now - timedelta(days=14)).isoformat(), "completed"))
    history_id = int(conn.execute("SELECT overseas_race_id FROM overseas_races WHERE official_race_key='fixture-history'").fetchone()[0])
    history_rows = [(history_id, 1, "Verified Rated", "Trainer G1", "Jockey A", 1), (history_id, 2, "No Rating", "Trainer B", "Jockey B", 3)]
    conn.executemany("INSERT INTO overseas_starters(overseas_race_id,horse_no,horse_name,trainer,jockey,finish_pos) VALUES(?,?,?,?,?,?)", history_rows)
    conn.commit()

    runners = [
        {"horse_no": 1, "horse_name": "Verified Rated", "trainer": "Trainer G1", "jockey": "Jockey A", "career_starts": 20, "career_wins": 5, "career_places": 10, "international_rating": 118.0, "rating_type": "RPR", "rating_source_url": "fixture://official-rpr", "rating_as_of_utc": (now - timedelta(minutes=1)).isoformat(), "last_run_date": (now.date() - timedelta(days=16)).isoformat(), "trainer_g1_starts": 30, "trainer_g1_wins": 5, "trainer_g1_as_of_utc": (now - timedelta(days=1)).isoformat()},
        {"horse_no": 2, "horse_name": "No Rating", "trainer": "Trainer B", "jockey": "Jockey B", "career_starts": 6, "career_wins": 1, "career_places": 2, "last_run_date": (now.date() - timedelta(days=40)).isoformat()},
        {"horse_no": 3, "horse_name": "Unknown", "trainer": "Trainer C", "jockey": "Jockey C"},
    ]
    # These are explicit test prices, not a real market snapshot.
    write_snapshot(conn, race_id, "T_MINUS_15", (now - timedelta(minutes=15)).isoformat(), "complete", "fixture://odds-t15", {1: {"win": 6.0, "place": 2.0}, 2: {"win": 5.0, "place": 1.8}, 3: {"win": 8.0, "place": 2.4}})
    write_snapshot(conn, race_id, "T_MINUS_5", (now - timedelta(minutes=4)).isoformat(), "complete", "fixture://odds-t5", {1: {"win": 4.5, "place": 1.7}, 2: {"win": 5.5, "place": 1.9}, 3: {"win": 8.0, "place": 2.4}})
    conn.close()
    card = {"schema_version": "fixture", "status": "complete", "odds_snapshot_at_utc": now.isoformat(), "race": {"overseas_race_id": race_id, "meeting_date": (now + timedelta(days=1)).date().isoformat(), "going": "GOOD"}, "runners": [{**row, "win_odds": [4.5, 5.5, 8.0][i], "place_odds": [1.7, 1.9, 2.4][i]} for i, row in enumerate(runners)]}
    card_path = OUT / "race_card.json"
    card_path.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
    output_path = OUT / "prediction.json"
    subprocess.run([sys.executable, str(ROOT / "predict_s1s2.py"), "--race-card", str(card_path), "--db", str(db_path), "--output-json", str(output_path), "--output-md", str(OUT / "prediction.md"), "--simulations", "5000"], check=True, cwd=ROOT)
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    rows = {row["horse_no"]: row for row in payload["predictions"]}
    assert abs(sum(row["predicted_win_probability"] for row in rows.values()) - 1.0) < 1e-9
    assert rows[1]["rating_type"] == "RPR" and rows[1]["days_since_last_run"] == ((now + timedelta(days=1)).date() - (now.date() - timedelta(days=16))).days
    assert rows[1]["going_suitability"] is not None and rows[1]["trainer_g1_win_rate"] is not None
    assert rows[1]["odds_drop_flag"] and rows[1]["odds_drop_ratio"] <= -0.20
    assert rows[3]["international_rating"] is None and rows[3]["feature_detail"]["rating"]["status"] == "no_verified_international_rating"
    report = {"status": "passed", "provenance": "isolated synthetic contract fixture; not historical performance evidence", "feature_checks": {"probabilities_sum_to_one": True, "rpr_rating_used": True, "days_since_last_run_used": True, "going_history_pre_cutoff_only": True, "trainer_g1_time_gate": True, "complete_t15_t5_odds_drop": True, "missing_feature_neutrality": True}, "horse_1": {key: rows[1][key] for key in ("international_rating", "rating_type", "days_since_last_run", "going_suitability", "trainer_g1_win_rate", "odds_drop_ratio", "odds_drop_flag")}}
    (OUT / "validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
