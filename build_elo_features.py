#!/usr/bin/env python3
"""Build leakage-safe pre-race ELO and late-closing proxy features from HKJC results.

The HKJC result page provides positions at calls and official finish times, but not a
per-horse electronic final-400m time. `closing400_proxy_pre` is therefore a transparent
position-and-margin based late-closing proxy, not a measured individual sectional time.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from statistics import mean
from typing import Iterable, Optional

from equipment_features import equipment_feature_flags
from v102_feature_utils import (
    body_weight_features, cold_start_prior_score, distance_match_prior,
    is_new_horse_from_prior_starts, trial_prior,
)

WITHDRAWN = {"WV", "WV-A", "WX-A", "WXNR"}
INITIAL_ELO = 1500.0
INITIAL_CLOSING = 50.0
HORSE_K = 28.0
JOCKEY_K = 15.0
CONDITION_K = 18.0
# Track-bias guardrails: sparse context samples must be close to neutral (1.0).
TRACK_BIAS_PRIOR_RUNNERS = 48.0
TRACK_BIAS_MAX_DEVIATION = 0.25
FEATURE_COLUMNS = (
    "race_date", "racecourse", "race_no", "horse_name", "horse_code", "jockey", "trainer",
    "race_class", "distance_m", "surface", "course_config", "going", "field_size", "draw",
    "draw_pct", "weight_lbs", "weight_delta", "horse_body_weight_pre", "horse_body_weight_known_pre",
    "body_weight_delta_pre", "body_weight_delta_known_pre", "is_extreme_body_weight_change_pre",
    "is_new_horse", "pedigree_distance_match_pre", "pedigree_prior_known_pre", "trial_prior_known_pre",
    "latest_trial_position_pre", "latest_trial_margin_pre", "latest_trial_qualified_pre", "cold_start_prior_pre",
    "horse_elo_pre", "horse_condition_elo_pre",
    "jockey_elo_pre", "trainer_win_rate_pre", "horse_win_rate_pre", "horse_top3_rate_pre",
    "condition_win_rate_pre", "recent_finish_fraction_pre", "recent_margin_pre", "recent_win_rate_pre",
    "closing400_proxy_pre", "closing400_trend_pre", "elo_vs_field", "jockey_elo_vs_field",
    "track_bias_pre", "track_bias_sample_pre", "class_level", "class_drop_from_last_pre",
    "class_weight_interaction_pre", "equipment_raw", "previous_equipment_raw_pre",
    "is_first_time_blinker", "is_equip_added", "equipment_changed", "equipment_history_known_pre",
    "trainer_equip_change_roi_pre", "trainer_equip_change_sample_pre",
    "target_win", "target_top3", "finish_pos", "finish_pos_text", "actual_closing400_proxy",
    "source_version",
)
INSERT_FEATURE_SQL = (
    "INSERT INTO elo_feature_store (" + ",".join(FEATURE_COLUMNS) + ") VALUES ("
    + ",".join("?" for _ in FEATURE_COLUMNS) + ")"
)


@dataclass
class HistoricalRun:
    finish_fraction: float
    margin_lengths: float
    weight_lbs: float
    closing_proxy: float
    class_level: int
    win: int
    top3: int
    equipment_raw: str | None
    body_weight_lbs: float | None


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def parse_running_positions(value: Optional[str]) -> list[int]:
    if not value:
        return []
    numbers = []
    for item in str(value).replace("\n", " ").split():
        try:
            numbers.append(int(item))
        except ValueError:
            continue
    return numbers


def race_closing_proxy(finish_pos: int, field_size: int, margin_lengths: float, running_positions: Optional[str]) -> float:
    """Return 0-100 proxy from final recognised running call to finish plus margin.

    A higher score means stronger late positional gain and/or a smaller final deficit.
    It uses the second-last reported position as the final-call position. It is not an
    official individual 400m split.
    """
    positions = parse_running_positions(running_positions)
    final_call = positions[-2] if len(positions) >= 2 else finish_pos
    gain = (final_call - finish_pos) / max(field_size - 1, 1)
    margin_penalty = min(max(margin_lengths, 0.0), 20.0) / 20.0
    raw = 50.0 + 42.0 * gain - 17.0 * margin_penalty
    return min(100.0, max(0.0, raw))


def smoothed_rate(wins: float, starts: float, baseline: float, strength: float) -> float:
    return (wins + baseline * strength) / (starts + strength)


def trainer_equipment_change_weight(
    change_wins: int, change_starts: int, trainer_wins: int, trainer_starts: int
) -> float:
    """Return a bounded, leakage-safe trainer equipment-change win-rate weight.

    The name retains the requested ROI terminology for downstream compatibility, but the
    source data holds winners rather than individual bet returns. It is therefore a
    smoothed win-rate ratio against the trainer's own pre-race baseline, over a rolling
    two-year window. Non-change runners receive neutral 1.0 in the caller.
    """
    baseline = smoothed_rate(trainer_wins, trainer_starts, 0.08, 20.0)
    changed_rate = smoothed_rate(change_wins, change_starts, baseline, 12.0)
    ratio = changed_rate / max(baseline, 0.02)
    return min(1.5, max(0.5, ratio))


def shrunk_track_bias(
    wins: float,
    expected_wins: float,
    runners: float,
    prior_runners: float = TRACK_BIAS_PRIOR_RUNNERS,
    max_deviation: float = TRACK_BIAS_MAX_DEVIATION,
) -> float:
    """Return a conservative, bounded draw-band bias around neutral 1.0.

    The raw ratio is smoothed with a context-specific expected-win prior, then
    additionally shrunk by sample reliability. This makes a one-off mud-track or
    wide-draw result nearly neutral while allowing a sustained, large sample to
    influence the model. The final cap prevents an outlier becoming a dominant
    LightGBM input even in an unusual historical context.
    """
    if runners <= 0.0 or expected_wins <= 0.0:
        return 1.0
    baseline_rate = expected_wins / runners
    prior_expected_wins = max(baseline_rate * prior_runners, 1e-12)
    posterior_ratio = (wins + prior_expected_wins) / (expected_wins + prior_expected_wins)
    reliability = runners / (runners + prior_runners)
    bias = 1.0 + reliability * (posterior_ratio - 1.0)
    return min(1.0 + max_deviation, max(1.0 - max_deviation, bias))


def class_level(race_class: Optional[str]) -> int:
    """Map local HKJC class to an ordered numerical level; larger is an easier class."""
    labels = {"第一班": 1, "第二班": 2, "第三班": 3, "第四班": 4, "第五班": 5}
    text = str(race_class or "")
    for label, level in labels.items():
        if label in text:
            return level
    return 6  # new horses / unclassified races: separate neutral level


def distance_bucket(distance_m: int) -> str:
    if distance_m <= 1200:
        return "短途"
    if distance_m <= 1650:
        return "中途"
    return "長途"


def draw_band(draw: Optional[int], field_size: int) -> str:
    if not draw or field_size < 2:
        return "未知"
    percentile = draw / field_size
    if percentile <= 1 / 3:
        return "內檔"
    if percentile <= 2 / 3:
        return "中檔"
    return "外檔"


def expected_field_score(rating: float, ratings: list[float]) -> float:
    """Mean expected pairwise score against remaining field runners."""
    opponents = [value for value in ratings if value != rating]
    if not opponents:
        return 0.5
    return mean(1.0 / (1.0 + 10.0 ** ((other - rating) / 400.0)) for other in opponents)


def normalized_actual_score(finish_pos: int, field_size: int) -> float:
    return (field_size - finish_pos) / max(field_size - 1, 1)


def recent_metric(history: deque[HistoricalRun], attr: str, default: float) -> float:
    if not history:
        return default
    return mean(getattr(item, attr) for item in history)


def recent_win_rate(history: deque[HistoricalRun], n: int = 5) -> float:
    values = list(history)[-n:]
    return mean(item.win for item in values) if values else 0.0


def create_schema(conn: sqlite3.Connection) -> None:
    # Equipment enrichment may run separately; ensure the feature builder remains
    # backward-compatible with pre-equipment databases and produces neutral flags.
    starter_columns = {row[1] for row in conn.execute("PRAGMA table_info(starters)")}
    if "equipment" not in starter_columns:
        conn.execute("ALTER TABLE starters ADD COLUMN equipment TEXT")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS starter_equipment (
            race_date TEXT NOT NULL, racecourse TEXT NOT NULL, race_no INTEGER NOT NULL,
            horse_name TEXT NOT NULL, horse_code TEXT, equipment_raw TEXT,
            source_url TEXT NOT NULL DEFAULT '', fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (race_date, racecourse, race_no, horse_name)
        );
        CREATE INDEX IF NOT EXISTS idx_starter_equipment_horse ON starter_equipment(horse_code, race_date);

        CREATE TABLE IF NOT EXISTS horse_new_horse_priors (
            horse_code TEXT NOT NULL,
            horse_name TEXT,
            as_of_date TEXT NOT NULL,
            sire_name TEXT,
            suggested_distance_text TEXT,
            pedigree_source_url TEXT NOT NULL DEFAULT '',
            latest_trial_date TEXT,
            latest_trial_position REAL,
            latest_trial_margin_lengths REAL,
            latest_trial_qualified TEXT,
            trial_source_url TEXT NOT NULL DEFAULT '',
            fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (horse_code, as_of_date)
        );
        CREATE INDEX IF NOT EXISTS idx_new_horse_prior_lookup ON horse_new_horse_priors(horse_code, as_of_date);

        CREATE TABLE IF NOT EXISTS elo_feature_store (
            race_date TEXT NOT NULL,
            racecourse TEXT NOT NULL,
            race_no INTEGER NOT NULL,
            horse_name TEXT NOT NULL,
            horse_code TEXT,
            jockey TEXT,
            trainer TEXT,
            race_class TEXT,
            distance_m INTEGER,
            surface TEXT,
            course_config TEXT,
            going TEXT,
            field_size INTEGER NOT NULL,
            draw INTEGER,
            draw_pct REAL,
            weight_lbs REAL,
            weight_delta REAL,
            horse_body_weight_pre REAL NOT NULL DEFAULT 0.0,
            horse_body_weight_known_pre INTEGER NOT NULL DEFAULT 0,
            body_weight_delta_pre REAL NOT NULL DEFAULT 0.0,
            body_weight_delta_known_pre INTEGER NOT NULL DEFAULT 0,
            is_extreme_body_weight_change_pre INTEGER NOT NULL DEFAULT 0,
            is_new_horse INTEGER NOT NULL DEFAULT 0,
            pedigree_distance_match_pre REAL NOT NULL DEFAULT 0.5,
            pedigree_prior_known_pre INTEGER NOT NULL DEFAULT 0,
            trial_prior_known_pre INTEGER NOT NULL DEFAULT 0,
            latest_trial_position_pre REAL NOT NULL DEFAULT 0.0,
            latest_trial_margin_pre REAL NOT NULL DEFAULT 0.0,
            latest_trial_qualified_pre INTEGER NOT NULL DEFAULT 0,
            cold_start_prior_pre REAL NOT NULL DEFAULT 0.5,
            horse_elo_pre REAL NOT NULL,
            horse_condition_elo_pre REAL NOT NULL,
            jockey_elo_pre REAL NOT NULL,
            trainer_win_rate_pre REAL NOT NULL,
            horse_win_rate_pre REAL NOT NULL,
            horse_top3_rate_pre REAL NOT NULL,
            condition_win_rate_pre REAL NOT NULL,
            recent_finish_fraction_pre REAL NOT NULL,
            recent_margin_pre REAL NOT NULL,
            recent_win_rate_pre REAL NOT NULL,
            closing400_proxy_pre REAL NOT NULL,
            closing400_trend_pre REAL NOT NULL,
            elo_vs_field REAL NOT NULL,
            jockey_elo_vs_field REAL NOT NULL,
            track_bias_pre REAL NOT NULL DEFAULT 1.0,
            track_bias_sample_pre INTEGER NOT NULL DEFAULT 0,
            class_level INTEGER NOT NULL DEFAULT 6,
            class_drop_from_last_pre REAL NOT NULL DEFAULT 0.0,
            class_weight_interaction_pre REAL NOT NULL DEFAULT 0.0,
            equipment_raw TEXT,
            previous_equipment_raw_pre TEXT,
            is_first_time_blinker INTEGER NOT NULL DEFAULT 0,
            is_equip_added INTEGER NOT NULL DEFAULT 0,
            equipment_changed INTEGER NOT NULL DEFAULT 0,
            equipment_history_known_pre INTEGER NOT NULL DEFAULT 0,
            trainer_equip_change_roi_pre REAL NOT NULL DEFAULT 1.0,
            trainer_equip_change_sample_pre INTEGER NOT NULL DEFAULT 0,
            target_win INTEGER NOT NULL,
            target_top3 INTEGER NOT NULL,
            finish_pos INTEGER NOT NULL,
            finish_pos_text TEXT,
            actual_closing400_proxy REAL NOT NULL,
            source_version TEXT NOT NULL,
            built_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (race_date, racecourse, race_no, horse_name)
        );
        CREATE INDEX IF NOT EXISTS idx_elo_feature_race ON elo_feature_store(race_date, racecourse, race_no);
        CREATE INDEX IF NOT EXISTS idx_elo_feature_horse ON elo_feature_store(horse_name, race_date);

        CREATE TABLE IF NOT EXISTS elo_current_state (
            entity_type TEXT NOT NULL,
            entity_key TEXT NOT NULL,
            rating REAL NOT NULL,
            starts INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (entity_type, entity_key)
        );
        """
    )
    existing = {row[1] for row in conn.execute("PRAGMA table_info(elo_feature_store)")}
    migrations = {
        "track_bias_pre": "REAL NOT NULL DEFAULT 1.0",
        "track_bias_sample_pre": "INTEGER NOT NULL DEFAULT 0",
        "class_level": "INTEGER NOT NULL DEFAULT 6",
        "class_drop_from_last_pre": "REAL NOT NULL DEFAULT 0.0",
        "class_weight_interaction_pre": "REAL NOT NULL DEFAULT 0.0",
        "horse_body_weight_pre": "REAL NOT NULL DEFAULT 0.0",
        "horse_body_weight_known_pre": "INTEGER NOT NULL DEFAULT 0",
        "body_weight_delta_pre": "REAL NOT NULL DEFAULT 0.0",
        "body_weight_delta_known_pre": "INTEGER NOT NULL DEFAULT 0",
        "is_extreme_body_weight_change_pre": "INTEGER NOT NULL DEFAULT 0",
        "is_new_horse": "INTEGER NOT NULL DEFAULT 0",
        "pedigree_distance_match_pre": "REAL NOT NULL DEFAULT 0.5",
        "pedigree_prior_known_pre": "INTEGER NOT NULL DEFAULT 0",
        "trial_prior_known_pre": "INTEGER NOT NULL DEFAULT 0",
        "latest_trial_position_pre": "REAL NOT NULL DEFAULT 0.0",
        "latest_trial_margin_pre": "REAL NOT NULL DEFAULT 0.0",
        "latest_trial_qualified_pre": "INTEGER NOT NULL DEFAULT 0",
        "cold_start_prior_pre": "REAL NOT NULL DEFAULT 0.5",
        "equipment_raw": "TEXT",
        "previous_equipment_raw_pre": "TEXT",
        "is_first_time_blinker": "INTEGER NOT NULL DEFAULT 0",
        "is_equip_added": "INTEGER NOT NULL DEFAULT 0",
        "equipment_changed": "INTEGER NOT NULL DEFAULT 0",
        "equipment_history_known_pre": "INTEGER NOT NULL DEFAULT 0",
        "trainer_equip_change_roi_pre": "REAL NOT NULL DEFAULT 1.0",
        "trainer_equip_change_sample_pre": "INTEGER NOT NULL DEFAULT 0",
    }
    for column, definition in migrations.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE elo_feature_store ADD COLUMN {column} {definition}")
    conn.commit()


def fetch_races(conn: sqlite3.Connection) -> Iterable[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT r.race_date, r.racecourse, r.race_no, r.race_class, r.distance_m,
               r.surface, r.course_config, r.going
        FROM races AS r
        WHERE r.race_status='completed'
        ORDER BY r.race_date, r.race_no
        """
    )


def fetch_runners(conn: sqlite3.Connection, race: sqlite3.Row) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT s.horse_name, s.horse_code, s.jockey, s.trainer, s.weight_lbs,
               s.declared_weight_kg AS horse_body_weight_lbs, s.draw,
               s.margin_lengths, s.running_positions, s.finish_pos, s.finish_pos_text,
               COALESCE(e.equipment_raw, s.equipment) AS equipment
        FROM starters AS s
        LEFT JOIN starter_equipment AS e
          ON e.race_date=s.race_date AND e.racecourse=s.racecourse AND e.race_no=s.race_no AND e.horse_name=s.horse_name
        WHERE s.race_date=? AND s.racecourse=? AND s.race_no=?
          AND s.horse_no IS NOT NULL AND s.finish_pos IS NOT NULL
          AND s.finish_pos_text NOT IN ('WV','WV-A','WX-A','WXNR')
        ORDER BY s.horse_no
        """,
        (race["race_date"], race["racecourse"], race["race_no"]),
    ).fetchall()


def load_new_horse_prior(conn: sqlite3.Connection, horse_code: Optional[str], as_of_date: str) -> sqlite3.Row | None:
    """Return the latest prior recorded no later than the target race date.

    The <= rule prevents a later biography or trial record leaking into an earlier start.
    """
    if not horse_code:
        return None
    return conn.execute(
        """
        SELECT sire_name, suggested_distance_text, latest_trial_date, latest_trial_position,
               latest_trial_margin_lengths, latest_trial_qualified
        FROM horse_new_horse_priors
        WHERE horse_code=? AND as_of_date<=?
        ORDER BY as_of_date DESC
        LIMIT 1
        """,
        (horse_code, as_of_date),
    ).fetchone()


def build(db_path: str, report_path: str) -> dict[str, object]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    create_schema(conn)
    conn.execute("DELETE FROM elo_feature_store")

    horse_elo: dict[str, float] = defaultdict(lambda: INITIAL_ELO)
    jockey_elo: dict[str, float] = defaultdict(lambda: INITIAL_ELO)
    condition_elo: dict[tuple[str, str, int, str], float] = defaultdict(lambda: INITIAL_ELO)
    horse_history: dict[str, deque[HistoricalRun]] = defaultdict(lambda: deque(maxlen=12))
    horse_stats: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])  # starts,wins,top3
    trainer_stats: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # starts,wins
    # Per trainer: (race_date, equipment_changed, win), trimmed to prior 730 days.
    trainer_equip_history: dict[str, deque[tuple[date, int, int]]] = defaultdict(deque)
    condition_stats: dict[tuple[str, str, int, str], list[int]] = defaultdict(lambda: [0, 0])
    # starts, wins, and expected wins (sum of 1/field size) by course/going/distance/draw band.
    track_bias_stats: dict[tuple[str, str, str, str, str, str], list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])

    inserted = 0
    completed_races = 0
    skipped_races = 0
    batch: list[tuple[object, ...]] = []
    for race in fetch_races(conn):
        if not race["distance_m"] or not race["surface"]:
            skipped_races += 1
            continue
        runners = fetch_runners(conn, race)
        field_size = len(runners)
        if field_size < 2 or sum(1 for row in runners if row["finish_pos"] == 1) != 1:
            skipped_races += 1
            continue
        completed_races += 1
        horse_ratings = [horse_elo[row["horse_name"]] for row in runners]
        jockey_ratings = [jockey_elo[row["jockey"]] for row in runners]
        pre_horse_field_mean = mean(horse_ratings)
        pre_jockey_field_mean = mean(jockey_ratings)
        race_context = (race["racecourse"], race["distance_m"], race["surface"])
        current_class_level = class_level(race["race_class"])
        race_day = date.fromisoformat(str(race["race_date"]))
        race_records: list[dict[str, object]] = []

        for index, row in enumerate(runners):
            horse = row["horse_name"]
            jockey = row["jockey"]
            trainer = row["trainer"]
            context_key = (horse, *race_context)
            history = horse_history[horse]
            starts, wins, top3 = horse_stats[horse]
            condition_starts, condition_wins = condition_stats[context_key]
            trainer_starts, trainer_wins = trainer_stats[trainer]
            # Only prior two years are available to this row; never use same-race results.
            trainer_history = trainer_equip_history[trainer]
            cutoff = race_day - timedelta(days=730)
            while trainer_history and trainer_history[0][0] < cutoff:
                trainer_history.popleft()
            change_starts = sum(entry[1] for entry in trainer_history)
            change_wins = sum(entry[2] for entry in trainer_history if entry[1])
            finish_pos = int(row["finish_pos"])
            margin = safe_float(row["margin_lengths"], 20.0)
            actual_close = race_closing_proxy(finish_pos, field_size, margin, row["running_positions"])
            prior_weight = history[-1].weight_lbs if history else None
            previous_equipment = history[-1].equipment_raw if history else None
            prior_body_weight = history[-1].body_weight_lbs if history else None
            body_features = body_weight_features(row["horse_body_weight_lbs"], prior_body_weight)
            is_new_horse = is_new_horse_from_prior_starts(starts)
            prior = load_new_horse_prior(conn, row["horse_code"], str(race["race_date"])) if is_new_horse else None
            pedigree_match, pedigree_known = distance_match_prior(
                prior["suggested_distance_text"] if prior else None, race["distance_m"]
            )
            trial_features = trial_prior(
                prior["latest_trial_position"] if prior else None,
                prior["latest_trial_margin_lengths"] if prior else None,
                prior["latest_trial_qualified"] if prior else None,
            )
            cold_start = cold_start_prior_score(
                pedigree_match, pedigree_known,
                trial_features["latest_trial_position_pre"], trial_features["latest_trial_margin_pre"],
                trial_features["latest_trial_qualified_pre"], trial_features["trial_prior_known_pre"],
            )
            equipment_flags = equipment_feature_flags(row["equipment"], previous_equipment, bool(history and previous_equipment is not None))
            prior_class_level = history[-1].class_level if history else current_class_level
            class_drop = current_class_level - prior_class_level
            weight = safe_float(row["weight_lbs"], 0.0)
            weight_delta = (weight - prior_weight) if prior_weight is not None else 0.0
            current_draw_band = draw_band(row["draw"], field_size)
            track_key = (
                race["racecourse"], str(race["surface"]), str(race["course_config"] or "未知"),
                str(race["going"] or "未知"), distance_bucket(int(race["distance_m"])), current_draw_band,
            )
            track_starts, track_wins, track_expected = track_bias_stats[track_key]
            track_bias = shrunk_track_bias(track_wins, track_expected, track_starts)
            pre_close = recent_metric(history, "closing_proxy", INITIAL_CLOSING)
            last3_close = mean(item.closing_proxy for item in list(history)[-3:]) if history else INITIAL_CLOSING
            all_close = recent_metric(history, "closing_proxy", INITIAL_CLOSING)
            features = {
                "horse_elo_pre": horse_elo[horse],
                "horse_condition_elo_pre": condition_elo[context_key],
                "jockey_elo_pre": jockey_elo[jockey],
                "trainer_win_rate_pre": smoothed_rate(trainer_wins, trainer_starts, 0.08, 20.0),
                "horse_win_rate_pre": smoothed_rate(wins, starts, 0.08, 10.0),
                "horse_top3_rate_pre": smoothed_rate(top3, starts, 0.24, 10.0),
                "condition_win_rate_pre": smoothed_rate(condition_wins, condition_starts, 0.08, 5.0),
                "recent_finish_fraction_pre": recent_metric(history, "finish_fraction", 0.5),
                "recent_margin_pre": recent_metric(history, "margin_lengths", 6.0),
                "recent_win_rate_pre": recent_win_rate(history),
                "closing400_proxy_pre": pre_close,
                "closing400_trend_pre": last3_close - all_close,
                "elo_vs_field": horse_elo[horse] - pre_horse_field_mean,
                "jockey_elo_vs_field": jockey_elo[jockey] - pre_jockey_field_mean,
                "track_bias_pre": track_bias,
                "track_bias_sample_pre": int(track_starts),
                "class_level": current_class_level,
                "class_drop_from_last_pre": class_drop,
                "class_weight_interaction_pre": class_drop * weight_delta,
                **body_features,
                "is_new_horse": is_new_horse,
                "pedigree_distance_match_pre": pedigree_match,
                "pedigree_prior_known_pre": pedigree_known,
                **trial_features,
                "cold_start_prior_pre": cold_start,
                "equipment_raw": row["equipment"],
                "previous_equipment_raw_pre": previous_equipment,
                **equipment_flags,
                "trainer_equip_change_roi_pre": (
                    trainer_equipment_change_weight(change_wins, change_starts, trainer_wins, trainer_starts)
                    if equipment_flags["equipment_changed"] else 1.0
                ),
                "trainer_equip_change_sample_pre": change_starts,
            }
            batch.append(
                (
                    race["race_date"], race["racecourse"], race["race_no"], horse, row["horse_code"],
                    jockey, trainer, race["race_class"], race["distance_m"], race["surface"],
                    race["course_config"], race["going"], field_size, row["draw"],
                    (safe_float(row["draw"]) / field_size) if row["draw"] else None, weight,
                    weight_delta, features["horse_body_weight_pre"], features["horse_body_weight_known_pre"],
                    features["body_weight_delta_pre"], features["body_weight_delta_known_pre"],
                    features["is_extreme_body_weight_change_pre"], features["is_new_horse"],
                    features["pedigree_distance_match_pre"], features["pedigree_prior_known_pre"],
                    features["trial_prior_known_pre"], features["latest_trial_position_pre"],
                    features["latest_trial_margin_pre"], features["latest_trial_qualified_pre"], features["cold_start_prior_pre"],
                    features["horse_elo_pre"], features["horse_condition_elo_pre"], features["jockey_elo_pre"],
                    features["trainer_win_rate_pre"], features["horse_win_rate_pre"], features["horse_top3_rate_pre"],
                    features["condition_win_rate_pre"], features["recent_finish_fraction_pre"],
                    features["recent_margin_pre"], features["recent_win_rate_pre"], features["closing400_proxy_pre"],
                    features["closing400_trend_pre"], features["elo_vs_field"], features["jockey_elo_vs_field"],
                    features["track_bias_pre"], features["track_bias_sample_pre"], features["class_level"],
                    features["class_drop_from_last_pre"], features["class_weight_interaction_pre"],
                    features["equipment_raw"], features["previous_equipment_raw_pre"],
                    features["is_first_time_blinker"], features["is_equip_added"], features["equipment_changed"], features["equipment_history_known_pre"],
                    features["trainer_equip_change_roi_pre"], features["trainer_equip_change_sample_pre"],
                    int(finish_pos == 1), int(finish_pos <= 3), finish_pos, row["finish_pos_text"], actual_close,
                    "elo_features_v10_2_advanced",
                )
            )
            race_records.append(
                {
                    "horse": horse,
                    "jockey": jockey,
                    "trainer": trainer,
                    "context_key": context_key,
                    "finish_pos": finish_pos,
                    "margin": margin,
                    "weight": weight,
                    "actual_close": actual_close,
                    "horse_rating": horse_elo[horse],
                    "jockey_rating": jockey_elo[jockey],
                    "condition_rating": condition_elo[context_key],
                    "track_key": track_key,
                    "race_day": race_day,
                    "equipment_raw": row["equipment"],
                    "equipment_changed": equipment_flags["equipment_changed"],
                }
            )

        for record in race_records:
            actual = normalized_actual_score(int(record["finish_pos"]), field_size)
            horse_expected = expected_field_score(float(record["horse_rating"]), horse_ratings)
            jockey_expected = expected_field_score(float(record["jockey_rating"]), jockey_ratings)
            condition_ratings = [condition_elo[(other["horse"], *race_context)] for other in race_records]
            condition_expected = expected_field_score(float(record["condition_rating"]), condition_ratings)
            horse_elo[str(record["horse"])] += HORSE_K * (actual - horse_expected)
            jockey_elo[str(record["jockey"])] += JOCKEY_K * (actual - jockey_expected)
            condition_elo[record["context_key"]] += CONDITION_K * (actual - condition_expected)
            horse = str(record["horse"])
            trainer = str(record["trainer"])
            finish_pos = int(record["finish_pos"])
            horse_history[horse].append(
                HistoricalRun(
                    finish_fraction=finish_pos / field_size,
                    margin_lengths=float(record["margin"]),
                    weight_lbs=float(record["weight"]),
                    closing_proxy=float(record["actual_close"]),
                    class_level=current_class_level,
                    win=int(finish_pos == 1),
                    top3=int(finish_pos <= 3),
                    equipment_raw=record["equipment_raw"],
                    body_weight_lbs=safe_float(row["horse_body_weight_lbs"], 0.0) or None,
                )
            )
            horse_stats[horse][0] += 1
            horse_stats[horse][1] += int(finish_pos == 1)
            horse_stats[horse][2] += int(finish_pos <= 3)
            trainer_stats[trainer][0] += 1
            trainer_stats[trainer][1] += int(finish_pos == 1)
            trainer_equip_history[trainer].append((record["race_day"], int(record["equipment_changed"]), int(finish_pos == 1)))
            condition_stats[record["context_key"]][0] += 1
            condition_stats[record["context_key"]][1] += int(finish_pos == 1)
            track_bias_stats[record["track_key"]][0] += 1.0
            track_bias_stats[record["track_key"]][1] += float(finish_pos == 1)
            track_bias_stats[record["track_key"]][2] += 1.0 / field_size

        if len(batch) >= 1000:
            conn.executemany(INSERT_FEATURE_SQL, batch)
            conn.commit()
            inserted += len(batch)
            batch.clear()

    if batch:
        conn.executemany(INSERT_FEATURE_SQL, batch)
        conn.commit()
        inserted += len(batch)

    conn.execute("DELETE FROM elo_current_state")
    state_rows = []
    for horse, rating in horse_elo.items():
        starts, wins, _ = horse_stats[horse]
        state_rows.append(("horse", horse, rating, starts, wins))
    for jockey, rating in jockey_elo.items():
        state_rows.append(("jockey", jockey, rating, 0, 0))
    for (horse, racecourse, distance_m, surface), rating in condition_elo.items():
        starts, wins = condition_stats[(horse, racecourse, distance_m, surface)]
        key = f"{horse}|{racecourse}|{distance_m}|{surface}"
        state_rows.append(("horse_condition", key, rating, starts, wins))
    conn.executemany(
        "INSERT INTO elo_current_state(entity_type,entity_key,rating,starts,wins) VALUES(?,?,?,?,?)",
        state_rows,
    )
    conn.commit()

    result = {
        "feature_rows": inserted,
        "races_used": completed_races,
        "races_skipped": skipped_races,
        "horse_count": len(horse_elo),
        "jockey_count": len(jockey_elo),
        "source_version": "elo_features_v10_2_advanced",
        "track_bias_prior_runners": TRACK_BIAS_PRIOR_RUNNERS,
        "track_bias_max_deviation": TRACK_BIAS_MAX_DEVIATION,
        "note": "closing400_proxy is derived from official running positions and margins; it is not a measured individual 400m sectional time. track_bias_pre uses expected-win smoothing, sample-reliability shrinkage toward 1.0, and a bounded deviation. Equipment flags use official gear records only from prior starts; trainer_equip_change_roi_pre is a bounded two-year smoothed win-rate weight, not literal betting ROI.",
    }
    Path(report_path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    conn.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="建立無未來資料的 ELO 與末段走勢特徵庫")
    parser.add_argument("--db", default="hkjc_last_season.sqlite")
    parser.add_argument("--report", default="elo_feature_report.json")
    args = parser.parse_args()
    build(args.db, args.report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
