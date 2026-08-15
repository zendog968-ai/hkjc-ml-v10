#!/usr/bin/env python3
"""Strict cross-quarter Double Trio stress and drawdown analysis.

Consumes one or more `double_trio_batch_details.csv` files produced by the V10.2
batch backtester.  It never substitutes final odds/payouts for missing pre-race
quotes: only rows already qualified by the batch backtester may enter indicator
EV, and only `settled=True` rows may enter realised ROI/drawdown.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

FONT = "Noto Sans CJK TC"


def as_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def hk_racing_season_quarter(ts: pd.Timestamp) -> str:
    """HK racing-season quarters: Sep-Nov Q1; Dec-Feb Q2; Mar-May Q3; Jun-Aug Q4."""
    month = ts.month
    season_start = ts.year if month >= 9 else ts.year - 1
    if month in (9, 10, 11): quarter = 1
    elif month in (12, 1, 2): quarter = 2
    elif month in (3, 4, 5): quarter = 3
    else: quarter = 4
    return f"{season_start}/{str(season_start + 1)[-2:]} Q{quarter}"


def calendar_quarter(ts: pd.Timestamp) -> str:
    return f"{ts.year} Q{((ts.month - 1) // 3) + 1}"


def input_paths(items: list[str], pattern: str | None) -> list[Path]:
    paths = [Path(item) for item in items]
    if pattern:
        paths.extend(Path(item) for item in glob.glob(pattern, recursive=True))
    unique = sorted({path.resolve() for path in paths})
    if not unique:
        raise ValueError("至少提供一個 --details 或 --details-glob")
    absent = [str(path) for path in unique if not path.exists()]
    if absent:
        raise ValueError("找不到輸入檔：" + "; ".join(absent))
    return unique


def load_details(paths: list[Path], allow_fixture: bool) -> tuple[pd.DataFrame, list[str]]:
    frames = []
    fixture_paths = []
    for path in paths:
        lower = str(path).lower()
        if "fixture" in lower or "test" in lower:
            fixture_paths.append(str(path))
        frame = pd.read_csv(path)
        frame["source_details_file"] = str(path)
        frames.append(frame)
    if fixture_paths and not allow_fixture:
        raise ValueError("偵測到 fixture／test 明細；不能產生真實跨季度結論。僅驗證程式時才用 --allow-fixture。")
    df = pd.concat(frames, ignore_index=True)
    required = {
        "snapshot_label", "snapshot_captured_at_utc", "model_generated_at_utc",
        "eligible", "settled", "stake", "indicator_expected_net", "actual_net_return",
        "actual_gross_return", "predicted_hit_probability",
    }
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError("批量明細缺少欄位：" + ", ".join(missing))
    df["snapshot_time"] = pd.to_datetime(df["snapshot_captured_at_utc"], utc=True, errors="coerce")
    df["model_time"] = pd.to_datetime(df["model_generated_at_utc"], utc=True, errors="coerce")
    if df["snapshot_time"].isna().any() or df["model_time"].isna().any():
        raise ValueError("明細含不可解析的 UTC 時間")
    for column in ("eligible", "settled"):
        df[column] = df[column].map(as_bool)
    df["time_valid"] = df["model_time"] <= df["snapshot_time"]
    corrupted = df["eligible"] & ~df["time_valid"]
    if corrupted.any():
        raise ValueError("輸入明細有標為 eligible 但模型晚於快照的列；archive 已受時間倒置污染")
    for column in ("stake", "indicator_expected_net", "actual_net_return", "actual_gross_return", "predicted_hit_probability"):
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df["settled_valid"] = df["settled"] & df["time_valid"] & df["actual_net_return"].notna() & df["stake"].gt(0)
    df["priced_valid"] = df["eligible"] & df["time_valid"] & df["indicator_expected_net"].notna() & df["stake"].gt(0)
    return df.sort_values(["snapshot_time", "candidate_file", "candidate_index"], kind="stable").reset_index(drop=True), fixture_paths


def max_drawdown(net_returns: pd.Series) -> dict[str, Any]:
    if net_returns.empty:
        return {"max_drawdown": np.nan, "max_drawdown_end_index": None, "max_drawdown_start_index": None, "longest_drawdown_observations": 0}
    equity = net_returns.cumsum()
    highwater = equity.cummax().clip(lower=0.0)
    drawdown = highwater - equity
    end_pos = int(drawdown.to_numpy().argmax())
    maximum = float(drawdown.iloc[end_pos])
    peak_before = equity.iloc[: end_pos + 1]
    starts = peak_before[peak_before == highwater.iloc[end_pos]]
    start_pos = int(starts.index[-1]) if not starts.empty else 0
    underwater = drawdown.gt(1e-12).to_numpy()
    run = longest = 0
    for flag in underwater:
        run = run + 1 if flag else 0
        longest = max(longest, run)
    return {"max_drawdown": maximum, "max_drawdown_end_index": end_pos, "max_drawdown_start_index": start_pos, "longest_drawdown_observations": int(longest)}


def quarter_summary(group: pd.DataFrame, min_settled: int) -> dict[str, Any]:
    settled = group[group["settled_valid"]].copy()
    priced = group[group["priced_valid"]].copy()
    stake = float(settled["stake"].sum())
    actual_net = float(settled["actual_net_return"].sum())
    indicator_stake = float(priced["stake"].sum())
    draw = max_drawdown(settled["actual_net_return"].reset_index(drop=True))
    return {
        "candidates_scanned": int(len(group)),
        "priced_candidates": int(len(priced)),
        "priced_coverage": float(len(priced) / len(group)) if len(group) else np.nan,
        "settled_candidates": int(len(settled)),
        "settlement_coverage_of_priced": float(len(settled) / len(priced)) if len(priced) else np.nan,
        "indicator_ev_per_stake": float(priced["indicator_expected_net"].sum() / indicator_stake) if indicator_stake else np.nan,
        "actual_total_stake": stake,
        "actual_total_net": actual_net,
        "actual_roi": float(actual_net / stake) if stake else np.nan,
        "hit_count": int(settled.get("hit", pd.Series(False, index=settled.index)).map(as_bool).sum()) if not settled.empty else 0,
        "hit_rate": float(settled.get("hit", pd.Series(False, index=settled.index)).map(as_bool).mean()) if not settled.empty else np.nan,
        "max_drawdown": draw["max_drawdown"],
        "max_drawdown_to_stake": float(draw["max_drawdown"] / stake) if stake else np.nan,
        "longest_drawdown_observations": draw["longest_drawdown_observations"],
        "sample_status": "可比較" if len(settled) >= min_settled else "探索性（已結算候選少於門檻）",
    }


def build_stress(df: pd.DataFrame, quarter_col: str, min_settled: int) -> pd.DataFrame:
    settled = df[df["settled_valid"]].copy()
    rows: list[dict[str, Any]] = []
    base = quarter_summary(df, min_settled)
    rows.append({"scenario": "全樣本", **base})
    if settled.empty:
        return pd.DataFrame(rows)
    best_index = settled["actual_net_return"].idxmax()
    without_best = df.drop(index=best_index)
    rows.append({"scenario": "移除單一最大正回報候選", **quarter_summary(without_best, min_settled)})
    quarter_nets = settled.groupby(quarter_col, sort=False)["actual_net_return"].sum()
    if len(quarter_nets) >= 2:
        worst_quarter = str(quarter_nets.idxmin())
        best_quarter = str(quarter_nets.idxmax())
        rows.append({"scenario": f"僅最差季度：{worst_quarter}", **quarter_summary(df[df[quarter_col] == worst_quarter], min_settled)})
        rows.append({"scenario": f"移除最佳季度：{best_quarter}", **quarter_summary(df[df[quarter_col] != best_quarter], min_settled)})
    return pd.DataFrame(rows)


def apply_rolling(df: pd.DataFrame, window: int) -> pd.DataFrame:
    out = df.copy()
    settled = out["settled_valid"]
    out["rolling_settled_count"] = settled.astype(int).rolling(window, min_periods=1).sum()
    out["rolling_net"] = out["actual_net_return"].where(settled, 0.0).rolling(window, min_periods=1).sum()
    out["rolling_stake"] = out["stake"].where(settled, 0.0).rolling(window, min_periods=1).sum()
    out["rolling_roi"] = np.where(out["rolling_stake"] > 0, out["rolling_net"] / out["rolling_stake"], np.nan)
    settled_net = out["actual_net_return"].where(settled, 0.0)
    out["cumulative_net"] = settled_net.cumsum()
    out["highwater_net"] = out["cumulative_net"].cummax().clip(lower=0.0)
    out["drawdown"] = out["highwater_net"] - out["cumulative_net"]
    return out


def make_charts(quarterly: pd.DataFrame, curve: pd.DataFrame, stress: pd.DataFrame, output: Path, source_quality: str) -> list[str]:
    plt.style.use("seaborn-v0_8-whitegrid")
    suffix = "【隔離 fixture，非真實歷史】" if source_quality == "fixture_only_not_real_history" else ""
    plt.rcParams.update({"font.family": FONT, "axes.unicode_minus": False})
    charts = []
    if not quarterly.empty:
        fig, axes = plt.subplots(1, 2, figsize=(15, 5.5))
        labels = quarterly["quarter"].astype(str)
        axes[0].bar(labels, quarterly["actual_roi"].fillna(0.0) * 100, color="#176B87")
        axes[0].axhline(0, color="#333333", linewidth=0.8)
        axes[0].set_title(f"孖T跨季度實現 ROI（僅已結算合資格候選）{suffix}")
        axes[0].set_ylabel("ROI（%）")
        axes[0].tick_params(axis="x", rotation=35)
        axes[1].bar(labels, quarterly["priced_coverage"].fillna(0.0) * 100, color="#5BAE8B")
        axes[1].set_title(f"跨季度可定價覆蓋率{suffix}")
        axes[1].set_ylabel("覆蓋率（%）")
        axes[1].tick_params(axis="x", rotation=35)
        fig.tight_layout()
        chart = output / "01_quarterly_roi_coverage.png"
        fig.savefig(chart, dpi=180, bbox_inches="tight")
        plt.close(fig)
        charts.append(str(chart))
    if not curve.empty:
        fig, axes = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
        axes[0].plot(curve["snapshot_time"], curve["cumulative_net"], color="#176B87", linewidth=2)
        axes[0].axhline(0, color="#333333", linewidth=0.8)
        axes[0].set_title(f"孖T已結算候選：跨季度累積淨回報{suffix}")
        axes[0].set_ylabel("累積淨回報（固定研究注額）")
        axes[1].fill_between(curve["snapshot_time"], curve["drawdown"], 0, color="#C44536", alpha=0.6)
        axes[1].set_title(f"峰值至谷底回撤{suffix}")
        axes[1].set_ylabel("回撤")
        fig.tight_layout()
        chart = output / "02_equity_drawdown.png"
        fig.savefig(chart, dpi=180, bbox_inches="tight")
        plt.close(fig)
        charts.append(str(chart))
    if not stress.empty:
        display = stress.dropna(subset=["actual_roi"])
        if not display.empty:
            fig, ax = plt.subplots(figsize=(12, 5.5))
            ax.barh(display["scenario"], display["actual_roi"] * 100, color="#B55D60")
            ax.axvline(0, color="#333333", linewidth=0.8)
            ax.set_title(f"跨季度壓力情境：實現 ROI 敏感度{suffix}")
            ax.set_xlabel("ROI（%）")
            fig.tight_layout()
            chart = output / "03_stress_scenarios.png"
            fig.savefig(chart, dpi=180, bbox_inches="tight")
            plt.close(fig)
            charts.append(str(chart))
    return charts


def main() -> int:
    parser = argparse.ArgumentParser(description="V10.2 孖T跨季度壓力測試與回撤分析")
    parser.add_argument("--details", action="append", default=[], help="可重複提供 double_trio_batch_details.csv")
    parser.add_argument("--details-glob", help="遞迴搜尋多個批量明細，例如 archive/**/double_trio_batch_details.csv")
    parser.add_argument("--snapshot-label", choices=["T_MINUS_15", "T_MINUS_5"], help="只分析一個時點；建議分開執行")
    parser.add_argument("--quarter-mode", choices=["calendar", "hk_season"], default="hk_season")
    parser.add_argument("--min-settled-per-quarter", type=int, default=15)
    parser.add_argument("--rolling-candidates", type=int, default=30)
    parser.add_argument("--allow-fixture", action="store_true", help="只供程式驗證；輸出會標為非真實資料")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    if args.min_settled_per_quarter < 1 or args.rolling_candidates < 1:
        raise ValueError("樣本門檻與滾動候選數必須至少為 1")
    paths = input_paths(args.details, args.details_glob)
    df, fixture_paths = load_details(paths, args.allow_fixture)
    if args.snapshot_label:
        df = df[df["snapshot_label"] == args.snapshot_label].copy()
    if df.empty:
        raise ValueError("篩選後沒有候選列；不可產生 ROI 結論")
    quarter_func = calendar_quarter if args.quarter_mode == "calendar" else hk_racing_season_quarter
    df["quarter"] = df["snapshot_time"].map(quarter_func)
    curve = apply_rolling(df, args.rolling_candidates)
    quarterly = (
        df.groupby(["snapshot_label", "quarter"], sort=False)
        .apply(lambda group: pd.Series(quarter_summary(group, args.min_settled_per_quarter)), include_groups=False)
        .reset_index()
    )
    stress = build_stress(df, "quarter", args.min_settled_per_quarter)
    global_draw = max_drawdown(curve.loc[curve["settled_valid"], "actual_net_return"].reset_index(drop=True))
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    curve.to_csv(output / "candidate_equity_curve.csv", index=False, encoding="utf-8-sig")
    quarterly.to_csv(output / "quarterly_stress_summary.csv", index=False, encoding="utf-8-sig")
    stress.to_csv(output / "stress_scenario_summary.csv", index=False, encoding="utf-8-sig")
    source_quality = "fixture_only_not_real_history" if fixture_paths else "candidate_details_supplied"
    charts = make_charts(quarterly, curve, stress, output, source_quality)
    overall = quarter_summary(df, args.min_settled_per_quarter)
    payload = {
        "analysis_type": "V10.2 Double Trio cross-quarter stress and drawdown",
        "source_quality": source_quality,
        "fixture_sources": fixture_paths,
        "input_files": [str(path) for path in paths],
        "snapshot_label_filter": args.snapshot_label,
        "quarter_mode": args.quarter_mode,
        "min_settled_per_quarter": args.min_settled_per_quarter,
        "rolling_candidates": args.rolling_candidates,
        "overall": overall,
        "global_max_drawdown": global_draw,
        "quarter_count": int(len(quarterly)),
        "time_inversion_rows_excluded": int((~df["time_valid"]).sum()),
        "data_quality_note": "若不存在具真實 T-15/T-5 組合快照的合資格候選，ROI、回撤與季度壓力結論必須為 N/A；不得用最終派彩、最終賠率或 fixture 補值。",
        "charts": charts,
    }
    (output / "cross_quarter_stress_summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
