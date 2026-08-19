#!/usr/bin/env python3
"""Production-model feature attribution for the 74-dimensional N6 MLP.

Uses Integrated Gradients on the saved PyTorch production model's pre-calibration
logit, aggregated from encoded dimensions back to original features. This is a
deterministic gradient-based local attribution method; it is not a claim of exact
Kernel/Tree SHAP values. Existing within-race permutation importance is used as
an independent predictive-performance cross-check.
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import torch

from analyze_model import chronological_test_split, original_feature_name
from n6.config import MODEL_PATH, PREPROCESSOR_PATH, REPORTS_DIR
from n6.feature_engineering import load_training_frame
from n6.model import load_model_bundle
from train import chronological_split

# 1,024 trapezoidal steps provide a tighter completeness approximation across
# the full held-out sample for this LayerNorm/GELU MLP.
INTEGRATION_STEPS = 1024
BATCH_SIZE = 256
PRODUCTION_PERMUTATION_REPORT = REPORTS_DIR / "n6_interpretability_report.json"
OUTPUT_JSON = REPORTS_DIR / "n6_production_feature_attribution.json"
OUTPUT_CSV = REPORTS_DIR / "n6_production_feature_attribution.csv"
OUTPUT_MD = REPORTS_DIR / "N6_PRODUCTION_FEATURE_ATTRIBUTION.md"


def integrated_gradients(
    model: torch.nn.Module,
    inputs: np.ndarray,
    baseline: np.ndarray,
    steps: int = INTEGRATION_STEPS,
    batch_size: int = BATCH_SIZE,
) -> tuple[np.ndarray, float]:
    """Return per-input IG values and mean absolute completeness error."""
    model.eval()
    baseline_tensor = torch.tensor(baseline, dtype=torch.float32)
    attributes = np.zeros_like(inputs, dtype=np.float32)
    completeness_errors: list[float] = []
    alphas = torch.linspace(0.0, 1.0, steps + 1, dtype=torch.float32)
    trapezoid_weights = torch.ones(steps + 1, dtype=torch.float32)
    trapezoid_weights[0] = 0.5
    trapezoid_weights[-1] = 0.5

    for start in range(0, len(inputs), batch_size):
        batch = torch.tensor(inputs[start:start + batch_size], dtype=torch.float32)
        delta = batch - baseline_tensor
        accumulated = torch.zeros_like(batch)
        for alpha, weight in zip(alphas, trapezoid_weights):
            interpolated = (baseline_tensor + alpha * delta).detach().requires_grad_(True)
            logits = model(interpolated).sum()
            gradients = torch.autograd.grad(logits, interpolated, create_graph=False)[0]
            accumulated += weight * gradients
        batch_attributions = delta * accumulated / float(steps)
        attributes[start:start + len(batch)] = batch_attributions.detach().cpu().numpy()
        with torch.no_grad():
            output_delta = model(batch) - model(baseline_tensor.expand_as(batch))
            attribution_sum = batch_attributions.sum(dim=1)
            completeness_errors.extend(torch.abs(output_delta - attribution_sum).cpu().numpy().tolist())
    return attributes, float(np.mean(completeness_errors))


def aggregate_attributions(
    attributions: np.ndarray,
    transformed_feature_names: list[str],
    permutation_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    groups: dict[str, list[int]] = {}
    for index, transformed_name in enumerate(transformed_feature_names):
        groups.setdefault(original_feature_name(str(transformed_name)), []).append(index)
    permutation_map = {row["feature"]: row for row in permutation_rows}
    rows: list[dict[str, Any]] = []
    for feature, indexes in groups.items():
        feature_values = attributions[:, indexes]
        per_row_sum = feature_values.sum(axis=1)
        mean_abs = float(np.abs(per_row_sum).mean())
        rows.append({
            "feature": feature,
            "transformed_dimensions": len(indexes),
            "mean_absolute_integrated_gradient": mean_abs,
            "mean_signed_integrated_gradient": float(per_row_sum.mean()),
            "median_absolute_integrated_gradient": float(np.median(np.abs(per_row_sum))),
            "p95_absolute_integrated_gradient": float(np.quantile(np.abs(per_row_sum), 0.95)),
            "permutation_race_brier_increase": permutation_map.get(feature, {}).get("mean_race_brier_increase"),
            "permutation_top_pick_rate_decrease": permutation_map.get(feature, {}).get("mean_top_pick_win_rate_decrease"),
            "permutation_rank": None,
        })
    rows.sort(key=lambda item: (-item["mean_absolute_integrated_gradient"], item["feature"]))
    total = sum(row["mean_absolute_integrated_gradient"] for row in rows)
    for rank, row in enumerate(rows, start=1):
        row["integrated_gradient_rank"] = rank
        row["integrated_gradient_share"] = row["mean_absolute_integrated_gradient"] / total if total else 0.0
    ranked_permutation = sorted(
        (row for row in rows if row["permutation_race_brier_increase"] is not None),
        key=lambda item: (-float(item["permutation_race_brier_increase"]), item["feature"]),
    )
    for rank, row in enumerate(ranked_permutation, start=1):
        row["permutation_rank"] = rank
    for row in rows:
        permutation_rank = row["permutation_rank"]
        row["consensus_rank_score"] = row["integrated_gradient_rank"] + (permutation_rank if permutation_rank is not None else len(rows) + 1)
    return sorted(rows, key=lambda item: (item["consensus_rank_score"], item["feature"]))


def course_stability(
    frame: pd.DataFrame, attributions: np.ndarray, transformed_feature_names: list[str], core_features: list[str]) -> list[dict[str, Any]]:
    groups: dict[str, list[int]] = {}
    for index, transformed_name in enumerate(transformed_feature_names):
        groups.setdefault(original_feature_name(str(transformed_name)), []).append(index)
    rows: list[dict[str, Any]] = []
    for course, indexes in frame.groupby("racecourse", sort=True).groups.items():
        values = attributions[np.asarray(list(indexes), dtype=int)]
        feature_means = {feature: float(np.abs(values[:, groups[feature]].sum(axis=1)).mean()) for feature in core_features}
        for feature, importance in feature_means.items():
            rows.append({"racecourse": str(course), "feature": feature, "mean_absolute_integrated_gradient": importance})
    return rows


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# N6 生產模型特徵歸因報告",
        "",
        "> 分析對象為目前由 N6 服務載入的 74 維 MLP。報告使用 Integrated Gradients（IG）解釋預校準 raw logit，並以先前同一保留測試期的場內置換 Brier 重要性作獨立交叉檢查。IG 是梯度式歸因，不是精確 SHAP 值；結果解釋歷史模型行為，不構成未來賽果或回報保證。",
        "",
        "## 方法與完整性檢核",
        "",
        f"以訓練期轉換後特徵平均作基準點，對 {report['sample']['test_rows']:,} 個保留測試資料列進行 {INTEGRATION_STEPS} 階 trapezoidal Integrated Gradients。平均 completeness 誤差為 **{report['integrated_gradients']['mean_absolute_completeness_error']:.8f}**；此數值愈接近零，代表單列歸因總和愈能重建 baseline 到模型 logit 的輸出差。",
        "",
        "## 前五個核心特徵",
        "",
        "| 共識排名 | 特徵 | IG 排名 | 平均絕對 IG | IG 佔比 | 置換排名 | 場內 Brier 增幅 |",
        "| ---: | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for index, row in enumerate(report["top_five_consensus_features"], start=1):
        permutation_rank = "—" if row["permutation_rank"] is None else str(row["permutation_rank"])
        permutation_delta = "—" if row["permutation_race_brier_increase"] is None else f"{row['permutation_race_brier_increase']:+.6f}"
        lines.append(f"| {index} | `{row['feature']}` | {row['integrated_gradient_rank']} | {row['mean_absolute_integrated_gradient']:.6f} | {row['integrated_gradient_share']:.2%} | {permutation_rank} | {permutation_delta} |")
    lines.extend([
        "",
        "## 歸因與置換重要性前十",
        "",
        "| 特徵 | IG 排名 | 平均絕對 IG | 置換排名 | Brier 增幅 | 共識分數 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ])
    for row in report["feature_attributions"][:10]:
        permutation_rank = "—" if row["permutation_rank"] is None else str(row["permutation_rank"])
        permutation_delta = "—" if row["permutation_race_brier_increase"] is None else f"{row['permutation_race_brier_increase']:+.6f}"
        lines.append(f"| `{row['feature']}` | {row['integrated_gradient_rank']} | {row['mean_absolute_integrated_gradient']:.6f} | {permutation_rank} | {permutation_delta} | {row['consensus_rank_score']} |")
    lines.extend([
        "",
        "## 限制",
        "",
        "IG 衡量某一基準點到資料列輸入的模型輸出貢獻，不衡量因果效應；高度相關的特徵（例如不同的賠率轉換）會分攤或重複歸因。置換重要性衡量的是打亂特徵後的預測品質惡化，較接近模型依賴程度，但也受相關特徵替代效應影響。因此，本報告以兩種方法均表現突出的特徵作為核心排序，不宜以單一指標調整生產模型。",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    bundle = load_model_bundle(MODEL_PATH, PREPROCESSOR_PATH)
    train_frame, _, test_frame = chronological_split(load_training_frame())
    transformed_test = np.asarray(bundle.preprocessor.transform(test_frame[bundle.metadata["feature_contract"]]), dtype=np.float32)
    transformed_train = np.asarray(bundle.preprocessor.transform(train_frame[bundle.metadata["feature_contract"]]), dtype=np.float32)
    names = list(bundle.preprocessor.get_feature_names_out())
    if transformed_test.shape[1] != 74 or transformed_test.shape[1] != len(names):
        raise ValueError(f"Expected production 74-dimensional contract; received {transformed_test.shape[1]} dimensions and {len(names)} names.")
    baseline = transformed_train.mean(axis=0).astype(np.float32)
    attributions, completeness_error = integrated_gradients(bundle.model, transformed_test, baseline)
    permutation_report = json.loads(PRODUCTION_PERMUTATION_REPORT.read_text(encoding="utf-8"))
    rows = aggregate_attributions(attributions, names, permutation_report["permutation_feature_importance"])
    core_features = [row["feature"] for row in rows[:5]]
    stability_rows = course_stability(test_frame.reset_index(drop=True), attributions, names, core_features)
    report = {
        "engine": "N6 Neural Calculation Engine",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "production_artifact": str(MODEL_PATH),
        "method": {
            "primary": "Integrated Gradients on pre-calibration MLP raw logits",
            "steps": INTEGRATION_STEPS,
            "baseline": "mean transformed training-window feature vector",
            "secondary_cross_check": "within-race permutation race-level Brier importance from existing N6 interpretability report",
            "shap_package_installed": False,
        },
        "sample": {
            "test_rows": int(len(test_frame)),
            "test_races": int(test_frame["race_group"].nunique()),
            "test_from": str(test_frame.race_date.min().date()),
            "test_to": str(test_frame.race_date.max().date()),
            "transformed_dimensions": int(transformed_test.shape[1]),
        },
        "integrated_gradients": {"mean_absolute_completeness_error": completeness_error},
        "feature_attributions": rows,
        "top_five_consensus_features": rows[:5],
        "course_stability_for_top_five": stability_rows,
    }
    OUTPUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with OUTPUT_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    OUTPUT_MD.write_text(markdown_report(report), encoding="utf-8")
    print(json.dumps({
        "top_five_consensus_features": report["top_five_consensus_features"],
        "sample": report["sample"],
        "mean_absolute_completeness_error": completeness_error,
        "reports": [str(OUTPUT_JSON), str(OUTPUT_CSV), str(OUTPUT_MD)],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
