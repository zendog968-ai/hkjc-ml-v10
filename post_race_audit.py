#!/usr/bin/env python3
"""V10.2 post-race audit for official archived local and overseas results.

The audit is intentionally graceful: no pre-race prediction means archived_only,
not a fabricated accuracy report.  Optional Telegram delivery uses only existing
environment credentials and never writes them to SQLite, Markdown, or Git.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_audit_schema(conn: sqlite3.Connection) -> None:
    """Apply additive audit migrations to databases created before V10.2.1."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(post_race_audits)")}
    migrations = {
        "brier_score": "REAL",
        "brier_status": "TEXT",
        "brier_field_size": "INTEGER",
        "brier_uniform_baseline": "REAL",
        "brier_probability_sum": "REAL",
    }
    for name, definition in migrations.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE post_race_audits ADD COLUMN {name} {definition}")
    conn.commit()


def parse_race_key(value: str) -> tuple[str, str, int]:
    parts = value.split(":")
    if len(parts) != 3:
        raise ValueError("race-key 格式必須為 YYYY-MM-DD:ST|HV|S1:場次")
    return parts[0], parts[1].upper(), int(parts[2])


def load_prediction(path: str | None) -> list[dict[str, Any]]:
    if not path:
        return []
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = payload.get("predictions") or payload.get("results") or []
    if not isinstance(rows, list):
        raise ValueError("prediction JSON 沒有 predictions/results 陣列。")
    return rows


def load_latest_overseas_prediction(conn: sqlite3.Connection, race_id: int) -> list[dict[str, Any]]:
    """Load the latest complete stored overseas prediction batch for a race.

    This makes automatic results archiving independent of a chat session while
    preserving the generated-at audit timestamp.  It never creates a prediction
    from post-race fields when no pre-race batch exists.
    """
    batch = conn.execute("""SELECT MAX(p.generated_at_utc)
                              FROM overseas_prerace_predictions AS p
                              JOIN overseas_races AS r ON r.overseas_race_id=p.overseas_race_id
                              WHERE p.overseas_race_id=?
                                AND r.scheduled_start_utc IS NOT NULL
                                AND datetime(p.generated_at_utc) < datetime(r.scheduled_start_utc)""", (race_id,)).fetchone()[0]
    if not batch:
        return []
    rows = conn.execute("""SELECT horse_no,predicted_win_probability,predicted_place_probability,win_odds_at_capture AS win_odds,
                                place_odds_at_capture AS place_odds,odds_drop_flag,cold_start_tier,prior_source
                         FROM overseas_prerace_predictions WHERE overseas_race_id=? AND generated_at_utc=? ORDER BY predicted_win_probability DESC""", (race_id, batch)).fetchall()
    return [dict(row) for row in rows]


def official_overseas(conn: sqlite3.Connection, date_value: str, code: str, race_no: int) -> tuple[int, list[dict], list[dict]]:
    row = conn.execute("""SELECT r.overseas_race_id FROM overseas_races r JOIN overseas_meetings m ON m.meeting_id=r.meeting_id
                          WHERE m.meeting_date=? AND m.simulcast_code=? AND r.race_no=?""", (date_value, code, race_no)).fetchone()
    if not row:
        raise ValueError("海外賽事尚未在官方 archive 中。請先執行 auto_archive_results.py 或回刷器。")
    race_id = int(row[0])
    starters = [dict(item) for item in conn.execute("SELECT horse_no,horse_name,finish_pos,finish_pos_text,margin_text,finish_time,final_win_odds,final_place_odds FROM overseas_starters WHERE overseas_race_id=? ORDER BY COALESCE(finish_pos,999),horse_no", (race_id,))]
    dividends = [dict(item) for item in conn.execute("SELECT pool_name,winning_combination,dividend_hkd FROM overseas_dividends WHERE overseas_race_id=? ORDER BY pool_name", (race_id,))]
    return race_id, starters, dividends


def official_local(conn: sqlite3.Connection, date_value: str, course: str, race_no: int) -> tuple[str, list[dict], list[dict]]:
    starters = [dict(item) for item in conn.execute("SELECT horse_no,horse_name,finish_pos,finish_pos_text,margin_text,finish_time,win_odds AS final_win_odds,NULL AS final_place_odds FROM starters WHERE race_date=? AND racecourse=? AND race_no=? ORDER BY COALESCE(finish_pos,999),horse_no", (date_value, course, race_no))]
    if not starters:
        raise ValueError("本地賽事尚未在官方 archive 中。")
    # Current local schema stores final Win odds on starters. Formal pool dividends are not yet normalized here.
    dividends = [dict(item) for item in conn.execute("SELECT 'WIN' AS pool_name,CAST(horse_no AS TEXT) AS winning_combination,win_odds AS dividend_hkd FROM starters WHERE race_date=? AND racecourse=? AND race_no=? AND finish_pos=1", (date_value, course, race_no))]
    return f"{date_value}:{course}:{race_no}", starters, dividends


def num(row: dict, *names: str) -> float | None:
    for name in names:
        value = row.get(name)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def settle_win_strategy(selections: list[dict], winner: dict[str, Any]) -> dict[str, Any]:
    """Settle a one-unit-per-selection Win research basket from official final odds.

    This deliberately does not settle Place selections: overseas place terms and
    dividends are jurisdiction-specific and may not be represented by an odds
    field alone.  The returned status remains explicit whenever settlement is
    unavailable rather than substituting pre-race prices.
    """
    selected_numbers = [row.get("horse_no") for row in selections if isinstance(row.get("horse_no"), int)]
    if not selected_numbers:
        return {"status": "no_qualifying_selection", "selected_horse_nos": [], "stake": 0.0, "gross_return": 0.0, "net_return": 0.0, "roi": None}
    winner_no = winner.get("horse_no")
    winner_odds = num(winner, "final_win_odds")
    if winner_no in selected_numbers and (winner_odds is None or winner_odds <= 1.0):
        return {"status": "unsettled_missing_official_final_win_odds", "selected_horse_nos": selected_numbers, "stake": float(len(selected_numbers)), "gross_return": None, "net_return": None, "roi": None}
    stake = float(len(selected_numbers))
    gross_return = float(winner_odds) if winner_no in selected_numbers and winner_odds is not None else 0.0
    net_return = gross_return - stake
    return {"status": "settled_official_final_win_odds", "selected_horse_nos": selected_numbers, "stake": stake, "gross_return": gross_return, "net_return": net_return, "roi": net_return / stake}


BRIER_SUM_TOLERANCE = 1e-6


def normalize_horse_no(value: Any) -> int | None:
    """Return a positive integer horse number without silently coercing fractions."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value.is_integer() and value > 0 else None
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            number = int(text)
            return number if number > 0 else None
    return None


def brier_result(status: str, *, field_size: int | None = None, probability_sum: float | None = None, score: float | None = None) -> dict[str, Any]:
    baseline = (1.0 - (1.0 / field_size)) if field_size and field_size > 0 else None
    return {
        "score": score,
        "status": status,
        "field_size": field_size,
        "uniform_baseline": baseline,
        "probability_sum": probability_sum,
    }


def validated_field_brier(predictions: list[dict], starters: list[dict]) -> dict[str, Any]:
    """Score only a complete, normalized pre-race probability vector.

    A non-score status is deliberate: the source rows remain auditable, but no
    number is emitted when official runners and the pre-race field cannot be
    matched one-to-one or when the vector is not a valid within-race probability.
    """
    official = [row for row in starters if normalize_horse_no(row.get("horse_no")) is not None]
    if not official:
        return brier_result("not_scored_no_official_field")
    official_numbers = [normalize_horse_no(row.get("horse_no")) for row in official]
    if len(set(official_numbers)) != len(official_numbers):
        return brier_result("not_scored_duplicate_official_horse_no", field_size=len(official_numbers))
    winner_rows = [row for row in official if row.get("finish_pos") == 1]
    if len(winner_rows) != 1:
        return brier_result("not_scored_missing_or_ambiguous_official_winner", field_size=len(official_numbers))
    winner_no = normalize_horse_no(winner_rows[0].get("horse_no"))

    if not predictions:
        return brier_result("not_scored_no_prerace_prediction", field_size=len(official_numbers))
    normalized: list[tuple[int, float]] = []
    for row in predictions:
        horse_no = normalize_horse_no(row.get("horse_no"))
        if horse_no is None:
            return brier_result("not_scored_invalid_prediction_horse_no", field_size=len(official_numbers))
        raw_probability = num(row, "predicted_win_probability", "win_probability")
        if raw_probability is None:
            return brier_result("not_scored_missing_probability", field_size=len(official_numbers))
        probability = float(raw_probability)
        if not math.isfinite(probability):
            return brier_result("not_scored_nonfinite_probability", field_size=len(official_numbers))
        if probability < 0.0 or probability > 1.0:
            return brier_result("not_scored_probability_out_of_range", field_size=len(official_numbers))
        normalized.append((horse_no, probability))

    prediction_numbers = [horse_no for horse_no, _ in normalized]
    if len(set(prediction_numbers)) != len(prediction_numbers):
        return brier_result("not_scored_duplicate_prediction_horse_no", field_size=len(official_numbers))
    if winner_no not in prediction_numbers:
        return brier_result("not_scored_winner_missing_from_prerace_field", field_size=len(official_numbers))
    if set(prediction_numbers) != set(official_numbers):
        return brier_result("not_scored_prerace_field_mismatch", field_size=len(official_numbers))

    probability_sum = float(sum(probability for _, probability in normalized))
    if not math.isfinite(probability_sum) or abs(probability_sum - 1.0) > BRIER_SUM_TOLERANCE:
        return brier_result("not_scored_probability_sum_not_one", field_size=len(official_numbers), probability_sum=probability_sum)
    score = float(sum((probability - (1.0 if horse_no == winner_no else 0.0)) ** 2 for horse_no, probability in normalized))
    return brier_result("scored", field_size=len(official_numbers), probability_sum=probability_sum, score=score)


def audit_predictions(predictions: list[dict], starters: list[dict]) -> dict[str, Any]:
    brier = validated_field_brier(predictions, starters)
    winner = next((row for row in starters if row.get("finish_pos") == 1), None)
    brier_fields = {
        "brier_score": brier["score"],
        "brier_status": brier["status"],
        "brier_field_size": brier["field_size"],
        "brier_uniform_baseline": brier["uniform_baseline"],
        "brier_probability_sum": brier["probability_sum"],
    }
    if not winner or not predictions:
        return {"had_prediction": bool(predictions), "winner": winner, "top1_hit": None, "top3_contains_winner": None, "stable_strategy_hit": None, "value_strategy_hit": None, "odds_drop_count": 0, "odds_drop_winner_count": 0, "settled_stake": None, "settled_net_return": None, "roi": None, "roi_status": "not_auditable_without_both_prediction_and_official_winner", "strategy_settlement": {}, **brier_fields}
    ordered = sorted(predictions, key=lambda row: num(row, "predicted_win_probability", "win_probability") or 0.0, reverse=True)
    winner_no = normalize_horse_no(winner.get("horse_no"))
    top1_hit = int(normalize_horse_no(ordered[0].get("horse_no")) == winner_no) if ordered and winner_no is not None else None
    top3_contains_winner = int(any(normalize_horse_no(row.get("horse_no")) == winner_no for row in ordered[:3])) if winner_no is not None else None
    stable = [row for row in ordered if (num(row, "predicted_win_probability", "win_probability") or 0) >= 0.10 or (num(row, "predicted_place_probability", "place_probability") or 0) >= 0.85]
    value = [row for row in ordered if (num(row, "win_odds", "market_odds") or 0) >= 10.0 and (num(row, "place_odds", "place_market_odds") or 0) >= 3.5 and (num(row, "predicted_win_probability", "win_probability") or 0) >= 0.08 and (num(row, "predicted_place_probability", "place_probability") or 0) >= 0.80]
    settlements = {"熱門穩攻_獨贏研究籃子": settle_win_strategy(stable, winner), "冷門突襲_獨贏研究籃子": settle_win_strategy(value, winner)}
    # Aggregate only baskets with verified final Win settlement.  Place ROI remains
    # N/A until jurisdiction-specific official place dividends can be normalised.
    settled = [item for item in settlements.values() if item["status"] == "settled_official_final_win_odds"]
    stake = sum(item["stake"] for item in settled) if settled else None
    net_return = sum(item["net_return"] for item in settled) if settled else None
    roi = net_return / stake if stake else None
    return {"had_prediction": True, "winner": winner, "top1_hit": top1_hit, "top3_contains_winner": top3_contains_winner, "stable_strategy_hit": int(any(normalize_horse_no(row.get("horse_no")) == winner_no for row in stable)) if stable and winner_no is not None else 0, "value_strategy_hit": int(any(normalize_horse_no(row.get("horse_no")) == winner_no for row in value)) if value and winner_no is not None else 0, "odds_drop_count": sum(bool(row.get("odds_drop_flag") or row.get("pre_gate_money_drop")) for row in ordered), "odds_drop_winner_count": int(any((row.get("odds_drop_flag") or row.get("pre_gate_money_drop")) and normalize_horse_no(row.get("horse_no")) == winner_no for row in ordered)) if winner_no is not None else 0, "settled_stake": stake, "settled_net_return": net_return, "roi": roi, "roi_status": "win_only_research_baskets_settled_from_official_final_win_odds; place_roi_na_pending_normalized_official_place_dividends", "strategy_settlement": settlements, **brier_fields}


def render_report(scope: str, key: str, audit: dict, starters: list[dict], dividends: list[dict]) -> str:
    lines = ["# 📊 賽日覆盤與勝率檢討報告", "", f"- 範圍：`{scope}`", f"- 賽事：`{key}`", "", "## 官方前四名", "", "| 名次 | 馬號 | 馬名 | 馬位差 | 完成時間 |", "|---:|---:|---|---|---|"]
    for row in starters[:4]:
        lines.append(f"| {row.get('finish_pos_text') or '—'} | {row.get('horse_no') or '—'} | {row.get('horse_name') or '—'} | {row.get('margin_text') or '—'} | {row.get('finish_time') or '—'} |")
    brier = audit.get("brier_score")
    brier_text = "N/A" if brier is None else f"{float(brier):.4f}"
    brier_status = audit.get("brier_status") or "not_scored_unknown"
    brier_field_size = audit.get("brier_field_size")
    brier_baseline = audit.get("brier_uniform_baseline")
    brier_sum = audit.get("brier_probability_sum")
    brier_context = f"status={brier_status}; field={brier_field_size if brier_field_size is not None else '—'}; baseline={'—' if brier_baseline is None else f'{float(brier_baseline):.4f}'}; p_sum={'—' if brier_sum is None else f'{float(brier_sum):.6f}'}"
    roi = audit.get("roi")
    roi_text = "N/A" if roi is None else f"{float(roi):+.2%}"
    lines += ["", "## 模型檢討", "", f"- Top 1 命中：`{audit['top1_hit']}`。", f"- Top 3 包含頭馬：`{audit['top3_contains_winner']}`。", f"- 熱門穩攻命中：`{audit['stable_strategy_hit']}`；冷門突襲命中：`{audit['value_strategy_hit']}`。", f"- 場內多馬勝率 Brier Score：`{brier_text}`（`{brier_context}`；越低代表機率與實際頭馬結果較接近；僅限通過 field／機率校驗的研究性審計）。", f"- 🔥 閘前資金落飛：共 {audit['odds_drop_count']} 匹，頭馬屬此標記：`{audit['odds_drop_winner_count']}`。", f"- 獨贏研究籃子合併 ROI：`{roi_text}`（{audit.get('roi_status','缺少可稽核結算資料')}）。", "", "## 策略結算", ""]
    settlements = audit.get("strategy_settlement") or {}
    if settlements:
        lines += ["| 策略 | 狀態 | 馬號 | 注數 | 淨回報 | ROI |", "|---|---|---|---:|---:|---:|"]
        for name, item in settlements.items():
            selected = ", ".join(str(number) for number in item.get("selected_horse_nos", [])) or "—"
            net = item.get("net_return")
            item_roi = item.get("roi")
            lines.append(f"| {name} | {item.get('status')} | {selected} | {item.get('stake', '—')} | {'—' if net is None else f'{float(net):+.2f}'} | {'—' if item_roi is None else f'{float(item_roi):+.2%}'} |")
    else:
        lines.append("沒有可結算的賽前策略籃子。")
    lines += ["", "## 官方派彩", ""]
    if dividends:
        lines += ["| 彩池 | 勝出組合 | 派彩（HK$） |", "|---|---|---:|"]
        lines += [f"| {row['pool_name']} | {row['winning_combination']} | {row['dividend_hkd'] if row['dividend_hkd'] is not None else '—'} |" for row in dividends]
    else:
        lines.append("官方派彩未在已歸檔來源中可用；已保留缺口而未補值。")
    return "\n".join(lines) + "\n"


def telegram_send(message: str) -> tuple[bool, str]:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False, "telegram_not_configured"
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": message[:4000]}).encode("utf-8")
    try:
        with urllib.request.urlopen(urllib.request.Request(endpoint, data=payload, method="POST"), timeout=20) as response:
            response.read()
        return True, "sent"
    except Exception as exc:
        return False, f"telegram_error:{type(exc).__name__}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="V10.2 賽後覆盤與官方賽果稽核。")
    parser.add_argument("--scope", choices=("local", "overseas"), required=True)
    parser.add_argument("--race-key", required=True)
    parser.add_argument("--db", default="hkjc_last_season.sqlite")
    parser.add_argument("--schema", default="schema_overseas_racing.sql", help="建立海外審計表所用 schema。")
    parser.add_argument("--prediction-json", help="可選：在賽前已生成並保存的預測 JSON；缺省時只歸檔。")
    parser.add_argument("--report-dir", default="archive/post_race_audits")
    parser.add_argument("--telegram", action="store_true", help="只在有賽前預測且主機已有 Telegram 環境變數時嘗試推送。")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    date_value, code, race_no = parse_race_key(args.race_key)
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    # The audit ledger is shared by local and overseas scopes.  Initializing the
    # additive schema here makes archived-only local audits safe on existing V10.2
    # databases without changing historical races/starter records.
    conn.executescript(Path(args.schema).read_text(encoding="utf-8"))
    ensure_audit_schema(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    if args.scope == "overseas":
        overseas_race_id, starters, dividends = official_overseas(conn, date_value, code, race_no)
    else:
        overseas_race_id, starters, dividends = official_local(conn, date_value, code, race_no)
    predictions = load_prediction(args.prediction_json)
    if not predictions and args.scope == "overseas":
        predictions = load_latest_overseas_prediction(conn, int(overseas_race_id))
    audit = audit_predictions(predictions, starters)
    report_path = None
    telegram_status = "not_requested"
    if audit["had_prediction"]:
        report_dir = Path(args.report_dir)
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"{args.scope}_{date_value}_{code}_{race_no}.md"
        report = render_report(args.scope, args.race_key, audit, starters, dividends)
        report_path.write_text(report, encoding="utf-8")
        if args.telegram:
            sent, telegram_status = telegram_send(f"📊 V10.2 賽日覆盤\n{args.race_key}\nTop1: {audit['top1_hit']} | Top3含頭馬: {audit['top3_contains_winner']}\n報告：{report_path.name}")
    conn.execute("""INSERT INTO post_race_audits(audit_scope,race_key,audited_at_utc,had_prerace_prediction,top1_hit,top3_contains_winner,stable_strategy_hit,value_strategy_hit,odds_drop_count,odds_drop_winner_count,settled_stake,settled_net_return,roi,brier_score,brier_status,brier_field_size,brier_uniform_baseline,brier_probability_sum,report_path,status,detail_json)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(audit_scope,race_key) DO UPDATE SET audited_at_utc=excluded.audited_at_utc,had_prerace_prediction=excluded.had_prerace_prediction,top1_hit=excluded.top1_hit,top3_contains_winner=excluded.top3_contains_winner,stable_strategy_hit=excluded.stable_strategy_hit,value_strategy_hit=excluded.value_strategy_hit,odds_drop_count=excluded.odds_drop_count,odds_drop_winner_count=excluded.odds_drop_winner_count,settled_stake=excluded.settled_stake,settled_net_return=excluded.settled_net_return,roi=excluded.roi,brier_score=excluded.brier_score,brier_status=excluded.brier_status,brier_field_size=excluded.brier_field_size,brier_uniform_baseline=excluded.brier_uniform_baseline,brier_probability_sum=excluded.brier_probability_sum,report_path=excluded.report_path,status=excluded.status,detail_json=excluded.detail_json""",
                 (args.scope, args.race_key, utc_now(), int(audit["had_prediction"]), audit["top1_hit"], audit["top3_contains_winner"], audit["stable_strategy_hit"], audit["value_strategy_hit"], audit["odds_drop_count"], audit["odds_drop_winner_count"], audit["settled_stake"], audit["settled_net_return"], audit["roi"], audit["brier_score"], audit["brier_status"], audit["brier_field_size"], audit["brier_uniform_baseline"], audit["brier_probability_sum"], str(report_path) if report_path else None, "audited" if audit["had_prediction"] else "archived_only", json.dumps({"telegram": telegram_status, "audit": audit}, ensure_ascii=False, default=str)))
    conn.commit()
    print(json.dumps({"scope": args.scope, "race_key": args.race_key, "status": "audited" if audit["had_prediction"] else "archived_only", "report_path": str(report_path) if report_path else None, "telegram": telegram_status}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
