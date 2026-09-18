"""Candidate-only fractional Kelly risk calculator; produces research output only."""
from __future__ import annotations
import argparse,json,math
from pathlib import Path
from typing import Any

def finite_probability(v): return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(float(v)) and 0<=float(v)<=1
def positive(v): return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(float(v)) and float(v)>0

def calculate_stakes(rows:list[dict[str,Any]],min_edge=.05,fraction=.25,max_single_stake=.05,max_race_exposure=.15):
    if not (0<=fraction<=1 and max_single_stake>=0 and max_race_exposure>=0 and min_edge>=0): raise ValueError('risk parameter out of range')
    out=[]
    for row in rows:
        x=dict(row); p=row.get('calibrated_win_probability',row.get('win_probability')); odds=row.get('win_odds')
        if not finite_probability(p) or not positive(odds):
            x.update({'status':'not_scored_invalid_probability_or_odds','ev':None,'full_kelly':None,'stake_fraction':None}); out.append(x); continue
        p=float(p); odds=float(odds); ev=p*odds-1.0; denom=odds-1.0
        full=((p*odds-1.0)/denom) if denom>0 else None
        if full is None or ev<=min_edge or full<=0:
            x.update({'status':'no_stake_nonpositive_or_below_min_edge','ev':ev,'full_kelly':full,'stake_fraction':0.0}); out.append(x); continue
        proposed=min(max_single_stake,fraction*full)
        x.update({'status':'eligible','ev':ev,'full_kelly':full,'stake_fraction':proposed}); out.append(x)
    total=math.fsum(float(x['stake_fraction']) for x in out if x.get('stake_fraction') is not None)
    scale=min(1.0,max_race_exposure/total) if total>0 else 1.0
    for x in out:
        if x.get('stake_fraction') is not None: x['stake_fraction']*=scale
    total_after=math.fsum(float(x['stake_fraction']) for x in out if x.get('stake_fraction') is not None)
    if total_after>0 and total_after<=max_race_exposure:
        # remove only floating-point residue from the final eligible row
        eligible=[x for x in out if x.get('stake_fraction') is not None and x['stake_fraction']>0]
        if eligible: eligible[-1]['stake_fraction']+=max_race_exposure-total_after if abs(total_after-max_race_exposure)<1e-12 else 0.0
    return {'status':'ok','parameters':{'min_edge':min_edge,'fraction':fraction,'max_single_stake':max_single_stake,'max_race_exposure':max_race_exposure},'rows':out,'total_stake_fraction':math.fsum(float(x['stake_fraction']) for x in out if x.get('stake_fraction') is not None)}

def main():
    p=argparse.ArgumentParser(); p.add_argument('--input',required=True,type=Path); p.add_argument('--output',required=True,type=Path); p.add_argument('--min-edge',type=float,default=.05); p.add_argument('--fraction',type=float,default=.25); p.add_argument('--max-single-stake',type=float,default=.05); p.add_argument('--max-race-exposure',type=float,default=.15); a=p.parse_args(); payload=json.loads(a.input.read_text(encoding='utf-8')); rows=payload.get('rows',payload) if isinstance(payload,(dict,list)) else None
    if not isinstance(rows,list): raise ValueError('input 必須是 rows 陣列')
    out=calculate_stakes(rows,a.min_edge,a.fraction,a.max_single_stake,a.max_race_exposure); a.output.parent.mkdir(parents=True,exist_ok=True); a.output.write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n',encoding='utf-8'); print(json.dumps({'status':out['status'],'total_stake_fraction':out['total_stake_fraction']},ensure_ascii=False)); return 0
if __name__=='__main__': raise SystemExit(main())
