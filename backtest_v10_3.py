#!/usr/bin/env python3
"""Leakage-safe V10.3 Bayesian overlay expanding-window backtest.

The script never calls V10.2 feature engineering, model training, predict.py, EV,
or Kelly code.  It reads a saved V10.2 pre-race CSV, uses ``target_win`` only for
an offline fit/evaluation, and stores a separate V10.3 research report.  Even if
all gates pass, its decision is ``REVIEW_REQUIRED``; it cannot replace V10.2.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any

import numpy as np

from bayesian_calibration import (
    BayesianCalibrationError,
    FrozenRace,
    fit_model,
    hkt_timestamp,
    load_frozen_csv,
    load_model,
    posterior_draw_vectors,
    sha256_file,
)

EPSILON = 1e-12


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="V10.3 Bayesian overlay 3-fold expanding-window backtest (research only)")
    parser.add_argument("--predictions", default="v102_multiseason_backtest_predictions.csv", help="Frozen V10.2 pre-race CSV with target_win for offline evaluation.")
    parser.add_argument("--initial-train-races", type=int, default=100)
    parser.add_argument("--validation-races", type=int, default=25, help="Forward validation segment; reported only, never used to refit test model.")
    parser.add_argument("--test-races", type=int, default=50)
    parser.add_argument("--max-folds", type=int, default=3)
    parser.add_argument("--svi-steps", type=int, default=10_000)
    parser.add_argument("--posterior-draws", type=int, default=400)
    parser.add_argument("--seed", type=int, default=10301)
    parser.add_argument("--output-dir", default="archive/v103_bayesian_backtest")
    return parser.parse_args()


def partition_complete_dates(
    date_groups: list[tuple[str, list[FrozenRace]]], start: int, minimum_races: int,
) -> tuple[int, list[FrozenRace]] | None:
    selected: list[FrozenRace] = []
    index = start
    while index < len(date_groups) and len(selected) < minimum_races:
        selected.extend(date_groups[index][1])
        index += 1
    return (index, selected) if len(selected) >= minimum_races else None


def make_folds(
    races: list[FrozenRace], initial_train: int, validation: int, test: int, max_folds: int,
) -> list[dict[str, list[FrozenRace]]]:
    if min(initial_train, validation, test, max_folds) < 1:
        raise BayesianCalibrationError("所有 expanding-window 大小必須為正整數。")
    by_date: dict[str, list[FrozenRace]] = defaultdict(list)
    for race in races:
        if not race.race_date:
            raise BayesianCalibrationError("歷史 CSV 缺少 race_date；拒絕不安全時間切分。")
        by_date[race.race_date].append(race)
    date_groups = [(date, by_date[date]) for date in sorted(by_date)]
    initial = partition_complete_dates(date_groups, 0, initial_train)
    if initial is None:
        return []
    train_end, _ = initial
    folds: list[dict[str, list[FrozenRace]]] = []
    while len(folds) < max_folds:
        validation_partition = partition_complete_dates(date_groups, train_end, validation)
        if validation_partition is None:
            break
        validation_end, validation_races = validation_partition
        test_partition = partition_complete_dates(date_groups, validation_end, test)
        if test_partition is None:
            break
        test_end, test_races = test_partition
        train_races = [race for _, group in date_groups[:train_end] for race in group]
        sets = [{race.race_date for race in segment} for segment in (train_races, validation_races, test_races)]
        if sets[0] & sets[1] or sets[0] & sets[2] or sets[1] & sets[2]:
            raise BayesianCalibrationError("同一賽日跨 train／validation／test；拒絕資料洩漏。")
        folds.append({"train": train_races, "validation": validation_races, "test": test_races})
        # Only the next fold can absorb the prior test as historical data.
        train_end = test_end
    return folds


def field_brier(probabilities: np.ndarray, winner_index: int) -> float:
    labels = np.zeros_like(probabilities)
    labels[winner_index] = 1.0
    return float(np.sum((probabilities - labels) ** 2))


def field_log_score(probabilities: np.ndarray, winner_index: int) -> float:
    return float(-math.log(max(float(probabilities[winner_index]), EPSILON)))


def normalized_entropy(probabilities: np.ndarray) -> float:
    if probabilities.size < 2:
        return 0.0
    return float(-np.sum(probabilities * np.log(np.clip(probabilities, EPSILON, 1.0))) / math.log(probabilities.size))


def winner_rank(probabilities: np.ndarray, race: FrozenRace) -> int:
    ordered = sorted(range(len(probabilities)), key=lambda index: (-float(probabilities[index]), race.horse_names[index]))
    return ordered.index(int(race.winner_index or 0)) + 1


def score_segment(model: dict[str, Any], races: list[FrozenRace], posterior_draws: int, seed: int, fold_id: int, partition: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for index, race in enumerate(races):
        if race.winner_index is None:
            raise BayesianCalibrationError("回測評分段缺少官方頭馬標籤。")
        vectors, course_status = posterior_draw_vectors(model, race, posterior_draws, seed + fold_id * 1_000_000 + index)
        overlay = np.mean(vectors, axis=0)
        baseline_top = int(np.argmax(race.baseline))
        overlay_top = int(np.argmax(overlay))
        entropies = np.asarray([normalized_entropy(vector) for vector in vectors], dtype=float)
        conservation_error = float(np.max(np.abs(np.sum(vectors, axis=1) - 1.0)))
        results.append({
            "fold_id": fold_id,
            "partition": partition,
            "race_key": race.race_key,
            "race_date": race.race_date,
            "racecourse": race.racecourse,
            "field_size": int(len(race.baseline)),
            "control_brier": field_brier(race.baseline, race.winner_index),
            "overlay_brier": field_brier(overlay, race.winner_index),
            "control_log_score": field_log_score(race.baseline, race.winner_index),
            "overlay_log_score": field_log_score(overlay, race.winner_index),
            "control_top1_won": int(baseline_top == race.winner_index),
            "overlay_top1_won": int(overlay_top == race.winner_index),
            "control_winner_rank": winner_rank(race.baseline, race),
            "overlay_winner_rank": winner_rank(overlay, race),
            "top1_rank_stability": float(np.mean(np.argmax(vectors, axis=1) == baseline_top)),
            "posterior_entropy_mean": float(np.mean(entropies)),
            "posterior_entropy_p05": float(np.quantile(entropies, 0.05)),
            "posterior_entropy_p95": float(np.quantile(entropies, 0.95)),
            "probability_sum_max_abs_error": conservation_error,
            "course_partial_pooling_status": course_status,
        })
    return results


def safe_mean(values: list[float]) -> float | None:
    return float(mean(values)) if values else None


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"races": 0}
    control_brier = [float(row["control_brier"]) for row in rows]
    overlay_brier = [float(row["overlay_brier"]) for row in rows]
    control_log = [float(row["control_log_score"]) for row in rows]
    overlay_log = [float(row["overlay_log_score"]) for row in rows]
    return {
        "races": len(rows),
        "control_mean_brier": safe_mean(control_brier),
        "overlay_mean_brier": safe_mean(overlay_brier),
        "overlay_brier_delta_vs_control": safe_mean([overlay - control for control, overlay in zip(control_brier, overlay_brier)]),
        "control_mean_log_score": safe_mean(control_log),
        "overlay_mean_log_score": safe_mean(overlay_log),
        "overlay_log_score_delta_vs_control": safe_mean([overlay - control for control, overlay in zip(control_log, overlay_log)]),
        "control_top1_win_rate": safe_mean([float(row["control_top1_won"]) for row in rows]),
        "overlay_top1_win_rate": safe_mean([float(row["overlay_top1_won"]) for row in rows]),
        "mean_top1_rank_stability": safe_mean([float(row["top1_rank_stability"]) for row in rows]),
        "mean_posterior_entropy": safe_mean([float(row["posterior_entropy_mean"]) for row in rows]),
        "max_probability_sum_error": max(float(row["probability_sum_max_abs_error"]) for row in rows),
    }


def gate(test_rows: list[dict[str, Any]], folds: list[dict[str, Any]]) -> dict[str, Any]:
    overall = metrics(test_rows)
    observed_folds = len(folds)
    observed_test = int(overall.get("races", 0))
    brier_improved = sum(
        1 for fold in folds
        if fold["test_metrics"].get("overlay_brier_delta_vs_control") is not None
        and float(fold["test_metrics"]["overlay_brier_delta_vs_control"]) <= -0.005
    )
    log_not_worse = sum(
        1 for fold in folds
        if fold["test_metrics"].get("overlay_log_score_delta_vs_control") is not None
        and float(fold["test_metrics"]["overlay_log_score_delta_vs_control"]) <= 0.0
    )
    required_fold_successes = 2
    complete = observed_folds >= 3 and observed_test >= 150
    probability_sum_ok = bool(float(overall.get("max_probability_sum_error", math.inf)) <= 1e-6)
    passed = bool(
        complete
        and float(overall.get("overlay_brier_delta_vs_control", math.inf)) <= -0.005
        and brier_improved >= required_fold_successes
        and log_not_worse >= required_fold_successes
        and probability_sum_ok
    )
    reasons: list[str] = []
    if not complete:
        reasons.append("未達至少 3 個完整 fold 及合計 150 場未見 test 的門檻。")
    if overall.get("overlay_brier_delta_vs_control") is None or float(overall["overlay_brier_delta_vs_control"]) > -0.005:
        reasons.append("整體場內 Brier 未改善至少 0.005。")
    if brier_improved < required_fold_successes:
        reasons.append("少於 2/3 fold 達到 Brier 改善至少 0.005。")
    if log_not_worse < required_fold_successes:
        reasons.append("少於 2/3 fold 的場內 log score 不惡化。")
    if not probability_sum_ok:
        reasons.append("至少一場 posterior draw 未通過場內機率守恆。")
    if passed:
        reasons.append("研究採納統計門檻通過；仍只可進入人工版本審查，嚴禁自動取代 V10.2。")
    return {
        "status": "evaluated_research_only" if complete else "insufficient_data",
        "required_folds": 3,
        "observed_folds": observed_folds,
        "required_test_races": 150,
        "observed_test_races": observed_test,
        "overall_brier_delta_vs_control": overall.get("overlay_brier_delta_vs_control"),
        "brier_improved_folds": brier_improved,
        "log_score_not_worse_folds": log_not_worse,
        "probability_sum_ok": probability_sum_ok,
        "statistical_gate_passed": passed,
        "adoption_decision": "REVIEW_REQUIRED" if passed else "NOT_ELIGIBLE",
        "formal_probability_replacement": False,
        "reasons": reasons,
    }


def build_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# V10.3 Bayesian Calibration Expanding-Window Backtest",
        "",
        "> 此回測只評估並列風險披露覆蓋層。它不重訓或改寫 V10.2 LightGBM／CatBoost、正式勝率、EV 或 Kelly。",
        "",
        "| 項目 | 值 |",
        "|---|---|",
        f"| 輸入 SHA-256 | `{report['input']['artifact_sha256']}` |",
        f"| 可用完整賽事 | {report['coverage']['usable_races']} |",
        f"| 產生 folds | {len(report['folds'])} |",
        f"| 未見 test 場次 | {report['test_metrics'].get('races', 0)} |",
        "",
        "## Fold 結果",
        "",
        "| Fold | Train／Validation／Test 場次 | Test Δ Brier | Test Δ log score | 場內守恆 |",
        "|---:|---:|---:|---:|---|",
    ]
    for fold in report["folds"]:
        metric = fold["test_metrics"]
        lines.append(
            f"| {fold['fold_id']} | {fold['train_races']}／{fold['validation_races']}／{fold['test_races']} "
            f"| {metric['overlay_brier_delta_vs_control']:.6f} | {metric['overlay_log_score_delta_vs_control']:.6f} "
            f"| {'PASS' if metric['max_probability_sum_error'] <= 1e-6 else 'FAIL'} |"
        )
    gate_result = report["pre_registered_gate"]
    lines.extend([
        "",
        "## 預先登記採納閘門",
        "",
        f"- 狀態：`{gate_result['status']}`；版本決策：`{gate_result['adoption_decision']}`。",
        f"- 觀察 folds／最低要求：{gate_result['observed_folds']}／{gate_result['required_folds']}；未見 test 場次：{gate_result['observed_test_races']}／{gate_result['required_test_races']}。",
        f"- 整體 Δ Brier：{gate_result['overall_brier_delta_vs_control'] if gate_result['overall_brier_delta_vs_control'] is not None else 'N/A'}；Brier 改善 folds：{gate_result['brier_improved_folds']}；log score 不惡化 folds：{gate_result['log_score_not_worse_folds']}。",
        "",
        "### 結論限制",
        "",
        "即使本回測的統計閘門通過，也只可進行人工 V10.3 版本審查。正式 `predicted_win_probability`、排名、EV 與 Kelly 不會由此程式更新或替換。",
        "",
    ])
    lines.extend(f"- {reason}" for reason in gate_result["reasons"])
    return "\n".join(lines) + "\n"


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0]) if rows else ["fold_id", "partition"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    try:
        source_path = Path(args.predictions)
        races, exclusions = load_frozen_csv(source_path, require_target=True)
        folds = make_folds(races, args.initial_train_races, args.validation_races, args.test_races, args.max_folds)
        if not folds:
            raise BayesianCalibrationError("資料不足以建立任何完整 expanding-window fold。")
        output_dir = Path(args.output_dir)
        model_dir = output_dir / "fold_models"
        fold_reports: list[dict[str, Any]] = []
        all_test_rows: list[dict[str, Any]] = []
        for fold_id, fold in enumerate(folds, start=1):
            model_path = model_dir / f"fold_{fold_id}.npz"
            metadata = fit_model(
                fold["train"], model_path, sha256_file(source_path), args.svi_steps, args.posterior_draws, args.seed + fold_id,
            )
            model = load_model(model_path)
            validation_rows = score_segment(model, fold["validation"], args.posterior_draws, args.seed, fold_id, "validation")
            test_rows = score_segment(model, fold["test"], args.posterior_draws, args.seed, fold_id, "test")
            all_test_rows.extend(test_rows)
            fold_reports.append({
                "fold_id": fold_id,
                "train_races": len(fold["train"]),
                "validation_races": len(fold["validation"]),
                "test_races": len(fold["test"]),
                "train_date_range": [fold["train"][0].race_date, fold["train"][-1].race_date],
                "validation_date_range": [fold["validation"][0].race_date, fold["validation"][-1].race_date],
                "test_date_range": [fold["test"][0].race_date, fold["test"][-1].race_date],
                "model_sha256": metadata["model_sha256"],
                "validation_metrics": metrics(validation_rows),
                "test_metrics": metrics(test_rows),
            })
        report = {
            "schema_version": "v10_3_bayesian_expanding_window_backtest_v1",
            "generated_at_hkt": hkt_timestamp(),
            "status": "ok",
            "formal_probability_replacement": False,
            "input": {"predictions": str(source_path), "artifact_sha256": sha256_file(source_path), "frozen_v102_artifact_only": True},
            "configuration": {"initial_train_races": args.initial_train_races, "validation_races": args.validation_races, "test_races": args.test_races, "max_folds": args.max_folds, "svi_steps": args.svi_steps, "posterior_draws": args.posterior_draws, "seed": args.seed, "temporal_split_unit": "race_date_complete_non_overlapping"},
            "coverage": {"usable_races": len(races), "excluded_races": sum(exclusions.values()), "exclusions": dict(exclusions)},
            "folds": fold_reports,
            "test_metrics": metrics(all_test_rows),
            "pre_registered_gate": gate(all_test_rows, fold_reports),
            "test_race_metrics": all_test_rows,
            "notice": "所有 fold 僅以其 train 日期作 NumPyro SVI fit；validation／test 均不參與 fit。V10.3 只產生研究性 posterior 指標。",
        }
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (output_dir / "report.md").write_text(build_markdown(report), encoding="utf-8")
        write_csv(output_dir / "test_race_metrics.csv", all_test_rows)
    except (BayesianCalibrationError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "input_error", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"status": "ok", "folds": len(report["folds"]), "test_races": report["test_metrics"]["races"], "gate": report["pre_registered_gate"], "output_dir": str(output_dir)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
