#!/usr/bin/env python3
"""Train N6 on V10's immutable historical feature store and write N6-owned artifacts."""

from __future__ import annotations

import argparse
import copy
import json
import random
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
from torch import nn
from torch.utils.data import DataLoader, TensorDataset

from n6.config import (
    ALL_FEATURES,
    CATEGORICAL_FEATURES,
    MODEL_PATH,
    NUMERIC_FEATURES,
    PREPROCESSOR_PATH,
    RANDOM_SEED,
    TARGET_COLUMN,
    TEST_PREDICTIONS_PATH,
    TRAINING_REPORT_PATH,
)
from n6.feature_engineering import load_training_frame, score_to_race_probabilities, source_inventory
from n6.model import RaceMLP


def set_reproducibility(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True, warn_only=True)


def chronological_split(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    dates = sorted(frame["race_date"].dt.strftime("%Y-%m-%d").unique())
    if len(dates) < 20:
        raise ValueError("N6 requires at least 20 distinct race days for chronological evaluation.")
    train_end = dates[max(1, int(len(dates) * 0.70)) - 1]
    validation_end = dates[max(2, int(len(dates) * 0.85)) - 1]
    train = frame[frame["race_date"] <= train_end].copy()
    validation = frame[(frame["race_date"] > train_end) & (frame["race_date"] <= validation_end)].copy()
    test = frame[frame["race_date"] > validation_end].copy()
    if min(len(train), len(validation), len(test)) == 0:
        raise ValueError("Chronological split produced an empty partition.")
    return train, validation, test


def make_preprocessor() -> ColumnTransformer:
    numeric_pipeline = Pipeline([
        ("impute", SimpleImputer(strategy="median", add_indicator=True)),
        ("scale", StandardScaler()),
    ])
    categorical_pipeline = Pipeline([
        ("impute", SimpleImputer(strategy="constant", fill_value="未知")),
        ("encode", OneHotEncoder(handle_unknown="ignore", sparse_output=False)),
    ])
    return ColumnTransformer([
        ("numeric", numeric_pipeline, NUMERIC_FEATURES),
        ("categorical", categorical_pipeline, CATEGORICAL_FEATURES),
    ], remainder="drop", verbose_feature_names_out=False)


def race_metrics(frame: pd.DataFrame, probability_column: str) -> dict[str, float | int]:
    rows = []
    for _, race in frame.groupby("race_group", sort=False):
        ordered = race.sort_values(probability_column, ascending=False)
        winner_count = int(race[TARGET_COLUMN].sum())
        if winner_count != 1:
            continue
        uniform = np.full(len(race), 1.0 / len(race))
        probabilities = ordered[probability_column].to_numpy(dtype=float)
        labels = ordered[TARGET_COLUMN].to_numpy(dtype=float)
        rows.append({
            "top_pick_win": int(ordered.iloc[0][TARGET_COLUMN] == 1),
            "top3_contains_winner": int(ordered.iloc[:3][TARGET_COLUMN].sum() > 0),
            "race_brier": float(np.sum((probabilities - labels) ** 2)),
            "uniform_race_brier": float(np.sum((uniform - labels) ** 2)),
        })
    if not rows:
        raise ValueError("No completed single-winner races available for evaluation.")
    summary = pd.DataFrame(rows)
    return {
        "races": int(len(summary)),
        "top_pick_win_rate": float(summary["top_pick_win"].mean()),
        "top3_contains_winner_rate": float(summary["top3_contains_winner"].mean()),
        "mean_race_brier_score": float(summary["race_brier"].mean()),
        "mean_uniform_race_brier_score": float(summary["uniform_race_brier"].mean()),
        "race_brier_improvement_vs_uniform": float(
            summary["uniform_race_brier"].mean() - summary["race_brier"].mean()
        ),
    }


def infer_logits(model: RaceMLP, values: np.ndarray) -> np.ndarray:
    model.eval()
    with torch.no_grad():
        return model(torch.tensor(values, dtype=torch.float32)).cpu().numpy()


def select_temperature(logits: np.ndarray, frame: pd.DataFrame) -> tuple[float, float]:
    temperatures = np.linspace(0.35, 3.0, 54)
    best_temperature = 1.0
    best_brier = float("inf")
    for temperature in temperatures:
        candidate = score_to_race_probabilities(logits / temperature, frame["race_group"])
        trial = frame[["race_group", TARGET_COLUMN]].copy()
        trial["probability"] = candidate
        brier = float(race_metrics(trial, "probability")["mean_race_brier_score"])
        if brier < best_brier:
            best_temperature = float(temperature)
            best_brier = brier
    return best_temperature, best_brier


def train_model(
    train_values: np.ndarray,
    validation_values: np.ndarray,
    train_frame: pd.DataFrame,
    validation_frame: pd.DataFrame,
    epochs: int,
) -> tuple[RaceMLP, dict[str, Any]]:
    input_dim = int(train_values.shape[1])
    model = RaceMLP(input_dim=input_dim)
    positives = int(train_frame[TARGET_COLUMN].sum())
    negatives = len(train_frame) - positives
    positive_weight = max(1.0, negatives / max(positives, 1))
    criterion = nn.BCEWithLogitsLoss(pos_weight=torch.tensor(positive_weight, dtype=torch.float32))
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3, weight_decay=2e-4)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode="min", factor=0.5, patience=8)
    dataset = TensorDataset(
        torch.tensor(train_values, dtype=torch.float32),
        torch.tensor(train_frame[TARGET_COLUMN].to_numpy(dtype=np.float32), dtype=torch.float32),
    )
    loader = DataLoader(dataset, batch_size=min(512, len(dataset)), shuffle=True, generator=torch.Generator().manual_seed(RANDOM_SEED))
    best_state: dict[str, torch.Tensor] | None = None
    best_epoch = 0
    best_brier = float("inf")
    patience = 24
    stale_epochs = 0
    history: list[dict[str, float | int]] = []

    for epoch in range(1, epochs + 1):
        model.train()
        losses = []
        for batch_values, batch_labels in loader:
            optimizer.zero_grad(set_to_none=True)
            loss = criterion(model(batch_values), batch_labels)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), max_norm=2.0)
            optimizer.step()
            losses.append(float(loss.item()))
        validation_logits = infer_logits(model, validation_values)
        validation_probabilities = score_to_race_probabilities(validation_logits, validation_frame["race_group"])
        evaluation = validation_frame[["race_group", TARGET_COLUMN]].copy()
        evaluation["probability"] = validation_probabilities
        validation_brier = float(race_metrics(evaluation, "probability")["mean_race_brier_score"])
        scheduler.step(validation_brier)
        history.append({"epoch": epoch, "train_weighted_bce": float(np.mean(losses)), "validation_race_brier": validation_brier})
        if validation_brier < best_brier - 1e-7:
            best_brier = validation_brier
            best_epoch = epoch
            best_state = copy.deepcopy(model.state_dict())
            stale_epochs = 0
        else:
            stale_epochs += 1
            if stale_epochs >= patience:
                break
    if best_state is None:
        raise RuntimeError("N6 MLP did not produce a valid checkpoint.")
    model.load_state_dict(best_state)
    return model, {
        "best_epoch": best_epoch,
        "epochs_completed": len(history),
        "best_validation_race_brier": best_brier,
        "positive_class_weight": positive_weight,
        "history": history,
    }


def run_training(epochs: int) -> dict[str, Any]:
    set_reproducibility(RANDOM_SEED)
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    TRAINING_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)

    source = source_inventory()
    frame = load_training_frame()
    train_frame, validation_frame, test_frame = chronological_split(frame)
    preprocessor = make_preprocessor()
    train_values = np.asarray(preprocessor.fit_transform(train_frame[ALL_FEATURES]), dtype=np.float32)
    validation_values = np.asarray(preprocessor.transform(validation_frame[ALL_FEATURES]), dtype=np.float32)
    test_values = np.asarray(preprocessor.transform(test_frame[ALL_FEATURES]), dtype=np.float32)
    model, history = train_model(train_values, validation_values, train_frame, validation_frame, epochs=epochs)

    validation_logits = infer_logits(model, validation_values)
    temperature, calibration_brier = select_temperature(validation_logits, validation_frame)
    test_logits = infer_logits(model, test_values)
    test_probabilities = score_to_race_probabilities(test_logits / temperature, test_frame["race_group"])

    predictions = test_frame[["race_date", "racecourse", "race_no", "horse_name", TARGET_COLUMN, "race_group"]].copy()
    predictions["neural_logit"] = test_logits
    predictions["neural_score"] = test_probabilities
    predictions["neural_rank"] = predictions.groupby("race_group")["neural_score"].rank(method="first", ascending=False).astype(int)
    predictions.to_csv(TEST_PREDICTIONS_PATH, index=False, encoding="utf-8-sig")

    test_metrics = race_metrics(predictions, "neural_score")
    row_probability = np.clip(test_probabilities, 1e-6, 1 - 1e-6)
    test_row_metrics = {
        "roc_auc": float(roc_auc_score(test_frame[TARGET_COLUMN], row_probability)),
        "binary_log_loss": float(log_loss(test_frame[TARGET_COLUMN], row_probability)),
    }
    input_dim = int(train_values.shape[1])
    report: dict[str, Any] = {
        "engine": "N6 Neural Calculation Engine",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "source": source,
        "strict_read_only_guarantee": "V10 database opened only with SQLite mode=ro&immutable=1 and PRAGMA query_only=ON; all artifacts below are N6-owned.",
        "architecture": {"type": "MLP", "input_dim": input_dim, "hidden_dims": [128, 64, 32], "dropout": 0.15, "activation": "GELU"},
        "target": TARGET_COLUMN,
        "features": {"numeric": NUMERIC_FEATURES, "categorical": CATEGORICAL_FEATURES, "count_before_encoding": len(ALL_FEATURES)},
        "split": {
            "train": {"rows": len(train_frame), "from": str(train_frame.race_date.min().date()), "to": str(train_frame.race_date.max().date())},
            "validation": {"rows": len(validation_frame), "from": str(validation_frame.race_date.min().date()), "to": str(validation_frame.race_date.max().date())},
            "test": {"rows": len(test_frame), "from": str(test_frame.race_date.min().date()), "to": str(test_frame.race_date.max().date())},
        },
        "training": {key: value for key, value in history.items() if key != "history"},
        "calibration": {"method": "race-level temperature scaling on validation window", "temperature": temperature, "validation_race_brier": calibration_brier},
        "test_race_metrics": test_metrics,
        "test_row_metrics": test_row_metrics,
        "artifacts": {"model": str(MODEL_PATH), "preprocessor": str(PREPROCESSOR_PATH), "predictions": str(TEST_PREDICTIONS_PATH)},
        "limitations": [
            "時間外測試僅衡量歷史資料上的泛化能力，不構成未來賽果、機率或任何結果保證。",
            "賠率以 starters.win_odds 歷史欄位提供；缺漏時由可用性旗標與中位數處理，不會假設未觀測賠率。",
            "API 的未來賽事推理僅採用輸入的賽前資料及 elo_current_state；不會讀取未來結果或修改 V10。",
        ],
    }
    artifact = {
        "artifact_type": "n6_race_mlp",
        "state_dict": model.state_dict(),
        "input_dim": input_dim,
        "hidden_dims": [128, 64, 32],
        "dropout": 0.15,
        "temperature": temperature,
        "feature_names": list(preprocessor.get_feature_names_out()),
        "feature_contract": ALL_FEATURES,
        "report": report,
    }
    torch.save(artifact, MODEL_PATH)
    joblib.dump(preprocessor, PREPROCESSOR_PATH)
    TRAINING_REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Train N6 MLP on V10's immutable historical feature store.")
    parser.add_argument("--epochs", type=int, default=160)
    arguments = parser.parse_args()
    report = run_training(epochs=max(20, arguments.epochs))
    print(json.dumps({
        "model": report["artifacts"]["model"],
        "test_race_metrics": report["test_race_metrics"],
        "test_row_metrics": report["test_row_metrics"],
        "split": report["split"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
