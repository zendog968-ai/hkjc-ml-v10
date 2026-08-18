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

from race_risk_guidance import build_race_guidance

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


def load_v103_bayesian_disclosure(path: str | None) -> dict[str, Any] | None:
    """Load a parallel V10.3 sidecar without allowing it to influence V10.2 logic."""
    if not path:
        return None
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "bayesian_status": "unavailable_invalid_sidecar",
            "reason": f"無法讀取 V10.3 sidecar：{type(exc).__name__}",
            "formal_probability_replacement": False,
            "rows": [],
        }
    if payload.get("formal_probability_replacement") is not False:
        return {
            "bayesian_status": "unavailable_invalid_sidecar",
            "reason": "V10.3 sidecar 未明確聲明不替換正式機率；已拒絕披露。",
            "formal_probability_replacement": False,
            "rows": [],
        }
    return payload


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
        "weight_lbs": finite_number(row.get("weight_lbs")),
        "win_odds": finite_number(row.get("market_odds")) if finite_number(row.get("market_odds")) is not None else finite_number(row.get("win_odds")),
        "place_odds": finite_number(row.get("place_market_odds")) if finite_number(row.get("place_market_odds")) is not None else finite_number(row.get("place_odds")),
        "win_ev_per_unit": finite_number(row.get("ev_per_unit")) if finite_number(row.get("ev_per_unit")) is not None else finite_number(row.get("win_ev")),
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
    date = str(race.get("race_date") or race.get("meeting_date") or "賽日未列")
    course = str(race.get("racecourse") or race.get("simulcast_code") or "")
    race_no = race.get("race_no")
    suffix = f"第{race_no}場" if race_no is not None else "場次未列"
    return f"{date} {course} {suffix}".strip()


def build_message(label: str, strategies: dict[str, list[dict[str, Any]]], guidance: dict[str, Any], bayesian: dict[str, Any] | None = None) -> str:
    lines = [f"V10.2 賽前模型篩選｜{label}", "僅供研究參考；賠率及出賽狀態以官方最後公布為準。"]
    if guidance.get("dispersion_warning"):
        lines.append("⚠️【高爆冷風險亂局】嚴禁以單一熱門作單膽。")
    uncertainty = guidance.get("uncertainty") or {}
    if uncertainty.get("low_separation_warning"):
        lines.append(str(uncertainty.get("label") or "⚠️【低分離度】場內排序缺乏足夠分離，不適合作單膽。"))
    if bayesian is not None:
        status = str(bayesian.get("bayesian_status") or "unavailable")
        if status == "available_research_only":
            lines.append(
                "V10.3 貝氏不確定性披露（不改動 V10.2）："
                f"首選穩定度{percentage(finite_number(bayesian.get('top1_rank_stability')))}／"
                f"後驗熵{finite_number(bayesian.get('posterior_entropy_mean')) or 0.0:.4f}。"
            )
        else:
            lines.append(f"V10.3 貝氏覆蓋層：{status}（V10.2 正式機率維持不變）。")
    lines.append(f"結構提示：{guidance['bet_recommendation']}")
    for category, selections in strategies.items():
        for item in selections:
            odds_part = f"獨贏{odds_text(item['win_odds'])}／位置{odds_text(item['place_odds'])}"
            lines.append(
                f"【{category}・{item['focus_level']}】{item['horse_name']}"
                f"｜獨贏{percentage(item['predicted_win_probability'])}"
                f"｜位置{percentage(item['predicted_place_probability'])}｜{odds_part}"
                + ("｜🔥閘前資金落飛" if item.get("gate_money_drop_flag") else "")
            )
    for item in guidance.get("value_bomb_candidates", []):
        reasons = "、".join(item.get("reasons") or [])
        lines.append(f"【{item['label']}】{item.get('horse_name') or '馬匹未列'}｜獨贏{odds_text(item.get('win_odds'))}｜{reasons}")
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
    guidance = output["race_guidance"]
    lines = [
        f"# V10.2 賽前掃描報告｜{output['race_label']}",
        "",
        f"> 產生時間（UTC）：{output['generated_at_utc']}。此報告僅供模型研究；不構成投注保證或自動投注指令。",
        "",
        "## 場內分佈與研究性結構提示",
        "",
        f"- 出賽馬數：`{guidance['field_size']}`；首選勝率：`{percentage(guidance['top1_win_probability'])}`。",
        f"- 風險標籤：**{guidance['dispersion_label'] or '一般分佈風險'}**。",
        f"- 結構提示：{guidance['bet_recommendation']}",
    ]
    uncertainty = guidance.get("uncertainty") or {}
    if uncertainty.get("status") == "available":
        lines.extend([
            f"- 首二機率：`{percentage(uncertainty.get('top1_probability'))}`／`{percentage(uncertainty.get('top2_probability'))}`；首二差距：`{percentage(uncertainty.get('top2_gap'))}`。",
            f"- 正規化熵：`{uncertainty.get('normalized_entropy', 0.0):.4f}`；集成分歧（首選）：`{percentage(uncertainty.get('ensemble_disagreement_top1'))}`。",
            f"- 不確定性標籤：**{uncertainty.get('label') or '未觸發低分離度提示'}**。",
        ])
    else:
        lines.append(f"- 不確定性診斷：`{uncertainty.get('status', 'unavailable')}`／`{uncertainty.get('reason', 'unknown')}`；不改動模型機率。")
    bayesian = output.get("v103_bayesian_disclosure")
    if bayesian is not None:
        lines.extend(["", "## V10.3 貝氏校準／不確定性披露（研究性）", ""])
        status = str(bayesian.get("bayesian_status") or "unavailable")
        if status == "available_research_only":
            lines.extend([
                "> 此區塊只披露後驗不確定性；V10.2 `predicted_win_probability`、排序、EV 與 Kelly 完全不變。",
                "",
                f"- V10.2 首選：`{bayesian.get('top1_v102_horse_name') or '—'}`；首選排名穩定度：`{percentage(finite_number(bayesian.get('top1_rank_stability')))}`。",
                f"- 後驗正規化熵：`{finite_number(bayesian.get('posterior_entropy_mean')) or 0.0:.4f}`（P05 `{finite_number(bayesian.get('posterior_entropy_p05')) or 0.0:.4f}`；P95 `{finite_number(bayesian.get('posterior_entropy_p95')) or 0.0:.4f}`）。",
                f"- 每次 posterior draw 場內機率守恆最大誤差：`{finite_number(bayesian.get('probability_sum_max_abs_error')) or 0.0:.2e}`。",
                "",
                "| 馬匹 | V10.2 勝率（保留） | 後驗均值 | P05 | P95 | 成分分歧敏感度 |",
                "|---|---:|---:|---:|---:|---:|",
            ])
            for item in bayesian.get("rows") or []:
                lines.append(
                    f"| {item.get('horse_name') or '—'} | {percentage(finite_number(item.get('v102_predicted_win_probability')))} "
                    f"| {percentage(finite_number(item.get('posterior_win_mean')))} | {percentage(finite_number(item.get('posterior_win_p05')))} "
                    f"| {percentage(finite_number(item.get('posterior_win_p95')))} | {finite_number(item.get('posterior_component_disagreement')) or 0.0:.4f} |"
                )
        else:
            lines.append(f"V10.3 overlay 目前不可用：`{status}`；原因：{bayesian.get('reason') or '未提供'}。V10.2 正式輸出維持不變。")
    lines.extend([
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
        "## 💣 高賠率冷門提示",
        "",
    ])
    bombs = guidance.get("value_bomb_candidates", [])
    if bombs:
        lines += ["| 馬匹 | 標籤 | 獨贏賠率 | 模型 EV | 輔助優勢 |", "|---|---|---:|---:|---|"]
        for item in bombs:
            lines.append(f"| {item.get('horse_name') or '—'} | {item['label']} | {odds_text(item.get('win_odds'))} | {signed_ev(item.get('win_ev'))} | {'、'.join(item.get('reasons') or [])} |")
    else:
        lines.append("沒有同時符合高賠率及可驗證輕磅／內檔條件的馬匹。")
    lines += [
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


def run(prediction_path: str, output_path: str, phone: str = DEFAULT_PHONE, markdown_output: str | None = None, bayesian_overlay_path: str | None = None) -> dict[str, Any]:
    prediction = json.loads(Path(prediction_path).read_text(encoding="utf-8"))
    strategies = classify_predictions(prediction)
    guidance = build_race_guidance(prediction.get("predictions", []))
    bayesian = load_v103_bayesian_disclosure(bayesian_overlay_path)
    selections = [item for items in strategies.values() for item in items]
    label = race_label(prediction)
    message = build_message(label, strategies, guidance, bayesian) if selections or guidance.get("value_bomb_candidates") else None
    output = {
        "schema_version": "v10_2_pre_race_filter_v2",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "race_label": label,
        "selection_count": len(selections),
        "selection_counts": {category: len(items) for category, items in strategies.items()},
        "selection_rules": {
            "熱門穩攻": f"{HOT_RULE}；位置勝率≥90%標註超級焦點",
            "冷門突襲 / Value Bomb": VALUE_BOMB_RULE,
        },
        "strategies": strategies,
        "race_guidance": guidance,
        "v103_bayesian_disclosure": bayesian,
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
    parser.add_argument("--bayesian-overlay", help="可選 V10.3 uncertainty sidecar；只作並列風險披露。")
    args = parser.parse_args()
    print(json.dumps(run(args.prediction, args.output, args.whatsapp_phone, args.markdown_output, args.bayesian_overlay), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
