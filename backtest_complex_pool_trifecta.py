#!/usr/bin/env python3
"""Strict batch backtest for V10.2 historical TRIFECTA_ORDERED candidates.

Input JSON documents must each follow the query_complex_pool_ev.py candidate format:
{
  "pool_snapshot_id": 123,
  "model_generated_at_utc": "...+00:00",
  "candidates": [{"selection_key": "L1:P1=2|L1:P2=7|L1:P3=4", "predicted_hit_probability": 0.01, "stake": 10.0}]
}

Indicator EV reads only the pre-race selection quote. Realized ROI is calculated
separately after the candidate is fixed, using official results and official payout
facts. No final payout is ever used to calculate pre-race EV.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from query_complex_pool_ev import (
    MODE_RULES,
    canonical_from_members,
    evaluate_candidate,
    load_snapshot,
    parse_utc,
)

RULE = MODE_RULES["trifecta"]


def read_candidate_documents(root: Path, pattern: str) -> list[tuple[Path, dict[str, Any]]]:
    documents: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(root.rglob(pattern)):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            documents.append((path, {"_load_error": f"無法讀取 JSON：{exc}"}))
            continue
        if not isinstance(payload, dict):
            documents.append((path, {"_load_error": "JSON 根節點必須為物件"}))
        else:
            documents.append((path, payload))
    return documents


def build_snapshot_gate(
    snapshot: sqlite3.Row,
    model_generated: datetime,
    requested_label: str,
    max_capture_delta_seconds: int,
) -> list[str]:
    errors: list[str] = []
    captured_at = parse_utc(snapshot["captured_at_utc"])
    if snapshot["pool_type"] != RULE["pool_type"]:
        errors.append("pool_type 不是 TRIFECTA_ORDERED")
    if snapshot["snapshot_label"] != requested_label:
        errors.append(f"快照標籤不是要求的 {requested_label}")
    if snapshot["status"] != "complete":
        errors.append("snapshot status 不是 complete")
    if snapshot["quote_completeness"] not in {"full", "partial"}:
        errors.append("quote_completeness 不是 full／partial")
    if int(snapshot["expected_leg_count"]) != 1:
        errors.append("三重彩彩池必須只有 1 關")
    if model_generated > captured_at:
        errors.append("模型候選生成時間晚於賽前快照")
    if not snapshot["scheduled_anchor_start_utc"]:
        errors.append("缺少 scheduled_anchor_start_utc")
    else:
        if captured_at >= parse_utc(snapshot["scheduled_anchor_start_utc"]):
            errors.append("快照不在錨點場開跑前")
    delta = snapshot["capture_delta_seconds"]
    if delta is None:
        errors.append("缺少 capture_delta_seconds，無法稽核 T-minus 偏差")
    elif abs(int(delta)) > max_capture_delta_seconds:
        errors.append(f"快照偏差超過 {max_capture_delta_seconds} 秒")
    return errors


def actual_winning_key(conn: sqlite3.Connection, pool_event_id: int) -> tuple[str | None, str | None]:
    members = conn.execute(
        """
        SELECT leg_no, finish_position, runner_no
        FROM official_pool_result_members
        WHERE pool_event_id = ? AND leg_no = 1 AND finish_position BETWEEN 1 AND 3
        ORDER BY leg_no, finish_position
        """,
        (pool_event_id,),
    ).fetchall()
    if len(members) != 3 or [int(row["finish_position"]) for row in members] != [1, 2, 3]:
        return None, "官方結果未提供完整 1-2-3 名次"
    return "|".join(
        f"L{row['leg_no']}:P{row['finish_position']}={row['runner_no']}" for row in members
    ), None


def actual_settlement(
    conn: sqlite3.Connection,
    snapshot: sqlite3.Row,
    selection_key: str,
    stake: float,
) -> dict[str, Any]:
    winner_key, result_error = actual_winning_key(conn, int(snapshot["pool_event_id"]))
    base = {
        "settled": False,
        "settlement_reason": None,
        "official_winning_selection_key": winner_key,
        "hit": None,
        "actual_payout_per_unit": None,
        "actual_payout_unit": None,
        "actual_payout_is_return_inclusive": None,
        "actual_gross_return": None,
        "actual_net_return": None,
    }
    if result_error:
        base["settlement_reason"] = result_error
        return base
    if winner_key != selection_key:
        base.update({
            "settled": True,
            "settlement_reason": "已結算落敗",
            "hit": False,
            "actual_gross_return": 0.0,
            "actual_net_return": -stake,
        })
        return base
    payout = conn.execute(
        """
        SELECT payout_per_unit, payout_unit, payout_is_return_inclusive
        FROM official_pool_payouts
        WHERE pool_event_id = ?
          AND payout_tier = 'MAIN'
          AND winning_selection_key = ?
        """,
        (snapshot["pool_event_id"], selection_key),
    ).fetchone()
    if payout is None:
        base["settlement_reason"] = "結果命中但缺少相符 MAIN 官方派彩"
        return base
    multiple = float(payout["payout_per_unit"]) / float(payout["payout_unit"])
    if int(payout["payout_is_return_inclusive"]) == 0:
        multiple += 1.0
    gross = stake * multiple
    base.update({
        "settled": True,
        "settlement_reason": "已結算命中",
        "hit": True,
        "actual_payout_per_unit": float(payout["payout_per_unit"]),
        "actual_payout_unit": float(payout["payout_unit"]),
        "actual_payout_is_return_inclusive": bool(payout["payout_is_return_inclusive"]),
        "actual_gross_return": gross,
        "actual_net_return": gross - stake,
    })
    return base


def csv_write(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="V10.2 三重彩 T-15／T-5 嚴格批量回測")
    parser.add_argument("--db", required=True, help="已套用複合彩池 schema 的 SQLite archive")
    parser.add_argument("--candidate-root", required=True, help="歷史候選 JSON 根目錄；每個檔案為一個已固定模型批次")
    parser.add_argument("--snapshot-label", required=True, choices=["T_MINUS_15", "T_MINUS_5"])
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--candidate-glob", default="*.json", help="遞迴搜尋候選檔模式，預設 *.json")
    parser.add_argument("--max-capture-delta-seconds", type=int, default=180, help="T-minus 實際捕捉偏差上限，預設 180 秒")
    args = parser.parse_args()
    if args.max_capture_delta_seconds < 0:
        raise ValueError("max-capture-delta-seconds 必須非負")

    root = Path(args.candidate_root)
    if not root.is_dir():
        raise ValueError(f"candidate-root 不是目錄：{root}")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    documents = read_candidate_documents(root, args.candidate_glob)
    if not documents:
        raise ValueError("找不到任何候選 JSON 檔")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    details: list[dict[str, Any]] = []
    errors: Counter[str] = Counter()

    for path, doc in documents:
        file_path = str(path)
        if "_load_error" in doc:
            details.append({"candidate_file": file_path, "eligible": False, "exclusion_reason": doc["_load_error"]})
            errors[doc["_load_error"]] += 1
            continue
        try:
            snapshot_id = int(doc["pool_snapshot_id"])
            model_generated_at = parse_utc(str(doc["model_generated_at_utc"]))
            candidates = doc["candidates"]
            if not isinstance(candidates, list) or not candidates:
                raise ValueError("candidates 必須為非空陣列")
            snapshot = load_snapshot(conn, snapshot_id)
            gates = build_snapshot_gate(
                snapshot, model_generated_at, args.snapshot_label, args.max_capture_delta_seconds
            )
        except (KeyError, ValueError, TypeError) as exc:
            reason = f"批次檔／快照無效：{exc}"
            details.append({"candidate_file": file_path, "eligible": False, "exclusion_reason": reason})
            errors[reason] += 1
            continue

        for candidate_index, candidate in enumerate(candidates, start=1):
            base = {
                "candidate_file": file_path,
                "candidate_index": candidate_index,
                "pool_snapshot_id": snapshot_id,
                "pool_event_id": snapshot["pool_event_id"],
                "snapshot_label": snapshot["snapshot_label"],
                "snapshot_captured_at_utc": snapshot["captured_at_utc"],
                "scheduled_anchor_start_utc": snapshot["scheduled_anchor_start_utc"],
                "capture_delta_seconds": snapshot["capture_delta_seconds"],
                "model_generated_at_utc": doc["model_generated_at_utc"],
                "snapshot_quote_completeness": snapshot["quote_completeness"],
            }
            if gates:
                row = {
                    **base,
                    "selection_key": str(candidate.get("selection_key", "")),
                    "predicted_hit_probability": candidate.get("predicted_hit_probability"),
                    "stake": candidate.get("stake"),
                    "payout_tier": "MAIN",
                    "eligible": False,
                    "exclusion_reason": "; ".join(gates),
                }
            else:
                row = {**base, **evaluate_candidate(conn, snapshot, candidate, RULE, model_generated_at)}
            if not row.get("eligible"):
                errors[str(row.get("exclusion_reason", "未分類排除原因"))] += 1
                row.update(actual_settlement(conn, snapshot, str(row.get("selection_key", "")), float(candidate.get("stake", 0) or 0)))
            else:
                row.update(actual_settlement(conn, snapshot, row["selection_key"], float(row["stake"])))
            details.append(row)

    conn.close()
    eligible = [row for row in details if row.get("eligible") is True]
    settled = [row for row in eligible if row.get("settled") is True]
    hits = [row for row in settled if row.get("hit") is True]
    indicator_stake = sum(float(row["stake"]) for row in eligible)
    indicator_net = sum(float(row["indicator_expected_net"]) for row in eligible)
    settled_stake = sum(float(row["stake"]) for row in settled)
    actual_net = sum(float(row["actual_net_return"]) for row in settled)
    actual_gross = sum(float(row["actual_gross_return"]) for row in settled)

    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for row in sorted(settled, key=lambda r: (str(r.get("snapshot_captured_at_utc", "")), str(r.get("candidate_file", "")), int(r.get("candidate_index", 0)))):
        cumulative += float(row["actual_net_return"])
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
        row["actual_cumulative_net"] = cumulative
        row["actual_drawdown"] = peak - cumulative

    summary = {
        "mode": "trifecta_batch_backtest",
        "snapshot_label": args.snapshot_label,
        "max_capture_delta_seconds": args.max_capture_delta_seconds,
        "candidate_files_scanned": len(documents),
        "candidate_count": len(details),
        "priced_candidate_count": len(eligible),
        "priced_candidate_coverage": (len(eligible) / len(details)) if details else None,
        "indicator_total_stake": indicator_stake,
        "indicator_total_expected_net": indicator_net if eligible else None,
        "portfolio_indicator_ev_per_stake": (indicator_net / indicator_stake) if indicator_stake else None,
        "settled_candidate_count": len(settled),
        "settlement_coverage_of_priced": (len(settled) / len(eligible)) if eligible else None,
        "hit_count": len(hits),
        "hit_rate": (len(hits) / len(settled)) if settled else None,
        "actual_total_stake": settled_stake,
        "actual_total_gross_return": actual_gross if settled else None,
        "actual_total_net_return": actual_net if settled else None,
        "actual_roi": (actual_net / settled_stake) if settled_stake else None,
        "actual_max_drawdown": max_drawdown if settled else None,
        "exclusion_reason_counts": dict(errors.most_common()),
        "notes": [
            "指標 EV 只使用賽前同一組合的顯示／估計報價；不使用 official_pool_payouts。",
            "實現 ROI 只在候選已固定後，使用 official_pool_result_members 和 official_pool_payouts 結算。",
            "若沒有合資格 T-15／T-5 快照、特定組合報價或正式結果／派彩，相關指標必須為 N/A，而非以最終市場資料補值。",
            "平分彩金的賽前估計派彩會變動；歷史指標 EV與實現 ROI均不構成未來回報保證。",
        ],
    }
    columns = [
        "candidate_file", "candidate_index", "pool_snapshot_id", "pool_event_id", "snapshot_label",
        "snapshot_captured_at_utc", "scheduled_anchor_start_utc", "capture_delta_seconds",
        "model_generated_at_utc", "snapshot_quote_completeness", "selection_key",
        "predicted_hit_probability", "stake", "payout_tier", "eligible", "exclusion_reason",
        "quote_kind", "quote_value", "quote_unit", "quote_is_return_inclusive",
        "gross_return_per_stake", "indicator_ev_per_stake", "indicator_expected_net",
        "settled", "settlement_reason", "official_winning_selection_key", "hit",
        "actual_payout_per_unit", "actual_payout_unit", "actual_payout_is_return_inclusive",
        "actual_gross_return", "actual_net_return", "actual_cumulative_net", "actual_drawdown",
    ]
    csv_write(output_dir / "trifecta_batch_details.csv", details, columns)
    csv_write(output_dir / "trifecta_batch_exclusions.csv", [row for row in details if not row.get("eligible")], columns)
    (output_dir / "trifecta_batch_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
