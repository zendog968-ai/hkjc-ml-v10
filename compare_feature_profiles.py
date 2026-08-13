#!/usr/bin/env python3
"""Compare base V10 versus expanded V10.1 feature profiles on the validation period only."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from lightgbm import LGBMClassifier, early_stopping
from sklearn.compose import ColumnTransformer
from sklearn.isotonic import IsotonicRegression
from sklearn.preprocessing import OneHotEncoder

from train_lightgbm import (
    CATEGORICAL_FEATURES,
    NUMERIC_FEATURES,
    chronological_split,
    load_feature_table,
    race_level_metrics,
)

V101_EXTRA = {
    "track_bias_pre", "track_bias_sample_pre", "class_level",
    "class_drop_from_last_pre", "class_weight_interaction_pre",
}


def fit_profile(train_df, valid_df, feature_names):
    preprocessor = ColumnTransformer(
        [("num", "passthrough", feature_names), ("cat", OneHotEncoder(handle_unknown="ignore"), CATEGORICAL_FEATURES)]
    )
    x_train = preprocessor.fit_transform(train_df[feature_names + CATEGORICAL_FEATURES])
    x_valid = preprocessor.transform(valid_df[feature_names + CATEGORICAL_FEATURES])
    model = LGBMClassifier(
        objective="binary", learning_rate=0.03, n_estimators=700, num_leaves=15, max_depth=4,
        min_child_samples=55, subsample=0.85, colsample_bytree=0.85, reg_alpha=0.3,
        reg_lambda=1.5, random_state=20260812, n_jobs=1, verbosity=-1,
    )
    model.fit(
        x_train, train_df["target_win"].astype(int), eval_set=[(x_valid, valid_df["target_win"].astype(int))],
        eval_metric="binary_logloss", callbacks=[early_stopping(stopping_rounds=45, verbose=False)],
    )
    raw = model.predict_proba(x_valid)[:, 1]
    calibrator = IsotonicRegression(out_of_bounds="clip").fit(raw, valid_df["target_win"].astype(int))
    output = valid_df[["race_date", "racecourse", "race_no", "horse_name", "target_win"]].copy()
    output["probability"] = calibrator.predict(raw)
    metrics = race_level_metrics(output, "probability")
    metrics["best_iteration"] = int(model.best_iteration_ or model.n_estimators)
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser(description="比較 V10 與 V10.1 特徵設定的時間序列驗證表現")
    parser.add_argument("--db", default="hkjc_last_season.sqlite")
    parser.add_argument("--output", default="feature_profile_comparison.json")
    args = parser.parse_args()
    df = load_feature_table(args.db)
    train_df, valid_df, _ = chronological_split(df)
    base_features = [item for item in NUMERIC_FEATURES if item not in V101_EXTRA]
    profiles = {
        "base_v10": fit_profile(train_df, valid_df, base_features),
        "expanded_v10_1": fit_profile(train_df, valid_df, NUMERIC_FEATURES),
    }
    selected = min(profiles, key=lambda key: profiles[key]["mean_race_brier_score"])
    result = {
        "selection_period": {"from": str(valid_df.race_date.min().date()), "to": str(valid_df.race_date.max().date())},
        "profiles": profiles,
        "selected_profile": selected,
        "selection_rule": "較低場內正規化平均 Brier score 為優；如相同則選首選勝出率較高者。",
        "note": "最終測試集不可用於選擇特徵設定；本結果只使用驗證期。",
    }
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
