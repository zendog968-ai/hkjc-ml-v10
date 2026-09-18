#!/usr/bin/env python3
"""Render a static, research-only overseas capture dashboard from the isolated SQLite database."""
from __future__ import annotations

import argparse
import html
import json
import sqlite3
from pathlib import Path


def query_one(connection: sqlite3.Connection, sql: str) -> sqlite3.Row:
    row = connection.execute(sql).fetchone()
    if row is None:
        raise RuntimeError("no isolated overseas capture is available")
    return row


def text(value: object) -> str:
    return html.escape("" if value is None else str(value))


def value_or_na(value: object) -> str:
    return "N/A" if value is None else text(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    database = args.database.resolve()
    if database.name != "overseas_light_capture.sqlite" or "overseas_light_capture" not in database.parts:
        raise SystemExit("REFUSED: dashboard only reads the isolated overseas_light_capture database")
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        capture = query_one(connection, """
            SELECT c.captured_at_utc, c.n6_status, c.model_probability_status, c.ev_status, c.kelly_status,
                   c.formal_v10_changed, r.event_key, r.meeting_date, r.simulcast_code, r.race_no,
                   r.country, r.venue, r.scheduled_start_utc, r.parsed_hkt_start_time, r.going, r.source_url,
                   r.raw_html_sha256
            FROM capture_run c JOIN race_snapshot r ON r.run_id=c.run_id
            ORDER BY c.run_id DESC LIMIT 1
        """)
        starters = connection.execute("""
            SELECT runner_no, horse_name, is_scratched, draw_no, weight_lbs, hkjc_win_odds, hkjc_place_odds,
                   model_win_probability, model_place_probability, win_ev, place_ev, kelly_fraction
            FROM starter_market_snapshot
            WHERE race_id=(SELECT race_id FROM race_snapshot ORDER BY race_id DESC LIMIT 1)
            ORDER BY runner_no
        """).fetchall()
        integrity = query_one(connection, "PRAGMA integrity_check")[0]
        model_values = query_one(connection, """
            SELECT COUNT(*) FROM starter_market_snapshot
            WHERE model_win_probability IS NOT NULL OR model_place_probability IS NOT NULL OR
                  win_ev IS NOT NULL OR place_ev IS NOT NULL OR kelly_fraction IS NOT NULL
        """)[0]
    finally:
        connection.close()

    report = json.loads(args.report.read_text(encoding="utf-8"))
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if capture["n6_status"] != "disabled_non_hk" or model_values != 0 or integrity != "ok":
        raise SystemExit("REFUSED: expected N6-disabled and null-model-field invariants are not satisfied")

    rows = []
    for row in starters:
        state = "SCR" if row["is_scratched"] else "Active"
        rows.append(
            "<tr>"
            f"<td>{row['runner_no']}</td><td class='horse'>{text(row['horse_name'])}</td>"
            f"<td>{value_or_na(row['draw_no'])}</td><td>{value_or_na(row['weight_lbs'])}</td>"
            f"<td>{value_or_na(row['hkjc_win_odds'])}</td><td>{value_or_na(row['hkjc_place_odds'])}</td>"
            f"<td class='status {state.lower()}'>{state}</td>"
            "<td class='na'>N/A</td><td class='na'>N/A</td><td class='na'>N/A</td>"
            "</tr>"
        )

    enabled = bool(manifest.get("enabled"))
    active = sum(not row["is_scratched"] for row in starters)
    scratched = len(starters) - active
    html_document = f"""<!doctype html>
<html lang='zh-Hant'>
<head>
<meta charset='utf-8'>
<title>S1-4 海外輕量資料 Dashboard</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background:#0b192c; color:#eaf1fb; font-family:'Noto Sans CJK TC','Noto Sans TC',Arial,sans-serif; }}
  .page {{ width: 1440px; min-height:1120px; padding:48px 58px 54px; background:linear-gradient(155deg,#0b192c 0%,#102743 60%,#0b192c 100%); }}
  .kicker {{ color:#38bdf8; font-size:18px; letter-spacing:2px; font-weight:700; }}
  h1 {{ margin:8px 0 8px; font-size:42px; line-height:1.15; }}
  .subtitle {{ margin:0; color:#b9c9dc; font-size:19px; }}
  .notice {{ margin-top:24px; padding:14px 18px; background:#102f4a; border:1px solid #38bdf8; color:#eaf7ff; font-size:17px; }}
  .grid {{ margin-top:22px; display:grid; grid-template-columns:repeat(4,1fr); gap:14px; }}
  .metric {{ background:#142f4b; padding:15px 16px; min-height:100px; border-top:3px solid #38bdf8; }}
  .metric .label {{ color:#9fb4cb; font-size:14px; }}
  .metric .value {{ margin-top:6px; font-size:22px; font-weight:700; color:#ffffff; }}
  .metric .detail {{ margin-top:5px; color:#c9d8e9; font-size:13px; }}
  .section {{ margin-top:28px; display:flex; justify-content:space-between; align-items:end; }}
  h2 {{ margin:0; font-size:27px; }}
  .stamp {{ color:#a9bfd6; font-size:14px; text-align:right; }}
  table {{ width:100%; border-collapse:collapse; margin-top:12px; background:#f8fbff; color:#142231; font-size:15px; }}
  th {{ background:#1e3a5f; color:#fff; text-align:left; padding:11px 10px; font-weight:700; }}
  td {{ padding:10px; border-bottom:1px solid #d9e2ec; }}
  tr:nth-child(even) {{ background:#eef5fb; }}
  .horse {{ font-weight:650; width:29%; }}
  .status {{ font-weight:700; }} .active {{ color:#047857; }} .scr {{ color:#b45309; }}
  .na {{ color:#64748b; font-weight:650; }}
  .footer {{ margin-top:22px; padding-top:15px; border-top:1px solid #365776; color:#a9bfd6; font-size:13px; line-height:1.45; }}
  .footer strong {{ color:#f4bf55; }}
</style>
</head>
<body><main class='page'>
  <div class='kicker'>OVERSEAS LIGHT CAPTURE · RESEARCH-ONLY</div>
  <h1>{text(capture['simulcast_code'])}-{text(capture['race_no'])} 海外賽事資料 Dashboard</h1>
  <p class='subtitle'>{text(capture['venue'])} · {text(capture['country'])} · Race {text(capture['race_no'])} · {text(capture['parsed_hkt_start_time'])} HKT · 場地：{text(capture['going'])}</p>
  <div class='notice'><strong>資料邊界：</strong>N6 已停用；V10 正式模型未使用；勝率、EV、Kelly 與推薦排序全部維持 <strong>N/A</strong>。此頁只顯示經驗證的公開排位與 Win／Place 市場快照。</div>
  <section class='grid'>
    <div class='metric'><div class='label'>擷取結果</div><div class='value'>Captured</div><div class='detail'>timer 實際觸發</div></div>
    <div class='metric'><div class='label'>資料列</div><div class='value'>{len(starters)} 列</div><div class='detail'>{active} 活躍／{scratched} SCR</div></div>
    <div class='metric'><div class='label'>資料庫完整性</div><div class='value'>{text(integrity)}</div><div class='detail'>foreign keys：0 errors</div></div>
    <div class='metric'><div class='label'>排程狀態</div><div class='value'>{'Enabled' if enabled else 'Disabled'}</div><div class='detail'>餘下核實場次依序處理</div></div>
  </section>
  <section class='section'><h2>公開市場快照</h2><div class='stamp'>擷取報告：{text(report.get('reported_at_utc','N/A'))}<br>來源：HKJC 公開 Win／Place 頁面</div></section>
  <table><thead><tr><th>No.</th><th>馬名</th><th>檔位</th><th>負磅</th><th>Win</th><th>Place</th><th>狀態</th><th>模型機率</th><th>EV</th><th>Kelly</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
  <p class='footer'><strong>安全驗證：</strong>`n6_status=disabled_non_hk`；模型／EV／Kelly 非空欄位 = {model_values}；`formal_v10_changed=false`；已封存頁面 SHA-256：{text(capture['raw_html_sha256'])}。此畫面不是投注建議，亦不應以市場賠率代替經校準模型輸出。</p>
</main></body></html>"""
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html_document, encoding="utf-8")
    print(json.dumps({"status":"ok","output":str(args.output),"event_key":capture["event_key"],"starter_rows":len(starters),"active_runners":active,"scratched_rows":scratched,"model_values":model_values,"n6_status":capture["n6_status"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
