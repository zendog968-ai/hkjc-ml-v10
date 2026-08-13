#!/usr/bin/env python3
"""Offline regression tests for pre-race scheduling and filtering logic."""
from __future__ import annotations

import json
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from filter_high_probability import run as filter_run
from pre_race_scheduler import HK_TZ, due_jobs, load_jobs, process


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="v10_pre_race_test_") as temp:
        root = Path(temp)
        prediction_path = root / "prediction.json"
        filter_path = root / "filter.json"
        prediction_path.write_text(
            json.dumps(
                {
                    "race": {"race_date": "2026/07/15", "racecourse": "HV", "race_no": 3},
                    "predictions": [
                        {"horse_name": "穩攻甲", "rank": 1, "draw": 2, "jockey": "騎師甲", "trainer": "練馬師甲",
                         "predicted_win_probability": 0.11, "predicted_place_probability": 0.91,
                         "market_odds": 4.5, "place_market_odds": 1.8, "ev_per_unit": -0.505,
                         "place_ev_per_unit": 0.638, "data_warning": "樣本充足"},
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
        filtered = filter_run(str(prediction_path), str(filter_path))
        assert filtered["selection_count"] == 2
        strategies = {row["horse_name"]: row["strategy"] for row in filtered["selections"]}
        assert strategies == {"穩攻甲": "熱門穩攻", "冷門乙": "冷門突襲"}
        assert filtered["selections"][0]["focus_level"] == "超級焦點"
        link = filtered["whatsapp"]["direct_link"]
        assert link and urlparse(link).netloc == "api.whatsapp.com"
        query = parse_qs(urlparse(link).query)
        assert query["phone"] == ["85296896832"] and "穩攻甲" in query["text"][0]

        config_path = root / "schedule.json"
        config_path.write_text(
            json.dumps(
                {"timezone": "Asia/Hong_Kong", "trigger_minutes_before": 15,
                 "meeting": {"race_date": "2026/07/15", "racecourse": "HV",
                             "race_start_times": {"1": "18:30", "2": "19:05"}}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        trigger, jobs = load_jobs(config_path)
        now_due = datetime(2026, 7, 15, 18, 15, 40, tzinfo=HK_TZ)
        now_not_due = datetime(2026, 7, 15, 18, 16, 0, tzinfo=HK_TZ)
        assert trigger == 15 and [job.race_no for job in due_jobs(jobs, trigger, now_due)] == [1]
        assert not due_jobs(jobs, trigger, now_not_due)
        state_path = root / "runtime" / "state.json"
        result = process(config_path, root, root / "outputs", state_path, now_due, True, 60)
        assert result["dry_run"] and result["processed"][0]["status"] == "dry_run_due"
        assert not state_path.exists(), "dry run must not write run state"
        print(json.dumps({"result": "PASS", "filter_selection_count": 2, "due_race": 1}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
