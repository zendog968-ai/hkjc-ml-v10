#!/usr/bin/env python3
"""Bind an immutable unbound Double Trio candidate file to one verified pool snapshot."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from query_complex_pool_ev import parse_utc


def main() -> int:
    parser = argparse.ArgumentParser(description="以時間及事件身分閘門綁定 V10.2 孖T候選與官方快照")
    parser.add_argument("--db", required=True)
    parser.add_argument("--unbound-candidates", required=True)
    parser.add_argument("--pool-snapshot-id", required=True, type=int)
    parser.add_argument("--snapshot-label", required=True, choices=["T_MINUS_15", "T_MINUS_5"])
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    source_path = Path(args.unbound_candidates)
    source_bytes = source_path.read_bytes()
    source = json.loads(source_bytes.decode("utf-8"))
    if source.get("candidate_format") != "V10_2_DOUBLE_TRIO_UNBOUND_V1" or source.get("pool_type") != "DOUBLE_TRIO":
        raise ValueError("unbound-candidates 不是預期的 V10.2 孖T未綁定候選格式")
    generated = parse_utc(str(source["model_generated_at_utc"]))
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    snapshot = conn.execute(
        """SELECT e.pool_type,e.pool_event_code,e.meeting_date,e.meeting_racecourse,e.expected_leg_count,
                  s.pool_snapshot_id,s.snapshot_label,s.captured_at_utc,s.anchor_leg_no,
                  s.scheduled_anchor_start_utc,s.capture_delta_seconds,s.status,s.quote_completeness
           FROM pre_race_pool_events e JOIN pre_race_pool_snapshots s USING(pool_event_id)
           WHERE s.pool_snapshot_id=?""",
        (args.pool_snapshot_id,),
    ).fetchone()
    conn.close()
    if snapshot is None:
        raise ValueError("找不到 pool_snapshot_id")
    errors = []
    if snapshot["pool_type"] != "DOUBLE_TRIO": errors.append("快照彩池不是 DOUBLE_TRIO")
    if snapshot["pool_event_code"] != source["pool_event_code"]: errors.append("pool_event_code 與未綁定候選不一致")
    if snapshot["snapshot_label"] != args.snapshot_label: errors.append("快照標籤與要求不一致")
    if snapshot["status"] != "complete": errors.append("快照 status 不是 complete")
    if snapshot["quote_completeness"] not in {"full", "partial"}: errors.append("快照沒有可用特定組合報價狀態")
    if int(snapshot["expected_leg_count"]) != 2 or int(snapshot["anchor_leg_no"]) != 1: errors.append("孖T必須是兩關且以第一關作時間錨點")
    captured = parse_utc(snapshot["captured_at_utc"])
    if generated > captured: errors.append("模型候選生成時間晚於快照")
    if not snapshot["scheduled_anchor_start_utc"] or captured >= parse_utc(snapshot["scheduled_anchor_start_utc"]): errors.append("快照不在第一關開跑前")
    if errors:
        raise ValueError("；".join(errors))
    bound = {
        "candidate_format": "V10_2_DOUBLE_TRIO_BOUND_V1",
        "pool_snapshot_id": int(snapshot["pool_snapshot_id"]),
        "snapshot_label": snapshot["snapshot_label"],
        "snapshot_captured_at_utc": snapshot["captured_at_utc"],
        "pool_event_code": snapshot["pool_event_code"],
        "model_generated_at_utc": source["model_generated_at_utc"],
        "unbound_candidate_path": str(source_path),
        "unbound_candidate_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "candidates": source["candidates"],
        "notes": [
            "模型候選先於快照固定；本綁定步驟不修改候選組合、機率或固定研究注額。",
            "後續 query_complex_pool_ev.py --mode double_trio 或批量回測器只會使用此快照內同一 MAIN 組合的特定報價。",
        ],
    }
    Path(args.output).write_text(json.dumps(bound, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"pool_snapshot_id": bound["pool_snapshot_id"], "candidate_count": len(bound["candidates"]), "output": args.output}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
