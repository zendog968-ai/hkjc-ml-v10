#!/usr/bin/env python3
"""Recent, leakage-safe evaluator for settled V10.2 win-prediction artifacts.

This tool evaluates predictions that were generated before this backtest process.
It never retrains a model or re-creates pre-race features from settled outcomes.
Official outcomes are represented only by the existing ``target_win`` label in the
input artifact, and are read exclusively for post-race scoring.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any, Iterable


DEFAULT_PREDICTIONS = "v102_multiseason_backtest_predictions.csv"
DEFAULT_JSON = "archive/backtest_reports/recent_v102_backtest.json"
DEFAULT_MD = "archive/backtest_reports/recent_v102_backtest.md"
REQUIRED_COLUMNS = {
    "race_date",
    "racecourse",
    "race_no",
    "horse_name",
    "target_win",
}


@dataclass(frozen=True)
class RaceMeta:
    group_id: str
    race_date: date
    racecourse: str
    race_no: str


@dataclass
class RaceResult:
    group_id: str
    race_date: str
    racecourse: str
    race_no: str
    field_size: int
    winner: str | None = None
    top_pick: str | None = None
    top_pick_won: bool | None = None
    winner_in_model_top3: bool | None = None
    winner_model_rank: int | None = None
    winner_predicted_probability: float | None = None
    brier_score: float | None = None
    uniform_brier_score: float | None = None
    status: str = "evaluated"
    reason: str | None = None


class BacktestInputError(ValueError):
    """Raised when the input artifact cannot support a safe backtest."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate recent settled V10.2 probability predictions without re-creating pre-race features."
    )
    parser.add_argument("--predictions", default=DEFAULT_PREDICTIONS, help="CSV prediction artifact to evaluate.")
    parser.add_argument(
        "--probability-column",
        default="race_normalized_probability",
        help="Column containing field-normalized pre-race win probabilities.",
    )
    window = parser.add_mutually_exclusive_group()
    window.add_argument(
        "--recent-races",
        type=int,
        default=50,
        help="Evaluate this many most-recent race groups in the artifact (default: 50).",
    )
    window.add_argument(
        "--recent-days",
        type=int,
        help="Evaluate groups within this many calendar days of the latest artifact race date.",
    )
    parser.add_argument("--calibration-bins", type=int, default=5, help="Number of probability calibration bins (default: 5).")
    parser.add_argument("--probability-tolerance", type=float, default=1e-6, help="Allowed absolute field probability-sum deviation.")
    parser.add_argument("--output-json", default=DEFAULT_JSON, help="Path for the machine-readable report.")
    parser.add_argument("--output-md", default=DEFAULT_MD, help="Path for the human-readable report.")
    return parser.parse_args()


def parse_race_date(raw: str | None) -> date:
    value = (raw or "").strip()
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise BacktestInputError(f"無法解析 race_date：{value!r}；要求 YYYY-MM-DD。") from exc


def parse_probability(raw: str | None) -> float | None:
    try:
        value = float((raw or "").strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def parse_target_win(raw: str | None) -> int | None:
    try:
        value = float((raw or "").strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value not in (0.0, 1.0):
        return None
    return int(value)


def normalise_header(header: str | None) -> str:
    return (header or "").lstrip("\ufeff").strip()


def load_prediction_rows(path: Path, probability_column: str) -> tuple[list[dict[str, str]], list[RaceMeta]]:
    if not path.exists():
        raise BacktestInputError(f"找不到預測檔：{path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise BacktestInputError("預測 CSV 沒有欄位標頭。")
        fieldnames = {normalise_header(name) for name in reader.fieldnames}
        missing = sorted(REQUIRED_COLUMNS - fieldnames)
        if missing:
            raise BacktestInputError(f"預測 CSV 缺少必要欄位：{', '.join(missing)}")
        if probability_column not in fieldnames:
            raise BacktestInputError(f"預測 CSV 缺少機率欄位：{probability_column}")
        rows = []
        race_meta: dict[str, RaceMeta] = {}
        for index, raw in enumerate(reader, start=2):
            row = {normalise_header(key): (value or "").strip() for key, value in raw.items()}
            row["_row_number"] = str(index)
            try:
                parsed_date = parse_race_date(row.get("race_date"))
            except BacktestInputError:
                # The group cannot safely be dated; retain it for an explicit exclusion record.
                parsed_date = None
            group_id = row.get("race_group") or "|".join(
                [row.get("race_date", ""), row.get("racecourse", ""), row.get("race_no", "")]
            )
            row["_group_id"] = group_id
            rows.append(row)
            if parsed_date is not None and group_id and group_id not in race_meta:
                race_meta[group_id] = RaceMeta(
                    group_id=group_id,
                    race_date=parsed_date,
                    racecourse=row.get("racecourse", ""),
                    race_no=row.get("race_no", ""),
                )
    if not race_meta:
        raise BacktestInputError("預測 CSV 沒有可解析的賽事日期與群組。")
    return rows, list(race_meta.values())


def race_sort_key(meta: RaceMeta) -> tuple[date, str, int, str]:
    try:
        race_no = int(meta.race_no)
    except ValueError:
        race_no = 9999
    return meta.race_date, meta.racecourse, race_no, meta.group_id


def select_recent_groups(metas: Iterable[RaceMeta], recent_races: int | None, recent_days: int | None) -> list[RaceMeta]:
    ordered = sorted(metas, key=race_sort_key, reverse=True)
    if recent_days is not None:
        if recent_days < 1:
            raise BacktestInputError("--recent-days 必須為正整數。")
        latest = ordered[0].race_date
        cutoff = latest - timedelta(days=recent_days - 1)
        return [meta for meta in ordered if meta.race_date >= cutoff]
    if recent_races is None or recent_races < 1:
        raise BacktestInputError("--recent-races 必須為正整數。")
    return ordered[:recent_races]


def evaluate_group(meta: RaceMeta, rows: list[dict[str, str]], probability_column: str, tolerance: float) -> tuple[RaceResult, list[tuple[float, int]]]:
    base = RaceResult(
        group_id=meta.group_id,
        race_date=meta.race_date.isoformat(),
        racecourse=meta.racecourse,
        race_no=meta.race_no,
        field_size=len(rows),
    )
    if len(rows) < 2:
        base.status, base.reason = "excluded", "field_size_lt_2"
        return base, []
    horse_names = [row.get("horse_name", "") for row in rows]
    if any(not horse for horse in horse_names):
        base.status, base.reason = "excluded", "missing_horse_name"
        return base, []
    if len(set(horse_names)) != len(horse_names):
        base.status, base.reason = "excluded", "duplicate_horse_name"
        return base, []
    labels = [parse_target_win(row.get("target_win")) for row in rows]
    if any(label is None for label in labels):
        base.status, base.reason = "excluded", "invalid_target_win"
        return base, []
    if sum(labels) != 1:
        base.status, base.reason = "excluded", "winner_count_not_one"
        return base, []
    probabilities = [parse_probability(row.get(probability_column)) for row in rows]
    if any(probability is None for probability in probabilities):
        base.status, base.reason = "excluded", "missing_or_nonfinite_probability"
        return base, []
    if any(probability < 0.0 or probability > 1.0 for probability in probabilities):
        base.status, base.reason = "excluded", "probability_out_of_range"
        return base, []
    probability_sum = sum(probabilities)
    if abs(probability_sum - 1.0) > tolerance:
        base.status, base.reason = "excluded", "probability_sum_not_one"
        return base, []

    ordered = sorted(zip(horse_names, probabilities, labels), key=lambda item: (-item[1], item[0]))
    winner_index = labels.index(1)
    winner_name = horse_names[winner_index]
    winner_probability = probabilities[winner_index]
    winner_rank = next(index for index, (name, _, _) in enumerate(ordered, start=1) if name == winner_name)
    brier = sum((probability - float(label)) ** 2 for probability, label in zip(probabilities, labels))
    uniform_probability = 1.0 / len(rows)
    uniform_brier = sum((uniform_probability - float(label)) ** 2 for label in labels)
    base.winner = winner_name
    base.top_pick = ordered[0][0]
    base.top_pick_won = ordered[0][0] == winner_name
    base.winner_in_model_top3 = any(name == winner_name for name, _, _ in ordered[:3])
    base.winner_model_rank = winner_rank
    base.winner_predicted_probability = winner_probability
    base.brier_score = brier
    base.uniform_brier_score = uniform_brier
    return base, list(zip(probabilities, labels))


def calibration_summary(pairs: list[tuple[float, int]], bins: int) -> list[dict[str, Any]]:
    if bins < 2:
        raise BacktestInputError("--calibration-bins 必須不少於 2。")
    bucket_values: list[list[tuple[float, int]]] = [[] for _ in range(bins)]
    for probability, label in pairs:
        index = min(int(probability * bins), bins - 1)
        bucket_values[index].append((probability, label))
    result = []
    for index, values in enumerate(bucket_values):
        lower = index / bins
        upper = (index + 1) / bins
        result.append(
            {
                "lower_inclusive": lower,
                "upper_inclusive": upper if index == bins - 1 else None,
                "upper_exclusive": None if index == bins - 1 else upper,
                "runners": len(values),
                "mean_predicted_probability": mean([item[0] for item in values]) if values else None,
                "observed_win_rate": mean([item[1] for item in values]) if values else None,
            }
        )
    return result


def percentage(value: float | None) -> str:
    return "N/A" if value is None else f"{100.0 * value:.2f}%"


def build_markdown(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    coverage = report["coverage"]
    sample_warning = (
        "\n> **探索性樣本警告：** 已評估賽事少於 15 場；本報告只適合作為診斷，不應用作模型強弱或策略優勢的定論。\n"
        if coverage["evaluated_races"] < 15
        else ""
    )
    lines = [
        "# V10.2 近期預測自動回測",
        "",
        "| 欄位 | 值 |",
        "|---|---|",
        f"| 預測資料 | `{report['input']['predictions']}` |",
        f"| 機率欄位 | `{report['input']['probability_column']}` |",
        f"| 回測窗口 | {report['window']['description']} |",
        f"| 資料日期範圍 | {coverage['date_range'] or 'N/A'} |",
        f"| 產生時間（HKT） | {report['generated_at_hkt']} |",
        "",
        "## 覆蓋與完整性",
        "",
        "| 指標 | 值 |",
        "|---|---:|",
        f"| 選取賽事群組 | {coverage['selected_race_groups']} |",
        f"| 已評估賽事 | {coverage['evaluated_races']} |",
        f"| 排除賽事 | {coverage['excluded_races']} |",
        f"| 已評估出賽馬 | {coverage['evaluated_runners']} |",
        "",
        "## 準確率與機率品質",
        "",
        "| 指標 | 結果 |",
        "|---|---:|",
        f"| 首選（Top-1）勝出率 | {percentage(metrics['top1_win_rate'])} |",
        f"| Top-3 包含頭馬率 | {percentage(metrics['top3_contains_winner_rate'])} |",
        f"| 平均頭馬預測機率 | {percentage(metrics['mean_winner_predicted_probability'])} |",
        f"| 平均頭馬模型排名 | {metrics['mean_winner_model_rank'] if metrics['mean_winner_model_rank'] is not None else 'N/A'} |",
        f"| 場內 Brier Score | {metrics['mean_race_brier_score'] if metrics['mean_race_brier_score'] is not None else 'N/A'} |",
        f"| 均勻機率 Brier 基準 | {metrics['mean_uniform_brier_score'] if metrics['mean_uniform_brier_score'] is not None else 'N/A'} |",
        f"| Brier 相對均勻基準改善 | {metrics['brier_improvement_vs_uniform'] if metrics['brier_improvement_vs_uniform'] is not None else 'N/A'} |",
        sample_warning.rstrip(),
        "",
        "## 校準分箱（逐馬）",
        "",
        "| 機率區間 | 出賽馬數 | 平均預測機率 | 實際頭馬率 |",
        "|---|---:|---:|---:|",
    ]
    for item in report["calibration"]:
        lower = item["lower_inclusive"]
        upper = item["upper_inclusive"] if item["upper_inclusive"] is not None else item["upper_exclusive"]
        closing = "]" if item["upper_inclusive"] is not None else ")"
        lines.append(
            f"| [{lower:.2f}, {upper:.2f}{closing} | {item['runners']} | "
            f"{percentage(item['mean_predicted_probability'])} | {percentage(item['observed_win_rate'])} |"
        )
    if report["exclusions"]:
        lines.extend(["", "## 排除原因", "", "| 原因 | 場次 |", "|---|---:|"])
        for reason, count in sorted(report["exclusions"].items()):
            lines.append(f"| `{reason}` | {count} |")
    lines.extend(
        [
            "",
            "## 方法與資料洩漏限制",
            "",
            "此工具只讀取已保存的預測機率與事後 `target_win` 標籤。它不會使用已結算賽果重建任何賽前特徵、重新訓練模型或覆寫預測。每場只在存在唯一頭馬、完整馬匹集合、有限且場內合計為 1 的機率向量時才計分。",
            "",
            "Brier Score 為每場所有出賽馬的平方誤差總和，再跨已評估賽事取平均；數值越低越好。均勻基準以同場每匹馬 `1 / field_size` 的機率計算，只有在相同已評估場次上才比較。",
        ]
    )
    return "\n".join(line for line in lines if line is not None) + "\n"


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    prediction_path = Path(args.predictions)
    rows, metas = load_prediction_rows(prediction_path, args.probability_column)
    selected = select_recent_groups(metas, args.recent_races, args.recent_days)
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[row["_group_id"]].append(row)

    results: list[RaceResult] = []
    pairs: list[tuple[float, int]] = []
    for meta in sorted(selected, key=race_sort_key):
        result, group_pairs = evaluate_group(meta, grouped[meta.group_id], args.probability_column, args.probability_tolerance)
        results.append(result)
        pairs.extend(group_pairs)
    evaluated = [item for item in results if item.status == "evaluated"]
    exclusions = Counter(item.reason for item in results if item.status != "evaluated" and item.reason)
    created = datetime.now().astimezone().astimezone().replace(microsecond=0)
    # The reporting host may be UTC; use an explicit fixed-offset HKT representation.
    generated_hkt = created.astimezone(datetime.strptime("+0800", "%z").tzinfo).strftime("%Y-%m-%d %H:%M:%S HKT")
    latest_date = max(meta.race_date for meta in selected).isoformat() if selected else None
    earliest_date = min(meta.race_date for meta in selected).isoformat() if selected else None
    mean_brier = mean([item.brier_score for item in evaluated]) if evaluated else None
    uniform_brier = mean([item.uniform_brier_score for item in evaluated]) if evaluated else None
    report = {
        "schema_version": "v1",
        "generated_at_hkt": generated_hkt,
        "status": "ok" if evaluated else "no_evaluable_races",
        "input": {
            "predictions": str(prediction_path),
            "probability_column": args.probability_column,
            "probability_tolerance": args.probability_tolerance,
            "artifact_only": True,
        },
        "window": {
            "recent_races": args.recent_races if args.recent_days is None else None,
            "recent_days": args.recent_days,
            "description": f"最近 {args.recent_days} 日（以資料中最新賽日為基準）" if args.recent_days is not None else f"最近 {args.recent_races} 場賽事群組",
        },
        "coverage": {
            "selected_race_groups": len(selected),
            "evaluated_races": len(evaluated),
            "excluded_races": len(results) - len(evaluated),
            "evaluated_runners": sum(item.field_size for item in evaluated),
            "date_range": f"{earliest_date} 至 {latest_date}" if earliest_date and latest_date else None,
        },
        "metrics": {
            "top1_win_rate": mean([float(item.top_pick_won) for item in evaluated]) if evaluated else None,
            "top3_contains_winner_rate": mean([float(item.winner_in_model_top3) for item in evaluated]) if evaluated else None,
            "mean_winner_predicted_probability": mean([item.winner_predicted_probability for item in evaluated]) if evaluated else None,
            "mean_winner_model_rank": mean([item.winner_model_rank for item in evaluated]) if evaluated else None,
            "mean_race_brier_score": mean_brier,
            "mean_uniform_brier_score": uniform_brier,
            "brier_improvement_vs_uniform": (uniform_brier - mean_brier) if mean_brier is not None and uniform_brier is not None else None,
        },
        "calibration": calibration_summary(pairs, args.calibration_bins),
        "exclusions": dict(exclusions),
        "race_results": [asdict(item) for item in results],
    }
    return report


def main() -> int:
    args = parse_args()
    try:
        report = build_report(args)
    except BacktestInputError as exc:
        print(json.dumps({"status": "input_error", "error": str(exc)}, ensure_ascii=False))
        return 2
    json_path = Path(args.output_json)
    md_path = Path(args.output_md)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(build_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report["status"],
                "evaluated_races": report["coverage"]["evaluated_races"],
                "top1_win_rate": report["metrics"]["top1_win_rate"],
                "top3_contains_winner_rate": report["metrics"]["top3_contains_winner_rate"],
                "mean_race_brier_score": report["metrics"]["mean_race_brier_score"],
                "mean_uniform_brier_score": report["metrics"]["mean_uniform_brier_score"],
                "output_json": str(json_path),
                "output_md": str(md_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
