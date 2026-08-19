"""Read-only, failure-tolerant N6 enrichment for V10 API responses.

This module never changes saved V10 runtime artifacts, V10 probabilities, EV, Kelly,
or existing guidance.  It calls only N6's loopback API and returns an in-memory
response enrichment suitable for the V10 read-only Dashboard.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import os
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

N6_BASE_URL = os.getenv("N6_API_BASE_URL", "http://127.0.0.1:5001").rstrip("/")
N6_TIMEOUT_SECONDS = float(os.getenv("N6_API_TIMEOUT_SECONDS", "2.5"))
V10_WEIGHT = 0.5
N6_WEIGHT = 0.5
# Scores within this display/ranking precision are treated as a numerical tie.
# It suppresses inconsequential floating-point differences from normalization while
# preserving materially distinct race-normalized probabilities.
JOINT_RANK_TIE_DECIMALS = 12
logger = logging.getLogger(__name__)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _horse_key(value: Any) -> str:
    return "".join(str(value or "").split())


def _runner_number(value: Any) -> int | None:
    number = _number(value)
    if number is None or number <= 0 or int(number) != number:
        return None
    return int(number)


def _joint_rank_key(row: dict[str, Any]) -> tuple[float, int, int, str]:
    """Return an input-order-independent ordering key for an N6+V10 joint score.

    The first key is a precision-bucketed joint probability. Exact and numerical
    near ties therefore resolve by the official runner number, then the normalized
    horse name only as a defensive fallback. Missing or invalid runner numbers sort
    after valid official numbers, so a malformed artifact cannot displace one.
    """
    probability = _number(row.get("joint_neural_probability"))
    if probability is None:
        raise ValueError("joint_neural_probability 無效")
    runner_no = _runner_number(row.get("horse_no", row.get("runner_no")))
    return (
        -round(probability, JOINT_RANK_TIE_DECIMALS),
        0 if runner_no is not None else 1,
        runner_no if runner_no is not None else 2**31 - 1,
        _horse_key(row.get("horse_name")),
    )


def _has_invalid_or_duplicate_runner_numbers(rows: list[dict[str, Any]]) -> bool:
    runner_numbers = [_runner_number(row.get("horse_no", row.get("runner_no"))) for row in rows]
    valid = [number for number in runner_numbers if number is not None]
    return len(valid) != len(runner_numbers) or len(valid) != len(set(valid))


def _post_json(path: str, payload: dict[str, Any] | None = None) -> tuple[int, dict[str, Any] | None]:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{N6_BASE_URL}{path}",
        data=body,
        method="POST",
        headers={"Accept": "application/json", **({"Content-Type": "application/json"} if body else {})},
    )
    try:
        with urlopen(request, timeout=N6_TIMEOUT_SECONDS) as response:
            raw = response.read(2 * 1024 * 1024)
            decoded = json.loads(raw.decode("utf-8"))
            return int(response.status), decoded if isinstance(decoded, dict) else None
    except HTTPError as error:
        return int(error.code), None
    except (URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        logger.warning("N6 loopback API unavailable: %s", type(error).__name__)
        return 0, None


def _live_payload(prediction: dict[str, Any], date: str, course: str, race_no: int) -> dict[str, Any] | None:
    race_data = prediction.get("race") if isinstance(prediction.get("race"), dict) else {}
    required = ("distance_m", "surface")
    if any(race_data.get(key) in (None, "") for key in required):
        return None
    runners: list[dict[str, Any]] = []
    for row in prediction.get("predictions", []):
        if not isinstance(row, dict):
            continue
        horse_name = row.get("horse_name")
        if not _horse_key(horse_name):
            continue
        runner = {
            "horse_name": horse_name,
            "horse_code": row.get("horse_code"),
            "jockey": row.get("jockey"),
            "trainer": row.get("trainer"),
            "draw": row.get("draw"),
            "weight_lbs": row.get("weight_lbs"),
            "win_odds": row.get("market_odds"),
        }
        runners.append({key: value for key, value in runner.items() if value not in (None, "")})
    if len(runners) < 2:
        return None
    return {
        "race": {
            "race_date": date,
            "racecourse": course,
            "race_no": race_no,
            "race_class": race_data.get("race_class"),
            "distance_m": race_data.get("distance_m"),
            "surface": race_data.get("surface"),
            "course_config": race_data.get("course_config"),
            "going": race_data.get("going"),
        },
        "runners": runners,
    }


def fetch_n6_scores(prediction: dict[str, Any], date: str, course: str, race_no: int) -> tuple[str, list[dict[str, Any]] | None, str, dict[str, Any] | None]:
    """Prefer historical pre-race features; fall back to a supplied future-race card."""
    status, payload = _post_json(f"/v1/inference/historical/{date}/{course}/{race_no}")
    if status == 200 and isinstance(payload, dict) and isinstance(payload.get("scores"), list):
        return "historical_pre_race_features", payload["scores"], "available", payload.get("model") if isinstance(payload.get("model"), dict) else None
    if status not in {0, 404}:
        return "unavailable", None, "N6 服務未能完成歷史賽事評分；V10 原有分析維持不變。", None
    live_payload = _live_payload(prediction, date, course, race_no)
    if live_payload is None:
        return "unavailable", None, "N6 未找到歷史特徵，且 V10 工件缺少完整賽前資料；V10 原有分析維持不變。", None
    status, payload = _post_json("/v1/inference/score", live_payload)
    if status == 200 and isinstance(payload, dict) and isinstance(payload.get("scores"), list):
        return "live_card_plus_current_elo", payload["scores"], "available", payload.get("model") if isinstance(payload.get("model"), dict) else None
    return "unavailable", None, "N6 服務暫不可用；V10 原有分析維持不變。", None


def _unavailable_enrichment(message: str) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "n6_base_url": "loopback internal service",
        "message": message,
        "notice": "N6 缺席時不會影響 V10 已保存的勝率、EV、Kelly 或既有風險提示。",
    }


def enrich_prediction(prediction: dict[str, Any], date: str, course: str, race_no: int) -> dict[str, Any]:
    """Return a copied V10 prediction containing optional N6 and joint-score fields."""
    result = copy.deepcopy(prediction)
    rows = result.get("predictions")
    if not isinstance(rows, list) or not rows:
        result["n6_integration"] = _unavailable_enrichment("V10 預測工件沒有可對接的馬匹列。")
        return result
    if any(not isinstance(row, dict) for row in rows) or _has_invalid_or_duplicate_runner_numbers(rows):
        result["n6_integration"] = _unavailable_enrichment("V10 工件含重複或無效馬號；為確保孖T聯合排名可重現，未顯示聯合推薦。")
        return result
    mode, scores, status, model_descriptor = fetch_n6_scores(result, date, course, race_no)
    if scores is None:
        result["n6_integration"] = _unavailable_enrichment(status)
        return result

    score_by_horse: dict[str, dict[str, Any]] = {}
    for score in scores:
        if isinstance(score, dict) and _horse_key(score.get("horse_name")):
            score_by_horse[_horse_key(score["horse_name"])] = score
    matched: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        score = score_by_horse.get(_horse_key(row.get("horse_name")))
        if score is None:
            continue
        neural_probability = _number(score.get("neural_win_probability"))
        neural_score = _number(score.get("neural_score"))
        neural_rank = _number(score.get("neural_rank"))
        if neural_probability is None or neural_probability < 0.0 or neural_score is None or neural_rank is None:
            continue
        row["n6_neural_win_probability"] = neural_probability
        row["n6_neural_score"] = neural_score
        row["n6_rank"] = int(neural_rank)
        matched.append(row)
    if len(matched) != len(rows):
        result["n6_integration"] = _unavailable_enrichment("N6 評分馬匹與 V10 工件未能完整對應；為避免局部排名誤導，未顯示聯合推薦。")
        return result

    v10_values = [_number(row.get("predicted_win_probability")) for row in matched]
    n6_values = [_number(row.get("n6_neural_win_probability")) for row in matched]
    if any(value is None or value < 0.0 for value in v10_values + n6_values):
        result["n6_integration"] = _unavailable_enrichment("V10 或 N6 機率格式無效；未顯示聯合推薦。")
        return result
    v10_total = math.fsum(float(value) for value in v10_values if value is not None)
    n6_total = math.fsum(float(value) for value in n6_values if value is not None)
    if v10_total <= 0.0 or n6_total <= 0.0:
        result["n6_integration"] = _unavailable_enrichment("V10 或 N6 的場內機率總和無效；未顯示聯合推薦。")
        return result

    for row, v10_value, n6_value in zip(matched, v10_values, n6_values):
        joint_probability = V10_WEIGHT * (float(v10_value) / v10_total) + N6_WEIGHT * (float(n6_value) / n6_total)
        row["joint_neural_probability"] = joint_probability
        row["joint_neural_score"] = 100.0 * joint_probability
    ordered = sorted(matched, key=_joint_rank_key)
    bucket_counts: dict[float, int] = {}
    for row in ordered:
        bucket = _joint_rank_key(row)[0]
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
    for rank, row in enumerate(ordered, start=1):
        row["joint_rank"] = rank
        row["joint_rank_tie_break"] = (
            "joint_probability_bucket_then_horse_no"
            if bucket_counts[_joint_rank_key(row)[0]] > 1
            else "joint_probability"
        )
        v10_rank = _number(row.get("rank"))
        n6_rank = _number(row.get("n6_rank"))
        is_consensus = bool(v10_rank is not None and n6_rank is not None and v10_rank <= 3 and n6_rank <= 3)
        row["joint_recommendation"] = "綜合聯合推薦" if is_consensus else "聯合觀察"
        row["joint_consensus"] = is_consensus

    candidates = [
        {
            "horse_name": row.get("horse_name"),
            "horse_no": row.get("horse_no"),
            "joint_rank": row["joint_rank"],
            "joint_neural_score": row["joint_neural_score"],
            "v10_rank": row.get("rank"),
            "n6_rank": row.get("n6_rank"),
        }
        for row in ordered if row["joint_consensus"]
    ]
    result["n6_integration"] = {
        "status": "available",
        "mode": mode,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "weights": {"v10_probability": V10_WEIGHT, "n6_probability": N6_WEIGHT},
        "method": "兩個場內正規化勝率以等權重合成；聯合排名按 12 位小數機率桶排序，數值同分再按官方馬號遞增，確保輸入列順序不影響排名。N6 只作輔助訊號，未改寫 V10 勝率、EV、Kelly 或既有風險提示。",
        "model": model_descriptor,
        "combined_recommendation": {
            "label": "綜合聯合推薦",
            "candidate_count": len(candidates),
            "candidates": candidates,
            "notice": "僅當 V10 與 N6 均列入同場前 3 名才標示。這是模型一致性提示，不是投注指令、保證或對未來賽果的承諾。",
        },
    }
    return result
