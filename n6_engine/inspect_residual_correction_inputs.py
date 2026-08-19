#!/usr/bin/env python3
"""Read-only inventory of pre-race inputs usable by residual calibration candidates."""
from __future__ import annotations

from n6.feature_engineering import load_training_frame

KEYWORDS = ("going", "odds", "barrier", "draw", "racecourse", "course", "stall", "distance", "surface")


def main() -> int:
    frame = load_training_frame()
    available = [column for column in frame.columns if any(key in column.lower() for key in KEYWORDS)]
    print("rows", len(frame), "races", frame["race_group"].nunique())
    print("matching_columns", available)
    for column in available:
        non_null = int(frame[column].notna().sum())
        unique = int(frame[column].nunique(dropna=True))
        sample = frame[column].dropna().head(5).tolist()
        print(f"{column}: non_null={non_null}; unique={unique}; sample={sample}")
    print("going_counts")
    print(frame["going"].value_counts(dropna=False).to_string())


if __name__ == "__main__":
    main()
