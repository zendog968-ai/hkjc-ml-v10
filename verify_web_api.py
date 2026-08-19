#!/usr/bin/env python3
"""Contract test for the HKJC ML V10 read-only FastAPI service."""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent


def digest_tree(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    fixture_root = Path(tempfile.mkdtemp(prefix="hkjc-v10-readonly-api-"))
    try:
        job_dir = fixture_root / "2026" / "08" / "18_ST_R01"
        prediction = {
            "model": "HKJC V10.2 fixture",
            "race": {"race_date": "2026-08-18", "racecourse": "ST", "race_no": 1},
            "predictions": [{"horse_no": 1, "horse_name": "測試馬", "predicted_win_probability": 1.0}],
        }
        filtered = {"selection_count": 1, "strategies": {"熱門穩攻": [{"horse_name": "測試馬"}]}}
        write_json(job_dir / "prediction.json", prediction)
        write_json(job_dir / "high_probability_filter.json", filtered)
        (job_dir / "pre_race_report.md").write_text("# 測試賽前報告\n\nV10.2 正式機率保持不變。\n", encoding="utf-8")
        for race_no, start_no in ((3, 1), (7, 11)):
            rows = [
                {
                    "horse_no": start_no + offset,
                    "horse_name": f"孖T測試馬{race_no}-{start_no + offset}",
                    "rank": offset + 1,
                    "predicted_win_probability": 0.30 - offset * 0.03,
                    "ev_per_unit": 0.03 * (4 - offset),
                    "market_odds": 8.0 if offset == 0 else (6.0 if offset == 1 else 7.0),
                    "odds_t_minus_15": 10.0 if offset == 0 else (5.0 if offset == 1 else 7.0),
                    "odds_t_minus_5": 8.0 if offset == 0 else (6.0 if offset == 1 else 7.0),
                    "odds_drop_ratio": -0.2 if offset == 0 else (0.2 if offset == 1 else 0.0),
                }
                for offset in range(5)
            ]
            dt_dir = fixture_root / "2026" / "08" / f"18_ST_R{race_no:02d}"
            write_json(dt_dir / "prediction.json", {"model": "HKJC V10.2 fixture", "race": {"race_date": "2026-08-18", "racecourse": "ST", "race_no": race_no}, "predictions": rows})
        write_json(
            fixture_root / "2026" / "08" / "18_ST_double_trio_official.json",
            {
                "schema_version": "v10_hkjc_double_trio_official_v1",
                "status": "official_confirmed",
                "meeting": {"race_date": "2026-08-18", "racecourse": "ST"},
                "source": {"url": "https://example.invalid/hkjc-official-double-trio", "mode": "offline_fixture"},
                "events": [{"pool_event_code": "DT-FIXTURE-R3-R7", "display_label": "第1口孖T", "legs": [{"leg_no": 1, "race_no": 3}, {"leg_no": 2, "race_no": 7}]}],
            },
        )
        backtest_summary_path = fixture_root / "double_trio_backtest_summary.json"
        write_json(backtest_summary_path, {
            "schema": "v10_double_trio_four_horse_backtest_v1",
            "readiness": "ready",
            "settled_record_count": 2,
            "cohort_policy": "Results are segregated by base_model_sha256 and are never aggregated across model retraining versions.",
            "cohorts": {"a" * 64: {"status": "exploratory", "settled_event_count": 2, "hit_count": 1, "hit_rate": 0.5, "roi": 0.25}},
        })
        before = digest_tree(fixture_root)

        os.environ["HKJC_RUNTIME_ROOT"] = str(fixture_root)
        sys.modules.pop("web_api", None)
        web_api = importlib.import_module("web_api")

        def fake_n6_enrichment(source: dict, *_: object) -> dict:
            enriched = json.loads(json.dumps(source, ensure_ascii=False))
            for position, row in enumerate(enriched.get("predictions", []), start=1):
                row["n6_neural_score"] = float(row.get("predicted_win_probability", 0.0)) * 100.0
                row["n6_rank"] = position
                row["joint_neural_probability"] = float(row.get("predicted_win_probability", 0.0))
                row["joint_neural_score"] = float(row.get("predicted_win_probability", 0.0)) * 100.0
                row["joint_rank"] = position
            enriched["n6_integration"] = {"status": "available", "method": "fixture"}
            return enriched

        web_api.enrich_prediction = fake_n6_enrichment
        web_api.DOUBLE_TRIO_BACKTEST_SUMMARY_PATH = backtest_summary_path.resolve()
        with TestClient(web_api.app) as client:
            health = client.get("/health")
            assert health.status_code == 200, health.text
            assert health.json()["status"] == "ok"
            assert health.json()["read_only"] is True

            listing = client.get("/api/races/2026-08-18")
            assert listing.status_code == 200, listing.text
            assert listing.json()["count"] == 3
            listed_races = listing.json()["races"]
            assert [(item["course"], item["race_no"]) for item in listed_races] == [("ST", 1), ("ST", 3), ("ST", 7)]
            assert listed_races[0] == {
                "date": "2026-08-18", "course": "ST", "race_no": 1,
                "artifact_directory": "2026/08/18_ST_R01",
                "has_high_probability_filter": True,
                "has_markdown_report": True,
                "has_v103_uncertainty_sidecar": False,
            }

            prediction_response = client.get("/api/prediction/2026-08-18/st/1")
            assert prediction_response.status_code == 200, prediction_response.text
            body = prediction_response.json()
            assert body["prediction"].get("n6_integration", {}).get("status") in {"available", "unavailable"}
            response_prediction = dict(body["prediction"])
            response_prediction.pop("n6_integration", None)
            for row in response_prediction.get("predictions", []):
                for key in ("n6_neural_win_probability", "n6_neural_score", "n6_rank", "joint_neural_probability", "joint_neural_score", "joint_rank", "joint_recommendation", "joint_consensus"):
                    row.pop(key, None)
            assert response_prediction == prediction
            assert body["high_probability_filter"] == filtered

            double_trio = client.get("/api/double-trio/2026-08-18/ST")
            assert double_trio.status_code == 200, double_trio.text
            strategy = double_trio.json()
            assert strategy["status"] == "ready", strategy
            event = strategy["events"][0]
            assert event["status"] == "ready", event
            assert [item["horse_no"] for item in event["legs"][0]["selections"]] == [1, 2, 3, 4]
            assert [item["horse_no"] for item in event["legs"][1]["selections"]] == [11, 12, 13, 14]
            assert event["combination_plan"]["total_bet_combinations"] == 16
            assert event["combination_plan"]["total_suggested_capital_hkd"] == 160.0
            assert event["odds_monitoring_summary"]["status"] == "available"
            assert event["odds_monitoring_summary"]["large_movement_count"] == 4
            assert event["legs"][0]["odds_monitoring"]["selections"][0]["movement_status"] == "large_shortening"

            backtest = client.get("/api/double-trio/backtest")
            assert backtest.status_code == 200, backtest.text
            assert backtest.json()["readiness"] == "ready"
            assert backtest.json()["settled_record_count"] == 2
            assert list(backtest.json()["cohorts"]) == ["a" * 64]

            report = client.get("/api/report/2026-08-18/ST/1")
            assert report.status_code == 200, report.text
            assert report.headers["content-type"].startswith("text/markdown")
            assert "# 測試賽前報告" in report.text

            assert client.get("/api/races/2026/08/18").status_code == 404
            assert client.get("/api/races/not-a-date").status_code == 422
            assert client.get("/api/prediction/2026-08-18/XX/1").status_code == 422
            assert client.get("/api/prediction/2026-08-18/ST/21").status_code == 422
            assert client.get("/api/prediction/2026-08-18/HV/1").status_code == 404
            assert client.post("/health").status_code == 405

            cors = client.options(
                "/health",
                headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"},
            )
            assert cors.status_code == 200, cors.text
            assert cors.headers.get("access-control-allow-origin") == "http://localhost:3000"

        after = digest_tree(fixture_root)
        assert before == after, "唯讀 API 測試後 runtime 工件被修改"
        print(json.dumps({
            "status": "PASS",
            "endpoints": ["/health", "/api/races/{date}", "/api/prediction/{date}/{course}/{race_no}", "/api/double-trio/{date}/{course}", "/api/double-trio/backtest", "/api/report/{date}/{course}/{race_no}"],
            "cors": "PASS",
            "read_only_artifacts": "PASS",
        }, ensure_ascii=False))
        return 0
    finally:
        os.environ.pop("HKJC_RUNTIME_ROOT", None)
        shutil.rmtree(fixture_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
