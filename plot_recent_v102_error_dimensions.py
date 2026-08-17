#!/usr/bin/env python3
"""Multi-dimensional visual diagnostics for recent V10.2 prediction error cases.

Input is the already-evaluated race-level output from
``analyze_recent_v102_prediction_errors.py``. This script does not fit or alter
models and does not infer missing official conditions.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402


DEFAULT_INPUT = "archive/error_analysis/recent_v102_manual/race_error_cases.csv"
DEFAULT_OUTPUT = "archive/error_analysis/recent_v102_dimensions"
EXPLORATORY_N = 15


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plot multi-dimensional V10.2 prediction error diagnostics.")
    parser.add_argument("--input", default=DEFAULT_INPUT, help="Race-level error case CSV.")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT)
    parser.add_argument("--exploratory-n", type=int, default=EXPLORATORY_N)
    return parser.parse_args()


def configure_font() -> None:
    names = {font.name for font in font_manager.fontManager.ttflist}
    for candidate in ("Noto Sans CJK TC", "Noto Sans CJK SC", "Noto Sans CJK JP", "sans-serif"):
        if candidate in names:
            plt.rcParams["font.family"] = candidate
            break
    plt.rcParams["axes.unicode_minus"] = False
    plt.rcParams["figure.dpi"] = 120


def load_cases(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, encoding="utf-8-sig")
    required = {"status", "brier_score", "uniform_brier_score", "top_pick_won", "top_pick_probability", "winner_model_rank", "field_size"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"輸入檔缺少欄位：{', '.join(sorted(missing))}")
    frame = frame[frame["status"] == "evaluated"].copy()
    if frame.empty:
        raise ValueError("沒有可評估場次。")
    for column in ("brier_score", "uniform_brier_score", "top_pick_probability", "winner_model_rank", "field_size", "probability_gap_top1_top2"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["top_pick_won"] = frame["top_pick_won"].astype(str).str.lower().eq("true")
    frame["brier_excess_vs_uniform"] = frame["brier_score"] - frame["uniform_brier_score"]
    frame["going"] = frame.get("going", pd.Series(index=frame.index, dtype=str)).fillna("N/A").replace("", "N/A")
    frame["racecourse"] = frame.get("racecourse", pd.Series(index=frame.index, dtype=str)).fillna("N/A").replace("", "N/A")
    frame["distance_m"] = pd.to_numeric(frame.get("distance_m", pd.Series(index=frame.index)), errors="coerce")
    frame["top_confidence_band"] = pd.cut(
        frame["top_pick_probability"],
        bins=[-np.inf, 0.10, 0.15, 0.20, np.inf],
        labels=["<10%", "10–<15%", "15–<20%", "≥20%"],
        right=False,
    ).astype(str)
    frame["gap_band"] = pd.cut(
        frame["probability_gap_top1_top2"],
        bins=[-np.inf, 0.01, 0.04, 0.08, np.inf],
        labels=["<1 個百分點", "1–<4 個百分點", "4–<8 個百分點", "≥8 個百分點"],
        right=False,
    ).astype(str)
    frame["winner_rank_band"] = pd.cut(
        frame["winner_model_rank"],
        bins=[0, 1, 3, 6, np.inf],
        labels=["1", "2–3", "4–6", "7+"],
        right=True,
    ).astype(str)
    return frame


def summary_by(frame: pd.DataFrame, dimension: str, exploratory_n: int) -> pd.DataFrame:
    result = frame.groupby(dimension, dropna=False).agg(
        races=("brier_score", "count"),
        mean_brier=("brier_score", "mean"),
        mean_uniform_brier=("uniform_brier_score", "mean"),
        mean_brier_excess=("brier_excess_vs_uniform", "mean"),
        top1_win_rate=("top_pick_won", "mean"),
        mean_top_probability=("top_pick_probability", "mean"),
        high_brier_rate=("high_brier", "mean") if "high_brier" in frame.columns else ("brier_score", lambda series: np.nan),
    ).reset_index().rename(columns={dimension: "group"})
    result["exploratory"] = result["races"] < exploratory_n
    return result.sort_values(["races", "group"], ascending=[False, True]).reset_index(drop=True)


def annotate_bars(ax: plt.Axes, bars: Any, counts: list[int], exploratory: list[bool], decimals: int = 3) -> None:
    ymax = ax.get_ylim()[1]
    offset = ymax * 0.02
    for bar, count, exploratory in zip(bars, counts, exploratory):
        suffix = "*" if exploratory else ""
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + offset, f"n={count}{suffix}", ha="center", va="bottom", fontsize=9)


def going_profile(frame: pd.DataFrame, output: Path, exploratory_n: int) -> tuple[pd.DataFrame, str]:
    table = summary_by(frame, "going", exploratory_n)
    table = table.sort_values("mean_brier", ascending=False)
    fig, ax = plt.subplots(figsize=(9.2, 5.5))
    colors = ["#C96D3D" if value > 0 else "#3A7D6A" for value in table["mean_brier_excess"]]
    bars = ax.bar(table["group"], table["mean_brier"], color=colors)
    ax.axhline(table["mean_uniform_brier"].mean(), color="#4D5D8F", linestyle="--", label="同組均勻基準平均")
    ax.set_title("場地狀況與場內 Brier Score")
    ax.set_xlabel("官方場地狀況")
    ax.set_ylabel("平均 Brier Score（越低越好）")
    annotate_bars(ax, bars, table["races"].tolist(), table["exploratory"].tolist())
    ax.legend(frameon=False)
    ax.grid(axis="y", alpha=0.18)
    fig.text(0.01, 0.01, "* n<15：探索性樣本，不作模型調參結論", fontsize=8, color="#555555")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    name = "going_brier_profile.png"
    fig.savefig(output / name, dpi=180)
    plt.close(fig)
    return table, name


def course_going_heatmap(frame: pd.DataFrame, output: Path, exploratory_n: int) -> tuple[pd.DataFrame, str]:
    mean = frame.pivot_table(index="racecourse", columns="going", values="brier_score", aggfunc="mean")
    count = frame.pivot_table(index="racecourse", columns="going", values="brier_score", aggfunc="count").fillna(0).astype(int)
    columns = sorted(mean.columns.tolist())
    mean = mean.reindex(columns=columns)
    count = count.reindex(columns=columns)
    fig, ax = plt.subplots(figsize=(max(7.5, 1.55 * len(columns) + 2), 4.8))
    masked = np.ma.masked_invalid(mean.to_numpy())
    image = ax.imshow(masked, cmap="YlOrRd", aspect="auto", vmin=np.nanmin(masked), vmax=np.nanmax(masked))
    ax.set_xticks(np.arange(len(columns)), columns, rotation=18, ha="right")
    ax.set_yticks(np.arange(len(mean.index)), mean.index)
    for row in range(len(mean.index)):
        for col in range(len(columns)):
            value = mean.iloc[row, col]
            n = count.iloc[row, col]
            if pd.notna(value):
                suffix = "*" if n < exploratory_n else ""
                ax.text(col, row, f"{value:.3f}\nn={n}{suffix}", ha="center", va="center", fontsize=9)
            else:
                ax.text(col, row, "N/A", ha="center", va="center", fontsize=9, color="#555555")
    colorbar = fig.colorbar(image, ax=ax)
    colorbar.set_label("平均 Brier Score（越低越好）")
    ax.set_title("馬場 × 場地狀況：Brier 關聯熱圖")
    fig.text(0.01, 0.01, "* n<15：探索性樣本；N/A：此窗口沒有對應已評估場次", fontsize=8, color="#555555")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    name = "course_going_brier_heatmap.png"
    fig.savefig(output / name, dpi=180)
    plt.close(fig)
    table = mean.stack(future_stack=True).rename("mean_brier").reset_index().merge(count.stack(future_stack=True).rename("races").reset_index(), on=["racecourse", "going"])
    table["exploratory"] = table["races"] < exploratory_n
    return table, name


def dual_metric_profile(table: pd.DataFrame, title: str, xlabel: str, output: Path, filename: str, exploratory_n: int) -> str:
    ordered = table.copy()
    fig, ax1 = plt.subplots(figsize=(9.2, 5.5))
    x = np.arange(len(ordered))
    bars = ax1.bar(x - 0.18, ordered["mean_brier"], width=0.36, color="#3E6D9C", label="平均 Brier")
    ax1.set_ylabel("平均 Brier Score（越低越好）")
    ax1.set_xticks(x, ordered["group"], rotation=12, ha="right")
    ax1.set_xlabel(xlabel)
    ax2 = ax1.twinx()
    ax2.bar(x + 0.18, ordered["top1_win_rate"] * 100, width=0.36, color="#D38B32", label="Top-1 勝出率")
    ax2.set_ylabel("Top-1 勝出率（%）")
    ax1.set_title(title)
    annotate_bars(ax1, bars, ordered["races"].tolist(), ordered["exploratory"].tolist())
    handles1, labels1 = ax1.get_legend_handles_labels()
    handles2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(handles1 + handles2, labels1 + labels2, frameon=False, loc="upper left")
    ax1.grid(axis="y", alpha=0.18)
    fig.text(0.01, 0.01, f"* n<{exploratory_n}：探索性樣本，不作模型調參結論", fontsize=8, color="#555555")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(output / filename, dpi=180)
    plt.close(fig)
    return filename


def field_scatter(frame: pd.DataFrame, output: Path) -> str:
    fig, ax = plt.subplots(figsize=(8.7, 5.5))
    colors = np.where(frame["top_pick_won"], "#1D7B61", "#C95545")
    sizes = 35 + frame["top_pick_probability"].fillna(0).to_numpy() * 430
    ax.scatter(frame["field_size"], frame["brier_excess_vs_uniform"], color=colors, s=sizes, alpha=0.78, edgecolors="white", linewidths=0.5)
    ax.axhline(0, color="#4D5D8F", linestyle="--", linewidth=1, label="均勻基準（0）")
    valid = frame.dropna(subset=["field_size", "brier_excess_vs_uniform"])
    pearson = valid["field_size"].corr(valid["brier_excess_vs_uniform"], method="pearson")
    spearman = valid["field_size"].corr(valid["brier_excess_vs_uniform"], method="spearman")
    ax.set_title("出賽馬數量與相對均勻基準 Brier")
    ax.set_xlabel("出賽馬數量")
    ax.set_ylabel("Brier − 同場均勻基準（<0 較佳）")
    ax.text(0.99, 0.03, f"Pearson r={pearson:.3f}\nSpearman ρ={spearman:.3f}", transform=ax.transAxes, ha="right", va="bottom", fontsize=9, bbox={"facecolor": "white", "edgecolor": "#BBBBBB", "alpha": 0.9})
    ax.legend(["Top-1 勝出", "Top-1 落敗", "均勻基準（0）"], frameon=False, loc="upper left")
    ax.grid(alpha=0.18)
    fig.tight_layout()
    name = "field_size_relative_brier_scatter.png"
    fig.savefig(output / name, dpi=180)
    plt.close(fig)
    return name


def distance_profile(frame: pd.DataFrame, output: Path, exploratory_n: int) -> tuple[pd.DataFrame, str]:
    work = frame.copy()
    work["distance_label"] = work["distance_m"].map(lambda value: f"{int(value)}m" if pd.notna(value) else "N/A")
    table = summary_by(work, "distance_label", exploratory_n)
    numeric_sort = {f"{int(value)}m": value for value in work["distance_m"].dropna().unique()}
    table["sort"] = table["group"].map(numeric_sort).fillna(99999)
    table = table.sort_values("sort").drop(columns="sort")
    name = dual_metric_profile(table, "路程與近期模型誤差", "路程", output, "distance_brier_top1_profile.png", exploratory_n)
    return table, name


def markdown_table(table: pd.DataFrame) -> list[str]:
    display = table.copy()
    for column in ("mean_brier", "mean_uniform_brier", "mean_brier_excess", "mean_top_probability", "high_brier_rate"):
        if column in display.columns:
            display[column] = display[column].map(lambda value: f"{value:.4f}" if pd.notna(value) else "N/A")
    if "top1_win_rate" in display.columns:
        display["top1_win_rate"] = display["top1_win_rate"].map(lambda value: f"{100 * value:.2f}%" if pd.notna(value) else "N/A")
    if "exploratory" in display.columns:
        display["exploratory"] = display["exploratory"].map(lambda value: "是" if value else "否")
    columns = list(display.columns)
    lines = ["| " + " | ".join(columns) + " |", "|" + "|".join("---" for _ in columns) + "|"]
    for _, row in display.iterrows():
        lines.append("| " + " | ".join(str(row[column]).replace("|", "\\|") for column in columns) + " |")
    return lines


def main() -> int:
    args = parse_args()
    if args.exploratory_n < 2:
        raise SystemExit("--exploratory-n 必須至少為 2。")
    configure_font()
    try:
        frame = load_cases(Path(args.input))
    except ValueError as exc:
        print(json.dumps({"status": "input_error", "error": str(exc)}, ensure_ascii=False))
        return 2
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)

    going, going_chart = going_profile(frame, output, args.exploratory_n)
    heatmap, heatmap_chart = course_going_heatmap(frame, output, args.exploratory_n)
    confidence = summary_by(frame, "top_confidence_band", args.exploratory_n)
    confidence["sort"] = confidence["group"].map({"<10%": 1, "10–<15%": 2, "15–<20%": 3, "≥20%": 4})
    confidence = confidence.sort_values("sort").drop(columns="sort")
    confidence_chart = dual_metric_profile(confidence, "首選信心與近期模型誤差", "首選預測機率分組", output, "top_confidence_brier_top1_profile.png", args.exploratory_n)
    rank = summary_by(frame, "winner_rank_band", args.exploratory_n)
    rank["sort"] = rank["group"].map({"1": 1, "2–3": 2, "4–6": 3, "7+": 4})
    rank = rank.sort_values("sort").drop(columns="sort")
    rank_chart = dual_metric_profile(rank, "頭馬模型排名與近期模型誤差", "真正頭馬的模型排名", output, "winner_rank_brier_top1_profile.png", args.exploratory_n)
    gap = summary_by(frame, "gap_band", args.exploratory_n)
    gap["sort"] = gap["group"].map({"<1 個百分點": 1, "1–<4 個百分點": 2, "4–<8 個百分點": 3, "≥8 個百分點": 4})
    gap = gap.sort_values("sort").drop(columns="sort")
    gap_chart = dual_metric_profile(gap, "首二機率分離度與近期模型誤差", "首選與第二選機率差距", output, "top2_gap_brier_top1_profile.png", args.exploratory_n)
    distance, distance_chart = distance_profile(frame, output, args.exploratory_n)
    field_chart = field_scatter(frame, output)

    tables = {
        "場地狀況": going,
        "首選信心": confidence,
        "頭馬模型排名": rank,
        "首二分離度": gap,
        "路程": distance,
        "馬場×場地狀況": heatmap,
    }
    combined = []
    for title, table in tables.items():
        copy = table.copy()
        copy.insert(0, "dimension", title)
        combined.append(copy)
    pd.concat(combined, ignore_index=True, sort=False).to_csv(output / "dimension_summary.csv", index=False, encoding="utf-8-sig")
    correlations = {
        "field_size_vs_relative_brier_pearson": float(frame["field_size"].corr(frame["brier_excess_vs_uniform"], method="pearson")),
        "field_size_vs_relative_brier_spearman": float(frame["field_size"].corr(frame["brier_excess_vs_uniform"], method="spearman")),
        "top_probability_vs_brier_pearson": float(frame["top_pick_probability"].corr(frame["brier_score"], method="pearson")),
        "top2_gap_vs_brier_pearson": float(frame["probability_gap_top1_top2"].corr(frame["brier_score"], method="pearson")),
    }
    payload = {
        "status": "ok",
        "input": args.input,
        "evaluated_races": int(len(frame)),
        "exploratory_n": args.exploratory_n,
        "correlations": correlations,
        "charts": [going_chart, heatmap_chart, confidence_chart, rank_chart, gap_chart, distance_chart, field_chart],
    }
    (output / "dimension_summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# V10.2 近期錯誤分析：多維圖表摘要",
        "",
        "> **樣本限制：** 全部圖表僅使用 50 場已評估、已保存的預測工件。任何 n<15 的條件組別均標記為探索性；圖表描述關聯，不代表因果或調參依據。",
        "",
        f"**輸入：** `{args.input}`；**已評估賽事：** {len(frame)}；**探索性門檻：** n<{args.exploratory_n}。",
        "",
        "## 圖表清單",
        "",
    ]
    for chart in payload["charts"]:
        lines.extend([f"![{chart}]({chart})", ""])
    lines.extend(["## 分層摘要", ""])
    for title, table in tables.items():
        lines.extend([f"### {title}", ""])
        lines.extend(markdown_table(table))
        lines.append("")
    lines.extend([
        "## 線性／單調相關摘要",
        "",
        f"- 出賽馬數量與相對均勻基準 Brier：Pearson r = {correlations['field_size_vs_relative_brier_pearson']:.3f}；Spearman ρ = {correlations['field_size_vs_relative_brier_spearman']:.3f}。",
        f"- 首選機率與場內 Brier：Pearson r = {correlations['top_probability_vs_brier_pearson']:.3f}。",
        f"- 首二機率差距與場內 Brier：Pearson r = {correlations['top2_gap_vs_brier_pearson']:.3f}。",
        "",
        "相關係數在此只作描述；它們不控制班次、場地、路程、賽日或特徵交互作用，因此不可解讀為模型因果弱點。",
    ])
    (output / "dimension_visualization_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
