#!/usr/bin/env python3
"""Regression tests for V10.1 odds, simulation and sparse-track-bias guardrails."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

from build_elo_features import shrunk_track_bias
from predict import coerce_usable_odds, plackett_luce_place_probability

FIXTURE_HTML = """<!doctype html><html><body><table>
<tr><td>馬號</td><td>馬名</td><td>獨贏</td><td>位置</td></tr>
<tr><td>1</td><td>正常馬</td><td>4.5</td><td>1.8</td></tr>
<tr><td>2</td><td>零值馬</td><td>0</td><td>2.3</td></tr>
<tr><td>3</td><td>退出馬</td><td>SCR</td><td>SCR</td></tr>
<tr><td>4</td><td>空值馬</td><td>8.0</td><td></td></tr>
</table></body></html>"""
RACE_CARD = {
    "runners": [
        {"horse_name": "正常馬"}, {"horse_name": "零值馬"},
        {"horse_name": "退出馬"}, {"horse_name": "空值馬"},
    ]
}


def assert_true(condition: bool, message: str, failures: list[str]) -> None:
    if not condition:
        failures.append(message)


def main() -> int:
    parser = argparse.ArgumentParser(description="V10.1 防禦式邊界與效能測試")
    parser.add_argument("--project-dir", default=str(Path(__file__).parent))
    parser.add_argument("--max-simulation-seconds", type=float, default=0.5)
    parser.add_argument("--output", default="v101_hardening_test_report.json")
    args = parser.parse_args()
    project = Path(args.project_dir).resolve()
    failures: list[str] = []
    results: dict[str, object] = {}

    # 1. 100k vectorized simulation, deliberately use a 20-runner max-field case.
    win_prob = np.linspace(1.0, 20.0, 20, dtype=float)
    win_prob /= win_prob.sum()
    place_prob, _, elapsed = plackett_luce_place_probability(win_prob, 4, 100_000, seed=7)
    results["place_simulation_seconds"] = elapsed
    results["place_probability_sum"] = float(place_prob.sum())
    assert_true(elapsed <= args.max_simulation_seconds, f"100k 模擬耗時 {elapsed:.6f}s，超過 {args.max_simulation_seconds:.3f}s", failures)
    assert_true(np.isclose(place_prob.sum(), 4.0), "位置機率總和不等於 4 個派彩名次", failures)

    # 2. Sparse / extreme track contexts should stay near neutral and always bounded.
    tiny_one_win = shrunk_track_bias(wins=1.0, expected_wins=1.0 / 12.0, runners=1.0)
    tiny_zero_win = shrunk_track_bias(wins=0.0, expected_wins=1.0 / 12.0, runners=1.0)
    large_extreme = shrunk_track_bias(wins=200.0, expected_wins=20.0, runners=240.0)
    results["track_bias_tiny_one_win"] = tiny_one_win
    results["track_bias_tiny_zero_win"] = tiny_zero_win
    results["track_bias_large_extreme"] = large_extreme
    assert_true(abs(tiny_one_win - 1.0) < 0.03, "單一極端樣本未充分收縮至 1.0", failures)
    assert_true(abs(tiny_zero_win - 1.0) < 0.03, "單一零勝樣本未充分收縮至 1.0", failures)
    assert_true(0.75 <= large_extreme <= 1.25, "大量樣本的極端偏差未受界限保護", failures)
    assert_true(shrunk_track_bias(0.0, 0.0, 0.0) == 1.0, "零樣本偏差沒有回歸 1.0", failures)

    # 3. Offline scraper: 0, SCR and blank should yield valid JSON nulls, not exceptions.
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fixture = root / "fixture.html"
        card = root / "race_card.json"
        win_out = root / "win.json"
        place_out = root / "place.json"
        combined_out = root / "combined.json"
        meta_out = root / "meta.json"
        fixture.write_text(FIXTURE_HTML, encoding="utf-8")
        card.write_text(json.dumps(RACE_CARD, ensure_ascii=False), encoding="utf-8")
        command = [
            sys.executable, str(project / "fetch_hkjc_live_odds.py"), "--html", str(fixture),
            "--race-card", str(card), "--output", str(win_out), "--place-output", str(place_out),
            "--combined-output", str(combined_out), "--metadata-output", str(meta_out),
        ]
        completed = subprocess.run(command, text=True, capture_output=True, check=False, timeout=30)
        assert_true(completed.returncode == 0, f"容錯賠率抓取器返回 {completed.returncode}: {completed.stderr}", failures)
        win = json.loads(win_out.read_text(encoding="utf-8"))
        place = json.loads(place_out.read_text(encoding="utf-8"))
        meta = json.loads(meta_out.read_text(encoding="utf-8"))
        results["odds_scraper_status"] = meta["status"]
        results["odds_scraper_pairs"] = meta["complete_win_place_pairs"]
        assert_true(win == {"正常馬": 4.5, "零值馬": None, "退出馬": None, "空值馬": 8.0}, "獨贏空值／SCR 降級輸出不正確", failures)
        assert_true(place == {"正常馬": 1.8, "零值馬": 2.3, "退出馬": None, "空值馬": None}, "位置空值／SCR 降級輸出不正確", failures)
        assert_true(meta["status"] == "degraded", "含空值／SCR 時 metadata 未標示 degraded", failures)

    # 4. predict.py utility must regard malformed live odds as no market odds.
    malformed_inputs = [None, 0, "0", "SCR", "WV", "", "N/A", float("nan"), 1.0]
    assert_true(all(coerce_usable_odds(value) is None for value in malformed_inputs), "predict.py 未把異常賠率轉為 None", failures)
    assert_true(coerce_usable_odds(4.5) == 4.5, "有效賠率未被保留", failures)

    report = {"result": "PASS" if not failures else "FAIL", "failures": failures, **results}
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
