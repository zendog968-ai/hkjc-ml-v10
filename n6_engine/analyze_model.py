#!/usr/bin/env python3
"""Model-agnostic feature importance and neural-weight diagnostics for N6.

The script reads N6-owned artifacts and V10 data through N6's immutable SQLite
access helper.  It does not modify either the model or the V10 source database.
"""

from __future__ import annotations

import csv
import html
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import torch
from torch import nn

from n6.config import (
    ALL_FEATURES,
    CATEGORICAL_FEATURES,
    MODEL_PATH,
    NUMERIC_FEATURES,
    PREPROCESSOR_PATH,
    RANDOM_SEED,
    REPORTS_DIR,
    TARGET_COLUMN,
)
from n6.feature_engineering import load_training_frame, score_to_race_probabilities
from n6.model import load_model_bundle

PERMUTATION_REPEATS = 6
TOP_FEATURE_COUNT = 15


def chronological_test_split(frame: pd.DataFrame) -> pd.DataFrame:
    """Replicate N6's untouched final time window without retraining the model."""
    dates = sorted(frame["race_date"].dt.strftime("%Y-%m-%d").unique())
    valid_end = dates[max(2, int(len(dates) * 0.85)) - 1]
    test = frame[frame["race_date"] > valid_end].copy()
    if test.empty:
        raise ValueError("No chronological test rows are available for interpretation.")
    return test


def race_metrics(frame: pd.DataFrame, probabilities: np.ndarray) -> dict[str, float | int]:
    work = frame[["race_group", TARGET_COLUMN]].copy()
    work["probability"] = probabilities
    race_briers: list[float] = []
    top_pick: list[int] = []
    for _, race in work.groupby("race_group", sort=False):
        labels = race[TARGET_COLUMN].to_numpy(dtype=float)
        if labels.sum() != 1:
            continue
        scores = race["probability"].to_numpy(dtype=float)
        race_briers.append(float(np.sum((scores - labels) ** 2)))
        top_pick.append(int(race.iloc[int(np.argmax(scores))][TARGET_COLUMN] == 1))
    return {
        "races": len(race_briers),
        "mean_race_brier_score": float(np.mean(race_briers)),
        "top_pick_win_rate": float(np.mean(top_pick)),
    }


def predict_probabilities(bundle: Any, frame: pd.DataFrame) -> np.ndarray:
    values = np.asarray(bundle.preprocessor.transform(frame[ALL_FEATURES]), dtype=np.float32)
    with torch.no_grad():
        logits = bundle.model(torch.tensor(values, dtype=torch.float32)).cpu().numpy()
    temperature = float(bundle.metadata.get("temperature", 1.0))
    return score_to_race_probabilities(logits / temperature, frame["race_group"])


def permute_within_races(frame: pd.DataFrame, feature: str, generator: np.random.Generator) -> pd.DataFrame:
    """Shuffle a feature inside each race, preserving the race's field composition."""
    altered = frame.copy()
    values = altered[feature].copy()
    for _, indices in altered.groupby("race_group", sort=False).groups.items():
        positions = np.asarray(list(indices), dtype=int)
        values.loc[positions] = generator.permutation(values.loc[positions].to_numpy())
    altered[feature] = values
    return altered


def permutation_importance(bundle: Any, test: pd.DataFrame, baseline: dict[str, float | int]) -> list[dict[str, Any]]:
    rng = np.random.default_rng(RANDOM_SEED)
    results: list[dict[str, Any]] = []
    for feature in ALL_FEATURES:
        brier_changes: list[float] = []
        top_pick_changes: list[float] = []
        for _ in range(PERMUTATION_REPEATS):
            altered = permute_within_races(test, feature, rng)
            metrics = race_metrics(altered, predict_probabilities(bundle, altered))
            brier_changes.append(float(metrics["mean_race_brier_score"]) - float(baseline["mean_race_brier_score"]))
            top_pick_changes.append(float(baseline["top_pick_win_rate"]) - float(metrics["top_pick_win_rate"]))
        results.append({
            "feature": feature,
            "feature_type": "numeric" if feature in NUMERIC_FEATURES else "categorical",
            "permutation_repeats": PERMUTATION_REPEATS,
            "mean_race_brier_increase": float(np.mean(brier_changes)),
            "std_race_brier_increase": float(np.std(brier_changes, ddof=0)),
            "mean_top_pick_win_rate_decrease": float(np.mean(top_pick_changes)),
            "std_top_pick_win_rate_decrease": float(np.std(top_pick_changes, ddof=0)),
        })
    return sorted(results, key=lambda item: (-item["mean_race_brier_increase"], item["feature"]))


def original_feature_name(transformed_name: str) -> str:
    if transformed_name.startswith("missingindicator_"):
        return transformed_name.removeprefix("missingindicator_")
    for categorical in CATEGORICAL_FEATURES:
        if transformed_name.startswith(f"{categorical}_"):
            return categorical
    return transformed_name


def distribution(values: np.ndarray) -> dict[str, float | int]:
    flattened = np.asarray(values, dtype=float).ravel()
    absolute = np.abs(flattened)
    return {
        "parameters": int(flattened.size),
        "mean": float(flattened.mean()),
        "std": float(flattened.std(ddof=0)),
        "min": float(flattened.min()),
        "p01": float(np.quantile(flattened, 0.01)),
        "p05": float(np.quantile(flattened, 0.05)),
        "median": float(np.median(flattened)),
        "p95": float(np.quantile(flattened, 0.95)),
        "p99": float(np.quantile(flattened, 0.99)),
        "max": float(flattened.max()),
        "mean_absolute": float(absolute.mean()),
        "max_absolute": float(absolute.max()),
        "negative_share": float(np.mean(flattened < 0.0)),
        "positive_share": float(np.mean(flattened > 0.0)),
        "near_zero_share": float(np.mean(absolute < 1e-6)),
    }


def weight_diagnostics(bundle: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    linear_layers = [(name, module) for name, module in bundle.model.named_modules() if isinstance(module, nn.Linear)]
    layer_rows: list[dict[str, Any]] = []
    for index, (name, layer) in enumerate(linear_layers, start=1):
        weight_stats = distribution(layer.weight.detach().cpu().numpy())
        bias_stats = distribution(layer.bias.detach().cpu().numpy())
        layer_rows.append({
            "layer": f"linear_{index}",
            "module": name,
            "shape": list(layer.weight.shape),
            "weight_distribution": weight_stats,
            "bias_distribution": bias_stats,
        })

    transformed_names = list(bundle.preprocessor.get_feature_names_out())
    first_weight = linear_layers[0][1].weight.detach().cpu().numpy()
    if first_weight.shape[1] != len(transformed_names):
        raise ValueError("Preprocessor feature dimension does not match the MLP input layer.")
    grouped: dict[str, list[float]] = defaultdict(list)
    for transformed_name, column in zip(transformed_names, first_weight.T):
        grouped[original_feature_name(str(transformed_name))].append(float(np.abs(column).mean()))
    total_mass = sum(sum(values) for values in grouped.values())
    feature_weight_rows = [
        {
            "feature": name,
            "transformed_dimensions": len(values),
            "first_layer_mean_absolute_weight": float(np.mean(values)),
            "first_layer_weight_mass_share": float(sum(values) / total_mass) if total_mass else 0.0,
        }
        for name, values in grouped.items()
    ]
    feature_weight_rows.sort(key=lambda item: (-item["first_layer_weight_mass_share"], item["feature"]))
    return layer_rows, feature_weight_rows


def svg_feature_chart(rows: list[dict[str, Any]], path: Path) -> None:
    selected = rows[:10]
    width, height, left, top, chart_width, row_height = 1080, 430, 290, 75, 730, 29
    maximum = max((row["mean_race_brier_increase"] for row in selected), default=1.0) or 1.0
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="36" y="38" font-family="Arial, sans-serif" font-size="22" font-weight="700" fill="#14213d">N6: Held-out Race-wise Permutation Feature Importance</text>',
        '<text x="36" y="61" font-family="Arial, sans-serif" font-size="13" fill="#64748b">Longer bars indicate a larger mean worsening in race-level Brier score.</text>',
    ]
    for index, row in enumerate(selected):
        y = top + index * row_height
        value = float(row["mean_race_brier_increase"])
        bar_width = max(0.0, value / maximum * chart_width)
        parts.append(f'<text x="{left - 12}" y="{y + 17}" text-anchor="end" font-family="Arial, sans-serif" font-size="14" fill="#334155">{html.escape(str(row["feature"]))}</text>')
        parts.append(f'<rect x="{left}" y="{y + 3}" width="{bar_width:.2f}" height="18" rx="3" fill="#2563eb"/>')
        parts.append(f'<text x="{left + bar_width + 8:.2f}" y="{y + 17}" font-family="Arial, sans-serif" font-size="13" fill="#0f172a">{value:+.5f}</text>')
    parts.append('</svg>')
    path.write_text("\n".join(parts), encoding="utf-8")


def svg_weight_chart(layer_rows: list[dict[str, Any]], bundle: Any, path: Path) -> None:
    linear_layers = [module for module in bundle.model.modules() if isinstance(module, nn.Linear)]
    weights = [layer.weight.detach().cpu().numpy().ravel() for layer in linear_layers]
    global_max = max(float(np.abs(values).max()) for values in weights)
    width, height, chart_width, chart_height, top = 1080, 390, 220, 200, 105
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="36" y="38" font-family="Arial, sans-serif" font-size="22" font-weight="700" fill="#14213d">N6: Linear-layer Weight Distribution</text>',
        '<text x="36" y="61" font-family="Arial, sans-serif" font-size="13" fill="#64748b">Histograms share a symmetric axis; concentration near zero indicates mostly small connections.</text>',
    ]
    for layer_index, (row, values) in enumerate(zip(layer_rows, weights)):
        x0 = 36 + layer_index * 260
        counts, _ = np.histogram(values, bins=24, range=(-global_max, global_max))
        maximum = max(int(counts.max()), 1)
        parts.append(f'<text x="{x0}" y="88" font-family="Arial, sans-serif" font-size="15" font-weight="700" fill="#334155">{html.escape(row["layer"])} {html.escape(str(row["shape"]))}</text>')
        parts.append(f'<line x1="{x0}" y1="{top + chart_height}" x2="{x0 + chart_width}" y2="{top + chart_height}" stroke="#94a3b8"/>')
        for bin_index, count in enumerate(counts):
            bar_width = chart_width / len(counts)
            bar_height = (int(count) / maximum) * chart_height
            x = x0 + bin_index * bar_width
            y = top + chart_height - bar_height
            color = "#60a5fa" if bin_index < len(counts) / 2 else "#2563eb"
            parts.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{max(bar_width - 1, 1):.2f}" height="{bar_height:.2f}" fill="{color}"/>')
        parts.append(f'<text x="{x0}" y="{top + chart_height + 22}" font-family="Arial, sans-serif" font-size="11" fill="#64748b">−{global_max:.2f}</text>')
        parts.append(f'<text x="{x0 + chart_width / 2:.2f}" y="{top + chart_height + 22}" text-anchor="middle" font-family="Arial, sans-serif" font-size="11" fill="#64748b">0</text>')
        parts.append(f'<text x="{x0 + chart_width:.2f}" y="{top + chart_height + 22}" text-anchor="end" font-family="Arial, sans-serif" font-size="11" fill="#64748b">+{global_max:.2f}</text>')
    parts.append('</svg>')
    path.write_text("\n".join(parts), encoding="utf-8")


def markdown_report(report: dict[str, Any], top_rows: list[dict[str, Any]], layer_rows: list[dict[str, Any]]) -> str:
    baseline = report["baseline_test_metrics"]
    lines = [
        "# N6 模型可解釋性報告",
        "",
        "> 本報告以 N6 的未回看時間序列測試期進行**場內擾動特徵重要性**分析，並彙總已訓練 MLP 的權重分佈。這些數值解釋既有模型在歷史資料上的行為，不代表未來賽果或回報保證。",
        "",
        "## 分析設定",
        "",
        f"測試期共有 {baseline['races']} 場單一頭馬賽事；基準 race-level Brier 為 **{baseline['mean_race_brier_score']:.6f}**，首選命中率為 **{baseline['top_pick_win_rate']:.2%}**。每一特徵在每場內隨機打亂 {PERMUTATION_REPEATS} 次，以保留同場馬匹組合；Brier 增幅愈高，表示該特徵對場內排序愈重要。",
        "",
        "## 前三項特徵",
        "",
        "| 排名 | 特徵 | Brier 平均增幅 | 首選命中率平均跌幅 | 第一層權重質量佔比 |",
        "| ---: | --- | ---: | ---: | ---: |",
    ]
    weight_map = {row["feature"]: row for row in report["first_layer_feature_weights"]}
    for index, row in enumerate(top_rows[:3], start=1):
        weight = weight_map.get(row["feature"], {})
        lines.append(
            f"| {index} | `{row['feature']}` | {row['mean_race_brier_increase']:+.6f} | {row['mean_top_pick_win_rate_decrease']:+.2%} | {weight.get('first_layer_weight_mass_share', 0.0):.2%} |"
        )
    lines.extend([
        "",
        "## 前十項擾動重要性",
        "",
        "| 排名 | 特徵 | 類型 | Brier 平均增幅 | Brier 標準差 |",
        "| ---: | --- | --- | ---: | ---: |",
    ])
    for index, row in enumerate(top_rows[:10], start=1):
        lines.append(f"| {index} | `{row['feature']}` | {row['feature_type']} | {row['mean_race_brier_increase']:+.6f} | {row['std_race_brier_increase']:.6f} |")
    lines.extend([
        "",
        "## 神經網絡權重分佈",
        "",
        "| 層 | 形狀 | 參數數 | 平均 | 標準差 | 平均絕對值 | 近零權重佔比 | 正／負比重 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ])
    for row in layer_rows:
        stats = row["weight_distribution"]
        lines.append(
            f"| `{row['layer']}` | `{row['shape']}` | {stats['parameters']:,} | {stats['mean']:+.5f} | {stats['std']:.5f} | {stats['mean_absolute']:.5f} | {stats['near_zero_share']:.2%} | {stats['positive_share']:.2%}／{stats['negative_share']:.2%} |"
        )
    lines.extend([
        "",
        "## 解讀限制",
        "",
        "置換重要性衡量的是『在此已訓練模型與保留測試期中，破壞該特徵後的績效變化』，不是因果效應，亦不等同於單一權重大小。第一層權重質量只反映輸入層的參數規模；由於後續 LayerNorm、GELU 與非線性層會產生交互作用，最終解讀應優先採用擾動重要性。",
        "",
        "## 產物",
        "",
        "- `n6_feature_importance.csv`：全部原始特徵的擾動結果。",
        "- `n6_model_weight_distribution.csv`：各線性層與 bias 的描述統計。",
        "- `n6_feature_importance.svg`、`n6_weight_distribution.svg`：對應視覺化。",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    bundle = load_model_bundle(MODEL_PATH, PREPROCESSOR_PATH)
    frame = load_training_frame()
    test = chronological_test_split(frame)
    baseline_probabilities = predict_probabilities(bundle, test)
    baseline = race_metrics(test, baseline_probabilities)
    importance_rows = permutation_importance(bundle, test, baseline)
    layer_rows, input_weight_rows = weight_diagnostics(bundle)

    report: dict[str, Any] = {
        "engine": "N6 Neural Calculation Engine",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "model_artifact": str(MODEL_PATH),
        "analysis_method": {
            "feature_importance": "within-race permutation importance on N6 chronological held-out test window",
            "permutation_repeats": PERMUTATION_REPEATS,
            "primary_metric": "mean race-level Brier score increase",
            "secondary_metric": "top-pick win-rate decrease",
            "weight_diagnostics": "descriptive statistics for trained Linear layer weights and biases",
        },
        "baseline_test_metrics": baseline,
        "top_three_features": importance_rows[:3],
        "permutation_feature_importance": importance_rows,
        "first_layer_feature_weights": input_weight_rows,
        "linear_layer_distributions": layer_rows,
    }
    json_path = REPORTS_DIR / "n6_interpretability_report.json"
    csv_path = REPORTS_DIR / "n6_feature_importance.csv"
    weight_csv_path = REPORTS_DIR / "n6_model_weight_distribution.csv"
    markdown_path = REPORTS_DIR / "N6_MODEL_INTERPRETABILITY.md"
    feature_svg_path = REPORTS_DIR / "n6_feature_importance.svg"
    weight_svg_path = REPORTS_DIR / "n6_weight_distribution.svg"

    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(importance_rows[0]))
        writer.writeheader()
        writer.writerows(importance_rows)
    flattened_layers = []
    for row in layer_rows:
        for distribution_type in ("weight_distribution", "bias_distribution"):
            flattened_layers.append({"layer": row["layer"], "module": row["module"], "shape": "x".join(map(str, row["shape"])), "parameter_type": distribution_type.removesuffix("_distribution"), **row[distribution_type]})
    with weight_csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(flattened_layers[0]))
        writer.writeheader()
        writer.writerows(flattened_layers)
    markdown_path.write_text(markdown_report(report, importance_rows, layer_rows), encoding="utf-8")
    svg_feature_chart(importance_rows, feature_svg_path)
    svg_weight_chart(layer_rows, bundle, weight_svg_path)
    print(json.dumps({
        "top_three_features": importance_rows[:3],
        "baseline_test_metrics": baseline,
        "reports": [str(json_path), str(csv_path), str(weight_csv_path), str(markdown_path), str(feature_svg_path), str(weight_svg_path)],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
