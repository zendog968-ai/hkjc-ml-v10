#!/usr/bin/env python3
"""Read-only York S1 top-event research EV analysis.

Ranks races by the single highest available research Win EV in each race.  It
never treats the uncalibrated overseas proxy as a production betting signal.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float('inf') else None


def main() -> int:
    parser = argparse.ArgumentParser(description='分析 York S1 研究性最高 EV 賽事。')
    parser.add_argument('--runtime-dir', default='runtime/overseas_deep')
    parser.add_argument('--output', default='reports/overseas_deep/YORK_S1_TOP_3_RESEARCH_EV_2026-08-19.md')
    args = parser.parse_args()
    events = []
    for race_no in range(1, 8):
        path = Path(args.runtime_dir) / f'york_s1_{race_no}_deep.json'
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue
        market = payload.get('market_research') if isinstance(payload.get('market_research'), dict) else {}
        candidates = []
        for runner in payload.get('starters') if isinstance(payload.get('starters'), list) else []:
            entry = runner.get('market_research') if isinstance(runner.get('market_research'), dict) else {}
            if entry.get('ev_kelly_status') != 'available_research_only':
                continue
            win_ev = finite(entry.get('win_ev'))
            probability = finite(entry.get('research_win_probability'))
            odds = finite(entry.get('win_odds'))
            if win_ev is None or probability is None or odds is None:
                continue
            candidates.append({
                'race_no': race_no,
                'hkt_start_time': (payload.get('race') or {}).get('hkt_start_time'),
                'horse_name': runner.get('horse_name'),
                'runner_no': runner.get('runner_no'),
                'deep_rank': runner.get('deep_rank'),
                'research_win_probability': probability,
                'win_odds': odds,
                'win_ev': win_ev,
                'full_kelly': finite(entry.get('kelly_full_fraction')) if finite(entry.get('kelly_full_fraction')) is not None else max((win_ev / (odds - 1.0)), 0.0),
                'capped_kelly': finite(entry.get('kelly_fraction')),
                'captured_at_utc': market.get('captured_at_utc'),
                'matched_runners': market.get('matched_runner_count'),
                'expected_runners': market.get('expected_runner_count'),
                'research_status': entry.get('ev_kelly_status'),
            })
        if candidates:
            events.append(max(candidates, key=lambda row: (row['win_ev'], row['research_win_probability'], -int(row['runner_no']))))
    top = sorted(events, key=lambda row: (-row['win_ev'], row['race_no']))[:3]
    rows = []
    for position, row in enumerate(top, start=1):
        p, d = row['research_win_probability'], row['win_odds']
        b = d - 1
        rows.append(
            f"| {position} | S1-{row['race_no']} | {row['hkt_start_time'] or '—'} | {row['runner_no']} {row['horse_name']} | {p:.2%} | {d:.1f} | {row['win_ev']:+.2%} | `{p:.6f}×{d:.1f}−1` | `{(p*d-1):+.6f}` | `{p:.6f}×{b:.1f}−(1−{p:.6f})` / `{b:.1f}` | {(row['full_kelly'] or 0.0):.2%} | {(row['capped_kelly'] or 0.0):.2%} |"
        )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    text = r"""# York S1 研究性最高 Win EV：前三場賽事

> **研究與風險披露：** 此清單按每場中最高的海外深度研究代理 Win EV 排序。機率由未校準的公開 RPR、TS、路程／Going／場地特徵轉換而成；它不是 V10.2 正式機率或已獲歷史驗證的海外模型，不構成投注建議或任何回報保證。

## 排名方法

每場先只保留完成 HKJC 官方 Win／Place 全場身份匹配、且 `ev_kelly_status = available_research_only` 的馬匹；再選出該場最高 Win EV，最後於 S1-1 至 S1-7 排名。所有報價均取自個別工件內已封存的 HKJC 公開市場快照。

## 計算公式

令 $p$ 為研究性場內勝率代理、$D$ 為 HKJC 顯示的十進制 Win 賠率、$b=D-1$：

- **Win EV：** $EV = pD - 1$
- **全 Kelly：** $f^* = \frac{pb-(1-p)}{b} = \frac{pD-1}{D-1}$
- **Dashboard 顯示 Kelly：** $\min(\max(f^*, 0), 5\%)$

5% 是研究介面既有的風險上限，不是個人化資金分配建議。若未完成身份匹配、資料代理校準或外部驗證，系統應保留數值為 N/A。

## 前三場（按各場最高研究性 Win EV）

| 場內排序 | 賽事 | HKT 開跑 | 馬號／馬匹 | 研究性 $p$ | HKJC Win $D$ | Win EV | EV 代入 | EV 結果 | 全 Kelly 代入 | 全 Kelly | 顯示上限 Kelly |
|---:|---|---|---|---:|---:|---:|---|---:|---|---:|---:|
""" + "\n".join(rows) + """

## 解讀限制

這三列僅代表公開深度代理與相應市場快照的數學差距。由於海外代理的嚴格歷史回測目前為 **N/A（0 個合格事件）**，不得把其正 EV 或 Kelly 數字當作具有校準保證的下注比例。系統要先保存不少於 15 場、具不可變賽前決策、來源雜湊、開跑時間與官方賽果配對的海外事件，才可報告探索性預測準確度。

## 資料來源

- HKJC York S1-1 至 S1-7 Win／Place 公開市場頁。
- Racing Post 與 At The Races 公開賽卡特徵，已存於對應海外資料工件的來源狀態欄位。
"""
    output.write_text(text, encoding='utf-8')
    print(json.dumps({'top_events': top, 'output': str(output)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
