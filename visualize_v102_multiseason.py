#!/usr/bin/env python3
"""Create V10.2 multi-season racing-data and out-of-time prediction visualizations."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


CJK_FONT = "Noto Sans CJK TC"
COLORS = {
    "blue": "#1F4E79",
    "teal": "#0F766E",
    "orange": "#D97706",
    "red": "#B91C1C",
    "gray": "#64748B",
    "light": "#E2E8F0",
    "green": "#15803D",
}
FEATURE_LABELS = {
    "recent_finish_fraction_pre": "近績名次比例",
    "jockey_elo_vs_field": "騎師 ELO 相對場內",
    "draw_pct": "檔位百分位",
    "jockey_elo_pre": "騎師 ELO",
    "horse_condition_elo_pre": "同條件馬匹 ELO",
    "closing400_trend_pre": "末段走勢代理趨勢",
    "recent_margin_pre": "近績馬位差",
    "horse_body_weight_pre": "馬匹體重",
    "body_weight_delta_pre": "體重變幅",
    "trainer_win_rate_pre": "練馬師勝率",
    "horse_win_rate_pre": "馬匹勝率",
    "elo_vs_field": "馬匹 ELO 相對場內",
    "condition_win_rate_pre": "同條件勝率",
    "closing400_proxy_pre": "末段走勢代理",
    "draw": "檔位",
    "track_bias_sample_pre": "跑道偏差樣本",
    "weight_lbs": "負磅",
    "horse_elo_pre": "馬匹 ELO",
    "horse_top3_rate_pre": "馬匹前三率",
    "track_bias_pre": "跑道偏差",
    "racecourse": "馬場",
    "weight_delta": "負磅變化",
}


def season_expr() -> str:
    return "CASE WHEN CAST(strftime('%m', race_date) AS INTEGER) >= 8 THEN strftime('%Y', race_date) || '/' || substr(CAST(CAST(strftime('%Y', race_date) AS INTEGER)+1 AS TEXT),3,2) ELSE CAST(CAST(strftime('%Y', race_date) AS INTEGER)-1 AS TEXT) || '/' || substr(strftime('%Y', race_date),3,2) END"


def decorate_axes(ax: plt.Axes) -> None:
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", color=COLORS["light"], linewidth=0.8)
    ax.set_axisbelow(True)


def annotate_bars(ax: plt.Axes, bars, fmt: str = "{:.0f}") -> None:
    for bar in bars:
        height = bar.get_height()
        ax.annotate(fmt.format(height), (bar.get_x() + bar.get_width() / 2, height), xytext=(0, 4),
                    textcoords="offset points", ha="center", va="bottom", fontsize=9)


def plot_season_coverage(season: pd.DataFrame, out: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(14, 5.8), constrained_layout=True)
    labels = season["season"].tolist()
    x = np.arange(len(labels))
    bars = axes[0].bar(x, season["completed_races"], color=COLORS["blue"], label="已完成場次")
    axes[0].bar(x, season["cancelled_or_void"], bottom=season["completed_races"], color=COLORS["orange"], label="取消／無效")
    axes[0].set_title("三馬季官方場次覆蓋")
    axes[0].set_xticks(x, labels)
    axes[0].set_ylabel("場次")
    annotate_bars(axes[0], bars)
    axes[0].legend(frameon=False)
    decorate_axes(axes[0])

    bars = axes[1].bar(x, season["starters"], color=COLORS["teal"])
    axes[1].set_title("三馬季馬匹出賽紀錄")
    axes[1].set_xticks(x, labels)
    axes[1].set_ylabel("逐馬出賽紀錄")
    annotate_bars(axes[1], bars, "{:,.0f}")
    decorate_axes(axes[1])
    fig.suptitle("V10.2 跨馬季官方賽果資料庫", fontsize=16, fontweight="bold")
    fig.savefig(out / "01_season_coverage.png", dpi=180, bbox_inches="tight")
    plt.close(fig)


def plot_race_profile(monthly: pd.DataFrame, fields: pd.DataFrame, out: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(14, 5.8), constrained_layout=True)
    pivot = monthly.pivot(index="month", columns="racecourse", values="completed_races").fillna(0)
    pivot = pivot.sort_index()
    pivot.plot(kind="bar", stacked=True, ax=axes[0], color={"ST": COLORS["blue"], "HV": COLORS["teal"]}, width=0.82)
    axes[0].set_title("按月份及馬場的已完成場次")
    axes[0].set_xlabel("月份")
    axes[0].set_ylabel("場次")
    axes[0].tick_params(axis="x", rotation=60, labelsize=8)
    axes[0].legend(title="馬場", frameon=False)
    decorate_axes(axes[0])

    axes[1].bar(fields["field_size"].astype(str), fields["race_count"], color=COLORS["orange"])
    axes[1].set_title("完成賽出賽馬數分布")
    axes[1].set_xlabel("出賽馬數")
    axes[1].set_ylabel("場次")
    decorate_axes(axes[1])
    fig.suptitle("賽程結構與場內規模", fontsize=16, fontweight="bold")
    fig.savefig(out / "02_race_profile.png", dpi=180, bbox_inches="tight")
    plt.close(fig)


def plot_model_performance(report: dict, predictions: pd.DataFrame, out: Path) -> dict:
    test = report["test_race_metrics"]
    group_sizes = predictions.groupby("race_group").size()
    uniform_top_pick = float((1 / group_sizes).mean())
    ensemble_brier = float(test["mean_race_brier_score"])
    uniform_brier = float(test["mean_uniform_race_brier_score"])
    lgb_brier = float(report["ensemble_weights"]["validation_race_brier"]["lightgbm"])
    cat_brier = float(report["ensemble_weights"]["validation_race_brier"]["catboost"])

    fig, axes = plt.subplots(1, 2, figsize=(14, 5.8), constrained_layout=True)
    bars = axes[0].bar(["首選勝出率", "等機會基準"], [test["top_pick_win_rate"], uniform_top_pick], color=[COLORS["blue"], COLORS["gray"]])
    axes[0].set_ylim(0, max(test["top_pick_win_rate"], uniform_top_pick) * 1.35)
    axes[0].set_title("首選命中率：V10.2 對等機會基準")
    axes[0].set_ylabel("勝出率")
    axes[0].yaxis.set_major_formatter(lambda value, _: f"{value:.0%}")
    annotate_bars(axes[0], bars, "{:.1%}")
    decorate_axes(axes[0])

    labels = ["LightGBM\n驗證", "CatBoost\n驗證", "集成\n測試", "等機會\n測試"]
    values = [lgb_brier, cat_brier, ensemble_brier, uniform_brier]
    bars = axes[1].bar(labels, values, color=[COLORS["blue"], COLORS["teal"], COLORS["green"], COLORS["gray"]])
    axes[1].set_title("場內 Brier score（越低越佳）")
    axes[1].set_ylabel("Brier score")
    axes[1].set_ylim(min(values) - 0.015, max(values) + 0.012)
    annotate_bars(axes[1], bars, "{:.3f}")
    decorate_axes(axes[1])
    fig.suptitle("V10.2 集成模型時間外表現", fontsize=16, fontweight="bold")
    fig.savefig(out / "03_model_performance.png", dpi=180, bbox_inches="tight")
    plt.close(fig)
    return {"uniform_top_pick_rate": uniform_top_pick, "ensemble_brier": ensemble_brier, "uniform_brier": uniform_brier}


def calibration_table(predictions: pd.DataFrame, column: str) -> pd.DataFrame:
    frame = predictions[[column, "target_win"]].dropna().copy()
    frame["bin"] = pd.qcut(frame[column], q=10, duplicates="drop")
    table = frame.groupby("bin", observed=True).agg(predicted=(column, "mean"), observed=("target_win", "mean"), count=("target_win", "size")).reset_index(drop=True)
    return table


def plot_calibration(predictions: pd.DataFrame, out: Path) -> None:
    fig, ax = plt.subplots(figsize=(7.5, 6.2), constrained_layout=True)
    configs = [
        ("lightgbm_calibrated_probability", "LightGBM", COLORS["blue"]),
        ("catboost_calibrated_probability", "CatBoost", COLORS["teal"]),
        ("race_normalized_probability", "集成（場內正規化）", COLORS["green"]),
    ]
    for column, label, color in configs:
        table = calibration_table(predictions, column)
        ax.plot(table["predicted"], table["observed"], marker="o", linewidth=2, label=label, color=color)
    ax.plot([0, 0.35], [0, 0.35], linestyle="--", color=COLORS["gray"], label="完全校準")
    ax.set_xlim(0, 0.35)
    ax.set_ylim(0, 0.35)
    ax.set_xlabel("平均預測勝率")
    ax.set_ylabel("實際勝出率")
    ax.set_title("時間外測試：機率校準曲線（逐馬列）")
    ax.legend(frameon=False, fontsize=9)
    decorate_axes(ax)
    fig.savefig(out / "04_calibration.png", dpi=180, bbox_inches="tight")
    plt.close(fig)


def plot_rank_and_features(report: dict, predictions: pd.DataFrame, out: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(14, 6.2), constrained_layout=True)
    winners = predictions[predictions["target_win"] == 1]
    rank_stats = winners.groupby("model_rank").size().reindex(range(1, int(winners["model_rank"].max()) + 1), fill_value=0)
    axes[0].bar(rank_stats.index.astype(str), rank_stats.values, color=[COLORS["green"] if rank == 1 else COLORS["blue"] for rank in rank_stats.index])
    axes[0].set_title("頭馬在模型排名的位置（時間外測試）")
    axes[0].set_xlabel("模型排名")
    axes[0].set_ylabel("頭馬數")
    decorate_axes(axes[0])

    features = report["top_lightgbm_feature_importance"][:12]
    labels = [FEATURE_LABELS.get(row["feature"].replace("num__", ""), row["feature"].replace("num__", "")) for row in features][::-1]
    values = [row["importance"] for row in features][::-1]
    axes[1].barh(labels, values, color=COLORS["orange"])
    axes[1].set_title("LightGBM 前 12 項特徵重要度")
    axes[1].set_xlabel("重要度（分裂次數）")
    decorate_axes(axes[1])
    fig.suptitle("排序命中與可解釋特徵", fontsize=16, fontweight="bold")
    fig.savefig(out / "05_rank_and_features.png", dpi=180, bbox_inches="tight")
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description="Render V10.2 multi-season visualization set")
    parser.add_argument("--db", default="hkjc_last_season.sqlite")
    parser.add_argument("--training-report", default="v102_multiseason_training_report.json")
    parser.add_argument("--predictions", default="v102_multiseason_backtest_predictions.csv")
    parser.add_argument("--output-dir", default="v102_visuals")
    args = parser.parse_args()

    plt.rcParams.update({"font.family": CJK_FONT, "axes.unicode_minus": False, "figure.facecolor": "white", "axes.facecolor": "white"})
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(args.db)
    s_expr = season_expr()
    season = pd.read_sql_query(
        f"""
        SELECT {s_expr} AS season,
               COUNT(DISTINCT r.race_date || '|' || r.racecourse) AS meetings,
               COUNT(*) AS races,
               SUM(CASE WHEN r.race_status='completed' THEN 1 ELSE 0 END) AS completed_races,
               SUM(CASE WHEN r.race_status IN ('cancelled','void') THEN 1 ELSE 0 END) AS cancelled_or_void,
               (SELECT COUNT(*) FROM starters s WHERE s.race_date BETWEEN MIN(r.race_date) AND MAX(r.race_date)) AS starters
        FROM races r
        GROUP BY season
        ORDER BY MIN(r.race_date)
        """,
        conn,
    )
    # The correlated aggregate above may include off-season rows at date edges; use exact per-season calculation.
    season["starters"] = [
        pd.read_sql_query(
            f"SELECT COUNT(*) AS n FROM starters WHERE {s_expr.replace('race_date', 'starters.race_date')}=?", conn, params=(label,)
        ).iloc[0, 0]
        for label in season["season"]
    ]
    monthly = pd.read_sql_query(
        """
        SELECT substr(race_date,1,7) AS month, racecourse, COUNT(*) AS completed_races
        FROM races WHERE race_status='completed'
        GROUP BY month,racecourse ORDER BY month,racecourse
        """,
        conn,
    )
    fields = pd.read_sql_query(
        """
        SELECT field_size, COUNT(*) AS race_count FROM (
          SELECT s.race_date,s.racecourse,s.race_no,COUNT(*) AS field_size
          FROM starters s JOIN races r USING(race_date,racecourse,race_no)
          WHERE r.race_status='completed'
          GROUP BY s.race_date,s.racecourse,s.race_no
        ) GROUP BY field_size ORDER BY field_size
        """,
        conn,
    )
    conn.close()

    report = json.loads(Path(args.training_report).read_text(encoding="utf-8"))
    predictions = pd.read_csv(args.predictions, encoding="utf-8-sig")
    plot_season_coverage(season, out)
    plot_race_profile(monthly, fields, out)
    performance = plot_model_performance(report, predictions, out)
    plot_calibration(predictions, out)
    plot_rank_and_features(report, predictions, out)

    season.to_csv(out / "season_summary.csv", index=False, encoding="utf-8-sig")
    summary = {
        "season_coverage": season.to_dict(orient="records"),
        "model_performance": performance,
        "test_metrics": report["test_race_metrics"],
        "chart_files": [path.name for path in sorted(out.glob("*.png"))],
    }
    (out / "visualization_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
