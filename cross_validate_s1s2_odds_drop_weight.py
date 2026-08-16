"""Walk-forward quarterly cross-validation for S1/S2 odds-drop log weights.

Training-quarter selection and next-quarter evaluation are strictly separated.
No final odds, results or later forecasts are allowed into the pre-race scoring.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from analyze_s1s2_odds_drop_sensitivity import RacePrediction, build_metrics, load_predictions, load_results
from s1s2_dynamic_kelly import derive_policy, simulate_test_quarter

DEFAULT_WEIGHTS = "0,0.05,0.10,0.20,0.30"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="海外 S1/S2 落飛權重跨季度走步交叉驗證")
    parser.add_argument("--prediction-glob", required=True)
    parser.add_argument("--results-csv", required=True, help="官方結果 CSV：race_key,horse_no,finish_pos")
    parser.add_argument("--weights", default=DEFAULT_WEIGHTS)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--season-mode", choices=("hk_season", "calendar"), default="hk_season")
    parser.add_argument("--min-total-races", type=int, default=100, help="完整已結算賽事最低總數。")
    parser.add_argument("--min-train-races", type=int, default=100, help="每個訓練窗最低完整已結算場次。")
    parser.add_argument("--min-test-races", type=int, default=15, help="每個未見季度最低完整已結算場次。")
    parser.add_argument("--min-folds-for-recommendation", type=int, default=2)
    parser.add_argument("--baseline-weight", type=float, default=None)
    parser.add_argument("--enable-dynamic-kelly", action="store_true", help="只用訓練期校準、樣本量及回撤推導的動態 Kelly 縮減。")
    parser.add_argument("--initial-bankroll", type=float, default=100.0)
    parser.add_argument("--kelly-max-single-fraction", type=float, default=0.01)
    parser.add_argument("--kelly-max-race-fraction", type=float, default=0.02)
    parser.add_argument("--kelly-drawdown-trigger", type=float, default=0.10)
    parser.add_argument("--kelly-drawdown-multiplier", type=float, default=0.50)
    parser.add_argument("--kelly-min-ev", type=float, default=0.0)
    return parser.parse_args()


def parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def quarter_label(value: str, mode: str) -> str | None:
    moment = parse_time(value)
    if moment is None:
        return None
    month = moment.month
    if mode == "calendar":
        return f"{moment.year}-Q{((month - 1) // 3) + 1}"
    season_start = moment.year if month >= 9 else moment.year - 1
    quarter = 1 if month in (9, 10, 11) else 2 if month in (12, 1, 2) else 3 if month in (3, 4, 5) else 4
    return f"{season_start}/{str(season_start + 1)[-2:]}-Q{quarter}"


def fully_settled(race: RacePrediction, results: dict[tuple[str, int], int]) -> bool:
    positions = [results.get((race.race_key, int(row["horse_no"]))) for row in race.rows]
    return bool(positions) and all(position is not None for position in positions)


def choose_weight(train: list[RacePrediction], results: dict[tuple[str, int], int], weights: list[float]) -> tuple[float | None, list[dict[str, Any]]]:
    summary, _ = build_metrics(train, results, weights)
    records = summary.to_dict(orient="records")
    valid = [row for row in records if row.get("brier_score") is not None]
    if not valid:
        return None, records
    # Calibration comes first. ROI is merely a tie-breaker; this prevents selecting
    # a high-variance weight solely because a small number of payouts were large.
    valid.sort(key=lambda row: (float(row["brier_score"]), -(float(row["realized_roi"]) if row.get("realized_roi") is not None else -999.0), float(row["weight"])))
    return float(valid[0]["weight"]), records


def main() -> int:
    args = parse_args()
    output = Path(args.output_dir); output.mkdir(parents=True, exist_ok=True)
    weights = sorted({float(part.strip()) for part in args.weights.split(",") if part.strip()})
    if not weights or any(weight < 0 or weight > 1 for weight in weights):
        raise SystemExit("--weights 必須是一組 0 至 1 的數字。")
    races, exclusions = load_predictions(args.prediction_glob, args.baseline_weight, False)
    results = load_results(args.results_csv)
    eligible = [race for race in races if fully_settled(race, results) and quarter_label(race.generated_at_utc, args.season_mode)]
    if len(eligible) < args.min_total_races:
        payload = {"status": "N/A_insufficient_complete_settled_races", "eligible_complete_settled_races": len(eligible), "minimum_required": args.min_total_races, "prediction_files_accepted": len(races), "official_result_rows_loaded": len(results), "weights": weights, "dynamic_kelly_enabled": args.enable_dynamic_kelly, "dynamic_kelly_status": "disabled_insufficient_complete_settled_races" if args.enable_dynamic_kelly else "not_requested", "excluded_predictions": exclusions, "note": "未使用合成資料、最終賠率或不完整快照補足 100 場門檻。"}
        (output / "cross_validation_summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False, indent=2)); return 0
    grouped: dict[str, list[RacePrediction]] = {}
    for race in sorted(eligible, key=lambda item: (item.generated_at_utc, item.race_key)):
        grouped.setdefault(quarter_label(race.generated_at_utc, args.season_mode) or "unknown", []).append(race)
    quarters = list(grouped)
    folds: list[dict[str, Any]] = []
    selection_rows: list[dict[str, Any]] = []
    for idx in range(1, len(quarters)):
        test_quarter = quarters[idx]
        train = [race for quarter in quarters[:idx] for race in grouped[quarter]]
        test = grouped[test_quarter]
        if len(train) < args.min_train_races or len(test) < args.min_test_races:
            folds.append({"test_quarter": test_quarter, "status": "skipped_insufficient_train_or_test", "train_races": len(train), "test_races": len(test)})
            continue
        chosen, train_grid = choose_weight(train, results, weights)
        for record in train_grid:
            selection_rows.append({"test_quarter": test_quarter, "train_races": len(train), **record})
        if chosen is None:
            folds.append({"test_quarter": test_quarter, "status": "skipped_no_train_calibration", "train_races": len(train), "test_races": len(test)})
            continue
        test_summary, test_details = build_metrics(test, results, [chosen])
        metric = test_summary.to_dict(orient="records")[0]
        metric.update({"test_quarter": test_quarter, "status": "evaluated", "selected_weight_from_prior_quarters": chosen, "train_races": len(train), "test_races": len(test), "train_quarters": quarters[:idx]})
        if args.enable_dynamic_kelly:
            policy = derive_policy(train, results, chosen, args.min_train_races, args.kelly_max_single_fraction, args.kelly_max_race_fraction, args.kelly_drawdown_trigger, args.kelly_drawdown_multiplier, args.kelly_min_ev)
            kelly_rows, kelly_summary = simulate_test_quarter(test, results, policy, args.initial_bankroll)
            metric.update({"dynamic_kelly_status": policy.status, "dynamic_kelly_scale": policy.effective_kelly_scale, "dynamic_kelly_training_brier": policy.brier_score, "dynamic_kelly_training_equal_brier": policy.equal_brier_score, "dynamic_kelly_test_roi": kelly_summary["roi_on_staked_capital"], "dynamic_kelly_test_max_drawdown": kelly_summary["max_drawdown_fraction"], "dynamic_kelly_test_net_pnl": kelly_summary["net_pnl"], "dynamic_kelly_policy": json.dumps(policy.as_dict(), ensure_ascii=False)})
            pd.DataFrame(kelly_rows).to_csv(output / f"dynamic_kelly_details_{test_quarter.replace('/', '_')}.csv", index=False)
        folds.append(metric)
        test_details.assign(test_quarter=test_quarter, selected_weight=chosen).to_csv(output / f"test_details_{test_quarter.replace('/', '_')}.csv", index=False)
    evaluated = [fold for fold in folds if fold.get("status") == "evaluated"]
    selected = [float(fold["selected_weight_from_prior_quarters"]) for fold in evaluated]
    recommendation: dict[str, Any]
    if len(evaluated) < args.min_folds_for_recommendation:
        recommendation = {"status": "exploratory_insufficient_walkforward_folds", "evaluated_folds": len(evaluated), "minimum_required": args.min_folds_for_recommendation, "recommended_weight": None}
    else:
        frequency = Counter(selected)
        # Consensus avoids retroactively selecting a single best out-of-sample quarter.
        recommended = sorted(frequency, key=lambda weight: (-frequency[weight], weight))[0]
        recommendation = {"status": "candidate_for_further_monitoring", "evaluated_folds": len(evaluated), "recommended_weight": recommended, "selection_frequency": dict(sorted(frequency.items())), "warning": "此為走步候選，不是保證最優或保證回報；仍須持續監察未見季度。"}
    pd.DataFrame(folds).to_csv(output / "walkforward_fold_summary.csv", index=False)
    pd.DataFrame(selection_rows).to_csv(output / "training_weight_grid.csv", index=False)
    pd.DataFrame(exclusions).to_csv(output / "cross_validation_exclusions.csv", index=False)
    payload = {"status": "completed", "season_mode": args.season_mode, "eligible_complete_settled_races": len(eligible), "weights": weights, "minimums": {"total": args.min_total_races, "train": args.min_train_races, "test": args.min_test_races, "folds": args.min_folds_for_recommendation}, "dynamic_kelly_enabled": args.enable_dynamic_kelly, "recommendation": recommendation, "folds": folds, "excluded_predictions": exclusions, "method": "prior-quarter calibration selection then next-quarter evaluation; Brier first, ROI only tie-breaker"}
    (output / "cross_validation_summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
