#!/usr/bin/env python3
"""Offline safety test for overseas_light_capture.py; makes no network request."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path

import overseas_light_capture as capture

ROOT = Path(__file__).resolve().parent
SAVED_HTML = ROOT / "runtime/overseas_s1_all_test/input/hkjc_s1_8_public_page.html"


def make_manifest(enabled: bool = True, simulcast_code: str = "S1") -> dict:
    return {
        "schema_version": "overseas_light_capture_manifest_v1",
        "enabled": enabled,
        "jurisdiction": "overseas",
        "allowed_hosts": ["bet.hkjc.com", "racing.hkjc.com"],
        "minimum_request_interval_seconds": 60,
        "events": [{
            "event_key": f"2026-08-29:{simulcast_code}:8",
            "meeting_date": "2026-08-29",
            "simulcast_code": simulcast_code,
            "race_no": 8,
            "country": "Australia",
            "venue": "Caulfield Racecourse",
            "scheduled_start_utc": "2026-08-29T07:20:00Z",
            "hkjc_win_place_url": f"https://bet.hkjc.com/en/racing/wp/2026-08-29/{simulcast_code}/8",
        }],
    }


def scalar(connection: sqlite3.Connection, sql: str) -> int | str:
    return connection.execute(sql).fetchone()[0]


def main() -> int:
    if not SAVED_HTML.is_file():
        raise SystemExit(f"Saved public fixture is missing: {SAVED_HTML}")
    os.environ["N6_STATUS"] = "disabled_non_hk"
    with tempfile.TemporaryDirectory(prefix="overseas-light-capture-test-") as temporary:
        root = Path(temporary)
        manifest_path = root / "active_manifest.json"
        manifest_path.write_text(json.dumps(make_manifest(), ensure_ascii=False), encoding="utf-8")
        manifest = capture.parse_manifest(manifest_path)
        capture.guard_runtime(root / "overseas_light_capture.sqlite", require_n6_disabled=True)
        html = SAVED_HTML.read_text(encoding="utf-8")
        page_meta, starters = capture.parse_page(html, manifest["events"][0])
        raw_path = root / "raw" / "S1_8.html"
        raw_path.parent.mkdir(parents=True)
        raw_path.write_text(html, encoding="utf-8")
        result = capture.persist_capture(
            root / "overseas_light_capture.sqlite",
            manifest["events"][0],
            page_meta,
            starters,
            raw_path,
            capture.hashlib.sha256(html.encode("utf-8")).hexdigest(),
            capture.hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        )
        database = root / "overseas_light_capture.sqlite"
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            checks = {
                "integrity_check": scalar(connection, "PRAGMA integrity_check"),
                "race_count": scalar(connection, "SELECT COUNT(*) FROM race_snapshot"),
                "listed_rows": scalar(connection, "SELECT COUNT(*) FROM starter_market_snapshot"),
                "scratched_rows": scalar(connection, "SELECT COUNT(*) FROM starter_market_snapshot WHERE is_scratched=1"),
                "model_fields_populated": scalar(connection, "SELECT COUNT(*) FROM starter_market_snapshot WHERE model_win_probability IS NOT NULL OR model_place_probability IS NOT NULL OR win_ev IS NOT NULL OR place_ev IS NOT NULL OR kelly_fraction IS NOT NULL"),
                "n6_status": scalar(connection, "SELECT n6_status FROM capture_run"),
            }
        finally:
            connection.close()
        invalid_manifest = root / "invalid_hk_manifest.json"
        invalid_manifest.write_text(json.dumps(make_manifest(simulcast_code="ST")), encoding="utf-8")
        rejected_hong_kong_input = False
        try:
            capture.parse_manifest(invalid_manifest)
        except ValueError:
            rejected_hong_kong_input = True
        passed = (
            result["integrity_check"] == "ok"
            and checks["integrity_check"] == "ok"
            and checks["race_count"] == 1
            and checks["listed_rows"] == 15
            and checks["scratched_rows"] == 3
            and checks["model_fields_populated"] == 0
            and checks["n6_status"] == "disabled_non_hk"
            and rejected_hong_kong_input
        )
        output = {"passed": passed, "network_calls": 0, "fixture": str(SAVED_HTML), "result": result, "checks": checks, "rejected_hong_kong_input": rejected_hong_kong_input}
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
