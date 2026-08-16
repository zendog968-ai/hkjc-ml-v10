"""Optional, source-gated S1/S2 feature enrichment.

This module never invents missing international ratings, last-run dates, going
history, G1 statistics or odds snapshots.  Each signal is neutral unless its
source is structured, time-valid and identifiable.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, datetime
from typing import Any

ALLOWED_RATING_TYPES = {"RPR", "IFHA", "WORLD_RATING", "INTERNATIONAL_RATING"}


def _add_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, sql_type in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")


def ensure_enrichment_schema(conn: sqlite3.Connection) -> None:
    _add_columns(conn, "overseas_starters", {
        "international_rating": "REAL", "rating_type": "TEXT", "rating_source_url": "TEXT", "rating_as_of_utc": "TEXT",
        "last_run_date": "TEXT", "going_history_json": "TEXT", "trainer_g1_starts": "INTEGER", "trainer_g1_wins": "INTEGER", "trainer_g1_as_of_utc": "TEXT",
    })
    _add_columns(conn, "overseas_prerace_predictions", {
        "international_rating": "REAL", "rating_type": "TEXT", "days_since_last_run": "INTEGER", "going_suitability": "REAL",
        "trainer_g1_win_rate": "REAL", "odds_drop_ratio": "REAL", "odds_drop_weight": "REAL", "weight_lbs": "REAL",
        "field_weight_mean": "REAL", "weight_advantage_lbs": "REAL", "recent_top4_rate": "REAL",
        "recent_top4_starts": "INTEGER", "weight_log_signal": "REAL", "recent_top4_log_signal": "REAL", "feature_detail_json": "TEXT",
    })
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS overseas_odds_snapshots (
        snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
        overseas_race_id INTEGER NOT NULL,
        snapshot_label TEXT NOT NULL CHECK(snapshot_label IN ('T_MINUS_15','T_MINUS_5','OTHER')),
        captured_at_utc TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('complete','degraded','unavailable')),
        source_url TEXT NOT NULL,
        source_document_id INTEGER,
        UNIQUE(overseas_race_id, snapshot_label, captured_at_utc)
    );
    CREATE TABLE IF NOT EXISTS overseas_odds_snapshot_runners (
        snapshot_id INTEGER NOT NULL,
        horse_no INTEGER NOT NULL,
        win_odds REAL,
        place_odds REAL,
        PRIMARY KEY(snapshot_id, horse_no)
    );
    CREATE INDEX IF NOT EXISTS idx_overseas_odds_snapshot_race ON overseas_odds_snapshots(overseas_race_id, snapshot_label, captured_at_utc);
    """)
    conn.commit()


def parse_iso_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip().replace("/", "-")
    for fmt in ("%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return None


def days_since_last_run(last_run_date: Any, race_date: Any) -> int | None:
    last_run = parse_iso_date(last_run_date)
    current = parse_iso_date(race_date)
    if last_run is None or current is None or last_run >= current:
        return None
    return (current - last_run).days


def _past_going_stats(conn: sqlite3.Connection, horse_name: str, race_going: str | None, as_of_utc: str) -> tuple[float, float] | None:
    if not horse_name or not race_going:
        return None
    row = conn.execute("""
        SELECT COUNT(*) AS starts, COALESCE(SUM(CASE WHEN s.finish_pos=1 THEN 1 ELSE 0 END),0) AS wins
        FROM overseas_starters AS s JOIN overseas_races AS r ON r.overseas_race_id=s.overseas_race_id
        WHERE s.horse_name=? AND r.going=? AND r.race_status='completed'
          AND r.scheduled_start_utc IS NOT NULL AND r.scheduled_start_utc < ?
          AND s.finish_pos IS NOT NULL AND s.withdrawal_status IS NULL
    """, (horse_name, race_going, as_of_utc)).fetchone()
    if not row or int(row[0] or 0) == 0:
        return None
    return float(row[0]), float(row[1])


def going_suitability(conn: sqlite3.Connection, runner: dict[str, Any], race_going: str | None, field_size: int, as_of_utc: str) -> tuple[float | None, float, dict[str, Any]]:
    stats = _past_going_stats(conn, str(runner.get("horse_name") or ""), race_going, as_of_utc)
    if stats is None:
        return None, 0.0, {"status": "no_pre_cutoff_structured_going_history"}
    starts, wins = stats
    base = 1.0 / max(field_size, 2)
    posterior = (wins + base * 16.0) / (starts + 16.0)
    evidence = starts / (starts + 16.0)
    suitability = posterior - base
    log_signal = max(-0.12, min(0.12, math.log(max(posterior, 1e-5) / base) * evidence * 0.25))
    return suitability, log_signal, {"starts": starts, "wins": wins, "posterior_rate": posterior, "evidence": evidence, "as_of_utc": as_of_utc}


def weight_log_signal(runner: dict[str, Any], field_weight_mean: float | None) -> tuple[float, float | None, dict[str, Any]]:
    """Return a small, field-relative weight signal from an official race-card weight.

    This is deliberately capped because a single overseas result must not turn
    low weight into a dominant determinant. It is neutral when the field mean
    or runner weight is unavailable.
    """
    weight = runner.get("weight_lbs")
    if not isinstance(weight, (int, float)) or not math.isfinite(float(weight)) or field_weight_mean is None:
        return 0.0, None, {"status": "no_verified_field_relative_weight"}
    advantage = float(field_weight_mean) - float(weight)
    signal = max(-0.06, min(0.06, advantage / 10.0 * 0.04))
    return signal, advantage, {"weight_lbs": float(weight), "field_weight_mean": float(field_weight_mean), "advantage_lbs": advantage, "formula": "clip((field_mean-weight)/10*0.04, -0.06, 0.06)"}


def _recent_top4_stats(conn: sqlite3.Connection, horse_name: str, as_of_utc: str, max_starts: int = 5) -> tuple[float, float] | None:
    if not horse_name:
        return None
    rows = conn.execute("""
        SELECT s.finish_pos
        FROM overseas_starters AS s
        JOIN overseas_races AS r ON r.overseas_race_id=s.overseas_race_id
        WHERE s.horse_name=? AND r.race_status='completed'
          AND r.scheduled_start_utc IS NOT NULL AND r.scheduled_start_utc < ?
          AND s.finish_pos IS NOT NULL AND s.withdrawal_status IS NULL
        ORDER BY r.scheduled_start_utc DESC
        LIMIT ?
    """, (horse_name, as_of_utc, max_starts)).fetchall()
    if not rows:
        return None
    starts = float(len(rows))
    top4 = float(sum(1 for row in rows if isinstance(row[0], int) and 1 <= int(row[0]) <= 4))
    return starts, top4


def recent_top4_log_signal(conn: sqlite3.Connection, runner: dict[str, Any], field_size: int, as_of_utc: str) -> tuple[float | None, int, float, dict[str, Any]]:
    """Use only the latest five completed overseas starts before model time.

    A Beta-style prior centred on 4/field_size prevents one recent placing from
    becoming an overconfident signal. Until archive coverage exists it remains
    exactly neutral.
    """
    stats = _recent_top4_stats(conn, str(runner.get("horse_name") or ""), as_of_utc)
    if stats is None:
        return None, 0, 0.0, {"status": "no_pre_cutoff_recent_top4_history"}
    starts, top4 = stats
    base = min(4.0 / max(field_size, 4), 1.0)
    prior_strength = 12.0
    posterior = (top4 + base * prior_strength) / (starts + prior_strength)
    evidence = starts / (starts + prior_strength)
    signal = max(-0.10, min(0.10, math.log(max(posterior, 1e-5) / base) * evidence * 0.16))
    return posterior, int(starts), signal, {"starts": int(starts), "top4": int(top4), "base_top4_rate": base, "posterior_top4_rate": posterior, "evidence": evidence, "max_history_starts": 5, "as_of_utc": as_of_utc}


def rating_log_signal(runner: dict[str, Any], field_rating_mean: float | None, model_as_of_utc: str) -> tuple[float, dict[str, Any]]:
    rating = runner.get("international_rating")
    rating_type = str(runner.get("rating_type") or "").upper()
    source_url = runner.get("rating_source_url")
    as_of_utc = runner.get("rating_as_of_utc")
    if not isinstance(rating, (int, float)) or not math.isfinite(float(rating)) or rating_type not in ALLOWED_RATING_TYPES or field_rating_mean is None or not isinstance(source_url, str) or not source_url or not isinstance(as_of_utc, str) or not as_of_utc or as_of_utc > model_as_of_utc:
        return 0.0, {"status": "no_verified_international_rating"}
    z = (float(rating) - field_rating_mean) / 10.0
    return max(-0.18, min(0.18, z * 0.10)), {"rating": float(rating), "rating_type": rating_type, "field_mean": field_rating_mean, "source_url": source_url, "as_of_utc": as_of_utc}


def trainer_g1_log_signal(runner: dict[str, Any], as_of_utc: str) -> tuple[float | None, float, dict[str, Any]]:
    starts = runner.get("trainer_g1_starts")
    wins = runner.get("trainer_g1_wins")
    timestamp = runner.get("trainer_g1_as_of_utc")
    if not isinstance(starts, (int, float)) or not isinstance(wins, (int, float)) or starts <= 0 or wins < 0 or wins > starts or not isinstance(timestamp, str) or timestamp > as_of_utc:
        return None, 0.0, {"status": "no_pre_cutoff_verified_g1_statistics"}
    rate = (float(wins) + 1.0) / (float(starts) + 20.0)
    evidence = float(starts) / (float(starts) + 20.0)
    # Reference 10% to prevent ordinary all-race strike rate masquerading as G1 skill.
    return rate, max(-0.08, min(0.08, math.log(max(rate, 1e-5) / 0.10) * evidence * 0.12)), {"starts": float(starts), "wins": float(wins), "rate": rate, "evidence": evidence, "as_of_utc": timestamp}


def odds_drop_ratios(conn: sqlite3.Connection, overseas_race_id: int, as_of_utc: str, max_model_lag_seconds: int = 300) -> dict[int, dict[str, Any]]:
    snapshots: dict[str, sqlite3.Row] = {}
    conn.row_factory = sqlite3.Row
    for label in ("T_MINUS_15", "T_MINUS_5"):
        row = conn.execute("""SELECT * FROM overseas_odds_snapshots WHERE overseas_race_id=? AND snapshot_label=? AND status='complete' ORDER BY captured_at_utc DESC LIMIT 1""", (overseas_race_id, label)).fetchone()
        if row:
            snapshots[label] = row
    if set(snapshots) != {"T_MINUS_15", "T_MINUS_5"} or snapshots["T_MINUS_5"]["captured_at_utc"] <= snapshots["T_MINUS_15"]["captured_at_utc"]:
        return {}
    try:
        model_at = datetime.fromisoformat(as_of_utc.replace("Z", "+00:00"))
        t5_at = datetime.fromisoformat(str(snapshots["T_MINUS_5"]["captured_at_utc"]).replace("Z", "+00:00"))
    except ValueError:
        return {}
    lag = (model_at - t5_at).total_seconds()
    if lag < 0 or lag > max_model_lag_seconds:
        return {}
    t15 = {row[0]: row[1] for row in conn.execute("SELECT horse_no,win_odds FROM overseas_odds_snapshot_runners WHERE snapshot_id=?", (snapshots["T_MINUS_15"]["snapshot_id"],))}
    t5 = {row[0]: row[1] for row in conn.execute("SELECT horse_no,win_odds FROM overseas_odds_snapshot_runners WHERE snapshot_id=?", (snapshots["T_MINUS_5"]["snapshot_id"],))}
    ratios: dict[int, dict[str, Any]] = {}
    for horse_no in sorted(set(t15).intersection(t5)):
        before, after = t15[horse_no], t5[horse_no]
        if not isinstance(before, (int, float)) or not isinstance(after, (int, float)) or before <= 1 or after <= 1:
            continue
        ratio = (float(after) - float(before)) / float(before)
        ratios[int(horse_no)] = {"ratio": ratio, "t15": float(before), "t5": float(after), "t15_at_utc": snapshots["T_MINUS_15"]["captured_at_utc"], "t5_at_utc": snapshots["T_MINUS_5"]["captured_at_utc"]}
    return ratios


def feature_enrichment(conn: sqlite3.Connection, race: dict[str, Any], runner: dict[str, Any], field_size: int, as_of_utc: str, field_rating_mean: float | None, field_weight_mean: float | None, odds_drop: dict[str, Any] | None, odds_drop_weight: float) -> dict[str, Any]:
    day_rest = days_since_last_run(runner.get("last_run_date"), race.get("meeting_date"))
    suitability, going_signal, going_detail = going_suitability(conn, runner, race.get("going"), field_size, as_of_utc)
    rating_signal, rating_detail = rating_log_signal(runner, field_rating_mean, as_of_utc)
    weight_signal, weight_advantage, weight_detail = weight_log_signal(runner, field_weight_mean)
    recent_top4_rate, recent_top4_starts, recent_top4_signal, recent_top4_detail = recent_top4_log_signal(conn, runner, field_size, as_of_utc)
    g1_rate, g1_signal, g1_detail = trainer_g1_log_signal(runner, as_of_utc)
    drop_ratio = odds_drop.get("ratio") if odds_drop else None
    drop_qualifies = drop_ratio is not None and drop_ratio <= -0.20
    # Layoff is recorded but not assigned a default direction until a time-ordered
    # overseas calibration set is available; arbitrary penalty would be worse than neutral.
    layoff_signal = 0.0
    drop_signal = float(odds_drop_weight) if drop_qualifies else 0.0
    total_signal = max(-0.30, min(0.30, rating_signal + going_signal + g1_signal + layoff_signal + weight_signal + recent_top4_signal + drop_signal))
    return {
        "log_strength_signal": total_signal,
        "international_rating": runner.get("international_rating"), "rating_type": runner.get("rating_type"),
        "days_since_last_run": day_rest, "going_suitability": suitability, "trainer_g1_win_rate": g1_rate,
        "weight_lbs": runner.get("weight_lbs"), "field_weight_mean": field_weight_mean, "weight_advantage_lbs": weight_advantage,
        "recent_top4_rate": recent_top4_rate, "recent_top4_starts": recent_top4_starts, "weight_log_signal": weight_signal, "recent_top4_log_signal": recent_top4_signal,
        "odds_drop_ratio": drop_ratio, "odds_drop_flag": drop_qualifies, "odds_drop_weight": float(odds_drop_weight) if drop_qualifies else 0.0,
        "detail": {"rating": rating_detail, "going": going_detail, "trainer_g1": g1_detail, "weight": weight_detail, "recent_top4": recent_top4_detail, "layoff": {"days_since_last_run": day_rest, "log_signal": layoff_signal, "status": "recorded_not_directionally_weighted_until_calibrated"}, "odds_drop": odds_drop or {"status": "no_complete_t15_t5_pair"}, "total_log_strength_signal": total_signal},
    }


def write_snapshot(conn: sqlite3.Connection, overseas_race_id: int, snapshot_label: str, captured_at_utc: str, status: str, source_url: str, odds_by_horse: dict[int, dict[str, Any]]) -> int:
    ensure_enrichment_schema(conn)
    cur = conn.execute("""INSERT INTO overseas_odds_snapshots(overseas_race_id,snapshot_label,captured_at_utc,status,source_url) VALUES(?,?,?,?,?)""", (overseas_race_id, snapshot_label, captured_at_utc, status, source_url))
    snapshot_id = int(cur.lastrowid)
    for horse_no, values in odds_by_horse.items():
        conn.execute("INSERT INTO overseas_odds_snapshot_runners(snapshot_id,horse_no,win_odds,place_odds) VALUES(?,?,?,?)", (snapshot_id, int(horse_no), values.get("win"), values.get("place")))
    conn.commit()
    return snapshot_id


def decode_source_fields(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    value = row["source_fields_json"] if isinstance(row, sqlite3.Row) else row.get("source_fields_json")
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}
