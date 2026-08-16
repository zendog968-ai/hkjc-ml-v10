"""Isolated contract checks for V10.2 overseas archiving, audit and risk guidance.

The HTML and prices below are labelled synthetic fixtures.  The test validates
parser/storage/report contracts; it is not a backtest or evidence of performance.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from filter_high_probability import run as run_filter
from overseas_hkjc_core import apply_results, parse_results
from race_risk_guidance import build_race_guidance

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "overseas_archive_audit_guidance_fixture"

RESULT_HTML = """
<html><body>
<table>
  <tr><th>Pla.</th><th>Horse No</th><th>Horse</th><th>Jockey</th><th>Trainer</th><th>Weight</th><th>Draw</th><th>Margin</th><th>Finish Time</th><th>Win Odds</th><th>Place Odds</th></tr>
  <tr><td>1</td><td>2</td><td>Official Winner</td><td>J. Official</td><td>T. Official</td><td>126</td><td>3</td><td>0</td><td>1:09.10</td><td>5.0</td><td>1.8</td></tr>
  <tr><td>2</td><td>1</td><td>Official Second</td><td>J. Runner</td><td>T. Runner</td><td>128</td><td>1</td><td>1 1/4</td><td>1:09.30</td><td>4.0</td><td>1.5</td></tr>
  <tr><td>3</td><td>3</td><td>Official Third</td><td>J. Third</td><td>T. Third</td><td>130</td><td>6</td><td>Neck</td><td>1:09.35</td><td>20.0</td><td>4.2</td></tr>
</table>
<table>
  <tr><th>Pool</th><th>Winning Combination</th><th>Dividend</th></tr>
  <tr><td>WIN</td><td>2</td><td>50.0</td></tr>
  <tr><td>PLACE</td><td>2</td><td>18.0</td></tr>
</table>
</body></html>
"""


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir()
    db_path = OUT / "fixture.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript((ROOT / "schema_overseas_racing.sql").read_text(encoding="utf-8"))
    conn.execute("INSERT INTO overseas_meetings(meeting_date,simulcast_code,fixture_url,discovery_status,discovered_at_utc) VALUES(?,?,?,?,?)", ("2026-08-16", "S1", "fixture://official", "discovered", "2026-08-16T00:00:00+00:00"))
    meeting_id = int(conn.execute("SELECT meeting_id FROM overseas_meetings").fetchone()[0])
    conn.execute("INSERT INTO overseas_races(meeting_id,race_no,official_race_key,race_status,result_url) VALUES(?,?,?,?,?)", (meeting_id, 1, "2026-08-16:S1:1", "discovered", "fixture://result"))
    race_id = int(conn.execute("SELECT overseas_race_id FROM overseas_races").fetchone()[0])
    starters, dividends = parse_results(RESULT_HTML)
    assert len(starters) == 3 and len(dividends) == 2
    assert starters[0]["final_win_odds"] == 5.0 and starters[1]["margin_lengths"] == 1.25 and starters[2]["margin_lengths"] == 0.3
    apply_results(conn, race_id, starters, dividends, "fixture://result")
    stored = conn.execute("SELECT horse_name,jockey,trainer,weight_lbs,draw,finish_pos,margin_lengths,finish_time,final_win_odds,final_place_odds FROM overseas_starters WHERE overseas_race_id=? AND horse_no=2", (race_id,)).fetchone()
    race_status = conn.execute("SELECT race_status FROM overseas_races WHERE overseas_race_id=?", (race_id,)).fetchone()[0]
    conn.commit()
    conn.close()
    assert stored == ("Official Winner", "J. Official", "T. Official", 126.0, 3, 1, 0.0, "1:09.10", 5.0, 1.8)
    assert race_status == "completed"

    predictions = {
        "predictions": [
            {"horse_no": 1, "horse_name": "Official Second", "predicted_win_probability": 0.50, "predicted_place_probability": 0.90, "market_odds": 4.0, "place_market_odds": 1.5},
            {"horse_no": 2, "horse_name": "Official Winner", "predicted_win_probability": 0.30, "predicted_place_probability": 0.85, "market_odds": 12.0, "place_market_odds": 4.0},
            {"horse_no": 3, "horse_name": "Official Third", "predicted_win_probability": 0.20, "predicted_place_probability": 0.70, "market_odds": 20.0, "place_market_odds": 4.2},
        ]
    }
    prediction_path = OUT / "prediction.json"
    prediction_path.write_text(json.dumps(predictions), encoding="utf-8")
    subprocess.run([
        sys.executable, str(ROOT / "post_race_audit.py"), "--scope", "overseas", "--race-key", "2026-08-16:S1:1",
        "--db", str(db_path), "--schema", str(ROOT / "schema_overseas_racing.sql"), "--prediction-json", str(prediction_path), "--report-dir", str(OUT / "reports"),
    ], cwd=ROOT, check=True)
    conn = sqlite3.connect(db_path)
    audit = conn.execute("SELECT top1_hit,top3_contains_winner,stable_strategy_hit,value_strategy_hit,settled_stake,settled_net_return,roi,brier_score,status FROM post_race_audits WHERE audit_scope='overseas' AND race_key='2026-08-16:S1:1'").fetchone()
    conn.close()
    assert audit is not None
    assert audit[0:4] == (0, 1, 1, 1)
    assert audit[4] == 4.0 and audit[5] == 6.0 and abs(audit[6] - 1.5) < 1e-12
    assert abs(audit[7] - 0.78) < 1e-12 and audit[8] == "audited"
    report = (OUT / "reports" / "overseas_2026-08-16_S1_1.md").read_text(encoding="utf-8")
    assert "Brier Score" in report and "策略結算" in report

    high_dispersion_rows = []
    for horse_no in range(1, 15):
        high_dispersion_rows.append({
            "horse_no": horse_no,
            "horse_name": f"Runner {horse_no}",
            "predicted_win_probability": 0.19 if horse_no == 1 else 0.81 / 13,
            "win_odds": 20.0 if horse_no == 14 else 8.0,
            "win_ev": 0.15 if horse_no == 14 else -0.1,
            "weight_lbs": 128.0 if horse_no == 14 else 132.0,
            "draw": 4 if horse_no == 14 else 8,
        })
    guidance = build_race_guidance(high_dispersion_rows)
    assert guidance["dispersion_warning"] and "不適合作單膽" in guidance["bet_recommendation"]
    assert len(guidance["value_bomb_candidates"]) == 1 and guidance["value_bomb_candidates"][0]["is_positive_model_ev"]
    scan_input = OUT / "scan_prediction.json"
    scan_input.write_text(json.dumps({"race": {"race_date": "2026-08-16", "racecourse": "ST", "race_no": 1}, "predictions": high_dispersion_rows}, ensure_ascii=False), encoding="utf-8")
    scan_output = OUT / "scan_output.json"
    scan_markdown = OUT / "scan_output.md"
    scan = run_filter(str(scan_input), str(scan_output), markdown_output=str(scan_markdown))
    assert scan["race_guidance"]["dispersion_warning"] and scan["whatsapp"]["direct_link"]
    scan_report = scan_markdown.read_text(encoding="utf-8")
    assert "高爆冷風險亂局" in scan_report and "💣 高 EV 冷門" in scan_report

    report_payload = {
        "status": "passed",
        "provenance": "isolated synthetic parser/audit/guidance fixture; not historical performance evidence",
        "checks": {
            "labelled_result_fields_archived": True,
            "incomplete_results_not_silently_completed": True,
            "official_final_win_roi_settlement": True,
            "field_brier_score_written": True,
            "strategy_settlement_reported": True,
            "high_dispersion_single_banker_warning": True,
            "positive_ev_light_weight_or_inside_draw_value_flag": True,
            "warning_and_value_labels_rendered_in_prerace_report": True,
        },
        "audit": {"top1_hit": audit[0], "top3_contains_winner": audit[1], "roi": audit[6], "brier_score": audit[7]},
        "guidance": guidance,
    }
    (OUT / "validation.json").write_text(json.dumps(report_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report_payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
