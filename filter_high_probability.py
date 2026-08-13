#!/usr/bin/env python3
"""Filter V10.1 prediction output into high-probability and value-watch selections.

This utility does not send WhatsApp messages or place bets. It generates a reviewable
WhatsApp Direct Link only when at least one runner satisfies a configured research rule.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

DEFAULT_PHONE = "85296896832"


def finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def percentage(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def odds_text(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}"


def filter_predictions(prediction: dict[str, Any]) -> list[dict[str, Any]]:
    """Apply the two user-specified, inclusive filtering rules.

    A runner can appear in both groups. Super focus is an additional label for a
    high-probability runner whose model-derived place probability is at least 90%.
    """
    selected: list[dict[str, Any]] = []
    for row in prediction.get("predictions", []):
        horse = str(row.get("horse_name") or "").strip()
        if not horse:
            continue
        win_probability = finite_number(row.get("predicted_win_probability"))
        place_probability = finite_number(row.get("predicted_place_probability"))
        win_odds = finite_number(row.get("market_odds"))
        place_odds = finite_number(row.get("place_market_odds"))
        if win_probability is None or place_probability is None:
            continue
        common = {
            "horse_name": horse,
            "rank": row.get("rank"),
            "draw": row.get("draw"),
            "jockey": row.get("jockey"),
            "trainer": row.get("trainer"),
            "predicted_win_probability": win_probability,
            "predicted_place_probability": place_probability,
            "win_odds": win_odds,
            "place_odds": place_odds,
            "win_ev_per_unit": finite_number(row.get("ev_per_unit")),
            "place_ev_per_unit": finite_number(row.get("place_ev_per_unit")),
            "data_warning": row.get("data_warning"),
        }
        if win_probability >= 0.10 or place_probability >= 0.85:
            category = "熱門穩攻"
            focus_level = "超級焦點" if place_probability >= 0.90 else "焦點"
            selected.append({**common, "strategy": category, "focus_level": focus_level,
                             "rule": "獨贏勝率≥10% 或 位置勝率≥85%"})
        if (
            win_odds is not None and place_odds is not None
            and win_odds >= 10.0 and place_odds >= 3.5
            and win_probability >= 0.08 and place_probability >= 0.80
        ):
            selected.append({**common, "strategy": "冷門突襲", "focus_level": "冷門值博",
                             "rule": "獨贏賠率≥10、位置賠率≥3.5、獨贏勝率≥8%、位置勝率≥80%"})
    return selected


def race_label(prediction: dict[str, Any]) -> str:
    race = prediction.get("race") or {}
    date = str(race.get("race_date") or "賽日未列")
    course = str(race.get("racecourse") or "")
    race_no = race.get("race_no")
    suffix = f"第{race_no}場" if race_no is not None else "場次未列"
    return f"{date} {course} {suffix}".strip()


def build_message(label: str, selections: list[dict[str, Any]]) -> str:
    lines = [f"V10.1 賽前15分鐘模型篩選｜{label}", "僅供研究參考；賠率及出賽狀態以官方最後公布為準。"]
    for item in selections:
        odds_part = f"獨贏{odds_text(item['win_odds'])}／位置{odds_text(item['place_odds'])}"
        lines.append(
            f"【{item['strategy']}・{item['focus_level']}】{item['horse_name']}"
            f"｜獨贏{percentage(item['predicted_win_probability'])}"
            f"｜位置{percentage(item['predicted_place_probability'])}｜{odds_part}"
        )
    return "\n".join(lines)


def whatsapp_link(phone: str, message: str) -> str:
    digits = "".join(char for char in phone if char.isdigit())
    if not digits:
        raise ValueError("WhatsApp 電話號碼不可為空。")
    return "https://api.whatsapp.com/send?" + urlencode({"phone": digits, "text": message})


def run(prediction_path: str, output_path: str, phone: str = DEFAULT_PHONE) -> dict[str, Any]:
    prediction = json.loads(Path(prediction_path).read_text(encoding="utf-8"))
    selections = filter_predictions(prediction)
    label = race_label(prediction)
    message = build_message(label, selections) if selections else None
    output = {
        "schema_version": "v10_1_pre_race_filter_v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "race_label": label,
        "selection_count": len(selections),
        "selection_rules": {
            "熱門穩攻": "獨贏勝率≥10% 或 位置勝率≥85%；位置勝率≥90%標註超級焦點",
            "冷門突襲": "獨贏賠率≥10、位置賠率≥3.5、獨贏勝率≥8%、位置勝率≥80%",
        },
        "selections": selections,
        "whatsapp": {
            "phone": "".join(char for char in phone if char.isdigit()),
            "message": message,
            "direct_link": whatsapp_link(phone, message) if message else None,
            "delivery": "preview_only_not_sent",
        },
        "notice": "這是模型篩選與預覽連結，不會自動發送訊息或執行任何投注。",
    }
    Path(output_path).write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="以 V10.1 預測結果篩選熱門穩攻與冷門突襲，產生 WhatsApp 預覽連結")
    parser.add_argument("--prediction", required=True, help="predict.py 產生的 prediction.json")
    parser.add_argument("--output", default="high_probability_filter.json")
    parser.add_argument("--whatsapp-phone", default=DEFAULT_PHONE)
    args = parser.parse_args()
    print(json.dumps(run(args.prediction, args.output, args.whatsapp_phone), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
