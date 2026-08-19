#!/usr/bin/env python3
"""Read-only FastAPI interface for HKJC ML V10 pre-race artifacts.

This service intentionally exposes only GET endpoints.  It reads generated files
under runtime/pre_race and never invokes prediction, scheduler, database-write,
EV, or Kelly logic.  The V10.2/V10.3 automation pipeline remains independent.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date as Date
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Path as ApiPath
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

from double_trio_strategy import build_meeting_strategies
from n6_integration import enrich_prediction

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
RUNTIME_ROOT = Path(os.getenv("HKJC_RUNTIME_ROOT", str(PROJECT_ROOT / "runtime" / "pre_race"))).expanduser().resolve()
MAX_JSON_BYTES = 10 * 1024 * 1024
MAX_REPORT_BYTES = 2 * 1024 * 1024
COURSES = {"ST", "HV"}
DOUBLE_TRIO_ARTIFACT_SUFFIX = "double_trio_official.json"
DOUBLE_TRIO_BACKTEST_SUMMARY_PATH = Path(
    os.getenv("HKJC_DOUBLE_TRIO_BACKTEST_SUMMARY", str(PROJECT_ROOT / "reports" / "double_trio_backtest" / "four_horse_summary.json"))
).expanduser().resolve()
OVERSEAS_DEEP_RUNTIME_ROOT = Path(
    os.getenv("HKJC_OVERSEAS_DEEP_RUNTIME_ROOT", str(PROJECT_ROOT / "runtime" / "overseas_deep"))
).expanduser().resolve()
OVERSEAS_DEEP_BACKTEST_SUMMARY_PATH = Path(
    os.getenv("HKJC_OVERSEAS_DEEP_BACKTEST_SUMMARY", str(PROJECT_ROOT / "reports" / "overseas_deep" / "OVERSEAS_DEEP_PROXY_BACKTEST.json"))
).expanduser().resolve()
JOB_DIRECTORY_RE = re.compile(r"^(?P<day>\d{2})_(?P<course>ST|HV)_R(?P<race_no>\d{1,2})$")
LEGACY_JOB_DIRECTORY_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<course>ST|HV)_R(?P<race_no>\d{1,2})$")


def cors_origins() -> list[str]:
    """Return explicit local origins unless the operator deliberately configures others."""
    configured = os.getenv("HKJC_API_CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")
    origins = [value.strip() for value in configured.split(",") if value.strip()]
    return origins or ["http://localhost:3000", "http://127.0.0.1:3000"]


app = FastAPI(
    title="HKJC ML V10 Read-Only API",
    version="1.0.0",
    description="唯讀查詢 V10 賽前 prediction、N6 輔助神經訊號、篩選結果與 Markdown 報告；不執行模型訓練或檔案寫入。",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins(),
    allow_credentials=False,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["Accept", "Content-Type"],
    max_age=600,
)


def parse_iso_date(value: str) -> Date:
    try:
        return Date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="date 必須為 YYYY-MM-DD 格式。") from exc


def normalize_course(value: str) -> str:
    course = value.strip().upper()
    if course not in COURSES:
        raise HTTPException(status_code=422, detail="course 必須為 ST 或 HV。")
    return course


def normalize_race_no(value: int) -> int:
    if not 1 <= value <= 20:
        raise HTTPException(status_code=422, detail="race_no 必須介乎 1 至 20。")
    return value


def normalize_simulcast_code(value: str) -> str:
    code = value.strip().upper()
    if not re.fullmatch(r"S[1-9][0-9]*", code):
        raise HTTPException(status_code=422, detail="simulcast_code 必須為 S1、S2 等官方代碼。")
    return code


def is_under_runtime(path: Path) -> bool:
    try:
        path.resolve().relative_to(RUNTIME_ROOT)
        return True
    except ValueError:
        return False


def safely_read_bytes(path: Path, limit: int) -> bytes:
    if not path.is_file() or not is_under_runtime(path):
        raise HTTPException(status_code=404, detail="找不到指定賽事工件。")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise HTTPException(status_code=404, detail="無法讀取指定賽事工件。") from exc
    if size > limit:
        raise HTTPException(status_code=413, detail="工件檔案超出 API 安全讀取上限。")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise HTTPException(status_code=404, detail="無法讀取指定賽事工件。") from exc


def read_json_project_report(path: Path) -> dict[str, Any]:
    """Read the one allow-listed backtest summary without permitting path selection."""
    if path != DOUBLE_TRIO_BACKTEST_SUMMARY_PATH or not path.is_file():
        return {
            "schema": "v10_double_trio_four_horse_backtest_v1",
            "readiness": "not_ready",
            "cohorts": {},
            "settled_record_count": 0,
            "notice": "尚未有可稽核的歷史孖T四匹複式結算資料；不以賽後重算或 fixture 代替。",
        }
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            raise ValueError("回測摘要超出安全讀取上限")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("Double Trio backtest summary unavailable: %s", type(exc).__name__)
        return {
            "schema": "v10_double_trio_four_horse_backtest_v1",
            "readiness": "not_ready",
            "cohorts": {},
            "settled_record_count": 0,
            "notice": "歷史孖T回測摘要暫不可讀取；不會顯示未驗證數值。",
        }
    return payload if isinstance(payload, dict) else {"readiness": "not_ready", "cohorts": {}, "settled_record_count": 0, "notice": "回測摘要格式無效；不會顯示未驗證數值。"}


def read_overseas_deep_backtest_summary() -> dict[str, Any]:
    """Read only the fixed overseas deep-proxy backtest summary."""
    path = OVERSEAS_DEEP_BACKTEST_SUMMARY_PATH
    fallback = {
        "status": "not_available",
        "strict_event_count": 0,
        "cohorts": [],
        "minimum_exploratory_events": 15,
        "warning": "尚未有可稽核的海外賽前深度決策與官方賽果配對；不以賽後重抓資料代替回測。",
    }
    if not path.is_file():
        return fallback
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            return fallback
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return fallback
    return payload if isinstance(payload, dict) else fallback


def read_json_artifact(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(safely_read_bytes(path, MAX_JSON_BYTES).decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=500, detail="prediction 工件不是 UTF-8 JSON。") from exc
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="prediction 工件 JSON 格式無效。") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=500, detail="prediction 工件頂層必須為 JSON object。")
    return payload


def read_overseas_deep_artifact(date_value: Date, simulcast_code: str, race_no: int) -> dict[str, Any]:
    """Read one fixed overseas deep-data artifact after payload identity validation.

    The caller cannot select a filename.  The endpoint is intentionally isolated
    from V10 local runtime, local SQLite and N6 enrichment.
    """
    if not OVERSEAS_DEEP_RUNTIME_ROOT.is_dir():
        raise HTTPException(status_code=404, detail="尚未有海外深度資料工件。")
    for path in sorted(OVERSEAS_DEEP_RUNTIME_ROOT.glob("*.json")):
        try:
            path.resolve().relative_to(OVERSEAS_DEEP_RUNTIME_ROOT)
            if not path.is_file() or path.stat().st_size > MAX_JSON_BYTES:
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        race = payload.get("race") if isinstance(payload.get("race"), dict) else {}
        n6 = payload.get("n6_integration") if isinstance(payload.get("n6_integration"), dict) else {}
        if (
            race.get("meeting_date") == date_value.isoformat()
            and race.get("simulcast_code") == simulcast_code
            and race.get("race_no") == race_no
            and n6.get("status") == "disabled_non_hk"
        ):
            return payload
    raise HTTPException(status_code=404, detail="找不到已驗證的海外深度資料工件。")


def job_identity(job_dir: Path) -> tuple[Date, str, int] | None:
    """Parse scheduler's current YYYY/MM/DD_ST_Rnn and legacy flat folder forms."""
    current_match = JOB_DIRECTORY_RE.fullmatch(job_dir.name)
    if current_match and job_dir.parent.name.isdigit() and len(job_dir.parent.name) == 2:
        year_text = job_dir.parent.parent.name
        if year_text.isdigit() and len(year_text) == 4:
            try:
                race_date = Date(int(year_text), int(job_dir.parent.name), int(current_match.group("day")))
            except ValueError:
                return None
            return race_date, current_match.group("course"), int(current_match.group("race_no"))
    legacy_match = LEGACY_JOB_DIRECTORY_RE.fullmatch(job_dir.name)
    if legacy_match:
        try:
            race_date = Date.fromisoformat(legacy_match.group("date"))
        except ValueError:
            return None
        return race_date, legacy_match.group("course"), int(legacy_match.group("race_no"))
    return None


def discover_jobs(requested_date: Date) -> list[dict[str, Any]]:
    """Discover only valid prediction directories below the fixed runtime root."""
    if not RUNTIME_ROOT.is_dir():
        return []
    jobs: list[dict[str, Any]] = []
    for prediction_path in RUNTIME_ROOT.rglob("prediction.json"):
        if not is_under_runtime(prediction_path):
            continue
        identity = job_identity(prediction_path.parent)
        if identity is None:
            continue
        race_date, course, race_no = identity
        if race_date != requested_date:
            continue
        jobs.append(
            {
                "date": race_date.isoformat(),
                "course": course,
                "race_no": race_no,
                "artifact_directory": str(prediction_path.parent.relative_to(RUNTIME_ROOT)),
                "has_high_probability_filter": (prediction_path.parent / "high_probability_filter.json").is_file(),
                "has_markdown_report": (prediction_path.parent / "pre_race_report.md").is_file(),
                "has_v103_uncertainty_sidecar": (prediction_path.parent / "v103_bayesian_uncertainty.json").is_file(),
            }
        )
    return sorted(jobs, key=lambda item: (item["course"], item["race_no"]))


def find_job(date_value: Date, course: str, race_no: int) -> Path:
    for job in discover_jobs(date_value):
        if job["course"] == course and job["race_no"] == race_no:
            path = RUNTIME_ROOT / job["artifact_directory"]
            if path.is_dir() and is_under_runtime(path):
                return path
    raise HTTPException(status_code=404, detail="指定日期、馬場或場次尚未產生 prediction.json。")


def double_trio_artifact_path(date_value: Date, course: str) -> Path:
    """Return the fixed, runtime-contained official Double Trio artifact location."""
    return RUNTIME_ROOT / f"{date_value.year:04d}" / f"{date_value.month:02d}" / f"{date_value.day:02d}_{course}_{DOUBLE_TRIO_ARTIFACT_SUFFIX}"


def enrich_for_double_trio(prediction: dict[str, Any], date_value: Date, course: str, race_no: int) -> dict[str, Any]:
    """Use the existing failure-tolerant N6 enrichment without altering saved data."""
    try:
        return enrich_prediction(prediction, date_value.isoformat(), course, race_no)
    except Exception as exc:  # N6 is optional, but Double Trio must fail closed without a full joint ranking.
        logger.warning("N6 enrichment for Double Trio failed closed: %s", type(exc).__name__)
        fallback = dict(prediction)
        fallback["n6_integration"] = {
            "status": "unavailable",
            "message": "N6 聯合排名暫不可用；孖T策略不會以不完整資料選馬。",
        }
        return fallback


def double_trio_strategy_for_meeting(date_value: Date, course: str) -> dict[str, Any]:
    """Assemble a display-only strategy from official legs and saved predictions."""
    artifact_path = double_trio_artifact_path(date_value, course)
    if not artifact_path.is_file():
        return {
            "status": "official_data_unavailable",
            "meeting": {"race_date": date_value.isoformat(), "racecourse": course},
            "events": [],
            "message": "尚未找到可驗證的官方孖T場次工件；系統不會以固定場次代替。",
        }
    official_payload = read_json_artifact(artifact_path)
    predictions_by_race: dict[int, dict[str, Any]] = {}
    events = official_payload.get("events") if isinstance(official_payload, dict) else []
    if isinstance(events, list):
        requested_races = {
            int(leg["race_no"])
            for event in events if isinstance(event, dict)
            for leg in (event.get("legs") if isinstance(event.get("legs"), list) else [])
            if isinstance(leg, dict) and isinstance(leg.get("race_no"), int)
        }
        for race_no in requested_races:
            try:
                job_dir = find_job(date_value, course, race_no)
            except HTTPException:
                continue
            predictions_by_race[race_no] = enrich_for_double_trio(
                read_json_artifact(job_dir / "prediction.json"), date_value, course, race_no
            )
    return build_meeting_strategies(official_payload, predictions_by_race)


@app.get("/health", tags=["system"])
async def health() -> dict[str, Any]:
    """Return service health without exposing filesystem paths or mutable state."""
    return {
        "status": "ok",
        "service": "hkjc-ml-v10-readonly-api",
        "read_only": True,
        "runtime_available": RUNTIME_ROOT.is_dir(),
        "cors_origin_count": len(cors_origins()),
    }


@app.get("/api/races/{date}", tags=["races"])
async def races_for_date(date: str = ApiPath(..., description="賽日，YYYY-MM-DD")) -> dict[str, Any]:
    """List races with a completed V10.2 prediction artifact for one date."""
    requested_date = parse_iso_date(date)
    races = discover_jobs(requested_date)
    return {"date": requested_date.isoformat(), "count": len(races), "races": races}


@app.get("/api/prediction/{date}/{course}/{race_no}", tags=["predictions"])
async def prediction_for_race(
    date: str = ApiPath(..., description="賽日，YYYY-MM-DD"),
    course: str = ApiPath(..., description="ST 或 HV"),
    race_no: int = ApiPath(..., description="場次"),
) -> dict[str, Any]:
    """Return the saved V10 prediction and optional high-probability filter verbatim."""
    requested_date = parse_iso_date(date)
    normalized_course = normalize_course(course)
    normalized_race_no = normalize_race_no(race_no)
    job_dir = find_job(requested_date, normalized_course, normalized_race_no)
    prediction = read_json_artifact(job_dir / "prediction.json")
    try:
        enriched_prediction = enrich_prediction(
            prediction, requested_date.isoformat(), normalized_course, normalized_race_no
        )
    except Exception as exc:  # N6 is strictly auxiliary; its failure must not block V10 reads.
        logger.warning("N6 enrichment failed closed: %s", type(exc).__name__)
        enriched_prediction = dict(prediction)
        enriched_prediction["n6_integration"] = {
            "status": "unavailable",
            "message": "N6 輔助服務暫不可用；V10 原有分析維持不變。",
            "notice": "未改寫 V10 已保存的勝率、EV、Kelly 或既有風險提示。",
        }
    filter_path = job_dir / "high_probability_filter.json"
    return {
        "date": requested_date.isoformat(),
        "course": normalized_course,
        "race_no": normalized_race_no,
        "prediction": enriched_prediction,
        "high_probability_filter": read_json_artifact(filter_path) if filter_path.is_file() else None,
    }


@app.get("/api/double-trio/{date}/{course}", tags=["double-trio"])
async def double_trio_for_date(
    date: str = ApiPath(..., description="賽日，YYYY-MM-DD"),
    course: str = ApiPath(..., description="ST 或 HV"),
) -> dict[str, Any]:
    """Return a display-only four-horse Double Trio plan from official legs only."""
    requested_date = parse_iso_date(date)
    normalized_course = normalize_course(course)
    return double_trio_strategy_for_meeting(requested_date, normalized_course)


@app.get("/api/double-trio/backtest", tags=["double-trio"])
async def double_trio_backtest_summary() -> dict[str, Any]:
    """Return only the allow-listed, cohort-isolated historical backtest summary."""
    return read_json_project_report(DOUBLE_TRIO_BACKTEST_SUMMARY_PATH)


@app.get("/api/overseas-deep/backtest", tags=["overseas-deep"])
async def overseas_deep_backtest_summary() -> dict[str, Any]:
    """Return the fixed strict overseas research-proxy evaluation summary."""
    return read_overseas_deep_backtest_summary()


@app.get("/api/overseas-deep/{date}/{simulcast_code}/{race_no}", tags=["overseas-deep"])
async def overseas_deep_for_race(
    date: str = ApiPath(..., description="海外賽日，YYYY-MM-DD"),
    simulcast_code: str = ApiPath(..., description="官方海外轉播代碼，例如 S1"),
    race_no: int = ApiPath(..., description="海外場次"),
) -> dict[str, Any]:
    """Return a source-labelled overseas deep-data artifact without N6 enrichment."""
    requested_date = parse_iso_date(date)
    normalized_code = normalize_simulcast_code(simulcast_code)
    normalized_race_no = normalize_race_no(race_no)
    return read_overseas_deep_artifact(requested_date, normalized_code, normalized_race_no)


@app.get("/api/report/{date}/{course}/{race_no}", response_class=PlainTextResponse, tags=["reports"])
async def report_for_race(
    date: str = ApiPath(..., description="賽日，YYYY-MM-DD"),
    course: str = ApiPath(..., description="ST 或 HV"),
    race_no: int = ApiPath(..., description="場次"),
) -> PlainTextResponse:
    """Return the saved Markdown report as text/markdown for client-side rendering."""
    requested_date = parse_iso_date(date)
    normalized_course = normalize_course(course)
    normalized_race_no = normalize_race_no(race_no)
    job_dir = find_job(requested_date, normalized_course, normalized_race_no)
    report_path = job_dir / "pre_race_report.md"
    report_text = safely_read_bytes(report_path, MAX_REPORT_BYTES).decode("utf-8")
    return PlainTextResponse(content=report_text, media_type="text/markdown; charset=utf-8")


@app.exception_handler(HTTPException)
async def http_exception_handler(_, exc: HTTPException) -> JSONResponse:
    """Keep HTTP errors consistently JSON for a future browser client."""
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail, "read_only": True})


# Local development command:
# uvicorn web_api:app --host 0.0.0.0 --port 8000 --reload
