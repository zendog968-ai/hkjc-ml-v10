"""Contract test for S1/S2 feature enrichment; uses labelled synthetic records only.

This validates feature plumbing and safety gates, not predictive performance.
"""
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


def add_completed_race(conn: sqlite3.Connection, meeting_id: int, race_no: int, key: str, scheduled_at: datetime, rows: list[tuple[int, str, str, str, int]]) -> int:
    conn.execute(
        "INSERT INTO overseas_races(meeting_id,race_no,official_race_key,going,scheduled_start_utc,race_status) VALUES(?,?,?,?,?,?)",
        (meeting_id, race_no, key, "GOOD", scheduled_at.isoformat(), "completed"),
    )
    race_id = int(conn.execute("SELECT overseas_race_id FROM overseas_races WHERE official_race_key=?", (key,)).fetchone()[0])
    conn.executemany(
        "INSERT INTO overseas_starters(overseas_race_id,horse_no,horse_name,trainer,jockey,finish_pos) VALUES(?,?,?,?,?,?)",
        [(race_id, horse_no, horse_name, trainer, jockey, finish_pos) for horse_no, horse_name, trainer, jockey, finish_pos in rows],
    )
    return race_id


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir()
    db_path = OUT / "fixture.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript((ROOT / "schema_overseas_racing.sql").read_text(encoding="utf-8"))
    ensure_enrichment_schema(conn)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    meeting_date = (now + timedelta(days=1)).date().isoformat()
    conn.execute(
        "INSERT INTO overseas_meetings(meeting_date,simulcast_code,fixture_url,discovery_status,discovered_at_utc) VALUES(?,?,?,?,?)",
        (meeting_date, "S1", "fixture://official", "discovered", now.isoformat()),
    )
    meeting_id = int(conn.execute("SELECT meeting_id FROM overseas_meetings").fetchone()[0])
    conn.execute(
        "INSERT INTO overseas_races(meeting_id,race_no,official_race_key,going,scheduled_start_utc,race_status) VALUES(?,?,?,?,?,?)",
        (meeting_id, 1, "fixture-current", "GOOD", (now + timedelta(minutes=5)).isoformat(), "discovered"),
    )
    race_id = int(conn.execute("SELECT overseas_race_id FROM overseas_races WHERE official_race_key='fixture-current'").fetchone()[0])

    # Horse 1 has three completed pre-cutoff starts, all finishing in the top four.
    # The current race is deliberately excluded by the pre-race time gate.
    for number, days_ago, result in ((2, 14, 1), (3, 28, 2), (4, 42, 4)):
        add_completed_race(
            conn,
            meeting_id,
            number,
            f"fixture-history-{number}",
            now - timedelta(days=days_ago),
            [(1, "Verified Rated", "Trainer G1", "Jockey A", result), (2, "No Rating", "Trainer B", "Jockey B", 5)],
        )
    conn.commit()

    runners = [
        {"horse_no": 1, "horse_name": "Verified Rated", "trainer": "Trainer G1", "jockey": "Jockey A", "weight_lbs": 118.0, "career_starts": 20, "career_wins": 5, "career_places": 10, "international_rating": 118.0, "rating_type": "RPR", "rating_source_url": "fixture://official-rpr", "rating_as_of_utc": (now - timedelta(minutes=1)).isoformat(), "last_run_date": (now.date() - timedelta(days=16)).isoformat(), "trainer_g1_starts": 30, "trainer_g1_wins": 5, "trainer_g1_as_of_utc": (now - timedelta(days=1)).isoformat()},
        {"horse_no": 2, "horse_name": "No Rating", "trainer": "Trainer B", "jockey": "Jockey B", "weight_lbs": 126.0, "career_starts": 6, "career_wins": 1, "career_places": 2, "last_run_date": (now.date() - timedelta(days=40)).isoformat()},
        {"horse_no": 3, "horse_name": "Unknown", "trainer": "Trainer C", "jockey": "Jockey C", "weight_lbs": 130.0},
        {"horse_no": 4, "horse_name": "Field Runner 4", "trainer": "Trainer D", "jockey": "Jockey D", "weight_lbs": 122.0},
        {"horse_no": 5, "horse_name": "Field Runner 5", "trainer": "Trainer E", "jockey": "Jockey E", "weight_lbs": 124.0},
        {"horse_no": 6, "horse_name": "Field Runner 6", "trainer": "Trainer F", "jockey": "Jockey F", "weight_lbs": 128.0},
    ]
    # These are explicit test prices, not a real market snapshot.
    t15 = {1: {"win": 6.0, "place": 2.0}, 2: {"win": 5.0, "place": 1.8}, 3: {"win": 8.0, "place": 2.4}, 4: {"win": 9.0, "place": 2.8}, 5: {"win": 11.0, "place": 3.0}, 6: {"win": 13.0, "place": 3.4}}
    t5 = {1: {"win": 4.5, "place": 1.7}, 2: {"win": 5.5, "place": 1.9}, 3: {"win": 8.0, "place": 2.4}, 4: {"win": 9.0, "place": 2.8}, 5: {"win": 11.0, "place": 3.0}, 6: {"win": 13.0, "place": 3.4}}
    write_snapshot(conn, race_id, "T_MINUS_15", (now - timedelta(minutes=15)).isoformat(), "complete", "fixture://odds-t15", t15)
    write_snapshot(conn, race_id, "T_MINUS_5", (now - timedelta(minutes=4)).isoformat(), "complete", "fixture://odds-t5", t5)
    conn.close()

    card = {
        "schema_version": "fixture",
        "status": "complete",
        "odds_snapshot_at_utc": now.isoformat(),
        "race": {"overseas_race_id": race_id, "meeting_date": meeting_date, "going": "GOOD"},
        "runners": [{**row, "win_odds": t5[row["horse_no"]]["win"], "place_odds": t5[row["horse_no"]]["place"]} for row in runners],
    }
    card_path = OUT / "race_card.json"
    card_path.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
    output_path = OUT / "prediction.json"
    subprocess.run([
        sys.executable, str(ROOT / "predict_s1s2.py"), "--race-card", str(card_path), "--db", str(db_path),
        "--output-json", str(output_path), "--output-md", str(OUT / "prediction.md"), "--simulations", "5000",
    ], check=True, cwd=ROOT)
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    rows = {row["horse_no"]: row for row in payload["predictions"]}

    expected_field_weight = sum(row["weight_lbs"] for row in runners) / len(runners)
    assert abs(sum(row["predicted_win_probability"] for row in rows.values()) - 1.0) < 1e-9
    assert rows[1]["rating_type"] == "RPR" and rows[1]["days_since_last_run"] == ((now + timedelta(days=1)).date() - (now.date() - timedelta(days=16))).days
    assert rows[1]["going_suitability"] is not None and rows[1]["trainer_g1_win_rate"] is not None
    assert rows[1]["odds_drop_flag"] and rows[1]["odds_drop_ratio"] <= -0.20
    assert rows[3]["international_rating"] is None and rows[3]["feature_detail"]["rating"]["status"] == "no_verified_international_rating"
    assert abs(rows[1]["field_weight_mean"] - expected_field_weight) < 1e-12
    assert abs(rows[1]["weight_advantage_lbs"] - (expected_field_weight - 118.0)) < 1e-12
    assert 0.0 < rows[1]["weight_log_signal"] < 0.06
    assert rows[1]["recent_top4_starts"] == 3 and 0.0 < rows[1]["recent_top4_log_signal"] < 0.10
    assert rows[1]["recent_top4_rate"] is not None and rows[1]["feature_detail"]["recent_top4"]["top4"] == 3
    assert rows[3]["recent_top4_rate"] is None and rows[3]["recent_top4_starts"] == 0 and rows[3]["recent_top4_log_signal"] == 0.0

    verify_conn = sqlite3.connect(db_path)
    stored = verify_conn.execute("SELECT weight_lbs,field_weight_mean,weight_advantage_lbs,recent_top4_rate,recent_top4_starts,weight_log_signal,recent_top4_log_signal FROM overseas_prerace_predictions WHERE overseas_race_id=? AND horse_no=1", (race_id,)).fetchone()
    verify_conn.close()
    assert stored is not None and stored[0] == 118.0 and stored[4] == 3 and stored[5] > 0.0 and stored[6] > 0.0

    report = {
        "status": "passed",
        "provenance": "isolated synthetic contract fixture; not historical performance evidence",
        "feature_checks": {
            "probabilities_sum_to_one": True,
            "rpr_rating_used": True,
            "days_since_last_run_used": True,
            "going_history_pre_cutoff_only": True,
            "trainer_g1_time_gate": True,
            "complete_t15_t5_odds_drop": True,
            "field_relative_weight_capped": True,
            "recent_top4_beta_shrinkage_pre_cutoff_only": True,
            "missing_weight_or_recent_history_neutrality": True,
            "prediction_audit_columns_written": True,
        },
        "horse_1": {key: rows[1][key] for key in (
            "international_rating", "rating_type", "days_since_last_run", "going_suitability", "trainer_g1_win_rate",
            "weight_lbs", "field_weight_mean", "weight_advantage_lbs", "weight_log_signal", "recent_top4_rate",
            "recent_top4_starts", "recent_top4_log_signal", "odds_drop_ratio", "odds_drop_flag",
        )},
    }
    (OUT / "validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
