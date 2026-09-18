#!/usr/bin/env python3
"""Settle and audit S5-3 using immutable pre-race public RPR/TS research.

This is an exploratory audit only.  It does not write V10.2, N6, odds, EV,
or Kelly artifacts.  The official result is hard-coded only after HKJC's
public result page confirmed the positions on 2026-08-22.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
PRE = ROOT / "reports/overseas_deep/S5_3_YORK_PUBLIC_RPR_TS_RESEARCH_2026-08-22.json"
SETTLEMENT = ROOT / "reports/overseas_deep/S5_3_OFFICIAL_SETTLEMENT_2026-08-22.json"
AUDIT_JSON = ROOT / "reports/overseas_deep/S5_3_PUBLIC_RPR_TS_EXPLORATORY_AUDIT_2026-08-22.json"
AUDIT_MD = ROOT / "reports/overseas_deep/S5_3_PUBLIC_RPR_TS_EXPLORATORY_AUDIT_2026-08-22.md"
RESULT_URL = "https://racing.hkjc.com/en-us/overseas/results?RaceDate=20260822&Racecourse=S5&RaceNo=3"

OFFICIAL_TOP4 = [
    {"place": 1, "horse_no": 8, "horse_name": "Daiquiri Bay"},
    {"place": 2, "horse_no": 9, "horse_name": "Hopewell Rock"},
    {"place": 3, "horse_no": 1, "horse_name": "Opportunity"},
    {"place": 4, "horse_no": 18, "horse_name": "Ascending"},
]


def sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_sha256(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def main() -> int:
    pre = json.loads(PRE.read_text(encoding="utf-8"))
    race = pre["race"]
    runners = list(pre["runners"])
    if race["simulcast"] != "S5-3" or race["active_runners"] != 22 or race["place_dividends"] != 4:
        raise ValueError("賽前工件不是22匹、4位置的S5-3契約；拒絕結算。")
    if pre["identity_gate"]["status"] != "complete" or pre["probability_contract"]["win_probability_sum"] != 1.0:
        raise ValueError("賽前身份或機率守恆閘門未通過；拒絕結算。")

    by_no = {int(r["runner_no"]): r for r in runners}
    official_numbers = [x["horse_no"] for x in OFFICIAL_TOP4]
    if any(no not in by_no for no in official_numbers):
        raise ValueError("官方前四包含不在封存有效field的馬匹；拒絕結算。")

    rank_order = [int(r["runner_no"]) for r in sorted(runners, key=lambda x: int(x["deep_rank"]))]
    actual_top4_set = set(official_numbers)
    winner_no = official_numbers[0]
    winner = by_no[winner_no]
    win_brier = sum((float(r["research_win_probability_uncalibrated"]) - (1.0 if int(r["runner_no"]) == winner_no else 0.0)) ** 2 for r in runners) / len(runners)
    place_brier = sum((float(r["research_place_probability_uncalibrated"]) - (1.0 if int(r["runner_no"]) in actual_top4_set else 0.0)) ** 2 for r in runners) / len(runners)
    winner_prob = float(winner["research_win_probability_uncalibrated"])

    settlement = {
        "status": "official_hkjc_confirmed",
        "race": {
            "date": race["date"], "simulcast": race["simulcast"], "title": race["title"],
            "venue": race["venue"], "scheduled_hkt": race["scheduled_hkt"],
            "going": race["going_hkjc"], "active_runners": race["active_runners"],
            "place_dividends": race["place_dividends"],
        },
        "official_source_url": RESULT_URL,
        "official_top4": OFFICIAL_TOP4,
        "confirmed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "settlement_type": "official_result_only_no_dividend_extracted",
    }
    SETTLEMENT.write_text(json.dumps(settlement, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    audit_base = {
        "status": "exploratory_uncalibrated_probabilistic_audit",
        "race": settlement["race"],
        "pre_race_path": str(PRE),
        "pre_race_sha256": sha256_path(PRE),
        "official_settlement_path": str(SETTLEMENT),
        "official_settlement_sha256": sha256_path(SETTLEMENT),
        "official_top4": OFFICIAL_TOP4,
        "pre_race_nr_order": rank_order,
        "winner_pre_race_rank": int(winner["deep_rank"]),
        "top1_hit": int(rank_order[0]) == winner_no,
        "top3_contains_winner": winner_no in rank_order[:3],
        "top4_contains_winner": winner_no in rank_order[:4],
        "top4_actual_top4_overlap_count": len(set(rank_order[:4]) & actual_top4_set),
        "top4_actual_top4_overlap_horses": sorted(set(rank_order[:4]) & actual_top4_set),
        "metrics_uncalibrated_research_only": {
            "win_brier_per_runner": round(win_brier, 6),
            "place_brier_per_runner_four_places": round(place_brier, 6),
            "winner_negative_log_probability": round(-math.log(max(winner_prob, 1e-15)), 6),
            "winner_win_probability": winner_prob,
        },
        "hard_gates": {
            "formal_ev": "N/A", "kelly": "N/A", "roi": "N/A",
            "model_calibration": "N/A_zero_qualified_historical_events_before_this_cohort",
            "v10_2": "not_used", "n6": "disabled_non_hk",
        },
        "interpretation": "N=1 four-place probabilistic exploratory record; do not recalibrate, compare as a trend, or use as a betting signal.",
    }
    audit_base["audit_sha256"] = canonical_sha256(audit_base)
    AUDIT_JSON.write_text(json.dumps(audit_base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    rows = "\n".join(
        f"| {x['place']} | #{x['horse_no']} {x['horse_name']} | {by_no[x['horse_no']]['deep_rank']} | {float(by_no[x['horse_no']]['research_win_probability_uncalibrated']):.2%} | {float(by_no[x['horse_no']]['research_place_probability_uncalibrated']):.2%} |"
        for x in OFFICIAL_TOP4
    )
    md = f"""# S5-3 Ebor Handicap｜正式探索性覆盤\n\n> **研究狀態：**HKJC官方結果已確認；機率、Brier與差異均為未校準研究紀錄，不構成V10.2正式輸出、EV、Kelly或投注結論。\n\n## 官方結算\n\n| 名次 | 馬匹 | 賽前RPR／TS排名 | 賽前Win代理 | 賽前4位置代理 |\n|---:|---|---:|---:|---:|\n{rows}\n\n## 排名與誤差\n\n| 指標 | 結果 |\n|---|---:|\n| 頭馬賽前排名 | {winner['deep_rank']} |\n| Top-1命中 | {audit_base['top1_hit']} |\n| Top-3包含頭馬 | {audit_base['top3_contains_winner']} |\n| Top-4包含頭馬 | {audit_base['top4_contains_winner']} |\n| 賽前Top-4與官方Top-4重疊 | {audit_base['top4_actual_top4_overlap_count']}/4 |\n| 未校準Win Brier（逐馬） | {win_brier:.6f} |\n| 未校準4位置Place Brier（逐馬） | {place_brier:.6f} |\n\n## 資料限制\n\n本場為22匹、4位置派彩的單一探索性事件。所有數值僅可累積至同版本的未來封存事件後再研究，現時不可據此調整任何模型或產生投注指示。\n\n## 來源\n\n- HKJC官方結果：{RESULT_URL}\n- 賽前RPR／TS工件：`{PRE.name}`\n"""
    AUDIT_MD.write_text(md, encoding="utf-8")
    print(json.dumps({"status": "ok", "audit": str(AUDIT_JSON), "settlement": str(SETTLEMENT), "winner_rank": int(winner["deep_rank"]), "top4_overlap": audit_base["top4_actual_top4_overlap_count"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
