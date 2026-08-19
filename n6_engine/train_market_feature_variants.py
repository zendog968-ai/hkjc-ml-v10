#!/usr/bin/env python3
"""Compare isolated N6 market-feature simplification variants.

Variants are trained only on the chronological N6 training window.  They never
write production N6 artifacts, the API contract, V10 runtime artifacts, or the
immutable V10 source database.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
import torch
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.metrics import log_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from n6.config import CATEGORICAL_FEATURES, MODELS_DIR, NUMERIC_FEATURES, RANDOM_SEED, REPORTS_DIR, TARGET_COLUMN
from n6.feature_engineering import load_training_frame, score_to_race_probabilities, source_inventory
from train import chronological_split, infer_logits, race_metrics, select_temperature, set_reproducibility, train_model

EXPERIMENT_ID = "market_feature_variants_v1"
EXPERIMENT_DIR = MODELS_DIR / "candidates" / EXPERIMENT_ID
REPORT_DIR = REPORTS_DIR / "candidates" / EXPERIMENT_ID
BASELINE_REPORT_PATH = REPORTS_DIR / "n6_training_report.json"
ORTHOGONAL_RESIDUAL = "market_log_odds_orthogonal_residual"


@dataclass(frozen=True)
class Variant:
    name: str
    description: str
    numeric_features: list[str]
    needs_orthogonal_residual: bool = False


VARIANTS = [
    Variant(
        name="implied_probability_only",
        description="Remove market_log_odds and retain market_implied_probability plus market_odds_available.",
        numeric_features=[feature for feature in NUMERIC_FEATURES if feature != "market_log_odds"],
    ),
    Variant(
        name="orthogonalized_log_odds_residual",
        description="Replace market_log_odds with a training-window linear residual after projection on market_implied_probability; retain implied probability and availability.",
        numeric_features=[
            ORTHOGONAL_RESIDUAL if feature == "market_log_odds" else feature
            for feature in NUMERIC_FEATURES
        ],
        needs_orthogonal_residual=True,
    ),
]


def make_preprocessor(numeric_features: list[str]) -> ColumnTransformer:
    return ColumnTransformer([
        ("numeric", Pipeline([
            ("impute", SimpleImputer(strategy="median", add_indicator=True)),
            ("scale", StandardScaler()),
        ]), numeric_features),
        ("categorical", Pipeline([
            ("impute", SimpleImputer(strategy="constant", fill_value="未知")),
            ("encode", OneHotEncoder(handle_unknown="ignore", sparse_output=False)),
        ]), CATEGORICAL_FEATURES),
    ], remainder="drop", verbose_feature_names_out=False)


def baseline_metrics() -> dict[str, Any]:
    report = json.loads(BASELINE_REPORT_PATH.read_text(encoding="utf-8"))
    return {
        "model_path": report["artifacts"]["model"],
        "test_race_metrics": report["test_race_metrics"],
        "test_row_metrics": report["test_row_metrics"],
        "split": report["split"],
    }


def fit_orthogonal_residual(train: pd.DataFrame, all_frames: list[pd.DataFrame]) -> tuple[float, float, list[pd.DataFrame], dict[str, float | int]]:
    valid = train[["market_implied_probability", "market_log_odds"]].dropna()
    if len(valid) < 20:
        raise ValueError("Insufficient valid market observations for orthogonalization.")
    x = valid["market_implied_probability"].to_numpy(dtype=float)
    y = valid["market_log_odds"].to_numpy(dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    result: list[pd.DataFrame] = []
    for frame in all_frames:
        output = frame.copy()
        x_all = pd.to_numeric(output["market_implied_probability"], errors="coerce")
        y_all = pd.to_numeric(output["market_log_odds"], errors="coerce")
        output[ORTHOGONAL_RESIDUAL] = y_all - (intercept + slope * x_all)
        result.append(output)
    train_residual = result[0][ORTHOGONAL_RESIDUAL]
    stats = {
        "train_valid_rows": int(len(valid)),
        "slope": float(slope),
        "intercept": float(intercept),
        "train_correlation_original": float(np.corrcoef(x, y)[0, 1]),
        "train_correlation_implied_vs_residual": float(pd.concat([result[0]["market_implied_probability"], train_residual], axis=1).dropna().corr().iloc[0, 1]),
    }
    return float(slope), float(intercept), result, stats


def train_variant(variant: Variant, frame: pd.DataFrame, baseline: dict[str, Any], epochs: int) -> dict[str, Any]:
    set_reproducibility(RANDOM_SEED)
    train_frame, validation_frame, test_frame = chronological_split(frame)
    transform_stats: dict[str, Any] = {"method": "feature removal"}
    if variant.needs_orthogonal_residual:
        _, _, transformed, transform_stats = fit_orthogonal_residual(train_frame, [train_frame, validation_frame, test_frame])
        train_frame, validation_frame, test_frame = transformed
    all_features = [*variant.numeric_features, *CATEGORICAL_FEATURES]
    preprocessor = make_preprocessor(variant.numeric_features)
    train_values = np.asarray(preprocessor.fit_transform(train_frame[all_features]), dtype=np.float32)
    validation_values = np.asarray(preprocessor.transform(validation_frame[all_features]), dtype=np.float32)
    test_values = np.asarray(preprocessor.transform(test_frame[all_features]), dtype=np.float32)
    model, history = train_model(train_values, validation_values, train_frame, validation_frame, epochs=epochs)
    validation_logits = infer_logits(model, validation_values)
    temperature, validation_brier = select_temperature(validation_logits, validation_frame)
    test_logits = infer_logits(model, test_values)
    probabilities = score_to_race_probabilities(test_logits / temperature, test_frame["race_group"])
    predictions = test_frame[["race_date", "racecourse", "race_no", "horse_name", TARGET_COLUMN, "race_group"]].copy()
    predictions["neural_logit"] = test_logits
    predictions["neural_score"] = probabilities
    predictions["neural_rank"] = predictions.groupby("race_group")["neural_score"].rank(method="first", ascending=False).astype(int)
    race_result = race_metrics(predictions, "neural_score")
    row_probability = np.clip(probabilities, 1e-6, 1.0 - 1e-6)
    row_result = {"roc_auc": float(roc_auc_score(test_frame[TARGET_COLUMN], row_probability)), "binary_log_loss": float(log_loss(test_frame[TARGET_COLUMN], row_probability))}
    baseline_brier = float(baseline["test_race_metrics"]["mean_race_brier_score"])
    variant_dir = EXPERIMENT_DIR / variant.name
    report_dir = REPORT_DIR / variant.name
    variant_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    model_path = variant_dir / "n6_mlp_candidate.pt"
    preprocessor_path = variant_dir / "n6_preprocessor_candidate.joblib"
    prediction_path = report_dir / "n6_test_predictions_candidate.csv"
    report_path = report_dir / "n6_candidate_training_report.json"
    predictions.to_csv(prediction_path, index=False, encoding="utf-8-sig")
    comparison = {
        "baseline_race_brier": baseline_brier,
        "candidate_race_brier": float(race_result["mean_race_brier_score"]),
        "race_brier_delta_candidate_minus_baseline": float(race_result["mean_race_brier_score"] - baseline_brier),
        "relative_race_brier_change": float((race_result["mean_race_brier_score"] - baseline_brier) / baseline_brier),
        "candidate_improves_brier": bool(race_result["mean_race_brier_score"] < baseline_brier),
        "baseline_top_pick_win_rate": float(baseline["test_race_metrics"]["top_pick_win_rate"]),
        "candidate_top_pick_win_rate": float(race_result["top_pick_win_rate"]),
        "top_pick_win_rate_delta": float(race_result["top_pick_win_rate"] - baseline["test_race_metrics"]["top_pick_win_rate"]),
        "baseline_roc_auc": float(baseline["test_row_metrics"]["roc_auc"]),
        "candidate_roc_auc": float(row_result["roc_auc"]),
        "roc_auc_delta": float(row_result["roc_auc"] - baseline["test_row_metrics"]["roc_auc"]),
    }
    report = {
        "engine": "N6 Neural Calculation Engine",
        "experiment_id": EXPERIMENT_ID,
        "variant": variant.name,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "candidate_only_guarantee": "No production N6 artifact, API service, V10 database, or V10 runtime artifact is written.",
        "feature_change": {"description": variant.description, "numeric_features": variant.numeric_features, "raw_feature_count": len(all_features), "transformation_statistics": transform_stats},
        "architecture": {"type": "MLP", "input_dim": int(train_values.shape[1]), "hidden_dims": [128, 64, 32], "dropout": 0.15},
        "split": {"train_rows": len(train_frame), "validation_rows": len(validation_frame), "test_rows": len(test_frame)},
        "training": {key: value for key, value in history.items() if key != "history"},
        "calibration": {"temperature": temperature, "validation_race_brier": validation_brier},
        "test_race_metrics": race_result,
        "test_row_metrics": row_result,
        "comparison": comparison,
        "artifacts": {"model": str(model_path), "preprocessor": str(preprocessor_path), "predictions": str(prediction_path)},
    }
    artifact = {"artifact_type": "n6_race_mlp_candidate", "experiment_id": EXPERIMENT_ID, "variant": variant.name, "state_dict": model.state_dict(), "input_dim": int(train_values.shape[1]), "hidden_dims": [128, 64, 32], "dropout": 0.15, "temperature": temperature, "feature_names": list(preprocessor.get_feature_names_out()), "feature_contract": all_features, "report": report}
    torch.save(artifact, model_path)
    joblib.dump(preprocessor, preprocessor_path)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Train isolated N6 market-feature simplification variants.")
    parser.add_argument("--epochs", type=int, default=160)
    arguments = parser.parse_args()
    source = source_inventory()
    frame = load_training_frame()
    baseline = baseline_metrics()
    reports = [train_variant(variant, frame, baseline, max(20, arguments.epochs)) for variant in VARIANTS]
    summary = {
        "engine": "N6 Neural Calculation Engine",
        "experiment_id": EXPERIMENT_ID,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "source": source,
        "baseline": baseline,
        "variants": [{"variant": report["variant"], "comparison": report["comparison"], "feature_change": report["feature_change"], "artifacts": report["artifacts"]} for report in reports],
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    summary_path = REPORT_DIR / "market_feature_variants_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
