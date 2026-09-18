#!/usr/bin/env python3
"""Manifest-scoped post-race audit runner for the 23:45 HKT timer.

The runner exits safely when the Manifest is not for today's HKT meeting or
when official results/predictions cannot be joined exactly. It does not alter
N6/V10 artifacts or production prediction files.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HK = ZoneInfo("Asia/Hong_Kong")


def load(path: Path, default=None):
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError): return default


def run(command: list[str], log: Path, timeout: int = 900) -> subprocess.CompletedProcess:
    p = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write("$ " + " ".join(command) + "\n" + (p.stdout or "") + "\n" + (p.stderr or "") + "\n")
    return p


def norm_date(value: str) -> str:
    return str(value).replace("/", "-")


def collect_predictions(root: Path, date: str, course: str) -> tuple[list[dict], list[str]]:
    year, month, day = date.split("-")
    base = root / year / month
    rows, missing = [], []
    for directory in sorted(base.glob(f"{day}_{course}_R*")):
        prediction = directory / "prediction.json"
        if not prediction.exists():
            missing.append(str(prediction)); continue
        payload = load(prediction, {})
        race = payload.get("race", {}) if isinstance(payload.get("race"), dict) else {}
        try: race_no = int(directory.name.rsplit("_R", 1)[1])
        except (ValueError, IndexError): missing.append(str(prediction)); continue
        for row in payload.get("predictions", []) if isinstance(payload.get("predictions"), list) else []:
            if isinstance(row, dict):
                item = dict(row); item.update({"race_date": date, "racecourse": course, "race_no": race_no}); rows.append(item)
    return rows, missing


def export_results(db_path: Path, output: Path, date: str, course: str) -> int:
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT race_date,racecourse,race_no,horse_no,horse_name,finish_pos FROM starters WHERE race_date=? AND racecourse=? ORDER BY race_no,finish_pos,horse_no", (date, course)).fetchall()
    conn.close()
    data = [{"race_date": d, "racecourse": c, "race_no": int(r), "horse_no": int(h), "horse_name": n, "finish_pos": int(pos)} for d,c,r,h,n,pos in rows if pos is not None and h is not None]
    output.write_text(json.dumps({"results": data}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return len(data)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default="runtime/pre_race_schedule_current.json", type=Path)
    parser.add_argument("--project-dir", default=".", type=Path)
    parser.add_argument("--output-root", default="runtime/post_race", type=Path)
    parser.add_argument("--allow-date", help="test-only HKT date YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    project = args.project_dir.resolve(); manifest = load(args.manifest.resolve(), {})
    meeting = manifest.get("meeting") if isinstance(manifest, dict) else None
    if not isinstance(meeting, dict): print(json.dumps({"status":"skipped_manifest_invalid"})); return 0
    date, course = norm_date(meeting.get("race_date", "")), str(meeting.get("racecourse", "")).upper()
    today = args.allow_date or datetime.now(HK).date().isoformat()
    if date != today: print(json.dumps({"status":"skipped_manifest_not_today","manifest_date":date,"today_hkt":today}, ensure_ascii=False)); return 0
    year, month, day = date.split("-"); out = args.output_root / year / month / day; out.mkdir(parents=True, exist_ok=True)
    lock_path = out / ".post_race_audit.lock"
    with lock_path.open("w", encoding="utf-8") as lock:
        try: fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: print(json.dumps({"status":"skipped_locked"})); return 0
        predictions, missing = collect_predictions(project / "runtime/pre_race", date, course)
        result = {"status":"started","race_date":date,"racecourse":course,"prediction_rows":len(predictions),"missing_predictions":missing}
        (out / "auto_audit_run.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if args.dry_run: result["status"] = "dry_run_ready"; print(json.dumps(result, ensure_ascii=False)); return 0
        if not predictions or missing:
            result["status"] = "fail_closed_missing_predictions"; (out / "auto_audit_run.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"); print(json.dumps(result, ensure_ascii=False)); return 2
        predictions_path = out / f"predictions_{date}_{course}.json"; predictions_path.write_text(json.dumps({"predictions": predictions}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        db_path = out / f"official_results_{date}_{course}.sqlite"; csv_path = out / f"official_results_{date}_{course}.csv"; log = out / "post_race_auto_audit.log"
        etl = run([sys.executable, str(project / "hkjc_last_season_etl.py"), "--db", str(db_path), "--csv", str(csv_path), "--start-date", date, "--end-date", date, "--delay-min", "0.2", "--delay-max", "0.5", "--log-level", "INFO"], log)
        if etl.returncode != 0: result.update({"status":"fail_closed_official_etl", "etl_returncode":etl.returncode}); (out / "auto_audit_run.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"); print(json.dumps(result, ensure_ascii=False)); return 2
        results_path = out / f"official_results_{date}_{course}.json"; count = export_results(db_path, results_path, date, course)
        if count == 0: result.update({"status":"fail_closed_no_results", "result_rows":0}); (out / "auto_audit_run.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"); print(json.dumps(result, ensure_ascii=False)); return 2
        audit_path = out / "brier_audit.json"; audit_log = out / "brier_audit.jsonl"
        audit = run([sys.executable, str(project / "post_race_brier_audit.py"), "--predictions", str(predictions_path), "--results", str(results_path), "--output", str(audit_path), "--log", str(audit_log)], log, 120)
        if audit.returncode != 0: result.update({"status":"fail_closed_brier_failed", "audit_returncode":audit.returncode}); (out / "auto_audit_run.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"); print(json.dumps(result, ensure_ascii=False)); return 2
        diagnostics = run([sys.executable, str(project / "post_race_model_diagnostics.py"), "--audit", str(audit_path), "--predictions-root", str(project / "runtime/pre_race"), "--output", str(out / "model_diagnostics.json")], log, 120)
        result.update({"status":"completed" if diagnostics.returncode == 0 else "completed_with_diagnostics_warning", "result_rows":count, "audit":str(audit_path), "diagnostics_returncode":diagnostics.returncode})
        (out / "auto_audit_run.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"); print(json.dumps(result, ensure_ascii=False)); return 0 if diagnostics.returncode == 0 else 1


if __name__ == "__main__": raise SystemExit(main())
