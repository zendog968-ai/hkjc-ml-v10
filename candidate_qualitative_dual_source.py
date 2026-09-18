#!/usr/bin/env python3
"""Candidate-only dual-source qualitative resolver.

Official HKJC source is preferred when a validated payload is supplied. The ONCC
fallback reuses the isolated text parser and never writes to V10/N6 inputs.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

NULL_QUAL = {
    "morning_trackwork": None,
    "expert_tips": None,
    "barrier_trial": None,
    "stable_intel": None,
}


def _empty(source: str = "none") -> dict[str, Any]:
    return {"source": source, "fields": copy.deepcopy(NULL_QUAL), "status": "not_available"}


def _normalise_official(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return _empty("official_invalid")
    # Only accept an explicitly validated runner object; no heuristic inference.
    required = {"horse_no", "horse_name"}
    if not required.issubset(payload):
        return _empty("official_invalid")
    fields = {key: payload.get(key) for key in NULL_QUAL}
    if not any(value is not None for value in fields.values()):
        return _empty("official_missing")
    return {"source": "official_hkjc", "fields": fields, "status": "ok"}


def resolve_qualitative(record: dict[str, Any], official_payload: Any = None, oncc_html: str | None = None) -> dict[str, Any]:
    """Return a display/research-only object; never mutate record or call N6."""
    official = _normalise_official(official_payload)
    if official["status"] == "ok":
        return official
    if oncc_html:
        # Import the already isolated parser only at runtime to keep this module portable.
        try:
            from sync_racing_pipeline_candidate import parse_qualitative_from_html
            parsed = parse_qualitative_from_html(oncc_html, int(record["horse_no"]), str(record.get("horse_name", "")))
            if any(value is not None for value in parsed.values()):
                return {"source": "oncc_fallback", "fields": parsed, "status": "ok"}
        except (ImportError, KeyError, TypeError, ValueError):
            pass
    return _empty("none")


def attach_dual_source(records: list[dict[str, Any]], official_by_key: dict[str, Any] | None = None, oncc_by_key: dict[str, str] | None = None) -> list[dict[str, Any]]:
    output = []
    official_by_key = official_by_key or {}
    oncc_by_key = oncc_by_key or {}
    for record in records:
        item = copy.deepcopy(record)
        key = f"{record.get('race_no')}:{record.get('horse_no')}"
        item["qualitative_intel"] = resolve_qualitative(item, official_by_key.get(key), oncc_by_key.get(key))
        output.append(item)
    return output


if __name__ == "__main__":
    print(json.dumps(_empty(), ensure_ascii=False))
