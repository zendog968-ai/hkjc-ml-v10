#!/usr/bin/env python3
"""Audit S1-4 to S1-6 overseas research ranking and market gaps.

Reads immutable pre-race overseas deep artifacts and hard-coded HKJC official
results/dividends verified on 2026-08-20.  It does not write V10, N6, or SQLite.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OFFICIAL: dict[int, dict[str, Any]] = {
    4: {
        "finish": [9, 1, 7, 3],
        "final_win_odds": {9: 13.0, 1: 17.0, 7: 3.0, 3: 1.5},
        "url": "https://bet.hkjc.com/en/racing/results/2026-08-19/S1/4",
    },
    5: {
        "finish": [4, 17, 16, 11],
        "final_win_odds": {4: 3.1, 17: 25.0, 16: 11.0, 11: 87.0},
        "url": "https://bet.hkjc.com/en/racing/results/2026-08-19/S1/5",
        "scratched": [14],
    },
    6: {
        "finish": [8, 6, 1, 4],
        "final_win_odds": {8: 9.1, 6: 13.0, 1: 3.6, 4: 16.0},
        "url": "https://bet.hkjc.com/en/racing/results/2026-08-19/S1/6",
    },
}


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def eligible(artifact: dict[str, Any], scratched: set[int]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for starter in artifact.get("starters", []):
        runner = int(starter["runner_no"])
        market = starter.get("market_research") or {}
        if runner in scratched or market.get("match_status") != "matched":
            continue
        if market.get("ev_kelly_status") != "available_research_only":
            continue
        row = dict(starter)
        row["market_research"] = market
        rows.append(row)
    return sorted(rows, key=lambda row: int(row.get("deep_rank", 999)))


def pformat(value: float | None) -> str:
    return "N/A" if value is None else f"{value * 100:.2f}%"


def fformat(value: float | None) -> str:
    """Format a probability difference expressed as a percentage-point delta."""
    return "N/A" if value is None else f"{value * 100:+.2f}pp"


def final_implied(odds: float | None) -> float | None:
    return None if not odds or odds <= 0 else 1.0 / odds


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", type=Path, default=Path("runtime/overseas_deep"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    lines = [
        "# York S1-4 至 S1-6：研究排序失準與市場差距審計",
        "",
        f"生成時間（UTC）：{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        "> 此審計比較封存賽前研究快照與 HKJC 已公布官方結果／最終 Win Odds。研究性機率尚未校準，市場差距不是市場錯價的證明，也不構成投注建議。",
        "",
        "## 場次級摘要",
        "",
        "| 場次 | 研究首選 | 首選機率 | 最終首選 Win Odds | 官方頭馬 | 頭馬研究排名 | 頭馬研究機率 | 頭馬最終 Win Odds |",
        "|---:|---|---:|---:|---|---:|---:|---:|",
    ]
    races: list[dict[str, Any]] = []
    for race_no, official in OFFICIAL.items():
        artifact = load(args.runtime_dir / f"york_s1_{race_no}_deep.json")
        rows = eligible(artifact, set(official.get("scratched", [])))
        by_runner = {int(row["runner_no"]): row for row in rows}
        top1 = rows[0]
        winner = int(official["finish"][0])
        winner_row = by_runner[winner]
        top_p = float(top1["market_research"]["research_win_probability"])
        win_p = float(winner_row["market_research"]["research_win_probability"])
        final_odds = official["final_win_odds"]
        lines.append(
            f"| S1-{race_no} | {top1['runner_no']} {top1['horse_name']} | {pformat(top_p)} | "
            f"{final_odds.get(int(top1['runner_no']), 'N/A')} | {winner} {winner_row['horse_name']} | "
            f"{winner_row['deep_rank']} | {pformat(win_p)} | {final_odds[winner]} |"
        )
        races.append({
            "race_no": race_no,
            "top1": top1,
            "winner": winner_row,
            "final_odds": final_odds,
            "official": official,
        })

    lines += ["", "## 研究機率與最終市場隱含機率", ""]
    for race in races:
        race_no = race["race_no"]
        top = race["top1"]
        winner = race["winner"]
        top_runner = int(top["runner_no"])
        winner_runner = int(winner["runner_no"])
        top_p = float(top["market_research"]["research_win_probability"])
        win_p = float(winner["market_research"]["research_win_probability"])
        top_market = final_implied(race["final_odds"].get(top_runner))
        win_market = final_implied(race["final_odds"].get(winner_runner))
        lines += [
            f"### S1-{race_no}",
            "",
            f"研究首選 **{top_runner} {top['horse_name']}**：研究機率 {pformat(top_p)}；"
            f"最終 Win Odds {race['final_odds'].get(top_runner, 'N/A')}；市場隱含機率 {pformat(top_market)}；"
            f"研究減市場差距 {fformat(top_p - top_market if top_market is not None else None)}。",
            "",
            f"官方頭馬 **{winner_runner} {winner['horse_name']}**：研究排名 {winner['deep_rank']}；研究機率 {pformat(win_p)}；"
            f"最終 Win Odds {race['final_odds'][winner_runner]}；市場隱含機率 {pformat(win_market)}；"
            f"研究減市場差距 {fformat(win_p - win_market if win_market is not None else None)}。",
            "",
        ]
        if race_no == 4:
            lines.append("誤差訊號：頭馬 Item 的 York 2/2、Good 3/3 條件資料在平滑後僅獲有限權重；研究把其列第 4，而最終市場也只給 13.0。研究首選 Ombudsman 以 48.03% 高居首，但最終市場隱含約 66.67%，顯示研究代理沒有充分反映市場對該馬的信心，亦未捕捉到最終第 4 的賽事結果。")
        elif race_no == 5:
            lines.append("誤差訊號：14 號已退出並從正式結算排除。頭馬 Small Fry 的長途、Good 與 York 特徵未進研究前列；賽前研究把焦點放在 Shrimp Shady、Team Player 與 Valedictory。長途 handicap 的步速、位置、負磅與非線性場地／路程互動均未在公開代理完整建模。")
        else:
            lines.append("誤差訊號：頭馬 Miss Yechance 的研究排名為第 5，公開代理僅給 7.32%，而最終市場隱含約 10.99%。19 駒 1000 米 handicap 的起步、分組、步速與檔位互動未被代理直接觀測；研究首選的 Good 場樣本 0/3 亦提醒能力分不應壓過條件資訊。")
        lines.append("")

    lines += [
        "## 可驗證的共同原因與非結論",
        "",
        "1. **機率校準不足：** 研究分數由公開 RPR／TS、路程、Going 與場地資料最小最大正規化後產生，並非以海外結果訓練的校準勝率。小場的高分馬可被推至過高機率，大場 handicap 的中段馬則可能被低估。",
        "2. **市場快照與最終市場不同：** EV 使用封存較早的 HKJC 盤口；最終 Win Odds 顯示市場在開跑前會再定價。此差距可被量化，但僅三場不可以稱為市場存在系統性偏差。",
        "3. **缺少關鍵可觀測特徵：** 目前沒有合法公開的 Timeform pace／步速、完整即時場地、跑法、近況／醫療與全面騎師／馬房條件交互資料；短途及 handicap 對這些因素較敏感。",
        "4. **樣本過小：** 三場失準不能支持任何特徵權重或策略的大改動；本文件提出的是待驗證假說，而非因果歸因。",
        "",
        "## 平行研究層改善與門檻",
        "",
        "- 保持 V10.2 本地正式機率、EV、Kelly 與 N6 不變；海外層繼續獨立 SQLite、獨立特徵、獨立版本。",
        "- 將每場最後一次有效官方 Win／Place 快照連同 scratches、snapshot timestamp、來源 HTML hash 與 field size 固化；任何有效出馬變動後，舊 EV 自動失效。",
        "- 把研究分數按 field size、race type（Group／handicap）、distance bucket 與 scratch state 分層校準；先累積至少 15 場探索性、150 場初步校準、再達 325 場走步驗證才考慮改善聲明。",
        "- 檢驗 RPR／TS、路程、Going、course 權重的單獨與交互貢獻；新增資料只可進平行候選模型，不能覆寫 V10.2。",
        "",
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines), encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
