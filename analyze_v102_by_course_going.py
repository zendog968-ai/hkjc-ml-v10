#!/usr/bin/env python3
"""Analyze V10.2 out-of-time performance by racecourse and official going."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

FONT = "Noto Sans CJK TC"
COLORS = {"ST": "#1F4E79", "HV": "#0F766E", "good": "#2563EB", "fast": "#D97706", "yielding": "#7C3AED", "other": "#64748B"}


def going_group(going: object) -> str:
    text = "" if pd.isna(going) else str(going).strip()
    if "快" in text:
        return "快地"
    if text == "好地" or text == "好":
        return "好地"
    if "黏" in text or "軟" in text or "濕" in text:
        return "黏／軟地"
    return text or "未標示"


def race_metrics(frame: pd.DataFrame) -> pd.Series:
    n = len(frame)
    prob = frame["race_normalized_probability"].astype(float).to_numpy()
    actual = frame["target_win"].astype(float).to_numpy()
    return pd.Series({
        "runners": n,
        "top_pick_win": int(((frame["model_rank"] == 1) & (frame["target_win"] == 1)).any()),
        "top3_contains_winner": int(((frame["model_rank"] <= 3) & (frame["target_win"] == 1)).any()),
        "race_brier": float(np.square(prob - actual).sum()),
        "uniform_brier": float(np.square((1 / n) - actual).sum()),
        "uniform_top_pick": 1 / n,
    })


def aggregate(grouped: pd.DataFrame, key: str) -> pd.DataFrame:
    rows = []
    for label, frame in grouped.groupby(key, dropna=False):
        races = frame.groupby("race_group", sort=False).apply(race_metrics, include_groups=False).reset_index(drop=True)
        rows.append({
            key: label,
            "races": len(races),
            "runners": int(races["runners"].sum()),
            "top_pick_win_rate": float(races["top_pick_win"].mean()),
            "top3_contains_winner_rate": float(races["top3_contains_winner"].mean()),
            "uniform_top_pick_rate": float(races["uniform_top_pick"].mean()),
            "mean_brier": float(races["race_brier"].mean()),
            "uniform_brier": float(races["uniform_brier"].mean()),
        })
    result = pd.DataFrame(rows)
    result["top_pick_lift"] = result["top_pick_win_rate"] / result["uniform_top_pick_rate"]
    result["brier_improvement"] = result["uniform_brier"] - result["mean_brier"]
    result["sample_flag"] = np.where(result["races"] < 15, "探索性（<15 場）", "可比較")
    return result.sort_values("races", ascending=False).reset_index(drop=True)


def plot_metric_bars(table: pd.DataFrame, key: str, title: str, out: Path) -> None:
    labels = table[key].astype(str).tolist()
    x = np.arange(len(labels))
    fig, axes = plt.subplots(1, 2, figsize=(14, 5.6), constrained_layout=True)
    bars = axes[0].bar(x - 0.18, table["top_pick_win_rate"], 0.36, label="V10.2 首選", color="#15803D")
    axes[0].bar(x + 0.18, table["uniform_top_pick_rate"], 0.36, label="等機會基準", color="#94A3B8")
    for bar in bars:
        axes[0].annotate(f"{bar.get_height():.1%}", (bar.get_x() + bar.get_width()/2, bar.get_height()), xytext=(0, 4), textcoords="offset points", ha="center", fontsize=9)
    axes[0].set_title("首選勝出率對基準")
    axes[0].set_ylabel("勝出率")
    axes[0].yaxis.set_major_formatter(lambda value, _: f"{value:.0%}")
    axes[0].set_xticks(x, labels)
    axes[0].legend(frameon=False)

    bars = axes[1].bar(x - 0.18, table["mean_brier"], 0.36, label="V10.2", color="#2563EB")
    axes[1].bar(x + 0.18, table["uniform_brier"], 0.36, label="等機會基準", color="#94A3B8")
    for bar in bars:
        axes[1].annotate(f"{bar.get_height():.3f}", (bar.get_x() + bar.get_width()/2, bar.get_height()), xytext=(0, 4), textcoords="offset points", ha="center", fontsize=9)
    axes[1].set_title("場內 Brier score（越低越佳）")
    axes[1].set_ylabel("Brier score")
    axes[1].set_xticks(x, labels)
    axes[1].legend(frameon=False)

    for ax in axes:
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", color="#E2E8F0")
        ax.set_axisbelow(True)
    fig.suptitle(title, fontsize=16, fontweight="bold")
    fig.savefig(out, dpi=180, bbox_inches="tight")
    plt.close(fig)


def plot_condition_heatmap(cross: pd.DataFrame, out: Path) -> None:
    pivot = cross.pivot(index="going_group", columns="racecourse", values="top_pick_win_rate")
    annot = pivot.copy().astype(object)
    for row in annot.index:
        for col in annot.columns:
            races = cross[(cross["going_group"] == row) & (cross["racecourse"] == col)]["races"]
            n = int(races.iloc[0]) if not races.empty else 0
            value = annot.loc[row, col]
            annot.loc[row, col] = f"{value:.1%}\n(n={n})" if pd.notna(value) else "—"
    fig, ax = plt.subplots(figsize=(7.4, 4.8), constrained_layout=True)
    matrix = pivot.to_numpy(dtype=float)
    image = ax.imshow(matrix, cmap="YlGnBu", aspect="auto", vmin=np.nanmin(matrix), vmax=np.nanmax(matrix))
    ax.set_xticks(np.arange(len(pivot.columns)), pivot.columns)
    ax.set_yticks(np.arange(len(pivot.index)), pivot.index)
    ax.set_title("首選勝出率：馬場 × 場地狀況")
    for i in range(matrix.shape[0]):
        for j in range(matrix.shape[1]):
            text = annot.iloc[i, j]
            ax.text(j, i, text, ha="center", va="center", fontsize=10,
                    color="white" if matrix[i, j] > np.nanmean(matrix) else "black")
    fig.colorbar(image, ax=ax, label="首選勝出率")
    fig.savefig(out, dpi=180, bbox_inches="tight")
    plt.close(fig)


def plot_calibration_by_course(data: pd.DataFrame, out: Path) -> None:
    fig, ax = plt.subplots(figsize=(7.5, 6.0), constrained_layout=True)
    for course, color in [("ST", COLORS["ST"]), ("HV", COLORS["HV"])]:
        subset = data[data["racecourse"] == course].copy()
        if subset.empty:
            continue
        subset["bin"] = pd.qcut(subset["race_normalized_probability"], q=8, duplicates="drop")
        table = subset.groupby("bin", observed=True).agg(pred=("race_normalized_probability", "mean"), actual=("target_win", "mean")).reset_index(drop=True)
        ax.plot(table["pred"], table["actual"], marker="o", linewidth=2, label=course, color=color)
    ax.plot([0, 0.4], [0, 0.4], "--", color="#64748B", label="完全校準")
    ax.set_xlim(0, 0.4)
    ax.set_ylim(0, 0.4)
    ax.set_xlabel("平均預測勝率")
    ax.set_ylabel("實際勝出率")
    ax.set_title("按馬場的時間外機率校準（逐馬列）")
    ax.legend(frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(color="#E2E8F0")
    fig.savefig(out, dpi=180, bbox_inches="tight")
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="hkjc_last_season.sqlite")
    parser.add_argument("--predictions", default="v102_multiseason_backtest_predictions.csv")
    parser.add_argument("--output-dir", default="v102_condition_analysis")
    args = parser.parse_args()
    plt.rcParams.update({"font.family": FONT, "axes.unicode_minus": False})
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    preds = pd.read_csv(args.predictions, encoding="utf-8-sig")
    conn = sqlite3.connect(args.db)
    races = pd.read_sql_query(
        """
        SELECT race_date,racecourse,race_no,going,surface,distance_m,course_config
        FROM races WHERE race_status='completed'
        """, conn)
    conn.close()
    data = preds.merge(races, on=["race_date", "racecourse", "race_no"], how="left", validate="many_to_one")
    if data["going"].isna().any():
        raise RuntimeError("Time-out predictions could not be matched to official race conditions.")
    data["going_group"] = data["going"].map(going_group)

    course = aggregate(data, "racecourse")
    going = aggregate(data, "going_group")
    cross = aggregate(data, "racecourse")  # overwritten below with racecourse x condition metrics
    rows = []
    for (course_name, condition), frame in data.groupby(["racecourse", "going_group"], dropna=False):
        metric = aggregate(frame.assign(group="all"), "group").iloc[0].to_dict()
        metric.pop("group", None)
        metric["racecourse"] = course_name
        metric["going_group"] = condition
        rows.append(metric)
    cross = pd.DataFrame(rows).sort_values(["going_group", "racecourse"])

    course.to_csv(out / "course_performance.csv", index=False, encoding="utf-8-sig")
    going.to_csv(out / "going_performance.csv", index=False, encoding="utf-8-sig")
    cross.to_csv(out / "course_going_performance.csv", index=False, encoding="utf-8-sig")
    data[["race_date", "racecourse", "race_no", "going", "going_group", "target_win", "race_normalized_probability", "model_rank"]].to_csv(out / "test_predictions_with_conditions.csv", index=False, encoding="utf-8-sig")

    plot_metric_bars(course, "racecourse", "V10.2 時間外表現：沙田 vs 跑馬地", out / "01_course_performance.png")
    plot_metric_bars(going, "going_group", "V10.2 時間外表現：按官方場地狀況", out / "02_going_performance.png")
    plot_condition_heatmap(cross, out / "03_course_going_heatmap.png")
    plot_calibration_by_course(data, out / "04_course_calibration.png")

    summary = {
        "test_races": int(data["race_group"].nunique()),
        "raw_official_going_counts": data.groupby("going")["race_group"].nunique().sort_values(ascending=False).to_dict(),
        "course_performance": course.to_dict(orient="records"),
        "going_performance": going.to_dict(orient="records"),
        "course_going_performance": cross.to_dict(orient="records"),
        "notes": [
            "All metrics are computed only from the V10.2 time-out test interval.",
            "Groups with fewer than 15 races are marked exploratory and should not be treated as stable performance estimates.",
            "Going is the official reported condition; fast-going samples may correspond to non-turf / all-weather races and should be interpreted separately from turf conditions.",
        ],
    }
    (out / "condition_analysis_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
