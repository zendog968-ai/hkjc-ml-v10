#!/usr/bin/env python3
"""Candidate-only PP capability-prior evidence report.

This module reads only the isolated P1 candidate SQLite database.  It never
imports V10/N6, emits no HK ELO or win probability, and refuses to turn a
small overseas form sample into a Hong Kong rating.  It converts verified PP
rows into transparent evidence sufficiency and uncertainty disclosures for a
future, separately-approved offline study.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MODEL_VERSION = "candidate_pp_capability_prior_v1"
DISABLED_OUTPUTS = {
    "hong_kong_elo": None,
    "formal_win_probability": None,
    "formal_place_probability": None,
    "expected_value": None,
    "kelly_fraction": None,
}


class CandidatePriorError(ValueError):
    """Raised when a candidate-only evidence report cannot be built safely."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _record_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _within_race_finish_fraction(position: int | None, field_size: int | None) -> float | None:
    """Describe placing within its own overseas race; it is not a capability rating."""
    if position is None or field_size is None or field_size < 2 or not (1 <= position <= field_size):
        return None
    return round((field_size - position) / (field_size - 1), 6)


def build_pp_prior(db_path: Path, horse_code: str) -> dict[str, Any]:
    """Return a non-predictive PP evidence report from the P1 isolated store."""
    if not db_path.is_file():
        raise CandidatePriorError(f"candidate database does not exist: {db_path}")
    if not horse_code or not horse_code.strip():
        raise CandidatePriorError("horse_code is required")

    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        identity = conn.execute(
            """
            SELECT i.pp_identity_id, i.hk_horse_code, i.hk_horse_name,
                   i.original_horse_name, i.source_country, i.official_anchor_match,
                   i.identity_confidence, i.parse_status, s.source_url,
                   s.retrieved_at_utc, s.content_sha256, s.source_id
            FROM pp_identity_map AS i
            JOIN source_manifests AS s ON s.source_id = i.source_id
            WHERE i.hk_horse_code = ? AND i.parse_status = 'accepted_p1'
            ORDER BY i.created_at_utc
            """,
            (horse_code.strip().upper(),),
        ).fetchone()
        if identity is None:
            raise CandidatePriorError("no accepted P1 PP identity anchor exists for horse")
        forms = conn.execute(
            """
            SELECT form_date, source_country, race_distance_m, finishing_position,
                   field_size, race_class_text, source_rating_raw, source_rating_name,
                   source_sectionals_available, source_record_sha256
            FROM pp_external_form
            WHERE pp_identity_id = ?
            ORDER BY form_date, pp_form_id
            """,
            (identity["pp_identity_id"],),
        ).fetchall()

    form_rows: list[dict[str, Any]] = []
    for row in forms:
        item = dict(row)
        item["within_race_finish_fraction"] = _within_race_finish_fraction(
            item["finishing_position"], item["field_size"]
        )
        form_rows.append(item)

    rating_rows = [row for row in form_rows if row["source_rating_raw"] is not None]
    sectional_rows = [row for row in form_rows if int(row["source_sectionals_available"]) == 1]
    complete_result_rows = [
        row
        for row in form_rows
        if all(row.get(field) is not None for field in ("form_date", "source_country", "race_distance_m", "finishing_position", "field_size"))
    ]
    result_fraction_rows = [row["within_race_finish_fraction"] for row in form_rows if row["within_race_finish_fraction"] is not None]

    evidence_gaps = []
    if len(form_rows) < 3:
        evidence_gaps.append("fewer_than_three_verified_overseas_form_records")
    if not rating_rows:
        evidence_gaps.append("no_verified_foreign_rating")
    if not sectional_rows:
        evidence_gaps.append("no_verified_foreign_sectionals")
    if not any(row["race_class_text"] for row in form_rows):
        evidence_gaps.append("no_verified_race_class_text")
    evidence_gaps.extend(
        [
            "no_hong_kong_race_result_history",
            "no_target_hong_kong_race_context",
            "no_cross_jurisdiction_conversion_cohort",
        ]
    )

    available_components = {
        "identity_anchor": int(identity["official_anchor_match"]) == 1,
        "overseas_form_rows": len(form_rows),
        "complete_result_rows": len(complete_result_rows),
        "foreign_rating_rows": len(rating_rows),
        "foreign_sectional_rows": len(sectional_rows),
        "race_class_text_rows": sum(bool(row["race_class_text"]) for row in form_rows),
    }
    identity_payload = {
        "hk_horse_code": identity["hk_horse_code"],
        "hk_horse_name": identity["hk_horse_name"],
        "original_horse_name": identity["original_horse_name"],
        "source_country": identity["source_country"],
        "official_anchor_match": bool(identity["official_anchor_match"]),
        "identity_confidence": identity["identity_confidence"],
        "parse_status": identity["parse_status"],
    }
    report: dict[str, Any] = {
        "model_version": MODEL_VERSION,
        "mode": "candidate_only_read_only",
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "database_open_mode": "sqlite_ro_candidate_p1",
        "database_sha256": _sha256(db_path),
        "identity": identity_payload,
        "source_provenance": {
            "source_id": identity["source_id"],
            "source_url": identity["source_url"],
            "retrieved_at_utc": identity["retrieved_at_utc"],
            "content_sha256": identity["content_sha256"],
        },
        "verified_external_form": form_rows,
        "within_source_descriptors": {
            "form_record_count": len(form_rows),
            "complete_result_record_count": len(complete_result_rows),
            "mean_within_race_finish_fraction": (
                round(sum(result_fraction_rows) / len(result_fraction_rows), 6) if result_fraction_rows else None
            ),
            "interpretation": "Within-race finish fraction describes a verified placing in its own overseas field only; it is not a Hong Kong capability score.",
        },
        "evidence_sufficiency": available_components,
        "conversion_status": "not_estimated_insufficient_cross_jurisdiction_evidence",
        "entry_pressure_disclosure": {
            "level": "very_high_uncertainty",
            "reasons": evidence_gaps,
            "required_before_any_offline_conversion_study": [
                "verified target Hong Kong race context",
                "multiple verified overseas form records with comparable field-level metadata",
                "a separately curated and time-ordered cross-jurisdiction validation cohort",
                "independent approval for offline candidate-only evaluation",
            ],
        },
        "explicitly_not_produced": DISABLED_OUTPUTS,
        "v10_action": "not_touched",
        "n6_action": "not_imported_not_touched",
        "network_requests": 0,
        "training_status": "not_trained_default_deny",
    }
    report["report_sha256_excluding_self"] = _record_hash(report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a read-only overseas PP evidence prior; never trains or predicts.")
    parser.add_argument("--db", default="runtime/preseason_candidate/p1_preseason_candidate.sqlite")
    parser.add_argument("--horse-code", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    if output.exists():
        raise SystemExit(f"refusing to overwrite candidate report: {output}")
    payload = build_pp_prior(Path(args.db), args.horse_code)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": "candidate_report_written", "output": str(output), "conversion_status": payload["conversion_status"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
