#!/usr/bin/env python3
"""Static contract test for the V10 same-origin web dashboard assets."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INDEX = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
STYLE = (ROOT / "frontend" / "style.css").read_text(encoding="utf-8")
APP = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
NGINX = (ROOT / "deploy" / "nginx" / "hkjc-api").read_text(encoding="utf-8")


def require(text: str, expected: str, source: str) -> None:
    assert expected in text, f"{source} 缺少必要契約：{expected}"


def main() -> int:
    for expected in ("bootstrap@5.3.3", "marked@12.0.2", "dompurify@3.1.5", 'id="raceDate"', 'id="loadRacesButton"', 'id="raceList"', 'id="predictionTableBody"', 'id="doubleTrioSection"', 'id="doubleTrioContent"', 'id="overseasDeepSection"', 'id="overseasDeepContent"', 'id="reportContent"'):
        require(INDEX, expected, "frontend/index.html")
    for expected in ("/api/races/", "/api/prediction/", "/api/double-trio/", "/api/double-trio/backtest", "/api/report/", "renderDoubleTrio", "renderDoubleTrioBacktest", "renderOverseasDeep", "loadOverseasDeep", "/api/overseas-deep/", "disabled_non_hk", "racing_post_rating", "top_speed_rating", "cohorts", "oddsMovementClass", "T-15", "T-5", "DOMPurify.sanitize", "positive-ev", "kelly_quarter_fraction_capped"):
        require(APP, expected, "frontend/app.js")
    for expected in (".positive-ev", ".markdown-body", ".race-button", ".double-trio-event", ".double-trio-capital", ".double-trio-backtest", ".backtest-na", ".backtest-exploratory", ".odds-movement-shortening", ".odds-movement-drift", ".odds-summary-alert", ".overseas-deep-badge", ".overseas-n6-disabled", ".overseas-deep-table", ".overseas-deep-awaiting"):
        require(STYLE, expected, "frontend/style.css")
    for expected in ("root /home/ubuntu/hkjc_v10_database/frontend;", "location ^~ /api/", "proxy_pass http://127.0.0.1:8000;", "location / {", "try_files $uri $uri/ /index.html;", "auth_basic"):
        require(NGINX, expected, "deploy/nginx/hkjc-api")
    assert "http://127.0.0.1:8000" not in APP, "前端不可直接跨來源呼叫 Uvicorn"
    print(json.dumps({
        "status": "PASS",
        "ui_contract": "date selector + race list + prediction table + isolated S1 overseas deep-data research view + official Double Trio strategy with odds movement and cohort-isolated historical backtest view + report view",
        "api_contract": "same-origin /api endpoints only",
        "security_contract": "DOMPurify markdown sanitization + Nginx Basic Auth boundary",
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
