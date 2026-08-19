#!/usr/bin/env python3
"""Validate rank preservation and cross-validation OOF coverage for rare-going candidate."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("reports/candidates/rare_going_partial_pooling_v1")
INPUT = ROOT / "partial_pooling_cv_oof_predictions.csv"
OUTPUT = ROOT / "partial_pooling_cv_validation.json"


def main() -> int:
    frame = pd.read_csv(INPUT)
    compared = []
    for race_group, group in frame.groupby("race_group", sort=False):
        baseline_index = int(np.argmax(group["baseline_probability"].to_numpy(dtype=float)))
        candidate_index = int(np.argmax(group["partial_pooling_rank_protected_probability"].to_numpy(dtype=float)))
        compared.append({"race_group": race_group, "same_top": baseline_index == candidate_index, "going": group["going"].iloc[0]})
    comparison = pd.DataFrame(compared)
    report = {
        "rows": int(len(frame)),
        "races": int(len(comparison)),
        "same_top_races": int(comparison["same_top"].sum()),
        "all_rank_protected": bool(comparison["same_top"].all()),
        "coverage_by_going": comparison.groupby("going").size().to_dict(),
    }
    if not report["all_rank_protected"]:
        raise AssertionError("At least one OOF race changed the baseline top pick.")
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
