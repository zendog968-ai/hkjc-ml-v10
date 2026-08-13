#!/usr/bin/env python3
"""HKJC equipment-code parsing and pre-race comparison helpers.

The official form guide uses equipment codes such as TT1, BO/TT, BO-/TT and --.
A suffix of 1 means first use, 2 means re-applied, and - means removed for that run.
This module compares active base equipment only; it retains the raw official string for audit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

# Longest alternatives first, to avoid interpreting BO as B + O or CP as C + P.
KNOWN_CODES = ("BO", "VO", "PC", "PS", "CP", "CO", "TT", "XB", "CC", "SB", "SR", "BL", "BR", "BK", "B", "V", "P", "H", "E")
TOKEN_RE = re.compile(r"(" + "|".join(KNOWN_CODES) + r")([12-])?")
BLINKER_CODES = {"B", "BO"}
EMPTY_MARKERS = {"", "--", "-", "—", "N/A", "NA"}


@dataclass(frozen=True)
class EquipmentSnapshot:
    raw: str | None
    active_codes: frozenset[str]
    first_time_codes: frozenset[str]


def normalize_equipment(value: object) -> str | None:
    if value is None:
        return None
    raw = str(value).replace("\xa0", " ").strip().upper()
    raw = re.sub(r"\s+", "", raw)
    return None if raw in EMPTY_MARKERS else raw


def parse_equipment(value: object) -> EquipmentSnapshot:
    raw = normalize_equipment(value)
    if raw is None:
        return EquipmentSnapshot(None, frozenset(), frozenset())
    active: set[str] = set()
    first_time: set[str] = set()
    for code, marker in TOKEN_RE.findall(raw):
        # A trailing minus denotes removal; it must not be treated as active gear.
        if marker != "-":
            active.add(code)
        if marker == "1":
            first_time.add(code)
    return EquipmentSnapshot(raw, frozenset(active), frozenset(first_time))


def equipment_feature_flags(current: object, previous: object, previous_known: bool) -> dict[str, int]:
    """Produce one-hot features without inferring a change when previous gear is unknown."""
    now = parse_equipment(current)
    prior = parse_equipment(previous)
    known = bool(previous_known and normalize_equipment(current) is not None)
    changed = int(known and now.active_codes != prior.active_codes)
    added = int(known and bool(now.active_codes - prior.active_codes))
    return {
        "is_first_time_blinker": int(bool(now.first_time_codes & BLINKER_CODES)),
        "is_equip_added": added,
        "equipment_changed": changed,
        "equipment_history_known_pre": int(known),
    }


def serialize_codes(codes: Iterable[str]) -> str:
    return "/".join(sorted(set(codes)))
