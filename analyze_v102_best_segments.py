#!/usr/bin/env python3
"""Compare odds structure and local CatBoost feature contributions for V10.2 best segments."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import joblib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from catboost import Pool

FONT = "Noto Sans CJK TC"
SEGMENTS = {
    "ST_快地": {"racecourse": "ST", "going_group": "快地", "label": "沙田・快地"},
    "HV_好地": {"racecourse": "HV", "going_group": "好地", "label": "跑馬地・好地"},
}
FEATURE_LABELS = {
    "recent_finish_fraction_pre": "近績名次比例", "jockey_elo_vs_field": "騎師 ELO 相對場內",
    "horse_condition_elo_pre": "同條件馬匹 ELO", "elo_vs_field": "馬匹 ELO 相對場內",
    "closing400_trend_pre": "末段走勢代理趨勢", "jockey_elo_pre": "騎師 ELO",
    "horse_elo_pre": "馬匹 ELO", "draw_pct": "檔位百分位", "draw": "檔位",
    "horse_win_rate_pre": "馬匹勝率", "horse_body_weight_pre": "馬匹體重",
    "body_weight_delta_pre": "體重變幅", "trainer_win_rate_pre": "練馬師勝率",
    "recent_margin_pre": "近績馬位差", "closing400_proxy_pre": "末段走勢代理",
    "condition_win_rate_pre": "同條件勝率", "weight_delta": "負磅變化",
    "track_bias_sample_pre": "跑道偏差樣本", "track_bias_pre": "跑道偏差",
    "weight_lbs": "負磅", "racecourse": "馬場", "going": "場地狀況",
    "surface": "跑道表面", "course_config": "跑道配置", "race_class": "班次",
}


def going_group(going: object) -> str:
    text = "" if pd.isna(going) else str(going).strip()
    if "快" in text:
        return "快地"
    if text == "好地" or text == "好":
        return "好地"
    if "黏" in text or "軟" in text or "濕" in text:
        return "黏／軟地"
    return text or "未標示"


def extract_odds_column(conn: sqlite3.Connection) -> str:
    cols = pd.read_sql_query("PRAGMA table_info(starters)", conn)["name"].tolist()
    candidates = ["win_odds", "odds", "starting_odds", "winodds"]
    for candidate in candidates:
        if candidate in cols:
            return candidate
    raise RuntimeError(f"No historical win-odds field in starters table. Found: {cols}")


def odds_bucket(value: float) -> str:
    if pd.isna(value) or value <= 0:
        return "缺失／無效"
    if value < 3.5:
        return "熱門（<3.5）"
    if value < 10:
        return "中賠（3.5–<10）"
    return "長途（≥10）"


def weighted_shap(bundle: dict, features: pd.DataFrame) -> pd.DataFrame:
    all_features = bundle["all_features"]
    cats = bundle["categorical_features"]
    nums = bundle["numeric_features"]
    X = features[all_features].copy()
    for col in nums:
        X[col] = pd.to_numeric(X[col], errors="coerce").fillna(0.0)
    for col in cats:
        X[col] = X[col].fillna("未知").astype(str)
    pool = Pool(X, cat_features=bundle["catboost_categorical_indices"])
    shap = np.asarray(bundle["catboost_model"].get_feature_importance(pool, type="ShapValues"), dtype=float)[:, :-1]
    return pd.DataFrame(np.abs(shap), columns=all_features, index=features.index)


def calc_odds_metrics(frame: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    valid = frame[frame["win_odds"].notna() & (frame["win_odds"] > 0)].copy()
    valid["odds_bucket"] = valid["win_odds"].map(odds_bucket)
    race_metrics = []
    for race, group in valid.groupby("race_group", sort=False):
        market_min = group["win_odds"].min()
        market_fav = group[np.isclose(group["win_odds"], market_min)]
        model_top = group[group["model_rank"] == 1]
        race_metrics.append({
            "race_group": race,
            "market_favorite_win": int(market_fav["target_win"].sum() > 0),
            "model_top_win": int(model_top["target_win"].sum() > 0),
            "model_market_agree": int(np.isclose(model_top["win_odds"].iloc[0], market_min)) if len(model_top) else 0,
            "model_top_odds": float(model_top["win_odds"].iloc[0]) if len(model_top) else np.nan,
            "winner_odds": float(group.loc[group["target_win"] == 1, "win_odds"].iloc[0]) if (group["target_win"] == 1).any() else np.nan,
        })
    race_stats = pd.DataFrame(race_metrics)
    bucket = valid.groupby("odds_bucket", observed=True).agg(
        runners=("horse_name", "size"), wins=("target_win", "sum"), avg_model_probability=("race_normalized_probability", "mean"), median_odds=("win_odds", "median")
    ).reset_index()
    bucket["winner_rate"] = bucket["wins"] / bucket["runners"]
    result = {
        "races_with_valid_odds": int(race_stats["race_group"].nunique()),
        "runners_with_valid_odds": int(len(valid)),
        "median_runner_odds": float(valid["win_odds"].median()),
        "mean_runner_odds": float(valid["win_odds"].mean()),
        "median_model_top_odds": float(race_stats["model_top_odds"].median()),
        "median_winner_odds": float(race_stats["winner_odds"].median()),
        "model_top_win_rate": float(race_stats["model_top_win"].mean()),
        "market_favorite_win_rate": float(race_stats["market_favorite_win"].mean()),
        "model_market_agreement_rate": float(race_stats["model_market_agree"].mean()),
    }
    return result, bucket


def plot_odds_structure(buckets: dict[str, pd.DataFrame], segment_stats: pd.DataFrame, out: Path) -> None:
    order = ["熱門（<3.5）", "中賠（3.5–<10）", "長途（≥10）"]
    fig, axes = plt.subplots(1, 2, figsize=(14, 5.8), constrained_layout=True)
    x = np.arange(len(order)); width = 0.35
    for i, (seg, color) in enumerate(zip(SEGMENTS, ["#1F4E79", "#0F766E"])):
        table = buckets[seg].set_index("odds_bucket").reindex(order).fillna(0)
        axes[0].bar(x + (i - 0.5) * width, table["runners"], width, label=SEGMENTS[seg]["label"], color=color)
        axes[1].bar(x + (i - 0.5) * width, table["winner_rate"], width, label=SEGMENTS[seg]["label"], color=color)
    axes[0].set_title("歷史獨贏賠率結構（逐馬列）")
    axes[0].set_xticks(x, order); axes[0].set_ylabel("馬匹列數"); axes[0].legend(frameon=False)
    axes[1].set_title("各賠率桶實際勝出率")
    axes[1].set_xticks(x, order); axes[1].set_ylabel("勝出率")
    axes[1].yaxis.set_major_formatter(lambda value, _: f"{value:.0%}")
    axes[1].legend(frameon=False)
    for ax in axes:
        ax.spines[["top", "right"]].set_visible(False); ax.grid(axis="y", color="#E2E8F0"); ax.set_axisbelow(True)
    fig.suptitle("最佳交叉組合：歷史獨贏賠率結構", fontsize=16, fontweight="bold")
    fig.savefig(out / "01_odds_structure.png", dpi=180, bbox_inches="tight"); plt.close(fig)

    fig, ax = plt.subplots(figsize=(8.5, 5.5), constrained_layout=True)
    stats = segment_stats.set_index("segment")
    labels = [SEGMENTS[key]["label"] for key in SEGMENTS]
    x = np.arange(len(labels)); width = 0.28
    metrics = [("model_top_win_rate", "模型首選勝出", "#15803D"), ("market_favorite_win_rate", "市場熱門勝出", "#D97706"), ("model_market_agreement_rate", "模型／市場首選一致", "#64748B")]
    for i, (col, label, color) in enumerate(metrics):
        vals = [stats.loc[key, col] for key in SEGMENTS]
        bars = ax.bar(x + (i - 1) * width, vals, width, label=label, color=color)
        for bar in bars:
            ax.annotate(f"{bar.get_height():.1%}", (bar.get_x()+bar.get_width()/2, bar.get_height()), xytext=(0,4), textcoords="offset points", ha="center", fontsize=9)
    ax.set_xticks(x, labels); ax.set_ylim(0, max(segment_stats[[x[0] for x in metrics]].max())*1.35)
    ax.set_ylabel("比率"); ax.yaxis.set_major_formatter(lambda value, _: f"{value:.0%}")
    ax.set_title("模型首選與歷史市場熱門的關係")
    ax.legend(frameon=False); ax.spines[["top", "right"]].set_visible(False); ax.grid(axis="y", color="#E2E8F0"); ax.set_axisbelow(True)
    fig.savefig(out / "02_model_market_relation.png", dpi=180, bbox_inches="tight"); plt.close(fig)


def plot_feature_difference(shap_tables: dict[str, pd.DataFrame], out: Path) -> pd.DataFrame:
    shares = {}
    for key, table in shap_tables.items():
        means = table.mean(axis=0)
        shares[key] = means / means.sum()
    df = pd.DataFrame(shares)
    diff = (df["ST_快地"] - df["HV_好地"]).sort_values()
    selected = pd.concat([diff.head(8), diff.tail(8)]).drop_duplicates()
    labels = [FEATURE_LABELS.get(name, name) for name in selected.index]
    colors = ["#1F4E79" if value > 0 else "#0F766E" for value in selected.values]
    fig, ax = plt.subplots(figsize=(9.8, 7.4), constrained_layout=True)
    ax.barh(labels, selected.values * 100, color=colors)
    ax.axvline(0, color="#334155", linewidth=1)
    ax.set_xlabel("平均絕對 SHAP 佔比差：沙田・快地 − 跑馬地・好地（百分點）")
    ax.set_title("CatBoost 局部特徵貢獻差異")
    ax.spines[["top", "right"]].set_visible(False); ax.grid(axis="x", color="#E2E8F0"); ax.set_axisbelow(True)
    fig.savefig(out / "03_local_feature_difference.png", dpi=180, bbox_inches="tight"); plt.close(fig)
    return df


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="hkjc_last_season.sqlite")
    parser.add_argument("--model", default="horse_model.pkl")
    parser.add_argument("--predictions", default="v102_multiseason_backtest_predictions.csv")
    parser.add_argument("--output-dir", default="v102_best_segment_analysis")
    args = parser.parse_args()
    plt.rcParams.update({"font.family": FONT, "axes.unicode_minus": False})
    out = Path(args.output_dir); out.mkdir(parents=True, exist_ok=True)

    preds = pd.read_csv(args.predictions, encoding="utf-8-sig")
    conn = sqlite3.connect(args.db)
    odds_col = extract_odds_column(conn)
    races = pd.read_sql_query("SELECT race_date,racecourse,race_no,going FROM races WHERE race_status='completed'", conn)
    starters = pd.read_sql_query(f"SELECT race_date,racecourse,race_no,horse_name,{odds_col} AS win_odds FROM starters", conn)
    features = pd.read_sql_query("SELECT * FROM elo_feature_store", conn)
    conn.close()
    preds = preds.merge(races, on=["race_date", "racecourse", "race_no"], how="left", validate="many_to_one")
    preds["going_group"] = preds["going"].map(going_group)
    data = preds.merge(starters, on=["race_date", "racecourse", "race_no", "horse_name"], how="left", validate="one_to_one")
    data["win_odds"] = pd.to_numeric(data["win_odds"], errors="coerce")

    bundle = joblib.load(args.model)
    segment_stats = []
    buckets = {}; shap_tables = {}
    all_rows = []
    for key, spec in SEGMENTS.items():
        subset = data[(data["racecourse"] == spec["racecourse"]) & (data["going_group"] == spec["going_group"])].copy()
        odds_stats, bucket = calc_odds_metrics(subset)
        feature_subset = features.merge(subset[["race_date", "racecourse", "race_no", "horse_name"]], on=["race_date", "racecourse", "race_no", "horse_name"], how="inner", validate="one_to_one")
        shap_tables[key] = weighted_shap(bundle, feature_subset)
        odds_stats.update({"segment": key, "label": spec["label"], "races": int(subset["race_group"].nunique()), "runners": int(len(subset))})
        segment_stats.append(odds_stats); buckets[key] = bucket
        bucket.assign(segment=key).to_csv(out / f"{key}_odds_bucket.csv", index=False, encoding="utf-8-sig")
        all_rows.append(subset)
    stats_df = pd.DataFrame(segment_stats)
    stats_df.to_csv(out / "segment_odds_summary.csv", index=False, encoding="utf-8-sig")
    local_shares = plot_feature_difference(shap_tables, out)
    local_shares.to_csv(out / "local_catboost_shap_share.csv", encoding="utf-8-sig")
    plot_odds_structure(buckets, stats_df, out)
    pd.concat(all_rows, ignore_index=True).to_csv(out / "segment_test_predictions_with_odds.csv", index=False, encoding="utf-8-sig")
    summary = {"segments": segment_stats, "odds_field": odds_col, "local_catboost_shap_share": local_shares.to_dict(orient="index")}
    (out / "best_segment_analysis_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"segments": segment_stats, "odds_field": odds_col}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
