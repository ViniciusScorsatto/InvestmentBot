from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from config import LEARNING_MODEL_MIN_SAMPLE, LEARNING_MODEL_MIN_SCORE
from db import fetch_all
from trade_utils import get_trade_direction


PRIOR_TRADES = 6
PRIOR_WIN_RATE = 0.5
PRIOR_AVG_R = 0.0


@dataclass(frozen=True)
class SliceStats:
    trades: int
    wins: int
    avg_r: float

    @property
    def win_rate(self) -> float:
        if self.trades == 0:
            return 0.0
        return self.wins / self.trades


def _metadata(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("metadata_json")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _feature_bucket(value: float | int | None, edges: tuple[float, ...]) -> str:
    if value is None:
        return "unknown"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "unknown"
    for edge in edges:
        if numeric <= edge:
            return f"<= {edge:g}"
    return f"> {edges[-1]:g}"


def _candidate_keys(setup: dict[str, Any]) -> list[tuple[str, ...]]:
    features = (setup.get("components") or {}).get("features") or {}
    direction = get_trade_direction(setup["strategy"])
    return [
        ("strategy", setup["strategy"]),
        ("strategy_timeframe", setup["strategy"], setup["timeframe"]),
        ("asset_class_strategy", setup["asset_class"], setup["strategy"]),
        ("direction_asset_class", direction, setup["asset_class"]),
        (
            "rsi_bucket",
            setup["strategy"],
            _feature_bucket(features.get("rsi"), (40, 50, 60, 70)),
        ),
        (
            "volume_bucket",
            setup["strategy"],
            _feature_bucket(features.get("volume_ratio"), (0.8, 1.0, 1.2, 1.5)),
        ),
        (
            "ema_gap_bucket",
            setup["strategy"],
            _feature_bucket(abs(_safe_float(features.get("ema_gap_pct"))), (0.005, 0.015, 0.03, 0.06)),
        ),
    ]


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _closed_trade_rows() -> list[dict[str, Any]]:
    try:
        return fetch_all(
            """
            SELECT asset, asset_class, strategy, timeframe, result_R, metadata_json
            FROM trades
            WHERE status != 'open' AND result_R IS NOT NULL
            ORDER BY id ASC
            """
        )
    except Exception:
        return []


def _append(stats: dict[tuple[str, ...], list[float]], key: tuple[str, ...], result_r: float) -> None:
    stats.setdefault(key, []).append(result_r)


@lru_cache(maxsize=1)
def learned_stats() -> dict[tuple[str, ...], SliceStats]:
    raw_stats: dict[tuple[str, ...], list[float]] = {}
    for row in _closed_trade_rows():
        result_r = float(row["result_R"])
        metadata = _metadata(row)
        features = metadata.get("features") if isinstance(metadata.get("features"), dict) else {}
        direction = get_trade_direction(row["strategy"])
        keys = [
            ("all",),
            ("strategy", row["strategy"]),
            ("strategy_timeframe", row["strategy"], row["timeframe"]),
            ("asset_class_strategy", row["asset_class"], row["strategy"]),
            ("direction_asset_class", direction, row["asset_class"]),
        ]
        if features:
            keys.extend(
                [
                    ("rsi_bucket", row["strategy"], _feature_bucket(features.get("rsi"), (40, 50, 60, 70))),
                    (
                        "volume_bucket",
                        row["strategy"],
                        _feature_bucket(features.get("volume_ratio"), (0.8, 1.0, 1.2, 1.5)),
                    ),
                    (
                        "ema_gap_bucket",
                        row["strategy"],
                        _feature_bucket(abs(_safe_float(features.get("ema_gap_pct"))), (0.005, 0.015, 0.03, 0.06)),
                    ),
                ]
            )
        for key in keys:
            _append(raw_stats, key, result_r)

    return {
        key: SliceStats(
            trades=len(results),
            wins=sum(1 for result in results if result > 0),
            avg_r=round(sum(results) / len(results), 3),
        )
        for key, results in raw_stats.items()
        if results
    }


def clear_learning_cache() -> None:
    learned_stats.cache_clear()


def _score_from_slice(item: SliceStats) -> int:
    weighted_trades = PRIOR_TRADES + item.trades
    weighted_wins = (PRIOR_TRADES * PRIOR_WIN_RATE) + item.wins
    weighted_r = (PRIOR_TRADES * PRIOR_AVG_R) + (item.avg_r * item.trades)
    learned_win_rate = weighted_wins / weighted_trades if weighted_trades else PRIOR_WIN_RATE
    learned_avg_r = weighted_r / weighted_trades if weighted_trades else PRIOR_AVG_R
    win_component = (learned_win_rate - 0.5) * 80
    r_component = max(min(learned_avg_r, 1.5), -1.5) * 20
    return int(round(max(0, min(100, 50 + win_component + r_component))))


def _key_label(key: tuple[str, ...]) -> tuple[str, str]:
    category = key[0].replace("_", " ").title()
    if len(key) == 1:
        return category, "All closed trades"
    return category, " / ".join(str(part) for part in key[1:])


def learning_model_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, item in learned_stats().items():
        category, label = _key_label(key)
        model_score = _score_from_slice(item)
        if item.trades < LEARNING_MODEL_MIN_SAMPLE:
            stance = "warming_up"
        elif model_score >= 60 and item.avg_r > 0:
            stance = "favored"
        elif model_score < LEARNING_MODEL_MIN_SCORE:
            stance = "penalized"
        else:
            stance = "neutral"
        rows.append(
            {
                "category": category,
                "slice": label,
                "trades": item.trades,
                "wins": item.wins,
                "win_rate": round(item.win_rate * 100, 1),
                "avg_R": item.avg_r,
                "model_score": model_score,
                "stance": stance,
                "active": item.trades >= LEARNING_MODEL_MIN_SAMPLE,
            }
        )
    return sorted(
        rows,
        key=lambda row: (row["active"], abs(row["model_score"] - 50), row["trades"]),
        reverse=True,
    )


def score_setup(setup: dict[str, Any]) -> dict[str, Any]:
    stats = learned_stats()
    global_stats = stats.get(("all",), SliceStats(trades=0, wins=0, avg_r=0))
    matched = [item for key in _candidate_keys(setup) if (item := stats.get(key)) is not None]

    weighted_trades = PRIOR_TRADES
    weighted_wins = PRIOR_TRADES * PRIOR_WIN_RATE
    weighted_r = PRIOR_TRADES * PRIOR_AVG_R
    for item in matched:
        weight = min(item.trades, 25)
        weighted_trades += weight
        weighted_wins += item.win_rate * weight
        weighted_r += item.avg_r * weight

    learned_win_rate = weighted_wins / weighted_trades if weighted_trades else PRIOR_WIN_RATE
    learned_avg_r = weighted_r / weighted_trades if weighted_trades else PRIOR_AVG_R
    sample_size = max((item.trades for item in matched), default=global_stats.trades)

    win_component = (learned_win_rate - 0.5) * 80
    r_component = max(min(learned_avg_r, 1.5), -1.5) * 20
    model_score = int(round(max(0, min(100, 50 + win_component + r_component))))
    enough_sample = sample_size >= LEARNING_MODEL_MIN_SAMPLE
    approved = (not enough_sample) or model_score >= LEARNING_MODEL_MIN_SCORE

    return {
        "model_score": model_score,
        "learned_win_rate": round(learned_win_rate * 100, 1),
        "learned_avg_R": round(learned_avg_r, 2),
        "sample_size": sample_size,
        "confidence": "active" if enough_sample else "warming_up",
        "approved": approved,
        "min_score": LEARNING_MODEL_MIN_SCORE if enough_sample else None,
    }
