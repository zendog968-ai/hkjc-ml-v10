"""Central configuration for the isolated N6 Neural Calculation Engine."""

from __future__ import annotations

import os
from pathlib import Path

N6_ROOT = Path(__file__).resolve().parents[1]
V10_DB_PATH = Path(
    os.getenv("N6_V10_DB_PATH", "/home/ubuntu/hkjc_v10_database/hkjc_last_season.sqlite")
).resolve()
MODELS_DIR = N6_ROOT / "models"
REPORTS_DIR = N6_ROOT / "reports"
LOGS_DIR = N6_ROOT / "logs"

MODEL_PATH = MODELS_DIR / "n6_mlp_model.pt"
PREPROCESSOR_PATH = MODELS_DIR / "n6_preprocessor.joblib"
TRAINING_REPORT_PATH = REPORTS_DIR / "n6_training_report.json"
TEST_PREDICTIONS_PATH = REPORTS_DIR / "n6_test_predictions.csv"
SOURCE_SCHEMA_REPORT_PATH = REPORTS_DIR / "v10_source_schema.json"

RANDOM_SEED = 20260819
TARGET_COLUMN = "target_win"
RACE_KEY_COLUMNS = ["race_date", "racecourse", "race_no"]
ENTITY_COLUMNS = ["horse_name", "jockey", "trainer"]

# The ELO columns are computed before each race by V10.  Market fields originate
# from starters.win_odds and are explicitly availability-flagged in the feature matrix.
NUMERIC_FEATURES = [
    "distance_m",
    "field_size",
    "draw",
    "draw_pct",
    "weight_lbs",
    "weight_delta",
    "horse_body_weight_pre",
    "horse_body_weight_known_pre",
    "body_weight_delta_pre",
    "body_weight_delta_known_pre",
    "is_extreme_body_weight_change_pre",
    "is_new_horse",
    "pedigree_distance_match_pre",
    "pedigree_prior_known_pre",
    "trial_prior_known_pre",
    "latest_trial_position_pre",
    "latest_trial_margin_pre",
    "latest_trial_qualified_pre",
    "cold_start_prior_pre",
    "horse_elo_pre",
    "horse_condition_elo_pre",
    "jockey_elo_pre",
    "trainer_win_rate_pre",
    "horse_win_rate_pre",
    "horse_top3_rate_pre",
    "condition_win_rate_pre",
    "recent_finish_fraction_pre",
    "recent_margin_pre",
    "recent_win_rate_pre",
    "closing400_proxy_pre",
    "closing400_trend_pre",
    "elo_vs_field",
    "jockey_elo_vs_field",
    "track_bias_pre",
    "track_bias_sample_pre",
    "class_level",
    "class_drop_from_last_pre",
    "class_weight_interaction_pre",
    "is_first_time_blinker",
    "is_equip_added",
    "equipment_changed",
    "equipment_history_known_pre",
    "trainer_equip_change_roi_pre",
    "trainer_equip_change_sample_pre",
    "market_log_odds",
    "market_implied_probability",
    "market_odds_available",
]

CATEGORICAL_FEATURES = ["racecourse", "race_class", "surface", "course_config", "going"]
ALL_FEATURES = NUMERIC_FEATURES + CATEGORICAL_FEATURES

# Feature-store fields supplied directly by V10; these have been produced before
# each labelled race and are the model's core leakage-safe inputs.
ELO_FEATURE_COLUMNS = [
    "race_date",
    "racecourse",
    "race_no",
    "horse_name",
    "horse_code",
    "jockey",
    "trainer",
    "race_class",
    *[field for field in NUMERIC_FEATURES if not field.startswith("market_")],
    "surface",
    "course_config",
    "going",
    "target_win",
]

# SQLite is always opened in immutable read-only mode.  The connection URI itself
# prevents write access even if a caller accidentally attempted a mutation.
def v10_read_only_uri() -> str:
    return f"file:{V10_DB_PATH}?mode=ro&immutable=1"
