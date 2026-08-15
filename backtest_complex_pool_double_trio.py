#!/usr/bin/env python3
"""Strict batch backtest for V10.2 historical HKJC Double Trio candidates.

Indicator EV reads only same-snapshot MAIN estimated/displayed quotes.  Realized
ROI is settled later, after candidates are fixed, from official result members and
MAIN/CONSOLATION official payouts.  A CONSOLATION settlement is permitted only when
no MAIN payout exists and the candidate's first-leg unordered trio matches the
official first-leg top-three set.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from query_complex_pool_ev import (
    MODE_RULES,
    canonical_from_members,
    evaluate_candidate,
    load_snapshot,
    parse_utc,
)

RULE = MODE_RULES["double_trio"]


def read_candidate_documents(root: Path, pattern: str) -> list[tuple[Path, dict[str, Any]]]:
    docs: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(root.rglob(pattern)):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            docs.append((path, payload if isinstance(payload, dict) else {"_load_error": "JSON 根節點必須為物件"}))
        except (OSError, json.JSONDecodeError) as exc:
            docs.append((path, {"_load_error": f"無法讀取 JSON：{exc}"}))
    return docs


def build_snapshot_gate(snapshot: sqlite3.Row, model_generated, label: str, max_delta: int) -> list[str]:
    errors: list[str] = []
    captured = parse_utc(snapshot["captured_at_utc"])
    if snapshot["pool_type"] != "DOUBLE_TRIO":
        errors.append("pool_type 不是 DOUBLE_TRIO")
    if snapshot["snapshot_label"] != label:
        errors.append(f"快照標籤不是要求的 {label}")
    if snapshot["status"] != "complete":
        errors.append("snapshot status 不是 complete")
    if snapshot["quote_completeness"] not in {"full", "partial"}:
        errors.append("quote_completeness 不是 full／partial")
    if int(snapshot["expected_leg_count"]) != 2:
        errors.append("孖T彩池必須有兩關")
    if model_generated > captured:
        errors.append("模型候選生成時間晚於賽前快照")
    if not snapshot["scheduled_anchor_start_utc"]:
        errors.append("缺少 scheduled_anchor_start_utc")
    elif captured >= parse_utc(snapshot["scheduled_anchor_start_utc"]):
        errors.append("快照不在第一關開跑前")
    if snapshot["anchor_leg_no"] != 1:
        errors.append("孖T的 T-minus 時間錨點必須是第一關")
    delta = snapshot["capture_delta_seconds"]
    if delta is None:
        errors.append("缺少 capture_delta_seconds，無法稽核 T-minus 偏差")
    elif abs(int(delta)) > max_delta:
        errors.append(f"快照偏差超過 {max_delta} 秒")
    return errors


def result_key_for_legs(conn: sqlite3.Connection, pool_event_id: int, legs: list[int]) -> tuple[str | None, str | None]:
    members: list[dict[str, int]] = []
    for leg_no in legs:
        rows = conn.execute(
            """SELECT leg_no, finish_position, runner_no
            FROM official_pool_result_members
            WHERE pool_event_id = ? AND leg_no = ? AND finish_position BETWEEN 1 AND 3
            ORDER BY finish_position""",
            (pool_event_id, leg_no),
        ).fetchall()
        if len(rows) != 3 or [int(row["finish_position"]) for row in rows] != [1, 2, 3]:
            return None, f"第 {leg_no} 關官方結果未提供完整頭三"
        # Feed synthetic position numbers; canonical_from_members reorders runners in a Double Trio leg.
        members.extend({"leg_no": int(row["leg_no"]), "position_no": int(row["finish_position"]), "runner_no": int(row["runner_no"])} for row in rows)
    return canonical_from_members(members, RULE), None


def candidate_first_leg_key(conn: sqlite3.Connection, snapshot_id: int, selection_key: str) -> tuple[str | None, str | None]:
    quote = conn.execute(
        """SELECT pool_selection_quote_id
        FROM pre_race_pool_selection_quotes
        WHERE pool_snapshot_id = ? AND selection_key = ? AND quoted_payout_tier = 'MAIN'""",
        (snapshot_id, selection_key),
    ).fetchone()
    if quote is None:
        return None, "找不到候選 MAIN 報價的成員資料"
    rows = conn.execute(
        """SELECT leg_no, position_no, runner_no
        FROM pre_race_pool_selection_members
        WHERE pool_selection_quote_id = ? AND leg_no = 1
        ORDER BY position_no""",
        (quote["pool_selection_quote_id"],),
    ).fetchall()
    if len(rows) != 3:
        return None, "候選第一關不是完整三匹組合"
    return canonical_from_members(rows, {"unordered_within_leg": True}), None


def payout_row(conn: sqlite3.Connection, event_id: int, tier: str, key: str) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT payout_per_unit, payout_unit, payout_is_return_inclusive
        FROM official_pool_payouts
        WHERE pool_event_id = ? AND payout_tier = ? AND winning_selection_key = ?""",
        (event_id, tier, key),
    ).fetchone()


def payout_return(stake: float, payout: sqlite3.Row) -> tuple[float, float]:
    multiple = float(payout["payout_per_unit"]) / float(payout["payout_unit"])
    if int(payout["payout_is_return_inclusive"]) == 0:
        multiple += 1.0
    gross = stake * multiple
    return gross, gross - stake


def actual_settlement(conn: sqlite3.Connection, snapshot: sqlite3.Row, selection_key: str, stake: float) -> dict[str, Any]:
    result = {
        "settled": False, "settlement_reason": None, "settlement_tier": None,
        "official_main_selection_key": None, "official_consolation_selection_key": None,
        "hit": None, "actual_payout_per_unit": None, "actual_payout_unit": None,
        "actual_payout_is_return_inclusive": None, "actual_gross_return": None, "actual_net_return": None,
    }
    event_id = int(snapshot["pool_event_id"])
    main_key, main_error = result_key_for_legs(conn, event_id, [1, 2])
    first_leg_key, first_error = result_key_for_legs(conn, event_id, [1])
    result["official_main_selection_key"] = main_key
    result["official_consolation_selection_key"] = first_leg_key
    if main_error or first_error:
        result["settlement_reason"] = main_error or first_error
        return result
    if selection_key == main_key:
        payout = payout_row(conn, event_id, "MAIN", main_key)
        if payout is None:
            result["settlement_reason"] = "MAIN 組合命中但缺少相符官方 MAIN 派彩"
            return result
        gross, net = payout_return(stake, payout)
        result.update({
            "settled": True, "settlement_reason": "已結算 MAIN 命中", "settlement_tier": "MAIN", "hit": True,
            "actual_payout_per_unit": float(payout["payout_per_unit"]), "actual_payout_unit": float(payout["payout_unit"]),
            "actual_payout_is_return_inclusive": bool(payout["payout_is_return_inclusive"]),
            "actual_gross_return": gross, "actual_net_return": net,
        })
        return result

    # Only allow official consolation when no MAIN payout exists anywhere for this pool event.
    main_payout_exists = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM official_pool_payouts WHERE pool_event_id = ? AND payout_tier = 'MAIN')",
        (event_id,),
    ).fetchone()[0]
    candidate_leg1_key, candidate_error = candidate_first_leg_key(conn, int(snapshot["pool_snapshot_id"]), selection_key)
    if candidate_error:
        result["settlement_reason"] = candidate_error
        return result
    if not main_payout_exists and candidate_leg1_key == first_leg_key:
        payout = payout_row(conn, event_id, "CONSOLATION", first_leg_key)
        if payout is None:
            result["settlement_reason"] = "第一關命中且無 MAIN 派彩，但缺少相符官方 CONSOLATION 派彩"
            return result
        gross, net = payout_return(stake, payout)
        result.update({
            "settled": True, "settlement_reason": "已結算 CONSOLATION 命中", "settlement_tier": "CONSOLATION", "hit": True,
            "actual_payout_per_unit": float(payout["payout_per_unit"]), "actual_payout_unit": float(payout["payout_unit"]),
            "actual_payout_is_return_inclusive": bool(payout["payout_is_return_inclusive"]),
            "actual_gross_return": gross, "actual_net_return": net,
        })
        return result

    result.update({"settled": True, "settlement_reason": "已結算落敗", "settlement_tier": "LOSS", "hit": False, "actual_gross_return": 0.0, "actual_net_return": -stake})
    return result


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="V10.2 孖T T-15／T-5 嚴格批量回測")
    parser.add_argument("--db", required=True)
    parser.add_argument("--candidate-root", required=True)
    parser.add_argument("--snapshot-label", required=True, choices=["T_MINUS_15", "T_MINUS_5"])
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--candidate-glob", default="*.json")
    parser.add_argument("--max-capture-delta-seconds", type=int, default=180)
    args = parser.parse_args()
    if args.max_capture_delta_seconds < 0:
        raise ValueError("max-capture-delta-seconds 必須非負")
    root = Path(args.candidate_root)
    if not root.is_dir():
        raise ValueError(f"candidate-root 不是目錄：{root}")
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    documents = read_candidate_documents(root, args.candidate_glob)
    if not documents:
        raise ValueError("找不到任何候選 JSON 檔")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    details: list[dict[str, Any]] = []
    exclusion_counts: Counter[str] = Counter()
    for path, doc in documents:
        base_file = {"candidate_file": str(path)}
        if "_load_error" in doc:
            details.append({**base_file, "eligible": False, "exclusion_reason": doc["_load_error"]})
            exclusion_counts[doc["_load_error"]] += 1
            continue
        try:
            snapshot_id = int(doc["pool_snapshot_id"])
            generated = parse_utc(str(doc["model_generated_at_utc"]))
            candidates = doc["candidates"]
            if not isinstance(candidates, list) or not candidates:
                raise ValueError("candidates 必須為非空陣列")
            snapshot = load_snapshot(conn, snapshot_id)
            gates = build_snapshot_gate(snapshot, generated, args.snapshot_label, args.max_capture_delta_seconds)
        except (KeyError, TypeError, ValueError) as exc:
            reason = f"批次檔／快照無效：{exc}"
            details.append({**base_file, "eligible": False, "exclusion_reason": reason})
            exclusion_counts[reason] += 1
            continue
        for index, candidate in enumerate(candidates, start=1):
            base = {
                **base_file, "candidate_index": index, "pool_snapshot_id": snapshot_id,
                "pool_event_id": snapshot["pool_event_id"], "snapshot_label": snapshot["snapshot_label"],
                "snapshot_captured_at_utc": snapshot["captured_at_utc"],
                "scheduled_anchor_start_utc": snapshot["scheduled_anchor_start_utc"],
                "capture_delta_seconds": snapshot["capture_delta_seconds"],
                "model_generated_at_utc": doc["model_generated_at_utc"],
                "snapshot_quote_completeness": snapshot["quote_completeness"],
            }
            if gates:
                row = {**base, "selection_key": str(candidate.get("selection_key", "")), "predicted_hit_probability": candidate.get("predicted_hit_probability"), "stake": candidate.get("stake"), "payout_tier": "MAIN", "eligible": False, "exclusion_reason": "; ".join(gates)}
            else:
                row = {**base, **evaluate_candidate(conn, snapshot, candidate, RULE, generated)}
            if not row.get("eligible"):
                exclusion_counts[str(row.get("exclusion_reason", "未分類排除原因"))] += 1
            else:
                row.update(actual_settlement(conn, snapshot, row["selection_key"], float(row["stake"])))
            details.append(row)
    conn.close()

    eligible = [row for row in details if row.get("eligible") is True]
    settled = [row for row in eligible if row.get("settled") is True]
    main_hits = [row for row in settled if row.get("settlement_tier") == "MAIN"]
    consolation_hits = [row for row in settled if row.get("settlement_tier") == "CONSOLATION"]
    indicator_stake = sum(float(row["stake"]) for row in eligible)
    indicator_net = sum(float(row["indicator_expected_net"]) for row in eligible)
    settled_stake = sum(float(row["stake"]) for row in settled)
    actual_net = sum(float(row["actual_net_return"]) for row in settled)
    actual_gross = sum(float(row["actual_gross_return"]) for row in settled)
    cumulative = peak = max_drawdown = 0.0
    for row in sorted(settled, key=lambda r: (str(r.get("snapshot_captured_at_utc", "")), str(r.get("candidate_file", "")), int(r.get("candidate_index", 0)))):
        cumulative += float(row["actual_net_return"])
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
        row["actual_cumulative_net"] = cumulative
        row["actual_drawdown"] = peak - cumulative
    summary = {
        "mode": "double_trio_batch_backtest", "snapshot_label": args.snapshot_label,
        "max_capture_delta_seconds": args.max_capture_delta_seconds, "candidate_files_scanned": len(documents),
        "candidate_count": len(details), "priced_candidate_count": len(eligible),
        "priced_candidate_coverage": (len(eligible) / len(details)) if details else None,
        "indicator_total_stake": indicator_stake,
        "indicator_total_expected_net": indicator_net if eligible else None,
        "portfolio_indicator_ev_per_stake": (indicator_net / indicator_stake) if indicator_stake else None,
        "settled_candidate_count": len(settled),
        "settlement_coverage_of_priced": (len(settled) / len(eligible)) if eligible else None,
        "main_hit_count": len(main_hits), "consolation_hit_count": len(consolation_hits),
        "hit_rate": (len(main_hits) + len(consolation_hits)) / len(settled) if settled else None,
        "actual_total_stake": settled_stake, "actual_total_gross_return": actual_gross if settled else None,
        "actual_total_net_return": actual_net if settled else None,
        "actual_roi": (actual_net / settled_stake) if settled_stake else None,
        "actual_max_drawdown": max_drawdown if settled else None,
        "exclusion_reason_counts": dict(exclusion_counts.most_common()),
        "notes": [
            "指標 EV只使用同一 T-15／T-5 快照的 MAIN 特定孖T組合報價，不使用 official_pool_payouts。",
            "實現 ROI只在候選固定後，以兩關官方頭三、MAIN派彩及符合官方條件的 CONSOLATION 派彩結算。",
            "CONSOLATION 只在沒有 MAIN 官方派彩、且候選第一關的無順序頭三命中時結算。",
            "缺少合資格快照、特定報價、官方頭三或必要派彩時，相關指標為 N/A；不得以最終市場資料補值。",
        ],
    }
    columns = [
        "candidate_file", "candidate_index", "pool_snapshot_id", "pool_event_id", "snapshot_label", "snapshot_captured_at_utc", "scheduled_anchor_start_utc", "capture_delta_seconds", "model_generated_at_utc", "snapshot_quote_completeness", "selection_key", "predicted_hit_probability", "stake", "payout_tier", "eligible", "exclusion_reason", "quote_kind", "quote_value", "quote_unit", "quote_is_return_inclusive", "gross_return_per_stake", "indicator_ev_per_stake", "indicator_expected_net", "settled", "settlement_reason", "settlement_tier", "official_main_selection_key", "official_consolation_selection_key", "hit", "actual_payout_per_unit", "actual_payout_unit", "actual_payout_is_return_inclusive", "actual_gross_return", "actual_net_return", "actual_cumulative_net", "actual_drawdown",
    ]
    write_csv(out / "double_trio_batch_details.csv", details, columns)
    write_csv(out / "double_trio_batch_exclusions.csv", [row for row in details if not row.get("eligible")], columns)
    (out / "double_trio_batch_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
