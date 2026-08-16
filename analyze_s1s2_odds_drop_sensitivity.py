"""Counterfactual S1/S2 odds-drop sensitivity analysis.

Uses only archived prediction JSONs that already passed the strict T-15/T-5 gate.
Official outcomes, when supplied, are joined separately and never affect the
counterfactual pre-race probability calculation.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

FONT = "Noto Sans CJK TC"
DEFAULT_WEIGHTS = (0.0, 0.05, 0.10, 0.20, 0.30)


@dataclass
class RacePrediction:
    race_key: str
    generated_at_utc: str
    baseline_weight: float
    rows: list[dict[str, Any]]
    provenance: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="海外 S1/S2 閘前落飛權重敏感度測試")
    parser.add_argument("--prediction-glob", required=True, help="已固定海外預測 JSON 的 glob；只接受已通過 T-15/T-5 閘門的輸出。")
    parser.add_argument("--weights", default=",".join(map(str, DEFAULT_WEIGHTS)), help="以逗號分隔的 log-weight，例如 0,0.05,0.10,0.20,0.30")
    parser.add_argument("--results-csv", help="可選官方賽果 CSV：race_key,horse_no,finish_pos；只用於事後 ROI，不影響賽前機率。")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--fixture-mode", action="store_true", help="明確標記輸入為隔離測試資料；輸出不會被描述為歷史績效。")
    parser.add_argument("--baseline-weight", type=float, default=None, help="若預測 JSON 未提供基本落飛權重，以此覆寫；預設由有標記馬的 odds_drop_weight 推斷。")
    return parser.parse_args()


def race_key(payload: dict[str, Any], path: Path) -> str:
    race = payload.get("race", {})
    race_id = race.get("overseas_race_id")
    if race_id is not None:
        return f"overseas_race_id:{race_id}"
    return f"{race.get('meeting_date', 'unknown')}|{race.get('simulcast_code', 'S?')}|{race.get('race_no', path.stem)}"


def infer_baseline(rows: list[dict[str, Any]], override: float | None) -> float:
    if override is not None:
        return override
    values = {round(float(row.get("odds_drop_weight") or 0.0), 10) for row in rows if bool(row.get("odds_drop_flag"))}
    if len(values) == 1:
        return float(values.pop())
    if not values:
        return 0.0
    raise ValueError("同一賽事的落飛權重不一致；請用 --baseline-weight 明確指定。")


def load_predictions(pattern: str, override: float | None, fixture_mode: bool) -> tuple[list[RacePrediction], list[dict[str, Any]]]:
    accepted: list[RacePrediction] = []
    exclusions: list[dict[str, Any]] = []
    for filename in sorted(glob.glob(pattern, recursive=True)):
        path = Path(filename)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            exclusions.append({"path": str(path), "reason": f"invalid_json:{type(exc).__name__}"})
            continue
        rows = payload.get("predictions")
        if not isinstance(rows, list) or not rows:
            exclusions.append({"path": str(path), "reason": "missing_predictions"})
            continue
        if payload.get("odds_snapshot_status") != "complete" or payload.get("input_status") not in {"complete", "degraded"}:
            exclusions.append({"path": str(path), "reason": "incomplete_prediction_or_odds_snapshot"})
            continue
        valid_rows: list[dict[str, Any]] = []
        for row in rows:
            detail = row.get("feature_detail", {}).get("odds_drop", {}) if isinstance(row.get("feature_detail"), dict) else {}
            if bool(row.get("odds_drop_flag")):
                ratio = row.get("odds_drop_ratio")
                if not isinstance(ratio, (int, float)) or ratio > -0.20 or not detail.get("t15_at_utc") or not detail.get("t5_at_utc"):
                    exclusions.append({"path": str(path), "horse_no": row.get("horse_no"), "reason": "invalid_drop_contract"})
                    continue
            if not isinstance(row.get("predicted_win_probability"), (int, float)) or not isinstance(row.get("horse_no"), int):
                exclusions.append({"path": str(path), "horse_no": row.get("horse_no"), "reason": "missing_probability_or_horse_no"})
                continue
            valid_rows.append(row)
        if len(valid_rows) < 2:
            exclusions.append({"path": str(path), "reason": "too_few_eligible_runners"})
            continue
        accepted.append(RacePrediction(race_key(payload, path), str(payload.get("generated_at_utc") or ""), infer_baseline(valid_rows, override), valid_rows, "isolated_fixture" if fixture_mode else "archived_prediction"))
    return accepted, exclusions


def load_results(path: str | None) -> dict[tuple[str, int], int]:
    if not path:
        return {}
    output: dict[tuple[str, int], int] = {}
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                output[(str(row["race_key"]), int(row["horse_no"]))] = int(row["finish_pos"])
            except (KeyError, TypeError, ValueError):
                continue
    return output


def rescore(rows: list[dict[str, Any]], baseline_weight: float, weight: float) -> list[dict[str, Any]]:
    raw: list[float] = []
    for row in rows:
        p = max(float(row["predicted_win_probability"]), 1e-12)
        is_drop = bool(row.get("odds_drop_flag"))
        raw.append(math.log(p) + ((weight - baseline_weight) if is_drop else 0.0))
    raw_np = np.asarray(raw, dtype=float)
    probs = np.exp(raw_np - raw_np.max())
    probs /= probs.sum()
    output = []
    for row, probability in zip(rows, probs, strict=True):
        odds = row.get("win_odds")
        ev = probability * float(odds) - 1.0 if isinstance(odds, (int, float)) and float(odds) > 1.0 else None
        output.append({**row, "counterfactual_probability": float(probability), "counterfactual_ev": ev})
    return sorted(output, key=lambda item: (-item["counterfactual_probability"], int(item["horse_no"])))


def max_drawdown(equity: list[float]) -> float | None:
    if not equity:
        return None
    peak = equity[0]
    drawdowns = []
    for value in equity:
        peak = max(peak, value)
        drawdowns.append(value - peak)
    return float(min(drawdowns))


def build_metrics(races: list[RacePrediction], results: dict[tuple[str, int], int], weights: list[float]) -> tuple[pd.DataFrame, pd.DataFrame]:
    summaries: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    baseline_top: dict[str, int] = {}
    for w_idx, weight in enumerate(weights):
        equity = [100.0]
        changed_top = 0
        selected_with_odds = 0
        selected_settled = 0
        selected_wins = 0
        total_return = 0.0
        total_stake = 0.0
        top_probabilities: list[float] = []
        top_evs: list[float] = []
        race_brier_scores: list[float] = []
        drop_runners = 0
        for race in sorted(races, key=lambda item: (item.generated_at_utc, item.race_key)):
            rescored = rescore(race.rows, race.baseline_weight, weight)
            top = rescored[0]
            race_outcomes = [results.get((race.race_key, int(row["horse_no"]))) for row in rescored]
            if all(outcome is not None for outcome in race_outcomes):
                race_brier_scores.append(float(sum((float(row["counterfactual_probability"]) - (1.0 if outcome == 1 else 0.0)) ** 2 for row, outcome in zip(rescored, race_outcomes, strict=True))))
            if w_idx == 0:
                baseline_top[race.race_key] = int(top["horse_no"])
            elif baseline_top.get(race.race_key) != int(top["horse_no"]):
                changed_top += 1
            drop_runners += sum(bool(row.get("odds_drop_flag")) for row in rescored)
            top_probabilities.append(float(top["counterfactual_probability"]))
            if top["counterfactual_ev"] is not None:
                top_evs.append(float(top["counterfactual_ev"]))
            outcome = results.get((race.race_key, int(top["horse_no"])))
            pnl: float | None = None
            if outcome is not None and isinstance(top.get("win_odds"), (int, float)) and float(top["win_odds"]) > 1.0:
                selected_with_odds += 1
                selected_settled += 1
                total_stake += 1.0
                gross = float(top["win_odds"]) if outcome == 1 else 0.0
                pnl = gross - 1.0
                total_return += gross
                selected_wins += int(outcome == 1)
                equity.append(equity[-1] + pnl)
            details.append({"weight": weight, "race_key": race.race_key, "generated_at_utc": race.generated_at_utc, "horse_no": top["horse_no"], "horse_name": top.get("horse_name"), "top_probability": top["counterfactual_probability"], "top_ev": top["counterfactual_ev"], "win_odds": top.get("win_odds"), "finish_pos": outcome, "pnl_per_unit": pnl, "baseline_weight": race.baseline_weight, "odds_drop_flag": bool(top.get("odds_drop_flag"))})
        has_outcomes = selected_settled > 0
        summaries.append({"weight": weight, "races": len(races), "drop_flagged_runners": drop_runners, "top_pick_changes_vs_first_weight": changed_top, "top_pick_change_rate": changed_top / len(races) if races else None, "mean_top_probability": float(np.mean(top_probabilities)) if top_probabilities else None, "mean_top_ev": float(np.mean(top_evs)) if top_evs else None, "brier_score": float(np.mean(race_brier_scores)) if race_brier_scores else None, "brier_races": len(race_brier_scores), "priced_top_picks": selected_with_odds, "settled_top_picks": selected_settled, "top_pick_wins": selected_wins if has_outcomes else None, "top_pick_win_rate": selected_wins / selected_settled if has_outcomes else None, "realized_roi": ((total_return - total_stake) / total_stake) if total_stake else None, "max_drawdown_units": max_drawdown(equity) if has_outcomes else None, "analysis_status": "historical_settled" if has_outcomes else "counterfactual_only_no_official_results"})
    return pd.DataFrame(summaries), pd.DataFrame(details)


def create_charts(summary: pd.DataFrame, details: pd.DataFrame, output_dir: Path, fixture_mode: bool) -> None:
    plt.style.use("seaborn-v0_8-whitegrid")
    plt.rcParams.update({"font.family": FONT, "axes.unicode_minus": False})
    note = "隔離 fixture：僅驗證流程，非歷史績效" if fixture_mode else "僅使用合資格賽前快照；無結算資料時 ROI 為 N/A"
    fig, ax = plt.subplots(figsize=(11, 6))
    ax.plot(summary["weight"], summary["mean_top_probability"], marker="o", label="首選平均勝率")
    ax.set_title("S1/S2 落飛權重敏感度：首選機率")
    ax.set_xlabel("odds-drop log weight")
    ax.set_ylabel("平均首選勝率")
    ax.text(0.01, 0.02, note, transform=ax.transAxes, fontsize=9, color="#7A2E0E")
    fig.tight_layout(); fig.savefig(output_dir / "01_top_probability_sensitivity.png", dpi=180); plt.close(fig)
    if summary["realized_roi"].notna().any():
        fig, ax = plt.subplots(figsize=(11, 6))
        ax.plot(summary["weight"], summary["realized_roi"], marker="o", color="#167C80", label="已結算 ROI")
        ax.axhline(0, color="#333333", linewidth=1)
        ax.set_title("S1/S2 落飛權重敏感度：已結算 ROI")
        ax.set_xlabel("odds-drop log weight"); ax.set_ylabel("ROI")
        ax.text(0.01, 0.02, note, transform=ax.transAxes, fontsize=9, color="#7A2E0E")
        fig.tight_layout(); fig.savefig(output_dir / "02_roi_sensitivity.png", dpi=180); plt.close(fig)


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir); output_dir.mkdir(parents=True, exist_ok=True)
    weights = sorted({float(value.strip()) for value in args.weights.split(",") if value.strip()})
    if any(weight < 0 or weight > 1 for weight in weights):
        raise SystemExit("權重必須在 0 至 1 之間。")
    races, exclusions = load_predictions(args.prediction_glob, args.baseline_weight, args.fixture_mode)
    results = load_results(args.results_csv)
    if not races:
        summary = {"status": "N/A_no_eligible_complete_t15_t5_predictions", "prediction_glob": args.prediction_glob, "weights": weights, "eligible_races": 0, "exclusions": exclusions, "note": "未以最終賠率、賽後派彩或不完整快照替代。"}
        (output_dir / "sensitivity_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2)); return 0
    summary_df, detail_df = build_metrics(races, results, weights)
    summary_df.to_csv(output_dir / "weight_sensitivity_summary.csv", index=False)
    detail_df.to_csv(output_dir / "top_pick_sensitivity_details.csv", index=False)
    pd.DataFrame(exclusions).to_csv(output_dir / "sensitivity_exclusions.csv", index=False)
    create_charts(summary_df, detail_df, output_dir, args.fixture_mode)
    report = {"status": "completed", "provenance": "isolated_fixture" if args.fixture_mode else "archived_prerace_predictions", "weights": weights, "eligible_races": len(races), "official_result_rows_loaded": len(results), "summary": summary_df.where(pd.notna(summary_df), None).to_dict(orient="records"), "exclusions": exclusions, "warning": "權重敏感度是反事實研究。若沒有逐場官方已結算結果，ROI、命中率及回撤均為 N/A。"}
    (output_dir / "sensitivity_summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
