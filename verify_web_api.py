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
        before = digest_tree(fixture_root)

        os.environ["HKJC_RUNTIME_ROOT"] = str(fixture_root)
        sys.modules.pop("web_api", None)
        web_api = importlib.import_module("web_api")
        with TestClient(web_api.app) as client:
            health = client.get("/health")
            assert health.status_code == 200, health.text
            assert health.json()["status"] == "ok"
            assert health.json()["read_only"] is True

            listing = client.get("/api/races/2026-08-18")
            assert listing.status_code == 200, listing.text
            assert listing.json()["count"] == 1
            assert listing.json()["races"] == [{
                "date": "2026-08-18", "course": "ST", "race_no": 1,
                "artifact_directory": "2026/08/18_ST_R01",
                "has_high_probability_filter": True,
                "has_markdown_report": True,
                "has_v103_uncertainty_sidecar": False,
            }]

            prediction_response = client.get("/api/prediction/2026-08-18/st/1")
            assert prediction_response.status_code == 200, prediction_response.text
            body = prediction_response.json()
            assert body["prediction"] == prediction
            assert body["high_probability_filter"] == filtered

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
            "endpoints": ["/health", "/api/races/{date}", "/api/prediction/{date}/{course}/{race_no}", "/api/report/{date}/{course}/{race_no}"],
            "cors": "PASS",
            "read_only_artifacts": "PASS",
        }, ensure_ascii=False))
        return 0
    finally:
        os.environ.pop("HKJC_RUNTIME_ROOT", None)
        shutil.rmtree(fixture_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
