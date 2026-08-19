#!/usr/bin/env python3
"""Inspect time-ordered rare-going coverage available for candidate calibration CV."""

from __future__ import annotations

import json

from n6.feature_engineering import load_training_frame
from train import chronological_split


def main() -> int:
    frame = load_training_frame()
    train, validation, test = chronological_split(frame)
    partitions = {"train": train, "validation": validation, "test": test, "post_train": frame[frame["race_date"] > train["race_date"].max()].copy()}
    report: dict[str, object] = {}
    for name, data in partitions.items():
        counts = (
            data.groupby("going", dropna=False)
            .agg(rows=("race_group", "size"), races=("race_group", "nunique"), winners=("target_win", "sum"), from_date=("race_date", "min"), to_date=("race_date", "max"))
            .reset_index()
            .sort_values("races", ascending=False)
        )
        report[name] = counts.to_dict(orient="records")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
