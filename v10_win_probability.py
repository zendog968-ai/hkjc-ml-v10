#!/usr/bin/env python3
"""V10 historical win-probability model for a new HKJC race.

This is a transparent, empirical model rather than betting advice. It requires a
SQLite database produced by hkjc_last_season_etl.py and accepts a JSON race card.
All probabilities are relative probabilities within the submitted field.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional


EPSILON = 1e-9
WITHDRAWN_STATUSES = ("WV", "WV-A", "WX-A", "WXNR")


@dataclass
class Runner:
    horse_name: str
    draw: int
    weight_lbs: float
    jockey: str
    trainer: str
    market_odds: Optional[float] = None


@dataclass
class FeatureSet:
    horse_name: str
    same_condition_starts: int
    same_condition_wins: int
    same_condition_win_rate: float
    recent_starts: int
    recent_form_score: float
    recent_margin_score: float
    last_weight_lbs: Optional[float]
    weight_change_lbs: Optional[float]
    weight_score: float
    draw_starts: int
    draw_win_rate: float
    draw_score: float
    jockey_starts: int
    jockey_win_rate: float
    jockey_score: float
    trainer_starts: int
    trainer_win_rate: float
    trainer_score: float
    raw_score: float


@dataclass
class Prediction:
    rank: int
    horse_name: str
    predicted_win_probability: float
    model_heat_index: float
    temperature_label: str
    value_index: Optional[float]
    value_label: Optional[str]
    same_condition_starts: int
    same_condition_win_rate: float
    recent_form_score: float
    draw_score: float
    weight_change_lbs: Optional[float]
    jockey_win_rate: float
    trainer_win_rate: float
    caution: str


class V10WinModel:
    """An explainable probability model with empirical-Bayes smoothing."""

    def __init__(self, db_path: str | Path, lookback: int = 6, as_of_date: Optional[str] = None, temperature: float = 2.0) -> None:
        if temperature <= 0:
            raise ValueError("temperature 必須為正數。")
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.lookback = lookback
        self.as_of_date = as_of_date
        self.temperature = temperature
        self._check_schema()
        cutoff_clause = "WHERE race_date < ?" if self.as_of_date else ""
        cutoff_params: tuple[Any, ...] = (self.as_of_date,) if self.as_of_date else ()
        global_where = ("WHERE finish_pos_text NOT IN ('WV','WV-A','WX-A','WXNR')" + (" AND race_date < ?" if self.as_of_date else ""))
        self.global_win_rate = self._scalar(
            f"SELECT AVG(CASE WHEN finish_pos=1 THEN 1.0 ELSE 0.0 END) FROM starters {global_where}",
            cutoff_params,
        ) or 0.08
        self.global_field_size = self._scalar(
            "SELECT AVG(field_size) FROM ("
            " SELECT race_date,racecourse,race_no,COUNT(*) AS field_size"
            " FROM starters " + cutoff_clause + " GROUP BY race_date,racecourse,race_no"
            ")",
            cutoff_params,
        ) or 12.0

    def close(self) -> None:
        self.conn.close()

    def _check_schema(self) -> None:
        required = {"races", "starters"}
        actual = {
            row[0]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing = required - actual
        if missing:
            raise ValueError(f"資料庫缺少必要資料表：{', '.join(sorted(missing))}")
        count = self._scalar("SELECT COUNT(*) FROM starters") or 0
        if count == 0:
            raise ValueError("資料庫沒有已完成賽事資料；請先執行抓取器。")

    def _scalar(self, query: str, params: Iterable[Any] = ()) -> Optional[float]:
        value = self.conn.execute(query, tuple(params)).fetchone()[0]
        return None if value is None else float(value)

    @staticmethod
    def _smoothed_rate(wins: float, starts: float, prior_rate: float, prior_strength: float) -> float:
        return (wins + prior_rate * prior_strength) / (starts + prior_strength)

    @staticmethod
    def _safe_logit_ratio(rate: float, baseline: float) -> float:
        return math.log((rate + EPSILON) / (baseline + EPSILON))

    def _condition_record(self, horse: str, race: dict[str, Any]) -> tuple[int, int, float]:
        cutoff = " AND s.race_date < ?" if self.as_of_date else ""
        params: list[Any] = [horse, race["racecourse"], race["distance_m"], race["surface"]]
        if self.as_of_date:
            params.append(self.as_of_date)
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS starts,
                   SUM(CASE WHEN s.finish_pos=1 THEN 1 ELSE 0 END) AS wins
            FROM starters AS s
            JOIN races AS r
              ON r.race_date=s.race_date AND r.racecourse=s.racecourse AND r.race_no=s.race_no
            WHERE s.horse_name=?
              AND r.racecourse=?
              AND r.distance_m=?
              AND r.surface=?
              AND r.race_status='completed'
              AND s.finish_pos_text NOT IN ('WV','WV-A','WX-A','WXNR')""" + cutoff,
            params,
        ).fetchone()
        starts, wins = int(row["starts"] or 0), int(row["wins"] or 0)
        rate = self._smoothed_rate(wins, starts, self.global_win_rate, 4.0)
        return starts, wins, rate

    def _recent_form(self, horse: str) -> tuple[int, float, float, Optional[float]]:
        rows = self.conn.execute(
            """
            SELECT s.finish_pos, s.margin_lengths, s.weight_lbs
            FROM starters AS s
            JOIN races AS r
              ON r.race_date=s.race_date AND r.racecourse=s.racecourse AND r.race_no=s.race_no
            WHERE s.horse_name=? AND r.race_status='completed' AND s.finish_pos IS NOT NULL"""
            + (" AND s.race_date < ?" if self.as_of_date else "")
            + """
            ORDER BY s.race_date DESC, s.race_no DESC
            LIMIT ?
            """,
            ((horse, self.as_of_date, self.lookback) if self.as_of_date else (horse, self.lookback)),
        ).fetchall()
        if not rows:
            return 0, 0.50, 0.50, None
        weighted_position = 0.0
        weighted_margin = 0.0
        total_weight = 0.0
        for index, row in enumerate(rows):
            recency_weight = 0.85**index
            pos = float(row["finish_pos"])
            # 1st=1.0, 2nd=0.83, 3rd=0.71; position score rapidly tapers below 6th.
            position_score = math.exp(-0.22 * max(pos - 1.0, 0.0))
            margin = max(float(row["margin_lengths"] or 8.0), 0.0)
            margin_score = math.exp(-0.18 * min(margin, 15.0))
            weighted_position += recency_weight * position_score
            weighted_margin += recency_weight * margin_score
            total_weight += recency_weight
        return (
            len(rows),
            weighted_position / total_weight,
            weighted_margin / total_weight,
            float(rows[0]["weight_lbs"]) if rows[0]["weight_lbs"] is not None else None,
        )

    def _draw_context(self, race: dict[str, Any], draw: int) -> tuple[int, float, float]:
        # Draw is classed (inner/middle/outer) to avoid false precision in sparse samples.
        band = "inner" if draw <= 4 else "middle" if draw <= 9 else "outer"
        clause = {
            "inner": "s.draw BETWEEN 1 AND 4",
            "middle": "s.draw BETWEEN 5 AND 9",
            "outer": "s.draw >= 10",
        }[band]
        row = self.conn.execute(
            f"""
            SELECT COUNT(*) AS starts,
                   SUM(CASE WHEN s.finish_pos=1 THEN 1 ELSE 0 END) AS wins
            FROM starters AS s
            JOIN races AS r
              ON r.race_date=s.race_date AND r.racecourse=s.racecourse AND r.race_no=s.race_no
            WHERE r.racecourse=? AND r.distance_m=? AND r.surface=?
              AND r.race_status='completed' AND {clause}
              AND s.finish_pos_text NOT IN ('WV','WV-A','WX-A','WXNR')"""
            + (" AND r.race_date < ?" if self.as_of_date else ""),
            ((race["racecourse"], race["distance_m"], race["surface"], self.as_of_date) if self.as_of_date else (race["racecourse"], race["distance_m"], race["surface"])),
        ).fetchone()
        starts, wins = int(row["starts"] or 0), int(row["wins"] or 0)
        rate = self._smoothed_rate(wins, starts, self.global_win_rate, 18.0)
        score = self._safe_logit_ratio(rate, self.global_win_rate)
        return starts, rate, score

    def _person_record(self, person: str, column: str) -> tuple[int, float, float]:
        if column not in {"jockey", "trainer"}:
            raise ValueError("Unsupported person column")
        row = self.conn.execute(
            f"""
            SELECT COUNT(*) AS starts,
                   SUM(CASE WHEN s.finish_pos=1 THEN 1 ELSE 0 END) AS wins
            FROM starters AS s
            JOIN races AS r
              ON r.race_date=s.race_date AND r.racecourse=s.racecourse AND r.race_no=s.race_no
            WHERE s.{column}=? AND r.race_status='completed'
              AND s.finish_pos_text NOT IN ('WV','WV-A','WX-A','WXNR')"""
            + (" AND r.race_date < ?" if self.as_of_date else ""),
            ((person, self.as_of_date) if self.as_of_date else (person,)),
        ).fetchone()
        starts, wins = int(row["starts"] or 0), int(row["wins"] or 0)
        rate = self._smoothed_rate(wins, starts, self.global_win_rate, 20.0)
        score = self._safe_logit_ratio(rate, self.global_win_rate)
        return starts, rate, score

    def build_features(self, race: dict[str, Any], runner: Runner) -> FeatureSet:
        cond_starts, cond_wins, cond_rate = self._condition_record(runner.horse_name, race)
        recent_starts, recent_pos, recent_margin, last_weight = self._recent_form(runner.horse_name)
        draw_starts, draw_rate, draw_score = self._draw_context(race, runner.draw)
        jockey_starts, jockey_rate, jockey_score = self._person_record(runner.jockey, "jockey")
        trainer_starts, trainer_rate, trainer_score = self._person_record(runner.trainer, "trainer")

        if last_weight is None:
            delta = None
            weight_score = 0.0
        else:
            delta = runner.weight_lbs - last_weight
            # +1 = lower weight, -1 = higher weight; cap to prevent false certainty.
            weight_score = max(-0.55, min(0.55, -delta * 0.045))

        condition_score = self._safe_logit_ratio(cond_rate, self.global_win_rate)
        # Relative recent form: neutral 0.50 gives zero contribution.
        recent_score = 1.10 * (recent_pos - 0.50) + 0.75 * (recent_margin - 0.50)
        sample_confidence = min(1.0, cond_starts / 4.0)
        raw_score = (
            1.15 * condition_score * (0.45 + 0.55 * sample_confidence)
            + 1.10 * recent_score
            + 0.70 * draw_score
            + 0.60 * weight_score
            + 0.55 * jockey_score
            + 0.45 * trainer_score
        )
        return FeatureSet(
            horse_name=runner.horse_name,
            same_condition_starts=cond_starts,
            same_condition_wins=cond_wins,
            same_condition_win_rate=cond_rate,
            recent_starts=recent_starts,
            recent_form_score=(recent_pos + recent_margin) / 2.0,
            recent_margin_score=recent_margin,
            last_weight_lbs=last_weight,
            weight_change_lbs=delta,
            weight_score=weight_score,
            draw_starts=draw_starts,
            draw_win_rate=draw_rate,
            draw_score=draw_score,
            jockey_starts=jockey_starts,
            jockey_win_rate=jockey_rate,
            jockey_score=jockey_score,
            trainer_starts=trainer_starts,
            trainer_win_rate=trainer_rate,
            trainer_score=trainer_score,
            raw_score=raw_score,
        )

    @staticmethod
    def _temperature_label(heat_index: float) -> str:
        if heat_index >= 160:
            return "極熱"
        if heat_index >= 120:
            return "偏熱"
        if heat_index >= 80:
            return "均衡"
        return "偏冷"

    @staticmethod
    def _caution(features: FeatureSet) -> str:
        flags = []
        if features.same_condition_starts == 0:
            flags.append("同程同場無紀錄")
        elif features.same_condition_starts < 2:
            flags.append("同程同場樣本偏少")
        if features.recent_starts < 2:
            flags.append("近績樣本偏少")
        if features.weight_change_lbs is None:
            flags.append("未能比較前仗負磅")
        return "；".join(flags) if flags else "樣本充足"

    def predict(self, race: dict[str, Any], runners: list[Runner]) -> tuple[list[Prediction], list[FeatureSet]]:
        if not 2 <= len(runners) <= 20:
            raise ValueError("每場須輸入 2 至 20 匹馬。")
        features = [self.build_features(race, runner) for runner in runners]
        max_score = max(item.raw_score for item in features)
        # Temperature prevents an uncalibrated linear score from producing overly extreme probabilities.
        exp_scores = [math.exp((item.raw_score - max_score) / self.temperature) for item in features]
        denominator = sum(exp_scores)
        probabilities = [score / denominator for score in exp_scores]
        baseline = 1.0 / len(runners)

        predictions = []
        for runner, feature, probability in zip(runners, features, probabilities):
            heat_index = 100.0 * probability / baseline
            value_index = None
            value_label = None
            if runner.market_odds and runner.market_odds > 1.0:
                implied = 1.0 / runner.market_odds
                value_index = probability / implied
                if value_index >= 1.20:
                    value_label = "模型相對看好"
                elif value_index <= 0.80:
                    value_label = "市場相對看好"
                else:
                    value_label = "接近市場"
            predictions.append(
                Prediction(
                    rank=0,
                    horse_name=runner.horse_name,
                    predicted_win_probability=probability,
                    model_heat_index=heat_index,
                    temperature_label=self._temperature_label(heat_index),
                    value_index=value_index,
                    value_label=value_label,
                    same_condition_starts=feature.same_condition_starts,
                    same_condition_win_rate=feature.same_condition_win_rate,
                    recent_form_score=feature.recent_form_score,
                    draw_score=feature.draw_score,
                    weight_change_lbs=feature.weight_change_lbs,
                    jockey_win_rate=feature.jockey_win_rate,
                    trainer_win_rate=feature.trainer_win_rate,
                    caution=self._caution(feature),
                )
            )
        predictions.sort(key=lambda item: item.predicted_win_probability, reverse=True)
        for rank, item in enumerate(predictions, start=1):
            item.rank = rank
        return predictions, features


def load_race_card(path: str | Path) -> tuple[dict[str, Any], list[Runner]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    race = payload.get("race", {})
    required = {"racecourse", "distance_m", "surface"}
    missing = required - set(race)
    if missing:
        raise ValueError(f"race 欄位缺少：{', '.join(sorted(missing))}")
    race["racecourse"] = str(race["racecourse"]).upper()
    race["distance_m"] = int(race["distance_m"])
    race["surface"] = str(race["surface"])
    runners = []
    for row in payload.get("runners", []):
        required_runner = {"horse_name", "draw", "weight_lbs", "jockey", "trainer"}
        missing_runner = required_runner - set(row)
        if missing_runner:
            raise ValueError(f"馬匹資料缺少：{', '.join(sorted(missing_runner))}")
        runners.append(
            Runner(
                horse_name=str(row["horse_name"]),
                draw=int(row["draw"]),
                weight_lbs=float(row["weight_lbs"]),
                jockey=str(row["jockey"]),
                trainer=str(row["trainer"]),
                market_odds=float(row["market_odds"]) if row.get("market_odds") not in (None, "") else None,
            )
        )
    return race, runners


def write_outputs(
    predictions: list[Prediction],
    features: list[FeatureSet],
    output_json: str | Path,
    output_csv: str | Path,
) -> None:
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "model": "V10 Historical Win Probability v1.0",
        "predictions": [asdict(item) for item in predictions],
        "feature_audit": [asdict(item) for item in features],
    }
    Path(output_json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    columns = list(asdict(predictions[0]).keys())
    with Path(output_csv).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(asdict(item) for item in predictions)


def print_report(race: dict[str, Any], predictions: list[Prediction]) -> None:
    print("V10 歷史勝率分析")
    cutoff_note = f"｜資料截點：{race.get('as_of_date')} 前" if race.get('as_of_date') else ""
    print(f"條件：{race['racecourse']}｜{race['distance_m']}米｜{race['surface']}｜參賽馬匹 {len(predictions)} 匹{cutoff_note}")
    header = "名次  馬名         勝出率     熱度    定位      同程同場  近績分  提示"
    print(header)
    print("-" * 96)
    for item in predictions:
        cond_rate = f"{item.same_condition_win_rate * 100:4.1f}%/{item.same_condition_starts}"
        print(
            f"{item.rank:>2}    {item.horse_name:<10} {item.predicted_win_probability*100:>6.2f}%"
            f"  {item.model_heat_index:>6.1f}  {item.temperature_label:<4}"
            f"  {cond_rate:<10} {item.recent_form_score:>5.2f}  {item.caution}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="V10 歷史勝率及冷熱指數分析")
    parser.add_argument("--db", default="hkjc_last_season.sqlite", help="SQLite 歷史資料庫")
    parser.add_argument("--race-card", required=True, help="新賽事輸入 JSON")
    parser.add_argument("--output-json", default="v10_prediction.json", help="完整輸出 JSON")
    parser.add_argument("--output-csv", default="v10_prediction.csv", help="排序後預測 CSV")
    parser.add_argument("--lookback", type=int, default=6, help="近績回望場數（預設 6 場）")
    parser.add_argument("--as-of-date", help="只使用此日期之前的賽果（YYYY-MM-DD；回測必填）")
    parser.add_argument("--temperature", type=float, default=2.0, help="機率溫度；較高數值較保守（預設 2.0）")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    race, runners = load_race_card(args.race_card)
    cutoff = args.as_of_date or race.get("as_of_date")
    if cutoff:
        datetime.strptime(cutoff, "%Y-%m-%d")
        race["as_of_date"] = cutoff
    model = V10WinModel(args.db, args.lookback, cutoff, args.temperature)
    try:
        predictions, features = model.predict(race, runners)
        write_outputs(predictions, features, args.output_json, args.output_csv)
        print_report(race, predictions)
        return 0
    finally:
        model.close()


if __name__ == "__main__":
    raise SystemExit(main())
