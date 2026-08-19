#!/usr/bin/env python3
"""Regression checks for official HKJC overseas market research integration."""
from __future__ import annotations

import math

from enrich_overseas_deep_hkjc_market import enrich, parse_rendered_hkjc_text
from fetch_hkjc_live_odds import parse_visible_odds_table
from fetch_overseas_deep_data import norm

TEXT = """No.\tColour\tHorse Name\tDraw\tWt.\tJockey\tTrainer\tWin\tPlace\tWin & Place
1\n\tAlpha (IRE)\t1\t120\tJ\tT\n4.0\n1.8\n2\n\tBravo (GB)\t2\t120\tJ\tT\n8.0\n2.6\n3\n\tCharlie (IRE)\n(SCR)\nSCR\nSCR\nF\n\tField\n"""

HTML = """
<table><tr><th>No.</th><th>Horse Name</th><th>Win</th><th>Place</th></tr>
<tr><td>1</td><td>Alpha (IRE)</td><td>4.0</td><td>1.8</td></tr>
<tr><td>2</td><td>Bravo (GB)</td><td>8.0</td><td>2.6</td></tr>
<tr><td>F</td><td>Field</td><td></td><td></td></tr></table>
"""


def payload() -> dict:
    return {
        "race": {"meeting_date": "2026-08-19", "simulcast_code": "S1", "race_no": 1},
        "n6_integration": {"status": "disabled_non_hk"},
        "starters": [
            {"runner_no": 1, "horse_name": "Alpha", "deep_composite_score": 72.0},
            {"runner_no": 2, "horse_name": "Bravo", "deep_composite_score": 46.0},
        ],
    }


def main() -> int:
    odds, metadata = parse_visible_odds_table(HTML)
    text_odds, text_metadata = parse_rendered_hkjc_text(TEXT)
    assert {norm(name): values for name, values in text_odds.items()} == {norm(name): values for name, values in odds.items()}, (text_odds, odds)
    assert text_metadata["scratched_rows"] == 1, text_metadata
    assert set(odds) == {"Alpha(IRE)", "Bravo(GB)"}, odds
    assert metadata["rows_parsed"] == 2, metadata
    complete = enrich(payload(), odds, "https://bet.hkjc.com/en/racing/wp/2026-08-19/S1/1", "2026-08-19T09:00:00+00:00", 2, 5000, 7, 0.05, [])
    market = complete["market_research"]
    assert market["status"] == "complete", market
    assert market["matched_runner_count"] == 2, market
    assert math.isclose(market["probability_sum"], 1.0, abs_tol=1e-12), market
    assert all(row["market_research"]["ev_kelly_status"] == "available_research_only" for row in complete["starters"]), complete
    assert all(0 <= row["market_research"]["kelly_fraction"] <= 0.05 for row in complete["starters"]), complete
    mismatched = enrich(payload(), {"Unknown": {"win": 3.0, "place": 1.5}}, "official", "2026-08-19T09:00:00+00:00", 2, 5000, 7, 0.05, [])
    assert mismatched["market_research"]["status"] == "identity_mismatch", mismatched
    assert all(row["market_research"]["win_ev"] is None for row in mismatched["starters"]), mismatched
    print("PASS: HKJC English HTML/text W/P parser, SCR exclusion, full identity match, probability conservation, EV/Kelly and mismatch stop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
