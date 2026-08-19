#!/usr/bin/env python3
"""Candidate-only partial-pooling calibration with rank protection for rare goings.

The base N6 model is frozen.  For each expanding temporal fold, a parent-group
isotonic calibrator is trained using observations strictly before the test fold.
A local-going calibrator is trained only when it has prior examples.  Their
outputs are blended in log-score space with w = local_races/(local_races+tau),
then normalised within race and projected to preserve the original N6 ranking.

This program never writes to V10 and never modifies N6 production artifacts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

import joblib
import numpy as np
import pandas as pd
import torch
from sklearn.isotonic import IsotonicRegression

from n6.config import MODEL_PATH, PREPROCESSOR_PATH, RANDOM_SEED, REPORTS_DIR, TARGET_COLUMN
from n6.feature_engineering import load_training_frame, score_to_race_probabilities
from n6.model import load_model_bundle

EXPERIMENT_ID = "rare_going_partial_pooling_v1"
OUTPUT_DIR = REPORTS_DIR / "candidates" / EXPERIMENT_ID
MODEL_DIR = Path("models") / "candidates" / EXPERIMENT_ID
SUMMARY_PATH = OUTPUT_DIR / "partial_pooling_cv_summary.json"
FOLD_METRICS_PATH = OUTPUT_DIR / "partial_pooling_cv_fold_metrics.csv"
OOF_PATH = OUTPUT_DIR / "partial_pooling_cv_oof_predictions.csv"
REPORT_PATH = OUTPUT_DIR / "N6_RARE_GOING_PARTIAL_POOLING_CV.md"
ARTIFACT_PATH = MODEL_DIR / "partial_pooling_cv_artifact.joblib"
RARE_GOINGS = ("黏地", "軟地")
TAU_CANDIDATES = (20.0, 40.0, 80.0)
MIN_LOCAL_RACES = 5
N_SPLITS = 3
EPSILON = 1e-8


@dataclass(frozen=True)
class Fold:
    going: str
    fold_id: int
    calibration_end: pd.Timestamp
    test_races: tuple[str, ...]


def parent_group(going: str) -> str:
    """Map exact going labels to physically coherent parent groups."""
    text = str(going)
    if text == "封地":
        return "all_weather"
    if text in {"好地", "好地至快地"}:
        return "turf_dry"
    return "turf_wet"


def rank_protect(raw: np.ndarray, baseline: np.ndarray, race_groups: pd.Series) -> np.ndarray:
    """Preserve every original within-race rank before normalisation."""
    adjusted = np.asarray(raw, dtype=float).copy()
    identifiers = pd.Series(np.arange(len(adjusted)))
    for _, locations in identifiers.groupby(race_groups.to_numpy(), sort=False):
        idx = locations.to_numpy()
        order = np.argsort(-baseline[idx], kind="stable")
        ordered = idx[order]
        values = adjusted[ordered].copy()
        for position in range(1, len(values)):
            values[position] = min(values[position], values[position - 1] * (1.0 - 1e-10))
        adjusted[ordered] = values
    return adjusted


def normalise(raw: np.ndarray, race_groups: pd.Series) -> np.ndarray:
    raw = np.clip(np.asarray(raw, dtype=float), EPSILON, None)
    output = np.zeros_like(raw)
    identifiers = pd.Series(np.arange(len(raw)))
    for _, locations in identifiers.groupby(race_groups.to_numpy(), sort=False):
        idx = locations.to_numpy()
        output[idx] = raw[idx] / raw[idx].sum()
    return output


def ece(frame: pd.DataFrame, probability_column: str, bins: int = 5) -> float | None:
    data = frame[[TARGET_COLUMN, probability_column]].dropna().copy()
    if len(data) < 30 or data[probability_column].nunique() < 2:
        return None
    try:
        data["bin"] = pd.qcut(data[probability_column], q=min(bins, data[probability_column].nunique()), duplicates="drop")
    except ValueError:
        return None
    numerator = 0.0
    for _, group in data.groupby("bin", observed=True):
        numerator += len(group) * abs(float(group[TARGET_COLUMN].mean() - group[probability_column].mean()))
    return float(numerator / len(data))


def race_metrics(frame: pd.DataFrame, probability_column: str) -> dict[str, float]:
    brier_values: list[float] = []
    top_values: list[float] = []
    for _, group in frame.groupby("race_group", sort=False):
        probability = group[probability_column].to_numpy(dtype=float)
        target = group[TARGET_COLUMN].to_numpy(dtype=float)
        brier_values.append(float(np.square(target - probability).sum()))
        top_values.append(float(target[int(np.argmax(probability))]))
    return {"race_brier": float(np.mean(brier_values)), "top_pick_win_rate": float(np.mean(top_values)), "ece": ece(frame, probability_column)}


def score_full_frame(frame: pd.DataFrame) -> pd.DataFrame:
    bundle = load_model_bundle(MODEL_PATH, PREPROCESSOR_PATH)
    if int(bundle.metadata.get("input_dim", -1)) != 72:
        raise ValueError("Expected the active 72-dimensional N6 production model.")
    contract = bundle.metadata["feature_contract"]
    values = np.asarray(bundle.preprocessor.transform(frame[contract]), dtype=np.float32)
    with torch.no_grad():
        logits = bundle.model(torch.tensor(values, dtype=torch.float32)).cpu().numpy()
    output = frame[["race_date", "racecourse", "race_no", "race_group", "horse_name", "going", TARGET_COLUMN]].copy()
    output["parent_group"] = output["going"].map(parent_group)
    output["baseline_probability"] = score_to_race_probabilities(logits / float(bundle.metadata.get("temperature", 1.0)), output["race_group"])
    output["production_release"] = bundle.metadata.get("production_release")
    return output


def expanding_folds(scored: pd.DataFrame, going: str) -> list[Fold]:
    rare = scored[scored["going"] == going].sort_values(["race_date", "race_group"]).drop_duplicates("race_group")
    race_ids = rare["race_group"].tolist()
    if len(race_ids) <= MIN_LOCAL_RACES:
        return []
    future = race_ids[MIN_LOCAL_RACES:]
    chunks = [tuple(chunk.tolist()) for chunk in np.array_split(np.asarray(future, dtype=object), min(N_SPLITS, len(future))) if len(chunk)]
    folds: list[Fold] = []
    for index, chunk in enumerate(chunks, start=1):
        first_date = pd.Timestamp(rare.set_index("race_group").loc[chunk[0], "race_date"])
        folds.append(Fold(going=going, fold_id=index, calibration_end=first_date, test_races=chunk))
    return folds


def fit_isotonic(data: pd.DataFrame) -> IsotonicRegression:
    calibrator = IsotonicRegression(y_min=EPSILON, y_max=1.0 - EPSILON, out_of_bounds="clip")
    calibrator.fit(data["baseline_probability"], data[TARGET_COLUMN])
    return calibrator


def apply_fold(scored: pd.DataFrame, fold: Fold, tau: float) -> tuple[pd.DataFrame, dict[str, Any]]:
    test = scored[scored["race_group"].isin(fold.test_races)].copy()
    calibration = scored[scored["race_date"] < fold.calibration_end].copy()
    if test.empty or calibration.empty:
        raise ValueError("Empty temporal fold encountered.")
    parent = parent_group(fold.going)
    parent_data = calibration[calibration["parent_group"] == parent]
    local_data = calibration[calibration["going"] == fold.going]
    if parent_data[TARGET_COLUMN].nunique() < 2:
        parent_data = calibration
        parent_fallback = True
    else:
        parent_fallback = False
    parent_calibrator = fit_isotonic(parent_data)
    local_races = int(local_data["race_group"].nunique())
    local_usable = local_races >= MIN_LOCAL_RACES and local_data[TARGET_COLUMN].nunique() == 2 and local_data["baseline_probability"].nunique() >= 2
    parent_score = parent_calibrator.predict(test["baseline_probability"])
    if local_usable:
        local_calibrator = fit_isotonic(local_data)
        local_score = local_calibrator.predict(test["baseline_probability"])
        local_weight = local_races / (local_races + tau)
        raw = np.exp((1.0 - local_weight) * np.log(np.clip(parent_score, EPSILON, None)) + local_weight * np.log(np.clip(local_score, EPSILON, None)))
    else:
        local_calibrator = None
        local_weight = 0.0
        raw = parent_score
    protected = rank_protect(raw, test["baseline_probability"].to_numpy(dtype=float), test["race_group"])
    test["partial_pooling_rank_protected_probability"] = normalise(protected, test["race_group"])
    test["fold_id"] = fold.fold_id
    test["tau"] = tau
    metadata = {
        "going": fold.going,
        "fold_id": fold.fold_id,
        "calibration_end_exclusive": fold.calibration_end.isoformat(),
        "test_races": int(test["race_group"].nunique()),
        "test_rows": int(len(test)),
        "parent_group": parent,
        "parent_rows": int(len(parent_data)),
        "parent_races": int(parent_data["race_group"].nunique()),
        "parent_fallback_to_global": parent_fallback,
        "local_rows": int(len(local_data)),
        "local_races": local_races,
        "local_usable": local_usable,
        "local_weight": float(local_weight),
    }
    return test, metadata


def evaluate_tau(scored: pd.DataFrame, folds: Iterable[Fold], tau: float) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    predictions: list[pd.DataFrame] = []
    details: list[dict[str, Any]] = []
    for fold in folds:
        test, metadata = apply_fold(scored, fold, tau)
        base = race_metrics(test, "baseline_probability")
        candidate = race_metrics(test, "partial_pooling_rank_protected_probability")
        metadata.update({
            "baseline_race_brier": base["race_brier"],
            "candidate_race_brier": candidate["race_brier"],
            "brier_delta": candidate["race_brier"] - base["race_brier"],
            "baseline_top_pick_win_rate": base["top_pick_win_rate"],
            "candidate_top_pick_win_rate": candidate["top_pick_win_rate"],
            "baseline_ece": base["ece"],
            "candidate_ece": candidate["ece"],
        })
        predictions.append(test)
        details.append(metadata)
    return pd.concat(predictions, ignore_index=True), details


def report_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# N6：稀少 Going 的父群組部分池化與排名保護交叉驗證",
        "",
        "> 這是候選層的時間序列交叉驗證。每折只以早於該折的真實賽事擬合父群組與局部校準器；N6 生產模型沒有重訓或改動。由於基礎 72 維模型本身已在較早資料訓練，本實驗主要驗證校準層的時間順序與高變異控制，並非完整的端到端重新訓練驗證。",
        "",
        "## 最佳收縮強度與 OOF 結果",
        "",
        "| Going | 折數／場數 | 基準 Brier | 部分池化 Brier | 差異 | 基準首選 | 候選首選 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in summary["per_going"]:
        lines.append(f"| {row['going']} | {row['folds']}／{row['races']} | {row['baseline_race_brier']:.6f} | {row['candidate_race_brier']:.6f} | {row['brier_delta']:+.6f} | {row['baseline_top_pick_win_rate']:.2%} | {row['candidate_top_pick_win_rate']:.2%} |")
    lines.extend(["", "## 解讀", "", "候選層會先從草地濕軟父群組取得單調校準訊號，再依過去局部場數以 `N/(N+tau)` 對黏地或軟地校準器作收縮。最後的非遞增投影嚴格保留每場原始 N6 排名，故首選命中率應與基準一致；若不一致即視為實作錯誤。稀少場數下的 Brier 差異只作探索性證據，不可直接據此部署。", ""])
    return "\n".join(lines)


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    scored = score_full_frame(load_training_frame())
    all_details: list[dict[str, Any]] = []
    all_predictions: dict[float, pd.DataFrame] = {}
    selection: list[dict[str, Any]] = []
    for going in RARE_GOINGS:
        folds = expanding_folds(scored, going)
        if not folds:
            continue
        for tau in TAU_CANDIDATES:
            predictions, details = evaluate_tau(scored, folds, tau)
            all_predictions[(going, tau)] = predictions
            total_base = race_metrics(predictions, "baseline_probability")
            total_candidate = race_metrics(predictions, "partial_pooling_rank_protected_probability")
            selection.append({"going": going, "tau": tau, "folds": len(folds), "races": int(predictions["race_group"].nunique()), "baseline_race_brier": total_base["race_brier"], "candidate_race_brier": total_candidate["race_brier"], "brier_delta": total_candidate["race_brier"] - total_base["race_brier"], "baseline_top_pick_win_rate": total_base["top_pick_win_rate"], "candidate_top_pick_win_rate": total_candidate["top_pick_win_rate"], "baseline_ece": total_base["ece"], "candidate_ece": total_candidate["ece"]})
            all_details.extend(details)
    selection_df = pd.DataFrame(selection)
    if selection_df.empty:
        raise RuntimeError("No eligible expanding folds for rare going cross-validation.")
    best_rows = []
    oof_parts = []
    for going, group in selection_df.groupby("going", sort=True):
        best = group.sort_values(["candidate_race_brier", "tau"], ascending=[True, True]).iloc[0].to_dict()
        best_rows.append(best)
        oof_parts.append(all_predictions[(going, float(best["tau"]))])
    oof = pd.concat(oof_parts, ignore_index=True)
    per_going = []
    for best in best_rows:
        data = oof[oof["going"] == best["going"]]
        base = race_metrics(data, "baseline_probability")
        candidate = race_metrics(data, "partial_pooling_rank_protected_probability")
        if not np.isclose(base["top_pick_win_rate"], candidate["top_pick_win_rate"]):
            raise AssertionError("Rank protection failed: top-pick rate changed.")
        per_going.append({"going": best["going"], "tau": float(best["tau"]), "folds": int(best["folds"]), "races": int(data["race_group"].nunique()), "baseline_race_brier": base["race_brier"], "candidate_race_brier": candidate["race_brier"], "brier_delta": candidate["race_brier"] - base["race_brier"], "baseline_top_pick_win_rate": base["top_pick_win_rate"], "candidate_top_pick_win_rate": candidate["top_pick_win_rate"], "baseline_ece": base["ece"], "candidate_ece": candidate["ece"]})
    summary = {"engine": "N6 Neural Calculation Engine", "experiment_id": EXPERIMENT_ID, "generated_at_utc": datetime.now(UTC).isoformat(), "candidate_only_guarantee": "No production model, production calibration layer, API, service, or V10 data was modified.", "base_model": str(MODEL_PATH), "method": {"parent_groups": {"turf_dry": ["好地", "好地至快地"], "turf_wet": ["好地至黏地", "黏地", "軟地", "濕快地", "濕慢地"], "all_weather": ["封地"]}, "local_weight": "N_local_races/(N_local_races+tau)", "tau_candidates": list(TAU_CANDIDATES), "min_local_races": MIN_LOCAL_RACES, "rank_protection": "non-increasing projection in baseline N6 race order"}, "selection_grid": selection, "per_going": per_going, "fold_details": all_details, "artifacts": {"summary": str(SUMMARY_PATH), "fold_metrics": str(FOLD_METRICS_PATH), "oof_predictions": str(OOF_PATH), "artifact": str(ARTIFACT_PATH)}}
    SUMMARY_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(all_details).to_csv(FOLD_METRICS_PATH, index=False, encoding="utf-8-sig")
    oof.to_csv(OOF_PATH, index=False, encoding="utf-8-sig")
    joblib.dump({"experiment_id": EXPERIMENT_ID, "method": summary["method"], "selection_grid": selection, "per_going": per_going}, ARTIFACT_PATH)
    REPORT_PATH.write_text(report_markdown(summary), encoding="utf-8")
    print(json.dumps({"per_going": per_going, "selection_grid": selection, "artifact": str(ARTIFACT_PATH)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
