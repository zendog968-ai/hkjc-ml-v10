#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "ingest_oncc_json.py"
SAMPLE = {
    "race_date": "2026-09-06",
    "race_no": 1,
    "horse_no": 1,
    "horse_name": "雙子美麗",
    "trainer": "告東尼",
    "jockey": "鍾易禮",
    "morning_trackwork": {"comment": "步幅輕快，晨操狀態穩定", "condition_score": 0.8},
    "expert_tips": {"recommend_count": 0},
    "barrier_trial": {"observation": "順走完成試閘", "effort_level": "EASY", "surge_rating": "STRONG"},
    "stable_intel": {"report": "馬房評語正面", "ambition_flag": True},
}

with tempfile.TemporaryDirectory(prefix="oncc_ingest_test_") as td:
    root = Path(td)
    input_path = root / "oncc_2026-09-06.json"
    db_path = root / "oncc_extension.sqlite"
    input_path.write_text(json.dumps(SAMPLE, ensure_ascii=False), encoding="utf-8")
    first = subprocess.run([sys.executable, str(SCRIPT), "--input", str(input_path), "--db", str(db_path)], text=True, capture_output=True)
    assert first.returncode == 0, first.stderr
    assert '"rows_ingested": 1' in first.stdout
    second = subprocess.run([sys.executable, str(SCRIPT), "--input", str(input_path), "--db", str(db_path)], text=True, capture_output=True)
    assert second.returncode == 0, second.stderr
    with sqlite3.connect(db_path) as con:
        row = con.execute("SELECT race_date,race_no,horse_no,horse_name,trainer,jockey,trackwork_comment,condition_score,expert_recommend_count,trial_observation,effort_level,surge_rating,stable_report,ambition_flag FROM oncc_qualitative_features").fetchone()
        count = con.execute("SELECT COUNT(*) FROM oncc_qualitative_features").fetchone()[0]
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        assert count == 1
        assert integrity == "ok"
        assert row == ("2026-09-06", 1, 1, "雙子美麗", "告東尼", "鍾易禮", "步幅輕快，晨操狀態穩定", 0.8, 0, "順走完成試閘", "EASY", "STRONG", "馬房評語正面", 1)
    formal_guard = subprocess.run([sys.executable, str(SCRIPT), "--input", str(input_path), "--db", str(root / "hkjc_last_season.sqlite")], text=True, capture_output=True)
    assert formal_guard.returncode == 2
    print({"status": "ok", "rows": count, "integrity_check": integrity, "idempotent_second_ingest": True, "formal_db_guard": True, "n6_status": "disabled_non_hk"})
