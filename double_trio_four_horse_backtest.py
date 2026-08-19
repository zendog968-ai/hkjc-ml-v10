#!/usr/bin/env python3
"""Strict, cohort-isolated backtest for immutable V10+N6 Double Trio decisions.

No post-hoc prediction is accepted. A record is eligible only when it contains the
pre-race four-horse decision, an immutable model SHA-256, both official top-three
sets, and an official payout. Metrics are never combined across model versions.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

MIN_EXPLORATORY_EVENTS = 15
UNIT_STAKE_HKD = 10.0


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def number(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def runner_set(value: Any) -> frozenset[int] | None:
    if not isinstance(value, list) or len(value) != 3:
        return None
    members: list[int] = []
    for item in value:
        parsed = number(item)
        if parsed is None or parsed <= 0 or int(parsed) != parsed:
            return None
        members.append(int(parsed))
    return frozenset(members) if len(set(members)) == 3 else None


def iso_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result if result.tzinfo is not None else None


def model_cohort(decision: dict[str, Any]) -> str | None:
    provenance = decision.get("provenance")
    value = provenance.get("base_model_sha256") if isinstance(provenance, dict) else None
    value = str(value or "").lower()
    return value if len(value) == 64 and all(char in "0123456789abcdef" for char in value) else None


def selected_four(decision: dict[str, Any], leg_no: int) -> frozenset[int] | None:
    strategy = decision.get("strategy")
    legs = strategy.get("legs") if isinstance(strategy, dict) else None
    if not isinstance(legs, list):
        return None
    matching = [leg for leg in legs if isinstance(leg, dict) and leg.get("leg_no") == leg_no]
    if len(matching) != 1:
        return None
    selections = matching[0].get("selections")
    if not isinstance(selections, list) or len(selections) != 4:
        return None
    members: list[int] = []
    for item in selections:
        parsed = number(item.get("horse_no") if isinstance(item, dict) else None)
        if parsed is None or parsed <= 0 or int(parsed) != parsed:
            return None
        members.append(int(parsed))
    return frozenset(members) if len(set(members)) == 4 else None


def validate_and_settle(decision: dict[str, Any], settlement: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    cohort = model_cohort(decision)
    if cohort is None:
        return None, "缺少有效 base_model_sha256，禁止跨模型版本混合"
    provenance = decision.get("provenance")
    if not isinstance(provenance, dict) or provenance.get("post_race_labels_included") is not False:
        return None, "賽前決策 provenance 不完整或含賽後標籤"
    generated_at = iso_time(provenance.get("generated_at_utc"))
    first_start = iso_time(provenance.get("first_leg_start_at_utc"))
    if generated_at is None or first_start is None or generated_at >= first_start:
        return None, "決策生成時間未能證明早於第一關開跑"
    first_selection = selected_four(decision, 1)
    second_selection = selected_four(decision, 2)
    if first_selection is None or second_selection is None:
        return None, "每關必須是四匹、馬號唯一的不可變選馬"
    official = settlement.get("official")
    if not isinstance(official, dict):
        return None, "缺少官方結算資料"
    first_result = runner_set(official.get("leg1_top3"))
    second_result = runner_set(official.get("leg2_top3"))
    if first_result is None or second_result is None:
        return None, "官方兩關頭三不完整"
    payout = number(official.get("main_payout_per_unit"))
    payout_unit = number(official.get("payout_unit"))
    if payout is None or payout_unit is None or payout_unit <= 0:
        return None, "缺少官方 MAIN 派彩"
    stake = number(decision.get("combination_plan", {}).get("total_suggested_capital_hkd"))
    if stake is None or stake <= 0:
        return None, "缺少有效十六注總本金"
    hit = first_result.issubset(first_selection) and second_result.issubset(second_selection)
    gross = stake * 0.0
    if hit:
        # Exactly one of the 16 cross-combinations matches two distinct official top-three sets.
        gross = UNIT_STAKE_HKD * (payout / payout_unit)
    return {
        "decision_id": decision.get("decision_id"),
        "meeting_date": decision.get("meeting", {}).get("race_date"),
        "racecourse": decision.get("meeting", {}).get("racecourse"),
        "base_model_sha256": cohort,
        "n6_release": provenance.get("n6_release"),
        "generated_at_utc": provenance.get("generated_at_utc"),
        "stake_hkd": stake,
        "hit": hit,
        "gross_return_hkd": gross,
        "net_return_hkd": gross - stake,
        "settlement_tier": "MAIN" if hit else "LOSS",
        "official_source_url": official.get("source_url"),
    }, None


def aggregate(records: list[dict[str, Any]], exclusions: Counter[str]) -> dict[str, Any]:
    cohorts: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[record["base_model_sha256"]].append(record)
    for sha, rows in sorted(grouped.items()):
        stake = sum(float(row["stake_hkd"]) for row in rows)
        gross = sum(float(row["gross_return_hkd"]) for row in rows)
        net = sum(float(row["net_return_hkd"]) for row in rows)
        hits = sum(bool(row["hit"]) for row in rows)
        exploratory = len(rows) < MIN_EXPLORATORY_EVENTS
        cohorts[sha] = {
            "status": "exploratory" if exploratory else "ready",
            "label": "探索性：完整結算事件少於15個" if exploratory else "資料充足",
            "settled_event_count": len(rows),
            "hit_count": hits,
            "hit_rate": hits / len(rows),
            "total_stake_hkd": stake,
            "gross_return_hkd": gross,
            "net_return_hkd": net,
            "roi": net / stake if stake else None,
            "n6_releases": sorted({str(row.get("n6_release") or "unknown") for row in rows}),
        }
    return {
        "schema": "v10_double_trio_four_horse_backtest_v1",
        "strategy": "V10+N6 official Double Trio, four runners per leg, C(4,3) × C(4,3) = 16 fixed HK$10 combinations",
        "readiness": "not_ready" if not cohorts else "ready",
        "cohort_policy": "Results are segregated by base_model_sha256 and are never aggregated across model retraining versions.",
        "minimum_exploratory_events": MIN_EXPLORATORY_EVENTS,
        "cohorts": cohorts,
        "settled_record_count": len(records),
        "excluded_record_count": sum(exclusions.values()),
        "exclusion_reason_counts": dict(exclusions.most_common()),
        "notice": "Only immutable pre-race V10+N6 decisions plus official results and MAIN payout are settled. No post-hoc reconstruction or future-information backfill is allowed.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="孖T四匹複式無未來資料回測")
    parser.add_argument("--decision-root", required=True)
    parser.add_argument("--settlement-root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    decisions = sorted(Path(args.decision_root).rglob("*.json"))
    settlements = {path.stem: payload for path in Path(args.settlement_root).rglob("*.json") if (payload := load_json(path)) is not None}
    records: list[dict[str, Any]] = []
    exclusions: Counter[str] = Counter()
    for path in decisions:
        decision = load_json(path)
        if decision is None:
            exclusions["無法讀取賽前決策 JSON"] += 1
            continue
        settlement_key = str(decision.get("decision_id") or path.stem)
        settlement = settlements.get(settlement_key)
        if settlement is None:
            exclusions["缺少同一決策 ID 的官方結算"] += 1
            continue
        record, reason = validate_and_settle(decision, settlement)
        if record is None:
            exclusions[str(reason)] += 1
        else:
            records.append(record)
    summary = aggregate(records, exclusions)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
