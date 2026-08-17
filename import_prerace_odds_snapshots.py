#!/usr/bin/env python3
"""Import valid V10.2 T-15/T-5 odds snapshots into an auditable SQLite archive.

The importer rejects final-odds overlays, degraded captures, missing race identity,
and files under test paths unless --include-test is supplied explicitly.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VALID_LABELS = {"T_MINUS_15", "T_MINUS_5"}


def parse_utc(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("缺少 captured_at_utc")
    datetime.fromisoformat(text.replace("Z", "+00:00"))
    return text


def normalized_date(value: object) -> str:
    text = str(value or "").strip().replace("/", "-")
    return datetime.strptime(text, "%Y-%m-%d").strftime("%Y-%m-%d")


def valid_odd(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"賠率不可解析：{value!r}")
    if number <= 1.0:
        raise ValueError(f"賠率必須大於 1.0：{value!r}")
    return number


def validate(path: Path) -> tuple[dict[str, Any], list[tuple[str, float | None, float | None]], str]:
    raw = path.read_bytes(); sha = hashlib.sha256(raw).hexdigest()
    obj = json.loads(raw.decode("utf-8"))
    if not isinstance(obj, dict) or obj.get("schema_version") != "v10.2_odds_snapshot":
        raise ValueError("不是 v10.2_odds_snapshot 架構")
    label = str(obj.get("snapshot_label") or "")
    if label not in VALID_LABELS:
        raise ValueError(f"快照標籤不合法：{label!r}")
    if obj.get("status") != "complete":
        raise ValueError("快照狀態不是 complete；不允許作真實回測資料")
    race = obj.get("race")
    if not isinstance(race, dict):
        raise ValueError("缺少 race 物件")
    date = normalized_date(race.get("race_date")); course = str(race.get("racecourse") or "").upper()
    if course not in {"ST", "HV"}: raise ValueError(f"馬場不合法：{course!r}")
    try: no = int(race.get("race_no"))
    except (TypeError, ValueError): raise ValueError("race_no 不合法")
    if no < 1: raise ValueError("race_no 必須為正整數")
    captured = parse_utc(obj.get("captured_at_utc"))
    odds = obj.get("odds")
    if not isinstance(odds, dict) or not odds: raise ValueError("缺少 odds 馬匹賠率物件")
    runners=[]
    for horse, values in odds.items():
        if not str(horse).strip() or not isinstance(values, dict): raise ValueError("馬名或賠率列不合法")
        runners.append((str(horse).strip(), valid_odd(values.get("win")), valid_odd(values.get("place"))))
    if not any(win is not None for _, win, _ in runners): raise ValueError("沒有可用獨贏賠率")
    normalised = {"schema_version":obj["schema_version"],"snapshot_label":label,"race_date":date,"racecourse":course,"race_no":no,"captured_at_utc":captured,"status":"complete","source_url":(obj.get("source_url") or (obj.get("metadata") or {}).get("source_url")),"source_mode":obj.get("source_mode")}
    return normalised, runners, sha


def main() -> int:
    parser=argparse.ArgumentParser(); parser.add_argument("--db",required=True); parser.add_argument("--schema",default="schema_prerace_odds_snapshots.sql"); parser.add_argument("--input-root",required=True); parser.add_argument("--include-test",action="store_true"); parser.add_argument("--report",default="snapshot_import_report.json"); args=parser.parse_args()
    root=Path(args.input_root); schema=Path(args.schema); db=Path(args.db)
    if not root.exists(): raise SystemExit(f"輸入目錄不存在：{root}")
    if not schema.exists(): raise SystemExit(f"資料庫結構檔不存在：{schema}")
    conn=sqlite3.connect(db); conn.execute("PRAGMA foreign_keys = ON"); conn.executescript(schema.read_text(encoding="utf-8")); conn.commit()
    report={"scanned":0,"imported":0,"already_present":0,"skipped_test":0,"rejected":[],"imported_files":[]}
    for path in sorted(root.rglob("*.json")):
        report["scanned"] += 1
        lowered={part.lower() for part in path.parts}
        if not args.include_test and any("test" in part for part in lowered):
            report["skipped_test"] += 1; continue
        try:
            header,runners,sha=validate(path)
            exists=conn.execute("SELECT snapshot_id FROM pre_race_odds_snapshots WHERE payload_sha256=?",(sha,)).fetchone()
            if exists:
                report["already_present"] += 1; continue
            now=datetime.now(timezone.utc).isoformat(timespec="seconds")
            cur=conn.execute("""INSERT INTO pre_race_odds_snapshots(schema_version,snapshot_label,race_date,racecourse,race_no,captured_at_utc,status,source_url,source_mode,source_file_path,payload_sha256,raw_payload_json,imported_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",(header["schema_version"],header["snapshot_label"],header["race_date"],header["racecourse"],header["race_no"],header["captured_at_utc"],header["status"],header["source_url"],header["source_mode"],str(path),sha,path.read_text(encoding="utf-8"),now))
            conn.executemany("INSERT INTO pre_race_odds_runner_prices(snapshot_id,horse_name,win_odds,place_odds) VALUES (?,?,?,?)",[(cur.lastrowid,*runner) for runner in runners])
            report["imported"] += 1; report["imported_files"].append(str(path))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, sqlite3.IntegrityError) as exc:
            report["rejected"].append({"file":str(path),"reason":f"{type(exc).__name__}: {exc}"})
    conn.commit(); tables=[row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]; counts={table:conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables if table.startswith("pre_race_odds_")}; conn.close()
    report["archive_counts"]=counts; Path(args.report).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8"); print(json.dumps(report,ensure_ascii=False,indent=2)); return 0

if __name__=="__main__": raise SystemExit(main())
