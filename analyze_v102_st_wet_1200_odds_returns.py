#!/usr/bin/env python3
"""Odds structure and fixed-stake historical return simulation for V10.2 Sha Tin wet 1200m races.

Uses final historical odds only as a post-hoc market benchmark. It is not an executable
pre-race return backtest because final odds are unavailable until betting closes.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

FONT = "Noto Sans CJK TC"
WET_RAW = {"好地至黏地", "黏地", "軟地", "濕慢地"}


def find_field(conn: sqlite3.Connection, table: str, candidates: list[str]) -> str:
    cols = pd.read_sql_query(f"PRAGMA table_info({table})", conn)["name"].tolist()
    for field in candidates:
        if field in cols:
            return field
    raise RuntimeError(f"No matching field found in {table}: {candidates}; available: {cols}")


def odds_bucket(odds: float) -> str:
    if pd.isna(odds) or odds <= 0:
        return "無效／缺失"
    if odds < 3.5:
        return "熱門（<3.5）"
    if odds < 10:
        return "中賠（3.5–<10）"
    return "長途（≥10）"


def max_drawdown(profit: pd.Series) -> float:
    cumulative = profit.cumsum()
    running_peak = cumulative.cummax().clip(lower=0.0)
    return float((cumulative - running_peak).min())


def bootstrap_roi(returns: np.ndarray, stake: float, seed: int, simulations: int = 20_000) -> dict:
    rng = np.random.default_rng(seed)
    n = len(returns)
    samples = rng.choice(returns, size=(simulations, n), replace=True)
    rois = samples.sum(axis=1) / (stake * n)
    return {
        "simulations": simulations,
        "roi_p05": float(np.quantile(rois, 0.05)),
        "roi_p25": float(np.quantile(rois, 0.25)),
        "roi_p50": float(np.quantile(rois, 0.50)),
        "roi_p75": float(np.quantile(rois, 0.75)),
        "roi_p95": float(np.quantile(rois, 0.95)),
        "probability_positive_roi": float((rois > 0).mean()),
        "samples": rois,
    }


def plot_odds_distribution(all_rows: pd.DataFrame, selections: pd.DataFrame, out: Path) -> None:
    order = ["熱門（<3.5）", "中賠（3.5–<10）", "長途（≥10）"]
    field = all_rows[all_rows.win_odds > 0].copy(); field["bucket"] = field.win_odds.map(odds_bucket)
    summary = field.groupby("bucket", observed=True).agg(runners=("horse_name", "size"), wins=("target_win", "sum"), median_odds=("win_odds", "median")).reindex(order).fillna(0)
    summary["win_rate"] = np.divide(summary.wins, summary.runners, out=np.zeros(len(summary)), where=summary.runners.to_numpy()!=0)
    top = selections[selections.strategy=="模型首選（每場 1 注）"].copy(); top["bucket"] = top.win_odds.map(odds_bucket)
    top_summary = top.groupby("bucket", observed=True).size().reindex(order).fillna(0)
    fig,axes=plt.subplots(1,2,figsize=(13.8,5.4),constrained_layout=True)
    x=np.arange(len(order)); width=.35
    axes[0].bar(x-width/2,summary.runners,width,label="全場逐馬列",color="#1F4E79")
    axes[0].bar(x+width/2,top_summary,width,label="模型首選列",color="#15803D")
    axes[0].set_title("歷史最終獨贏賠率分布")
    axes[0].set_xticks(x,order); axes[0].set_ylabel("馬匹／首選列數"); axes[0].legend(frameon=False)
    axes[1].bar(x,summary.win_rate,color="#D97706")
    for bar,value in zip(axes[1].patches,summary.win_rate):
        axes[1].annotate(f"{value:.1%}",(bar.get_x()+bar.get_width()/2,value),xytext=(0,4),textcoords="offset points",ha="center",fontsize=9)
    axes[1].set_title("各賠率桶的實際勝出率")
    axes[1].set_xticks(x,order); axes[1].set_ylabel("勝出率"); axes[1].yaxis.set_major_formatter(lambda v,_:f"{v:.0%}")
    for ax in axes:
        ax.spines[["top","right"]].set_visible(False); ax.grid(axis="y",color="#E2E8F0"); ax.set_axisbelow(True)
    fig.savefig(out/"01_odds_distribution.png",dpi=180,bbox_inches="tight"); plt.close(fig)
    summary.reset_index().to_csv(out/"odds_bucket_summary.csv",index=False,encoding="utf-8-sig")


def plot_return_path(selections: pd.DataFrame, strategy_summary: pd.DataFrame, out: Path) -> None:
    fig,axes=plt.subplots(1,2,figsize=(14.5,5.5),constrained_layout=True)
    colors={"模型首選（每場 1 注）":"#15803D","市場熱門（每場 1 注）":"#D97706"}
    for strategy,group in selections.groupby("strategy",sort=False):
        group=group.sort_values(["race_date","race_no"]).copy()
        group["cum_profit"]=group.profit.cumsum()
        axes[0].plot(range(1,len(group)+1),group.cum_profit,marker="o",label=strategy,color=colors.get(strategy,"#2563EB"))
    axes[0].axhline(0,color="#334155",linewidth=1); axes[0].set_title("固定注額歷史累積損益（每場 1 單位）")
    axes[0].set_xlabel("賽事序號（按日期／場次）"); axes[0].set_ylabel("累積損益（單位）"); axes[0].legend(frameon=False)
    ss=strategy_summary.copy(); x=np.arange(len(ss)); bars=axes[1].bar(x,ss.roi,color=[colors.get(x,"#2563EB") for x in ss.strategy])
    for bar,row in zip(bars,ss.itertuples()):
        axes[1].annotate(f"{row.roi:.1%}\n{row.wins}/{row.bets}",(bar.get_x()+bar.get_width()/2,bar.get_height()),xytext=(0,5 if bar.get_height()>=0 else -14),textcoords="offset points",ha="center",fontsize=9)
    axes[1].axhline(0,color="#334155",linewidth=1); axes[1].set_xticks(x,ss.strategy,rotation=12,ha="right"); axes[1].set_title("固定注額歷史 ROI（最終賠率事後代理）")
    axes[1].set_ylabel("ROI"); axes[1].yaxis.set_major_formatter(lambda v,_:f"{v:.0%}")
    for ax in axes:
        ax.spines[["top","right"]].set_visible(False); ax.grid(axis="y",color="#E2E8F0"); ax.set_axisbelow(True)
    fig.savefig(out/"02_fixed_stake_return_simulation.png",dpi=180,bbox_inches="tight"); plt.close(fig)


def plot_bootstrap(boot: dict[str, dict], out: Path) -> None:
    fig,ax=plt.subplots(figsize=(10.5,5.8),constrained_layout=True)
    colors={"模型首選（每場 1 注）":"#15803D","市場熱門（每場 1 注）":"#D97706"}
    for strategy,vals in boot.items():
        ax.hist(vals["samples"],bins=55,density=True,alpha=.48,label=strategy,color=colors[strategy])
        ax.axvline(vals["roi_p50"],color=colors[strategy],linestyle="--",linewidth=1.5)
    ax.axvline(0,color="#334155",linewidth=1); ax.set_title("逐場重抽樣：17 場固定注額 ROI 的不確定性")
    ax.set_xlabel("ROI"); ax.set_ylabel("密度"); ax.xaxis.set_major_formatter(lambda v,_:f"{v:.0%}"); ax.legend(frameon=False)
    ax.spines[["top","right"]].set_visible(False); ax.grid(axis="y",color="#E2E8F0"); ax.set_axisbelow(True)
    fig.savefig(out/"03_bootstrap_roi_uncertainty.png",dpi=180,bbox_inches="tight"); plt.close(fig)


def main() -> None:
    parser=argparse.ArgumentParser(); parser.add_argument("--db",default="hkjc_last_season.sqlite"); parser.add_argument("--predictions",default="v102_multiseason_backtest_predictions.csv"); parser.add_argument("--output-dir",default="v102_st_wet_1200_odds_return_analysis"); parser.add_argument("--stake",type=float,default=1.0); args=parser.parse_args()
    if args.stake <= 0: raise ValueError("stake must be positive")
    plt.rcParams.update({"font.family":FONT,"axes.unicode_minus":False})
    out=Path(args.output_dir); out.mkdir(parents=True,exist_ok=True)
    pred=pd.read_csv(args.predictions,encoding="utf-8-sig")
    conn=sqlite3.connect(args.db)
    distance_col=find_field(conn,"races",["distance_m","distance","race_distance"])
    odds_col=find_field(conn,"starters",["win_odds","odds","starting_odds","winodds"])
    races=pd.read_sql_query(f"SELECT race_date,racecourse,race_no,going,{distance_col} AS distance_m FROM races WHERE race_status='completed'",conn)
    starters=pd.read_sql_query(f"SELECT race_date,racecourse,race_no,horse_name,{odds_col} AS win_odds FROM starters",conn)
    conn.close()
    data=pred.merge(races,on=["race_date","racecourse","race_no"],how="left",validate="many_to_one")
    data=data.merge(starters,on=["race_date","racecourse","race_no","horse_name"],how="left",validate="one_to_one")
    data["distance_m"]=pd.to_numeric(data.distance_m,errors="coerce"); data["win_odds"]=pd.to_numeric(data.win_odds,errors="coerce")
    sample=data[(data.racecourse=="ST")&(data.distance_m==1200)&(data.going.isin(WET_RAW))&(data.win_odds>0)].copy()
    if sample.race_group.nunique() < 2: raise RuntimeError("Insufficient matched odds sample.")
    # Deterministic top horse, with model rank / model probability / horse name ordering for any rare tie.
    selected=[]
    for race,group in sample.groupby("race_group",sort=False):
        ranked=group.sort_values(["model_rank","race_normalized_probability","horse_name"],ascending=[True,False,True])
        model_top=ranked.iloc[0].copy(); model_top["strategy"]="模型首選（每場 1 注）"; selected.append(model_top)
        favourite=group.sort_values(["win_odds","model_rank","horse_name"],ascending=[True,True,True]).iloc[0].copy(); favourite["strategy"]="市場熱門（每場 1 注）"; selected.append(favourite)
    selections=pd.DataFrame(selected).reset_index(drop=True)
    selections["stake"]=args.stake
    selections["payout"]=np.where(selections.target_win.astype(int)==1,selections.win_odds*args.stake,0.0)
    selections["profit"]=selections.payout-selections.stake
    selections["historical_final_odds_ev_proxy"]=selections.race_normalized_probability*selections.win_odds-1
    selections["odds_bucket"]=selections.win_odds.map(odds_bucket)
    selections.sort_values(["strategy","race_date","race_no"],inplace=True)
    selections.to_csv(out/"selection_level_fixed_stake_returns.csv",index=False,encoding="utf-8-sig")
    stats=[]; boot={}
    for strategy,group in selections.groupby("strategy",sort=False):
        returns=group.profit.to_numpy(float)
        ordered = group.sort_values(["race_date", "race_no"])
        stats.append({"strategy":strategy,"bets":int(len(group)),"wins":int(group.target_win.sum()),"hit_rate":float(group.target_win.mean()),"total_stake":float(group.stake.sum()),"gross_payout":float(group.payout.sum()),"net_profit":float(group.profit.sum()),"roi":float(group.profit.sum()/group.stake.sum()),"median_odds":float(group.win_odds.median()),"max_drawdown_units":max_drawdown(ordered.profit),"mean_historical_final_odds_ev_proxy":float(group.historical_final_odds_ev_proxy.mean())})
        boot[strategy]=bootstrap_roi(returns,args.stake,seed=20260815 + len(boot))
    strategy_summary=pd.DataFrame(stats); strategy_summary.to_csv(out/"fixed_stake_strategy_summary.csv",index=False,encoding="utf-8-sig")
    bucket_summary = selections.groupby(["strategy", "odds_bucket"], observed=True).agg(bets=("horse_name", "size"), wins=("target_win", "sum"), total_stake=("stake", "sum"), gross_payout=("payout", "sum"), net_profit=("profit", "sum")).reset_index()
    bucket_summary["hit_rate"] = bucket_summary["wins"] / bucket_summary["bets"]
    bucket_summary["roi"] = bucket_summary["net_profit"] / bucket_summary["total_stake"]
    bucket_summary.to_csv(out/"selection_odds_bucket_return_summary.csv",index=False,encoding="utf-8-sig")
    boot_summary=[]
    for strategy,vals in boot.items():
        boot_summary.append({k:v for k,v in vals.items() if k!="samples"}|{"strategy":strategy})
    pd.DataFrame(boot_summary).to_csv(out/"bootstrap_roi_summary.csv",index=False,encoding="utf-8-sig")
    plot_odds_distribution(sample,selections,out); plot_return_path(selections,strategy_summary,out); plot_bootstrap(boot,out)
    model=selections[selections.strategy=="模型首選（每場 1 注）"].copy()
    market=selections[selections.strategy=="市場熱門（每場 1 注）"].copy()
    agreement=float((model.sort_values("race_group").horse_name.to_numpy()==market.sort_values("race_group").horse_name.to_numpy()).mean())
    return_data={
      "scope":{"races":int(sample.race_group.nunique()),"runners":int(len(sample)),"conditions":"ST; 1200m; 好地至黏地／黏地／軟地／濕慢地"},
      "odds_field":odds_col,"distance_field":distance_col,"model_market_agreement_rate":agreement,
      "strategy_summary":stats,"selection_odds_bucket_returns":bucket_summary.to_dict(orient="records"),"bootstrap":boot_summary,
      "warning":"Fixed-stake results use final historical odds as a post-hoc proxy. Final odds are not a pre-race executable price; no result is a profitability guarantee."
    }
    (out/"odds_return_analysis_summary.json").write_text(json.dumps(return_data,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(return_data,ensure_ascii=False,indent=2))

if __name__=="__main__": main()
