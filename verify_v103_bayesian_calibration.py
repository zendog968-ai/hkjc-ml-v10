#!/usr/bin/env python3
"""Contract verification for V10.3 Bayesian provenance-enforced calibration.

The fixture is deliberately *exploratory*: it uses a small slice of saved V10.2
history solely to verify provenance, model-version isolation and sidecar contracts.
It is not a 150/325-race adoption result and cannot authorize V10.3 replacement.
"""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from bayesian_calibration import load_frozen_csv, load_model, overlay_prediction, sha256_file
from filter_high_probability import run as render_filter_report

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "v103_bayesian_contract_fixture"
HISTORICAL = ROOT / "v102_multiseason_backtest_predictions.csv"
SOURCE_PREDICTION = ROOT / "ml_sample_prediction.json"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_verified_fixture() -> tuple[Path, Path, Path, int, str]:
    """Create a canonical CSV and manifest from real saved V10.2 history rows."""
    base_model = OUTPUT / "base_model_fixture.pkl"
    base_model.write_bytes(b"v10.2 base model hash fixture; never used for inference\n")
    base_model_sha = sha256_file(base_model)
    with HISTORICAL.open("r", encoding="utf-8-sig", newline="") as handle:
        source_rows = list(csv.DictReader(handle))
        assert source_rows, "歷史 V10.2 工件不應為空"
        fieldnames = list(source_rows[0])
    selected_groups: list[str] = []
    for row in source_rows:
        group = str(row.get("race_group") or "")
        if group and group not in selected_groups:
            selected_groups.append(group)
        if len(selected_groups) >= 60:
            break
    assert len(selected_groups) == 60, "需要至少 60 場真實歷史賽事作契約 fixture"
    selected = [dict(row) for row in source_rows if row.get("race_group") in set(selected_groups)]
    canonical = (OUTPUT / "canonical_training" / "fixture" / "v103_immutable_unseen_cohort.csv").resolve()
    canonical.parent.mkdir(parents=True, exist_ok=True)
    with canonical.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[*fieldnames, "base_model_sha256"])
        writer.writeheader()
        for row in selected:
            row["base_model_sha256"] = base_model_sha
            writer.writerow(row)
    races, exclusions = load_frozen_csv(canonical, require_target=True)
    assert not exclusions, f"canonical fixture 不應含排除：{dict(exclusions)}"
    assert len(races) == 60, len(races)
    manifest = OUTPUT / "manifest_latest.json"
    fingerprint = sha256_bytes("fixture-cohort-fingerprint".encode("utf-8"))
    write_json(manifest, {
        "schema_version": "v10_3_unseen_cohort_manifest_v1",
        "generated_at_hkt": "2026-08-18T13:00:00+08:00",
        "model_cohorts": {
            base_model_sha: {
                "record_count": len(races),
                "cohort_fingerprint": fingerprint,
                "canonical_training_csv_path": str(canonical),
                "canonical_training_csv_sha256": sha256_file(canonical),
                "canonical_training_race_count": len(races),
                "canonical_training_schema": "v10_3_immutable_unseen_cohort_csv_v2",
            }
        },
    })
    return canonical, manifest, base_model, len(races), base_model_sha


def run_fit(canonical: Path, manifest: Path, base_model: Path, output_model: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable, str(ROOT / "bayesian_calibration.py"), "fit",
            "--predictions", str(canonical),
            "--cohort-manifest", str(manifest),
            "--base-model", str(base_model),
            "--output-model", str(output_model),
            "--advi-steps", "200", "--posterior-draws", "60", "--seed", "10301",
        ],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )


def main() -> int:
    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)
    OUTPUT.mkdir(parents=True)
    historical_before = sha256_file(HISTORICAL)
    canonical, manifest, base_model, race_count, base_model_sha = build_verified_fixture()

    model_path = OUTPUT / "posterior_model.npz"
    fitted = run_fit(canonical, manifest, base_model, model_path)
    assert fitted.returncode == 0, {"stdout": fitted.stdout, "stderr": fitted.stderr}
    fit_payload = json.loads(fitted.stdout)
    metadata = load_model(model_path)["metadata"]
    provenance = metadata["cohort_provenance"]
    assert provenance["verification_status"] == "verified_immutable_unseen_cohort"
    assert provenance["base_model_sha256"] == base_model_sha
    assert provenance["canonical_training_csv_sha256"] == sha256_file(canonical)
    assert provenance["canonical_training_race_count"] == race_count
    assert metadata["formal_probability_replacement"] is False
    assert metadata["v102_core_modified"] is False
    assert fit_payload["cohort_provenance"]["manifest_sha256"] == sha256_file(manifest)

    # Reject a base model whose full hash has no manifest bucket.
    other_model = OUTPUT / "other_model.pkl"
    other_model.write_bytes(b"different base model version\n")
    wrong_model = run_fit(canonical, manifest, other_model, OUTPUT / "wrong_model.npz")
    assert wrong_model.returncode == 2 and "目前 base-model SHA-256" in wrong_model.stdout, wrong_model.stdout

    # Reject any modification to canonical CSV even when its row values remain parseable.
    original_csv = canonical.read_text(encoding="utf-8")
    canonical.write_text(original_csv + "\n", encoding="utf-8")
    tampered = run_fit(canonical, manifest, base_model, OUTPUT / "tampered.npz")
    assert tampered.returncode == 2 and "SHA-256" in tampered.stdout, tampered.stdout
    canonical.write_text(original_csv, encoding="utf-8")

    # Existing source prediction is only read. Add missing identity in a fixture copy.
    source_before = sha256_file(SOURCE_PREDICTION)
    prediction = json.loads(SOURCE_PREDICTION.read_text(encoding="utf-8"))
    prediction["race"] = {**(prediction.get("race") or {}), "race_date": "2026-08-18", "racecourse": "HV", "race_no": 1}
    fixture_prediction = OUTPUT / "prediction.json"
    write_json(fixture_prediction, prediction)
    sidecar_path = OUTPUT / "uncertainty_sidecar.json"
    sidecar = overlay_prediction(model_path, fixture_prediction, sidecar_path, posterior_draws=60, seed=10301)
    assert sha256_file(SOURCE_PREDICTION) == source_before, "V10.2 source prediction 被修改"
    assert sha256_file(HISTORICAL) == historical_before, "V10.2 historical artifact 被修改"
    assert sidecar["bayesian_status"] == "available_research_only", sidecar
    assert sidecar["formal_probability_replacement"] is False
    assert sidecar["model_sha256"] == sha256_file(model_path)
    assert sidecar["probability_sum_max_abs_error"] <= 1e-6
    assert len(sidecar["rows"]) == len(prediction["predictions"])
    for row in sidecar["rows"]:
        assert 0.0 <= row["posterior_win_p05"] <= row["posterior_win_mean"] <= row["posterior_win_p95"] <= 1.0

    filtered = OUTPUT / "filter.json"
    markdown = OUTPUT / "report.md"
    rendered = render_filter_report(str(fixture_prediction), str(filtered), markdown_output=str(markdown), bayesian_overlay_path=str(sidecar_path))
    assert rendered["v103_bayesian_disclosure"]["formal_probability_replacement"] is False
    rendered_text = markdown.read_text(encoding="utf-8")
    assert "V10.3 貝氏校準／不確定性披露" in rendered_text
    assert "V10.2 `predicted_win_probability`、排序、EV 與 Kelly 完全不變" in rendered_text

    summary = {
        "status": "passed_exploratory_contract_only",
        "historical_races_used": race_count,
        "base_model_sha256": base_model_sha,
        "model_sha256": sha256_file(model_path),
        "manifest_sha256": sha256_file(manifest),
        "canonical_csv_sha256": sha256_file(canonical),
        "cross_model_rejection": True,
        "tampered_csv_rejection": True,
        "sidecar_probability_sum_max_abs_error": sidecar["probability_sum_max_abs_error"],
        "formal_probability_replacement": sidecar["formal_probability_replacement"],
        "source_prediction_sha256": source_before,
    }
    write_json(OUTPUT / "validation.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
