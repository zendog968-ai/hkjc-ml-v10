#!/usr/bin/env python3
"""Leakage-safe expanding-window evaluation for a V10.3 Bayesian temperature overlay.

The input must be an already-saved, field-normalized V10.2 pre-race prediction
artifact with post-race ``target_win`` labels only for evaluation.  This script
never rebuilds features, retrains LightGBM/CatBoost, or changes V10.2 production
probabilities.  The experimental overlay applies a Bayesian posterior over one
positive temperature parameter to the frozen field probabilities:

    q_i(T) = softmax(T * log(p_i))

where log(T) has a zero-centred Normal prior.  Candidate prior scales are chosen
only on each fold's validation segment.  Test segments remain untouched until
all candidate settings are locked.

A common positive temperature is strictly rank-preserving.  Accordingly this
scaffold can assess calibration and posterior probability width, but it cannot
claim a change in Top-1/Top-3 ordering, winner rank 7+ rate, or meaningful
posterior rank stability.  Those require a later non-monotonic hierarchical
feature overlay and must be evaluated as a separate V10.3 experiment.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from statistics import mean
from typing import Any, Iterable

from backtest_recent_v102_predictions import (
    BacktestInputError,
    RaceMeta,
    evaluate_group,
    load_prediction_rows,
    parse_probability,
    parse_target_win,
    race_sort_key,
)

DEFAULT_PREDICTIONS = "v102_multiseason_backtest_predictions.csv"
DEFAULT_JSON = "archive/v103_bayesian_validation/walk_forward_report.json"
DEFAULT_MD = "archive/v103_bayesian_validation/walk_forward_report.md"
DEFAULT_RACE_CSV = "archive/v103_bayesian_validation/walk_forward_race_metrics.csv"
DEFAULT_PRIOR_SDS = (0.15, 0.30, 0.50)
EPSILON = 1e-12


@dataclass(frozen=True)
class FrozenRace:
    meta: RaceMeta
    horse_names: tuple[str, ...]
    probabilities: tuple[float, ...]
    labels: tuple[int, ...]


@dataclass(frozen=True)
class TemperaturePosterior:
    log_temperature_map: float
    log_temperature_posterior_sd: float
    temperature_map: float
    prior_sd: float
    train_races: int
    curvature: float
    status: str


class WalkForwardError(ValueError):
    """Raised for invalid user inputs or unsafe prediction artifacts."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Leakage-safe expanding-window evaluator for the V10.3 Bayesian temperature overlay."
    )
    parser.add_argument("--predictions", default=DEFAULT_PREDICTIONS, help="Saved V10.2 prediction CSV artifact.")
    parser.add_argument(
        "--probability-column",
        default="race_normalized_probability",
        help="Frozen field-normalized V10.2 probability column.",
    )
    parser.add_argument("--min-train-races", type=int, default=200, help="Initial expanding training race count.")
    parser.add_argument("--validation-races", type=int, default=50, help="Contiguous validation race count per fold.")
    parser.add_argument("--test-races", type=int, default=50, help="Contiguous unseen test race count per fold.")
    parser.add_argument("--max-folds", type=int, default=3, help="Maximum expanding folds to produce.")
    parser.add_argument(
        "--prior-sds",
        default=",".join(str(item) for item in DEFAULT_PRIOR_SDS),
        help="Comma-separated Normal prior SD candidates for log temperature; selected on validation only.",
    )
    parser.add_argument("--posterior-draws", type=int, default=400, help="Fixed posterior draws per test race.")
    parser.add_argument("--seed", type=int, default=10301, help="Fixed random seed for posterior draw provenance.")
    parser.add_argument("--probability-tolerance", type=float, default=1e-6, help="Allowed field probability-sum deviation.")
    parser.add_argument("--output-json", default=DEFAULT_JSON, help="Machine-readable validation report.")
    parser.add_argument("--output-md", default=DEFAULT_MD, help="Human-readable validation report.")
    parser.add_argument("--output-race-csv", default=DEFAULT_RACE_CSV, help="Per-test-race metrics CSV.")
    return parser.parse_args()


def parse_prior_sds(raw: str) -> tuple[float, ...]:
    values: list[float] = []
    for part in raw.split(","):
        try:
            value = float(part.strip())
        except ValueError as exc:
            raise WalkForwardError(f"無法解析 prior SD：{part!r}") from exc
        if not math.isfinite(value) or value <= 0.0:
            raise WalkForwardError("所有 prior SD 必須為有限正數。")
        values.append(value)
    if not values:
        raise WalkForwardError("至少需要一個 prior SD 候選。")
    return tuple(sorted(set(values)))


def fixed_hkt_timestamp() -> str:
    hkt = timezone(timedelta(hours=8))
    return datetime.now(timezone.utc).astimezone(hkt).replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S HKT")


def artifact_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_softmax_from_probabilities(probabilities: Iterable[float], temperature: float) -> list[float]:
    if not math.isfinite(temperature) or temperature <= 0.0:
        raise WalkForwardError("temperature 必須為有限正數。")
    probs = list(probabilities)
    if not probs or any((not math.isfinite(item) or item <= 0.0) for item in probs):
        raise WalkForwardError("overlay 只接受嚴格正且有限的場內機率。")
    logits = [temperature * math.log(max(item, EPSILON)) for item in probs]
    maximum = max(logits)
    weights = [math.exp(item - maximum) for item in logits]
    total = sum(weights)
    return [item / total for item in weights]


def race_negative_log_posterior(log_temperature: float, races: Iterable[FrozenRace], prior_sd: float) -> float:
    if not math.isfinite(log_temperature):
        return math.inf
    temperature = math.exp(log_temperature)
    total = 0.5 * (log_temperature / prior_sd) ** 2
    for race in races:
        calibrated = stable_softmax_from_probabilities(race.probabilities, temperature)
        winner_index = race.labels.index(1)
        total -= math.log(max(calibrated[winner_index], EPSILON))
    return total


def fit_temperature_posterior(races: list[FrozenRace], prior_sd: float) -> TemperaturePosterior:
    if not races:
        return TemperaturePosterior(0.0, 0.0, 1.0, prior_sd, 0, 0.0, "unavailable_no_training_races")
    # A deterministic one-dimensional MAP grid avoids an untracked SciPy/PyMC dependency
    # in this evaluation scaffold.  The posterior is then locally approximated by Laplace.
    grid = [(-1.5 + index * 3.0 / 600.0) for index in range(601)]
    losses = [race_negative_log_posterior(item, races, prior_sd) for item in grid]
    index = min(range(len(grid)), key=lambda item: losses[item])
    map_log_temperature = grid[index]
    step = 1e-3
    centre = race_negative_log_posterior(map_log_temperature, races, prior_sd)
    left = race_negative_log_posterior(map_log_temperature - step, races, prior_sd)
    right = race_negative_log_posterior(map_log_temperature + step, races, prior_sd)
    curvature = max((left - 2.0 * centre + right) / (step**2), EPSILON)
    posterior_sd = math.sqrt(1.0 / curvature)
    return TemperaturePosterior(
        log_temperature_map=map_log_temperature,
        log_temperature_posterior_sd=posterior_sd,
        temperature_map=math.exp(map_log_temperature),
        prior_sd=prior_sd,
        train_races=len(races),
        curvature=curvature,
        status="available_laplace_approximation",
    )


def brier_score(probabilities: Iterable[float], labels: Iterable[int]) -> float:
    return sum((probability - float(label)) ** 2 for probability, label in zip(probabilities, labels))


def log_score(probabilities: Iterable[float], labels: Iterable[int]) -> float:
    winner_index = list(labels).index(1)
    return -math.log(max(list(probabilities)[winner_index], EPSILON))


def winner_rank(probabilities: Iterable[float], labels: Iterable[int], horse_names: Iterable[str]) -> int:
    triples = sorted(zip(horse_names, probabilities, labels), key=lambda item: (-item[1], item[0]))
    return next(index for index, (_, _, label) in enumerate(triples, start=1) if label == 1)


def normalized_entropy(probabilities: Iterable[float]) -> float:
    values = list(probabilities)
    if len(values) < 2:
        return 0.0
    entropy = -sum(item * math.log(max(item, EPSILON)) for item in values)
    return entropy / math.log(len(values))


def draw_summary(
    race: FrozenRace,
    posterior: TemperaturePosterior,
    draws: int,
    rng: random.Random,
) -> dict[str, Any]:
    if draws < 10:
        raise WalkForwardError("--posterior-draws 必須不少於 10。")
    vectors: list[list[float]] = []
    baseline_top_index = max(range(len(race.probabilities)), key=lambda item: (race.probabilities[item], race.horse_names[item]))
    stable_count = 0
    entropies: list[float] = []
    for _ in range(draws):
        log_temperature = rng.gauss(posterior.log_temperature_map, posterior.log_temperature_posterior_sd)
        vector = stable_softmax_from_probabilities(race.probabilities, math.exp(log_temperature))
        if abs(sum(vector) - 1.0) > 1e-10:
            raise WalkForwardError("posterior draw 未能保持場內機率守恆。")
        vectors.append(vector)
        draw_top_index = max(range(len(vector)), key=lambda item: (vector[item], race.horse_names[item]))
        stable_count += int(draw_top_index == baseline_top_index)
        entropies.append(normalized_entropy(vector))
    by_horse = list(zip(*vectors))
    means = [mean(items) for items in by_horse]
    p05 = [sorted(items)[max(0, int(math.floor(0.05 * (len(items) - 1))))] for items in by_horse]
    p95 = [sorted(items)[min(len(items) - 1, int(math.ceil(0.95 * (len(items) - 1))))] for items in by_horse]
    return {
        "posterior_win_mean": means,
        "posterior_win_p05": p05,
        "posterior_win_p95": p95,
        "top1_rank_stability": stable_count / draws,
        "rank_stability_semantics": "not_informative_rank_preserving_temperature",
        "posterior_entropy_mean": mean(entropies),
        "draws": draws,
        "probability_sum_max_abs_error": max(abs(sum(vector) - 1.0) for vector in vectors),
    }


def load_frozen_races(path: Path, probability_column: str, tolerance: float) -> tuple[list[FrozenRace], Counter[str]]:
    rows, metas = load_prediction_rows(path, probability_column)
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[row["_group_id"]].append(row)
    frozen: list[FrozenRace] = []
    exclusions: Counter[str] = Counter()
    for meta in sorted(metas, key=race_sort_key):
        result, _ = evaluate_group(meta, grouped[meta.group_id], probability_column, tolerance)
        if result.status != "evaluated":
            exclusions[result.reason or "unknown"] += 1
            continue
        ordered_rows = grouped[meta.group_id]
        probabilities = tuple(parse_probability(row.get(probability_column)) for row in ordered_rows)
        labels = tuple(parse_target_win(row.get("target_win")) for row in ordered_rows)
        if any(item is None for item in probabilities) or any(item is None for item in labels):
            exclusions["unexpected_parse_failure"] += 1
            continue
        if any(item <= 0.0 for item in probabilities):
            exclusions["zero_probability_not_supported_by_log_overlay"] += 1
            continue
        frozen.append(
            FrozenRace(
                meta=meta,
                horse_names=tuple(row["horse_name"] for row in ordered_rows),
                probabilities=tuple(float(item) for item in probabilities),
                labels=tuple(int(item) for item in labels),
            )
        )
    if not frozen:
        raise WalkForwardError("沒有可作 V10.3 overlay 評估的完整、嚴格正機率賽事。")
    return frozen, exclusions


def _partition_full_dates(date_groups: list[tuple[Any, list[FrozenRace]]], start: int, minimum_races: int) -> tuple[int, list[FrozenRace]] | None:
    """Return a contiguous partition containing complete race dates only."""
    selected: list[FrozenRace] = []
    index = start
    while index < len(date_groups) and len(selected) < minimum_races:
        selected.extend(date_groups[index][1])
        index += 1
    return (index, selected) if len(selected) >= minimum_races else None


def make_folds(races: list[FrozenRace], min_train: int, validation: int, test: int, max_folds: int) -> list[dict[str, list[FrozenRace]]]:
    if min_train < 1 or validation < 1 or test < 1 or max_folds < 1:
        raise WalkForwardError("所有 fold 大小與 --max-folds 必須為正整數。")
    by_date: dict[Any, list[FrozenRace]] = defaultdict(list)
    for race in races:
        by_date[race.meta.race_date].append(race)
    date_groups = [(race_date, by_date[race_date]) for race_date in sorted(by_date)]
    initial_train = _partition_full_dates(date_groups, 0, min_train)
    if initial_train is None:
        return []
    train_end, _ = initial_train
    folds: list[dict[str, list[FrozenRace]]] = []
    while len(folds) < max_folds:
        validation_partition = _partition_full_dates(date_groups, train_end, validation)
        if validation_partition is None:
            break
        validation_end, validation_races = validation_partition
        test_partition = _partition_full_dates(date_groups, validation_end, test)
        if test_partition is None:
            break
        test_end, test_races = test_partition
        train_races = [race for _, group in date_groups[:train_end] for race in group]
        # Race dates are the only safe temporal unit in this artifact: exact scheduled
        # start times are not present, so a date may never be split between partitions.
        train_dates = {race.meta.race_date for race in train_races}
        validation_dates = {race.meta.race_date for race in validation_races}
        test_dates = {race.meta.race_date for race in test_races}
        if train_dates & validation_dates or train_dates & test_dates or validation_dates & test_dates:
            raise WalkForwardError("fold 賽日交疊；拒絕在沒有精確開跑時間的工件中進行不安全切分。")
        folds.append({"train": train_races, "validation": validation_races, "test": test_races})
        # Subsequent folds may absorb prior validation/test dates as past information,
        # but never use a future date before its own test evaluation completes.
        train_end = test_end
    return folds


def score_partition(races: list[FrozenRace], posterior: TemperaturePosterior, draws: int, rng: random.Random, fold_id: int, partition: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for race in races:
        posterior_summary = draw_summary(race, posterior, draws, rng)
        overlay = posterior_summary["posterior_win_mean"]
        control_top_index = max(range(len(race.probabilities)), key=lambda item: (race.probabilities[item], race.horse_names[item]))
        overlay_top_index = max(range(len(overlay)), key=lambda item: (overlay[item], race.horse_names[item]))
        winner_index = race.labels.index(1)
        control_rank = winner_rank(race.probabilities, race.labels, race.horse_names)
        overlay_rank = winner_rank(overlay, race.labels, race.horse_names)
        results.append(
            {
                "fold_id": fold_id,
                "partition": partition,
                "race_group": race.meta.group_id,
                "race_date": race.meta.race_date.isoformat(),
                "racecourse": race.meta.racecourse,
                "race_no": race.meta.race_no,
                "field_size": len(race.probabilities),
                "control_brier": brier_score(race.probabilities, race.labels),
                "overlay_brier": brier_score(overlay, race.labels),
                "control_log_score": log_score(race.probabilities, race.labels),
                "overlay_log_score": log_score(overlay, race.labels),
                "control_top1_won": int(control_top_index == winner_index),
                "overlay_top1_won": int(overlay_top_index == winner_index),
                "control_top3_contains_winner": int(control_rank <= 3),
                "overlay_top3_contains_winner": int(overlay_rank <= 3),
                "control_winner_rank": control_rank,
                "overlay_winner_rank": overlay_rank,
                "control_winner_rank_7_plus": int(control_rank >= 7),
                "overlay_winner_rank_7_plus": int(overlay_rank >= 7),
                "top1_rank_stability": posterior_summary["top1_rank_stability"],
                "rank_stability_semantics": posterior_summary["rank_stability_semantics"],
                "posterior_entropy_mean": posterior_summary["posterior_entropy_mean"],
                "posterior_draws": posterior_summary["draws"],
                "posterior_probability_sum_max_abs_error": posterior_summary["probability_sum_max_abs_error"],
                "posterior_winner_mean": overlay[winner_index],
                "posterior_winner_p05": posterior_summary["posterior_win_p05"][winner_index],
                "posterior_winner_p95": posterior_summary["posterior_win_p95"][winner_index],
            }
        )
    return results


def safe_mean(values: Iterable[float]) -> float | None:
    values = list(values)
    return mean(values) if values else None


def partition_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"races": 0}
    low_stability = [item for item in rows if item["top1_rank_stability"] < 0.80]
    tail = [item for item in rows if item["control_winner_rank_7_plus"] == 1]
    return {
        "races": len(rows),
        "control_mean_brier": safe_mean(item["control_brier"] for item in rows),
        "overlay_mean_brier": safe_mean(item["overlay_brier"] for item in rows),
        "overlay_brier_delta_vs_control": safe_mean(item["overlay_brier"] - item["control_brier"] for item in rows),
        "control_mean_log_score": safe_mean(item["control_log_score"] for item in rows),
        "overlay_mean_log_score": safe_mean(item["overlay_log_score"] for item in rows),
        "overlay_log_score_delta_vs_control": safe_mean(item["overlay_log_score"] - item["control_log_score"] for item in rows),
        "control_top1_win_rate": safe_mean(item["control_top1_won"] for item in rows),
        "overlay_top1_win_rate": safe_mean(item["overlay_top1_won"] for item in rows),
        "control_top3_rate": safe_mean(item["control_top3_contains_winner"] for item in rows),
        "overlay_top3_rate": safe_mean(item["overlay_top3_contains_winner"] for item in rows),
        "control_rank_7_plus_rate": safe_mean(item["control_winner_rank_7_plus"] for item in rows),
        "overlay_rank_7_plus_rate": safe_mean(item["overlay_winner_rank_7_plus"] for item in rows),
        "tail_races": len(tail),
        "low_stability_races": len(low_stability),
        "low_stability_mean_brier": safe_mean(item["control_brier"] for item in low_stability),
        "high_stability_mean_brier": safe_mean(item["control_brier"] for item in rows if item["top1_rank_stability"] >= 0.80),
        "max_posterior_probability_sum_error": max(item["posterior_probability_sum_max_abs_error"] for item in rows),
    }


def candidate_validation_score(rows: list[dict[str, Any]]) -> tuple[float, float]:
    """Validation-only deterministic selection: lower overlay Brier, then log score."""
    metrics = partition_metrics(rows)
    return (
        metrics["overlay_mean_brier"] if metrics["overlay_mean_brier"] is not None else math.inf,
        metrics["overlay_mean_log_score"] if metrics["overlay_mean_log_score"] is not None else math.inf,
    )


def pre_registered_gate(test_rows: list[dict[str, Any]], fold_summaries: list[dict[str, Any]]) -> dict[str, Any]:
    overall = partition_metrics(test_rows)
    total_test = overall["races"]
    complete_fold_count = len(fold_summaries)
    brier_improved_folds = sum(1 for item in fold_summaries if item["test_metrics"]["overlay_brier_delta_vs_control"] is not None and item["test_metrics"]["overlay_brier_delta_vs_control"] <= -0.005)
    log_not_worse_folds = sum(1 for item in fold_summaries if item["test_metrics"]["overlay_log_score_delta_vs_control"] is not None and item["test_metrics"]["overlay_log_score_delta_vs_control"] <= 0.0)
    tail_n = overall.get("tail_races", 0)
    # A scalar positive temperature preserves all within-race orderings by definition.
    # Tail-rank change is intentionally N/A, not a zero improvement claim.
    tail_relative_reduction = None
    gate = {
        "status": "insufficient_data" if complete_fold_count < 3 or total_test < 150 else "evaluated_not_adopted",
        "required_folds": 3,
        "observed_folds": complete_fold_count,
        "required_test_races": 150,
        "observed_test_races": total_test,
        "overall_brier_delta_vs_control": overall.get("overlay_brier_delta_vs_control"),
        "brier_improved_folds": brier_improved_folds,
        "log_score_not_worse_folds": log_not_worse_folds,
        "tail_rank7_races": tail_n,
        "tail_rank7_relative_reduction": tail_relative_reduction,
        "rank_change_evaluable": False,
        "probability_sum_ok": bool(overall.get("max_posterior_probability_sum_error", math.inf) <= 1e-6),
        "adoption_decision": "NOT_ELIGIBLE",
        "reasons": [],
    }
    if gate["status"] == "insufficient_data":
        gate["reasons"].append("未達最少 3 folds／150 場未見 test 資料門檻；只作示範性研究。")
        return gate
    if overall["overlay_brier_delta_vs_control"] is None or overall["overlay_brier_delta_vs_control"] > -0.005:
        gate["reasons"].append("整體 Brier 未改善至少 0.005。")
    if brier_improved_folds < math.ceil(2 * complete_fold_count / 3):
        gate["reasons"].append("至少 2/3 fold 的 Brier 改善要求未通過。")
    if log_not_worse_folds < math.ceil(2 * complete_fold_count / 3):
        gate["reasons"].append("至少 2/3 fold 的 log score 不惡化要求未通過。")
    if not gate["probability_sum_ok"]:
        gate["reasons"].append("posterior draw 的場內機率守恆未通過。")
    gate["reasons"].append("本 temperature overlay 嚴格保序，無法評估或改善頭馬 rank 7+；該採納項只適用於日後非單調層級覆蓋實驗。")
    if tail_n < 25:
        gate["reasons"].append("頭馬 control rank 7+ 切片少於 25 場；日後非單調覆蓋實驗亦不得在此切片聲稱改善。")
    if not gate["reasons"]:
        gate["status"] = "eligible_for_separate_v103b_review"
        gate["adoption_decision"] = "REVIEW_REQUIRED"
    return gate


def build_markdown(report: dict[str, Any]) -> str:
    gate = report["pre_registered_gate"]
    lines = [
        "# V10.3 貝氏校準覆蓋層 Expanding-Window 驗證",
        "",
        "> 本報告只評估平行研究覆蓋層。正式 V10.2 `predicted_win_probability` 不會被本流程覆寫。",
        "",
        "| 項目 | 值 |",
        "|---|---|",
        f"| 來源工件 | `{report['input']['predictions']}` |",
        f"| 工件 SHA-256 | `{report['input']['artifact_sha256']}` |",
        f"| 產生時間（HKT） | {report['generated_at_hkt']} |",
        f"| 可評估完整賽事 | {report['coverage']['usable_races']} |",
        f"| 排除賽事 | {report['coverage']['excluded_races']} |",
        f"| 產生 fold | {len(report['folds'])} |",
        "",
        "## Fold 設計",
        "",
        "每個 fold 均按完整、不交疊賽日順序切分為 expanding train、連續 validation 和最後未見 test。prior SD 只在 validation 以 overlay Brier／log score 選擇；test 在設定鎖定後只評估一次。",
        "",
        "| Fold | Train 日期 | Validation 日期 | Test 日期 | 選定 prior SD | MAP T | Test 場次 | Δ Brier | Δ log score |",
        "|---:|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for fold in report["folds"]:
        metrics = fold["test_metrics"]
        lines.append(
            f"| {fold['fold_id']} | {fold['train_date_range']} | {fold['validation_date_range']} | {fold['test_date_range']} | "
            f"{fold['selected_prior_sd']:.3f} | {fold['posterior']['temperature_map']:.4f} | {metrics['races']} | "
            f"{metrics['overlay_brier_delta_vs_control']:.6f} | {metrics['overlay_log_score_delta_vs_control']:.6f} |"
        )
    lines.extend(
        [
            "",
            "## 未見 Test 總結",
            "",
            "| 指標 | Control V10.2 | V10.3 Bayesian overlay | 差異（overlay − control） |",
            "|---|---:|---:|---:|",
        ]
    )
    overall = report["test_metrics"]
    for title, control_key, overlay_key, delta_key, percent in [
        ("場內 Brier", "control_mean_brier", "overlay_mean_brier", "overlay_brier_delta_vs_control", False),
        ("場內 log score", "control_mean_log_score", "overlay_mean_log_score", "overlay_log_score_delta_vs_control", False),
        ("Top-1 勝出率", "control_top1_win_rate", "overlay_top1_win_rate", None, True),
        ("Top-3 包含頭馬率", "control_top3_rate", "overlay_top3_rate", None, True),
        ("頭馬 rank 7+ 比率", "control_rank_7_plus_rate", "overlay_rank_7_plus_rate", None, True),
    ]:
        control = overall.get(control_key)
        overlay = overall.get(overlay_key)
        delta = overall.get(delta_key) if delta_key else (overlay - control if control is not None and overlay is not None else None)
        formatter = (lambda value: "N/A" if value is None else f"{100 * value:.2f}%") if percent else (lambda value: "N/A" if value is None else f"{value:.6f}")
        lines.append(f"| {title} | {formatter(control)} | {formatter(overlay)} | {formatter(delta)} |")
    lines.extend(
        [
            "",
            "## 預先登記採納閘門",
            "",
            "| 項目 | 結果 |",
            "|---|---|",
            f"| 閘門狀態 | `{gate['status']}` |",
            f"| 觀察 folds／最低要求 | {gate['observed_folds']}／{gate['required_folds']} |",
            f"| 未見 test 場次／最低要求 | {gate['observed_test_races']}／{gate['required_test_races']} |",
            f"| 每 draw 場內機率守恆 | {'PASS' if gate['probability_sum_ok'] else 'FAIL'} |",
            f"| 版本決策 | `{gate['adoption_decision']}` |",
            "",
        ]
    )
    if gate["reasons"]:
        lines.extend(["### 未通過／未達門檻原因", ""])
        lines.extend(f"- {reason}" for reason in gate["reasons"])
        lines.append("")
    lines.extend(
        [
            "## 資料洩漏限制",
            "",
            "本驗證器只使用已保存的 V10.2 賽前場內機率及在評估階段才讀取的 `target_win`。每個 fold 的 posterior 只以其 train 賽事擬合，prior candidate 只用 validation 選擇，test 絕不參與設參。這是 Laplace 近似的 Bayesian temperature calibration scaffold，不是已部署的全貝氏 Plackett–Luce 主模型。",
            "",
            "單一二元賽果不可用於判定某匹馬的 credible interval 是否『包含真值』。不確定性表現只在未見 test 的群組 proper scores、posterior rank stability 與 Brier／tail-rank 切片中檢視。",
        ]
    )
    return "\n".join(lines) + "\n"


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    prediction_path = Path(args.predictions)
    prior_sds = parse_prior_sds(args.prior_sds)
    races, exclusions = load_frozen_races(prediction_path, args.probability_column, args.probability_tolerance)
    folds = make_folds(races, args.min_train_races, args.validation_races, args.test_races, args.max_folds)
    if not folds:
        raise WalkForwardError("可用賽事不足以形成任何 expanding-window fold；請降低示範窗口或累積更多預測工件。")
    fold_reports: list[dict[str, Any]] = []
    all_test_rows: list[dict[str, Any]] = []
    for fold_id, fold in enumerate(folds, start=1):
        candidates: list[tuple[tuple[float, float], float, TemperaturePosterior, list[dict[str, Any]]]] = []
        for prior_sd in prior_sds:
            posterior = fit_temperature_posterior(fold["train"], prior_sd)
            # Reuse the same normal sequence across prior candidates within a fold.
            # This makes validation selection reproducible and avoids candidate-order noise.
            validation_rng = random.Random(args.seed + fold_id * 10000)
            validation_rows = score_partition(fold["validation"], posterior, args.posterior_draws, validation_rng, fold_id, "validation")
            candidates.append((candidate_validation_score(validation_rows), prior_sd, posterior, validation_rows))
        _, selected_prior_sd, posterior, validation_rows = min(candidates, key=lambda item: (item[0], item[1]))
        # Refit only with the expanding training segment, never training+validation.
        # This keeps validation out of posterior fitting and makes its role strictly configuration selection.
        posterior = fit_temperature_posterior(fold["train"], selected_prior_sd)
        test_rng = random.Random(args.seed + fold_id * 10000 + 1)
        test_rows = score_partition(fold["test"], posterior, args.posterior_draws, test_rng, fold_id, "test")
        all_test_rows.extend(test_rows)
        fold_reports.append(
            {
                "fold_id": fold_id,
                "train_races": len(fold["train"]),
                "validation_races": len(fold["validation"]),
                "test_races": len(fold["test"]),
                "train_date_range": f"{fold['train'][0].meta.race_date.isoformat()} 至 {fold['train'][-1].meta.race_date.isoformat()}",
                "validation_date_range": f"{fold['validation'][0].meta.race_date.isoformat()} 至 {fold['validation'][-1].meta.race_date.isoformat()}",
                "test_date_range": f"{fold['test'][0].meta.race_date.isoformat()} 至 {fold['test'][-1].meta.race_date.isoformat()}",
                "selected_prior_sd": selected_prior_sd,
                "validation_candidate_scores": [
                    {"prior_sd": prior, "overlay_brier": score[0], "overlay_log_score": score[1]}
                    for score, prior, _, _ in candidates
                ],
                "posterior": asdict(posterior),
                "validation_metrics": partition_metrics(validation_rows),
                "test_metrics": partition_metrics(test_rows),
            }
        )
    gate = pre_registered_gate(all_test_rows, fold_reports)
    return {
        "schema_version": "v103_bayesian_overlay_walk_forward_v1",
        "generated_at_hkt": fixed_hkt_timestamp(),
        "status": "ok",
        "input": {
            "predictions": str(prediction_path),
            "artifact_sha256": artifact_sha256(prediction_path),
            "probability_column": args.probability_column,
            "artifact_only": True,
            "formal_probability_replacement": False,
        },
        "configuration": {
            "min_train_races": args.min_train_races,
            "validation_races": args.validation_races,
            "test_races": args.test_races,
            "max_folds": args.max_folds,
            "prior_sds": list(prior_sds),
            "posterior_draws": args.posterior_draws,
            "seed": args.seed,
            "probability_tolerance": args.probability_tolerance,
            "posterior_method": "one_dimensional_laplace_temperature_overlay_rank_preserving",
            "temporal_split_unit": "race_date_complete_non_overlapping",
        },
        "coverage": {
            "usable_races": len(races),
            "excluded_races": sum(exclusions.values()),
            "exclusions": dict(exclusions),
            "available_date_range": f"{races[0].meta.race_date.isoformat()} 至 {races[-1].meta.race_date.isoformat()}",
        },
        "folds": fold_reports,
        "test_metrics": partition_metrics(all_test_rows),
        "pre_registered_gate": gate,
        "method_limitations": [
            "本工具只評估保存 V10.2 賽前機率上的實驗性 Bayesian temperature overlay，不重建 ELO 或賽前特徵。",
            "來源工件沒有精確 scheduled start；因此 train、validation 和 test 一律以完整、不交疊 race_date 切分，避免同日資料跨邊界。",
            "Laplace posterior 是 V10.3-P1 的低依賴概念驗證；在宣稱完整 Bayesian model 前，必須與固定 slice 的 NUTS reference posterior 比對。",
            "單一正 temperature 嚴格保留 V10.2 場內排序，因此 Top-1／Top-3、頭馬 rank 7+ 與 rank stability 的改變不屬本實驗可評估目標。",
            "posterior mean 僅作平行研究欄位，不能改寫 V10.2 正式勝率或直接用作投注輸出。",
        ],
        "test_race_metrics": all_test_rows,
    }


def write_race_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) if rows else ["fold_id", "partition"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    try:
        report = build_report(args)
    except (BacktestInputError, WalkForwardError) as exc:
        print(json.dumps({"status": "input_error", "error": str(exc)}, ensure_ascii=False))
        return 2
    json_path = Path(args.output_json)
    md_path = Path(args.output_md)
    race_csv_path = Path(args.output_race_csv)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(build_markdown(report), encoding="utf-8")
    write_race_csv(race_csv_path, report["test_race_metrics"])
    print(json.dumps({
        "status": report["status"],
        "folds": len(report["folds"]),
        "usable_races": report["coverage"]["usable_races"],
        "test_races": report["test_metrics"]["races"],
        "gate_status": report["pre_registered_gate"]["status"],
        "output_json": str(json_path),
        "output_md": str(md_path),
        "output_race_csv": str(race_csv_path),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
