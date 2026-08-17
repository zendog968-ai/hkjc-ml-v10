#!/usr/bin/env python3
"""Audit and backtest V10.2 ST wet 1200m selections using only persisted pre-race odds snapshots.

The script deliberately refuses to replace missing T-15/T-5 snapshots with final odds.
Only JSON files with schema_version=v10.2_odds_snapshot, the exact race identity,
a recognised snapshot label, status=complete and a usable WIN price are eligible.
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
LABELS = {"T_MINUS_15": "T-15", "T_MINUS_5": "T-5"}


def find_distance_column(conn: sqlite3.Connection) -> str:
    columns = pd.read_sql_query("PRAGMA table_info(races)", conn)["name"].tolist()
    for column in ("distance_m", "distance", "race_distance"):
        if column in columns:
            return column
    raise RuntimeError("races 表未有可用路程欄位。")


def load_candidates(root: Path) -> list[tuple[Path, dict]]:
    candidates: list[tuple[Path, dict]] = []
    for path in root.rglob("*.json"):
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(obj, dict) and obj.get("schema_version") == "v10.2_odds_snapshot":
            candidates.append((path, obj))
    return candidates


def usable_price(value: object) -> float | None:
    try:
        odds = float(value)
    except (TypeError, ValueError):
        return None
    return odds if np.isfinite(odds) and odds > 1.0 else None


def field_rows(snapshot_items: list[tuple[Path, dict]]) -> pd.DataFrame:
    rows=[]
    for path,obj in snapshot_items:
        race=obj.get("race") or {}; label=str(obj.get("snapshot_label") or "")
        odds=obj.get("odds") or {}
        if label not in LABELS or not isinstance(odds,dict):
            continue
        for horse, values in odds.items():
            if isinstance(values,dict):
                rows.append({"snapshot_path":str(path),"snapshot_label":label,"captured_at_utc":obj.get("captured_at_utc"),"snapshot_status":obj.get("status"),"race_date":str(race.get("race_date") or "").replace("/","-"),"racecourse":str(race.get("racecourse") or "").upper(),"race_no":race.get("race_no"),"horse_name":horse,"win_odds":usable_price(values.get("win"))})
    return pd.DataFrame(rows)


def summarize_results(joined: pd.DataFrame, label: str) -> dict:
    eligible=joined[(joined.snapshot_label==label)&(joined.snapshot_status=="complete")&(joined.win_odds.notna())].copy()
    if eligible.empty:
        return {"snapshot_label":label,"eligible_bets":0,"covered_races":0,"wins":0,"hit_rate":None,"total_stake":0.0,"gross_payout":0.0,"net_profit":0.0,"roi":None,"mean_ev":None}
    eligible["stake"]=1.0
    eligible["payout"]=np.where(eligible.target_win.astype(int)==1,eligible.win_odds,0.0)
    eligible["profit"]=eligible.payout-eligible.stake
    eligible["ev_at_snapshot"]=eligible.race_normalized_probability*eligible.win_odds-1
    return {"snapshot_label":label,"eligible_bets":int(len(eligible)),"covered_races":int(eligible.race_group.nunique()),"wins":int(eligible.target_win.sum()),"hit_rate":float(eligible.target_win.mean()),"total_stake":float(eligible.stake.sum()),"gross_payout":float(eligible.payout.sum()),"net_profit":float(eligible.profit.sum()),"roi":float(eligible.profit.sum()/eligible.stake.sum()),"mean_ev":float(eligible.ev_at_snapshot.mean())}


def plot_coverage(audit: pd.DataFrame, out: Path) -> None:
    labels=[LABELS[x] for x in audit.snapshot_label]
    fig,ax=plt.subplots(figsize=(8.5,5.2),constrained_layout=True)
    colors=["#15803D" if x>0 else "#B91C1C" for x in audit.covered_races]
    bars=ax.bar(labels,audit.covered_races,color=colors)
    for bar,row in zip(bars,audit.itertuples()):
        ax.annotate(f"{row.covered_races}/{row.target_races} 場",(bar.get_x()+bar.get_width()/2,bar.get_height()),xytext=(0,5),textcoords="offset points",ha="center",fontsize=11)
    ax.set_title("沙田濕地 1,200 米：可用官方賽前快照覆蓋")
    ax.set_ylabel("模型首選具完整可用獨贏賠率的場次")
    ax.set_ylim(0,max(1,audit.target_races.max())*1.2)
    ax.spines[["top","right"]].set_visible(False); ax.grid(axis="y",color="#E2E8F0"); ax.set_axisbelow(True)
    fig.savefig(out/"01_prerace_snapshot_coverage.png",dpi=180,bbox_inches="tight"); plt.close(fig)


def main() -> None:
    parser=argparse.ArgumentParser(); parser.add_argument("--db",default="hkjc_last_season.sqlite"); parser.add_argument("--predictions",default="v102_multiseason_backtest_predictions.csv"); parser.add_argument("--snapshot-root",default="runtime/pre_race"); parser.add_argument("--output-dir",default="v102_st_wet_1200_prerace_backtest"); args=parser.parse_args()
    plt.rcParams.update({"font.family":FONT,"axes.unicode_minus":False})
    out=Path(args.output_dir); out.mkdir(parents=True,exist_ok=True)
    predictions=pd.read_csv(args.predictions,encoding="utf-8-sig")
    conn=sqlite3.connect(args.db); distance_col=find_distance_column(conn)
    races=pd.read_sql_query(f"SELECT race_date,racecourse,race_no,going,{distance_col} AS distance_m FROM races WHERE race_status='completed'",conn)
    database_tables=pd.read_sql_query("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",conn)["name"].tolist(); conn.close()
    data=predictions.merge(races,on=["race_date","racecourse","race_no"],how="left",validate="many_to_one")
    data["distance_m"]=pd.to_numeric(data.distance_m,errors="coerce")
    target=data[(data.racecourse=="ST")&(data.distance_m==1200)&(data.going.isin(WET_RAW))].copy()
    model_top=target.sort_values(["race_group","model_rank","race_normalized_probability","horse_name"],ascending=[True,True,False,True]).groupby("race_group",as_index=False).first()
    snapshot_root=Path(args.snapshot_root)
    items=load_candidates(snapshot_root) if snapshot_root.exists() else []
    fields=field_rows(items)
    if not fields.empty:
        fields["race_no"]=pd.to_numeric(fields.race_no,errors="coerce").astype("Int64")
        fields=fields.dropna(subset=["race_no"]); fields["race_no"]=fields.race_no.astype(int)
        fields=fields.sort_values("captured_at_utc").drop_duplicates(["snapshot_label","race_date","racecourse","race_no","horse_name"],keep="last")
        joined=model_top.merge(fields,on=["race_date","racecourse","race_no","horse_name"],how="left",validate="one_to_many")
    else:
        joined=model_top.copy(); joined["snapshot_label"]=pd.NA; joined["snapshot_status"]=pd.NA; joined["captured_at_utc"]=pd.NA; joined["win_odds"]=np.nan; joined["snapshot_path"]=pd.NA
    audit_rows=[]
    for label in LABELS:
        covered=joined[(joined.snapshot_label==label)&(joined.snapshot_status=="complete")&(joined.win_odds.notna())]
        audit_rows.append({"snapshot_label":label,"target_races":int(model_top.race_group.nunique()),"snapshot_files_found":sum(1 for _,obj in items if str(obj.get("snapshot_label"))==label),"covered_races":int(covered.race_group.nunique()),"coverage_rate":float(covered.race_group.nunique()/model_top.race_group.nunique()),"status":"可回測" if not covered.empty else "無可用匹配快照：不可進行真實賽前回測"})
    audit=pd.DataFrame(audit_rows); audit.to_csv(out/"snapshot_coverage_audit.csv",index=False,encoding="utf-8-sig")
    joined.to_csv(out/"model_top_snapshot_match_audit.csv",index=False,encoding="utf-8-sig")
    result_summary=[summarize_results(joined,label) for label in LABELS]
    pd.DataFrame(result_summary).to_csv(out/"prerace_odds_backtest_summary.csv",index=False,encoding="utf-8-sig")
    plot_coverage(audit,out)
    payload={"scope":{"target_races":int(model_top.race_group.nunique()),"conditions":"ST; 1200m; 好地至黏地／黏地／軟地／濕慢地"},"snapshot_root":str(snapshot_root),"snapshot_files_found":len(items),"database_tables":database_tables,"audit":audit_rows,"pre_race_backtest":result_summary,"integrity_rule":"Only complete T-15/T-5 snapshot files matched to the exact race and model-top horse are eligible. Final odds are never substituted."}
    (out/"prerace_snapshot_backtest_audit.json").write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(payload,ensure_ascii=False,indent=2))

if __name__=="__main__": main()
