#!/usr/bin/env python3
"""Offline regression tests for pre-race scheduling, reports, filtering, and action gate."""
from __future__ import annotations

import json
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from filter_high_probability import run as filter_run
from github_race_day_gate import load_schedule as load_action_schedule, select_due_job
from pre_race_scheduler import HK_TZ, due_stages, load_jobs, process


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="v10_pre_race_test_") as temp:
        root = Path(temp)
        prediction_path = root / "prediction.json"
        filter_path = root / "filter.json"
        markdown_path = root / "report.md"
        prediction_path.write_text(
            json.dumps(
                {
                    "race": {"race_date": "2026/07/15", "racecourse": "HV", "race_no": 3},
                    "predictions": [
                        {"horse_name": "穩攻甲", "rank": 1, "draw": 2, "jockey": "騎師甲", "trainer": "練馬師甲",
                         "predicted_win_probability": 0.11, "predicted_place_probability": 0.91,
                         "market_odds": 4.5, "place_market_odds": 1.8, "ev_per_unit": -0.505,
                         "place_ev_per_unit": 0.638, "odds_drop_ratio": -0.25, "gate_money_drop_flag": True,
                         "market_movement_label": "🔥 閘前資金落飛", "data_warning": "樣本充足"},
                        {"horse_name": "冷門乙", "rank": 4, "draw": 9, "jockey": "騎師乙", "trainer": "練馬師乙",
                         "predicted_win_probability": 0.085, "predicted_place_probability": 0.81,
                         "market_odds": 12.0, "place_market_odds": 3.6, "ev_per_unit": 0.02,
                         "place_ev_per_unit": 1.916, "data_warning": "樣本充足"},
                        {"horse_name": "未達標丙", "rank": 5, "draw": 7, "jockey": "騎師丙", "trainer": "練馬師丙",
                         "predicted_win_probability": 0.079, "predicted_place_probability": 0.79,
                         "market_odds": 11.0, "place_market_odds": 3.7, "ev_per_unit": -0.131,
                         "place_ev_per_unit": 1.923, "data_warning": "樣本充足"},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        filtered = filter_run(str(prediction_path), str(filter_path), markdown_output=str(markdown_path))
        assert filtered["selection_count"] == 2
        assert filtered["selection_counts"] == {"熱門穩攻": 1, "冷門突襲 / Value Bomb": 1}
        assert filtered["strategies"]["熱門穩攻"][0]["horse_name"] == "穩攻甲"
        assert filtered["strategies"]["熱門穩攻"][0]["focus_level"] == "超級焦點"
        assert filtered["strategies"]["冷門突襲 / Value Bomb"][0]["horse_name"] == "冷門乙"
        link = filtered["whatsapp"]["direct_link"]
        assert link and urlparse(link).netloc == "api.whatsapp.com"
        query = parse_qs(urlparse(link).query)
        assert query["phone"] == ["85296896832"] and "穩攻甲" in query["text"][0] and "冷門乙" in query["text"][0]
        markdown = markdown_path.read_text(encoding="utf-8")
        assert "## 熱門穩攻" in markdown and "## 冷門突襲 / Value Bomb" in markdown and "WhatsApp" in markdown and "閘前資金落飛" in markdown

        config_path = root / "schedule.json"
        config_path.write_text(
            json.dumps(
                {"timezone": "Asia/Hong_Kong", "snapshot_minutes_before": [15, 5],
                 "meeting": {"race_date": "2026/07/15", "racecourse": "HV",
                             "race_start_times": {"1": "18:30", "2": "19:05"}}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        offsets, jobs = load_jobs(config_path)
        now_t15 = datetime(2026, 7, 15, 18, 15, 40, tzinfo=HK_TZ)
        now_t5 = datetime(2026, 7, 15, 18, 25, 0, tzinfo=HK_TZ)
        now_not_due = datetime(2026, 7, 15, 18, 16, 0, tzinfo=HK_TZ)
        assert offsets == (15, 5) and [(job.race_no, offset) for job, offset in due_stages(jobs, offsets, now_t15)] == [(1, 15)]
        assert [(job.race_no, offset) for job, offset in due_stages(jobs, offsets, now_t5)] == [(1, 5)]
        assert not due_stages(jobs, offsets, now_not_due)
        state_path = root / "runtime" / "state.json"
        result = process(config_path, root, root / "outputs", state_path, now_t15, True, 60)
        assert result["dry_run"] and result["processed"][0]["status"] == "dry_run_due" and result["processed"][0]["stage"] == "T_MINUS_15"
        assert not state_path.exists(), "dry run must not write run state"

        action_config = root / "action_schedule.json"
        action_config.write_text(
            json.dumps(
                {"timezone": "Asia/Hong_Kong", "trigger_minutes_before": 60, "trigger_window_minutes": 10,
                 "meeting": {"race_date": "2026/07/15", "racecourse": "HV", "race_start_times": {"1": "18:30"}}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        action_payload, action_jobs = load_action_schedule(action_config)
        gate_due = select_due_job(action_payload, action_jobs, datetime(2026, 7, 15, 17, 30, tzinfo=HK_TZ))
        gate_not_due = select_due_job(action_payload, action_jobs, datetime(2026, 7, 15, 17, 41, tzinfo=HK_TZ))
        assert gate_due["should_run"] == "true" and gate_due["race_no"] == "1"
        assert gate_not_due["should_run"] == "false"
        print(json.dumps({"result": "PASS", "filter_selection_count": 2, "due_race": 1, "github_gate": "PASS"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
