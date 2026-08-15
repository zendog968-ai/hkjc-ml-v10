#!/usr/bin/env python3
"""Query indicator EV for one pre-race complex-pool snapshot.

Supported modes:
  - trifecta: exact ordered first/second/third candidate (TRIFECTA_ORDERED, MAIN)
  - double_trio: two unordered top-three sets (DOUBLE_TRIO, MAIN)
  - six_win_bonus: six exact winners candidate (SIX_UP, SIX_WIN_BONUS)

This tool intentionally never reads official_pool_payouts.  The reported EV is
an indicator based on a pre-race displayed/estimated quote, not a guaranteed
return in a pari-mutuel pool.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MODE_RULES = {
    "trifecta": {
        "pool_type": "TRIFECTA_ORDERED",
        "payout_tier": "MAIN",
        "ordering": "ORDERED",
        "legs": 1,
        "positions": [1, 2, 3],
    },
    "double_trio": {
        "pool_type": "DOUBLE_TRIO",
        "payout_tier": "MAIN",
        "ordering": "LEGGED",
        "legs": 2,
        "positions": [1, 2, 3],
        "unordered_within_leg": True,
    },
    "six_win_bonus": {
        "pool_type": "SIX_UP",
        "payout_tier": "SIX_WIN_BONUS",
        "ordering": "LEGGED",
        "legs": 6,
        "positions": [1],
    },
}
VALID_QUOTE_KINDS = {"ESTIMATED_DIVIDEND", "DISPLAYED_ODDS"}


def parse_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"時間必須帶 UTC 偏移：{value}")
    return dt.astimezone(timezone.utc)


def canonical_from_members(members: list[sqlite3.Row], rule: dict[str, Any] | None = None) -> str:
    # Double Trio is unordered inside each leg.  Canonicalise its head-three set by
    # runner number, then assign P1..P3 deterministically; do not preserve finish order.
    if rule and rule.get("unordered_within_leg"):
        parts: list[str] = []
        for leg_no in sorted({int(row["leg_no"]) for row in members}):
            leg_members = sorted(
                (row for row in members if int(row["leg_no"]) == leg_no),
                key=lambda row: int(row["runner_no"]),
            )
            for position_no, row in enumerate(leg_members, start=1):
                parts.append(f"L{leg_no}:P{position_no}={row['runner_no']}")
        return "|".join(parts)
    ordered = sorted(members, key=lambda x: (x["leg_no"], x["position_no"]))
    return "|".join(
        f"L{row['leg_no']}:P{row['position_no']}={row['runner_no']}" for row in ordered
    )


def validate_members(members: list[sqlite3.Row], rule: dict[str, Any]) -> str | None:
    if len(members) != rule["legs"] * len(rule["positions"]):
        return "組合成員數與彩池規則不符"
    actual_legs = sorted({row["leg_no"] for row in members})
    if actual_legs != list(range(1, rule["legs"] + 1)):
        return "組合關次不完整或非連續"
    for leg_no in actual_legs:
        leg_members = [row for row in members if row["leg_no"] == leg_no]
        positions = sorted(row["position_no"] for row in leg_members)
        if positions != rule["positions"]:
            return f"第 {leg_no} 關位置定義不符合 {rule['positions']}"
        if len({int(row["runner_no"]) for row in leg_members}) != len(leg_members):
            return f"第 {leg_no} 關有重複馬號"
    return None


def load_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT e.pool_event_id, e.pool_type, e.expected_leg_count,
               s.pool_snapshot_id, s.snapshot_label, s.captured_at_utc,
               s.anchor_leg_no, s.scheduled_anchor_start_utc, s.capture_delta_seconds,
               s.status, s.quote_completeness
        FROM pre_race_pool_events AS e
        JOIN pre_race_pool_snapshots AS s USING (pool_event_id)
        WHERE s.pool_snapshot_id = ?
        """,
        (snapshot_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"找不到 pool_snapshot_id={snapshot_id}")
    return row


def load_quote(conn: sqlite3.Connection, snapshot_id: int, selection_key: str, tier: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM pre_race_pool_selection_quotes
        WHERE pool_snapshot_id = ? AND selection_key = ? AND quoted_payout_tier = ?
        """,
        (snapshot_id, selection_key, tier),
    ).fetchone()


def evaluate_candidate(
    conn: sqlite3.Connection,
    snapshot: sqlite3.Row,
    candidate: dict[str, Any],
    rule: dict[str, Any],
    model_generated_at_utc: datetime,
) -> dict[str, Any]:
    selection_key = str(candidate.get("selection_key", ""))
    result: dict[str, Any] = {
        "selection_key": selection_key,
        "predicted_hit_probability": candidate.get("predicted_hit_probability"),
        "stake": candidate.get("stake"),
        "payout_tier": rule["payout_tier"],
        "eligible": False,
        "exclusion_reason": None,
        "quote_kind": None,
        "quote_value": None,
        "quote_unit": None,
        "gross_return_per_stake": None,
        "indicator_ev_per_stake": None,
        "indicator_expected_net": None,
    }
    try:
        probability = float(candidate["predicted_hit_probability"])
        stake = float(candidate["stake"])
    except (KeyError, TypeError, ValueError):
        result["exclusion_reason"] = "缺少或無法解析 predicted_hit_probability／stake"
        return result
    if not 0.0 <= probability <= 1.0 or stake <= 0.0:
        result["exclusion_reason"] = "模型機率必須在 0 至 1 之間，且 stake 必須大於 0"
        return result
    quote = load_quote(conn, snapshot["pool_snapshot_id"], selection_key, rule["payout_tier"])
    if quote is None:
        result["exclusion_reason"] = "該候選組合沒有相同快照、相同派彩層的特定報價"
        return result
    members = conn.execute(
        """
        SELECT leg_no, position_no, runner_no, horse_name
        FROM pre_race_pool_selection_members
        WHERE pool_selection_quote_id = ?
        ORDER BY leg_no, position_no
        """,
        (quote["pool_selection_quote_id"],),
    ).fetchall()
    member_error = validate_members(members, rule)
    if member_error:
        result["exclusion_reason"] = member_error
        return result
    if canonical_from_members(members, rule) != selection_key:
        result["exclusion_reason"] = "selection_key 與已保存組合成員或孖T每關無順序 canonical key 不一致"
        return result
    if quote["selection_ordering"] != rule["ordering"]:
        result["exclusion_reason"] = "組合順序性與指定彩池規則不一致"
        return result
    if quote["quote_kind"] not in VALID_QUOTE_KINDS:
        result["exclusion_reason"] = "只可用特定組合的 DISPLAYED_ODDS／ESTIMATED_DIVIDEND 計算指標 EV"
        return result
    if quote["quote_value"] is None or quote["quote_unit"] is None or quote["quote_is_return_inclusive"] is None:
        result["exclusion_reason"] = "報價缺少金額、下注單位或回報是否連本資訊"
        return result
    price_multiple = float(quote["quote_value"]) / float(quote["quote_unit"])
    gross_multiple = price_multiple if int(quote["quote_is_return_inclusive"]) == 1 else price_multiple + 1.0
    gross_return = stake * gross_multiple
    expected_net = probability * gross_return - stake
    result.update(
        {
            "eligible": True,
            "quote_kind": quote["quote_kind"],
            "quote_value": float(quote["quote_value"]),
            "quote_unit": float(quote["quote_unit"]),
            "quote_is_return_inclusive": bool(quote["quote_is_return_inclusive"]),
            "gross_return_per_stake": gross_multiple,
            "indicator_ev_per_stake": expected_net / stake,
            "indicator_expected_net": expected_net,
        }
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="查詢三重彩或六寶獎的嚴格賽前指標 EV")
    parser.add_argument("--db", required=True, help="已套用單馬及複合彩池 schema 的 SQLite archive")
    parser.add_argument("--mode", required=True, choices=sorted(MODE_RULES), help="trifecta、double_trio 或 six_win_bonus")
    parser.add_argument("--candidate-file", required=True, help="模型候選組合 JSON")
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-csv", required=True)
    args = parser.parse_args()

    candidates_doc = json.loads(Path(args.candidate_file).read_text(encoding="utf-8"))
    snapshot_id = int(candidates_doc["pool_snapshot_id"])
    model_generated = parse_utc(str(candidates_doc["model_generated_at_utc"]))
    candidates = candidates_doc.get("candidates", [])
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("candidate-file 必須包含非空的 candidates 陣列")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rule = MODE_RULES[args.mode]
    snapshot = load_snapshot(conn, snapshot_id)
    captured_at = parse_utc(snapshot["captured_at_utc"])
    gate_errors: list[str] = []
    if snapshot["pool_type"] != rule["pool_type"]:
        gate_errors.append(f"pool_type 必須為 {rule['pool_type']}")
    if snapshot["status"] != "complete":
        gate_errors.append("snapshot status 必須為 complete")
    if snapshot["quote_completeness"] not in {"full", "partial"}:
        gate_errors.append("quote_completeness 必須為 full 或 partial，且候選本身必須有特定報價")
    if int(snapshot["expected_leg_count"]) != rule["legs"]:
        gate_errors.append("彩池預期關數與查詢模式不一致")
    if model_generated > captured_at:
        gate_errors.append("模型候選生成時間晚於賽前快照；無法證明模型在價格觀察時已固定")
    if not snapshot["scheduled_anchor_start_utc"]:
        gate_errors.append("缺少 scheduled_anchor_start_utc，無法驗證快照在關閉前")
    else:
        anchor_start = parse_utc(snapshot["scheduled_anchor_start_utc"])
        if captured_at >= anchor_start:
            gate_errors.append("快照捕捉時間不在錨點關開跑前")

    rows: list[dict[str, Any]] = []
    if not gate_errors:
        for candidate in candidates:
            rows.append(evaluate_candidate(conn, snapshot, candidate, rule, model_generated))
    else:
        rows = [
            {
                "selection_key": str(candidate.get("selection_key", "")),
                "predicted_hit_probability": candidate.get("predicted_hit_probability"),
                "stake": candidate.get("stake"),
                "payout_tier": rule["payout_tier"],
                "eligible": False,
                "exclusion_reason": "; ".join(gate_errors),
                "quote_kind": None,
                "quote_value": None,
                "quote_unit": None,
                "gross_return_per_stake": None,
                "indicator_ev_per_stake": None,
                "indicator_expected_net": None,
            }
            for candidate in candidates
        ]

    eligible = [row for row in rows if row["eligible"]]
    total_stake = sum(float(row["stake"]) for row in eligible)
    total_expected_net = sum(float(row["indicator_expected_net"]) for row in eligible)
    summary = {
        "mode": args.mode,
        "pool_snapshot_id": snapshot_id,
        "pool_type": snapshot["pool_type"],
        "snapshot_label": snapshot["snapshot_label"],
        "snapshot_captured_at_utc": snapshot["captured_at_utc"],
        "model_generated_at_utc": candidates_doc["model_generated_at_utc"],
        "snapshot_quote_completeness": snapshot["quote_completeness"],
        "gate_errors": gate_errors,
        "candidate_count": len(rows),
        "priced_candidate_count": len(eligible),
        "priced_candidate_coverage": len(eligible) / len(rows),
        "total_stake": total_stake,
        "total_indicator_expected_net": total_expected_net if eligible else None,
        "portfolio_indicator_ev_per_stake": (total_expected_net / total_stake) if total_stake else None,
        "rows": rows,
        "notes": [
            "指標 EV 只使用賽前顯示／估計組合報價；不讀取 official_pool_payouts。",
            "平分彩金的最終派彩會在關閉前變動，因此指標 EV 不代表保證回報。",
            "三重彩機率必須是精確 1-2-3 順序的聯合機率；孖T機率必須是兩關各自頭三無順序集合均命中的聯合機率；六寶獎機率必須是六關全勝的聯合機率。",
        ],
    }
    Path(args.output_json).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    columns = [
        "selection_key", "payout_tier", "predicted_hit_probability", "stake", "eligible",
        "exclusion_reason", "quote_kind", "quote_value", "quote_unit",
        "quote_is_return_inclusive", "gross_return_per_stake", "indicator_ev_per_stake",
        "indicator_expected_net",
    ]
    with Path(args.output_csv).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    conn.close()
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
