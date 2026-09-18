from __future__ import annotations
import importlib.util
import json
import sqlite3
from pathlib import Path

spec = importlib.util.spec_from_file_location("pipeline", "sync_racing_pipeline.py")
mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(mod)
html = Path("phase1_oncc_fixture.html").read_text(encoding="utf-8")
parsed = mod.parse_qualitative_from_html(html, 7, "測試良駒")
print("QUALITATIVE=" + json.dumps(parsed, ensure_ascii=False, sort_keys=True))
assert parsed["morning_trackwork"]["condition_score"] == 0.85
assert parsed["expert_tips"]["recommend_count"] >= 1
assert parsed["barrier_trial"]["effort_level"] == "EASY"
assert parsed["barrier_trial"]["surge_rating"] == "STRONG"
assert parsed["stable_intel"]["ambition_flag"] is True
record = {"race_date": "2026-09-06", "race_no": 1, "horse_no": 7, "horse_name": "測試良駒"}
out = mod.attach_qualitative_data([record], "2026-09-06", {1: html})
assert out[0]["stable_intel"]["ambition_flag"] is True

spec2 = importlib.util.spec_from_file_location("audit", "post_race_brier_audit.py")
audit = importlib.util.module_from_spec(spec2)
assert spec2.loader is not None
spec2.loader.exec_module(audit)
report = audit.audit(Path("phase1_predictions_fixture.json"), Path("phase1_results_fixture.json"), Path("phase1_test_output/brier_report.json"), Path("phase1_test_output/brier_audit.jsonl"))
print("BRIER=" + json.dumps({k: report[k] for k in ("race_count", "scored_race_count", "not_scored_race_count", "daily_brier_score")}, ensure_ascii=False))
assert report["race_count"] == 1
assert report["scored_race_count"] == 1
assert report["not_scored_race_count"] == 0
assert abs(report["daily_brier_score"] - 0.38) < 1e-12

with sqlite3.connect("runtime/oncc_extension/oncc_extension.sqlite") as conn:
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("select count(*) from oncc_qualitative_features").fetchone()[0] == 120
print("SQLITE=integrity_ok rows=120")
print("ISOLATION=passed")
