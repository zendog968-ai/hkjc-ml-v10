#!/usr/bin/env python3
"""Prepare a validated N6 production release from the approved 72D candidate.

This program writes only a staged release under N6 models/releases.  It never
replaces the live model, restarts services, or writes to V10.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import torch

from n6.config import CATEGORICAL_FEATURES, MODELS_DIR, REPORTS_DIR
from n6.model import load_model_bundle

RELEASE_ID = "production_72d_market_implied_20260818T210739Z"
PRODUCTION_LABEL = "n6-production-72d-market-implied-v1"
CANDIDATE_ROOT = MODELS_DIR / "candidates" / "market_feature_variants_v1" / "implied_probability_only"
CANDIDATE_REPORT_ROOT = REPORTS_DIR / "candidates" / "market_feature_variants_v1" / "implied_probability_only"
CANDIDATE_MODEL = CANDIDATE_ROOT / "n6_mlp_candidate.pt"
CANDIDATE_PREPROCESSOR = CANDIDATE_ROOT / "n6_preprocessor_candidate.joblib"
CANDIDATE_REPORT = CANDIDATE_REPORT_ROOT / "n6_candidate_training_report.json"
CANDIDATE_PREDICTIONS = CANDIDATE_REPORT_ROOT / "n6_test_predictions_candidate.csv"
BOOTSTRAP_REPORT = CANDIDATE_REPORT_ROOT / "paired_race_bootstrap_validation.json"
CURRENT_REPORT = REPORTS_DIR / "n6_training_report.json"
STAGED_ROOT = MODELS_DIR / "releases" / RELEASE_ID
STAGED_MODEL = STAGED_ROOT / "n6_mlp_model.pt"
STAGED_PREPROCESSOR = STAGED_ROOT / "n6_preprocessor.joblib"
STAGED_REPORT = STAGED_ROOT / "n6_training_report.json"
STAGED_PREDICTIONS = STAGED_ROOT / "n6_test_predictions.csv"
STAGED_MANIFEST = STAGED_ROOT / "release_manifest.json"


def main() -> int:
    if STAGED_ROOT.exists():
        raise FileExistsError(f"Staged release already exists: {STAGED_ROOT}")
    for path in (CANDIDATE_MODEL, CANDIDATE_PREPROCESSOR, CANDIDATE_REPORT, CANDIDATE_PREDICTIONS, BOOTSTRAP_REPORT, CURRENT_REPORT):
        if not path.is_file():
            raise FileNotFoundError(path)
    candidate = torch.load(CANDIDATE_MODEL, map_location="cpu", weights_only=False)
    if candidate.get("artifact_type") != "n6_race_mlp_candidate" or int(candidate.get("input_dim", -1)) != 72:
        raise ValueError("Approved 72D candidate artifact is missing or has an unexpected contract.")
    contract = list(candidate.get("feature_contract", []))
    if "market_implied_probability" not in contract or "market_odds_available" not in contract or "market_log_odds" in contract:
        raise ValueError("Candidate market feature contract is not the approved implied-probability-only version.")
    candidate_report = json.loads(CANDIDATE_REPORT.read_text(encoding="utf-8"))
    bootstrap = json.loads(BOOTSTRAP_REPORT.read_text(encoding="utf-8"))
    old_report = json.loads(CURRENT_REPORT.read_text(encoding="utf-8"))
    production_report = {
        "engine": "N6 Neural Calculation Engine",
        "production_release": PRODUCTION_LABEL,
        "promoted_at_utc": datetime.now(UTC).isoformat(),
        "promotion_basis": {
            "experiment_id": candidate_report["experiment_id"],
            "variant": candidate_report["variant"],
            "approval": "User-approved promotion after candidate-only time-series evaluation.",
            "paired_race_bootstrap": bootstrap,
        },
        "source": old_report.get("source"),
        "strict_read_only_guarantee": "V10 database opened only with SQLite mode=ro&immutable=1 and PRAGMA query_only=ON; all artifacts below are N6-owned.",
        "architecture": candidate_report["architecture"],
        "target": old_report.get("target", "target_win"),
        "features": {
            "numeric": candidate_report["feature_change"]["numeric_features"],
            "categorical": CATEGORICAL_FEATURES,
            "count_before_encoding": candidate_report["feature_change"]["raw_feature_count"],
            "market_feature_policy": "market_implied_probability + market_odds_available; market_log_odds excluded",
        },
        "split": old_report.get("split"),
        "training": candidate_report["training"],
        "calibration": candidate_report["calibration"],
        "test_race_metrics": candidate_report["test_race_metrics"],
        "test_row_metrics": candidate_report["test_row_metrics"],
        "comparison_to_prior_74d": candidate_report["comparison"],
        "artifacts": {
            "model": "/home/ubuntu/n6_engine/models/n6_mlp_model.pt",
            "preprocessor": "/home/ubuntu/n6_engine/models/n6_preprocessor.joblib",
            "predictions": "/home/ubuntu/n6_engine/reports/n6_test_predictions.csv",
        },
        "limitations": [
            "時間外測試僅衡量歷史資料上的泛化能力，不構成未來賽果、機率或任何結果保證。",
            "賠率以 starters.win_odds 歷史欄位提供；生產模型只使用 market_implied_probability 與 market_odds_available，缺漏值由前處理器處理。",
            "API 的未來賽事推理僅採用輸入的賽前資料及 elo_current_state；不會讀取未來結果或修改 V10。",
        ],
    }
    candidate["artifact_type"] = "n6_race_mlp"
    candidate["production_release"] = PRODUCTION_LABEL
    candidate["report"] = production_report
    STAGED_ROOT.mkdir(parents=True, mode=0o750)
    torch.save(candidate, STAGED_MODEL)
    shutil.copy2(CANDIDATE_PREPROCESSOR, STAGED_PREPROCESSOR)
    shutil.copy2(CANDIDATE_PREDICTIONS, STAGED_PREDICTIONS)
    STAGED_REPORT.write_text(json.dumps(production_report, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest = {
        "release_id": RELEASE_ID,
        "production_label": PRODUCTION_LABEL,
        "prepared_at_utc": datetime.now(UTC).isoformat(),
        "candidate_sources": {
            "model": str(CANDIDATE_MODEL),
            "preprocessor": str(CANDIDATE_PREPROCESSOR),
            "training_report": str(CANDIDATE_REPORT),
            "bootstrap_validation": str(BOOTSTRAP_REPORT),
        },
        "market_feature_policy": production_report["features"]["market_feature_policy"],
        "input_dim": 72,
        "raw_feature_count": len(contract),
        "rollback_release": "models/releases/production_74d_pre_market_implied_20260818T210900Z",
    }
    STAGED_MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    for path in STAGED_ROOT.iterdir():
        path.chmod(0o600)
    bundle = load_model_bundle(STAGED_MODEL, STAGED_PREPROCESSOR)
    if int(bundle.metadata.get("input_dim", -1)) != 72 or bundle.metadata.get("production_release") != PRODUCTION_LABEL:
        raise ValueError("Staged model failed production release validation.")
    print(json.dumps({"staged_release": str(STAGED_ROOT), "model": str(STAGED_MODEL), "input_dim": bundle.metadata.get("input_dim"), "production_release": bundle.metadata.get("production_release"), "market_feature_policy": manifest["market_feature_policy"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
