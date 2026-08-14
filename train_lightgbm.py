#!/usr/bin/env python3
"""Train a leakage-safe V10.2 LightGBM + CatBoost race-ranking ensemble.

Both learners are trained only on pre-race features from earlier dates. Their raw ranking
scores are calibrated on a chronological validation period, blended with validation-derived
inverse-Brier weights, then evaluated once on an untouched final test period.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from catboost import CatBoostRanker, Pool
from lightgbm import LGBMRanker, early_stopping
from sklearn.compose import ColumnTransformer
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
from sklearn.preprocessing import OneHotEncoder

NUMERIC_FEATURES = [
    "distance_m", "field_size", "draw", "draw_pct", "weight_lbs", "weight_delta",
    "horse_body_weight_pre", "horse_body_weight_known_pre", "body_weight_delta_pre",
    "body_weight_delta_known_pre", "is_extreme_body_weight_change_pre", "is_new_horse",
    "pedigree_distance_match_pre", "pedigree_prior_known_pre", "trial_prior_known_pre",
    "latest_trial_position_pre", "latest_trial_margin_pre", "latest_trial_qualified_pre",
    "cold_start_prior_pre", "horse_elo_pre", "horse_condition_elo_pre", "jockey_elo_pre",
    "trainer_win_rate_pre", "horse_win_rate_pre", "horse_top3_rate_pre", "condition_win_rate_pre",
    "recent_finish_fraction_pre", "recent_margin_pre", "recent_win_rate_pre", "closing400_proxy_pre",
    "closing400_trend_pre", "elo_vs_field", "jockey_elo_vs_field", "track_bias_pre",
    "track_bias_sample_pre", "class_level", "class_drop_from_last_pre", "class_weight_interaction_pre",
    "is_first_time_blinker", "is_equip_added", "equipment_changed", "equipment_history_known_pre",
    "trainer_equip_change_roi_pre", "trainer_equip_change_sample_pre",
]
CATEGORICAL_FEATURES = ["racecourse", "race_class", "surface", "course_config", "going"]
ALL_FEATURES = NUMERIC_FEATURES + CATEGORICAL_FEATURES
KEY_COLUMNS = ["race_date", "racecourse", "race_no", "horse_name", "target_win"]


def load_feature_table(db_path: str) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    selected = list(dict.fromkeys(KEY_COLUMNS + ALL_FEATURES))
    df = pd.read_sql_query("SELECT " + ",".join(selected) + " FROM elo_feature_store ORDER BY race_date,race_no,horse_name", conn)
    conn.close()
    if df.empty:
        raise ValueError("elo_feature_store 為空；請先執行 build_elo_features.py。")
    df["race_date"] = pd.to_datetime(df["race_date"])
    for col in NUMERIC_FEATURES:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    for col in CATEGORICAL_FEATURES:
        df[col] = df[col].fillna("未知").astype(str)
    df["race_group"] = (
        df["race_date"].dt.strftime("%Y-%m-%d") + "|" + df["racecourse"].astype(str) + "|" + df["race_no"].astype(str)
    )
    return df


def chronological_split(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    dates = sorted(df["race_date"].dt.strftime("%Y-%m-%d").unique())
    if len(dates) < 20:
        raise ValueError("賽日數量不足以進行時間切分。")
    train_end = dates[max(1, int(len(dates) * 0.70)) - 1]
    valid_end = dates[max(2, int(len(dates) * 0.85)) - 1]
    train = df[df["race_date"] <= train_end].copy()
    valid = df[(df["race_date"] > train_end) & (df["race_date"] <= valid_end)].copy()
    test = df[df["race_date"] > valid_end].copy()
    if min(len(train), len(valid), len(test)) == 0:
        raise ValueError("時間切分產生空資料集。")
    return train, valid, test


def group_sizes(df: pd.DataFrame) -> list[int]:
    return df.groupby("race_group", sort=False).size().astype(int).tolist()


def normalize_by_race(df: pd.DataFrame, probability_column: str) -> pd.Series:
    values = df[probability_column].clip(lower=1e-6)
    return values / values.groupby(df["race_group"]).transform("sum")


def race_level_metrics(df: pd.DataFrame, probability_column: str) -> dict[str, float]:
    data = df.copy()
    data["race_probability"] = normalize_by_race(data, probability_column)
    top_pick_wins: list[int] = []
    top3_has_winner: list[int] = []
    brier_scores: list[float] = []
    uniform_brier_scores: list[float] = []
    for _, group in data.groupby("race_group", sort=False):
        ordered = group.sort_values("race_probability", ascending=False)
        top_pick_wins.append(int(ordered.iloc[0]["target_win"] == 1))
        top3_has_winner.append(int(ordered.iloc[:3]["target_win"].sum() > 0))
        y = group["target_win"].to_numpy(dtype=float)
        p = group["race_probability"].to_numpy(dtype=float)
        uniform = np.full(len(group), 1.0 / len(group))
        brier_scores.append(float(np.sum((p - y) ** 2)))
        uniform_brier_scores.append(float(np.sum((uniform - y) ** 2)))
    return {
        "races": int(len(top_pick_wins)),
        "top_pick_win_rate": float(np.mean(top_pick_wins)),
        "top3_contains_winner_rate": float(np.mean(top3_has_winner)),
        "mean_race_brier_score": float(np.mean(brier_scores)),
        "mean_uniform_race_brier_score": float(np.mean(uniform_brier_scores)),
    }


def make_lgbm() -> LGBMRanker:
    return LGBMRanker(
        objective="lambdarank", metric="ndcg", ndcg_at=[1, 3], learning_rate=0.03,
        n_estimators=700, num_leaves=15, max_depth=4, min_child_samples=55,
        subsample=0.85, colsample_bytree=0.85, reg_alpha=0.3, reg_lambda=1.5,
        random_state=20260814, n_jobs=1, verbosity=-1,
    )


def make_catboost() -> CatBoostRanker:
    return CatBoostRanker(
        loss_function="YetiRank", eval_metric="NDCG:top=3", iterations=700,
        learning_rate=0.03, depth=5, l2_leaf_reg=5.0, random_seed=20260814,
        random_strength=0.4, thread_count=1, verbose=False, allow_writing_files=False,
    )


def safe_isotonic(raw: np.ndarray, y: pd.Series) -> IsotonicRegression:
    calibrator = IsotonicRegression(out_of_bounds="clip")
    calibrator.fit(raw, y.astype(int))
    return calibrator


def model_weights(valid_df: pd.DataFrame, lgb_prob: np.ndarray, cat_prob: np.ndarray) -> dict[str, float]:
    lgb_data = valid_df[["race_group", "target_win"]].copy(); lgb_data["p"] = lgb_prob
    cat_data = valid_df[["race_group", "target_win"]].copy(); cat_data["p"] = cat_prob
    lgb_brier = race_level_metrics(lgb_data, "p")["mean_race_brier_score"]
    cat_brier = race_level_metrics(cat_data, "p")["mean_race_brier_score"]
    inverse = np.array([1.0 / max(lgb_brier, 1e-6), 1.0 / max(cat_brier, 1e-6)])
    inverse /= inverse.sum()
    return {"lightgbm": float(inverse[0]), "catboost": float(inverse[1]), "validation_race_brier": {"lightgbm": lgb_brier, "catboost": cat_brier}}


def train(db_path: str, model_path: str, report_path: str, predictions_path: str) -> dict[str, Any]:
    df = load_feature_table(db_path)
    train_df, valid_df, test_df = chronological_split(df)
    preprocessor = ColumnTransformer([
        ("num", "passthrough", NUMERIC_FEATURES),
        ("cat", OneHotEncoder(handle_unknown="ignore"), CATEGORICAL_FEATURES),
    ], remainder="drop")
    x_train_lgb = preprocessor.fit_transform(train_df[ALL_FEATURES])
    x_valid_lgb = preprocessor.transform(valid_df[ALL_FEATURES])
    x_test_lgb = preprocessor.transform(test_df[ALL_FEATURES])
    y_train = train_df["target_win"].astype(int)
    y_valid = valid_df["target_win"].astype(int)
    y_test = test_df["target_win"].astype(int)

    lgbm = make_lgbm()
    lgbm.fit(
        x_train_lgb, y_train, group=group_sizes(train_df),
        eval_set=[(x_valid_lgb, y_valid)], eval_group=[group_sizes(valid_df)],
        callbacks=[early_stopping(stopping_rounds=45, verbose=False)],
    )
    lgb_valid_raw = lgbm.predict(x_valid_lgb)
    lgb_calibrator = safe_isotonic(lgb_valid_raw, y_valid)
    lgb_valid_prob = lgb_calibrator.predict(lgb_valid_raw)
    lgb_test_prob = lgb_calibrator.predict(lgbm.predict(x_test_lgb))

    # CatBoost natively receives raw categorical strings and race group identifiers.
    x_train_cat = train_df[ALL_FEATURES].copy(); x_valid_cat = valid_df[ALL_FEATURES].copy(); x_test_cat = test_df[ALL_FEATURES].copy()
    cat_indices = [x_train_cat.columns.get_loc(name) for name in CATEGORICAL_FEATURES]
    train_pool = Pool(x_train_cat, y_train, group_id=train_df["race_group"], cat_features=cat_indices)
    valid_pool = Pool(x_valid_cat, y_valid, group_id=valid_df["race_group"], cat_features=cat_indices)
    catboost = make_catboost()
    catboost.fit(train_pool, eval_set=valid_pool, early_stopping_rounds=45, verbose=False)
    cat_valid_raw = catboost.predict(valid_pool)
    cat_calibrator = safe_isotonic(cat_valid_raw, y_valid)
    cat_valid_prob = cat_calibrator.predict(cat_valid_raw)
    cat_test_prob = cat_calibrator.predict(catboost.predict(Pool(x_test_cat, cat_features=cat_indices)))

    weights = model_weights(valid_df, lgb_valid_prob, cat_valid_prob)
    blended_test = weights["lightgbm"] * lgb_test_prob + weights["catboost"] * cat_test_prob
    result_df = test_df[KEY_COLUMNS + ["race_group"]].copy()
    result_df["lightgbm_calibrated_probability"] = lgb_test_prob
    result_df["catboost_calibrated_probability"] = cat_test_prob
    result_df["ensemble_raw_probability"] = blended_test
    result_df["race_normalized_probability"] = normalize_by_race(result_df, "ensemble_raw_probability")
    result_df["model_rank"] = result_df.groupby("race_group")["race_normalized_probability"].rank(method="first", ascending=False).astype(int)
    result_df.to_csv(predictions_path, index=False, encoding="utf-8-sig")

    lgb_names = list(preprocessor.get_feature_names_out())
    lgb_importance = sorted(({"feature": name, "importance": int(value)} for name, value in zip(lgb_names, lgbm.feature_importances_)), key=lambda item: item["importance"], reverse=True)
    cat_values = np.asarray(catboost.get_feature_importance(type="PredictionValuesChange"), dtype=float).reshape(-1)
    cat_importance = sorted(
        ({"feature": name, "importance": float(value)} for name, value in zip(ALL_FEATURES, cat_values)),
        key=lambda item: item["importance"], reverse=True,
    )
    test_metrics = {
        "brier_score": float(brier_score_loss(y_test, np.clip(blended_test, 1e-6, 1 - 1e-6))),
        "log_loss": float(log_loss(y_test, np.clip(blended_test, 1e-6, 1 - 1e-6))),
        "roc_auc": float(roc_auc_score(y_test, blended_test)),
    }
    report: dict[str, Any] = {
        "model": "HKJC V10.2 LightGBM + CatBoost ranking ensemble",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "feature_version": "elo_features_v10_2_advanced",
        "target": "target_win",
        "ensemble_weights": weights,
        "lightgbm_best_iteration": int(lgbm.best_iteration_ or lgbm.n_estimators),
        "catboost_best_iteration": int(catboost.get_best_iteration() or catboost.get_params().get("iterations", 700)),
        "split": {
            "train": {"rows": len(train_df), "from": str(train_df.race_date.min().date()), "to": str(train_df.race_date.max().date())},
            "validation": {"rows": len(valid_df), "from": str(valid_df.race_date.min().date()), "to": str(valid_df.race_date.max().date())},
            "test": {"rows": len(test_df), "from": str(test_df.race_date.min().date()), "to": str(test_df.race_date.max().date())},
        },
        "test_row_metrics": test_metrics,
        "test_race_metrics": race_level_metrics(result_df.rename(columns={"race_normalized_probability": "model_probability"}), "model_probability"),
        "top_lightgbm_feature_importance": lgb_importance[:20],
        "top_catboost_feature_importance": cat_importance[:20],
        "limitations": [
            "賠率落飛資料只在預測時由賽前雙快照產生；因目前歷史資料庫未有足夠的標籤化快照，未被用作訓練特徵。",
            "新馬血統與試閘先驗在官方資料可得時以低權重特徵使用；未知資料維持中性值並產生可用性警示。",
            "位置機率由集成獨贏強度的名次模擬推導，不是獨立訓練的位置模型。",
            "所有驗證均為時間外歷史研究，不代表未來賽果或回報保證。",
        ],
    }
    bundle = {
        "bundle_type": "v10_2_ensemble", "lightgbm_model": lgbm, "lightgbm_preprocessor": preprocessor,
        "lightgbm_calibrator": lgb_calibrator, "catboost_model": catboost, "catboost_calibrator": cat_calibrator,
        "catboost_categorical_indices": cat_indices, "ensemble_weights": weights,
        "numeric_features": NUMERIC_FEATURES, "categorical_features": CATEGORICAL_FEATURES,
        "all_features": ALL_FEATURES, "feature_version": "elo_features_v10_2_advanced", "report": report,
    }
    joblib.dump(bundle, model_path)
    Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="訓練 V10.2 LightGBM＋CatBoost 集成勝率模型")
    parser.add_argument("--db", default="hkjc_last_season.sqlite")
    parser.add_argument("--model", default="horse_model.pkl")
    parser.add_argument("--report", default="lightgbm_training_report.json")
    parser.add_argument("--predictions", default="lightgbm_backtest_predictions.csv")
    args = parser.parse_args()
    train(args.db, args.model, args.report, args.predictions)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
