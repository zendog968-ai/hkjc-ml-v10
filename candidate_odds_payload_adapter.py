#!/usr/bin/env python3
"""Offline-only adapter for a validated public GraphQL payload."""
from __future__ import annotations

import json
import math
from typing import Any


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def parse_pm_pools(payload: dict[str, Any], race_no: int) -> dict[str, Any]:
    """Parse only an already supplied payload; never requests or stores credentials."""
    root = payload.get("data", {}) if isinstance(payload, dict) else {}
    meeting = root.get("raceMeeting", {}) if isinstance(root, dict) else {}
    if meeting.get("raceNo") not in (race_no, str(race_no)):
        return {"status": "degraded", "odds": [], "metadata": {"complete_win_place_pairs": 0, "reason": "race_mismatch"}}
    pools = meeting.get("pmPools")
    if not isinstance(pools, list):
        return {"status": "degraded", "odds": [], "metadata": {"complete_win_place_pairs": 0, "reason": "missing_pmPools"}}
    by_horse: dict[int, dict[str, float | None]] = {}
    for pool in pools:
        if not isinstance(pool, dict):
            continue
        kind = str(pool.get("oddsType", "")).upper()
        if kind not in {"WIN", "PLA"} or not isinstance(pool.get("runners"), list):
            continue
        for runner in pool["runners"]:
            if not isinstance(runner, dict):
                continue
            try:
                horse_no = int(runner.get("horseNo"))
            except (TypeError, ValueError):
                continue
            odds = _num(runner.get("odds"))
            if odds is None:
                continue
            by_horse.setdefault(horse_no, {})[kind] = odds
    rows = []
    for horse_no in sorted(by_horse):
        win = by_horse[horse_no].get("WIN")
        pla = by_horse[horse_no].get("PLA")
        if win is None or pla is None:
            continue
        rows.append({"horse_no": horse_no, "win_odds": win, "place_odds": pla})
    complete = len(rows)
    return {"status": "ok" if complete else "degraded", "odds": rows, "metadata": {"complete_win_place_pairs": complete, "rows_parsed": complete}}


if __name__ == "__main__":
    import sys
    print(json.dumps(parse_pm_pools(json.load(open(sys.argv[1], encoding="utf-8")), int(sys.argv[2])), ensure_ascii=False, indent=2))
