#!/usr/bin/env python3
"""Validate V10.1 dual-market prediction output integrity."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="核對 V10.1 位置機率、EV 及凱利輸出")
    parser.add_argument("--prediction", required=True)
    parser.add_argument("--output", default="place_prediction_quality_report.json")
    args = parser.parse_args()
    payload = json.loads(Path(args.prediction).read_text(encoding="utf-8"))
    rows = payload["predictions"]
    place_positions = int(payload["place_model"]["dividend_positions"])
    place_sum = sum(float(row["predicted_place_probability"]) for row in rows)
    failures: list[str] = []
    if not math.isclose(place_sum, float(place_positions), rel_tol=0.0, abs_tol=1e-10):
        failures.append(f"位置機率總和 {place_sum} 不等於派彩名次 {place_positions}")
    for row in rows:
        p = float(row["predicted_place_probability"])
        odds = row["place_market_odds"]
        ev = row["place_ev_per_unit"]
        full = float(row["place_kelly_full_fraction"])
        quarter = float(row["place_kelly_quarter_fraction_capped"])
        if not 0 <= p <= 1:
            failures.append(f"{row['horse_name']} 的位置機率超出 [0,1]")
        if odds is None:
            if ev is not None or full != 0.0 or quarter != 0.0:
                failures.append(f"{row['horse_name']} 無位置賠率時 EV 或 Kelly 非空")
        else:
            expected_ev = p * float(odds) - 1.0
            if not math.isclose(float(ev), expected_ev, rel_tol=0.0, abs_tol=1e-12):
                failures.append(f"{row['horse_name']} 的位置 EV 不符合 p×odds−1")
            expected_full = max(0.0, expected_ev / (float(odds) - 1.0))
            expected_quarter = min(0.05, expected_full * 0.25) if expected_full > 0 else 0.0
            if not math.isclose(full, expected_full, rel_tol=0.0, abs_tol=1e-12):
                failures.append(f"{row['horse_name']} 的位置 Kelly 不一致")
            if not math.isclose(quarter, expected_quarter, rel_tol=0.0, abs_tol=1e-12):
                failures.append(f"{row['horse_name']} 的四分之一位置 Kelly 不一致")
    report = {
        "result": "PASS" if not failures else "FAIL",
        "runner_count": len(rows),
        "place_dividend_positions": place_positions,
        "place_probability_sum": place_sum,
        "matched_place_odds": payload.get("market_overlays", {}).get("matched_place_odds"),
        "failures": failures,
    }
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
