"""Read-only V10 data access and leakage-safe N6 feature engineering."""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from typing import Any

import numpy as np
import pandas as pd

from .config import (
    ALL_FEATURES,
    CATEGORICAL_FEATURES,
    ELO_FEATURE_COLUMNS,
    NUMERIC_FEATURES,
    RACE_KEY_COLUMNS,
    TARGET_COLUMN,
    V10_DB_PATH,
    v10_read_only_uri,
)


def connect_v10_read_only() -> sqlite3.Connection:
    """Open the V10 database with three independent write barriers."""
    if not V10_DB_PATH.is_file():
        raise FileNotFoundError(f"V10 read-only source database not found: {V10_DB_PATH}")
    connection = sqlite3.connect(v10_read_only_uri(), uri=True)
    connection.execute("PRAGMA query_only = ON")
    return connection


def _quoted_columns(alias: str, columns: Iterable[str]) -> str:
    return ",\n            ".join(f"{alias}.\"{column}\"" for column in columns)


def _read_sql_frame(
    connection: sqlite3.Connection,
    query: str,
    params: tuple[object, ...] = (),
) -> pd.DataFrame:
    """Fetch a read-only query while explicitly finalising its SQLite cursor.

    pandas.read_sql_query creates a cursor internally. Explicitly closing that
    cursor before the enclosing connection closes avoids a high-frequency API
    path retaining database descriptors until a later garbage-collection cycle.
    """
    cursor = connection.execute(query, params)
    try:
        columns = [description[0] for description in cursor.description or ()]
        rows = cursor.fetchall()
    finally:
        cursor.close()
    return pd.DataFrame.from_records(rows, columns=columns)


def source_inventory() -> dict[str, Any]:
    """Return only metadata and row counts from V10 for the training report."""
    tables = ("races", "starters", "elo_feature_store", "elo_current_state")
    with closing(connect_v10_read_only()) as connection:
        counts = {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in tables
        }
        date_range = connection.execute(
            "SELECT MIN(race_date), MAX(race_date), COUNT(DISTINCT race_date) FROM elo_feature_store"
        ).fetchone()
        entity_types = connection.execute(
            "SELECT entity_type, COUNT(*) FROM elo_current_state GROUP BY entity_type ORDER BY entity_type"
        ).fetchall()
    return {
        "database": str(V10_DB_PATH),
        "access": "SQLite URI mode=ro&immutable=1; PRAGMA query_only=ON",
        "row_counts": counts,
        "elo_feature_date_range": {"from": date_range[0], "to": date_range[1], "race_days": int(date_range[2])},
        "current_elo_entity_types": {entity_type: int(count) for entity_type, count in entity_types},
    }


def load_training_frame() -> pd.DataFrame:
    """Load only V10 pre-race features and winner labels in chronological order.

    V10's ``elo_feature_store`` is the primary leakage-safe source.  It is joined
    to ``races`` to ensure completed official races and to ``starters`` solely for
    historical market odds.  Odds are availability-flagged; missing values remain
    distinct from true odds before imputation.
    """
    feature_columns = [column for column in ELO_FEATURE_COLUMNS if column not in {
        "race_date", "racecourse", "race_no", "horse_name", "horse_code", "jockey", "trainer",
        "race_class", "distance_m", "surface", "course_config", "going"
    }]
    selected = _quoted_columns("e", feature_columns)
    query = f"""
        SELECT
            e.race_date,
            e.racecourse,
            e.race_no,
            e.horse_name,
            e.horse_code,
            e.jockey,
            e.trainer,
            COALESCE(r.race_class, e.race_class) AS race_class,
            COALESCE(r.distance_m, e.distance_m) AS distance_m,
            COALESCE(r.surface, e.surface) AS surface,
            COALESCE(r.course_config, e.course_config) AS course_config,
            COALESCE(r.going, e.going) AS going,
            {selected},
            s.win_odds
        FROM elo_feature_store AS e
        INNER JOIN races AS r
            ON r.race_date = e.race_date
           AND r.racecourse = e.racecourse
           AND r.race_no = e.race_no
        LEFT JOIN starters AS s
            ON s.race_date = e.race_date
           AND s.racecourse = e.racecourse
           AND s.race_no = e.race_no
           AND s.horse_name = e.horse_name
        WHERE r.race_status = 'completed'
          AND e.target_win IN (0, 1)
        ORDER BY e.race_date, e.racecourse, e.race_no, e.horse_name
    """
    with closing(connect_v10_read_only()) as connection:
        frame = _read_sql_frame(connection, query)
    if frame.empty:
        raise ValueError("No labelled completed V10 rows were returned for N6 training.")
    return clean_feature_frame(frame, require_target=True)


def clean_feature_frame(frame: pd.DataFrame, require_target: bool) -> pd.DataFrame:
    """Create stable model columns without creating or altering source records."""
    data = frame.copy()
    # Market fields are deterministically derived from starters.win_odds below.
    required = set(RACE_KEY_COLUMNS + ["horse_name"] + [
        column for column in ALL_FEATURES if not column.startswith("market_")
    ])
    missing = sorted(required.difference(data.columns))
    if missing:
        raise ValueError(f"Feature source is missing required fields: {', '.join(missing)}")
    if require_target and TARGET_COLUMN not in data.columns:
        raise ValueError("Target label is required for training.")

    data["race_date"] = pd.to_datetime(data["race_date"], errors="coerce")
    if data["race_date"].isna().any():
        raise ValueError("Invalid race_date encountered in V10 source.")
    for column in NUMERIC_FEATURES:
        if column not in data.columns:
            data[column] = np.nan
        data[column] = pd.to_numeric(data[column], errors="coerce")
    odds = pd.to_numeric(data.get("win_odds"), errors="coerce")
    valid_odds = odds.where(odds > 1.0)
    data["market_odds_available"] = valid_odds.notna().astype(float)
    data["market_log_odds"] = np.log(valid_odds)
    data["market_implied_probability"] = 1.0 / valid_odds
    for column in CATEGORICAL_FEATURES:
        data[column] = data[column].fillna("未知").astype(str).str.strip().replace("", "未知")
    data["race_group"] = (
        data["race_date"].dt.strftime("%Y-%m-%d")
        + "|" + data["racecourse"]
        + "|" + data["race_no"].astype(str)
    )
    if require_target:
        data[TARGET_COLUMN] = pd.to_numeric(data[TARGET_COLUMN], errors="coerce").fillna(0).astype(int)
    return data


EQUIPMENT_ODDS_INTERACTION_FEATURES = [
    "equipment_changed_x_odds_1_lt5",
    "equipment_changed_x_odds_5_lt10",
    "equipment_changed_x_odds_10_lt20",
    "equipment_changed_x_odds_20_plus",
]


def add_equipment_odds_interaction_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Return a copy with leakage-safe equipment-change × historical-odds flags.

    The interaction uses only ``equipment_changed`` and the pre-race ``win_odds``
    already used by N6.  Missing or invalid odds activate no band, preserving the
    original ``equipment_changed`` signal and its availability handling.
    """
    data = frame.copy()
    changed = pd.to_numeric(data.get("equipment_changed"), errors="coerce").fillna(0.0).gt(0.0)
    odds = pd.to_numeric(data.get("win_odds"), errors="coerce")
    valid_odds = odds.where(odds > 1.0)
    bands = {
        "equipment_changed_x_odds_1_lt5": valid_odds.ge(1.0) & valid_odds.lt(5.0),
        "equipment_changed_x_odds_5_lt10": valid_odds.ge(5.0) & valid_odds.lt(10.0),
        "equipment_changed_x_odds_10_lt20": valid_odds.ge(10.0) & valid_odds.lt(20.0),
        "equipment_changed_x_odds_20_plus": valid_odds.ge(20.0),
    }
    for feature, in_band in bands.items():
        data[feature] = (changed & in_band).astype(float)
    return data


def load_historical_race(race_date: str, racecourse: str, race_no: int) -> pd.DataFrame:
    """Load a historical V10 race's known pre-race features for API verification."""
    query = """
        SELECT e.*, s.win_odds
        FROM elo_feature_store AS e
        LEFT JOIN starters AS s
          ON s.race_date = e.race_date AND s.racecourse = e.racecourse
         AND s.race_no = e.race_no AND s.horse_name = e.horse_name
        WHERE e.race_date = ? AND e.racecourse = ? AND e.race_no = ?
        ORDER BY e.horse_name
    """
    with closing(connect_v10_read_only()) as connection:
        frame = _read_sql_frame(connection, query, (race_date, racecourse, int(race_no)))
    if frame.empty:
        raise LookupError("No V10 ELO feature rows match this race.")
    return clean_feature_frame(frame, require_target=False)


def _current_elo_maps() -> tuple[dict[tuple[str, str], float], dict[tuple[str, str], tuple[int, int]]]:
    with closing(connect_v10_read_only()) as connection:
        rows = connection.execute(
            "SELECT entity_type, entity_key, rating, starts, wins FROM elo_current_state"
        ).fetchall()
    ratings = {(str(kind), str(key)): float(rating) for kind, key, rating, _, _ in rows}
    records = {(str(kind), str(key)): (int(starts), int(wins)) for kind, key, _, starts, wins in rows}
    return ratings, records


def build_live_feature_frame(race: dict[str, Any], runners: list[dict[str, Any]]) -> pd.DataFrame:
    """Build a future-race N6 matrix using caller inputs plus V10 current ELO state.

    No race result is queried here.  If an input does not carry an ELO value, N6
    queries only ``elo_current_state``.  Newly supplied values always take
    precedence so V10 can pass its exact pre-race snapshot.
    """
    if not runners:
        raise ValueError("At least one runner is required.")
    if len(runners) > 30:
        raise ValueError("At most 30 runners are accepted per race.")
    required_race = ("race_date", "racecourse", "race_no")
    absent = [key for key in required_race if race.get(key) in (None, "")]
    if absent:
        raise ValueError(f"Missing race fields: {', '.join(absent)}")
    ratings, records = _current_elo_maps()
    field_size = len(runners)
    normalized: list[dict[str, Any]] = []
    for index, runner in enumerate(runners, start=1):
        horse_name = str(runner.get("horse_name", "")).strip()
        if not horse_name:
            raise ValueError(f"Runner {index} is missing horse_name.")
        jockey = str(runner.get("jockey", "未知")).strip() or "未知"
        trainer = str(runner.get("trainer", "未知")).strip() or "未知"
        record: dict[str, Any] = dict(race)
        record.update(runner)
        record["horse_name"] = horse_name
        record["jockey"] = jockey
        record["trainer"] = trainer
        record["field_size"] = record.get("field_size") or field_size
        draw = record.get("draw")
        record["draw_pct"] = (float(draw) / float(record["field_size"])) if draw not in (None, "", 0) else np.nan
        record["horse_elo_pre"] = record.get("horse_elo_pre", ratings.get(("horse", horse_name), 1500.0))
        record["jockey_elo_pre"] = record.get("jockey_elo_pre", ratings.get(("jockey", jockey), 1500.0))
        # Must match V10's feature contract: horse|course|distance|surface.
        condition_key = f"{horse_name}|{record.get('racecourse', '')}|{record.get('distance_m', '')}|{record.get('surface', '')}"
        record["horse_condition_elo_pre"] = record.get(
            "horse_condition_elo_pre",
            ratings.get(("horse_condition", condition_key), record["horse_elo_pre"]),
        )
        starts, wins = records.get(("horse", horse_name), (0, 0))
        if record.get("horse_win_rate_pre") in (None, "") and starts:
            record["horse_win_rate_pre"] = wins / starts
        normalized.append(record)
    frame = pd.DataFrame(normalized)
    for feature in ALL_FEATURES:
        if feature not in frame.columns:
            frame[feature] = np.nan if feature in NUMERIC_FEATURES else "未知"
    frame["win_odds"] = frame.get("win_odds", np.nan)
    return clean_feature_frame(frame, require_target=False)


def score_to_race_probabilities(raw_scores: np.ndarray, race_groups: pd.Series) -> np.ndarray:
    """Softmax-normalise neural logits inside each race to produce comparable scores."""
    output = np.zeros(len(raw_scores), dtype=float)
    working = pd.DataFrame({"group": race_groups.to_numpy(), "score": raw_scores})
    for _, group in working.groupby("group", sort=False):
        positions = group.index.to_numpy()
        logits = group["score"].to_numpy(dtype=float)
        logits = logits - np.max(logits)
        exp_values = np.exp(np.clip(logits, -60, 60))
        output[positions] = exp_values / exp_values.sum()
    return output


def log_odds_from_probability(probabilities: np.ndarray) -> np.ndarray:
    values = np.clip(probabilities, 1e-6, 1 - 1e-6)
    return np.log(values / (1 - values))
