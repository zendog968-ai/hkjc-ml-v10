#!/usr/bin/env python3
"""Fetch HKJC-published Double Trio legs into an auditable meeting artifact.

The parser accepts only explicit First/Second Leg (or 1st/2nd Double Trio) text
from an official public page. If the page is unavailable or its structure changes,
it writes a valid ``pending`` artifact rather than guessing fixed race numbers.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from double_trio_strategy import OFFICIAL_EVENT_SCHEMA
from fetch_hkjc_live_odds import atomic_json_write, enforce_min_interval, fetch_rendered_public_page

DEFAULT_OFFICIAL_URL = "https://bet.hkjc.com/en/racing/dt/"
DEFAULT_MIN_INTERVAL_SECONDS = 300


def normalize_date(value: str) -> str:
    return datetime.strptime(value.replace("/", "-"), "%Y-%m-%d").date().isoformat()


def normalize_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))


def _event(label: str, first_race: str, second_race: str, index: int) -> dict[str, Any] | None:
    first, second = int(first_race), int(second_race)
    if first <= 0 or second <= 0 or first == second:
        return None
    return {
        "pool_event_code": f"HKJC-DT-{index}-R{first:02d}-R{second:02d}",
        "display_label": label.strip() or f"第{index}口孖T",
        "legs": [{"leg_no": 1, "race_no": first}, {"leg_no": 2, "race_no": second}],
    }


def official_meeting_matches(html: str, race_date: str, racecourse: str) -> bool:
    """Require the official page to explicitly identify the requested local meeting."""
    text = normalize_text(html).casefold()
    parsed = datetime.strptime(race_date, "%Y-%m-%d")
    date_markers = {race_date.casefold(), parsed.strftime("%d/%m/%Y").casefold()}
    course_markers = {
        "ST": ("sha tin", "沙田"),
        "HV": ("happy valley", "跑馬地"),
    }
    return any(marker in text for marker in date_markers) and any(marker.casefold() in text for marker in course_markers[racecourse])


def parse_double_trio_events(html: str) -> list[dict[str, Any]]:
    """Extract all explicit double-trio race pairs from a rendered official page."""
    text = normalize_text(html)
    matches: list[dict[str, Any]] = []
    # Official compact announcements, e.g. "1st Double Trio (Race 4 to Race 5)".
    compact_patterns = (
        re.compile(r"(?P<label>(?:\d+(?:st|nd|rd|th)\s+)?Double\s+Trio)\s*\(?\s*(?:Race\s*)?(?P<first>\d+)\s*(?:to|-|至)\s*(?:Race\s*)?(?P<second>\d+)\s*\)?", re.I),
        re.compile(r"(?P<label>第\s*[一二三四五六\d]+\s*口?\s*孖T)\s*\(?\s*第?\s*(?P<first>\d+)\s*場?\s*(?:至|到|-|及)\s*第?\s*(?P<second>\d+)\s*場?\s*\)?", re.I),
    )
    for pattern in compact_patterns:
        for match in pattern.finditer(text):
            item = _event(match.group("label"), match.group("first"), match.group("second"), len(matches) + 1)
            if item is not None:
                matches.append(item)
    # Active official pool page, e.g. "First Leg - Race 4 ... Second Leg - Race 5".
    active_patterns = (
        re.compile(r"(?P<label>(?:\d+(?:st|nd|rd|th)\s+)?Double\s+Trio)?\s*First\s+Leg\s*(?:-|:)?\s*Race\s*(?P<first>\d+).*?Second\s+Leg\s*(?:-|:)?\s*Race\s*(?P<second>\d+)", re.I),
        re.compile(r"(?P<label>第\s*[一二三四五六\d]+\s*口?\s*孖T)?\s*第\s*一\s*關\s*(?:-|：|:)?\s*第?\s*(?P<first>\d+)\s*場.*?第\s*二\s*關\s*(?:-|：|:)?\s*第?\s*(?P<second>\d+)\s*場", re.I),
    )
    for pattern in active_patterns:
        for match in pattern.finditer(text):
            item = _event(match.group("label") or "官方孖T", match.group("first"), match.group("second"), len(matches) + 1)
            if item is not None:
                matches.append(item)
    # Canonicalise and retain the published event order without duplicate leg pairs.
    unique: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for item in matches:
        pair = (item["legs"][0]["race_no"], item["legs"][1]["race_no"])
        if pair not in seen:
            seen.add(pair)
            item["pool_event_code"] = f"HKJC-DT-{len(unique) + 1}-R{pair[0]:02d}-R{pair[1]:02d}"
            unique.append(item)
    return unique


def build_payload(
    race_date: str,
    racecourse: str,
    source_url: str,
    source_mode: str,
    html: str | None,
    error: str | None = None,
) -> dict[str, Any]:
    meeting_matches = bool(html) and official_meeting_matches(html or "", race_date, racecourse)
    events = parse_double_trio_events(html) if meeting_matches and html else []
    return {
        "schema_version": OFFICIAL_EVENT_SCHEMA,
        "status": "official_confirmed" if events else "pending",
        "meeting": {"race_date": race_date, "racecourse": racecourse},
        "fetched_at_utc": datetime.now(UTC).isoformat(),
        "source": {"url": source_url, "mode": source_mode},
        "events": events,
        "message": None if events else (error or ("HKJC 公開頁的賽日或馬場與要求不一致；系統不會使用該頁的孖T場次。" if html and not meeting_matches else "HKJC 公開孖T頁尚未列出可驗證的首關及次關；系統不會以固定場次代替。")),
        "notice": "本工件僅保存官方公開頁明示的孖T首關／次關。它不包含投注提交或任何自動下注行為。",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取 HKJC 官方孖T首關及次關，解析失敗時安全輸出 pending 工件。")
    parser.add_argument("--date", required=True, help="賽日 YYYY-MM-DD 或 YYYY/MM/DD")
    parser.add_argument("--racecourse", required=True, choices=["ST", "HV", "st", "hv"])
    parser.add_argument("--output", required=True)
    parser.add_argument("--url", default=DEFAULT_OFFICIAL_URL, help="HKJC 官方孖T公開頁 URL")
    parser.add_argument("--html", help="離線官方 HTML 測試檔；指定時不發出網路請求")
    parser.add_argument("--timeout", type=int, default=35)
    parser.add_argument("--min-interval", type=int, default=DEFAULT_MIN_INTERVAL_SECONDS)
    parser.add_argument("--state-file", default="runtime/pre_race/double_trio_rate_limit_state.json")
    args = parser.parse_args()
    race_date = normalize_date(args.date)
    course = args.racecourse.upper()
    source_mode = "offline_html" if args.html else "public_rendered_page"
    content: str | None = None
    error: str | None = None
    try:
        if args.html:
            content = Path(args.html).read_text(encoding="utf-8")
        else:
            state = Path(args.state_file)
            enforce_min_interval(state, max(DEFAULT_MIN_INTERVAL_SECONDS, args.min_interval))
            atomic_json_write(state, {"last_request_epoch": time.time(), "url": args.url})
            content = fetch_rendered_public_page(args.url, args.timeout)
    except (OSError, RuntimeError, ValueError) as exc:
        error = f"官方孖T資料暫不可用：{type(exc).__name__}"
    payload = build_payload(race_date, course, args.url, source_mode, content, error)
    atomic_json_write(Path(args.output), payload)
    print(json.dumps({"status": payload["status"], "event_count": len(payload["events"]), "output": args.output}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
