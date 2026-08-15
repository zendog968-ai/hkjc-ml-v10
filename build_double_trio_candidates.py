#!/usr/bin/env python3
"""Build unbound V10.2 Double Trio candidates from two fixed race predictions.

The candidate file deliberately does not contain a pool_snapshot_id.  Generate it
before the T-15/T-5 quote capture, then use bind_double_trio_candidates.py to
attach a verified later snapshot without changing the model choices.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("model-generated-at-utc 必須帶 UTC 偏移")
    return parsed.astimezone(timezone.utc)


def top3_set_probability(rows: tuple[dict[str, Any], dict[str, Any], dict[str, Any]]) -> float:
    """Exact unordered top-three set probability under a PL race-ranking proxy."""
    weights = [float(row["predicted_win_probability"]) for row in rows]
    total = 0.0
    for order in itertools.permutations(weights):
        first, second, third = order
        denom_second = 1.0 - first
        denom_third = 1.0 - first - second
        if denom_second <= 1e-12 or denom_third <= 1e-12:
            continue
        total += first * (second / denom_second) * (third / denom_third)
    return total


def runner_no(row: dict[str, Any]) -> int:
    value = row.get("horse_no", row.get("runner_no"))
    if value is None:
        raise ValueError(f"預測列缺少 horse_no／runner_no：{row.get('horse_name', '未知馬匹')}")
    number = int(value)
    if number <= 0:
        raise ValueError("horse_no／runner_no 必須為正整數")
    return number


def canonical_leg(leg_no: int, rows: tuple[dict[str, Any], dict[str, Any], dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    ordered = sorted(rows, key=runner_no)
    key = "|".join(f"L{leg_no}:P{position}={runner_no(row)}" for position, row in enumerate(ordered, start=1))
    members = [{"runner_no": runner_no(row), "horse_name": row.get("horse_name"), "predicted_win_probability": float(row["predicted_win_probability"])} for row in ordered]
    return key, members


def parse_prediction(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    race = doc.get("race")
    predictions = doc.get("predictions")
    if not isinstance(race, dict) or not isinstance(predictions, list):
        raise ValueError(f"{path} 必須是 predict.py 的 JSON 輸出")
    usable = []
    for row in predictions:
        try:
            runner_no(row)
            probability = float(row["predicted_win_probability"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{path} 有無效預測列：{exc}") from exc
        if not math.isfinite(probability) or probability <= 0:
            continue
        usable.append(row)
    if len(usable) < 3:
        raise ValueError(f"{path} 少於三匹可用馬")
    usable.sort(key=lambda row: float(row["predicted_win_probability"]), reverse=True)
    return race, usable


def main() -> int:
    parser = argparse.ArgumentParser(description="由兩關 V10.2 預測生成未綁定孖T候選組合")
    parser.add_argument("--leg1-prediction", required=True)
    parser.add_argument("--leg2-prediction", required=True)
    parser.add_argument("--pool-event-code", required=True, help="官方孖T彩池識別碼，稍後與快照事件連接")
    parser.add_argument("--model-generated-at-utc", required=True)
    parser.add_argument("--top-runners-per-leg", type=int, default=5)
    parser.add_argument("--max-candidates", type=int, default=30)
    parser.add_argument("--research-stake", type=float, default=10.0, help="固定研究用每組合注額，不是下注指示")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.top_runners_per_leg < 3 or args.max_candidates < 1 or args.research_stake <= 0:
        raise ValueError("top-runners-per-leg 至少 3、max-candidates 至少 1、research-stake 必須大於 0")
    generated = parse_utc(args.model_generated_at_utc)
    race1, predictions1 = parse_prediction(Path(args.leg1_prediction))
    race2, predictions2 = parse_prediction(Path(args.leg2_prediction))
    if race1.get("race_date") != race2.get("race_date") or race1.get("racecourse") != race2.get("racecourse"):
        raise ValueError("兩關必須屬同一賽日及馬場")
    if race1.get("race_no") == race2.get("race_no"):
        raise ValueError("孖T兩關不可為同一場次")
    selected1 = predictions1[:args.top_runners_per_leg]
    selected2 = predictions2[:args.top_runners_per_leg]
    combinations1 = []
    for combo in itertools.combinations(selected1, 3):
        key, members = canonical_leg(1, combo)
        combinations1.append({"key": key, "members": members, "probability": top3_set_probability(combo)})
    combinations2 = []
    for combo in itertools.combinations(selected2, 3):
        key, members = canonical_leg(2, combo)
        combinations2.append({"key": key, "members": members, "probability": top3_set_probability(combo)})
    candidates = []
    for first in combinations1:
        for second in combinations2:
            joint = first["probability"] * second["probability"]
            candidates.append({
                "selection_key": f"{first['key']}|{second['key']}",
                "predicted_hit_probability": joint,
                "stake": args.research_stake,
                "leg1_top3_set_probability": first["probability"],
                "leg2_top3_set_probability": second["probability"],
                "leg1_members": first["members"],
                "leg2_members": second["members"],
            })
    candidates.sort(key=lambda row: row["predicted_hit_probability"], reverse=True)
    output = {
        "candidate_format": "V10_2_DOUBLE_TRIO_UNBOUND_V1",
        "pool_type": "DOUBLE_TRIO",
        "pool_event_code": args.pool_event_code,
        "model_generated_at_utc": generated.isoformat(),
        "probability_method": "Product of two exact unordered top-three Plackett-Luce set probabilities; cross-leg independence approximation.",
        "research_stake_per_combination": args.research_stake,
        "leg1": {"race": race1, "prediction_source": str(Path(args.leg1_prediction)), "top_runner_count": len(selected1)},
        "leg2": {"race": race2, "prediction_source": str(Path(args.leg2_prediction)), "top_runner_count": len(selected2)},
        "total_cross_combinations_before_cap": len(candidates),
        "candidates": candidates[:args.max_candidates],
        "notes": [
            "此檔案在賽前市場快照前生成，刻意不含 pool_snapshot_id 或市場報價。",
            "孖T每關頭三不分次序；selection_key 以每關馬號遞增編碼。",
            "跨關聯合機率採獨立近似，只能作模型研究輸入；不是保證命中或收益。",
        ],
    }
    raw = json.dumps(output, ensure_ascii=False, sort_keys=True).encode("utf-8")
    output["candidate_payload_sha256"] = hashlib.sha256(raw).hexdigest()
    Path(args.output).write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"candidate_count": len(output["candidates"]), "pool_event_code": args.pool_event_code, "output": args.output}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
