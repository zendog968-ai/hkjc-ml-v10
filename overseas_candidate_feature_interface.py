#!/usr/bin/env python3
"""Candidate-only overseas feature interface.

This module is deliberately independent from N6, V10.2, and all production
prediction paths.  It defines *optional* pre-race fields for future overseas
research.  Missing values stay missing and availability flags remain zero;
there is no imputation from post-race data.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FEATURE_INTERFACE_VERSION = "overseas_candidate_features_v1"
OVERSEAS_N6_STATUS = "disabled_non_hk"

OPTIONAL_FEATURES = (
    "sectional_early_pace_rating_pre",
    "sectional_mid_race_pace_rating_pre",
    "sectional_final_600_seconds_pre",
    "sectional_final_400_seconds_pre",
    "sectional_source_quality_pre",
    "jockey_elo_pre",
    "trainer_elo_pre",
)

AVAILABILITY_FLAGS = tuple(f"{feature}_available" for feature in OPTIONAL_FEATURES)


class CandidateFeatureContractError(ValueError):
    """Raised when a candidate feature would violate the pre-race contract."""


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise CandidateFeatureContractError("UTC timestamp must carry an explicit offset")
    return parsed.astimezone(timezone.utc)


def source_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def feature_contract_sha256() -> str:
    """Return the source hash that identifies this candidate feature contract."""
    return source_hash(Path(__file__).resolve())


def _optional_float(value: Any, field: str) -> tuple[float | None, float]:
    """Return a finite numeric value and an availability flag without imputation."""
    if value in (None, ""):
        return None, 0.0
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise CandidateFeatureContractError(f"{field} must be numeric or null") from exc
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        raise CandidateFeatureContractError(f"{field} must be finite")
    return parsed, 1.0


def build_optional_feature_row(
    *,
    runner_no: int,
    horse_name: str,
    scheduled_start_utc: str,
    source_captured_at_utc: str,
    source_path: str | Path | None,
    sectional: dict[str, Any] | None = None,
    jockey_elo: Any = None,
    trainer_elo: Any = None,
) -> dict[str, Any]:
    """Build a pre-race candidate row while rejecting post-start source data.

    ``sectional`` may contain early/mid pace ratings and final sectionals only
    when its source was captured before the declared start.  This function
    creates no estimate when an optional source is unavailable.
    """
    if not isinstance(runner_no, int) or runner_no < 1:
        raise CandidateFeatureContractError("runner_no must be a positive integer")
    if not isinstance(horse_name, str) or not horse_name.strip():
        raise CandidateFeatureContractError("horse_name is required")
    scheduled = parse_utc(scheduled_start_utc)
    captured = parse_utc(source_captured_at_utc)
    if captured >= scheduled:
        raise CandidateFeatureContractError("candidate feature source is not strictly pre-race")
    source_file = Path(source_path) if source_path else None
    if source_file is not None and not source_file.is_file():
        raise CandidateFeatureContractError("candidate feature source file is missing")

    sectional = sectional or {}
    raw = {
        "sectional_early_pace_rating_pre": sectional.get("early_pace_rating"),
        "sectional_mid_race_pace_rating_pre": sectional.get("mid_race_pace_rating"),
        "sectional_final_600_seconds_pre": sectional.get("final_600_seconds"),
        "sectional_final_400_seconds_pre": sectional.get("final_400_seconds"),
        "sectional_source_quality_pre": sectional.get("source_quality"),
        "jockey_elo_pre": jockey_elo,
        "trainer_elo_pre": trainer_elo,
    }
    row: dict[str, Any] = {
        "feature_interface_version": FEATURE_INTERFACE_VERSION,
        "feature_contract_sha256": feature_contract_sha256(),
        "n6_status": OVERSEAS_N6_STATUS,
        "runner_no": runner_no,
        "horse_name": horse_name.strip(),
        "scheduled_start_utc": scheduled.isoformat(timespec="seconds"),
        "source_captured_at_utc": captured.isoformat(timespec="seconds"),
        "source_path": str(source_file) if source_file else None,
        "source_sha256": source_hash(source_file) if source_file else None,
    }
    for field, value in raw.items():
        parsed, available = _optional_float(value, field)
        row[field] = parsed
        row[f"{field}_available"] = available
    return row


def attach_candidate_feature_block(
    decision: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a candidate-only annex; never alters a sealed decision in place."""
    if decision.get("n6_status") != OVERSEAS_N6_STATUS:
        raise CandidateFeatureContractError("overseas candidate block requires disabled_non_hk")
    return {
        "schema_version": "overseas_candidate_feature_annex_v1",
        "feature_interface_version": FEATURE_INTERFACE_VERSION,
        "feature_contract_sha256": feature_contract_sha256(),
        "decision_reference": {
            "event_key": decision.get("race", {}).get("event_key"),
            "captured_at_utc": decision.get("captured_at_utc"),
            "scheduled_start_utc": decision.get("scheduled_start_utc"),
        },
        "n6_status": OVERSEAS_N6_STATUS,
        "training_status": "not_applicable_feature_annex_only",
        "rows": rows,
    }


def write_feature_annex(path: Path, payload: dict[str, Any]) -> str:
    """Write an append-only candidate annex; refuse overwrites."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"candidate feature annex already exists: {path}")
    body = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with path.open("xb") as handle:
        handle.write(body)
        handle.flush()
    return hashlib.sha256(body).hexdigest()


@dataclass(frozen=True)
class CandidateFeatureAvailability:
    total_rows: int
    sectional_complete_rows: int
    jockey_elo_available_rows: int
    trainer_elo_available_rows: int


def summarize_feature_availability(rows: list[dict[str, Any]]) -> CandidateFeatureAvailability:
    """Summarise availability only; it makes no suitability or performance claim."""
    sectional_flags = (
        "sectional_early_pace_rating_pre_available",
        "sectional_mid_race_pace_rating_pre_available",
        "sectional_final_600_seconds_pre_available",
        "sectional_final_400_seconds_pre_available",
    )
    return CandidateFeatureAvailability(
        total_rows=len(rows),
        sectional_complete_rows=sum(all(row.get(flag) == 1.0 for flag in sectional_flags) for row in rows),
        jockey_elo_available_rows=sum(row.get("jockey_elo_pre_available") == 1.0 for row in rows),
        trainer_elo_available_rows=sum(row.get("trainer_elo_pre_available") == 1.0 for row in rows),
    )


if __name__ == "__main__":
    raise SystemExit("This module is an import-only candidate research interface; it never runs N6 or training.")
