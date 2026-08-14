#!/usr/bin/env python3
"""Regression checks for V10.2 advanced feature and ensemble paths.

The test uses stored public-page fixtures and deterministic local copies only; it sends no
network requests. It asserts behaviour rather than historical profitability.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from filter_high_probability import candidate_row
from predict import predict
from v102_feature_utils import body_weight_features, cold_start_prior_score, distance_match_prior, trial_prior

ROOT = Path(__file__).resolve().parent


def main() -> int:
    # Body-weight guardrails.
    assert body_weight_features(1100, 1084)["is_extreme_body_weight_change_pre"] == 1
    assert body_weight_features(None, 1084)["horse_body_weight_known_pre"] == 0
    # No readable pedigree/trial must stay neutral rather than invent a strong score.
    match, known = distance_match_prior(None, 1200)
    trial = trial_prior(None, None, None)
    assert (match, known, trial["trial_prior_known_pre"]) == (0.5, 0, 0)
    assert cold_start_prior_score(match, known, 0, 0, 0, 0) == 0.5

    fixture_card = ROOT / "odds_place_live_test" / "test_dual_market_race_card.json"
    db = ROOT / "hkjc_last_season.sqlite"
    model = ROOT / "horse_model.pkl"
    with tempfile.TemporaryDirectory(prefix="v102_regression_") as folder:
        work = Path(folder)
        # Reuse only locally stored official-page fixture odds. The late file is a deterministic
        # replay copy with one published win price reduced 20%, testing label mechanics.
        card_payload = json.loads(fixture_card.read_text(encoding="utf-8"))
        horses = [row["horse_name"] for row in card_payload["runners"]]
        early = {
            "schema_version": "v10.2_odds_snapshot", "snapshot_label": "T_MINUS_15", "status": "complete",
            "odds": {horse: {"win": 8.0 + index, "place": 3.0 + index / 10.0} for index, horse in enumerate(horses)},
        }
        late = json.loads(json.dumps(early)); late["snapshot_label"] = "T_MINUS_5"; late["odds"][horses[0]]["win"] = 6.4
        (work / "early.json").write_text(json.dumps(early, ensure_ascii=False), encoding="utf-8")
        (work / "late.json").write_text(json.dumps(late, ensure_ascii=False), encoding="utf-8")
        # Use existing standalone overlays from the public fixture if they exist; otherwise
        # snapshot content is sufficient for the movement-specific assertion.
        win = {name: values["win"] for name, values in late["odds"].items()}
        place = {name: values["place"] for name, values in late["odds"].items()}
        (work / "win.json").write_text(json.dumps(win, ensure_ascii=False), encoding="utf-8")
        (work / "place.json").write_text(json.dumps(place, ensure_ascii=False), encoding="utf-8")
        result = predict(
            str(db), str(model), str(fixture_card), str(work / "prediction.json"), str(work / "prediction.csv"),
            str(work / "win.json"), str(work / "place.json"), str(work / "early.json"), str(work / "late.json"),
            place_simulations=10_000, simulation_seed=20260814,
        )
        row = next(item for item in result["predictions"] if item["horse_name"] == horses[0])
        assert abs(row["odds_drop_ratio"] + 0.2) < 1e-12
        assert row["gate_money_drop_flag"] is True
        assert row["market_movement_label"] == "🔥 閘前資金落飛"
        assert abs(sum(item["predicted_win_probability"] for item in result["predictions"]) - 1.0) < 1e-9
        assert result["model"].startswith("HKJC V10.2")
        assert candidate_row(row)["gate_money_drop_flag"] is True
    print(json.dumps({"status": "PASS", "checks": ["body_weight", "cold_start_neutral", "odds_drop", "ensemble_probability"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
