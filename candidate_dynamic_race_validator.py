"""Candidate-only late scratching and jockey-change validator.

This module transforms a pre-race candidate matrix into an active-runner
prediction batch. It never imports or opens V10/N6 production artifacts.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

SCRATCH_STATUSES = {"SCR", "SCRATCHED", "WITHDRAWN", "退賽", "退出", "取消出賽"}
DEFAULT_JOCKEY_FEATURES = {
    "jockey_code": None,
    "jockey_win_rate": 0.0,
    "jockey_place_rate": 0.0,
    "feature_source": "default_conservative",
}


def _horse_no(row: dict[str, Any]) -> int:
    value = row.get("horse_no")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("horse_no 必須為正整數")
    return value


def _is_scratched(latest: dict[str, Any]) -> bool:
    if latest.get("scratched") is True or latest.get("withdrawn") is True:
        return True
    status = str(latest.get("status", "")).strip().upper()
    return status in SCRATCH_STATUSES


def _stable_softmax(scores: list[float]) -> list[float]:
    if not scores or any(not math.isfinite(x) for x in scores):
        raise ValueError("活躍馬匹的分數必須是有限數值")
    pivot = max(scores)
    weights = [math.exp(x - pivot) for x in scores]
    total = math.fsum(weights)
    if not math.isfinite(total) or total <= 0:
        raise ValueError("Softmax 分母無效")
    probabilities = [w / total for w in weights]
    # Correct only floating-point residue, never alter model ordering.
    probabilities[-1] += 1.0 - math.fsum(probabilities)
    return probabilities


def _score(row: dict[str, Any]) -> float:
    for key in ("model_logit", "raw_score", "score"):
        value = row.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = float(value)
            if math.isfinite(value):
                return value
    prior = row.get("predicted_win_probability")
    if isinstance(prior, (int, float)) and not isinstance(prior, bool) and 0.0 < float(prior) <= 1.0:
        return math.log(float(prior))
    raise ValueError(f"馬號 {_horse_no(row)} 缺少有限 model_logit/raw_score/score")


def _jockey_profile(name: str | None, profiles: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if name and name in profiles:
        profile = dict(DEFAULT_JOCKEY_FEATURES)
        profile.update(profiles[name])
        profile["feature_source"] = "historical_profile"
        return profile
    return dict(DEFAULT_JOCKEY_FEATURES)


def validate_and_transform(
    matrix: list[dict[str, Any]],
    latest_lineup: list[dict[str, Any]],
    jockey_profiles: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Filter scratches, apply latest jockeys, and renormalize active runners.

    `matrix` is treated as opaque candidate feature rows. Only the output
    metadata fields are added; no fixed-width production feature vector is
    constructed or mutated.
    """
    profiles = jockey_profiles or {}
    latest_by_no: dict[int, dict[str, Any]] = {}
    for latest in latest_lineup:
        no = _horse_no(latest)
        if no in latest_by_no:
            raise ValueError(f"最新陣容含重複馬號 {no}")
        latest_by_no[no] = latest

    active: list[dict[str, Any]] = []
    scratched: list[dict[str, Any]] = []
    jockey_changes: list[dict[str, Any]] = []
    for original in matrix:
        no = _horse_no(original)
        latest = latest_by_no.get(no)
        if latest is None:
            raise ValueError(f"馬號 {no} 不在最新陣容，拒絕推論")
        if _is_scratched(latest):
            scratched.append({"horse_no": no, "reason": "late_scratching"})
            continue
        row = dict(original)
        old_jockey = str(original.get("jockey", "")).strip() or None
        new_jockey = str(latest.get("jockey") or latest.get("jockey_name") or "").strip() or None
        if new_jockey is None:
            raise ValueError(f"活躍馬號 {no} 缺少最新騎師資料")
        changed = old_jockey != new_jockey
        row["jockey"] = new_jockey
        row["jockey_changed"] = changed
        row["jockey_features"] = _jockey_profile(new_jockey, profiles)
        if changed:
            jockey_changes.append({"horse_no": no, "from": old_jockey, "to": new_jockey, "feature_source": row["jockey_features"]["feature_source"]})
        row["lineup_status"] = "active"
        active.append(row)
    if not active:
        raise ValueError("退賽過濾後沒有活躍馬匹，拒絕產出預測")
    probabilities = _stable_softmax([_score(row) for row in active])
    for row, probability in zip(active, probabilities):
        row["predicted_win_probability"] = probability
    return {
        "predictions": active,
        "scratched": scratched,
        "jockey_changes": jockey_changes,
        "active_runner_count": len(active),
        "scratched_count": len(scratched),
        "probability_sum": math.fsum(row["predicted_win_probability"] for row in active),
        "status": "ok",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Candidate late-lineup validator")
    parser.add_argument("--matrix", required=True, type=Path)
    parser.add_argument("--lineup", required=True, type=Path)
    parser.add_argument("--jockey-profiles", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    matrix = json.loads(args.matrix.read_text(encoding="utf-8"))
    lineup = json.loads(args.lineup.read_text(encoding="utf-8"))
    profiles = json.loads(args.jockey_profiles.read_text(encoding="utf-8")) if args.jockey_profiles else {}
    result = validate_and_transform(matrix["matrix"] if isinstance(matrix, dict) else matrix, lineup["lineup"] if isinstance(lineup, dict) else lineup, profiles)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("active_runner_count", "scratched_count", "probability_sum", "status")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
