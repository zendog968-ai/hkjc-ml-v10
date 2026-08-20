#!/usr/bin/env python3
"""Summarise only officially settled York S1 overseas research snapshots.

This utility intentionally excludes user-reported / pending results. It does not
modify V10 or N6 artifacts; it writes a separate report under reports/ only.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Strict scores require a field-compatible immutable pre-race probability snapshot.
# S1-5 and S1-7 had scratches after the available snapshot, so their retained
# probabilities cannot be used for Brier or EV/ROI without a fresh snapshot.
INCOMPATIBLE_FIELD_RACES = {
    5: "14 Ghaiyya scratched after the available pre-race market/probability snapshot",
    7: "four final scratches make the available pre-race market/probability snapshot field-incompatible",
}

OFFICIAL_SETTLEMENTS: dict[int, dict[str, Any]] = {
    1: {
        "official_url": "https://bet.hkjc.com/en/racing/results/2026-08-19/S1/1",
        "finish_order": [18, 5, 9, 16],
        "win_dividends": {18: 92.50},
        "place_dividends": {18: 26.00, 5: 19.00, 9: 18.50, 16: 47.00},
        "place_dividend_count": 4,
    },
    2: {
        "official_url": "https://bet.hkjc.com/en/racing/results/2026-08-19/S1/2",
        "finish_order": [7, 6, 10, 1],
        "win_dividends": {7: 18.00},
        "place_dividends": {7: 11.50, 6: 15.00, 10: 20.00},
        "place_dividend_count": 3,
        "scratched": [4],
    },
    3: {
        "official_url": "https://bet.hkjc.com/en/racing/results/2026-08-19/S1/3",
        "finish_order": [2, 5, 1, 6],
        "win_dividends": {2: 23.50},
        "place_dividends": {2: 12.50, 5: 20.50},
        "place_dividend_count": 2,
    },
    4: {
        "official_url": "https://bet.hkjc.com/en/racing/results/2026-08-19/S1/4",
        "finish_order": [9, 1, 7, 3],
        "win_dividends": {9: 136.00},
        "place_dividends": {9: 20.00, 1: 24.50, 7: 11.00},
        "place_dividend_count": 3,
    },
    5: {
        "official_url": "https://bet.hkjc.com/en/racing/results/2026-08-19/S1/5",
        "finish_order": [4, 17, 16, 11],
        "win_dividends": {4: 31.00},
        "place_dividends": {4: 14.50, 17: 65.50, 16: 37.00},
        "place_dividend_count": 3,
        "scratched": [14],
    },
    6: {
        "official_url": "https://bet.hkjc.com/en/racing/results/2026-08-19/S1/6",
        "finish_order": [8, 6, 1, 4],
        "win_dividends": {8: 91.00},
        "place_dividends": {8: 28.50, 6: 43.50, 1: 16.50},
        "place_dividend_count": 3,
    },
}


@dataclass(frozen=True)
class BetResult:
    race_no: int
    runner_no: int
    horse_name: str
    market: str
    stake_hkd: float
    return_hkd: float
    won: bool


def load_artifact(runtime_dir: Path, race_no: int) -> dict[str, Any]:
    path = runtime_dir / f"york_s1_{race_no}_deep.json"
    with path.open(encoding="utf-8") as handle:
        artifact = json.load(handle)
    if artifact.get("market_research", {}).get("status") != "complete":
        raise ValueError(f"S1-{race_no}: incomplete market research artifact")
    return artifact


def research_rows(artifact: dict[str, Any], scratched: set[int]) -> list[dict[str, Any]]:
    rows = []
    for starter in artifact.get("starters", []):
        runner_no = starter.get("runner_no")
        market = starter.get("market_research") or {}
        if runner_no in scratched or market.get("match_status") != "matched":
            continue
        if market.get("ev_kelly_status") != "available_research_only":
            continue
        row = dict(starter)
        row["market_research"] = market
        rows.append(row)
    return sorted(rows, key=lambda item: int(item.get("deep_rank", 9999)))


def settle_positive_ev_bets(
    race_no: int,
    rows: list[dict[str, Any]],
    settlement: dict[str, Any],
    stake_hkd: float,
) -> tuple[list[BetResult], list[BetResult]]:
    win_bets: list[BetResult] = []
    place_bets: list[BetResult] = []
    for row in rows:
        market = row["market_research"]
        runner_no = int(row["runner_no"])
        horse_name = str(row.get("horse_name", ""))
        if float(market.get("win_ev") or 0.0) > 0.0:
            dividend = float(settlement["win_dividends"].get(runner_no, 0.0))
            win_bets.append(BetResult(
                race_no, runner_no, horse_name, "Win", stake_hkd, dividend if dividend else 0.0, dividend > 0.0
            ))
        if float(market.get("place_ev") or 0.0) > 0.0:
            dividend = float(settlement["place_dividends"].get(runner_no, 0.0))
            place_bets.append(BetResult(
                race_no, runner_no, horse_name, "Place", stake_hkd, dividend if dividend else 0.0, dividend > 0.0
            ))
    return win_bets, place_bets


def totals(bets: list[BetResult]) -> dict[str, float | int | None]:
    stake = sum(item.stake_hkd for item in bets)
    returned = sum(item.return_hkd for item in bets)
    return {
        "bets": len(bets),
        "wins": sum(1 for item in bets if item.won),
        "stake_hkd": round(stake, 2),
        "return_hkd": round(returned, 2),
        "profit_hkd": round(returned - stake, 2),
        "roi": round((returned - stake) / stake, 6) if stake else None,
    }


def pct(value: float | None) -> str:
    return "N/A" if value is None else f"{value * 100:.2f}%"


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# York S1 已正式結算的研究快照摘要",
        "",
        f"生成時間（UTC）：{summary['generated_at_utc']}",
        "",
        "> 此報告只使用已在 HKJC 官方結果頁確認的賽果與派彩。未發布或使用者提供但未核實的結果不會計入命中率或 ROI。研究性 EV 及機率來自未校準海外深度代理，並非 V10.2 正式模型表現。",
        "",
        "## 資料覆蓋",
        "",
        "| 場次 | 官方結果 | Top-1 馬號 | Top-1 命中 | 頭馬是否在研究前 3 | 官方頁 |",
        "|---:|---|---:|---|---|---|",
    ]
    for race in summary["races"]:
        finish = "-".join(map(str, race["finish_order"]))
        lines.append(
            f"| S1-{race['race_no']} | {finish} | {race['top1_runner_no']} | "
            f"{'是' if race['top1_win'] else '否'} | {'是' if race['winner_in_top3'] else '否'} | "
            f"[HKJC]({race['official_url']}) |"
        )
    accuracy = summary["accuracy"]
    lines += [
        "",
        "## 研究排序命中",
        "",
        f"- 嚴格 field-compatible 的正式結算場次：{accuracy['settled_races']} 場；未結算／不納入場次：S1-{', S1-'.join(map(str, summary['pending_races'])) or '無'}。",
        f"- Top-1 頭馬命中：{accuracy['top1_wins']}/{accuracy['settled_races']}（{pct(accuracy['top1_win_rate'])}）。",
        f"- 頭馬在研究前 3：{accuracy['winner_in_top3']}/{accuracy['settled_races']}（{pct(accuracy['winner_in_top3_rate'])}）。",
        f"- 多分類 Brier 分數（每場 \u03a3(p−y)² 平均）：{accuracy['mean_multiclass_brier']:.4f}；未有同口徑海外基準，僅作探索性記錄。",
        "",
        "## 統一模擬規則下的研究性回報",
        "",
        "模擬規則：每一匹封存時點 `EV > 0` 的馬，各以 HK$10 平注計算；派彩使用 HKJC 官方已公布的每 HK$10 派彩。這不是使用者實際投注紀錄，也不表示可重複的策略績效。",
        "",
        "| 市場 | 研究性正 EV 注數 | 命中注數 | 本金 | 回報 | 盈虧 | ROI |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for key in ("win", "place", "combined"):
        total = summary["simulation"][key]
        label = {"win": "Win", "place": "Place", "combined": "合併"}[key]
        lines.append(
            f"| {label} | {total['bets']} | {total['wins']} | HK${total['stake_hkd']:.2f} | "
            f"HK${total['return_hkd']:.2f} | HK${total['profit_hkd']:.2f} | {pct(total['roi'])} |"
        )
    lines += [
        "",
        "## 資料限制",
        "",
        "本日目前僅有少量 field-compatible 的正式結算場次，遠少於 15 場探索性門檻；所有命中率和 ROI 只可作當日暫時觀察。S1-5 的14號退出與 S1-7 的四匹退出，使可用賽前市場／機率快照與最終有效出馬不相容，兩場均嚴格排除；任何未有官方結果／派彩的場次也會排除。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", type=Path, default=Path("runtime/overseas_deep"))
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    parser.add_argument("--stake-hkd", type=float, default=10.0)
    args = parser.parse_args()

    races: list[dict[str, Any]] = []
    all_win_bets: list[BetResult] = []
    all_place_bets: list[BetResult] = []
    top1_wins = 0
    winner_in_top3 = 0

    for race_no, settlement in OFFICIAL_SETTLEMENTS.items():
        if race_no in INCOMPATIBLE_FIELD_RACES:
            continue
        artifact = load_artifact(args.runtime_dir, race_no)
        rows = research_rows(artifact, set(settlement.get("scratched", [])))
        if not rows:
            raise ValueError(f"S1-{race_no}: no eligible matched research rows")
        winner = settlement["finish_order"][0]
        top1 = int(rows[0]["runner_no"])
        top3 = [int(row["runner_no"]) for row in rows[:3]]
        race_brier = sum(
            (float(row["market_research"]["research_win_probability"]) - int(int(row["runner_no"]) == winner)) ** 2
            for row in rows
        )
        top1_wins += int(top1 == winner)
        winner_in_top3 += int(winner in top3)
        win_bets, place_bets = settle_positive_ev_bets(race_no, rows, settlement, args.stake_hkd)
        all_win_bets.extend(win_bets)
        all_place_bets.extend(place_bets)
        races.append({
            "race_no": race_no,
            "official_url": settlement["official_url"],
            "finish_order": settlement["finish_order"],
            "top1_runner_no": top1,
            "top1_horse_name": rows[0]["horse_name"],
            "top1_win": top1 == winner,
            "winner_in_top3": winner in top3,
            "top3_runner_numbers": top3,
            "multiclass_brier": race_brier,
            "research_positive_win_ev": [item.__dict__ for item in win_bets],
            "research_positive_place_ev": [item.__dict__ for item in place_bets],
        })

    combined_bets = all_win_bets + all_place_bets
    summary = {
        "schema_version": "v10_york_s1_settled_summary_v2",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "research_only": True,
        "stake_rule": f"HK${args.stake_hkd:.2f} flat stake per pre-race positive-EV row per market",
        "races": races,
        "excluded_races": INCOMPATIBLE_FIELD_RACES,
        "pending_races": [5, 7],
        "accuracy": {
            "settled_races": len(races),
            "top1_wins": top1_wins,
            "top1_win_rate": top1_wins / len(races),
            "winner_in_top3": winner_in_top3,
            "winner_in_top3_rate": winner_in_top3 / len(races),
            "mean_multiclass_brier": sum(race["multiclass_brier"] for race in races) / len(races),
            "exploratory": len(races) < 15,
        },
        "simulation": {
            "win": totals(all_win_bets),
            "place": totals(all_place_bets),
            "combined": totals(combined_bets),
        },
    }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_md.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.output_md.write_text(render_markdown(summary) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
