#!/usr/bin/env python3
"""Append immutable ranking-only overseas exploratory backtest records.

This utility never writes V10.2's Hong Kong database, model bundle, N6 artifacts,
or calibrated probabilities.  It stores a separate, explicitly non-probabilistic
cohort for pre-race public ranking snapshots that have an HKJC official settlement.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "overseas_exploratory_backtest.sqlite"
STUDY_ID = "overseas_public_ranking_exploratory_v1"
METHOD_VERSION = "australia_public_nr_ranking_v1"


def sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS exploratory_backtest_studies (
            study_id TEXT PRIMARY KEY,
            methodology_version TEXT NOT NULL,
            scope TEXT NOT NULL CHECK(scope='ranking_only_uncalibrated'),
            created_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS exploratory_backtest_events (
            study_id TEXT NOT NULL,
            event_key TEXT NOT NULL,
            meeting_date TEXT NOT NULL,
            simulcast_code TEXT NOT NULL,
            race_no INTEGER NOT NULL,
            scheduled_hkt TEXT NOT NULL,
            going TEXT,
            active_runners INTEGER NOT NULL,
            method_version TEXT NOT NULL,
            probability_status TEXT NOT NULL CHECK(probability_status='N/A_uncalibrated_ranking_only'),
            ev_status TEXT NOT NULL CHECK(ev_status='N/A'),
            kelly_status TEXT NOT NULL CHECK(kelly_status='N/A'),
            pre_race_path TEXT NOT NULL,
            pre_race_sha256 TEXT NOT NULL,
            settlement_path TEXT NOT NULL,
            settlement_sha256 TEXT NOT NULL,
            audit_path TEXT NOT NULL,
            audit_sha256 TEXT NOT NULL,
            official_top4_json TEXT NOT NULL,
            pre_race_rank_json TEXT NOT NULL,
            top1_hit INTEGER NOT NULL CHECK(top1_hit IN(0,1)),
            top3_contains_winner INTEGER NOT NULL CHECK(top3_contains_winner IN(0,1)),
            top4_overlap_count INTEGER NOT NULL CHECK(top4_overlap_count BETWEEN 0 AND 4),
            record_sha256 TEXT NOT NULL,
            inserted_at_utc TEXT NOT NULL,
            PRIMARY KEY(study_id,event_key),
            FOREIGN KEY(study_id) REFERENCES exploratory_backtest_studies(study_id)
        );
        """
    )
    conn.execute(
        """INSERT INTO exploratory_backtest_studies(study_id,methodology_version,scope,created_at_utc)
           VALUES(?,?,?,?) ON CONFLICT(study_id) DO NOTHING""",
        (STUDY_ID, METHOD_VERSION, "ranking_only_uncalibrated", datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )
    row = conn.execute("SELECT methodology_version,scope FROM exploratory_backtest_studies WHERE study_id=?", (STUDY_ID,)).fetchone()
    if row != (METHOD_VERSION, "ranking_only_uncalibrated"):
        raise RuntimeError("探索性study版本不一致；拒絕混合cohort。")
    conn.commit()


def build_record(pre: dict[str, Any], settlement: dict[str, Any], audit: dict[str, Any], paths: dict[str, Path]) -> dict[str, Any]:
    if pre.get("status") != "public_sources_matched_research_only":
        raise ValueError("賽前工件並非已匹配的公開研究快照。")
    if pre.get("hard_gates", {}).get("formal_ev") != "N/A" or pre.get("hard_gates", {}).get("kelly") != "N/A":
        raise ValueError("只允許正式EV及Kelly均為N/A的排名型探索紀錄。")
    if settlement.get("settlement_status") != "official_confirmed":
        raise ValueError("只允許HKJC官方已確認的結算。")
    if audit.get("probability_metrics", "").split(":", 1)[0] != "N/A":
        raise ValueError("排名型探索紀錄不可含機率指標。")
    race = pre["race"]
    expected = [int(x) for x in audit["official_top4"]]
    official = [int(x["horse_no"]) for x in settlement["official_top4"]]
    if expected != official:
        raise ValueError("覆盤與官方結算前四名不一致。")
    ranks = [int(x) for x in audit["pre_race_nr_order"]]
    event_key = f'{race["date"]}:{race["simulcast"]}'
    record = {
        "study_id": STUDY_ID,
        "event_key": event_key,
        "meeting_date": race["date"],
        "simulcast_code": race["simulcast"].split("-")[0],
        "race_no": int(race["simulcast"].split("-")[1]),
        "scheduled_hkt": race["scheduled_hkt"],
        "going": race["going"],
        "active_runners": int(race["active_runners"]),
        "method_version": METHOD_VERSION,
        "probability_status": "N/A_uncalibrated_ranking_only",
        "ev_status": "N/A",
        "kelly_status": "N/A",
        "pre_race_path": str(paths["pre"]),
        "pre_race_sha256": sha256_path(paths["pre"]),
        "settlement_path": str(paths["settlement"]),
        "settlement_sha256": sha256_path(paths["settlement"]),
        "audit_path": str(paths["audit"]),
        "audit_sha256": sha256_path(paths["audit"]),
        "official_top4": official,
        "pre_race_rank": ranks,
        "top1_hit": bool(audit["top1_hit"]),
        "top3_contains_winner": bool(audit["top3_contains_winner"]),
        "top4_overlap_count": int(audit["top4_actual_top4_overlap_count"]),
    }
    immutable = dict(record)
    record["record_sha256"] = hashlib.sha256(canonical_bytes(immutable)).hexdigest()
    return record


def append_record(db: Path, record: dict[str, Any]) -> str:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    try:
        init_db(conn)
        existing = conn.execute(
            "SELECT record_sha256 FROM exploratory_backtest_events WHERE study_id=? AND event_key=?",
            (record["study_id"], record["event_key"]),
        ).fetchone()
        if existing:
            if existing[0] != record["record_sha256"]:
                raise RuntimeError("同一event_key的不可變紀錄雜湊不一致；拒絕覆寫。")
            return "already_recorded"
        conn.execute(
            """INSERT INTO exploratory_backtest_events(
                study_id,event_key,meeting_date,simulcast_code,race_no,scheduled_hkt,going,active_runners,method_version,
                probability_status,ev_status,kelly_status,pre_race_path,pre_race_sha256,settlement_path,settlement_sha256,
                audit_path,audit_sha256,official_top4_json,pre_race_rank_json,top1_hit,top3_contains_winner,top4_overlap_count,
                record_sha256,inserted_at_utc
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                record["study_id"], record["event_key"], record["meeting_date"], record["simulcast_code"], record["race_no"],
                record["scheduled_hkt"], record["going"], record["active_runners"], record["method_version"],
                record["probability_status"], record["ev_status"], record["kelly_status"], record["pre_race_path"],
                record["pre_race_sha256"], record["settlement_path"], record["settlement_sha256"], record["audit_path"],
                record["audit_sha256"], json.dumps(record["official_top4"]), json.dumps(record["pre_race_rank"]),
                int(record["top1_hit"]), int(record["top3_contains_winner"]), record["top4_overlap_count"],
                record["record_sha256"], datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        return "appended"
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="追加海外公開排名探索性回測紀錄")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--pre", default="reports/overseas_deep/S4_7_VERIFIED_PUBLIC_RESEARCH_2026-08-22.json")
    parser.add_argument("--settlement", default="reports/overseas_deep/S4_7_OFFICIAL_SETTLEMENT_2026-08-22.json")
    parser.add_argument("--audit", default="reports/overseas_deep/S4_7_PUBLIC_NR_EXPLORATORY_AUDIT_2026-08-22.json")
    parser.add_argument("--report", default="reports/overseas_deep/S4_7_EXPLORATORY_BACKTEST_LEDGER_APPEND_2026-08-22.json")
    args = parser.parse_args()
    paths = {key: ROOT / value for key, value in {"pre": args.pre, "settlement": args.settlement, "audit": args.audit}.items()}
    pre, settlement, audit = (json.loads(paths[key].read_text(encoding="utf-8")) for key in ("pre", "settlement", "audit"))
    record = build_record(pre, settlement, audit, paths)
    outcome = append_record(ROOT / args.db, record)
    report = {"status": outcome, "database": str(ROOT / args.db), "record": record}
    (ROOT / args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": outcome, "event_key": record["event_key"], "record_sha256": record["record_sha256"], "database": str(ROOT / args.db)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
