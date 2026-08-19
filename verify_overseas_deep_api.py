#!/usr/bin/env python3
"""Read-only contract test for the S1/S2 overseas deep-data API route."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent


def main() -> int:
    with tempfile.TemporaryDirectory() as temp_dir:
        runtime = Path(temp_dir) / "overseas_deep"
        runtime.mkdir()
        artifact = {
            "scrape_run": {"status": "complete", "source_notes": "fixture"},
            "race": {"meeting_date": "2026-08-19", "simulcast_code": "S1", "race_no": 1, "venue": "York"},
            "n6_integration": {"status": "disabled_non_hk"},
            "starters": [{"runner_no": 1, "horse_name": "Fixture Runner", "racing_post_rating": 110, "top_speed_rating": 95}],
        }
        (runtime / "fixture.json").write_text(json.dumps(artifact), encoding="utf-8")
        before = {path.name: path.read_bytes() for path in runtime.iterdir()}
        os.environ["HKJC_OVERSEAS_DEEP_RUNTIME_ROOT"] = str(runtime)
        sys.modules.pop("web_api", None)
        import web_api  # pylint: disable=import-outside-toplevel
        client = TestClient(web_api.app)
        ok = client.get("/api/overseas-deep/2026-08-19/S1/1")
        assert ok.status_code == 200 and ok.json()["n6_integration"]["status"] == "disabled_non_hk"
        assert client.get("/api/overseas-deep/2026-08-19/bad/1").status_code == 422
        assert client.get("/api/overseas-deep/2026-08-19/S1/21").status_code == 422
        assert client.get("/api/overseas-deep/2026-08-20/S1/1").status_code == 404
        after = {path.name: path.read_bytes() for path in runtime.iterdir()}
        assert before == after, "唯讀 API 不可改寫海外 runtime 工件"
    os.environ.pop("HKJC_OVERSEAS_DEEP_RUNTIME_ROOT", None)
    print("PASS: overseas deep API identity validation, N6 isolation and read-only contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
