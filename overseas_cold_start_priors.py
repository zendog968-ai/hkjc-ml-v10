"""Hierarchical, leakage-safe priors for S1/S2 runners without HK ELO.

Every historical aggregate has an explicit `as_of_utc` cutoff.  When a source is
absent or has too few starts, it contributes zero *relative* signal and the
runner naturally shrinks to a neutral within-field strength of 1.0.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class PriorResult:
    strength: float
    tier: str
    source: str
    confidence: float
    uncertainty: float
    detail: dict[str, Any]


def _safe_float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and math.isfinite(float(value)) else None


def _evidence(starts: float, prior_strength: float) -> float:
    return max(0.0, min(1.0, starts / (starts + prior_strength)))


def _posterior_rate(wins: float, starts: float, base_rate: float, prior_strength: float) -> float:
    base = min(max(base_rate, 1e-4), 0.999)
    return (wins + base * prior_strength) / (starts + prior_strength)


def _historical_actor_stats(conn: sqlite3.Connection, column: str, actor: str | None, as_of_utc: str) -> tuple[float, float] | None:
    if not actor:
        return None
    # Result rows are only usable if their scheduled start is known and before
    # the model generation timestamp. This prevents same-day and future leakage.
    sql = f"""
        SELECT COUNT(*) AS starts,
               COALESCE(SUM(CASE WHEN s.finish_pos=1 THEN 1 ELSE 0 END), 0) AS wins
        FROM overseas_starters AS s
        JOIN overseas_races AS r ON r.overseas_race_id=s.overseas_race_id
        WHERE s.{column}=?
          AND r.race_status='completed'
          AND r.scheduled_start_utc IS NOT NULL
          AND r.scheduled_start_utc < ?
          AND s.withdrawal_status IS NULL
          AND s.finish_pos IS NOT NULL
    """
    row = conn.execute(sql, (actor, as_of_utc)).fetchone()
    if not row or int(row[0] or 0) <= 0:
        return None
    return float(row[0]), float(row[1])


def _historical_base_rate(conn: sqlite3.Connection, as_of_utc: str, default: float) -> float:
    row = conn.execute(
        """
        SELECT COUNT(*) AS starts,
               COALESCE(SUM(CASE WHEN s.finish_pos=1 THEN 1 ELSE 0 END), 0) AS wins
        FROM overseas_starters AS s
        JOIN overseas_races AS r ON r.overseas_race_id=s.overseas_race_id
        WHERE r.race_status='completed'
          AND r.scheduled_start_utc IS NOT NULL
          AND r.scheduled_start_utc < ?
          AND s.withdrawal_status IS NULL
          AND s.finish_pos IS NOT NULL
        """,
        (as_of_utc,),
    ).fetchone()
    starts = float(row[0] or 0) if row else 0.0
    wins = float(row[1] or 0) if row else 0.0
    return wins / starts if starts >= 100 else default


def ensure_prediction_prior_columns(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(overseas_prerace_predictions)")}
    additions = {
        "prior_confidence": "REAL",
        "prior_uncertainty": "REAL",
        "prior_detail_json": "TEXT",
    }
    for name, sql_type in additions.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE overseas_prerace_predictions ADD COLUMN {name} {sql_type}")
    conn.commit()


def hierarchical_prior(conn: sqlite3.Connection, runner: dict[str, Any], field_size: int, as_of_utc: str) -> PriorResult:
    """Return a relative PL strength; all unknown runners receive strength 1.0."""
    field_base = 1.0 / max(field_size, 2)
    archive_base = _historical_base_rate(conn, as_of_utc, field_base)
    horse_starts = _safe_float(runner.get("career_starts"))
    horse_wins = _safe_float(runner.get("career_wins"))
    components: list[dict[str, Any]] = []
    weighted_log_signal = 0.0
    confidence_numer = 0.0
    confidence_denom = 0.0

    if horse_starts is not None and horse_wins is not None and horse_starts > 0 and 0 <= horse_wins <= horse_starts:
        evidence = _evidence(horse_starts, 20.0)
        posterior = _posterior_rate(horse_wins, horse_starts, field_base, 20.0)
        signal = math.log(max(posterior, 1e-5) / field_base)
        weight = 0.65
        weighted_log_signal += weight * evidence * signal
        confidence_numer += weight * evidence
        confidence_denom += weight
        components.append({"name": "horse_career", "starts": horse_starts, "wins": horse_wins, "posterior_rate": posterior, "evidence": evidence, "weight": weight})
    else:
        confidence_denom += 0.65
        components.append({"name": "horse_career", "status": "missing_or_unusable"})

    for name, column, weight, prior_n in (("trainer", "trainer", 0.20, 40.0), ("jockey", "jockey", 0.15, 30.0)):
        actor_stats = _historical_actor_stats(conn, column, runner.get(name), as_of_utc)
        confidence_denom += weight
        if actor_stats is None:
            components.append({"name": name, "status": "no_pre_cutoff_archive_history"})
            continue
        starts, wins = actor_stats
        evidence = _evidence(starts, prior_n)
        posterior = _posterior_rate(wins, starts, archive_base, prior_n)
        signal = math.log(max(posterior, 1e-5) / archive_base)
        weighted_log_signal += weight * evidence * signal
        confidence_numer += weight * evidence
        components.append({"name": name, "starts": starts, "wins": wins, "posterior_rate": posterior, "evidence": evidence, "weight": weight})

    confidence = confidence_numer / confidence_denom if confidence_denom else 0.0
    uncertainty = 1.0 - confidence
    # Uncertainty tempers, rather than amplifies, cross-jurisdiction signals.
    tempered_signal = weighted_log_signal * (0.25 + 0.75 * confidence)
    strength = float(min(max(math.exp(tempered_signal), 0.35), 2.85))
    if confidence < 0.15:
        tier = "neutral_field_prior"
    elif confidence < 0.45:
        tier = "hierarchical_low_confidence"
    elif confidence < 0.75:
        tier = "hierarchical_medium_confidence"
    else:
        tier = "hierarchical_high_confidence"
    source = "pre_cutoff_public_career_and_overseas_archive" if confidence > 0 else "neutral_field_prior_no_usable_history"
    detail = {
        "prior_version": "overseas_hierarchical_prior_v2",
        "as_of_utc": as_of_utc,
        "field_base_rate": field_base,
        "archive_base_rate": archive_base,
        "components": components,
        "tempered_log_signal": tempered_signal,
    }
    return PriorResult(strength, tier, source, confidence, uncertainty, detail)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
