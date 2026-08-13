#!/usr/bin/env python3
"""Static validation for the V10.1 GitHub Actions race-day workflow."""
from __future__ import annotations

from pathlib import Path

import yaml


REQUIRED_SNIPPETS = (
    "github_race_day_gate.py",
    "fetch_hkjc_racecard.py",
    "fetch_hkjc_live_odds.py",
    "predict.py",
    "filter_high_probability.py",
    "--markdown-output",
    "actions/upload-artifact@v4",
    "gh release download v10-assets",
    "*/5 * * * *",
)


def main() -> int:
    path = Path(".github/workflows/race_day_scan.yml")
    source = path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(source)
    assert workflow["name"] == "V10.1 Race Day 60-Minute Scan"
    assert "jobs" in workflow and {"gate", "scan"}.issubset(workflow["jobs"])
    assert all(snippet in source for snippet in REQUIRED_SNIPPETS)
    scan = workflow["jobs"]["scan"]
    assert scan["needs"] == "gate"
    assert "needs.gate.outputs.should_run" in scan["if"]
    assert workflow["permissions"]["contents"] == "read"
    print('{"result":"PASS","workflow":"race_day_scan.yml"}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
