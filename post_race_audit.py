#!/usr/bin/env python3
"""V10.2 post-race audit for official archived local and overseas results.

The audit is intentionally graceful: no pre-race prediction means archived_only,
not a fabricated accuracy report.  Optional Telegram delivery uses only existing
environment credentials and never writes them to SQLite, Markdown, or Git.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
    batch = conn.execute("SELECT MAX(generated_at_utc) FROM overseas_prerace_predictions WHERE overseas_race_id=?", (race_id,)).fetchone()[0]
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
    starters = [dict(item) for item in conn.execute("SELECT horse_no,horse_name,finish_pos,finish_pos_text,margin_text,finish_time FROM overseas_starters WHERE overseas_race_id=? ORDER BY COALESCE(finish_pos,999),horse_no", (race_id,))]
    dividends = [dict(item) for item in conn.execute("SELECT pool_name,winning_combination,dividend_hkd FROM overseas_dividends WHERE overseas_race_id=? ORDER BY pool_name", (race_id,))]
    return race_id, starters, dividends


def official_local(conn: sqlite3.Connection, date_value: str, course: str, race_no: int) -> tuple[str, list[dict], list[dict]]:
    starters = [dict(item) for item in conn.execute("SELECT horse_no,horse_name,finish_pos,finish_pos_text,margin_text,finish_time FROM starters WHERE race_date=? AND racecourse=? AND race_no=? ORDER BY COALESCE(finish_pos,999),horse_no", (date_value, course, race_no))]
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


def audit_predictions(predictions: list[dict], starters: list[dict]) -> dict[str, Any]:
    winner = next((row for row in starters if row.get("finish_pos") == 1), None)
    if not winner or not predictions:
        return {"had_prediction": bool(predictions), "winner": winner, "top1_hit": None, "top3_contains_winner": None, "stable_strategy_hit": None, "value_strategy_hit": None, "odds_drop_count": 0, "odds_drop_winner_count": 0, "settled_stake": None, "settled_net_return": None, "roi": None}
    ordered = sorted(predictions, key=lambda row: num(row, "predicted_win_probability", "win_probability") or 0.0, reverse=True)
    winner_no = winner.get("horse_no")
    top1_hit = int(ordered[0].get("horse_no") == winner_no) if ordered else 0
    top3_contains_winner = int(any(row.get("horse_no") == winner_no for row in ordered[:3]))
    stable = [row for row in ordered if (num(row, "predicted_win_probability", "win_probability") or 0) >= 0.10 or (num(row, "predicted_place_probability", "place_probability") or 0) >= 0.85]
    value = [row for row in ordered if (num(row, "win_odds") or 0) >= 10.0 and (num(row, "place_odds") or 0) >= 3.5 and (num(row, "predicted_win_probability", "win_probability") or 0) >= 0.08 and (num(row, "predicted_place_probability", "place_probability") or 0) >= 0.80]
    final_win_dividend = None
    for row in starters:
        if row.get("horse_no") == winner_no:
            # A prediction may retain odds captured before start but it is not suitable for settlement.
            break
    # Settled Win ROI can only be calculated where user supplied a separately captured final dividend field.
    # This avoids misusing T-15/T-5 odds as a final realised return.
    return {"had_prediction": True, "winner": winner, "top1_hit": top1_hit, "top3_contains_winner": top3_contains_winner, "stable_strategy_hit": int(any(row.get("horse_no") == winner_no for row in stable)) if stable else 0, "value_strategy_hit": int(any(row.get("horse_no") == winner_no for row in value)) if value else 0, "odds_drop_count": sum(bool(row.get("odds_drop_flag") or row.get("pre_gate_money_drop")) for row in ordered), "odds_drop_winner_count": int(any((row.get("odds_drop_flag") or row.get("pre_gate_money_drop")) and row.get("horse_no") == winner_no for row in ordered)), "settled_stake": None, "settled_net_return": None, "roi": None, "roi_status": "not_settled_without_explicit_final_dividend_mapping"}


def render_report(scope: str, key: str, audit: dict, starters: list[dict], dividends: list[dict]) -> str:
    lines = ["# 📊 賽日覆盤與勝率檢討報告", "", f"- 範圍：`{scope}`", f"- 賽事：`{key}`", "", "## 官方前四名", "", "| 名次 | 馬號 | 馬名 | 馬位差 | 完成時間 |", "|---:|---:|---|---|---|"]
    for row in starters[:4]:
        lines.append(f"| {row.get('finish_pos_text') or '—'} | {row.get('horse_no') or '—'} | {row.get('horse_name') or '—'} | {row.get('margin_text') or '—'} | {row.get('finish_time') or '—'} |")
    lines += ["", "## 模型檢討", "", f"- Top 1 命中：`{audit['top1_hit']}`。", f"- Top 3 包含頭馬：`{audit['top3_contains_winner']}`。", f"- 熱門穩攻命中：`{audit['stable_strategy_hit']}`；冷門突襲命中：`{audit['value_strategy_hit']}`。", f"- 🔥 閘前資金落飛：共 {audit['odds_drop_count']} 匹，頭馬屬此標記：`{audit['odds_drop_winner_count']}`。", f"- 投注 ROI：`N/A`（{audit.get('roi_status','缺少可稽核結算資料')}）。", "", "## 官方派彩", ""]
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
    conn.execute("""INSERT INTO post_race_audits(audit_scope,race_key,audited_at_utc,had_prerace_prediction,top1_hit,top3_contains_winner,stable_strategy_hit,value_strategy_hit,odds_drop_count,odds_drop_winner_count,settled_stake,settled_net_return,roi,report_path,status,detail_json)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(audit_scope,race_key) DO UPDATE SET audited_at_utc=excluded.audited_at_utc,had_prerace_prediction=excluded.had_prerace_prediction,top1_hit=excluded.top1_hit,top3_contains_winner=excluded.top3_contains_winner,stable_strategy_hit=excluded.stable_strategy_hit,value_strategy_hit=excluded.value_strategy_hit,odds_drop_count=excluded.odds_drop_count,odds_drop_winner_count=excluded.odds_drop_winner_count,report_path=excluded.report_path,status=excluded.status,detail_json=excluded.detail_json""",
                 (args.scope, args.race_key, utc_now(), int(audit["had_prediction"]), audit["top1_hit"], audit["top3_contains_winner"], audit["stable_strategy_hit"], audit["value_strategy_hit"], audit["odds_drop_count"], audit["odds_drop_winner_count"], audit["settled_stake"], audit["settled_net_return"], audit["roi"], str(report_path) if report_path else None, "audited" if audit["had_prediction"] else "archived_only", json.dumps({"telegram": telegram_status, "audit": audit}, ensure_ascii=False, default=str)))
    conn.commit()
    print(json.dumps({"scope": args.scope, "race_key": args.race_key, "status": "audited" if audit["had_prediction"] else "archived_only", "report_path": str(report_path) if report_path else None, "telegram": telegram_status}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
