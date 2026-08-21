#!/usr/bin/env python3
"""Immutable 15-event overseas deep-research blind-test pipeline.

This pipeline is intentionally isolated from V10.2's Hong Kong model and N6.
It seals a decision only before the declared UTC start, validates the public
RPR/TS + official HKJC market identity gate, and writes results separately after
an official HKJC result page is available. It never recreates a past decision.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from overseas_hkjc_core import (
    OfficialOverseasClient,
    OverseasMeeting,
    RESULT_URL,
    apply_results,
    compact_date,
    parse_results,
    upsert_meeting,
    upsert_race,
)

ROOT = Path(__file__).resolve().parent
ALLOWED_HOSTS = {
    "www.racingpost.com", "racingpost.com", "www.attheraces.com", "attheraces.com",
    "bet.hkjc.com", "racing.hkjc.com",
}
PIPELINE_VERSION = "overseas_immutable_blindtest_pipeline_v1"
PROXY_VERSION = "overseas_rpr_ts_public_composite_pl_v1"
DEFAULT_STUDY_ID = "overseas_rpr_ts_exploratory_15_v1"


def run_schema(conn: sqlite3.Connection, schema_path: Path) -> None:
    conn.executescript(schema_path.read_text(encoding="utf-8"))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("UTC時間必須含時區，例如 2026-08-21T12:00:00Z")
    return parsed.astimezone(timezone.utc)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_path(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def safe_event_key(value: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in value)


def ensure_https_url(value: object, allowed_hosts: set[str] = ALLOWED_HOSTS) -> str:
    if not isinstance(value, str):
        raise ValueError("來源網址必須為文字")
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
        raise ValueError(f"來源網址未在已核實的公開域名白名單內：{value}")
    return value


def atomic_create_json(path: Path, payload: dict[str, Any]) -> str:
    """Create once only; a decision/result never overwrites an existing artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o440)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink(missing_ok=True)
        finally:
            raise
    return sha256_bytes(body)


def atomic_replace_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def init_ledger(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS blindtest_studies (
            study_id TEXT PRIMARY KEY,
            max_events INTEGER NOT NULL CHECK(max_events = 15),
            pipeline_version TEXT NOT NULL,
            proxy_version TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS blindtest_events (
            study_id TEXT NOT NULL,
            event_key TEXT NOT NULL,
            scheduled_start_utc TEXT NOT NULL,
            decision_path TEXT,
            decision_sha256 TEXT,
            captured_at_utc TEXT,
            result_path TEXT,
            result_sha256 TEXT,
            settled_at_utc TEXT,
            status TEXT NOT NULL CHECK(status IN ('captured','settled','result_unavailable','invalid','cap_reached')),
            note TEXT,
            PRIMARY KEY(study_id, event_key),
            FOREIGN KEY(study_id) REFERENCES blindtest_studies(study_id)
        );
        CREATE INDEX IF NOT EXISTS idx_blindtest_events_status ON blindtest_events(study_id,status,scheduled_start_utc);
        """
    )
    conn.commit()
    return conn


def ensure_study(conn: sqlite3.Connection, study_id: str, max_events: int) -> None:
    if max_events != 15:
        raise ValueError("真實盲測只接受硬性15場上限；不可自行更改。")
    conn.execute(
        """INSERT INTO blindtest_studies(study_id,max_events,pipeline_version,proxy_version,created_at_utc)
           VALUES(?,?,?,?,?) ON CONFLICT(study_id) DO NOTHING""",
        (study_id, max_events, PIPELINE_VERSION, PROXY_VERSION, utc_now()),
    )
    row = conn.execute("SELECT max_events,pipeline_version,proxy_version FROM blindtest_studies WHERE study_id=?", (study_id,)).fetchone()
    if row is None or int(row["max_events"]) != 15 or row["pipeline_version"] != PIPELINE_VERSION or row["proxy_version"] != PROXY_VERSION:
        raise ValueError("現有盲測study的版本或15場上限與本管線不相符；為防混合cohort已停止。")
    conn.commit()


def captured_count(conn: sqlite3.Connection, study_id: str) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM blindtest_events WHERE study_id=? AND status IN ('captured','settled')", (study_id,)).fetchone()[0])


def validate_manifest(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "overseas_blindtest_manifest_v1":
        raise ValueError("manifest schema_version 必須為 overseas_blindtest_manifest_v1。")
    if manifest.get("study_id", DEFAULT_STUDY_ID) != DEFAULT_STUDY_ID:
        raise ValueError("僅接受預先註冊的15場海外RPR/TS探索性study_id。")
    if manifest.get("max_events", 15) != 15:
        raise ValueError("manifest max_events 必須固定為15。")
    events = manifest.get("events")
    if not isinstance(events, list) or not events:
        raise ValueError("manifest events 必須為非空陣列。")
    seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    for source in events:
        if not isinstance(source, dict):
            raise ValueError("manifest event 必須為物件。")
        date = source.get("meeting_date")
        code = str(source.get("simulcast_code", "")).upper()
        race_no = source.get("race_no")
        if not isinstance(date, str) or code not in {"S1", "S2", "S3"} or not isinstance(race_no, int) or race_no < 1:
            raise ValueError("每場必須有 meeting_date、S1/S2/S3、正整數 race_no。")
        event_key = f"{date}:{code}:{race_no}"
        if event_key in seen:
            raise ValueError(f"manifest event 重複：{event_key}")
        seen.add(event_key)
        event = dict(source)
        event["event_key"] = event_key
        event["scheduled_start_utc"] = parse_utc(str(event.get("scheduled_start_utc"))).isoformat(timespec="seconds")
        for field in ("fixture_url", "summary_url", "racing_post_url", "at_the_races_url", "hkjc_win_place_url", "hkjc_result_url"):
            event[field] = ensure_https_url(event.get(field))
        if not isinstance(event.get("venue"), str) or not event["venue"].strip():
            raise ValueError(f"{event_key} 缺少已核實 venue。")
        if not isinstance(event.get("local_start_time"), str) or not isinstance(event.get("hkt_start_time"), str):
            raise ValueError(f"{event_key} 缺少已核實 local_start_time／hkt_start_time。")
        if event.get("place_dividends") not in {3, 4}:
            raise ValueError(f"{event_key} place_dividends 必須為3或4，且須按HKJC公告核實。")
        validated.append(event)
    return validated


def raw_hashes(payload: dict[str, Any], deep_path: Path, enriched_path: Path, manifest_hash: str) -> dict[str, str]:
    hashes = {
        "manifest": manifest_hash,
        "deep_artifact": sha256_path(deep_path),
        "market_artifact": sha256_path(enriched_path),
    }
    raw = payload.get("raw_artifacts")
    if not isinstance(raw, dict):
        raise ValueError("賽前工件缺少 raw_artifacts，不能建立不可變決策。")
    for label, value in sorted(raw.items()):
        if not isinstance(value, str):
            continue
        path = Path(value)
        if not path.is_file():
            raise ValueError(f"來源原文不存在：{label}={path}")
        hashes[f"raw_{label}"] = sha256_path(path)
    required = {"raw_racing_post", "raw_at_the_races", "raw_hkjc_market"}
    if not required.issubset(hashes):
        raise ValueError("賽前封存必須同時具備Racing Post、At The Races及官方HKJC市場原文雜湊。")
    return hashes


def validate_enriched(payload: dict[str, Any], event: dict[str, Any], captured_at: str) -> list[dict[str, Any]]:
    race = payload.get("race")
    market = payload.get("market_research")
    if not isinstance(race, dict) or not isinstance(market, dict):
        raise ValueError("市場整合工件缺少race或market_research。")
    if (race.get("meeting_date"), str(race.get("simulcast_code", "")).upper(), race.get("race_no")) != (event["meeting_date"], event["simulcast_code"], event["race_no"]):
        raise ValueError("市場整合工件與官方manifest場次不一致。")
    if payload.get("n6_integration", {}).get("status") != "disabled_non_hk" or market.get("n6_status") != "disabled_non_hk":
        raise ValueError("海外工件必須明確N6 disabled_non_hk。")
    if market.get("status") != "complete" or market.get("matched_runner_count") != market.get("expected_runner_count"):
        raise ValueError("HKJC官方市場未完成全場一對一身份匹配；禁止封存決策。")
    scheduled = parse_utc(event["scheduled_start_utc"])
    if parse_utc(captured_at) >= scheduled:
        raise ValueError("封存時間不在開跑前；禁止建立賽前決策。")
    starters = payload.get("starters")
    if not isinstance(starters, list) or len(starters) < 2:
        raise ValueError("有效馬匹少於兩匹；禁止建立研究機率。")
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    win_sum = 0.0
    for source in starters:
        entry = source.get("market_research") if isinstance(source, dict) else None
        if not isinstance(entry, dict):
            raise ValueError("缺少市場研究列。")
        no = source.get("runner_no")
        if not isinstance(no, int) or no in seen:
            raise ValueError("馬號缺失或重複。")
        seen.add(no)
        if entry.get("match_status") != "matched":
            raise ValueError("存在未匹配馬匹；禁止封存。")
        try:
            win = float(entry["research_win_probability"])
            place = float(entry["research_place_probability"])
            score = float(source["deep_composite_score"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("研究機率或公開深度分數不完整。") from exc
        if not (math.isfinite(win) and math.isfinite(place) and math.isfinite(score) and 0.0 <= win <= 1.0 and 0.0 <= place <= 1.0):
            raise ValueError("研究機率不在有效範圍。")
        win_sum += win
        rows.append({
            "runner_no": no,
            "horse_name": source.get("horse_name"),
            "deep_score": score,
            "deep_rank": source.get("deep_rank"),
            "research_win_probability": win,
            "research_place_probability": place,
            "win_odds": entry.get("win_odds"),
            "place_odds": entry.get("place_odds"),
            "win_ev_uncalibrated": entry.get("win_ev"),
            "place_ev_uncalibrated": entry.get("place_ev"),
            "identity_match": entry.get("match_status"),
        })
    if not math.isclose(win_sum, 1.0, abs_tol=1e-9):
        raise ValueError(f"研究勝率守恆失敗：{win_sum}")
    if market.get("probability_method") is None:
        raise ValueError("研究機率方法缺失。")
    return sorted(rows, key=lambda item: item["runner_no"])


def canonical_write_prediction(canonical_db: Path, schema: Path, event: dict[str, Any], decision: dict[str, Any], decision_path: Path) -> None:
    """Write uncalibrated research predictions only into the separate overseas archive DB."""
    conn = sqlite3.connect(canonical_db)
    try:
        run_schema(conn, schema)
        meeting = OverseasMeeting(
            meeting_date=event["meeting_date"], simulcast_code=event["simulcast_code"],
            meeting_name=f"{event['venue']} overseas blind-test", location=event["venue"],
            fixture_url=event["fixture_url"], summary_url=event["summary_url"], seed_race_no=event["race_no"],
        )
        meeting_id = upsert_meeting(conn, meeting, None)
        race_id = upsert_race(
            conn, meeting_id, meeting, event["race_no"], race_status="discovered",
            scheduled_start_local=event["local_start_time"], scheduled_start_utc=event["scheduled_start_utc"],
            racecard_url=event["summary_url"], result_url=event["hkjc_result_url"],
        )
        captured = decision["captured_at_utc"]
        for row in decision["runners"]:
            conn.execute(
                """INSERT INTO overseas_prerace_predictions(
                       overseas_race_id,generated_at_utc,model_version,horse_no,predicted_win_probability,predicted_place_probability,
                       cold_start_tier,prior_source,win_odds_at_capture,place_odds_at_capture,win_ev,place_ev,kelly_fraction,
                       odds_snapshot_status,odds_snapshot_at_utc,source_json_path,feature_detail_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(overseas_race_id,generated_at_utc,model_version,horse_no) DO NOTHING""",
                (race_id, captured, PROXY_VERSION, row["runner_no"], row["research_win_probability"], row["research_place_probability"],
                 "uncalibrated_public_deep", "RPR_TS_public_composite", row.get("win_odds"), row.get("place_odds"),
                 row.get("win_ev_uncalibrated"), row.get("place_ev_uncalibrated"), None, "complete_identity_matched",
                 captured, str(decision_path), json.dumps({"research_only": True, "deep_score": row["deep_score"], "identity_match": "matched"}, ensure_ascii=False)),
            )
        conn.commit()
    finally:
        conn.close()


def seal_decision(
    conn: sqlite3.Connection, study_id: str, event: dict[str, Any], payload: dict[str, Any], deep_path: Path,
    enriched_path: Path, decision_dir: Path, manifest_hash: str, canonical_db: Path | None, canonical_schema: Path | None,
) -> dict[str, Any]:
    existing = conn.execute("SELECT status,decision_path FROM blindtest_events WHERE study_id=? AND event_key=?", (study_id, event["event_key"])).fetchone()
    if existing is not None:
        return {"event_key": event["event_key"], "status": "already_sealed", "decision_path": existing["decision_path"]}
    if captured_count(conn, study_id) >= 15:
        return {"event_key": event["event_key"], "status": "cap_reached"}
    captured = utc_now()
    runners = validate_enriched(payload, event, captured)
    hashes = raw_hashes(payload, deep_path, enriched_path, manifest_hash)
    decision = {
        "schema_version": "overseas_blindtest_decision_v1",
        "study_id": study_id,
        "pipeline_version": PIPELINE_VERSION,
        "proxy_version": PROXY_VERSION,
        "captured_at_utc": captured,
        "scheduled_start_utc": event["scheduled_start_utc"],
        "race": {"meeting_date": event["meeting_date"], "simulcast_code": event["simulcast_code"], "race_no": event["race_no"], "venue": event["venue"], "event_key": event["event_key"]},
        "source_urls": {key: event[key] for key in ("fixture_url", "summary_url", "racing_post_url", "at_the_races_url", "hkjc_win_place_url", "hkjc_result_url")},
        "source_hashes": hashes,
        "feature_availability": payload.get("field_availability"),
        "probability_contract": {"method": payload["market_research"]["probability_method"], "win_probability_sum": round(sum(row["research_win_probability"] for row in runners), 12), "status": "uncalibrated_research_only"},
        "n6_status": "disabled_non_hk",
        "runners": runners,
        "prohibitions": ["no_v10_2_probability_replacement", "no_n6_overseas_inference", "no_post_race_feature_refresh", "no_betting_instruction"],
    }
    decision_path = decision_dir / f"{safe_event_key(event['event_key'])}.json"
    decision_hash = atomic_create_json(decision_path, decision)
    if canonical_db is not None and canonical_schema is not None:
        canonical_write_prediction(canonical_db, canonical_schema, event, decision, decision_path)
    conn.execute(
        """INSERT INTO blindtest_events(study_id,event_key,scheduled_start_utc,decision_path,decision_sha256,captured_at_utc,status,note)
           VALUES(?,?,?,?,?,?,?,?)""",
        (study_id, event["event_key"], event["scheduled_start_utc"], str(decision_path), decision_hash, captured, "captured", "full_identity_match; uncalibrated_research_proxy"),
    )
    conn.commit()
    return {"event_key": event["event_key"], "status": "sealed", "decision_path": str(decision_path), "decision_sha256": decision_hash}


def run_capture_for_event(event: dict[str, Any], work_dir: Path, deep_db: Path, deep_schema: Path) -> tuple[Path, Path]:
    """Use public sources only; HKJC market requests remain rate-limited by the existing integrator."""
    event_tag = safe_event_key(event["event_key"])
    deep_path = work_dir / f"{event_tag}_deep.json"
    enriched_path = work_dir / f"{event_tag}_market.json"
    deep_command = [
        sys.executable, str(ROOT / "fetch_overseas_deep_data.py"), "--date", event["meeting_date"],
        "--simulcast-code", event["simulcast_code"], "--race-no", str(event["race_no"]), "--venue", event["venue"],
        "--racing-post-url", event["racing_post_url"], "--at-the-races-url", event["at_the_races_url"],
        "--local-start-time", event["local_start_time"], "--hkt-start-time", event["hkt_start_time"],
        "--db", str(deep_db), "--schema", str(deep_schema), "--raw-dir", str(ROOT / "archive/overseas_deep_raw"),
        "--output", str(deep_path), "--skip-hkjc-odds",
    ]
    completed = subprocess.run(deep_command, cwd=ROOT, text=True, capture_output=True, check=False, timeout=120)
    if completed.returncode != 0:
        raise RuntimeError(f"公開深度擷取失敗：{completed.stderr[-600:]}")
    seed = int(hashlib.sha256(event["event_key"].encode("utf-8")).hexdigest()[:8], 16)
    market_command = [
        sys.executable, str(ROOT / "enrich_overseas_deep_hkjc_market.py"), "--deep-input", str(deep_path),
        "--output", str(enriched_path), "--odds-url", event["hkjc_win_place_url"], "--db", str(deep_db),
        "--schema", str(deep_schema), "--raw-dir", str(ROOT / "archive/overseas_deep_raw"),
        "--state-file", str(ROOT / "runtime/overseas_blindtest/hkjc_market_request_state.json"),
        "--min-interval", "60", "--place-dividends", str(event["place_dividends"]), "--seed", str(seed),
    ]
    completed = subprocess.run(market_command, cwd=ROOT, text=True, capture_output=True, check=False, timeout=180)
    if completed.returncode != 0:
        raise RuntimeError(f"HKJC市場整合失敗：{completed.stderr[-600:]}")
    return deep_path, enriched_path


def settle_result(
    conn: sqlite3.Connection, study_id: str, event: dict[str, Any], result_dir: Path, canonical_db: Path, canonical_schema: Path,
) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM blindtest_events WHERE study_id=? AND event_key=?", (study_id, event["event_key"])).fetchone()
    if row is None or row["status"] != "captured":
        return {"event_key": event["event_key"], "status": "not_due_or_not_captured"}
    decision = json.loads(Path(row["decision_path"]).read_text(encoding="utf-8"))
    scheduled = parse_utc(decision["scheduled_start_utc"])
    if datetime.now(timezone.utc) < scheduled + timedelta(minutes=10):
        return {"event_key": event["event_key"], "status": "result_not_due"}
    canonical_db.parent.mkdir(parents=True, exist_ok=True)
    official = sqlite3.connect(canonical_db)
    try:
        official.row_factory = sqlite3.Row
        run_schema(official, canonical_schema)
        meeting = OverseasMeeting(event["meeting_date"], event["simulcast_code"], f"{event['venue']} overseas blind-test", event["venue"], event["fixture_url"], event["summary_url"], event["race_no"])
        meeting_id = upsert_meeting(official, meeting, None)
        race_id = upsert_race(official, meeting_id, meeting, event["race_no"], race_status="discovered", scheduled_start_local=event["local_start_time"], scheduled_start_utc=event["scheduled_start_utc"], racecard_url=event["summary_url"], result_url=event["hkjc_result_url"])
        client = OfficialOverseasClient(official, ROOT / "archive/overseas_official_raw")
        try:
            html, result_doc = client.get_rendered(event["hkjc_result_url"], "result", wait_mode="result")
            starters, dividends = parse_results(html)
        except Exception as exc:
            conn.execute("UPDATE blindtest_events SET status='result_unavailable',note=? WHERE study_id=? AND event_key=?", (type(exc).__name__, study_id, event["event_key"]))
            conn.commit()
            return {"event_key": event["event_key"], "status": "result_unavailable", "detail": type(exc).__name__}
        decision_numbers = {int(item["runner_no"]) for item in decision["runners"]}
        result_numbers = {int(item["horse_no"]) for item in starters if isinstance(item.get("horse_no"), int)}
        finish = [int(item["horse_no"]) for item in sorted(starters, key=lambda item: (item.get("finish_pos") if isinstance(item.get("finish_pos"), int) else 999, item.get("horse_no", 999))) if isinstance(item.get("finish_pos"), int) and item.get("finish_pos") >= 1]
        if result_numbers != decision_numbers or not finish or len(set(finish)) != len(finish):
            conn.execute("UPDATE blindtest_events SET status='invalid',note=? WHERE study_id=? AND event_key=?", ("official_result_field_mismatch", study_id, event["event_key"]))
            conn.commit()
            return {"event_key": event["event_key"], "status": "result_field_mismatch"}
        apply_results(official, race_id, starters, dividends, event["hkjc_result_url"])
        official.execute("UPDATE overseas_races SET result_document_id=? WHERE overseas_race_id=?", (result_doc, race_id))
        official.commit()
        source = official.execute("SELECT content_sha256,body_path,fetched_at_utc FROM overseas_source_documents WHERE document_id=?", (result_doc,)).fetchone()
        result = {
            "schema_version": "overseas_blindtest_result_v1", "study_id": study_id, "pipeline_version": PIPELINE_VERSION,
            "published_at_utc": source["fetched_at_utc"], "race": decision["race"], "source_url": event["hkjc_result_url"],
            "source_hashes": {"official_result": source["content_sha256"]}, "source_body_path": source["body_path"],
            "finish_order": finish, "field_numbers": sorted(result_numbers), "dividends": dividends,
        }
        result_path = result_dir / f"{safe_event_key(event['event_key'])}.json"
        result_hash = atomic_create_json(result_path, result)
        conn.execute("UPDATE blindtest_events SET status='settled',result_path=?,result_sha256=?,settled_at_utc=?,note=? WHERE study_id=? AND event_key=?", (str(result_path), result_hash, utc_now(), "official_result_field_matched", study_id, event["event_key"]))
        conn.commit()
        return {"event_key": event["event_key"], "status": "settled", "result_path": str(result_path)}
    finally:
        official.close()


def validate_manifest_offline(args: argparse.Namespace) -> int:
    """Validate manifest syntax and contract only; it never opens a network connection or ledger."""
    manifest_path = Path(args.manifest)
    report_path = Path(args.validation_report)
    if not manifest_path.is_file():
        atomic_replace_json(report_path, {"status": "missing_manifest", "checked_at_utc": utc_now(), "manifest": str(manifest_path), "network_access": "none"})
        print(json.dumps({"status": "missing_manifest", "report": str(report_path)}, ensure_ascii=False))
        return 1
    try:
        raw = manifest_path.read_bytes()
        events = validate_manifest(json.loads(raw.decode("utf-8")))
        report = {
            "status": "valid_offline", "checked_at_utc": utc_now(), "manifest": str(manifest_path),
            "manifest_sha256": sha256_bytes(raw), "study_id": DEFAULT_STUDY_ID, "max_events": 15,
            "event_keys": [event["event_key"] for event in events], "scheduled_start_utc": [event["scheduled_start_utc"] for event in events],
            "n6_status_required": "disabled_non_hk", "network_access": "none",
        }
        atomic_replace_json(report_path, report)
        print(json.dumps({"status": report["status"], "events": len(events), "report": str(report_path)}, ensure_ascii=False))
        return 0
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        atomic_replace_json(report_path, {"status": "invalid_offline", "checked_at_utc": utc_now(), "manifest": str(manifest_path), "error": f"{type(exc).__name__}: {exc}", "network_access": "none"})
        print(json.dumps({"status": "invalid_offline", "report": str(report_path)}, ensure_ascii=False))
        return 1


def tick(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest)
    status_path = Path(args.status)
    if not manifest_path.is_file():
        atomic_replace_json(status_path, {"status": "awaiting_official_manifest", "checked_at_utc": utc_now(), "message": "未有已核實未來S1/S2/S3 manifest；不會猜測賽程或發出外部請求。", "n6_status": "disabled_non_hk"})
        print(json.dumps({"status": "awaiting_official_manifest"}, ensure_ascii=False))
        return 0
    manifest_bytes = manifest_path.read_bytes()
    events = validate_manifest(json.loads(manifest_bytes.decode("utf-8")))
    ledger_path = Path(args.ledger)
    lock_path = ledger_path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            atomic_replace_json(status_path, {"status": "already_running", "checked_at_utc": utc_now()})
            return 0
        conn = init_ledger(ledger_path)
        try:
            ensure_study(conn, DEFAULT_STUDY_ID, 15)
            outcomes: list[dict[str, Any]] = []
            for event in events:
                outcomes.append(settle_result(conn, DEFAULT_STUDY_ID, event, Path(args.results_dir), Path(args.canonical_db), Path(args.canonical_schema)))
            now = datetime.now(timezone.utc)
            for event in events:
                start = parse_utc(event["scheduled_start_utc"])
                minutes = (start - now).total_seconds() / 60.0
                if not (0.0 < minutes <= args.capture_window_minutes):
                    continue
                if captured_count(conn, DEFAULT_STUDY_ID) >= 15:
                    outcomes.append({"event_key": event["event_key"], "status": "cap_reached"})
                    break
                try:
                    deep_path, enriched_path = run_capture_for_event(event, Path(args.work_dir), Path(args.deep_db), Path(args.deep_schema))
                    payload = json.loads(enriched_path.read_text(encoding="utf-8"))
                    outcomes.append(seal_decision(conn, DEFAULT_STUDY_ID, event, payload, deep_path, enriched_path, Path(args.decisions_dir), sha256_bytes(manifest_bytes), Path(args.canonical_db), Path(args.canonical_schema)))
                except Exception as exc:
                    outcomes.append({"event_key": event["event_key"], "status": "capture_failed", "detail": f"{type(exc).__name__}: {str(exc)[:300]}"})
            report = {"schema_version": PIPELINE_VERSION, "checked_at_utc": utc_now(), "study_id": DEFAULT_STUDY_ID, "captured_count": captured_count(conn, DEFAULT_STUDY_ID), "max_events": 15, "n6_status": "disabled_non_hk", "outcomes": outcomes}
            atomic_replace_json(status_path, report)
            print(json.dumps(report, ensure_ascii=False))
        finally:
            conn.close()
    return 0


def fixture_payload(event: dict[str, Any], root: Path) -> tuple[dict[str, Any], Path, Path]:
    raw = root / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    raw_paths = {}
    for label, content in {"racing_post": "fixture_rpr_ts", "at_the_races": "fixture_form", "hkjc_market": "fixture_market"}.items():
        path = raw / f"{label}.txt"
        path.write_text(content, encoding="utf-8")
        raw_paths[label] = str(path)
    scores = [80.0, 60.0, 40.0]
    weights = [math.exp((value - max(scores)) / 20.0) for value in scores]
    total = sum(weights)
    wins = [value / total for value in weights]
    payload = {
        "race": {"meeting_date": event["meeting_date"], "simulcast_code": event["simulcast_code"], "race_no": event["race_no"]},
        "n6_integration": {"status": "disabled_non_hk"}, "field_availability": {"rpr": "available_public", "top_speed": "available_public", "hkjc_odds": "available_public"},
        "raw_artifacts": raw_paths,
        "market_research": {"status": "complete", "matched_runner_count": 3, "expected_runner_count": 3, "probability_method": "fixture_uncalibrated_proxy", "n6_status": "disabled_non_hk"},
        "starters": [
            {"runner_no": i + 1, "horse_name": f"Fixture Horse {i + 1}", "deep_composite_score": score, "deep_rank": i + 1,
             "market_research": {"match_status": "matched", "research_win_probability": wins[i], "research_place_probability": [0.90, 0.72, 0.38][i], "win_odds": [2.1, 4.5, 10.0][i], "place_odds": [1.3, 1.8, 3.3][i], "win_ev": None, "place_ev": None}}
            for i, score in enumerate(scores)
        ],
    }
    deep = root / "fixture_deep.json"; market = root / "fixture_market.json"
    deep.write_text(json.dumps({"fixture": True}, sort_keys=True), encoding="utf-8")
    market.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return payload, deep, market


def simulate(args: argparse.Namespace) -> int:
    report_root = Path(args.simulation_dir)
    if report_root.exists():
        raise SystemExit("模擬目錄已存在；為保留不可變測試證據，請使用新目錄。")
    report_root.mkdir(parents=True)
    before = {"v10_sqlite_sha256": sha256_path(ROOT / "hkjc_last_season.sqlite"), "v10_model_sha256": sha256_path(ROOT / "horse_model.pkl")}
    ledger = report_root / "blindtest.sqlite"
    conn = init_ledger(ledger)
    study_id = DEFAULT_STUDY_ID
    ensure_study(conn, study_id, 15)
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    base = {
        "meeting_date": "2099-01-01", "simulcast_code": "S1", "venue": "Simulation", "scheduled_start_utc": future,
        "local_start_time": "12:00 UTC", "hkt_start_time": "20:00 HKT", "place_dividends": 3,
        "fixture_url": "https://racing.hkjc.com/en-us/overseas/simulcast_fixture?y=2099-2100",
        "summary_url": "https://racing.hkjc.com/en-us/overseas/race-summary?RaceDate=20990101&Racecourse=S1",
        "racing_post_url": "https://www.racingpost.com/racecards/1/example/2099-01-01/1/",
        "at_the_races_url": "https://www.attheraces.com/racecards/Example/01-January-2099",
        "hkjc_win_place_url": "https://bet.hkjc.com/en/racing/wp/2099-01-01/S1/1",
        "hkjc_result_url": "https://racing.hkjc.com/en-us/overseas/results?RaceDate=20990101&Racecourse=S1&RaceNo=1",
    }
    outcomes = []
    for number in range(1, 16):
        event = dict(base); event["race_no"] = number; event["event_key"] = f"2099-01-01:S1:{number}"
        payload, deep, market = fixture_payload(event, report_root / f"fixture_{number}")
        outcomes.append(seal_decision(conn, study_id, event, payload, deep, market, report_root / "decisions", f"fixture-manifest-{number}", None, None))
    sixteenth = dict(base); sixteenth["race_no"] = 16; sixteenth["event_key"] = "2099-01-01:S1:16"
    payload, deep, market = fixture_payload(sixteenth, report_root / "fixture_16")
    cap_outcome = seal_decision(conn, study_id, sixteenth, payload, deep, market, report_root / "decisions", "fixture-manifest-16", None, None)
    duplicate_outcome = seal_decision(conn, study_id, dict(base, race_no=1, event_key="2099-01-01:S1:1"), *fixture_payload(dict(base, race_no=1, event_key="2099-01-01:S1:1"), report_root / "duplicate"), report_root / "decisions", "fixture-duplicate", None, None)
    # Test an immutable result document without querying a live source.
    first = dict(base, race_no=1, event_key="2099-01-01:S1:1")
    result = {"schema_version": "overseas_blindtest_result_v1", "study_id": study_id, "published_at_utc": (parse_utc(future) + timedelta(hours=2)).isoformat(timespec="seconds"), "race": {"meeting_date": first["meeting_date"], "simulcast_code": "S1", "race_no": 1}, "source_hashes": {"official_result": "fixture"}, "finish_order": [2, 1, 3]}
    result_path = report_root / "results/2099_01_01_S1_1.json"
    result_hash = atomic_create_json(result_path, result)
    conn.execute("UPDATE blindtest_events SET status='settled',result_path=?,result_sha256=?,settled_at_utc=? WHERE study_id=? AND event_key=?", (str(result_path), result_hash, utc_now(), study_id, first["event_key"]))
    conn.commit()
    after = {"v10_sqlite_sha256": sha256_path(ROOT / "hkjc_last_season.sqlite"), "v10_model_sha256": sha256_path(ROOT / "horse_model.pkl")}
    report = {
        "status": "passed" if captured_count(conn, study_id) == 15 and cap_outcome["status"] == "cap_reached" and duplicate_outcome["status"] == "already_sealed" and before == after else "failed",
        "captured_or_settled_count": captured_count(conn, study_id), "cap_outcome": cap_outcome, "duplicate_outcome": duplicate_outcome,
        "immutable_result_created": result_path.is_file(), "v10_integrity_before": before, "v10_integrity_after": after,
        "n6_status": "disabled_non_hk", "outcomes": outcomes,
    }
    conn.close()
    atomic_replace_json(report_root / "SIMULATION_REPORT.json", report)
    print(json.dumps({"status": report["status"], "report": str(report_root / "SIMULATION_REPORT.json")}, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="未來15場海外RPR/TS研究盲測的不可變賽前封存及賽後結算。")
    parser.add_argument("--mode", choices=["tick", "simulate", "validate-manifest"], default="tick")
    parser.add_argument("--manifest", default="runtime/overseas_blindtest/active_manifest.json")
    parser.add_argument("--ledger", default="runtime/overseas_blindtest/overseas_blindtest.sqlite")
    parser.add_argument("--status", default="runtime/overseas_blindtest/status.json")
    parser.add_argument("--work-dir", default="runtime/overseas_blindtest/work")
    parser.add_argument("--decisions-dir", default="archive/overseas_deep_backtest/decisions")
    parser.add_argument("--results-dir", default="archive/overseas_deep_backtest/results")
    parser.add_argument("--deep-db", default="overseas_deep_racing.sqlite")
    parser.add_argument("--deep-schema", default="schema_overseas_deep_racing.sql")
    parser.add_argument("--canonical-db", default="overseas_blindtest_official.sqlite")
    parser.add_argument("--canonical-schema", default="schema_overseas_racing.sql")
    parser.add_argument("--capture-window-minutes", type=float, default=15.0)
    parser.add_argument("--simulation-dir", default="reports/overseas_deep/OVERSEAS_BLINDTEST_PIPELINE_SIMULATION_2026-08-20")
    parser.add_argument("--validation-report", default="reports/overseas_deep/OVERSEAS_BLINDTEST_MANIFEST_VALIDATION.json")
    args = parser.parse_args()
    if not 5.0 <= args.capture_window_minutes <= 20.0:
        raise SystemExit("capture-window-minutes 必須介乎5至20分鐘，且實際封存仍須嚴格早於開跑。")
    if args.mode == "simulate":
        return simulate(args)
    if args.mode == "validate-manifest":
        return validate_manifest_offline(args)
    return tick(args)


if __name__ == "__main__":
    raise SystemExit(main())
