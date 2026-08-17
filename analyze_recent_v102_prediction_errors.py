#!/usr/bin/env python3
"""Error analysis for recent, settled V10.2 probability prediction artifacts.

The analysis scores only pre-existing probability rows from an input CSV. It uses
``target_win`` solely as a settled post-race label and optionally enriches groups
with already-stored local official race metadata. It never retrains a model or
recreates pre-race features from post-race outcomes.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402


DEFAULT_PREDICTIONS = "v102_multiseason_backtest_predictions.csv"
DEFAULT_DB = "hkjc_last_season.sqlite"
DEFAULT_OUTPUT_DIR = "archive/error_analysis/recent_v102"
REQUIRED_COLUMNS = {"race_date", "racecourse", "race_no", "horse_name", "target_win"}


def args_parser() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Diagnose high-Brier and Top-1-loss races from saved V10.2 predictions.")
    parser.add_argument("--predictions", default=DEFAULT_PREDICTIONS)
    parser.add_argument("--db", default=DEFAULT_DB, help="Optional local SQLite database for official race-condition enrichment.")
    parser.add_argument("--probability-column", default="race_normalized_probability")
    parser.add_argument("--recent-races", type=int, default=50)
    parser.add_argument("--recent-days", type=int, help="Mutually exclusive with --recent-races; anchored to artifact latest race date.")
    parser.add_argument("--high-brier-quantile", type=float, default=0.80, help="Quantile threshold in (0, 1); default top 20%% Brier cases.")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def hkt_now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S HKT")


def parse_date(value: Any) -> date | None:
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def num(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (ValueError, TypeError):
        return None
    return parsed if math.isfinite(parsed) else None


def int_label(value: Any) -> int | None:
    parsed = num(value)
    return int(parsed) if parsed in (0.0, 1.0) else None


def group_id(row: pd.Series) -> str:
    supplied = str(row.get("race_group", "")).strip()
    if supplied and supplied.lower() != "nan":
        return supplied
    return "|".join(str(row.get(key, "")).strip() for key in ("race_date", "racecourse", "race_no"))


def load_prediction_artifact(path: Path, probability_column: str) -> pd.DataFrame:
    if not path.exists():
        raise ValueError(f"找不到預測工件：{path}")
    frame = pd.read_csv(path, encoding="utf-8-sig", dtype=str)
    frame.columns = [str(column).lstrip("\ufeff").strip() for column in frame.columns]
    missing = REQUIRED_COLUMNS - set(frame.columns)
    if missing:
        raise ValueError(f"預測工件缺少必要欄位：{', '.join(sorted(missing))}")
    if probability_column not in frame.columns:
        raise ValueError(f"預測工件缺少機率欄位：{probability_column}")
    frame["race_date_parsed"] = frame["race_date"].map(parse_date)
    frame["group_id"] = frame.apply(group_id, axis=1)
    frame["probability"] = frame[probability_column].map(num)
    frame["target"] = frame["target_win"].map(int_label)
    frame["horse_name"] = frame["horse_name"].fillna("").astype(str).str.strip()
    return frame


def select_recent_groups(frame: pd.DataFrame, recent_races: int, recent_days: int | None) -> list[str]:
    meta = frame.dropna(subset=["race_date_parsed"]).drop_duplicates("group_id")[["group_id", "race_date_parsed", "racecourse", "race_no"]].copy()
    if meta.empty:
        raise ValueError("預測工件沒有可解析的 race_date。")
    meta["race_no_sort"] = pd.to_numeric(meta["race_no"], errors="coerce").fillna(9999)
    meta = meta.sort_values(["race_date_parsed", "racecourse", "race_no_sort", "group_id"], ascending=[False, False, False, False])
    if recent_days is not None:
        if recent_days < 1:
            raise ValueError("--recent-days 必須為正整數。")
        latest = meta.iloc[0]["race_date_parsed"]
        cutoff = latest - timedelta(days=recent_days - 1)
        return meta.loc[meta["race_date_parsed"] >= cutoff, "group_id"].tolist()
    if recent_races < 1:
        raise ValueError("--recent-races 必須為正整數。")
    return meta.head(recent_races)["group_id"].tolist()


def evaluate_group(group: pd.DataFrame) -> dict[str, Any]:
    ordered_source = group.copy()
    race_date = str(ordered_source.iloc[0]["race_date"])
    racecourse = str(ordered_source.iloc[0]["racecourse"])
    race_no = str(ordered_source.iloc[0]["race_no"])
    base: dict[str, Any] = {
        "group_id": str(ordered_source.iloc[0]["group_id"]),
        "race_date": race_date,
        "racecourse": racecourse,
        "race_no": race_no,
        "field_size": len(ordered_source),
        "status": "evaluated",
        "reason": None,
    }
    if len(group) < 2:
        return {**base, "status": "excluded", "reason": "field_size_lt_2"}
    horses = group["horse_name"].tolist()
    if any(not name for name in horses):
        return {**base, "status": "excluded", "reason": "missing_horse_name"}
    if len(set(horses)) != len(horses):
        return {**base, "status": "excluded", "reason": "duplicate_horse_name"}
    probabilities = group["probability"].tolist()
    if any(value is None or pd.isna(value) for value in probabilities):
        return {**base, "status": "excluded", "reason": "missing_or_nonfinite_probability"}
    if any(value < 0 or value > 1 for value in probabilities):
        return {**base, "status": "excluded", "reason": "probability_out_of_range"}
    if abs(sum(probabilities) - 1.0) > 1e-6:
        return {**base, "status": "excluded", "reason": "probability_sum_not_one"}
    targets = group["target"].tolist()
    if any(value is None or pd.isna(value) for value in targets) or sum(targets) != 1:
        return {**base, "status": "excluded", "reason": "winner_count_not_one"}
    ordered = group.sort_values(["probability", "horse_name"], ascending=[False, True]).reset_index(drop=True)
    winner = group.loc[group["target"] == 1].iloc[0]
    winner_rank = int(ordered.index[ordered["horse_name"] == winner["horse_name"]][0]) + 1
    top = ordered.iloc[0]
    brier = float(sum((group["probability"] - group["target"].astype(float)) ** 2))
    uniform_brier = float(sum((1.0 / len(group) - group["target"].astype(float)) ** 2))
    return {
        **base,
        "winner": winner["horse_name"],
        "winner_probability": float(winner["probability"]),
        "winner_model_rank": winner_rank,
        "top_pick": top["horse_name"],
        "top_pick_probability": float(top["probability"]),
        "top_pick_won": bool(top["horse_name"] == winner["horse_name"]),
        "top3_contains_winner": bool(winner_rank <= 3),
        "brier_score": brier,
        "uniform_brier_score": uniform_brier,
        "brier_excess_vs_uniform": brier - uniform_brier,
        "probability_gap_top1_top2": float(ordered.iloc[0]["probability"] - ordered.iloc[1]["probability"]),
    }


def optional_race_metadata(db_path: Path, groups: pd.DataFrame) -> pd.DataFrame:
    fallback = groups[["race_date", "racecourse", "race_no"]].drop_duplicates().copy()
    if not db_path.exists():
        fallback["metadata_status"] = "db_unavailable"
        return fallback
    try:
        with sqlite3.connect(db_path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(races)").fetchall()}
            desired = [column for column in ("race_date", "racecourse", "race_no", "distance_m", "surface", "course_config", "going", "race_class") if column in columns]
            if not {"race_date", "racecourse", "race_no"}.issubset(desired):
                fallback["metadata_status"] = "race_keys_missing"
                return fallback
            query = f"SELECT {', '.join(desired)} FROM races"
            metadata = pd.read_sql_query(query, conn, dtype=str)
    except sqlite3.Error:
        fallback["metadata_status"] = "db_query_failed"
        return fallback
    metadata["metadata_status"] = "matched"
    return metadata


def confidence_band(probability: float) -> str:
    if probability < 0.10:
        return "<10%"
    if probability < 0.15:
        return "10–<15%"
    if probability < 0.20:
        return "15–<20%"
    return "≥20%"


def rank_band(rank: int) -> str:
    return "1" if rank == 1 else "2–3" if rank <= 3 else "4–6" if rank <= 6 else "7+"


def aggregate_strata(cases: pd.DataFrame, column: str) -> list[dict[str, Any]]:
    if column not in cases.columns:
        return []
    work = cases.copy()
    work[column] = work[column].fillna("N/A").astype(str).replace("", "N/A")
    result = []
    for value, group in work.groupby(column, dropna=False):
        evaluated = group[group["status"] == "evaluated"]
        if evaluated.empty:
            continue
        result.append(
            {
                "stratum": str(value),
                "races": int(len(evaluated)),
                "top1_win_rate": float(evaluated["top_pick_won"].mean()),
                "mean_brier": float(evaluated["brier_score"].mean()),
                "mean_brier_excess_vs_uniform": float(evaluated["brier_excess_vs_uniform"].mean()),
                "high_brier_races": int(evaluated["high_brier"].sum()),
            }
        )
    return sorted(result, key=lambda item: (-item["races"], item["stratum"]))


def set_cjk_font() -> None:
    candidates = ["Noto Sans CJK TC", "Noto Sans CJK SC", "Noto Sans CJK JP", "sans-serif"]
    available = {font.name for font in font_manager.fontManager.ttflist}
    for candidate in candidates:
        if candidate in available:
            plt.rcParams["font.family"] = candidate
            break
    plt.rcParams["axes.unicode_minus"] = False


def make_charts(cases: pd.DataFrame, output_dir: Path) -> list[str]:
    set_cjk_font()
    evaluated = cases[cases["status"] == "evaluated"].copy()
    if evaluated.empty:
        return []
    paths = []
    scatter_path = output_dir / "top_pick_probability_vs_brier.png"
    fig, ax = plt.subplots(figsize=(8, 5.2))
    lost = evaluated[~evaluated["top_pick_won"]]
    won = evaluated[evaluated["top_pick_won"]]
    ax.scatter(lost["top_pick_probability"], lost["brier_score"], color="#C53A2B", label="Top-1 落敗", alpha=0.75)
    ax.scatter(won["top_pick_probability"], won["brier_score"], color="#167C5A", label="Top-1 勝出", alpha=0.85)
    if evaluated["high_brier"].any():
        threshold = evaluated.loc[evaluated["high_brier"], "brier_score"].min()
        ax.axhline(threshold, color="#7B61A8", linestyle="--", linewidth=1, label="高 Brier 門檻")
    ax.set_title("首選機率與場內 Brier Score")
    ax.set_xlabel("首選預測機率")
    ax.set_ylabel("場內 Brier Score（越低越好）")
    ax.legend(frameon=False)
    ax.grid(alpha=0.2)
    fig.tight_layout()
    fig.savefig(scatter_path, dpi=180)
    plt.close(fig)
    paths.append(scatter_path.name)

    course = evaluated.groupby("racecourse").agg(races=("group_id", "count"), mean_brier=("brier_score", "mean"), top1=("top_pick_won", "mean")).reset_index()
    if not course.empty:
        bar_path = output_dir / "course_error_profile.png"
        fig, ax1 = plt.subplots(figsize=(7.6, 5.2))
        x = np.arange(len(course))
        ax1.bar(x - 0.18, course["mean_brier"], width=0.36, color="#3E6D9C", label="平均 Brier")
        ax1.set_ylabel("平均 Brier Score（越低越好）")
        ax1.set_xticks(x, course["racecourse"])
        ax2 = ax1.twinx()
        ax2.bar(x + 0.18, course["top1"] * 100, width=0.36, color="#D38B32", label="Top-1 勝出率")
        ax2.set_ylabel("Top-1 勝出率（%）")
        ax1.set_title("按馬場的近期錯誤概況")
        handles1, labels1 = ax1.get_legend_handles_labels()
        handles2, labels2 = ax2.get_legend_handles_labels()
        ax1.legend(handles1 + handles2, labels1 + labels2, frameon=False, loc="upper left")
        fig.tight_layout()
        fig.savefig(bar_path, dpi=180)
        plt.close(fig)
        paths.append(bar_path.name)
    return paths


def markdown_table(rows: list[dict[str, Any]], headers: list[tuple[str, str]], decimals: set[str] | None = None) -> list[str]:
    decimals = decimals or set()
    lines = ["| " + " | ".join(title for _, title in headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        values = []
        for key, _ in headers:
            value = row.get(key)
            if value is None or (isinstance(value, float) and math.isnan(value)):
                text = "N/A"
            elif key in decimals:
                text = f"{float(value):.4f}"
            elif key.endswith("_rate"):
                text = f"{100 * float(value):.2f}%"
            else:
                text = str(value)
            values.append(text.replace("|", "\\|"))
        lines.append("| " + " | ".join(values) + " |")
    return lines


def render_report(summary: dict[str, Any], cases: pd.DataFrame, charts: list[str]) -> str:
    evaluated = cases[cases["status"] == "evaluated"].copy()
    high = evaluated[evaluated["high_brier"]].sort_values("brier_score", ascending=False)
    top1_losses = evaluated[~evaluated["top_pick_won"]].sort_values("brier_score", ascending=False)
    lines = [
        "# V10.2 近期預測錯誤分析",
        "",
        "> **研究範圍：** 本報告只分析已保存的賽前機率工件與其事後 `target_win` 結果。它不會重建賽前特徵或改寫模型；因此所有結論均是後驗診斷，而非自動調參依據。",
        "",
        "## 樣本與總覽",
        "",
        "| 指標 | 值 |",
        "|---|---:|",
        f"| 預測工件 | `{summary['input']['predictions']}` |",
        f"| 時間窗口 | {summary['window']} |",
        f"| 資料日期 | {summary['date_range']} |",
        f"| 已評估賽事 | {summary['evaluated_races']} |",
        f"| Top-1 落敗 | {summary['top1_losses']} ({summary['top1_loss_rate'] * 100:.2f}%) |",
        f"| 高 Brier 場次 | {summary['high_brier_races']}（≥ 第 {summary['high_brier_quantile'] * 100:.0f} 百分位，門檻 {summary['high_brier_threshold']:.4f}） |",
        f"| 平均 Brier | {summary['mean_brier']:.4f} |",
        f"| 均勻基準 Brier | {summary['mean_uniform_brier']:.4f} |",
        f"| 平均 Brier 改善 | {summary['mean_uniform_brier'] - summary['mean_brier']:.4f} |",
        "",
    ]
    if summary["evaluated_races"] < 15:
        lines.extend(["> **探索性樣本警告：** 少於 15 場，不能用於結論性比較或模型調參。", ""])
    lines.extend(["## 高 Brier 場次", ""])
    high_rows = high.head(12).to_dict("records")
    lines.extend(markdown_table(high_rows, [
        ("race_date", "日期"), ("racecourse", "馬場"), ("race_no", "場次"), ("field_size", "出賽"),
        ("top_pick", "Top-1"), ("top_pick_probability", "Top-1 機率"), ("winner", "頭馬"),
        ("winner_model_rank", "頭馬排名"), ("winner_probability", "頭馬機率"), ("brier_score", "Brier"),
        ("going", "場地狀況"),
    ], decimals={"top_pick_probability", "winner_probability", "brier_score"}))
    lines.extend(["", "## Top-1 落敗中誤差最高的場次", ""])
    loss_rows = top1_losses.head(12).to_dict("records")
    lines.extend(markdown_table(loss_rows, [
        ("race_date", "日期"), ("racecourse", "馬場"), ("race_no", "場次"), ("top_pick", "Top-1"),
        ("top_pick_probability", "Top-1 機率"), ("probability_gap_top1_top2", "首二差距"),
        ("winner", "頭馬"), ("winner_model_rank", "頭馬排名"), ("brier_score", "Brier"),
    ], decimals={"top_pick_probability", "probability_gap_top1_top2", "brier_score"}))
    lines.extend(["", "## 分層診斷", ""])
    for title, rows in summary["strata"].items():
        lines.extend([f"### {title}", ""])
        lines.extend(markdown_table(rows, [
            ("stratum", "組別"), ("races", "賽事"), ("top1_win_rate", "Top-1 勝出率"),
            ("mean_brier", "平均 Brier"), ("mean_brier_excess_vs_uniform", "相對均勻誤差"), ("high_brier_races", "高 Brier 場次"),
        ], decimals={"mean_brier", "mean_brier_excess_vs_uniform"}))
        lines.append("")
    lines.extend(["## 診斷圖表", ""])
    for chart in charts:
        lines.extend([f"![{chart}]({chart})", ""])
    lines.extend([
        "## 保守解讀與後續驗證",
        "",
        "高 Brier 通常表示模型對錯誤候選給予過多機率，或低估真正頭馬；它不是單獨的下注訊號。應先用持續擴大的時間外樣本重複驗證同一條件組別，再考慮特徵、校準或模型權重修改。",
        "",
        "若 Top-1 機率低而首二差距小，Top-1 落敗通常反映場內本來就缺少明確優勢，不能把它解讀為特定馬匹／騎師／賽道因素失效。若高 Brier 在相同官方場地、距離或班次中跨多個獨立賽日穩定重現，才應把該組別列入下一輪時間序列驗證候選。",
        "",
        "本次分層只使用本地 SQLite 已保存的官方賽事條件；若資料庫未匹配，表中標記為 N/A，而不猜測環境因素。",
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    args = args_parser()
    if args.recent_days is not None and args.recent_races != 50:
        print(json.dumps({"status": "input_error", "error": "使用 --recent-days 時不要同時自訂 --recent-races。"}, ensure_ascii=False))
        return 2
    if not 0 < args.high_brier_quantile < 1:
        print(json.dumps({"status": "input_error", "error": "--high-brier-quantile 必須介乎 0 與 1。"}, ensure_ascii=False))
        return 2
    try:
        frame = load_prediction_artifact(Path(args.predictions), args.probability_column)
        selected_ids = select_recent_groups(frame, args.recent_races, args.recent_days)
    except ValueError as exc:
        print(json.dumps({"status": "input_error", "error": str(exc)}, ensure_ascii=False))
        return 2
    selected = frame[frame["group_id"].isin(selected_ids)].copy()
    cases = pd.DataFrame([evaluate_group(group) for _, group in selected.groupby("group_id", sort=False)])
    metadata = optional_race_metadata(Path(args.db), cases)
    for column in ("race_date", "racecourse", "race_no"):
        cases[column] = cases[column].astype(str)
        metadata[column] = metadata[column].astype(str)
    cases = cases.merge(metadata, on=["race_date", "racecourse", "race_no"], how="left")
    evaluated = cases[cases["status"] == "evaluated"].copy()
    if evaluated.empty:
        print(json.dumps({"status": "no_evaluable_races", "selected_races": len(cases)}, ensure_ascii=False))
        return 1
    threshold = float(evaluated["brier_score"].quantile(args.high_brier_quantile, interpolation="higher"))
    evaluated["high_brier"] = evaluated["brier_score"] >= threshold
    cases = cases.merge(evaluated[["group_id", "high_brier"]], on="group_id", how="left")
    cases["high_brier"] = cases["high_brier"].fillna(False).astype(bool)
    cases["top_pick_confidence_band"] = cases["top_pick_probability"].map(lambda value: confidence_band(value) if pd.notna(value) else "N/A")
    cases["winner_rank_band"] = cases["winner_model_rank"].map(lambda value: rank_band(int(value)) if pd.notna(value) else "N/A")
    strata = {
        "按馬場": aggregate_strata(cases, "racecourse"),
        "按場地狀況": aggregate_strata(cases, "going"),
        "按首選機率": aggregate_strata(cases, "top_pick_confidence_band"),
        "按頭馬模型排名": aggregate_strata(cases, "winner_rank_band"),
    }
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    charts = make_charts(cases, output_dir)
    date_values = sorted(set(cases["race_date"].tolist()))
    mean_brier = float(evaluated["brier_score"].mean())
    mean_uniform = float(evaluated["uniform_brier_score"].mean())
    summary = {
        "schema_version": "v1",
        "generated_at_hkt": hkt_now(),
        "input": {"predictions": args.predictions, "db": args.db, "probability_column": args.probability_column, "artifact_only": True},
        "window": f"最近 {args.recent_days} 日（按預測工件最新賽日）" if args.recent_days is not None else f"最近 {args.recent_races} 場賽事群組",
        "date_range": f"{date_values[0]} 至 {date_values[-1]}" if date_values else None,
        "selected_races": int(len(cases)),
        "evaluated_races": int(len(evaluated)),
        "excluded_races": int((cases["status"] != "evaluated").sum()),
        "top1_losses": int((~evaluated["top_pick_won"]).sum()),
        "top1_loss_rate": float((~evaluated["top_pick_won"]).mean()),
        "mean_brier": mean_brier,
        "mean_uniform_brier": mean_uniform,
        "high_brier_quantile": args.high_brier_quantile,
        "high_brier_threshold": threshold,
        "high_brier_races": int(evaluated["high_brier"].sum()),
        "exclusions": dict(Counter(cases.loc[cases["status"] != "evaluated", "reason"].dropna())),
        "strata": strata,
        "charts": charts,
    }
    cases.to_csv(output_dir / "race_error_cases.csv", index=False, encoding="utf-8-sig")
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / "error_analysis.md").write_text(render_report(summary, cases, charts), encoding="utf-8")
    print(json.dumps({key: summary[key] for key in ("evaluated_races", "top1_losses", "top1_loss_rate", "mean_brier", "high_brier_threshold", "high_brier_races")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
