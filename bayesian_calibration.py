#!/usr/bin/env python3
"""V10.3 P1 Bayesian calibration and uncertainty overlay.

This module is intentionally separate from the V10.2 LightGBM + CatBoost
prediction path.  It consumes only frozen, field-normalized V10.2 pre-race
probabilities as an offset and emits a sidecar JSON for risk disclosure.  It
never overwrites ``predicted_win_probability``, rank, EV, or Kelly fields.

The first implementation is a deliberately small hierarchical NumPyro model:

    logit_ri = alpha * log(p_v102_ri) + beta_course[course_r] * delta_component_ri
    winner_r ~ Categorical(softmax(logit_r))

where ``delta_component`` is the within-field difference between normalized
LightGBM and CatBoost component strengths.  Course slopes receive partial
pooling around a global slope.  All inputs are pre-race fields saved by V10.2;
post-race labels are accepted only by the offline ``fit`` command.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import platform
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Iterable

import numpy as np

SCHEMA_VERSION = "v10_3_bayesian_calibration_overlay_v1"
MODEL_SCHEMA_VERSION = "v10_3_bayesian_calibration_model_v1"
EPSILON = 1e-12
PROBABILITY_TOLERANCE = 1e-6
DEFAULT_MODEL = "models/v103_bayesian_calibration.npz"


class BayesianCalibrationError(ValueError):
    """Raised when a frozen V10.2 artifact fails the V10.3 data contract."""


@dataclass(frozen=True)
class FrozenRace:
    race_key: str
    race_date: str
    racecourse: str
    horse_names: tuple[str, ...]
    baseline: np.ndarray
    component_delta: np.ndarray
    winner_index: int | None


def hkt_timestamp() -> str:
    hkt = timezone(timedelta(hours=8))
    return datetime.now(timezone.utc).astimezone(hkt).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def finite_number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def parse_int(value: Any) -> int | None:
    number = finite_number(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def stable_softmax(values: Iterable[float]) -> np.ndarray:
    array = np.asarray(list(values), dtype=float)
    if array.size == 0 or not np.isfinite(array).all():
        raise BayesianCalibrationError("無法對空白或非有限 logits 進行場內 softmax。")
    shifted = array - np.max(array)
    weights = np.exp(shifted)
    total = float(np.sum(weights))
    if not math.isfinite(total) or total <= 0.0:
        raise BayesianCalibrationError("場內 softmax 產生無效總權重。")
    return weights / total


def quantile(values: np.ndarray, probability: float) -> np.ndarray:
    return np.quantile(values, probability, axis=0, method="linear")


def normalize_component(values: list[float | None]) -> np.ndarray:
    """Normalize valid V10.2 component strengths for an audit-only contrast.

    Missing CatBoost output remains a neutral zero contrast rather than being
    imputed from results or future data.
    """
    if any(item is None or item < 0.0 for item in values):
        return np.zeros(len(values), dtype=float)
    array = np.asarray([float(item) for item in values], dtype=float)
    total = float(np.sum(array))
    return array / total if total > EPSILON else np.zeros(len(values), dtype=float)


def race_key_from_row(row: dict[str, Any]) -> str:
    explicit = str(row.get("race_group") or "").strip()
    if explicit:
        return explicit
    race_date = str(row.get("race_date") or "").strip()
    course = str(row.get("racecourse") or "").strip()
    race_no = str(row.get("race_no") or "").strip()
    if race_date and course and race_no:
        return f"{race_date}|{course}|{race_no}"
    raise BayesianCalibrationError("缺少 race_group 或 race_date/racecourse/race_no，無法建立安全場次分組。")


def build_frozen_race(rows: list[dict[str, Any]], require_target: bool) -> FrozenRace:
    if len(rows) < 2:
        raise BayesianCalibrationError("場次出賽馬少於 2 匹。")
    baseline_values = [finite_number(row.get("race_normalized_probability", row.get("predicted_win_probability"))) for row in rows]
    if any(value is None or value <= 0.0 or value > 1.0 for value in baseline_values):
        raise BayesianCalibrationError("V10.2 offset 必須為嚴格正且介於 0 至 1 的場內機率。")
    baseline = np.asarray([float(value) for value in baseline_values], dtype=float)
    if abs(float(np.sum(baseline)) - 1.0) > PROBABILITY_TOLERANCE:
        raise BayesianCalibrationError("V10.2 場內機率和不等於 1；拒絕在不安全輸入上套用 overlay。")

    lgb = normalize_component([finite_number(row.get("lightgbm_calibrated_probability")) for row in rows])
    cat = normalize_component([finite_number(row.get("catboost_calibrated_probability")) for row in rows])
    component_delta = lgb - cat
    # Floating-point centering protects the softmax intercept from a field-wide
    # component scale artefact while retaining runner-relative disagreement.
    component_delta = component_delta - float(np.mean(component_delta))

    labels = [parse_int(row.get("target_win")) for row in rows]
    if require_target:
        if any(label not in {0, 1} for label in labels) or sum(int(label or 0) for label in labels) != 1:
            raise BayesianCalibrationError("離線 fit 每場必須有唯一官方 target_win=1。")
        winner_index = labels.index(1)
    else:
        winner_index = None

    names = tuple(str(row.get("horse_name") or row.get("horse_no") or "").strip() for row in rows)
    if any(not name for name in names) or len(set(names)) != len(names):
        raise BayesianCalibrationError("馬匹名稱缺失或在同一場重複，不能建立可稽核 posterior。")
    course = str(rows[0].get("racecourse") or "UNKNOWN").strip().upper() or "UNKNOWN"
    race_date = str(rows[0].get("race_date") or "").strip()
    return FrozenRace(
        race_key=race_key_from_row(rows[0]),
        race_date=race_date,
        racecourse=course,
        horse_names=names,
        baseline=baseline,
        component_delta=component_delta,
        winner_index=winner_index,
    )


def load_frozen_csv(path: Path, require_target: bool = True) -> tuple[list[FrozenRace], Counter[str]]:
    if not path.exists():
        raise BayesianCalibrationError(f"找不到輸入工件：{path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise BayesianCalibrationError("預測 CSV 缺少標題列。")
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in reader:
            grouped[race_key_from_row(row)].append(row)
    frozen: list[FrozenRace] = []
    exclusions: Counter[str] = Counter()
    for key, rows in grouped.items():
        try:
            race = build_frozen_race(rows, require_target=require_target)
        except BayesianCalibrationError as exc:
            exclusions[str(exc)] += 1
            continue
        frozen.append(race)
    frozen.sort(key=lambda item: (item.race_date, item.racecourse, item.race_key))
    if not frozen:
        raise BayesianCalibrationError("沒有任何符合 V10.3 場內機率與標籤契約的賽事。")
    return frozen, exclusions


def read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BayesianCalibrationError(f"無法讀取 {label}：{path}") from exc
    if not isinstance(payload, dict):
        raise BayesianCalibrationError(f"{label} 必須為 JSON object。")
    return payload


def validate_cohort_provenance(
    manifest_path: Path,
    input_path: Path,
    base_model_path: Path,
) -> dict[str, Any]:
    """Validate a collector-produced immutable cohort before a formal fit.

    The input CSV must be the collector's canonical materialization for exactly
    one full base-model SHA-256 bucket.  This rejects copied, mixed-version, or
    reconstructed CSVs even when their numerical rows appear plausible.
    """
    if not manifest_path.is_file():
        raise BayesianCalibrationError(f"找不到 cohort provenance manifest：{manifest_path}")
    if not input_path.is_file():
        raise BayesianCalibrationError(f"找不到 cohort canonical CSV：{input_path}")
    if not base_model_path.is_file():
        raise BayesianCalibrationError(f"找不到 base model：{base_model_path}")
    manifest = read_json_object(manifest_path, "cohort provenance manifest")
    if manifest.get("schema_version") != "v10_3_unseen_cohort_manifest_v1":
        raise BayesianCalibrationError("cohort provenance manifest schema 不相容。")
    base_model_sha = sha256_file(base_model_path)
    cohorts = manifest.get("model_cohorts")
    if not isinstance(cohorts, dict):
        raise BayesianCalibrationError("cohort provenance manifest 缺少 model_cohorts。")
    cohort = cohorts.get(base_model_sha)
    if not isinstance(cohort, dict):
        raise BayesianCalibrationError("manifest 沒有目前 base-model SHA-256 的獨立 cohort；禁止混合或借用其他模型版本。")
    canonical_text = str(cohort.get("canonical_training_csv_path") or "").strip()
    expected_csv_sha = str(cohort.get("canonical_training_csv_sha256") or "").strip().lower()
    expected_races = parse_int(cohort.get("canonical_training_race_count"))
    fingerprint = str(cohort.get("cohort_fingerprint") or "").strip().lower()
    if not canonical_text or len(expected_csv_sha) != 64 or any(char not in "0123456789abcdef" for char in expected_csv_sha):
        raise BayesianCalibrationError("manifest 缺少 canonical training CSV 路徑或有效 SHA-256。")
    if expected_races is None or expected_races < 1:
        raise BayesianCalibrationError("manifest 缺少有效 canonical_training_race_count。")
    if len(fingerprint) != 64 or any(char not in "0123456789abcdef" for char in fingerprint):
        raise BayesianCalibrationError("manifest 缺少有效 cohort_fingerprint。")
    canonical_path = Path(canonical_text).expanduser().resolve()
    if input_path.resolve() != canonical_path:
        raise BayesianCalibrationError("fit 輸入不是 manifest 登記的 canonical training CSV；拒絕複製或重建檔案。")
    actual_csv_sha = sha256_file(input_path)
    if actual_csv_sha != expected_csv_sha:
        raise BayesianCalibrationError("canonical training CSV SHA-256 與 manifest 不一致。")
    with input_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"race_date", "racecourse", "race_no", "horse_name", "race_normalized_probability", "target_win", "base_model_sha256"}
        if reader.fieldnames is None or not required.issubset(set(reader.fieldnames)):
            raise BayesianCalibrationError("canonical training CSV 缺少 provenance 所需欄位。")
        for line_no, row in enumerate(reader, start=2):
            if str(row.get("base_model_sha256") or "").strip().lower() != base_model_sha:
                raise BayesianCalibrationError(f"canonical training CSV 第 {line_no} 行的 base_model_sha256 不屬於目前模型。")
    return {
        "verification_status": "verified_immutable_unseen_cohort",
        "manifest_path": str(manifest_path.resolve()),
        "manifest_sha256": sha256_file(manifest_path),
        "cohort_fingerprint": fingerprint,
        "base_model_path": str(base_model_path.resolve()),
        "base_model_sha256": base_model_sha,
        "canonical_training_csv_path": str(input_path.resolve()),
        "canonical_training_csv_sha256": actual_csv_sha,
        "canonical_training_race_count": expected_races,
        "cohort_record_count": parse_int(cohort.get("record_count")),
    }


def fit_model(
    races: list[FrozenRace],
    output_path: Path,
    input_hash: str,
    advi_steps: int,
    posterior_draws: int,
    seed: int,
    cohort_provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fit a small partially pooled NumPyro categorical calibration model offline."""
    if advi_steps < 100:
        raise BayesianCalibrationError("SVI steps 必須不少於 100。")
    if posterior_draws < 50:
        raise BayesianCalibrationError("posterior draws 必須不少於 50。")
    if any(race.winner_index is None for race in races):
        raise BayesianCalibrationError("fit 只允許使用已結算且有唯一頭馬標籤的歷史賽事。")
    try:
        import jax
        import jax.numpy as jnp
        import numpyro
        import numpyro.distributions as dist
        from numpyro.infer import SVI, Trace_ELBO
        from numpyro.infer.autoguide import AutoNormal
        from numpyro.optim import Adam
    except ImportError as exc:  # pragma: no cover - exercised only on dependency-missing hosts.
        raise BayesianCalibrationError("未安裝 NumPyro／JAX；請以 requirements.txt 安裝 V10.3 的可選研究依賴。") from exc

    courses = sorted({race.racecourse for race in races})
    course_index = {course: index for index, course in enumerate(courses)}
    # Vectorize ragged fields into one padded categorical likelihood.  The mask
    # prevents padded runners from entering any per-race softmax and avoids a
    # JAX graph node per historical race, which is essential for monthly fits.
    race_count = len(races)
    max_field_size = max(len(race.baseline) for race in races)
    baseline_log = np.full((race_count, max_field_size), math.log(EPSILON), dtype=float)
    component_delta = np.zeros((race_count, max_field_size), dtype=float)
    runner_mask = np.zeros((race_count, max_field_size), dtype=bool)
    winners = np.zeros(race_count, dtype=np.int32)
    race_course_indices = np.zeros(race_count, dtype=np.int32)
    for index, race in enumerate(races):
        field_size = len(race.baseline)
        baseline_log[index, :field_size] = np.log(np.clip(race.baseline, EPSILON, 1.0))
        component_delta[index, :field_size] = race.component_delta
        runner_mask[index, :field_size] = True
        winners[index] = int(race.winner_index or 0)
        race_course_indices[index] = course_index[race.racecourse]
    baseline_log_jax = jnp.asarray(baseline_log)
    component_delta_jax = jnp.asarray(component_delta)
    runner_mask_jax = jnp.asarray(runner_mask)
    winners_jax = jnp.asarray(winners)
    race_course_indices_jax = jnp.asarray(race_course_indices)

    def model() -> None:
        log_alpha = numpyro.sample("log_alpha", dist.Normal(0.0, 0.25))
        alpha = jnp.exp(log_alpha)
        beta_global = numpyro.sample("beta_global", dist.Normal(0.0, 0.30))
        sigma_course = numpyro.sample("sigma_course", dist.HalfNormal(0.20))
        beta_course_raw = numpyro.sample("beta_course_raw", dist.Normal(0.0, 1.0).expand([len(courses)]))
        beta_course = beta_global + sigma_course * beta_course_raw
        logits = alpha * baseline_log_jax + beta_course[race_course_indices_jax][:, None] * component_delta_jax
        masked_logits = jnp.where(runner_mask_jax, logits, -jnp.inf)
        numpyro.sample("winner", dist.Categorical(logits=masked_logits), obs=winners_jax)

    guide = AutoNormal(model)
    svi = SVI(model, guide, Adam(0.02), Trace_ELBO())
    rng_key = jax.random.PRNGKey(seed)
    svi_state = svi.init(rng_key)
    for _ in range(advi_steps):
        svi_state, _ = svi.update(svi_state)
    params = svi.get_params(svi_state)
    posterior_samples = guide.sample_posterior(jax.random.PRNGKey(seed + 1), params, sample_shape=(posterior_draws,))

    log_alpha_draws = np.asarray(posterior_samples["log_alpha"], dtype=float)
    alpha_draws = np.exp(log_alpha_draws)
    beta_global_draws = np.asarray(posterior_samples["beta_global"], dtype=float)
    sigma_course_draws = np.asarray(posterior_samples["sigma_course"], dtype=float)
    beta_course_raw_draws = np.asarray(posterior_samples["beta_course_raw"], dtype=float)
    beta_course_draws = beta_global_draws[:, None] + sigma_course_draws[:, None] * beta_course_raw_draws
    if alpha_draws.shape[0] != posterior_draws or beta_course_draws.shape != (posterior_draws, len(courses)):
        raise BayesianCalibrationError("NumPyro posterior draws 形狀不符合已登記資料契約。")
    if not all(np.isfinite(item).all() for item in (alpha_draws, beta_global_draws, beta_course_draws, sigma_course_draws)):
        raise BayesianCalibrationError("NumPyro posterior draws 含非有限值，拒絕封存模型。")

    metadata = {
        "schema_version": MODEL_SCHEMA_VERSION,
        "generated_at_hkt": hkt_timestamp(),
        "method": "numpyro_svi_autonormal_hierarchical_offset_categorical",
        "formal_probability_replacement": False,
        "v102_core_modified": False,
        "input_artifact_sha256": input_hash,
        "input_races": len(races),
        "cohort_provenance": cohort_provenance or {
            "verification_status": "unverified_exploratory_direct_api",
            "notice": "直接呼叫 fit_model 的探索性研究工件不得作正式採納或賽前披露模型。",
        },
        "courses": courses,
        "svi_steps": advi_steps,
        "posterior_draws": posterior_draws,
        "seed": seed,
        "python_version": platform.python_version(),
        "numpyro_version": str(numpyro.__version__),
        "jax_version": str(jax.__version__),
        "parameter_summary": {
            "alpha": summary(alpha_draws),
            "beta_global": summary(beta_global_draws),
            "sigma_course": summary(sigma_course_draws),
            "beta_course": {course: summary(beta_course_draws[:, index]) for index, course in enumerate(courses)},
        },
        "notice": "此模型只以 frozen V10.2 pre-race probability 作 offset；target_win 只用於離線 fit，正式賽前只輸出風險披露 sidecar。",
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        output_path,
        alpha_draws=alpha_draws,
        beta_global_draws=beta_global_draws,
        beta_course_draws=beta_course_draws,
        sigma_course_draws=sigma_course_draws,
        courses=np.asarray(courses, dtype=str),
        metadata_json=np.asarray(json.dumps(metadata, ensure_ascii=False), dtype=str),
    )
    metadata["model_sha256"] = sha256_file(output_path)
    return metadata


def summary(values: np.ndarray) -> dict[str, float]:
    array = np.asarray(values, dtype=float)
    return {
        "posterior_mean": float(np.mean(array)),
        "posterior_p05": float(np.quantile(array, 0.05)),
        "posterior_p95": float(np.quantile(array, 0.95)),
    }


def load_model(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(path)
    with np.load(path, allow_pickle=False) as archive:
        metadata = json.loads(str(archive["metadata_json"].item()))
        if metadata.get("schema_version") != MODEL_SCHEMA_VERSION:
            raise BayesianCalibrationError("Bayesian model schema 不相容。")
        model = {
            "metadata": metadata,
            "alpha_draws": np.asarray(archive["alpha_draws"], dtype=float),
            "beta_global_draws": np.asarray(archive["beta_global_draws"], dtype=float),
            "beta_course_draws": np.asarray(archive["beta_course_draws"], dtype=float),
            "courses": [str(value) for value in archive["courses"].tolist()],
        }
    draws = model["alpha_draws"].shape[0]
    if draws < 50 or model["beta_course_draws"].shape != (draws, len(model["courses"])):
        raise BayesianCalibrationError("Bayesian model draws 無效或不完整。")
    return model


def sample_indices(total: int, desired: int, seed: int) -> np.ndarray:
    if desired < 50:
        raise BayesianCalibrationError("賽前 posterior draws 必須不少於 50。")
    if desired >= total:
        return np.arange(total)
    rng = np.random.default_rng(seed)
    return np.sort(rng.choice(total, size=desired, replace=False))


def posterior_draw_vectors(
    model: dict[str, Any],
    frozen: FrozenRace,
    posterior_draws: int,
    seed: int,
) -> tuple[np.ndarray, str]:
    """Generate field-normalized posterior vectors from a saved V10.3 model."""
    indices = sample_indices(model["alpha_draws"].shape[0], posterior_draws, seed)
    alpha = model["alpha_draws"][indices]
    beta_global = model["beta_global_draws"][indices]
    try:
        course_position = model["courses"].index(frozen.racecourse)
        beta = model["beta_course_draws"][indices, course_position]
        course_status = "modelled_course"
    except ValueError:
        beta = beta_global
        course_status = "unseen_course_global_partial_pooling"
    vectors = np.vstack([
        stable_softmax(alpha[draw] * np.log(np.clip(frozen.baseline, EPSILON, 1.0)) + beta[draw] * frozen.component_delta)
        for draw in range(len(indices))
    ])
    if float(np.max(np.abs(np.sum(vectors, axis=1) - 1.0))) > 1e-10:
        raise BayesianCalibrationError("posterior draws 未保持場內機率守恆。")
    return vectors, course_status


def build_unavailable_sidecar(prediction_path: Path, output_path: Path, status: str, reason: str) -> dict[str, Any]:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at_hkt": hkt_timestamp(),
        "bayesian_status": status,
        "reason": reason,
        "source_prediction_path": str(prediction_path),
        "source_prediction_sha256": sha256_file(prediction_path) if prediction_path.exists() else None,
        "formal_probability_replacement": False,
        "v102_probability_contract": "V10.2 predicted_win_probability, rank, EV and Kelly are not modified.",
        "rows": [],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def overlay_prediction(
    model_path: Path,
    prediction_path: Path,
    output_path: Path,
    posterior_draws: int,
    seed: int,
    race_date_override: str | None = None,
    racecourse_override: str | None = None,
    race_no_override: int | None = None,
) -> dict[str, Any]:
    if not prediction_path.exists():
        raise BayesianCalibrationError(f"找不到 V10.2 預測 JSON：{prediction_path}")
    if not model_path.exists():
        return build_unavailable_sidecar(prediction_path, output_path, "unavailable_model_artifact", "尚未完成離線 Bayesian fit；V10.2 正式預測維持不變。")
    model = load_model(model_path)
    source = json.loads(prediction_path.read_text(encoding="utf-8"))
    rows = source.get("predictions")
    if not isinstance(rows, list) or not rows:
        return build_unavailable_sidecar(prediction_path, output_path, "unavailable_invalid_prediction", "V10.2 prediction JSON 缺少 predictions rows。")
    race_metadata = dict(source.get("race") or {})
    if race_date_override:
        race_metadata["race_date"] = race_date_override
    if racecourse_override:
        race_metadata["racecourse"] = racecourse_override
    if race_no_override is not None:
        race_metadata["race_no"] = race_no_override
    # Live V10.2 JSON keeps race identity at top level, whereas offline CSV keeps
    # it per row.  Make an in-memory copy only; the original prediction JSON is
    # never rewritten or supplemented with posterior fields.
    rows_with_race_identity: list[dict[str, Any]] = []
    for row in rows:
        enriched = dict(row)
        enriched.setdefault("race_date", race_metadata.get("race_date") or race_metadata.get("meeting_date"))
        enriched.setdefault("racecourse", race_metadata.get("racecourse"))
        enriched.setdefault("race_no", race_metadata.get("race_no"))
        rows_with_race_identity.append(enriched)
    try:
        frozen = build_frozen_race(rows_with_race_identity, require_target=False)
    except BayesianCalibrationError as exc:
        return build_unavailable_sidecar(prediction_path, output_path, "unavailable_input_contract", str(exc))

    vectors, course_status = posterior_draw_vectors(model, frozen, posterior_draws, seed)
    conservation_error = float(np.max(np.abs(np.sum(vectors, axis=1) - 1.0)))
    indices = sample_indices(model["alpha_draws"].shape[0], posterior_draws, seed)
    beta_global = model["beta_global_draws"][indices]
    try:
        course_position = model["courses"].index(frozen.racecourse)
        beta = model["beta_course_draws"][indices, course_position]
    except ValueError:
        beta = beta_global
    baseline_top = int(np.argmax(frozen.baseline))
    stability = float(np.mean(np.argmax(vectors, axis=1) == baseline_top))
    entropies = -np.sum(vectors * np.log(np.clip(vectors, EPSILON, 1.0)), axis=1) / math.log(len(rows))
    posterior_mean = np.mean(vectors, axis=0)
    p05 = quantile(vectors, 0.05)
    p95 = quantile(vectors, 0.95)
    component_effect = beta[:, None] * frozen.component_delta[None, :]
    output_rows = []
    for index, raw in enumerate(rows):
        output_rows.append({
            "horse_no": raw.get("horse_no"),
            "horse_name": frozen.horse_names[index],
            "v102_predicted_win_probability": float(frozen.baseline[index]),
            "posterior_win_mean": float(posterior_mean[index]),
            "posterior_win_p05": float(p05[index]),
            "posterior_win_p95": float(p95[index]),
            "posterior_component_disagreement": float(np.mean(np.abs(component_effect[:, index]))),
            "posterior_component_effect_p05": float(quantile(component_effect[:, index], 0.05)),
            "posterior_component_effect_p95": float(quantile(component_effect[:, index], 0.95)),
        })
    model_sha = sha256_file(model_path)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at_hkt": hkt_timestamp(),
        "bayesian_status": "available_research_only",
        "method": model["metadata"]["method"],
        "source_prediction_path": str(prediction_path),
        "source_prediction_sha256": sha256_file(prediction_path),
        "model_path": str(model_path),
        "model_sha256": model_sha,
        "formal_probability_replacement": False,
        "v102_probability_contract": "V10.2 predicted_win_probability, rank, EV and Kelly are not modified.",
        "race": race_metadata,
        "racecourse_partial_pooling_status": course_status,
        "posterior_draws": int(vectors.shape[0]),
        "posterior_draw_seed": seed,
        "parameter_posterior": model["metadata"]["parameter_summary"],
        "top1_v102_horse_name": frozen.horse_names[baseline_top],
        "top1_rank_stability": stability,
        "posterior_entropy_mean": float(np.mean(entropies)),
        "posterior_entropy_p05": float(np.quantile(entropies, 0.05)),
        "posterior_entropy_p95": float(np.quantile(entropies, 0.95)),
        "probability_sum_max_abs_error": conservation_error,
        "rows": output_rows,
        "notice": "Posterior 分位數是邊際摘要，彼此不須橫向相加為 1。每一個 posterior draw 均已檢查場內機率和為 1。此 sidecar 只作風險披露，不得取代 V10.2 排序或機率。",
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def fit_command(args: argparse.Namespace) -> dict[str, Any]:
    input_path = Path(args.predictions).resolve()
    provenance = validate_cohort_provenance(
        Path(args.cohort_manifest).resolve(), input_path, Path(args.base_model).resolve(),
    )
    if args.max_train_races is not None:
        raise BayesianCalibrationError("已驗證 cohort fit 不允許 --max-train-races；必須使用 manifest 登記的完整同模型資料。")
    races, exclusions = load_frozen_csv(input_path, require_target=True)
    if exclusions:
        raise BayesianCalibrationError(f"canonical cohort CSV 含被排除賽事：{dict(exclusions)}")
    if len(races) != int(provenance["canonical_training_race_count"]):
        raise BayesianCalibrationError("canonical cohort CSV 的可用賽事數與 manifest 登記計數不一致。")
    metadata = fit_model(
        races=races,
        output_path=Path(args.output_model),
        input_hash=sha256_file(input_path),
        advi_steps=args.advi_steps,
        posterior_draws=args.posterior_draws,
        seed=args.seed,
        cohort_provenance=provenance,
    )
    return {"status": "fit_complete_research_only", "model": metadata, "cohort_provenance": provenance, "exclusions": dict(exclusions)}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="V10.3 NumPyro Bayesian calibration / uncertainty overlay (research only)")
    commands = parser.add_subparsers(dest="command", required=True)
    fit = commands.add_parser("fit", help="Offline fit from frozen settled V10.2 prediction CSV.")
    fit.add_argument("--predictions", required=True, help="Manifest 登記的 immutable unseen cohort canonical CSV；不可使用任意歷史 CSV。")
    fit.add_argument("--cohort-manifest", required=True, help="collect_v103_unseen_cohort.py 寫出的 manifest_latest.json。")
    fit.add_argument("--base-model", required=True, help="產生 cohort 的 horse_model.pkl；會即時計算完整 SHA-256。")
    fit.add_argument("--output-model", default=DEFAULT_MODEL, help="Output compressed posterior draw artifact (.npz).")
    fit.add_argument("--advi-steps", type=int, default=10_000, help="NumPyro AutoNormal SVI optimization steps.")
    fit.add_argument("--posterior-draws", type=int, default=400, help="Saved posterior draws.")
    fit.add_argument("--max-train-races", type=int, help="保留作相容性提示；經驗證 cohort fit 會拒絕子樣本，以維持 manifest 可追溯性。")
    fit.add_argument("--seed", type=int, default=10301)
    predict = commands.add_parser("predict", help="Produce a parallel uncertainty sidecar from a frozen V10.2 prediction JSON.")
    predict.add_argument("--model", default=DEFAULT_MODEL, help="Fitted V10.3 posterior model artifact.")
    predict.add_argument("--prediction", required=True, help="V10.2 prediction.json; never modified in place.")
    predict.add_argument("--output", required=True, help="V10.3 uncertainty sidecar JSON.")
    predict.add_argument("--posterior-draws", type=int, default=200, help="Posterior draws used for the sidecar.")
    predict.add_argument("--seed", type=int, default=10301)
    predict.add_argument("--race-date", help="官方排程已知時的 YYYY-MM-DD 賽日覆寫；不修改原始 V10.2 JSON。")
    predict.add_argument("--racecourse", help="官方排程已知時的 ST/HV 馬場覆寫。")
    predict.add_argument("--race-no", type=int, help="官方排程已知時的場次覆寫。")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "fit":
            result = fit_command(args)
        else:
            result = overlay_prediction(
                Path(args.model), Path(args.prediction), Path(args.output), args.posterior_draws, args.seed,
                args.race_date, args.racecourse, args.race_no,
            )
    except (BayesianCalibrationError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "input_error", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
