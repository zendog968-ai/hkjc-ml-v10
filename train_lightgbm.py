#!/usr/bin/env python3
"""Train a leakage-safe LightGBM win-probability model from pre-race ELO features."""
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
from lightgbm import LGBMClassifier, early_stopping
from sklearn.compose import ColumnTransformer
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
from sklearn.preprocessing import OneHotEncoder

NUMERIC_FEATURES = [
    "distance_m", "field_size", "draw", "draw_pct", "weight_lbs", "weight_delta",
    "horse_elo_pre", "horse_condition_elo_pre", "jockey_elo_pre", "trainer_win_rate_pre",
    "horse_win_rate_pre", "horse_top3_rate_pre", "condition_win_rate_pre",
    "recent_finish_fraction_pre", "recent_margin_pre", "recent_win_rate_pre",
    "closing400_proxy_pre", "closing400_trend_pre", "elo_vs_field", "jockey_elo_vs_field",
    "track_bias_pre", "track_bias_sample_pre", "class_level", "class_drop_from_last_pre",
    "class_weight_interaction_pre", "is_first_time_blinker", "is_equip_added",
    "equipment_changed", "equipment_history_known_pre",
    "trainer_equip_change_roi_pre", "trainer_equip_change_sample_pre",
]
CATEGORICAL_FEATURES = ["racecourse", "race_class", "surface", "course_config", "going"]
ALL_FEATURES = NUMERIC_FEATURES + CATEGORICAL_FEATURES
KEY_COLUMNS = ["race_date", "racecourse", "race_no", "horse_name", "target_win"]


def load_feature_table(db_path: str) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    selected_columns = list(dict.fromkeys(KEY_COLUMNS + ALL_FEATURES))
    query = "SELECT " + ",".join(selected_columns) + " FROM elo_feature_store ORDER BY race_date,race_no,horse_name"
    df = pd.read_sql_query(query, conn)
    conn.close()
    if df.empty:
        raise ValueError("elo_feature_store 為空；請先執行 build_elo_features.py。")
    df["race_date"] = pd.to_datetime(df["race_date"])
    for col in NUMERIC_FEATURES:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    for col in CATEGORICAL_FEATURES:
        df[col] = df[col].fillna("未知").astype(str)
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


def normalize_by_race(df: pd.DataFrame, probability_column: str) -> pd.Series:
    values = df[probability_column].clip(lower=1e-6)
    sums = values.groupby([df["race_date"], df["racecourse"], df["race_no"]]).transform("sum")
    return values / sums


def race_level_metrics(df: pd.DataFrame, probability_column: str) -> dict[str, float]:
    data = df.copy()
    data["race_probability"] = normalize_by_race(data, probability_column)
    grouped = data.groupby(["race_date", "racecourse", "race_no"], sort=False)
    top_pick_wins = []
    top3_has_winner = []
    brier_scores = []
    uniform_brier_scores = []
    for _, group in grouped:
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


def train(db_path: str, model_path: str, report_path: str, predictions_path: str) -> dict[str, Any]:
    df = load_feature_table(db_path)
    train_df, valid_df, test_df = chronological_split(df)
    preprocessor = ColumnTransformer(
        transformers=[
            ("num", "passthrough", NUMERIC_FEATURES),
            ("cat", OneHotEncoder(handle_unknown="ignore"), CATEGORICAL_FEATURES),
        ],
        remainder="drop",
    )
    x_train = preprocessor.fit_transform(train_df[ALL_FEATURES])
    x_valid = preprocessor.transform(valid_df[ALL_FEATURES])
    x_test = preprocessor.transform(test_df[ALL_FEATURES])
    y_train = train_df["target_win"].astype(int)
    y_valid = valid_df["target_win"].astype(int)
    y_test = test_df["target_win"].astype(int)

    model = LGBMClassifier(
        objective="binary",
        learning_rate=0.03,
        n_estimators=700,
        num_leaves=15,
        max_depth=4,
        min_child_samples=55,
        subsample=0.85,
        colsample_bytree=0.85,
        reg_alpha=0.3,
        reg_lambda=1.5,
        random_state=20260812,
        n_jobs=1,
        verbosity=-1,
    )
    model.fit(
        x_train,
        y_train,
        eval_set=[(x_valid, y_valid)],
        eval_metric="binary_logloss",
        callbacks=[early_stopping(stopping_rounds=45, verbose=False)],
    )
    valid_raw = model.predict_proba(x_valid)[:, 1]
    calibrator = IsotonicRegression(out_of_bounds="clip")
    calibrator.fit(valid_raw, y_valid)
    test_raw = model.predict_proba(x_test)[:, 1]
    test_calibrated = calibrator.predict(test_raw)

    result_df = test_df[KEY_COLUMNS].copy()
    result_df["raw_probability"] = test_raw
    result_df["calibrated_probability"] = test_calibrated
    result_df["race_normalized_probability"] = normalize_by_race(
        result_df.rename(columns={"calibrated_probability": "probability"}), "probability"
    )
    result_df["model_rank"] = result_df.groupby(["race_date", "racecourse", "race_no"])["race_normalized_probability"].rank(method="first", ascending=False).astype(int)
    result_df.to_csv(predictions_path, index=False, encoding="utf-8-sig")

    feature_names = list(preprocessor.get_feature_names_out())
    feature_importance = sorted(
        (
            {"feature": name, "importance": int(score)}
            for name, score in zip(feature_names, model.feature_importances_)
        ),
        key=lambda item: item["importance"],
        reverse=True,
    )
    test_row_metrics = {
        "brier_score": float(brier_score_loss(y_test, test_calibrated)),
        "log_loss": float(log_loss(y_test, np.clip(test_calibrated, 1e-6, 1 - 1e-6))),
        "roc_auc": float(roc_auc_score(y_test, test_calibrated)),
    }
    report = {
        "model": "HKJC V10.1 LightGBM equipment v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "feature_version": "elo_features_v10_1_equipment",
        "target": "target_win",
        "feature_count_after_encoding": len(feature_names),
        "best_iteration": int(model.best_iteration_ or model.n_estimators),
        "split": {
            "train": {"rows": len(train_df), "from": str(train_df.race_date.min().date()), "to": str(train_df.race_date.max().date())},
            "validation": {"rows": len(valid_df), "from": str(valid_df.race_date.min().date()), "to": str(valid_df.race_date.max().date())},
            "test": {"rows": len(test_df), "from": str(test_df.race_date.min().date()), "to": str(test_df.race_date.max().date())},
        },
        "test_row_metrics": test_row_metrics,
        "test_race_metrics": race_level_metrics(
            result_df.rename(columns={"race_normalized_probability": "model_probability"}), "model_probability"
        ),
        "top_feature_importance": feature_importance[:20],
        "limitations": [
            "末段400米為根據官方沿途走位及頭馬距離建立的代理指標，並非個別馬匹實測分段時間。",
            "模型未使用未來賽果；測試集採最後15%賽日作時間外驗證。",
            "市場賠率不會進入模型訓練，僅在預測時用於比較模型概率與市場隱含機率。",
            "跑道偏差為同一賽道設定、場地狀況、路程組別及內中外檔的歷史先驗；採預期勝率平滑、樣本可靠度收縮至 1.0 及 ±0.25 邊界，並不保證當日偏差持續存在。",
            "班次變化以相鄰兩仗的香港班次級別計算；新馬或非標準班次使用中性級別處理。",
            "裝備特徵來自香港賽馬會官方排位及馬匹近績配備欄。首次眼罩、任何新增配備及配備變動均只使用該場之前的資料；未知歷史採中性值。",
            "trainer_equip_change_roi_pre 是近兩年裝備變動馬的平滑勝率相對馬房基準權重，並非按投注派彩計算的字面 ROI；權重設有 0.5 至 1.5 邊界。",
        ],
    }
    bundle = {
        "model": model,
        "preprocessor": preprocessor,
        "calibrator": calibrator,
        "numeric_features": NUMERIC_FEATURES,
        "categorical_features": CATEGORICAL_FEATURES,
        "all_features": ALL_FEATURES,
        "feature_version": "elo_features_v10_1_equipment",
        "report": report,
    }
    joblib.dump(bundle, model_path)
    Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="訓練香港賽馬 LightGBM 勝率模型")
    parser.add_argument("--db", default="hkjc_last_season.sqlite")
    parser.add_argument("--model", default="horse_model.pkl")
    parser.add_argument("--report", default="lightgbm_training_report.json")
    parser.add_argument("--predictions", default="lightgbm_backtest_predictions.csv")
    args = parser.parse_args()
    train(args.db, args.model, args.report, args.predictions)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
