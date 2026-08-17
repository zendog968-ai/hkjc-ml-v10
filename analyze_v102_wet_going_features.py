#!/usr/bin/env python3
"""Analyze V10.2 time-out performance and local features in wet / yielding conditions."""
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
    "trainer_win_rate_pre": "練馬師勝率", "closing400_proxy_pre": "末段走勢代理",
}


def per_race_metrics(frame: pd.DataFrame) -> pd.Series:
    y = frame["target_win"].astype(float).to_numpy()
    p = frame["race_normalized_probability"].astype(float).to_numpy()
    n = len(frame)
    return pd.Series({
        "top_pick_win": int(((frame["model_rank"] == 1) & (frame["target_win"] == 1)).any()),
        "top3_contains_winner": int(((frame["model_rank"] <= 3) & (frame["target_win"] == 1)).any()),
        "brier": float(np.square(p-y).sum()),
        "uniform_brier": float(np.square(1/n-y).sum()),
        "uniform_top_rate": 1/n,
    })


def summarize(frame: pd.DataFrame, group: str) -> dict:
    races = frame.groupby("race_group", sort=False).apply(per_race_metrics, include_groups=False).reset_index(drop=True)
    return {
        "group": group, "races": int(len(races)), "runners": int(len(frame)),
        "top_pick_win_rate": float(races["top_pick_win"].mean()),
        "top3_contains_winner_rate": float(races["top3_contains_winner"].mean()),
        "uniform_top_pick_rate": float(races["uniform_top_rate"].mean()),
        "mean_brier": float(races["brier"].mean()),
        "uniform_brier": float(races["uniform_brier"].mean()),
        "top_pick_lift": float(races["top_pick_win"].mean() / races["uniform_top_rate"].mean()),
        "brier_improvement": float(races["uniform_brier"].mean() - races["brier"].mean()),
        "sample_flag": "可比較" if len(races) >= 15 else "探索性（<15 場）",
    }


def shap_values(bundle: dict, df: pd.DataFrame) -> pd.DataFrame:
    cols = bundle["all_features"]
    X = df[cols].copy()
    for col in bundle["numeric_features"]:
        X[col] = pd.to_numeric(X[col], errors="coerce").fillna(0.0)
    for col in bundle["categorical_features"]:
        X[col] = X[col].fillna("未知").astype(str)
    pool = Pool(X, cat_features=bundle["catboost_categorical_indices"])
    values = bundle["catboost_model"].get_feature_importance(pool, type="ShapValues")[:, :-1]
    return pd.DataFrame(np.abs(values), columns=cols, index=df.index)


def plot_raw_samples(raw: pd.DataFrame, out: Path) -> None:
    pivot = raw.pivot(index="going", columns="racecourse", values="races").fillna(0).sort_index()
    ax = pivot.plot(kind="bar", figsize=(9, 5.5), color={"ST": "#1F4E79", "HV": "#0F766E"})
    ax.set_title("濕地原始官方場地狀況：馬場樣本數")
    ax.set_xlabel("官方場地狀況"); ax.set_ylabel("場次"); ax.legend(title="馬場", frameon=False)
    ax.spines[["top", "right"]].set_visible(False); ax.grid(axis="y", color="#E2E8F0"); ax.set_axisbelow(True)
    plt.tight_layout(); plt.savefig(out / "01_wet_raw_condition_samples.png", dpi=180, bbox_inches="tight"); plt.close()


def plot_performance(summary: pd.DataFrame, out: Path) -> None:
    labels = summary["group"].tolist(); x=np.arange(len(labels)); width=.35
    fig, axes = plt.subplots(1,2,figsize=(15,5.8),constrained_layout=True)
    axes[0].bar(x-.18,summary["top_pick_win_rate"],.36,label="V10.2 首選",color="#15803D")
    axes[0].bar(x+.18,summary["uniform_top_pick_rate"],.36,label="等機會基準",color="#94A3B8")
    for i,row in summary.iterrows():
        axes[0].annotate(f"{row.top_pick_win_rate:.1%}\nn={row.races}",(i-.18,row.top_pick_win_rate),xytext=(0,4),textcoords="offset points",ha="center",fontsize=9)
    axes[0].set_title("濕地組別首選勝出率"); axes[0].set_xticks(x,labels,rotation=15,ha="right"); axes[0].set_ylabel("勝出率"); axes[0].yaxis.set_major_formatter(lambda v,_:f"{v:.0%}"); axes[0].legend(frameon=False)
    axes[1].bar(x-.18,summary["mean_brier"],.36,label="V10.2",color="#2563EB")
    axes[1].bar(x+.18,summary["uniform_brier"],.36,label="等機會基準",color="#94A3B8")
    axes[1].set_title("濕地組別 Brier score（越低越佳）"); axes[1].set_xticks(x,labels,rotation=15,ha="right"); axes[1].set_ylabel("Brier score"); axes[1].legend(frameon=False)
    for ax in axes:
        ax.spines[["top","right"]].set_visible(False); ax.grid(axis="y",color="#E2E8F0"); ax.set_axisbelow(True)
    fig.savefig(out / "02_wet_performance.png",dpi=180,bbox_inches="tight"); plt.close(fig)


def plot_shap(shares: pd.DataFrame, out: Path) -> None:
    # Show the most material features in pooled wet rows and retain each group as a column.
    mean_share = shares.mean(axis=1).sort_values(ascending=False)
    selected = mean_share.head(14).index
    matrix = shares.loc[selected].iloc[::-1]
    fig, ax = plt.subplots(figsize=(10,7.5),constrained_layout=True)
    image = ax.imshow(matrix.to_numpy()*100, cmap="YlGnBu", aspect="auto")
    ax.set_xticks(range(len(matrix.columns)), matrix.columns, rotation=15, ha="right")
    ax.set_yticks(range(len(matrix.index)), [LABELS.get(x,x) for x in matrix.index])
    for i in range(matrix.shape[0]):
        for j in range(matrix.shape[1]):
            ax.text(j,i,f"{matrix.iloc[i,j]*100:.1f}",ha="center",va="center",fontsize=9, color="white" if matrix.iloc[i,j] > matrix.to_numpy().mean() else "black")
    ax.set_title("濕地組別：CatBoost 平均絕對 SHAP 佔比（%）")
    fig.colorbar(image,ax=ax,label="平均絕對 SHAP 佔比（%）")
    fig.savefig(out / "03_wet_local_feature_shares.png",dpi=180,bbox_inches="tight"); plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--db",default="hkjc_last_season.sqlite"); parser.add_argument("--model",default="horse_model.pkl"); parser.add_argument("--predictions",default="v102_multiseason_backtest_predictions.csv"); parser.add_argument("--output-dir",default="v102_wet_going_analysis"); args=parser.parse_args()
    plt.rcParams.update({"font.family":"Noto Sans CJK TC","axes.unicode_minus":False})
    out=Path(args.output_dir); out.mkdir(parents=True,exist_ok=True)
    pred=pd.read_csv(args.predictions,encoding="utf-8-sig")
    conn=sqlite3.connect(args.db)
    race=pd.read_sql_query("SELECT race_date,racecourse,race_no,going FROM races WHERE race_status='completed'",conn)
    feat=pd.read_sql_query("SELECT * FROM elo_feature_store",conn); conn.close()
    data=pred.merge(race,on=["race_date","racecourse","race_no"],how="left",validate="many_to_one")
    wet=data[data["going"].isin(WET_RAW)].copy()
    if wet.empty: raise RuntimeError("No wet-going rows in the time-out test set.")
    raw=wet.groupby(["racecourse","going"])["race_group"].nunique().reset_index(name="races")
    raw.to_csv(out/"wet_raw_condition_counts.csv",index=False,encoding="utf-8-sig")
    plot_raw_samples(raw,out)

    definitions={
      "沙田・濕地合併": wet[wet.racecourse=="ST"],
      "跑馬地・濕地合併": wet[wet.racecourse=="HV"],
      "沙田・好至黏地": wet[(wet.racecourse=="ST")&(wet.going=="好地至黏地")],
      "跑馬地・好至黏地": wet[(wet.racecourse=="HV")&(wet.going=="好地至黏地")],
      "沙田・黏／軟／濕慢": wet[(wet.racecourse=="ST")&(wet.going!="好地至黏地")],
      "跑馬地・黏／軟／濕慢": wet[(wet.racecourse=="HV")&(wet.going!="好地至黏地")],
    }
    summary=[]; shares={}; rows=[]; bundle=joblib.load(args.model)
    for name, frame in definitions.items():
        if frame.empty: continue
        summary.append(summarize(frame,name)); rows.append(frame.assign(segment=name))
        keys=frame[["race_date","racecourse","race_no","horse_name"]]
        local=feat.merge(keys,on=["race_date","racecourse","race_no","horse_name"],how="inner",validate="one_to_one")
        sv=shap_values(bundle,local).mean(axis=0); shares[name]=sv/sv.sum()
    summary_df=pd.DataFrame(summary); summary_df.to_csv(out/"wet_segment_performance.csv",index=False,encoding="utf-8-sig")
    plot_performance(summary_df,out)
    shares_df=pd.DataFrame(shares); shares_df.to_csv(out/"wet_local_catboost_shap_share.csv",encoding="utf-8-sig")
    plot_shap(shares_df,out)
    pd.concat(rows,ignore_index=True).to_csv(out/"wet_test_predictions.csv",index=False,encoding="utf-8-sig")
    summary_json={"raw_condition_counts":raw.to_dict(orient="records"),"performance":summary,"local_shap_share":shares_df.to_dict(orient="index"),"note":"Groups below 15 races are exploratory; all values derive from fixed time-out test predictions."}
    (out/"wet_going_analysis_summary.json").write_text(json.dumps(summary_json,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"raw_condition_counts":raw.to_dict(orient="records"),"performance":summary},ensure_ascii=False,indent=2))

if __name__=="__main__": main()
