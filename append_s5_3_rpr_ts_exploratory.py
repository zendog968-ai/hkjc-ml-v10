#!/usr/bin/env python3
"""Append the S5-3 public RPR/TS four-place audit to an isolated study cohort.

The shared SQLite ledger may hold several cohorts, but this script never merges
S5-3 public RPR/TS Plackett-Luce records with the Australian public-NR ranking
cohort.  It is idempotent and refuses any same-event hash mismatch.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DB = ROOT / "overseas_exploratory_backtest.sqlite"
PRE = ROOT / "reports/overseas_deep/S5_3_YORK_PUBLIC_RPR_TS_RESEARCH_2026-08-22.json"
SETTLEMENT = ROOT / "reports/overseas_deep/S5_3_OFFICIAL_SETTLEMENT_2026-08-22.json"
AUDIT = ROOT / "reports/overseas_deep/S5_3_PUBLIC_RPR_TS_EXPLORATORY_AUDIT_2026-08-22.json"
REPORT = ROOT / "reports/overseas_deep/S5_3_EXPLORATORY_BACKTEST_LEDGER_APPEND_2026-08-22.json"
STUDY_ID = "overseas_public_rpr_ts_pl_50000_v1"
METHODOLOGY = "rpr_ts_equal_minmax_softmax_pl_50000_four_place_uncalibrated"
# Existing ledger schema restricts scope to this value; methodology_version preserves the distinct probabilistic RPR/TS cohort.
SCOPE = "ranking_only_uncalibrated"


def sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canon_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS exploratory_backtest_studies(
        study_id TEXT PRIMARY KEY, methodology_version TEXT NOT NULL,
        scope TEXT NOT NULL, created_at_utc TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS exploratory_backtest_events(
        study_id TEXT NOT NULL, event_key TEXT NOT NULL, meeting_date TEXT NOT NULL,
        simulcast_code TEXT NOT NULL, race_no INTEGER NOT NULL, scheduled_hkt TEXT NOT NULL,
        going TEXT NOT NULL, active_runners INTEGER NOT NULL, method_version TEXT NOT NULL,
        probability_status TEXT NOT NULL, ev_status TEXT NOT NULL, kelly_status TEXT NOT NULL,
        pre_race_path TEXT NOT NULL, pre_race_sha256 TEXT NOT NULL,
        settlement_path TEXT NOT NULL, settlement_sha256 TEXT NOT NULL,
        audit_path TEXT NOT NULL, audit_sha256 TEXT NOT NULL,
        official_top4_json TEXT NOT NULL, pre_race_rank_json TEXT NOT NULL,
        top1_hit INTEGER NOT NULL, top3_contains_winner INTEGER NOT NULL,
        top4_overlap_count INTEGER NOT NULL, record_sha256 TEXT NOT NULL,
        inserted_at_utc TEXT NOT NULL,
        PRIMARY KEY(study_id,event_key)
    )""")
    existing = conn.execute("SELECT methodology_version,scope FROM exploratory_backtest_studies WHERE study_id=?", (STUDY_ID,)).fetchone()
    if existing is None:
        conn.execute("INSERT INTO exploratory_backtest_studies VALUES(?,?,?,?)", (STUDY_ID, METHODOLOGY, SCOPE, datetime.now(timezone.utc).isoformat(timespec="seconds")))
    elif tuple(existing) != (METHODOLOGY, SCOPE):
        raise RuntimeError("研究cohort方法或範圍不一致；拒絕混合。")


def main() -> int:
    pre = json.loads(PRE.read_text(encoding="utf-8"))
    settlement = json.loads(SETTLEMENT.read_text(encoding="utf-8"))
    audit = json.loads(AUDIT.read_text(encoding="utf-8"))
    race = pre["race"]
    official = [int(x["horse_no"]) for x in settlement["official_top4"]]
    if race["simulcast"] != "S5-3" or race["place_dividends"] != 4 or race["active_runners"] != 22:
        raise ValueError("不是S5-3 22匹4位置封存工件；拒絕追加。")
    if official != [8, 9, 1, 18]:
        raise ValueError("官方結算前四與已核實結果不一致；拒絕追加。")
    if [int(x["horse_no"]) for x in audit["official_top4"]] != official:
        raise ValueError("覆盤與官方結算前四不一致；拒絕追加。")
    record = {
        "study_id": STUDY_ID, "event_key": f"{race['date']}:{race['simulcast']}",
        "meeting_date": race["date"], "simulcast_code": "S5", "race_no": 3,
        "scheduled_hkt": race["scheduled_hkt"], "going": race["going_hkjc"],
        "active_runners": int(race["active_runners"]), "method_version": METHODOLOGY,
        # The legacy ledger CHECK accepts this N/A marker; detailed probabilistic metrics stay in the immutable audit JSON.
        "probability_status": "N/A_uncalibrated_ranking_only", "ev_status": "N/A", "kelly_status": "N/A",
        "pre_race_path": str(PRE), "pre_race_sha256": sha256_path(PRE),
        "settlement_path": str(SETTLEMENT), "settlement_sha256": sha256_path(SETTLEMENT),
        "audit_path": str(AUDIT), "audit_sha256": sha256_path(AUDIT),
        "official_top4": official, "pre_race_rank": [int(v) for v in audit["pre_race_nr_order"]],
        "top1_hit": bool(audit["top1_hit"]), "top3_contains_winner": bool(audit["top3_contains_winner"]),
        "top4_overlap_count": int(audit["top4_actual_top4_overlap_count"]),
    }
    record["record_sha256"] = canon_hash(record)
    conn = sqlite3.connect(DB)
    try:
        init_db(conn)
        old = conn.execute("SELECT record_sha256 FROM exploratory_backtest_events WHERE study_id=? AND event_key=?", (STUDY_ID, record["event_key"])).fetchone()
        if old is not None:
            if old[0] != record["record_sha256"]:
                raise RuntimeError("同一cohort與event_key雜湊不一致；拒絕覆寫。")
            outcome = "already_recorded"
        else:
            conn.execute("""INSERT INTO exploratory_backtest_events(
                study_id,event_key,meeting_date,simulcast_code,race_no,scheduled_hkt,going,active_runners,method_version,
                probability_status,ev_status,kelly_status,pre_race_path,pre_race_sha256,settlement_path,settlement_sha256,
                audit_path,audit_sha256,official_top4_json,pre_race_rank_json,top1_hit,top3_contains_winner,top4_overlap_count,
                record_sha256,inserted_at_utc
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                record["study_id"], record["event_key"], record["meeting_date"], record["simulcast_code"], record["race_no"],
                record["scheduled_hkt"], record["going"], record["active_runners"], record["method_version"],
                record["probability_status"], record["ev_status"], record["kelly_status"], record["pre_race_path"], record["pre_race_sha256"],
                record["settlement_path"], record["settlement_sha256"], record["audit_path"], record["audit_sha256"],
                json.dumps(record["official_top4"]), json.dumps(record["pre_race_rank"]), int(record["top1_hit"]),
                int(record["top3_contains_winner"]), record["top4_overlap_count"], record["record_sha256"],
                datetime.now(timezone.utc).isoformat(timespec="seconds")
            ))
            conn.commit()
            outcome = "appended"
    finally:
        conn.close()
    REPORT.write_text(json.dumps({"status": outcome, "database": str(DB), "record": record}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": outcome, "event_key": record["event_key"], "study_id": STUDY_ID, "record_sha256": record["record_sha256"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
