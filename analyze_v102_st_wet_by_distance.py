#!/usr/bin/env python3
"""Distance-level time-out performance and local feature analysis for Sha Tin wet-going races."""
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
WET_RAW = {"好地至黏地", "黏地", "軟地", "濕慢地"}
LABELS = {
    "recent_finish_fraction_pre": "近績名次比例", "horse_condition_elo_pre": "同條件馬匹 ELO",
    "jockey_elo_vs_field": "騎師 ELO 相對場內", "elo_vs_field": "馬匹 ELO 相對場內",
    "horse_elo_pre": "馬匹 ELO", "draw": "檔位", "draw_pct": "檔位百分位",
    "jockey_elo_pre": "騎師 ELO", "closing400_trend_pre": "末段走勢代理趨勢",
    "horse_win_rate_pre": "馬匹勝率", "horse_body_weight_pre": "馬匹體重",
    "body_weight_delta_pre": "體重變幅", "recent_margin_pre": "近績馬位差",
    "track_bias_pre": "跑道偏差", "track_bias_sample_pre": "跑道偏差樣本",
    "weight_lbs": "負磅", "weight_delta": "負磅變化", "condition_win_rate_pre": "同條件勝率",
    "trainer_win_rate_pre": "練馬師勝率", "racecourse": "馬場", "race_class": "班次",
    "closing400_proxy_pre": "末段走勢代理",
}


def find_distance_column(conn: sqlite3.Connection) -> str:
    columns = pd.read_sql_query("PRAGMA table_info(races)", conn)["name"].tolist()
    for col in ("distance_m", "distance", "race_distance"):
        if col in columns:
            return col
    raise RuntimeError(f"Unable to find a distance field in races. Available columns: {columns}")


def per_race_metrics(frame: pd.DataFrame) -> pd.Series:
    y = frame["target_win"].astype(float).to_numpy()
    p = frame["race_normalized_probability"].astype(float).to_numpy()
    n = len(frame)
    return pd.Series({
        "top_pick_win": int(((frame["model_rank"] == 1) & (frame["target_win"] == 1)).any()),
        "top3_contains_winner": int(((frame["model_rank"] <= 3) & (frame["target_win"] == 1)).any()),
        "brier": float(np.square(p - y).sum()),
        "uniform_brier": float(np.square((1 / n) - y).sum()),
        "uniform_top_pick_rate": 1 / n,
    })


def summarize(frame: pd.DataFrame, distance: int) -> dict:
    races = frame.groupby("race_group", sort=False).apply(per_race_metrics, include_groups=False).reset_index(drop=True)
    return {
        "distance_m": int(distance), "races": int(len(races)), "runners": int(len(frame)),
        "top_pick_win_rate": float(races["top_pick_win"].mean()),
        "top3_contains_winner_rate": float(races["top3_contains_winner"].mean()),
        "uniform_top_pick_rate": float(races["uniform_top_pick_rate"].mean()),
        "top_pick_lift": float(races["top_pick_win"].mean() / races["uniform_top_pick_rate"].mean()),
        "mean_brier": float(races["brier"].mean()),
        "uniform_brier": float(races["uniform_brier"].mean()),
        "brier_improvement": float(races["uniform_brier"].mean() - races["brier"].mean()),
        "sample_flag": "可比較" if len(races) >= 15 else "探索性（<15 場）",
    }


def shap_share(bundle: dict, frame: pd.DataFrame) -> pd.Series:
    cols = bundle["all_features"]
    X = frame[cols].copy()
    for col in bundle["numeric_features"]:
        X[col] = pd.to_numeric(X[col], errors="coerce").fillna(0.0)
    for col in bundle["categorical_features"]:
        X[col] = X[col].fillna("未知").astype(str)
    pool = Pool(X, cat_features=bundle["catboost_categorical_indices"])
    values = np.abs(bundle["catboost_model"].get_feature_importance(pool, type="ShapValues")[:, :-1])
    means = pd.Series(values.mean(axis=0), index=cols)
    return means / means.sum()


def plot_counts(summary: pd.DataFrame, out: Path) -> None:
    fig, ax = plt.subplots(figsize=(9, 5.2), constrained_layout=True)
    colours = ["#1F4E79" if n >= 15 else "#94A3B8" for n in summary["races"]]
    bars = ax.bar(summary["distance_m"].astype(str), summary["races"], color=colours)
    for bar, row in zip(bars, summary.itertuples()):
        ax.annotate(f"{row.races} 場\n{row.sample_flag}", (bar.get_x()+bar.get_width()/2,bar.get_height()), xytext=(0,4), textcoords="offset points", ha="center", fontsize=9)
    ax.set_title("沙田濕地：時間外樣本的路程分布")
    ax.set_xlabel("路程（米）"); ax.set_ylabel("場次")
    ax.spines[["top", "right"]].set_visible(False); ax.grid(axis="y", color="#E2E8F0"); ax.set_axisbelow(True)
    fig.savefig(out / "01_distance_sample_counts.png", dpi=180, bbox_inches="tight"); plt.close(fig)


def plot_performance(summary: pd.DataFrame, out: Path) -> None:
    labels=summary["distance_m"].astype(str).tolist(); x=np.arange(len(labels)); width=.28
    fig, axes = plt.subplots(1, 2, figsize=(14.5, 5.5), constrained_layout=True)
    for j, (column, baseline, title, ylabel) in enumerate([
        ("top_pick_win_rate", "uniform_top_pick_rate", "首選勝出率與等機會基準", "勝出率"),
        ("mean_brier", "uniform_brier", "Brier score 與等機會基準（越低越佳）", "Brier score"),
    ]):
        ax=axes[j]
        ax.bar(x-width/2, summary[column], width, label="V10.2", color="#15803D" if j==0 else "#2563EB")
        ax.bar(x+width/2, summary[baseline], width, label="等機會基準", color="#94A3B8")
        for i,row in summary.iterrows():
            value=row[column]
            ax.annotate(f"{value:.1%}" if j==0 else f"{value:.3f}", (i-width/2,value), xytext=(0,4), textcoords="offset points", ha="center", fontsize=9)
        ax.set_title(title); ax.set_xticks(x, [f"{label}米" for label in labels]); ax.set_ylabel(ylabel); ax.legend(frameon=False)
        if j==0: ax.yaxis.set_major_formatter(lambda value,_:f"{value:.0%}")
        ax.spines[["top","right"]].set_visible(False); ax.grid(axis="y",color="#E2E8F0"); ax.set_axisbelow(True)
    fig.savefig(out / "02_distance_performance.png", dpi=180, bbox_inches="tight"); plt.close(fig)


def plot_shap_difference(shares: pd.DataFrame, out: Path) -> pd.DataFrame:
    if not {1200,1600}.issubset(shares.columns):
        return pd.DataFrame()
    diff=(shares[1200]-shares[1600]).sort_values()
    chosen=pd.concat([diff.head(9),diff.tail(9)]).drop_duplicates()
    fig,ax=plt.subplots(figsize=(10,7.4),constrained_layout=True)
    labels=[LABELS.get(k,k) for k in chosen.index]
    colours=["#1F4E79" if v>0 else "#0F766E" for v in chosen]
    ax.barh(labels,chosen*100,color=colours); ax.axvline(0,color="#334155",linewidth=1)
    ax.set_title("沙田濕地：局部特徵貢獻差異")
    ax.set_xlabel("平均絕對 SHAP 佔比差：1,200米 − 1,600米（百分點）")
    ax.spines[["top","right"]].set_visible(False); ax.grid(axis="x",color="#E2E8F0"); ax.set_axisbelow(True)
    fig.savefig(out / "03_1200_vs_1600_feature_difference.png",dpi=180,bbox_inches="tight"); plt.close(fig)
    return pd.DataFrame({"feature":chosen.index,"shap_share_1200":shares.loc[chosen.index,1200].values,"shap_share_1600":shares.loc[chosen.index,1600].values,"difference_1200_minus_1600":chosen.values})


def main() -> None:
    parser=argparse.ArgumentParser(); parser.add_argument("--db",default="hkjc_last_season.sqlite"); parser.add_argument("--model",default="horse_model.pkl"); parser.add_argument("--predictions",default="v102_multiseason_backtest_predictions.csv"); parser.add_argument("--output-dir",default="v102_st_wet_distance_analysis"); args=parser.parse_args()
    plt.rcParams.update({"font.family":FONT,"axes.unicode_minus":False})
    out=Path(args.output_dir); out.mkdir(parents=True,exist_ok=True)
    predictions=pd.read_csv(args.predictions,encoding="utf-8-sig")
    conn=sqlite3.connect(args.db); distance_col=find_distance_column(conn)
    races=pd.read_sql_query(f"SELECT race_date,racecourse,race_no,going,{distance_col} AS distance_m FROM races WHERE race_status='completed'",conn)
    features=pd.read_sql_query("SELECT * FROM elo_feature_store",conn); conn.close()
    data=predictions.merge(races,on=["race_date","racecourse","race_no"],how="left",validate="many_to_one")
    data["distance_m"]=pd.to_numeric(data["distance_m"],errors="coerce").astype("Int64")
    wet=data[(data["racecourse"]=="ST") & (data["going"].isin(WET_RAW))].copy()
    if wet.empty: raise RuntimeError("No Sha Tin wet-going rows found in test predictions.")
    distance_counts=wet.groupby("distance_m")["race_group"].nunique().sort_index()
    summary=[]; shares={}; subsets=[]; bundle=joblib.load(args.model)
    for distance,n_races in distance_counts.items():
        subset=wet[wet["distance_m"]==distance].copy(); summary.append(summarize(subset,int(distance))); subsets.append(subset)
        keys=subset[["race_date","racecourse","race_no","horse_name"]]
        feat=features.merge(keys,on=["race_date","racecourse","race_no","horse_name"],how="inner",validate="one_to_one")
        shares[int(distance)]=shap_share(bundle,feat)
    summary_df=pd.DataFrame(summary).sort_values("distance_m")
    shares_df=pd.DataFrame(shares).sort_index(axis=1)
    summary_df.to_csv(out/"st_wet_distance_performance.csv",index=False,encoding="utf-8-sig")
    shares_df.to_csv(out/"st_wet_distance_catboost_shap_share.csv",encoding="utf-8-sig")
    pd.concat(subsets,ignore_index=True).to_csv(out/"st_wet_test_predictions_with_distance.csv",index=False,encoding="utf-8-sig")
    plot_counts(summary_df,out); plot_performance(summary_df,out); diff=plot_shap_difference(shares_df,out)
    if not diff.empty: diff.to_csv(out/"shap_1200_vs_1600_difference.csv",index=False,encoding="utf-8-sig")
    report={"distance_field":distance_col,"performance":summary,"feature_share":shares_df.to_dict(orient="index"),"note":"All splits use fixed V10.2 time-out predictions; distance groups below 15 races are exploratory."}
    (out/"st_wet_distance_summary.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"distance_field":distance_col,"performance":summary},ensure_ascii=False,indent=2))

if __name__=="__main__": main()
