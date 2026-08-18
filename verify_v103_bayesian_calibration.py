#!/usr/bin/env python3
"""Contract verification for the V10.3 NumPyro Bayesian risk-disclosure overlay.

The test uses the repository's saved V10.2 historical artifact for an intentionally
small *exploratory* fit.  It verifies only data contracts and non-interference;
it is not evidence of calibration improvement or readiness to adopt V10.3.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from bayesian_calibration import (
    fit_model,
    load_frozen_csv,
    overlay_prediction,
    sha256_file,
)
from filter_high_probability import run as render_filter_report

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "v103_bayesian_contract_fixture"
HISTORICAL = ROOT / "v102_multiseason_backtest_predictions.csv"
SOURCE_PREDICTION = ROOT / "ml_sample_prediction.json"


def main() -> int:
    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)
    OUTPUT.mkdir(parents=True)
    races, exclusions = load_frozen_csv(HISTORICAL, require_target=True)
    assert len(races) >= 60, f"預期至少 60 場真實歷史賽事，實得 {len(races)}"
    assert not exclusions, f"歷史輸入不應有完整性排除：{dict(exclusions)}"

    model_path = OUTPUT / "posterior_model.npz"
    metadata = fit_model(
        races=races[-60:],
        output_path=model_path,
        input_hash=sha256_file(HISTORICAL),
        advi_steps=200,
        posterior_draws=60,
        seed=10301,
    )
    assert metadata["formal_probability_replacement"] is False
    assert metadata["v102_core_modified"] is False
    assert model_path.exists()

    # This older saved V10.2 JSON is used only as a structural report fixture. Add
    # missing official identity in an in-memory copy, never edit its source file.
    source_before = sha256_file(SOURCE_PREDICTION)
    prediction = json.loads(SOURCE_PREDICTION.read_text(encoding="utf-8"))
    prediction["race"] = {
        **(prediction.get("race") or {}),
        "race_date": "2026-08-18",
        "racecourse": "HV",
        "race_no": 1,
    }
    fixture_prediction = OUTPUT / "prediction.json"
    fixture_prediction.write_text(json.dumps(prediction, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sidecar_path = OUTPUT / "uncertainty_sidecar.json"
    sidecar = overlay_prediction(model_path, fixture_prediction, sidecar_path, posterior_draws=60, seed=10301)
    assert sha256_file(SOURCE_PREDICTION) == source_before, "V10.2 source prediction 被修改"
    assert sidecar["bayesian_status"] == "available_research_only", sidecar
    assert sidecar["formal_probability_replacement"] is False
    assert sidecar["probability_sum_max_abs_error"] <= 1e-6
    assert len(sidecar["rows"]) == len(prediction["predictions"])
    for row in sidecar["rows"]:
        assert 0.0 <= row["posterior_win_p05"] <= row["posterior_win_mean"] <= row["posterior_win_p95"] <= 1.0
        assert "v102_predicted_win_probability" in row

    filtered = OUTPUT / "filter.json"
    markdown = OUTPUT / "report.md"
    rendered = render_filter_report(str(fixture_prediction), str(filtered), markdown_output=str(markdown), bayesian_overlay_path=str(sidecar_path))
    assert rendered["v103_bayesian_disclosure"]["formal_probability_replacement"] is False
    rendered_text = markdown.read_text(encoding="utf-8")
    assert "V10.3 貝氏校準／不確定性披露" in rendered_text
    assert "V10.2 `predicted_win_probability`、排序、EV 與 Kelly 完全不變" in rendered_text

    summary = {
        "status": "passed_exploratory_contract_only",
        "historical_races_used": 60,
        "model_sha256": metadata["model_sha256"],
        "sidecar_probability_sum_max_abs_error": sidecar["probability_sum_max_abs_error"],
        "formal_probability_replacement": sidecar["formal_probability_replacement"],
        "source_prediction_sha256": source_before,
    }
    (OUTPUT / "validation.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
