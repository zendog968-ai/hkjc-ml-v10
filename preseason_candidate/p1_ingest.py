#!/usr/bin/env python3
"""P1 offline-only PP and barrier-trial candidate ingestion.

The program consumes an explicit archive manifest. It does not issue HTTP requests,
does not open the production V10 SQLite database, and does not import N6. A separate
manual capture helper is required before this parser can see any new official source.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

PARSER_VERSION = "preseason_p1_ingest_v4"
SCHEMA_VERSION = "n6_preseason_candidate_p1_v1"


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(encoded)


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def parse_trial_seconds(value: str) -> float:
    matched = re.fullmatch(r"(\d+)\.(\d{2})\.(\d{2})", value.strip())
    if not matched:
        raise ValueError(f"Unsupported official trial time: {value!r}")
    minutes, seconds, hundredths = matched.groups()
    return int(minutes) * 60 + int(seconds) + int(hundredths) / 100.0


def compact_text(value: str) -> str:
    return " ".join(value.split())


def load_archive(source: dict[str, Any], project_root: Path) -> tuple[Path, str, str]:
    relative = source["local_relative_path"]
    path = Path(relative)
    if not path.is_absolute():
        path = (project_root / path).resolve()
    raw = path.read_bytes()
    actual_sha = sha256_bytes(raw)
    expected_sha = source["content_sha256"].lower()
    if actual_sha != expected_sha:
        raise ValueError(f"source hash mismatch for {relative}: expected {expected_sha}, got {actual_sha}")
    return path, raw.decode("utf-8", errors="replace"), actual_sha


def validate_manifest(manifest: dict[str, Any], project_root: Path) -> list[dict[str, Any]]:
    if manifest.get("mode") != "offline_archive_only":
        raise ValueError("P1 ingest accepts only mode=offline_archive_only")
    if manifest.get("production_write_forbidden") is not True or manifest.get("n6_service_integration") != "forbidden":
        raise ValueError("P1 manifest must explicitly forbid production writes and N6 integration")
    as_of = parse_utc(manifest["as_of_utc"])
    sources = manifest.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("P1 manifest must contain at least one archived source")
    validated: list[dict[str, Any]] = []
    for raw_source in sources:
        required = {"source_kind", "source_url", "retrieved_at_utc", "local_relative_path", "content_sha256"}
        missing = sorted(required - raw_source.keys())
        if missing:
            raise ValueError(f"source manifest missing keys: {missing}")
        if raw_source["source_kind"] not in {"hkjc_pp_list", "hkjc_pp_form", "hkjc_barrier_trial"}:
            raise ValueError(f"unsupported P1 source kind: {raw_source['source_kind']}")
        if parse_utc(raw_source["retrieved_at_utc"]) >= as_of:
            raise ValueError("P1 source capture must be strictly earlier than candidate as_of_utc")
        path, text, actual_sha = load_archive(raw_source, project_root)
        source = dict(raw_source)
        source["resolved_path"] = str(path)
        source["text"] = text
        source["actual_sha"] = actual_sha
        validated.append(source)
    return validated


def decode_pp_parsing_copy(raw: str) -> str:
    # The saved bytes remain the source of truth. This copy only removes JSON
    # presentation escapes present in the official response payload.
    return raw.replace("\\n", " ").replace("\\t", " ").translate({92: None})


def parse_pp_form_note(note: str, identity_country: str) -> dict[str, Any] | None:
    # Official PP notes vary by jurisdiction: the text between date and distance
    # can contain track, state, and local grade abbreviations. Identity country is
    # anchored separately from the official former-name field, so do not infer it
    # from this variable text.
    matched = re.search(
        r"(?P<date>\d{1,2}\.\d{1,2}\.\d{4}),?\s*"
        r"(?P<class>.*?)\s+(?P<distance>\d+)M\s+"
        r"(?P<position>\d+)\s*(?:st|nd|rd|th)\s*/\s*(?P<field>\d+)",
        note,
        re.IGNORECASE,
    )
    if not matched:
        return None
    groups = matched.groupdict()
    race_class = compact_text(groups["class"])
    if not race_class:
        return None
    return {
        "form_date": groups["date"],
        "source_country": identity_country,
        "race_distance_m": int(groups["distance"]),
        "finishing_position": int(groups["position"]),
        "field_size": int(groups["field"]),
        "race_class_text": race_class,
    }


def parse_pp_source(raw: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    text = decode_pp_parsing_copy(raw)
    anchors = list(re.finditer(r'"brandNo":\{"value":"(?P<code>[A-Z]\d{3})"\}', text))
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for index, anchor in enumerate(anchors):
        segment = text[anchor.start() : anchors[index + 1].start() if index + 1 < len(anchors) else anchor.start() + 6000]
        code = anchor.group("code")
        def field(label: str) -> str | None:
            match = re.search(rf'"{label}":\{{"value":"([^"]*)"\}}', segment)
            return match.group(1).strip() if match else None
        hk_name = field("hKName")
        former = field("formerName")
        note = field("notes")
        former_match = re.fullmatch(r"(.+?)\s*\(([A-Z]{2,3})\)", former or "")
        if not hk_name or not former_match:
            rejected.append({
                "entity_kind": "pp_identity", "entity_anchor": code,
                "reason_code": "identity_anchor_incomplete_or_ambiguous",
                "detail": {"has_hk_name": bool(hk_name), "former_name_raw": former},
            })
            continue
        original_horse_name, source_country = former_match.groups()
        record = {
            "hk_horse_code": code,
            "hk_horse_name": hk_name,
            "original_horse_name": original_horse_name.strip(),
            "source_country": source_country,
            "official_anchor_match": 1,
            "identity_confidence": 1.0,
            "form": parse_pp_form_note(note or "", source_country),
            "note_raw": note or "",
        }
        accepted.append(record)
        if record["form"] is None:
            rejected.append({
                "entity_kind": "pp_form", "entity_anchor": code,
                "reason_code": "official_note_not_in_recognised_date_class_country_distance_result_format",
                "detail": {"note_raw": note or ""},
            })
    if not anchors:
        raise ValueError("no official PP brandNo anchors found in archived source")
    return accepted, rejected


def parse_lbw(raw: str, rank: int) -> float | None:
    value = compact_text(raw)
    if not value:
        return 0.0 if rank == 1 else None
    match = re.fullmatch(r"(?P<whole>\d+)(?:-(?P<numerator>\d+)\/(?P<denominator>\d+))?L", value)
    if not match:
        return None
    whole = float(match.group("whole"))
    if match.group("numerator") is None:
        return whole
    return whole + int(match.group("numerator")) / int(match.group("denominator"))


def parse_positions(raw: str) -> list[int] | None:
    values = compact_text(raw).split()
    if not values or not all(item.isdigit() for item in values):
        return None
    return [int(item) for item in values]


def parse_trial_source(raw: str, trial_date: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    soup = BeautifulSoup(raw, "html.parser")
    tables = soup.find_all("table")
    meta_indices: list[tuple[int, re.Match[str]]] = []
    for index, table in enumerate(tables):
        table_text = compact_text(table.get_text(" ", strip=True))
        matched = re.search(
            r"Batch\s+(?P<batch>\d+)\s+-\s+(?P<venue>SHA TIN|HAPPY VALLEY)\s+"
            r"(?P<surface>[A-Z ]+?)\s+-\s+(?P<distance>\d+)m",
            table_text,
        )
        if matched:
            meta_indices.append((index, matched))
    if not meta_indices:
        raise ValueError("no HKJC barrier-trial batch metadata tables found in archived source")

    batches: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for meta_offset, (table_index, header_match) in enumerate(meta_indices):
        meta_table = tables[table_index]
        metadata_text = compact_text(meta_table.get_text(" ", strip=True))
        going_match = re.search(r"Going:\s*(?P<going>[A-Z ]+?)(?=\s+Time:)", metadata_text)
        time_match = re.search(r"Time:\s*(?P<time>\d+\.\d{2}\.\d{2})", metadata_text)
        sectional_match = re.search(r"Sectional Time:\s*(?P<sectionals>(?:\d+(?:\.\d+)?\s*)+)", metadata_text)
        label = f"Batch {header_match.group('batch')}"
        if not (going_match and time_match and sectional_match):
            rejected.append({
                "entity_kind": "trial_batch", "entity_anchor": label,
                "reason_code": "metadata_missing_going_time_or_sectionals",
                "detail": {"metadata_text": metadata_text},
            })
            continue
        try:
            batch_time = parse_trial_seconds(time_match.group("time"))
            sectionals = [float(value) for value in sectional_match.group("sectionals").split()]
        except ValueError as error:
            rejected.append({
                "entity_kind": "trial_batch", "entity_anchor": label,
                "reason_code": "metadata_time_or_sectionals_unparseable",
                "detail": {"error": str(error)},
            })
            continue
        search_end = meta_indices[meta_offset + 1][0] if meta_offset + 1 < len(meta_indices) else len(tables)
        entries_table = None
        for candidate in tables[table_index + 1 : search_end]:
            rows = candidate.find_all("tr")
            if not rows:
                continue
            headers = [compact_text(cell.get_text(" ", strip=True)) for cell in rows[0].find_all(["th", "td"])]
            required = {"Horse", "Jockey", "Trainer", "Draw", "Gear", "LBW", "Running Position", "Time", "Result", "Comment"}
            if required.issubset(headers):
                entries_table = candidate
                break
        if entries_table is None:
            rejected.append({
                "entity_kind": "trial_batch", "entity_anchor": label,
                "reason_code": "following_runner_table_not_found",
                "detail": {},
            })
            continue
        rows = entries_table.find_all("tr")
        headers = [compact_text(cell.get_text(" ", strip=True)) for cell in rows[0].find_all(["th", "td"])]
        header_index = {name: headers.index(name) for name in headers}
        entries: list[dict[str, Any]] = []
        for ordinal, row in enumerate(rows[1:], start=1):
            cells = [compact_text(cell.get_text(" ", strip=True)) for cell in row.find_all(["th", "td"])]
            if len(cells) != len(headers):
                rejected.append({
                    "entity_kind": "trial_entry", "entity_anchor": f"{label}:row:{ordinal}",
                    "reason_code": "runner_row_column_count_mismatch",
                    "detail": {"column_count": len(cells), "expected": len(headers)},
                })
                continue
            horse_raw = cells[header_index["Horse"]]
            horse_match = re.fullmatch(r"(?P<name>.+?)\s*\((?P<code>[A-Z]\d{3})\)", horse_raw)
            positions = parse_positions(cells[header_index["Running Position"]])
            time_raw = cells[header_index["Time"]]
            try:
                finish_time = parse_trial_seconds(time_raw)
            except ValueError:
                finish_time = None
            if not horse_match or positions is None or finish_time is None:
                rejected.append({
                    "entity_kind": "trial_entry", "entity_anchor": f"{label}:{horse_raw or ordinal}",
                    "reason_code": "runner_identity_positions_or_time_unparseable",
                    "detail": {"horse_raw": horse_raw, "positions_raw": cells[header_index['Running Position']], "time_raw": time_raw},
                })
                continue
            entries.append({
                "horse_code": horse_match.group("code"),
                "horse_name": horse_match.group("name").strip(),
                "jockey": cells[header_index["Jockey"]] or None,
                "trainer": cells[header_index["Trainer"]] or None,
                "draw": int(cells[header_index["Draw"]]) if cells[header_index["Draw"]].isdigit() else None,
                "gear": cells[header_index["Gear"]] or None,
                "lbw_raw": cells[header_index["LBW"]] or None,
                "lbw": parse_lbw(cells[header_index["LBW"]], ordinal),
                "running_positions": positions,
                "finish_rank": ordinal,
                "finish_time_seconds": finish_time,
                "time_vs_batch_seconds": round(finish_time - batch_time, 4),
                "final_sectional_vs_batch_seconds": None,
                "position_gain": positions[0] - positions[-1],
                "comment": cells[header_index["Comment"]] or None,
            })
        if not entries:
            rejected.append({
                "entity_kind": "trial_batch", "entity_anchor": label,
                "reason_code": "no_structurally_accepted_runner_rows",
                "detail": {},
            })
            continue
        field_size = len(entries)
        for entry in entries:
            entry["finish_rank_pct"] = 1.0 if field_size == 1 else round(1 - (entry["finish_rank"] - 1) / (field_size - 1), 6)
        batches.append({
            "trial_date": trial_date,
            "venue": header_match.group("venue"),
            "surface": compact_text(header_match.group("surface")),
            "distance_m": int(header_match.group("distance")),
            "going": compact_text(going_match.group("going")),
            "batch_label": label,
            "batch_time_seconds": batch_time,
            "sectionals": sectionals,
            "entries": entries,
        })
    return batches, rejected


def init_db(db_path: Path, schema_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(schema_path.read_text(encoding="utf-8"))
    return connection


def insert_immutable(connection: sqlite3.Connection, table: str, key_field: str, values: dict[str, Any]) -> None:
    columns = list(values)
    try:
        connection.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            [values[column] for column in columns],
        )
    except sqlite3.IntegrityError as error:
        existing = connection.execute(f"SELECT * FROM {table} WHERE {key_field} = ?", (values[key_field],)).fetchone()
        if existing is None:
            raise RuntimeError(f"immutable insert failed for {table}/{values[key_field]}: {error}") from error
        names = [column[0] for column in connection.execute(f"SELECT * FROM {table} LIMIT 0").description]
        existing_values = dict(zip(names, existing))
        mismatches = {
            key: {"existing": existing_values.get(key), "incoming": value}
            for key, value in values.items()
            if key != "created_at_utc" and existing_values.get(key) != value
        }
        if mismatches:
            raise RuntimeError(f"immutable hash/key conflict for {table}/{values[key_field]}: {mismatches}")


def save_rejection(connection: sqlite3.Connection, run_id: str, source_id: str | None, rejection: dict[str, Any], created: str) -> None:
    payload = {
        "run": run_id, "source": source_id, "kind": rejection["entity_kind"],
        "anchor": rejection["entity_anchor"], "code": rejection["reason_code"], "detail": rejection["detail"],
    }
    rejection_id = stable_hash(payload)
    insert_immutable(connection, "ingest_rejections", "rejection_id", {
        "rejection_id": rejection_id,
        "ingest_run_id": run_id,
        "source_id": source_id,
        "entity_kind": rejection["entity_kind"],
        "entity_anchor": rejection["entity_anchor"],
        "reason_code": rejection["reason_code"],
        "detail_json": json.dumps(rejection["detail"], ensure_ascii=False, sort_keys=True),
        "created_at_utc": created,
    })


def ingest(args: argparse.Namespace) -> dict[str, Any]:
    manifest_path = Path(args.manifest).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    project_root = Path(args.project_root).resolve()
    sources = validate_manifest(manifest, project_root)
    pp_sources = [source for source in sources if source["source_kind"] in {"hkjc_pp_list", "hkjc_pp_form"}]
    trial_sources = [source for source in sources if source["source_kind"] == "hkjc_barrier_trial"]
    parsed_pp: list[tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]] = []
    parsed_trials: list[tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]] = []
    all_rejections: list[dict[str, Any]] = []
    for source in pp_sources:
        identities, rejections = parse_pp_source(source["text"])
        parsed_pp.append((source, identities, rejections))
        all_rejections.extend(rejections)
    for source in trial_sources:
        date_match = re.search(r"Date=(\d{2})/(\d{2})/(\d{4})", source["source_url"])
        if not date_match:
            raise ValueError("trial source URL must contain Date=DD/MM/YYYY for candidate temporal provenance")
        day, month, year = date_match.groups()
        trial_date = f"{year}-{month}-{day}"
        batches, rejections = parse_trial_source(source["text"], trial_date)
        parsed_trials.append((source, batches, rejections))
        all_rejections.extend(rejections)

    source_summary = [{key: source[key] for key in ("source_kind", "source_url", "retrieved_at_utc", "content_sha256", "local_relative_path")} for source in sources]
    run_payload = {"parser_version": PARSER_VERSION, "schema_version": SCHEMA_VERSION, "as_of_utc": manifest["as_of_utc"], "sources": source_summary}
    run_sha = stable_hash(run_payload)
    run_id = stable_hash({"p1_ingest_run": run_sha})
    created = utc_now()
    db_path = Path(args.db).resolve()
    connection = init_db(db_path, Path(args.schema).resolve())
    try:
        status = "partial" if all_rejections else "accepted"
        insert_immutable(connection, "p1_ingest_runs", "ingest_run_id", {
            "ingest_run_id": run_id, "parser_version": PARSER_VERSION, "candidate_schema_version": SCHEMA_VERSION,
            "mode": manifest["mode"], "as_of_utc": manifest["as_of_utc"], "pp_source_count": len(pp_sources),
            "trial_source_count": len(trial_sources), "status": status, "run_sha256": run_sha, "created_at_utc": created,
        })
        source_ids: dict[str, str] = {}
        for source in sources:
            source_id = stable_hash({"ingest_run_id": run_id, "kind": source["source_kind"], "url": source["source_url"], "sha": source["actual_sha"]})
            source_ids[source["actual_sha"]] = source_id
            insert_immutable(connection, "source_manifests", "source_id", {
                "source_id": source_id, "ingest_run_id": run_id, "source_kind": source["source_kind"],
                "source_url": source["source_url"], "retrieved_at_utc": source["retrieved_at_utc"],
                "content_sha256": source["actual_sha"], "local_relative_path": source["local_relative_path"],
                "parser_version": PARSER_VERSION, "created_at_utc": created,
            })
        manifest_sha = stable_hash(source_summary)
        for source, identities, rejections in parsed_pp:
            source_id = source_ids[source["actual_sha"]]
            for rejection in rejections:
                save_rejection(connection, run_id, source_id, rejection, created)
            for identity in identities:
                identity_id = stable_hash({"source_id": source_id, "hk_horse_code": identity["hk_horse_code"]})
                insert_immutable(connection, "pp_identity_map", "pp_identity_id", {
                    "pp_identity_id": identity_id, "source_id": source_id, "hk_horse_code": identity["hk_horse_code"],
                    "hk_horse_name": identity["hk_horse_name"], "original_horse_name": identity["original_horse_name"],
                    "source_country": identity["source_country"], "official_anchor_match": 1, "identity_confidence": 1.0,
                    "parse_status": "accepted_p1", "created_at_utc": created,
                })
                form = identity["form"]
                if form is None:
                    continue
                record_hash = stable_hash({"identity": identity["hk_horse_code"], "note": identity["note_raw"], "form": form})
                form_id = stable_hash({"pp_identity_id": identity_id, "record": record_hash})
                insert_immutable(connection, "pp_external_form", "pp_form_id", {
                    "pp_form_id": form_id, "pp_identity_id": identity_id, "form_date": form["form_date"],
                    "source_country": form["source_country"], "race_distance_m": form["race_distance_m"],
                    "finishing_position": form["finishing_position"], "field_size": form["field_size"],
                    "race_class_text": form["race_class_text"], "source_rating_raw": None, "source_rating_name": None,
                    "source_sectionals_available": 0, "source_record_sha256": record_hash, "created_at_utc": created,
                })
                features = {
                    "pp_available": 1, "pp_identity_match_confidence": 1.0, "pp_source_country": form["source_country"],
                    "pp_starts_pre_import": 1, "pp_latest_distance_m": form["race_distance_m"],
                    "pp_source_rating_available": 0, "pp_strength_prior_mean_candidate": None, "pp_strength_prior_sd_candidate": None,
                }
                availability = {name: int(value is not None) for name, value in features.items()}
                snapshot_payload = {"schema": SCHEMA_VERSION, "subject": identity["hk_horse_code"], "features": features, "availability": availability, "source_manifest_sha256": manifest_sha}
                snapshot_sha = stable_hash(snapshot_payload)
                snapshot_id = stable_hash({"type": "pp", "payload": snapshot_sha})
                insert_immutable(connection, "candidate_feature_snapshots", "snapshot_id", {
                    "snapshot_id": snapshot_id, "candidate_schema_version": SCHEMA_VERSION, "subject_type": "pp",
                    "subject_key": identity["hk_horse_code"], "as_of_utc": manifest["as_of_utc"],
                    "feature_json": json.dumps(features, ensure_ascii=False, sort_keys=True),
                    "availability_json": json.dumps(availability, ensure_ascii=False, sort_keys=True),
                    "source_manifest_sha256": manifest_sha, "snapshot_sha256": snapshot_sha, "created_at_utc": created,
                })
        for source, batches, rejections in parsed_trials:
            source_id = source_ids[source["actual_sha"]]
            for rejection in rejections:
                save_rejection(connection, run_id, source_id, rejection, created)
            for batch in batches:
                batch_id = stable_hash({"source_id": source_id, "label": batch["batch_label"]})
                insert_immutable(connection, "trial_batches", "trial_batch_id", {
                    "trial_batch_id": batch_id, "source_id": source_id, "trial_date": batch["trial_date"], "venue": batch["venue"],
                    "surface": batch["surface"], "distance_m": batch["distance_m"], "going": batch["going"],
                    "batch_label": batch["batch_label"], "batch_time_seconds": batch["batch_time_seconds"],
                    "sectional_json": json.dumps(batch["sectionals"]), "created_at_utc": created,
                })
                for entry in batch["entries"]:
                    entry_id = stable_hash({"batch": batch_id, "horse": entry["horse_code"]})
                    insert_immutable(connection, "trial_entries", "trial_entry_id", {
                        "trial_entry_id": entry_id, "trial_batch_id": batch_id, "horse_code": entry["horse_code"],
                        "horse_name": entry["horse_name"], "jockey": entry["jockey"], "trainer": entry["trainer"], "draw": entry["draw"],
                        "gear": entry["gear"], "lbw": entry["lbw"], "lbw_raw": entry["lbw_raw"],
                        "running_position_json": json.dumps(entry["running_positions"]), "finish_rank": entry["finish_rank"],
                        "finish_time_seconds": entry["finish_time_seconds"], "time_vs_batch_seconds": entry["time_vs_batch_seconds"],
                        "final_sectional_vs_batch_seconds": entry["final_sectional_vs_batch_seconds"], "finish_rank_pct": entry["finish_rank_pct"],
                        "position_gain": entry["position_gain"], "comment": entry["comment"], "created_at_utc": created,
                    })
                    features = {
                        "trial_available": 1, "trial_time_vs_batch_seconds_pre": entry["time_vs_batch_seconds"],
                        "trial_final_sectional_vs_batch_seconds_pre": None, "trial_finish_rank_pct_pre": entry["finish_rank_pct"],
                        "trial_position_gain_pre": entry["position_gain"], "trial_count_90d_pre": 1,
                    }
                    availability = {name: int(value is not None) for name, value in features.items()}
                    snapshot_payload = {"schema": SCHEMA_VERSION, "subject": entry["horse_code"], "features": features, "availability": availability, "source_manifest_sha256": manifest_sha}
                    snapshot_sha = stable_hash(snapshot_payload)
                    snapshot_id = stable_hash({"type": "trial", "payload": snapshot_sha})
                    insert_immutable(connection, "candidate_feature_snapshots", "snapshot_id", {
                        "snapshot_id": snapshot_id, "candidate_schema_version": SCHEMA_VERSION, "subject_type": "trial",
                        "subject_key": entry["horse_code"], "as_of_utc": manifest["as_of_utc"],
                        "feature_json": json.dumps(features, ensure_ascii=False, sort_keys=True),
                        "availability_json": json.dumps(availability, ensure_ascii=False, sort_keys=True),
                        "source_manifest_sha256": manifest_sha, "snapshot_sha256": snapshot_sha, "created_at_utc": created,
                    })
        connection.commit()
        counts = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("p1_ingest_runs", "source_manifests", "pp_identity_map", "pp_external_form", "trial_batches", "trial_entries", "candidate_feature_snapshots", "ingest_rejections")}
    finally:
        connection.close()
    report = {
        "status": "pass", "mode": "offline_archive_only", "parser_version": PARSER_VERSION, "candidate_schema_version": SCHEMA_VERSION,
        "database": str(db_path), "source_manifest_sha256": manifest_sha, "run_sha256": run_sha, "database_tables": counts,
        "parsed": {
            "pp_identity_accepted": sum(len(items) for _, items, _ in parsed_pp),
            "pp_form_accepted": sum(sum(1 for row in items if row["form"] is not None) for _, items, _ in parsed_pp),
            "trial_batches_accepted": sum(len(items) for _, items, _ in parsed_trials),
            "trial_entries_accepted": sum(sum(len(batch["entries"]) for batch in items) for _, items, _ in parsed_trials),
            "rejected": len(all_rejections),
            "rejections": all_rejections,
        },
        "production_access": {"v10_sqlite_opened": False, "n6_imported": False, "network_requests": 0},
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest P1 candidate PP and trial archives without network access")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--project-root", default=".")
    args = parser.parse_args()
    report = ingest(args)
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    compact = dict(report["parsed"])
    compact.pop("rejections", None)
    print(json.dumps({"status": report["status"], "parsed": compact, "tables": report["database_tables"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
