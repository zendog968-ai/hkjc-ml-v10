#!/usr/bin/env python3
"""Filter V10.1 prediction output and generate research reports.

The script creates structured JSON plus an optional Markdown report. It never sends
WhatsApp messages or places bets: the WhatsApp Direct Link is a review-only URL.
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
HOT_RULE = "獨贏勝率≥10% 或 位置勝率≥85%"
VALUE_BOMB_RULE = "獨贏賠率≥10、位置賠率≥3.5、獨贏勝率≥8%、位置勝率≥80%"


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


def signed_ev(value: float | None) -> str:
    return "—" if value is None else f"{value:+.3f}"


def candidate_row(row: dict[str, Any]) -> dict[str, Any] | None:
    horse = str(row.get("horse_name") or "").strip()
    win_probability = finite_number(row.get("predicted_win_probability"))
    place_probability = finite_number(row.get("predicted_place_probability"))
    if not horse or win_probability is None or place_probability is None:
        return None
    return {
        "horse_name": horse,
        "rank": row.get("rank"),
        "draw": row.get("draw"),
        "jockey": row.get("jockey"),
        "trainer": row.get("trainer"),
        "predicted_win_probability": win_probability,
        "predicted_place_probability": place_probability,
        "win_odds": finite_number(row.get("market_odds")),
        "place_odds": finite_number(row.get("place_market_odds")),
        "win_ev_per_unit": finite_number(row.get("ev_per_unit")),
        "place_ev_per_unit": finite_number(row.get("place_ev_per_unit")),
        "odds_drop_ratio": finite_number(row.get("odds_drop_ratio")),
        "gate_money_drop_flag": bool(row.get("gate_money_drop_flag")),
        "market_movement_label": row.get("market_movement_label"),
        "data_warning": row.get("data_warning"),
    }


def classify_predictions(prediction: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Return separated Hot Attack and Value Bomb lists using inclusive thresholds."""
    hot: list[dict[str, Any]] = []
    value_bomb: list[dict[str, Any]] = []
    for raw in prediction.get("predictions", []):
        item = candidate_row(raw)
        if item is None:
            continue
        win_probability = item["predicted_win_probability"]
        place_probability = item["predicted_place_probability"]
        win_odds = item["win_odds"]
        place_odds = item["place_odds"]
        if win_probability >= 0.10 or place_probability >= 0.85:
            hot.append({
                **item,
                "strategy": "熱門穩攻",
                "focus_level": "超級焦點" if place_probability >= 0.90 else "焦點",
                "rule": HOT_RULE,
            })
        if (
            win_odds is not None and place_odds is not None
            and win_odds >= 10.0 and place_odds >= 3.5
            and win_probability >= 0.08 and place_probability >= 0.80
        ):
            value_bomb.append({
                **item,
                "strategy": "冷門突襲 / Value Bomb",
                "focus_level": "冷門值博",
                "rule": VALUE_BOMB_RULE,
            })
    hot.sort(key=lambda item: (item["focus_level"] != "超級焦點", -item["predicted_place_probability"], -item["predicted_win_probability"]))
    value_bomb.sort(key=lambda item: (-item["predicted_place_probability"], -item["predicted_win_probability"], -float(item["win_odds"] or 0)))
    return {"熱門穩攻": hot, "冷門突襲 / Value Bomb": value_bomb}


def race_label(prediction: dict[str, Any]) -> str:
    race = prediction.get("race") or {}
    date = str(race.get("race_date") or "賽日未列")
    course = str(race.get("racecourse") or "")
    race_no = race.get("race_no")
    suffix = f"第{race_no}場" if race_no is not None else "場次未列"
    return f"{date} {course} {suffix}".strip()


def build_message(label: str, strategies: dict[str, list[dict[str, Any]]]) -> str:
    lines = [f"V10.1 賽前模型篩選｜{label}", "僅供研究參考；賠率及出賽狀態以官方最後公布為準。"]
    for category, selections in strategies.items():
        for item in selections:
            odds_part = f"獨贏{odds_text(item['win_odds'])}／位置{odds_text(item['place_odds'])}"
            lines.append(
                f"【{category}・{item['focus_level']}】{item['horse_name']}"
                f"｜獨贏{percentage(item['predicted_win_probability'])}"
                f"｜位置{percentage(item['predicted_place_probability'])}｜{odds_part}"
                + ("｜🔥閘前資金落飛" if item.get("gate_money_drop_flag") else "")
            )
    return "\n".join(lines)


def whatsapp_link(phone: str, message: str) -> str:
    digits = "".join(char for char in phone if char.isdigit())
    if not digits:
        raise ValueError("WhatsApp 電話號碼不可為空。")
    return "https://api.whatsapp.com/send?" + urlencode({"phone": digits, "text": message})


def markdown_table(selections: list[dict[str, Any]]) -> list[str]:
    if not selections:
        return ["目前沒有符合此策略門檻的馬匹。"]
    lines = [
        "| 馬匹 | 焦點 | 檔位 | 騎師 | 獨贏機率 | 位置機率 | 獨贏賠率 | 位置賠率 | 獨贏 EV | 位置 EV | 資金動向 |",
        "|---|---|---:|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for item in selections:
        lines.append(
            f"| {item['horse_name']} | {item['focus_level']} | {item.get('draw', '—')} | {item.get('jockey', '—')} "
            f"| {percentage(item['predicted_win_probability'])} | {percentage(item['predicted_place_probability'])} "
            f"| {odds_text(item['win_odds'])} | {odds_text(item['place_odds'])} "
            f"| {signed_ev(item['win_ev_per_unit'])} | {signed_ev(item['place_ev_per_unit'])} | "
            f"{'🔥 閘前資金落飛' if item.get('gate_money_drop_flag') else '—'} |"
        )
    return lines


def build_markdown(output: dict[str, Any]) -> str:
    strategies = output["strategies"]
    lines = [
        f"# V10.2 賽前掃描報告｜{output['race_label']}",
        "",
        f"> 產生時間（UTC）：{output['generated_at_utc']}。此報告僅供模型研究；不構成投注保證或自動投注指令。",
        "",
        "## 熱門穩攻",
        "",
        f"**規則：** {output['selection_rules']['熱門穩攻']}",
        "",
        *markdown_table(strategies["熱門穩攻"]),
        "",
        "## 冷門突襲 / Value Bomb",
        "",
        f"**規則：** {output['selection_rules']['冷門突襲 / Value Bomb']}",
        "",
        *markdown_table(strategies["冷門突襲 / Value Bomb"]),
        "",
        "## WhatsApp 預覽",
        "",
    ]
    link = output["whatsapp"]["direct_link"]
    if link:
        lines.extend([f"[開啟 WhatsApp 訊息預覽]({link})", "", f"直接連結：`{link}`"])
    else:
        lines.append("沒有符合條件的馬匹，因此未生成 WhatsApp 預覽連結。")
    lines.extend([
        "",
        "## 資料注意事項",
        "",
        "如賠率檔狀態為 `degraded`、有撤回馬，或出現樣本不足警示，應以香港賽馬會最後公布資料覆核。🔥 閘前資金落飛僅表示公開獨贏賠率由賽前15分鐘至5分鐘下跌達20%或以上，並非大戶身份、內幕消息或賽果保證。市場賠率只作比較，位置機率是由集成獨贏強度推導的模擬代理。",
    ])
    return "\n".join(lines) + "\n"


def run(prediction_path: str, output_path: str, phone: str = DEFAULT_PHONE, markdown_output: str | None = None) -> dict[str, Any]:
    prediction = json.loads(Path(prediction_path).read_text(encoding="utf-8"))
    strategies = classify_predictions(prediction)
    selections = [item for items in strategies.values() for item in items]
    label = race_label(prediction)
    message = build_message(label, strategies) if selections else None
    output = {
        "schema_version": "v10_2_pre_race_filter_v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "race_label": label,
        "selection_count": len(selections),
        "selection_counts": {category: len(items) for category, items in strategies.items()},
        "selection_rules": {
            "熱門穩攻": f"{HOT_RULE}；位置勝率≥90%標註超級焦點",
            "冷門突襲 / Value Bomb": VALUE_BOMB_RULE,
        },
        "strategies": strategies,
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
    if markdown_output:
        Path(markdown_output).write_text(build_markdown(output), encoding="utf-8")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="以 V10.2 集成預測結果篩選熱門穩攻與冷門突襲，產生 JSON、Markdown 與 WhatsApp 預覽連結")
    parser.add_argument("--prediction", required=True, help="predict.py 產生的 prediction.json")
    parser.add_argument("--output", default="high_probability_filter.json")
    parser.add_argument("--markdown-output", help="可選：輸出的 Markdown 報告路徑")
    parser.add_argument("--whatsapp-phone", default=DEFAULT_PHONE)
    args = parser.parse_args()
    print(json.dumps(run(args.prediction, args.output, args.whatsapp_phone, args.markdown_output), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
