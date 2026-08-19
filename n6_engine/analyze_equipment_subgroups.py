#!/usr/bin/env python3
"""Cross-group analysis of N6's equipment_changed feature on the held-out period.

The program uses the saved N6 model and reads V10 only through N6's existing
immutable data loader. It never retrains, writes V10 data, or changes model
artifacts.  All odds below are historical source odds, not live odds.
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from analyze_model import chronological_test_split, predict_probabilities
from n6.config import MODEL_PATH, PREPROCESSOR_PATH, RANDOM_SEED, REPORTS_DIR, TARGET_COLUMN
from n6.feature_engineering import load_training_frame
from n6.model import load_model_bundle

ODDS_BINS = [1.0, 5.0, 10.0, 20.0, np.inf]
ODDS_LABELS = ["1–<5", "5–<10", "10–<20", "20+"]
MIN_RUNNERS_FOR_DIRECTIONAL_READING = 30
MIN_WINNERS_FOR_DIRECTIONAL_READING = 3
BOOTSTRAP_REPLICATES = 1000


def finite_number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) else None


def add_analysis_columns(frame: pd.DataFrame, probabilities: np.ndarray) -> pd.DataFrame:
    data = frame.copy()
    data["neural_probability"] = probabilities.astype(float)
    data["neural_rank"] = data.groupby("race_group", sort=False)["neural_probability"].rank(method="first", ascending=False).astype(int)
    data["equipment_changed_flag"] = pd.to_numeric(data["equipment_changed"], errors="coerce").fillna(0.0).gt(0.0)
    historical_odds = pd.to_numeric(data.get("win_odds"), errors="coerce")
    data["historical_odds"] = historical_odds.where(historical_odds > 1.0)
    data["odds_band"] = pd.cut(
        data["historical_odds"],
        bins=ODDS_BINS,
        labels=ODDS_LABELS,
        right=False,
        include_lowest=False,
    ).astype("object").fillna("無可用歷史賠率")
    data["equipment_status"] = np.where(data["equipment_changed_flag"], "有配備變動", "無配備變動")
    data["squared_error"] = (data["neural_probability"] - data[TARGET_COLUMN].astype(float)) ** 2
    return data


def group_metrics(data: pd.DataFrame, group_columns: list[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for keys, subset in data.groupby(group_columns, observed=False, dropna=False, sort=True):
        if not isinstance(keys, tuple):
            keys = (keys,)
        mapping = dict(zip(group_columns, keys))
        top_picks = subset[subset["neural_rank"] == 1]
        n = int(len(subset))
        wins = int(subset[TARGET_COLUMN].sum())
        actual_rate = float(wins / n) if n else None
        predicted_rate = float(subset["neural_probability"].mean()) if n else None
        record: dict[str, Any] = {
            **mapping,
            "runners": n,
            "races": int(subset["race_group"].nunique()),
            "winners": wins,
            "actual_win_rate": actual_rate,
            "mean_neural_probability": predicted_rate,
            "calibration_gap_probability_points": ((predicted_rate - actual_rate) * 100.0) if actual_rate is not None and predicted_rate is not None else None,
            "runner_brier_score": float(subset["squared_error"].mean()) if n else None,
            "top_pick_count": int(len(top_picks)),
            "top_pick_win_rate": float(top_picks[TARGET_COLUMN].mean()) if len(top_picks) else None,
            "sample_reading": "可作方向性比較" if n >= MIN_RUNNERS_FOR_DIRECTIONAL_READING and wins >= MIN_WINNERS_FOR_DIRECTIONAL_READING else "探索性；樣本或頭馬數偏少",
        }
        records.append(record)
    return records


def clustered_bootstrap_brier_delta(comparison: pd.DataFrame, generator: np.random.Generator) -> dict[str, float]:
    """Bootstrap Brier deltas by race, retaining within-race dependence."""
    work = comparison.copy()
    work["brier_delta"] = work["ablated_squared_error"] - work["base_squared_error"]
    race_summary = work.groupby("race_group", sort=False).agg(delta_sum=("brier_delta", "sum"), runner_count=("brier_delta", "size"))
    deltas = race_summary["delta_sum"].to_numpy(dtype=float)
    counts = race_summary["runner_count"].to_numpy(dtype=float)
    indices = generator.integers(0, len(deltas), size=(BOOTSTRAP_REPLICATES, len(deltas)))
    bootstrap = deltas[indices].sum(axis=1) / counts[indices].sum(axis=1)
    return {
        "bootstrap_ci95_lower": float(np.quantile(bootstrap, 0.025)),
        "bootstrap_ci95_upper": float(np.quantile(bootstrap, 0.975)),
        "bootstrap_positive_share": float(np.mean(bootstrap > 0.0)),
    }


def changed_group_ablation(bundle: Any, base: pd.DataFrame, group_columns: list[str]) -> list[dict[str, Any]]:
    """Remove the observed equipment-change flag for changed runners in each cell.

    The network is re-scored for the full affected races, preserving its native
    race-normalisation. Metrics are then evaluated only on that cell's changed
    runners. A positive Brier delta means ablation worsened historical accuracy.
    """
    changed = base[base["equipment_changed_flag"]].copy()
    rows: list[dict[str, Any]] = []
    generator = np.random.default_rng(RANDOM_SEED)
    for keys, subset in changed.groupby(group_columns, observed=False, dropna=False, sort=True):
        if subset.empty:
            continue
        if not isinstance(keys, tuple):
            keys = (keys,)
        mapping = dict(zip(group_columns, keys))
        affected_indices = subset.index
        ablated = base.copy()
        ablated.loc[affected_indices, "equipment_changed"] = 0.0
        ablated_probabilities = predict_probabilities(bundle, ablated)
        comparison = base.loc[affected_indices, ["race_group", TARGET_COLUMN, "neural_probability"]].copy()
        comparison["ablated_probability"] = pd.Series(ablated_probabilities, index=ablated.index).loc[affected_indices]
        comparison["base_squared_error"] = (comparison["neural_probability"] - comparison[TARGET_COLUMN].astype(float)) ** 2
        comparison["ablated_squared_error"] = (comparison["ablated_probability"] - comparison[TARGET_COLUMN].astype(float)) ** 2
        n = int(len(comparison))
        wins = int(comparison[TARGET_COLUMN].sum())
        base_brier = float(comparison["base_squared_error"].mean())
        ablated_brier = float(comparison["ablated_squared_error"].mean())
        brier_delta = ablated_brier - base_brier
        probability_shift_pp = float((comparison["neural_probability"] - comparison["ablated_probability"]).mean() * 100.0)
        stability = clustered_bootstrap_brier_delta(comparison, generator)
        if n >= MIN_RUNNERS_FOR_DIRECTIONAL_READING and wins >= MIN_WINNERS_FOR_DIRECTIONAL_READING and stability["bootstrap_ci95_lower"] > 0.0:
            stability_label = "穩健支持：95% CI 全為正"
        elif n >= MIN_RUNNERS_FOR_DIRECTIONAL_READING and wins >= MIN_WINNERS_FOR_DIRECTIONAL_READING and stability["bootstrap_ci95_upper"] < 0.0:
            stability_label = "穩健反證：95% CI 全為負"
        else:
            stability_label = "不確定：95% CI 跨越零或樣本偏少"
        rows.append({
            **mapping,
            "changed_runners": n,
            "changed_races": int(comparison["race_group"].nunique()),
            "changed_winners": wins,
            "base_runner_brier_score": base_brier,
            "ablated_runner_brier_score": ablated_brier,
            "ablation_brier_delta": brier_delta,
            "mean_probability_contribution_pp": probability_shift_pp,
            **stability,
            "stability_label": stability_label,
            "interpretation": "移除變動旗標後誤差上升：該子群組中模型現有用法與較佳歷史機率品質一致" if brier_delta > 0 else "移除變動旗標後誤差下降或持平：該子群組中現有用法未顯示穩健改善",
            "sample_reading": "可作方向性比較" if n >= MIN_RUNNERS_FOR_DIRECTIONAL_READING and wins >= MIN_WINNERS_FOR_DIRECTIONAL_READING else "探索性；樣本或頭馬數偏少",
        })
    return rows


def csv_write(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def percent(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value * 100.0:.{digits}f}%"


def signed(value: float | None, digits: int = 4) -> str:
    return "—" if value is None else f"{value:+.{digits}f}"


def markdown_report(
    base: pd.DataFrame,
    performance: list[dict[str, Any]],
    ablation: list[dict[str, Any]],
) -> str:
    changed = base[base["equipment_changed_flag"]]
    unchanged = base[~base["equipment_changed_flag"]]
    date_min = base["race_date"].min().strftime("%Y-%m-%d")
    date_max = base["race_date"].max().strftime("%Y-%m-%d")
    lines = [
        "# N6 配備變動子群組交叉分析",
        "",
        "> 本報告檢視已訓練 N6 模型在其**未回看時間序列測試期**中的歷史行為。所有賠率為 SQLite 留存的歷史 `win_odds`，不是即時賠率；結果描述模型在該樣本的關聯與誤差表現，不構成未來賽果、回報或因果保證。",
        "",
        "## 範圍與方法",
        "",
        f"分析樣本為 {date_min} 至 {date_max} 的 {base['race_group'].nunique():,} 場、{len(base):,} 匹資料列。`equipment_changed > 0` 定義為「有配備變動」；賠率區間使用 [1, 5)、[5, 10)、[10, 20)、[20, +∞)。",
        "",
        "表一比較同一馬場／賠率區間中的有、無配備變動馬匹之實際頭馬率、平均 N6 機率與 runner-level Brier。表二則進行模型內部消融：只把該交叉單元中『有配備變動』的旗標暫時設為零並重新作場內正規化。**消融 Brier Δ > 0** 表示移除該訊號後歷史誤差上升；反之則不支持該子群組的穩健改善。",
        "",
        "## 整體樣本",
        "",
        "| 狀態 | 馬匹列 | 場數 | 頭馬數 | 實際頭馬率 | 平均 N6 機率 | runner Brier |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label, subset in (("有配備變動", changed), ("無配備變動", unchanged)):
        n = len(subset)
        lines.append(
            f"| {label} | {n:,} | {subset['race_group'].nunique():,} | {int(subset[TARGET_COLUMN].sum()):,} | {percent(float(subset[TARGET_COLUMN].mean()))} | {percent(float(subset['neural_probability'].mean()))} | {float(subset['squared_error'].mean()):.5f} |"
        )
    lines.extend([
        "",
        "## 馬場 × 歷史賠率 × 配備狀態",
        "",
        "| 馬場 | 賠率區間 | 配備狀態 | 馬匹列 | 頭馬數 | 實際頭馬率 | 平均 N6 機率 | 校準差（pp） | Brier | 首選命中率 | 樣本判讀 |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ])
    for row in performance:
        lines.append(
            f"| {row['racecourse']} | {row['odds_band']} | {row['equipment_status']} | {row['runners']:,} | {row['winners']:,} | {percent(row['actual_win_rate'])} | {percent(row['mean_neural_probability'])} | {row['calibration_gap_probability_points']:+.2f} | {row['runner_brier_score']:.5f} | {percent(row['top_pick_win_rate'])} | {row['sample_reading']} |"
        )
    lines.extend([
        "",
        "## 有配備變動馬匹的條件消融",
        "",
        "| 馬場 | 賠率區間 | 變動馬匹列 | 頭馬數 | 原 Brier | 移除旗標後 Brier | 消融 Brier Δ | 95% CI | 正向 bootstrap 比率 | 平均機率貢獻（pp） | 穩定性 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | --- |",
    ])
    for row in ablation:
        lines.append(
            f"| {row['racecourse']} | {row['odds_band']} | {row['changed_runners']:,} | {row['changed_winners']:,} | {row['base_runner_brier_score']:.5f} | {row['ablated_runner_brier_score']:.5f} | {signed(row['ablation_brier_delta'], 5)} | [{row['bootstrap_ci95_lower']:+.5f}, {row['bootstrap_ci95_upper']:+.5f}] | {row['bootstrap_positive_share']:.1%} | {row['mean_probability_contribution_pp']:+.2f} | {row['stability_label']} |"
        )
    lines.extend([
        "",
        "## 判讀限制",
        "",
        "配備變動不是隨機實驗處置，可能與馬匹狀態、教練決策、賽事級別、臨場市場判斷及資料覆蓋度共同相關。因此，本分析不能把消融結果解讀為配備本身造成的因果效應。對少於 30 匹或少於 3 個頭馬的子群組，報告明確標為探索性，不宜據此調整模型權重或制定操作規則。",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    bundle = load_model_bundle(MODEL_PATH, PREPROCESSOR_PATH)
    test = chronological_test_split(load_training_frame())
    base = add_analysis_columns(test, predict_probabilities(bundle, test))
    performance = group_metrics(base, ["racecourse", "odds_band", "equipment_status"])
    ablation = changed_group_ablation(bundle, base, ["racecourse", "odds_band"])

    payload = {
        "engine": "N6 Neural Calculation Engine",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "source": "held-out chronological N6 test frame; V10 SQLite read-only through N6 loader",
        "odds_definition": {"source_column": "starters.win_odds", "valid_range": "odds > 1", "bands": ODDS_LABELS},
        "equipment_definition": "equipment_changed > 0",
        "sample_thresholds": {"minimum_runners": MIN_RUNNERS_FOR_DIRECTIONAL_READING, "minimum_winners": MIN_WINNERS_FOR_DIRECTIONAL_READING},
        "stability_method": {"bootstrap_replicates": BOOTSTRAP_REPLICATES, "resampling_unit": "race_group", "interval": "two-sided 95 percent percentile interval"},
        "overall": {
            "runners": int(len(base)),
            "races": int(base["race_group"].nunique()),
            "equipment_changed_runners": int(base["equipment_changed_flag"].sum()),
            "equipment_changed_winners": int(base.loc[base["equipment_changed_flag"], TARGET_COLUMN].sum()),
            "missing_historical_odds": int(base["historical_odds"].isna().sum()),
        },
        "performance_by_course_odds_equipment": performance,
        "equipment_changed_ablation_by_course_odds": ablation,
    }
    json_path = REPORTS_DIR / "n6_equipment_changed_cross_analysis.json"
    performance_csv = REPORTS_DIR / "n6_equipment_changed_cross_performance.csv"
    ablation_csv = REPORTS_DIR / "n6_equipment_changed_ablation.csv"
    markdown_path = REPORTS_DIR / "N6_EQUIPMENT_CHANGED_CROSS_ANALYSIS.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    csv_write(performance_csv, performance)
    csv_write(ablation_csv, ablation)
    markdown_path.write_text(markdown_report(base, performance, ablation), encoding="utf-8")
    print(json.dumps({
        "overall": payload["overall"],
        "performance_rows": performance,
        "ablation_rows": ablation,
        "reports": [str(json_path), str(performance_csv), str(ablation_csv), str(markdown_path)],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
