"""Candidate-only T-15/T-5 odds drift analyzer; no live betting or model access."""
from __future__ import annotations
import argparse,json,math
from pathlib import Path
from typing import Any

DROP_THRESHOLD=0.15
DIVERGENCE_THRESHOLD=0.15

def key(row):
    try: return (int(row['race_no']),int(row['horse_no']))
    except (KeyError,TypeError,ValueError): return None

def positive_number(row,name):
    value=row.get(name)
    if isinstance(value,bool) or not isinstance(value,(int,float)):
        return None
    value=float(value)
    return value if math.isfinite(value) and value>0 else None

def analyze_snapshots(t15:list[dict[str,Any]],t5:list[dict[str,Any]],drop_threshold=DROP_THRESHOLD,divergence_threshold=DIVERGENCE_THRESHOLD):
    a={key(r):r for r in t15 if key(r) is not None}; b={key(r):r for r in t5 if key(r) is not None}
    if len(a)!=len(t15) or len(b)!=len(t5): raise ValueError('snapshot 含無效或重複 race_no/horse_no')
    rows=[]
    for k in sorted(set(a)|set(b)):
        r15,r5=a.get(k),b.get(k)
        base={'race_no':k[0],'horse_no':k[1]}
        if r15 is None or r5 is None:
            base.update({'status':'not_scored_missing_snapshot','win_drift_ratio':None,'place_drift_ratio':None,'smart_money_flag':None})
            rows.append(base); continue
        w15,w5=positive_number(r15,'win_odds'),positive_number(r5,'win_odds')
        p15,p5=positive_number(r15,'place_odds'),positive_number(r5,'place_odds')
        if any(x is None for x in (w15,w5,p15,p5)):
            base.update({'status':'not_scored_invalid_odds','win_drift_ratio':None,'place_drift_ratio':None,'smart_money_flag':None})
            rows.append(base); continue
        wr=(w5-w15)/w15; pr=(p5-p15)/p15
        base.update({'status':'ok','win_t15':w15,'win_t5':w5,'place_t15':p15,'place_t5':p5,'win_drift_ratio':wr,'place_drift_ratio':pr,'win_drop_flag':wr<=-abs(drop_threshold),'win_place_divergence':abs(wr-pr),'smart_money_flag':wr<=-abs(drop_threshold) or abs(wr-pr)>=abs(divergence_threshold)})
        rows.append(base)
    return {'status':'ok','thresholds':{'drop_threshold':drop_threshold,'divergence_threshold':divergence_threshold},'rows':rows}

def main():
    p=argparse.ArgumentParser(); p.add_argument('--t15',required=True,type=Path); p.add_argument('--t5',required=True,type=Path); p.add_argument('--output',required=True,type=Path); a=p.parse_args()
    x=lambda path: json.loads(path.read_text(encoding='utf-8')); t15=x(a.t15); t5=x(a.t5); t15=t15.get('snapshots',t15) if isinstance(t15,dict) else t15; t5=t5.get('snapshots',t5) if isinstance(t5,dict) else t5
    out=analyze_snapshots(t15,t5); a.output.parent.mkdir(parents=True,exist_ok=True); a.output.write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n',encoding='utf-8'); print(json.dumps({'status':out['status'],'row_count':len(out['rows'])},ensure_ascii=False)); return 0
if __name__=='__main__': raise SystemExit(main())
