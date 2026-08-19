"""Loopback-only FastAPI inference service for the N6 neural engine."""

from __future__ import annotations

from functools import lru_cache
import logging
import os
import threading
from typing import Any

import numpy as np
import pandas as pd
import torch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .config import MODEL_PATH, PREPROCESSOR_PATH, TEST_PREDICTIONS_PATH, TRAINING_REPORT_PATH, V10_DB_PATH
from .feature_engineering import build_live_feature_frame, load_historical_race, score_to_race_probabilities
from .model import LoadedN6Model, load_model_bundle

logger = logging.getLogger(__name__)


def _max_concurrent_inferences() -> int:
    """Return a bounded CPU-safe inference concurrency limit for this process."""
    raw = os.getenv("N6_MAX_CONCURRENT_INFERENCES", "2")
    try:
        value = int(raw)
    except ValueError as error:
        raise RuntimeError("N6_MAX_CONCURRENT_INFERENCES must be a positive integer") from error
    if value < 1 or value > 8:
        raise RuntimeError("N6_MAX_CONCURRENT_INFERENCES must be between 1 and 8")
    return value


MAX_CONCURRENT_INFERENCES = _max_concurrent_inferences()
INFERENCE_GATE = threading.BoundedSemaphore(MAX_CONCURRENT_INFERENCES)

app = FastAPI(
    title="N6 Neural Calculation Engine",
    version="1.0.0",
    description="Internal loopback-only neural race scoring API. V10 is always read using immutable SQLite read-only connections.",
    docs_url=None,
    redoc_url=None,
)


class RaceInput(BaseModel):
    model_config = ConfigDict(extra="allow")

    race_date: str = Field(..., description="Race date, YYYY-MM-DD")
    racecourse: str = Field(..., min_length=1, max_length=16)
    race_no: int = Field(..., ge=1, le=20)
    race_class: str | None = None
    distance_m: float | None = Field(default=None, ge=400, le=4000)
    surface: str | None = None
    course_config: str | None = None
    going: str | None = None


class RunnerInput(BaseModel):
    model_config = ConfigDict(extra="allow")

    horse_name: str = Field(..., min_length=1, max_length=100)
    horse_code: str | None = Field(default=None, max_length=32)
    jockey: str | None = Field(default=None, max_length=100)
    trainer: str | None = Field(default=None, max_length=100)
    draw: int | None = Field(default=None, ge=1, le=30)
    weight_lbs: float | None = Field(default=None, ge=80, le=160)
    win_odds: float | None = Field(default=None, gt=1.0, le=1000.0)


class ScoreRequest(BaseModel):
    race: RaceInput
    runners: list[RunnerInput] = Field(..., min_length=1, max_length=30)


@lru_cache(maxsize=1)
def get_bundle() -> LoadedN6Model:
    return load_model_bundle(MODEL_PATH, PREPROCESSOR_PATH)


def _model_descriptor(bundle: LoadedN6Model) -> dict[str, Any]:
    """Expose a compact, caller-safe production model contract."""
    contract = list(bundle.metadata.get("feature_contract", []))
    return {
        "production_release": bundle.metadata.get("production_release", "unversioned"),
        "experiment_id": bundle.metadata.get("experiment_id"),
        "variant": bundle.metadata.get("variant"),
        "input_dim": bundle.metadata.get("input_dim"),
        "raw_feature_count": len(contract),
        "market_feature_policy": "market_implied_probability + market_odds_available; market_log_odds excluded" if "market_log_odds" not in contract else "market_log_odds + market_implied_probability + market_odds_available",
        "feature_contract": contract,
    }


@app.on_event("startup")
def preload_model() -> None:
    """Load model artifacts in every worker before accepting inference requests."""
    get_bundle()
    logger.info("N6 model preloaded; max_concurrent_inferences=%s", MAX_CONCURRENT_INFERENCES)


def _score(frame: pd.DataFrame) -> list[dict[str, Any]]:
    bundle = get_bundle()
    values = np.asarray(bundle.preprocessor.transform(frame), dtype=np.float32)
    with torch.no_grad():
        logits = bundle.model(torch.tensor(values, dtype=torch.float32)).cpu().numpy()
    temperature = float(bundle.metadata.get("temperature", 1.0))
    probabilities = score_to_race_probabilities(logits / temperature, frame["race_group"])
    output = frame[["race_date", "racecourse", "race_no", "horse_name"]].copy()
    output["neural_win_probability"] = probabilities
    output["neural_score"] = probabilities * 100.0
    output["neural_rank"] = output.groupby(["race_date", "racecourse", "race_no"])["neural_win_probability"].rank(
        method="first", ascending=False
    ).astype(int)
    output = output.sort_values("neural_rank")
    return [
        {
            "horse_name": str(row.horse_name),
            "neural_rank": int(row.neural_rank),
            "neural_score": round(float(row.neural_score), 4),
            "neural_win_probability": round(float(row.neural_win_probability), 6),
        }
        for row in output.itertuples(index=False)
    ]


@app.get("/health")
def health() -> dict[str, Any]:
    try:
        bundle = get_bundle()
        return {
            "status": "ok",
            "engine": "N6",
            "model_loaded": True,
            "artifact_type": bundle.metadata.get("artifact_type"),
            "input_dim": bundle.metadata.get("input_dim"),
            "v10_source_path": str(V10_DB_PATH),
            "v10_access": "mode=ro&immutable=1; PRAGMA query_only=ON",
            "bind_policy": "loopback only (127.0.0.1:5001)",
            "max_concurrent_inferences": MAX_CONCURRENT_INFERENCES,
            "model": _model_descriptor(bundle),
        }
    except Exception as error:  # pragma: no cover - production readiness response
        raise HTTPException(status_code=503, detail=f"N6 model unavailable: {error}") from error


@app.get("/v1/model-info")
def model_info() -> dict[str, Any]:
    try:
        bundle = get_bundle()
        report = bundle.metadata.get("report", {})
        return {
            "engine": "N6",
            "architecture": report.get("architecture"),
            "source": report.get("source"),
            "split": report.get("split"),
            "test_race_metrics": report.get("test_race_metrics"),
            "test_row_metrics": report.get("test_row_metrics"),
            "calibration": report.get("calibration"),
            "model": _model_descriptor(bundle),
            "artifacts_present": {
                "model": MODEL_PATH.is_file(),
                "preprocessor": PREPROCESSOR_PATH.is_file(),
                "training_report": TRAINING_REPORT_PATH.is_file(),
                "test_predictions": TEST_PREDICTIONS_PATH.is_file(),
            },
        }
    except Exception as error:
        raise HTTPException(status_code=503, detail=f"N6 model unavailable: {error}") from error


@app.post("/v1/inference/score")
def score_live(request: ScoreRequest) -> dict[str, Any]:
    """Score a supplied pre-race card using live values and read-only V10 ELO state."""
    try:
        race = request.race.model_dump(exclude_none=True)
        runners = [runner.model_dump(exclude_none=True) for runner in request.runners]
        with INFERENCE_GATE:
            frame = build_live_feature_frame(race, runners)
            scores = _score(frame)
        return {
            "engine": "N6",
            "race": {"race_date": race["race_date"], "racecourse": race["racecourse"], "race_no": race["race_no"]},
            "result_type": "pre_race_neural_score",
            "scores": scores,
            "data_contract": "caller-provided pre-race values plus read-only elo_current_state; no V10 writes",
            "model": _model_descriptor(get_bundle()),
        }
    except (ValueError, FileNotFoundError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:  # pragma: no cover - defensive API boundary
        logger.exception("N6 live inference failed")
        raise HTTPException(status_code=500, detail=f"N6 inference failure: {error}") from error


@app.post("/v1/inference/historical/{race_date}/{racecourse}/{race_no}")
def score_historical(race_date: str, racecourse: str, race_no: int) -> dict[str, Any]:
    """Audit a known historical race using only stored pre-race V10 feature values."""
    try:
        with INFERENCE_GATE:
            frame = load_historical_race(race_date, racecourse, race_no)
            scores = _score(frame)
        return {
            "engine": "N6",
            "race": {"race_date": race_date, "racecourse": racecourse, "race_no": race_no},
            "result_type": "historical_pre_race_neural_score",
            "scores": scores,
            "data_contract": "elo_feature_store + starters.win_odds under immutable SQLite read-only access",
            "model": _model_descriptor(get_bundle()),
        }
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (ValueError, FileNotFoundError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:  # pragma: no cover - defensive API boundary
        logger.exception("N6 historical inference failed")
        raise HTTPException(status_code=500, detail=f"N6 historical inference failure: {error}") from error
