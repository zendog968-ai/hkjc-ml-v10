#!/usr/bin/env python3
"""Fetch public HKJC Win and Place odds into model-ready overlay files.

The script reads only the publicly rendered HKJC odds page, never logs in and never
places bets. It makes one page-load attempt per live run and applies a local minimum
interval. A transient page / network error produces a valid degraded overlay with
null odds rather than a malformed file or an exception that blocks predict.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

PUBLIC_ODDS_URL = "https://bet.hkjc.com/ch/racing/wp"
DEFAULT_MIN_INTERVAL_SECONDS = 60
SCRATCH_MARKERS = {"SCR", "退出", "已退出", "撤回", "WV", "WV-A", "WX-A", "WXNR"}


def clean_text(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def parse_odd(value: str) -> float | None:
    """Return a usable payout multiple; blank, SCR, 0 and invalid values become None."""
    text = clean_text(value).replace(",", "")
    if not text or text.upper() in {"-", "--", "N/A", "NULL", "NONE", "SCR"} or text in {"未有", "暫無", "退出", "撤回"}:
        return None
    match = re.fullmatch(r"(\d+(?:\.\d+)?)", text)
    if not match:
        return None
    odds = float(match.group(1))
    # HKJC displayed payout multiples must exceed 1.0; 0 normally means unavailable.
    return odds if odds > 1.0 else None


def is_scratched_row(cells: list[str]) -> bool:
    merged = "|".join(cells).upper()
    return any(marker.upper() in merged for marker in SCRATCH_MARKERS)


def parse_visible_odds_table(html: str) -> tuple[dict[str, dict[str, float | None]], dict[str, Any]]:
    """Extract horse-name / {win, place}, retaining unavailable values as None."""
    soup = BeautifulSoup(html, "html.parser")
    best: tuple[dict[str, dict[str, float | None]], dict[str, Any]] | None = None
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        header_index = -1
        horse_index = win_index = place_index = -1
        for index, row in enumerate(rows):
            cells = [clean_text(cell.get_text(" ", strip=True)) for cell in row.find_all(["th", "td"])]
            lowered = [cell.lower() for cell in cells]
            horse_label = "馬名" if "馬名" in cells else ("horsename" if "horsename" in lowered else None)
            win_label = "獨贏" if "獨贏" in cells else ("win" if "win" in lowered else None)
            place_label = "位置" if "位置" in cells else ("place" if "place" in lowered else None)
            if horse_label is not None and win_label is not None and place_label is not None:
                header_index = index
                horse_index = cells.index(horse_label) if horse_label == "馬名" else lowered.index(horse_label)
                win_index = cells.index(win_label) if win_label == "獨贏" else lowered.index(win_label)
                place_index = cells.index(place_label) if place_label == "位置" else lowered.index(place_label)
                break
        if header_index < 0:
            continue
        odds: dict[str, dict[str, float | None]] = {}
        scratched: list[str] = []
        missing_win: list[str] = []
        missing_place: list[str] = []
        for row in rows[header_index + 1 :]:
            cells = [clean_text(cell.get_text(" ", strip=True)) for cell in row.find_all("td")]
            if len(cells) <= max(horse_index, win_index, place_index):
                continue
            horse = cells[horse_index]
            if not horse or horse.casefold() in {"全餐", "f", "field"}:
                continue
            if is_scratched_row(cells):
                scratched.append(horse)
                continue
            win = parse_odd(cells[win_index])
            place = parse_odd(cells[place_index])
            odds[horse] = {"win": win, "place": place}
            if win is None:
                missing_win.append(horse)
            if place is None:
                missing_place.append(horse)
        if odds:
            metadata = {
                "table_rows": len(rows),
                "header": [clean_text(cell.get_text(" ", strip=True)) for cell in rows[header_index].find_all(["th", "td"])],
                "rows_parsed": len(odds),
                "scratched_horses": scratched,
                "missing_win_odds": missing_win,
                "missing_place_odds": missing_place,
            }
            if best is None or len(odds) > len(best[0]):
                best = (odds, metadata)
    if best is None:
        raise ValueError("未能在公開頁面找到可辨識的馬名、獨贏及位置賠率表。")
    return best


def load_expected_horses(race_card_path: str | None) -> set[str] | None:
    if not race_card_path:
        return None
    payload = json.loads(Path(race_card_path).read_text(encoding="utf-8"))
    horses = {clean_text(str(row.get("horse_name", ""))) for row in payload.get("runners", []) if row.get("horse_name")}
    if not horses:
        raise ValueError("race_card 未包含 runners[].horse_name，無法進行馬名對應。")
    return horses


def enforce_min_interval(state_path: Path, minimum_seconds: int) -> None:
    if not state_path.exists():
        return
    try:
        previous = json.loads(state_path.read_text(encoding="utf-8"))
        elapsed = time.time() - float(previous.get("last_request_epoch", 0))
    except (json.JSONDecodeError, ValueError, OSError):
        return
    if elapsed < minimum_seconds:
        raise RuntimeError(f"為避免高頻請求，請至少等待 {int(minimum_seconds - elapsed) + 1} 秒後再抓取。")


def fetch_rendered_public_page(url: str, timeout_seconds: int) -> str:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("缺少 playwright；請先依 requirements.txt 安裝，並確認系統可使用 Chromium。") from exc
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path="/usr/bin/chromium", headless=True, args=["--no-sandbox"])
            try:
                page = browser.new_page(locale="zh-HK", user_agent="Mozilla/5.0 (V10.1 research helper; public odds reader)")
                response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)
                if response is not None and response.status in {403, 429}:
                    raise RuntimeError(f"HKJC HTTP {response.status}; immediate stop required")
                # A table is sufficient; odds values may legitimately be temporarily blank.
                page.wait_for_function(
                    """() => Array.from(document.querySelectorAll('table')).some(
                        table => table.innerText.includes('馬名') && table.innerText.includes('獨贏') && table.innerText.includes('位置')
                    )""",
                    timeout=timeout_seconds * 1000,
                )
                return page.content()
            finally:
                browser.close()
    except PlaywrightTimeoutError as exc:
        raise RuntimeError("公開賠率頁在指定時間內沒有載入可辨識表格。") from exc
    except Exception as exc:  # Network / browser errors are intentionally converted to a safe degraded result.
        raise RuntimeError(f"公開賠率頁讀取失敗：{type(exc).__name__}") from exc


def atomic_json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="讀取 HKJC 公開獨贏及位置賠率並生成容錯覆蓋 JSON")
    parser.add_argument("--output", default="odds_overlay.json", help="獨贏覆蓋 JSON；與 predict.py 相容")
    parser.add_argument("--place-output", default="place_odds_overlay.json", help="位置覆蓋 JSON")
    parser.add_argument("--combined-output", default="odds_overlay_combined.json", help="包含 win / place 的合併 JSON")
    parser.add_argument("--metadata-output", default="odds_overlay.meta.json", help="抓取中繼資料 JSON")
    parser.add_argument("--race-card", help="可選：race_card.json；會保留名單中所有馬匹，無可用賠率填 null")
    parser.add_argument("--url", default=PUBLIC_ODDS_URL, help="公開賠率頁 URL")
    parser.add_argument("--timeout", type=int, default=30, help="頁面載入逾時秒數")
    parser.add_argument("--min-interval", type=int, default=DEFAULT_MIN_INTERVAL_SECONDS, help="兩次公開頁請求最短間隔秒數")
    parser.add_argument("--state-file", default=".hkjc_live_odds_state.json", help="限速狀態檔路徑")
    parser.add_argument("--html", help="離線測試 HTML；指定時不發送網絡請求")
    parser.add_argument("--raw-html-output", help="可選：封存原始公開賠率頁HTML並記錄SHA-256")
    parser.add_argument("--snapshot-output", help="V10.2 可選：保存帶時間與場次標籤的雙市場賠率快照 JSON")
    parser.add_argument("--snapshot-label", default="", help="V10.2 快照標籤，例如 T_MINUS_15 或 T_MINUS_5")
    parser.add_argument("--race-date", help="V10.2 快照賽日 YYYY/MM/DD；只供審計")
    parser.add_argument("--racecourse", choices=["ST", "HV", "st", "hv"], help="V10.2 快照馬場；只供審計")
    parser.add_argument("--race-no", type=int, help="V10.2 快照場次；只供審計")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    expected_horses = load_expected_horses(args.race_card)
    requested_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    source_mode = "offline_html" if args.html else "public_rendered_page"
    warnings: list[str] = []
    table_info: dict[str, Any] = {"table_rows": 0, "rows_parsed": 0, "scratched_horses": [], "missing_win_odds": [], "missing_place_odds": []}
    parsed: dict[str, dict[str, float | None]] = {}
    raw_html_archive: dict[str, str] | None = None
    try:
        if args.html:
            html = Path(args.html).read_text(encoding="utf-8")
        else:
            state_path = Path(args.state_file)
            enforce_min_interval(state_path, max(args.min_interval, DEFAULT_MIN_INTERVAL_SECONDS))
            # Record the attempted request too, preventing rapid retries after timeout/error.
            atomic_json_write(state_path, {"last_request_epoch": time.time(), "url": args.url})
            html = fetch_rendered_public_page(args.url, args.timeout)
        if args.raw_html_output:
            raw_target = Path(args.raw_html_output)
            raw_target.parent.mkdir(parents=True, exist_ok=True)
            raw_bytes = html.encode("utf-8")
            raw_target.write_bytes(raw_bytes)
            raw_html_archive = {"path": str(raw_target), "sha256": hashlib.sha256(raw_bytes).hexdigest()}
        parsed, table_info = parse_visible_odds_table(html)
    except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as exc:
        warnings.append(f"賠率來源暫不可用：{exc}；已輸出 null 覆蓋檔，predict.py 可繼續執行。")
    if expected_horses is not None:
        selected = {horse: parsed.get(horse, {"win": None, "place": None}) for horse in sorted(expected_horses)}
        unmatched = sorted(expected_horses - set(parsed))
    else:
        selected = parsed
        unmatched = []
    win_overlay = {horse: values.get("win") for horse, values in selected.items()}
    place_overlay = {horse: values.get("place") for horse, values in selected.items()}
    combined_overlay = {"win": win_overlay, "place": place_overlay}
    # All output files are valid JSON even during a degraded source state.
    atomic_json_write(Path(args.output), win_overlay)
    atomic_json_write(Path(args.place_output), place_overlay)
    atomic_json_write(Path(args.combined_output), combined_overlay)
    available_pairs = sum(values.get("win") is not None and values.get("place") is not None for values in selected.values())
    metadata = {
        "status": "complete" if not warnings and not unmatched and available_pairs == len(selected) else "degraded",
        "source_mode": source_mode,
        "source_url": args.url if not args.html else str(Path(args.html).resolve()),
        "raw_html_archive": raw_html_archive,
        "fetched_at_utc": requested_at,
        "odds_types": ["WIN", "PLA"],
        "odds_definition": {
            "win": "HKJC 公開頁顯示的獨贏派彩倍數；0、SCR、空值或無法解析時為 null。",
            "place": "HKJC 公開頁顯示的位置派彩倍數；0、SCR、空值或無法解析時為 null。",
        },
        "runners_written": len(selected),
        "complete_win_place_pairs": available_pairs,
        "race_card_filter_applied": expected_horses is not None,
        "unmatched_race_card_horses": unmatched,
        "table_info": table_info,
        "warnings": warnings,
        "output_files": {"win": str(Path(args.output)), "place": str(Path(args.place_output)), "combined": str(Path(args.combined_output))},
        "warning": "此程式採降級輸出：賠率空值、SCR 或頁面逾時會寫入 null，而非中斷後續預測。請查看 metadata.status 與 warnings。",
    }
    atomic_json_write(Path(args.metadata_output), metadata)
    if args.snapshot_output:
        snapshot = {
            "schema_version": "v10.2_odds_snapshot",
            "snapshot_label": str(args.snapshot_label or ""),
            "captured_at_utc": requested_at,
            "race": {
                "race_date": args.race_date,
                "racecourse": args.racecourse.upper() if args.racecourse else None,
                "race_no": args.race_no,
            },
            "status": metadata["status"],
            "odds": selected,
            "metadata_file": str(Path(args.metadata_output)),
            "source_url": metadata["source_url"],
            "source_mode": source_mode,
            "raw_html_archive": raw_html_archive,
        }
        atomic_json_write(Path(args.snapshot_output), snapshot)
        metadata["snapshot_output"] = str(Path(args.snapshot_output))
        atomic_json_write(Path(args.metadata_output), metadata)
    print(json.dumps(metadata, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        raise SystemExit(2)
