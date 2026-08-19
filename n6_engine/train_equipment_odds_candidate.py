#!/usr/bin/env python3
"""Train an isolated N6 candidate with equipment-change × odds-band features.

This experiment never writes N6's production model, preprocessor, API contract,
or V10 database. Candidate artifacts are stored under N6-owned candidates paths.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

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
from n6.feature_engineering import (
    EQUIPMENT_ODDS_INTERACTION_FEATURES,
    add_equipment_odds_interaction_features,
    load_training_frame,
    score_to_race_probabilities,
    source_inventory,
)
from train import chronological_split, infer_logits, race_metrics, select_temperature, set_reproducibility, train_model

EXPERIMENT_ID = "equipment_odds_interaction_v1"
CANDIDATE_NUMERIC_FEATURES = [*NUMERIC_FEATURES, *EQUIPMENT_ODDS_INTERACTION_FEATURES]
CANDIDATE_ALL_FEATURES = [*CANDIDATE_NUMERIC_FEATURES, *CATEGORICAL_FEATURES]
CANDIDATE_DIR = MODELS_DIR / "candidates" / EXPERIMENT_ID
CANDIDATE_REPORTS_DIR = REPORTS_DIR / "candidates" / EXPERIMENT_ID
CANDIDATE_MODEL_PATH = CANDIDATE_DIR / "n6_mlp_candidate.pt"
CANDIDATE_PREPROCESSOR_PATH = CANDIDATE_DIR / "n6_preprocessor_candidate.joblib"
CANDIDATE_PREDICTIONS_PATH = CANDIDATE_REPORTS_DIR / "n6_test_predictions_candidate.csv"
CANDIDATE_REPORT_PATH = CANDIDATE_REPORTS_DIR / "n6_candidate_training_report.json"
BASELINE_REPORT_PATH = REPORTS_DIR / "n6_training_report.json"


def make_candidate_preprocessor() -> ColumnTransformer:
    numeric_pipeline = Pipeline([
        ("impute", SimpleImputer(strategy="median", add_indicator=True)),
        ("scale", StandardScaler()),
    ])
    categorical_pipeline = Pipeline([
        ("impute", SimpleImputer(strategy="constant", fill_value="未知")),
        ("encode", OneHotEncoder(handle_unknown="ignore", sparse_output=False)),
    ])
    return ColumnTransformer([
        ("numeric", numeric_pipeline, CANDIDATE_NUMERIC_FEATURES),
        ("categorical", categorical_pipeline, CATEGORICAL_FEATURES),
    ], remainder="drop", verbose_feature_names_out=False)


def baseline_metrics() -> dict:
    if not BASELINE_REPORT_PATH.is_file():
        raise FileNotFoundError(f"Missing production baseline report: {BASELINE_REPORT_PATH}")
    report = json.loads(BASELINE_REPORT_PATH.read_text(encoding="utf-8"))
    return {
        "model_path": str(report.get("artifacts", {}).get("model", "unknown")),
        "test_race_metrics": report["test_race_metrics"],
        "test_row_metrics": report["test_row_metrics"],
        "split": report["split"],
    }


def run_experiment(epochs: int) -> dict:
    set_reproducibility(RANDOM_SEED)
    CANDIDATE_DIR.mkdir(parents=True, exist_ok=True)
    CANDIDATE_REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    baseline = baseline_metrics()
    source = source_inventory()
    frame = add_equipment_odds_interaction_features(load_training_frame())
    train_frame, validation_frame, test_frame = chronological_split(frame)
    preprocessor = make_candidate_preprocessor()
    train_values = np.asarray(preprocessor.fit_transform(train_frame[CANDIDATE_ALL_FEATURES]), dtype=np.float32)
    validation_values = np.asarray(preprocessor.transform(validation_frame[CANDIDATE_ALL_FEATURES]), dtype=np.float32)
    test_values = np.asarray(preprocessor.transform(test_frame[CANDIDATE_ALL_FEATURES]), dtype=np.float32)
    model, history = train_model(train_values, validation_values, train_frame, validation_frame, epochs=epochs)

    validation_logits = infer_logits(model, validation_values)
    temperature, validation_calibration_brier = select_temperature(validation_logits, validation_frame)
    test_logits = infer_logits(model, test_values)
    test_probabilities = score_to_race_probabilities(test_logits / temperature, test_frame["race_group"])
    predictions = test_frame[["race_date", "racecourse", "race_no", "horse_name", TARGET_COLUMN, "race_group"]].copy()
    predictions["neural_logit"] = test_logits
    predictions["neural_score"] = test_probabilities
    predictions["neural_rank"] = predictions.groupby("race_group")["neural_score"].rank(method="first", ascending=False).astype(int)
    predictions.to_csv(CANDIDATE_PREDICTIONS_PATH, index=False, encoding="utf-8-sig")

    candidate_race_metrics = race_metrics(predictions, "neural_score")
    row_probability = np.clip(test_probabilities, 1e-6, 1.0 - 1e-6)
    candidate_row_metrics = {
        "roc_auc": float(roc_auc_score(test_frame[TARGET_COLUMN], row_probability)),
        "binary_log_loss": float(log_loss(test_frame[TARGET_COLUMN], row_probability)),
    }
    baseline_brier = float(baseline["test_race_metrics"]["mean_race_brier_score"])
    candidate_brier = float(candidate_race_metrics["mean_race_brier_score"])
    comparison = {
        "baseline_race_brier": baseline_brier,
        "candidate_race_brier": candidate_brier,
        "race_brier_delta_candidate_minus_baseline": candidate_brier - baseline_brier,
        "relative_race_brier_change": (candidate_brier - baseline_brier) / baseline_brier,
        "candidate_improves_brier": candidate_brier < baseline_brier,
        "baseline_top_pick_win_rate": float(baseline["test_race_metrics"]["top_pick_win_rate"]),
        "candidate_top_pick_win_rate": float(candidate_race_metrics["top_pick_win_rate"]),
        "top_pick_win_rate_delta": float(candidate_race_metrics["top_pick_win_rate"] - baseline["test_race_metrics"]["top_pick_win_rate"]),
        "baseline_roc_auc": float(baseline["test_row_metrics"]["roc_auc"]),
        "candidate_roc_auc": float(candidate_row_metrics["roc_auc"]),
        "roc_auc_delta": float(candidate_row_metrics["roc_auc"] - baseline["test_row_metrics"]["roc_auc"]),
    }
    report = {
        "engine": "N6 Neural Calculation Engine",
        "experiment_id": EXPERIMENT_ID,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "experiment_guarantee": "Candidate-only experiment; production N6 artifacts, N6 API service, V10 database, and V10 runtime artifacts are not written.",
        "source": source,
        "feature_change": {
            "added_numeric_features": EQUIPMENT_ODDS_INTERACTION_FEATURES,
            "definition": "equipment_changed > 0 AND historical starters.win_odds falls in [1,5), [5,10), [10,20), or [20,+∞). Missing/invalid odds activate no interaction.",
            "baseline_raw_feature_count": len(NUMERIC_FEATURES) + len(CATEGORICAL_FEATURES),
            "candidate_raw_feature_count": len(CANDIDATE_ALL_FEATURES),
        },
        "architecture": {"type": "MLP", "input_dim": int(train_values.shape[1]), "hidden_dims": [128, 64, 32], "dropout": 0.15, "activation": "GELU"},
        "split": {
            "train": {"rows": len(train_frame), "from": str(train_frame.race_date.min().date()), "to": str(train_frame.race_date.max().date())},
            "validation": {"rows": len(validation_frame), "from": str(validation_frame.race_date.min().date()), "to": str(validation_frame.race_date.max().date())},
            "test": {"rows": len(test_frame), "from": str(test_frame.race_date.min().date()), "to": str(test_frame.race_date.max().date())},
        },
        "training": {key: value for key, value in history.items() if key != "history"},
        "calibration": {"method": "race-level temperature scaling on validation window", "temperature": temperature, "validation_race_brier": validation_calibration_brier},
        "test_race_metrics": candidate_race_metrics,
        "test_row_metrics": candidate_row_metrics,
        "baseline_reference": baseline,
        "comparison": comparison,
        "artifacts": {"model": str(CANDIDATE_MODEL_PATH), "preprocessor": str(CANDIDATE_PREPROCESSOR_PATH), "predictions": str(CANDIDATE_PREDICTIONS_PATH)},
    }
    artifact = {
        "artifact_type": "n6_race_mlp_candidate",
        "experiment_id": EXPERIMENT_ID,
        "state_dict": model.state_dict(),
        "input_dim": int(train_values.shape[1]),
        "hidden_dims": [128, 64, 32],
        "dropout": 0.15,
        "temperature": temperature,
        "feature_names": list(preprocessor.get_feature_names_out()),
        "feature_contract": CANDIDATE_ALL_FEATURES,
        "report": report,
    }
    torch.save(artifact, CANDIDATE_MODEL_PATH)
    joblib.dump(preprocessor, CANDIDATE_PREPROCESSOR_PATH)
    CANDIDATE_REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Train N6 candidate with equipment × odds interaction features.")
    parser.add_argument("--epochs", type=int, default=160)
    arguments = parser.parse_args()
    report = run_experiment(epochs=max(20, arguments.epochs))
    print(json.dumps({
        "experiment_id": report["experiment_id"],
        "comparison": report["comparison"],
        "candidate_artifacts": report["artifacts"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
