from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from config import (LEARNING_MODEL_BLOCK_MIN_SAMPLE, LEARNING_MODEL_MIN_SAMPLE,
                    LEARNING_MODEL_MIN_SCORE, STRATEGY_VERSION, SIM_FEE_BPS, SIM_SLIPPAGE_BPS)
from db import fetch_all
from trade_utils import get_trade_direction


PRIOR_TRADES = 6
PRIOR_WIN_RATE = 0.5
PRIOR_AVG_R = 0.0
BLOCKING_KEY_TYPES = {
    "setup_slice",
    "strategy_timeframe",
    "asset_class_strategy",
    "rsi_bucket",
    "volume_bucket",
    "ema_gap_bucket",
}


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
        ("setup_slice", setup["asset_class"], setup["strategy"], setup["timeframe"]),
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
            _feature_bucket(abs(_safe_float(features["ema_gap_pct"])) if features.get("ema_gap_pct") is not None else None, (0.005, 0.015, 0.03, 0.06)),
        ),
    ]


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _closed_trade_rows() -> list[dict[str, Any]]:
    # Raise on DB errors so unavailable feedback is not cached as an empty history.
    return fetch_all(
        """
        SELECT id, asset, asset_class, strategy, timeframe, date_opened, date_closed,
               result_R AS "result_R", metadata_json
        FROM trades
        WHERE status != 'open' AND result_R IS NOT NULL
        ORDER BY id ASC
        """
    )


def _append(stats: dict[tuple[str, ...], list[float]], key: tuple[str, ...], result_r: float) -> None:
    stats.setdefault(key, []).append(result_r)


def _row_value(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row:
            return row[key]
    return None


def eligible_history(row: dict[str, Any]) -> bool:
    metadata = _metadata(row)
    execution = metadata.get("execution", {})
    return (metadata.get("strategy_version") == STRATEGY_VERSION
            and execution.get("fee_bps") == SIM_FEE_BPS
            and execution.get("slippage_bps") == SIM_SLIPPAGE_BPS)


@lru_cache(maxsize=1)
def learned_stats() -> dict[tuple[str, ...], SliceStats]:
    return build_stats(_closed_trade_rows())


def build_stats(rows: list[dict[str, Any]]) -> dict[tuple[str, ...], SliceStats]:
    raw_stats: dict[tuple[str, ...], list[float]] = {}
    for row in rows:
        if not eligible_history(row):
            continue
        result_value = _row_value(row, "result_R", "result_r")
        if result_value is None:
            continue
        result_r = float(result_value)
        metadata = _metadata(row)
        features = metadata.get("features") if isinstance(metadata.get("features"), dict) else {}
        strategy = str(_row_value(row, "strategy") or "")
        timeframe = str(_row_value(row, "timeframe") or "")
        asset_class = str(_row_value(row, "asset_class") or "")
        direction = get_trade_direction(strategy)
        keys = [
            ("all",),
            ("setup_slice", asset_class, strategy, timeframe),
            ("strategy", strategy),
            ("strategy_timeframe", strategy, timeframe),
            ("asset_class_strategy", asset_class, strategy),
            ("direction_asset_class", direction, asset_class),
        ]
        if features:
            keys.extend(
                [
                    ("rsi_bucket", strategy, _feature_bucket(features.get("rsi"), (40, 50, 60, 70))),
                    (
                        "volume_bucket",
                        strategy,
                        _feature_bucket(features.get("volume_ratio"), (0.8, 1.0, 1.2, 1.5)),
                    ),
                    (
                        "ema_gap_bucket",
                        strategy,
                        _feature_bucket(abs(_safe_float(features["ema_gap_pct"])) if features.get("ema_gap_pct") is not None else None, (0.005, 0.015, 0.03, 0.06)),
                    ),
                ]
            )
        for key in keys:
            _append(raw_stats, key, result_r)

    return {
        key: SliceStats(
            trades=len(results),
            wins=sum(1 for result in results if result > 0),
            avg_r=sum(results) / len(results),
        )
        for key, results in raw_stats.items()
        if results
    }


def clear_learning_cache() -> None:
    learned_stats.cache_clear()


def _score_from_slice(item: SliceStats) -> int:
    # Return expectancy drives rank; win rate is descriptive, not a second objective.
    shrunk_r = item.avg_r * item.trades / (PRIOR_TRADES + item.trades)
    return int(round(max(0, min(100, 50 + 30 * shrunk_r))))


def _key_label(key: tuple[str, ...]) -> tuple[str, str]:
    category = key[0].replace("_", " ").title()
    if len(key) == 1:
        return category, "All closed trades"
    return category, " / ".join(str(part) for part in key[1:])


def _blocking_slice(key: tuple[str, ...], item: SliceStats) -> dict[str, Any] | None:
    if key[0] not in BLOCKING_KEY_TYPES:
        return None
    if item.trades < LEARNING_MODEL_BLOCK_MIN_SAMPLE:
        return None
    model_score = _score_from_slice(item)
    if model_score >= LEARNING_MODEL_MIN_SCORE or item.avg_r >= 0:
        return None
    category, label = _key_label(key)
    return {
        "category": category,
        "slice": label,
        "trades": item.trades,
        "win_rate": round(item.win_rate * 100, 1),
        "avg_R": item.avg_r,
        "model_score": model_score,
    }


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


def score_setup(setup: dict[str, Any], stats: dict[tuple[str, ...], SliceStats] | None = None) -> dict[str, Any]:
    if stats is None:
        stats = learned_stats()
    matched_slices = [(key, item) for key in _candidate_keys(setup) if (item := stats.get(key)) is not None]
    # Use one cohort, avoiding repeated counting of the same trades across seven slices.
    primary = SliceStats(trades=0, wins=0, avg_r=0)
    primary_key: tuple[str, ...] | None = None
    for category in ("setup_slice", "strategy_timeframe", "asset_class_strategy", "strategy"):
        choices = [(key, item) for key, item in matched_slices if key[0] == category]
        if choices:
            key, item = choices[0]
            if primary_key is None:
                primary_key, primary = key, item
            if item.trades >= LEARNING_MODEL_MIN_SAMPLE:
                primary_key, primary = key, item
                break
    sample_size = primary.trades
    enough_sample = sample_size >= LEARNING_MODEL_MIN_SAMPLE
    learned_win_rate = ((PRIOR_TRADES * PRIOR_WIN_RATE + primary.wins)
                        / (PRIOR_TRADES + sample_size))
    learned_avg_r = primary.avg_r * sample_size / (PRIOR_TRADES + sample_size)
    model_score = _score_from_slice(primary) if enough_sample else 50
    blocking_slices = [
        blocking_slice
        for key, item in matched_slices
        if (blocking_slice := _blocking_slice(key, item)) is not None
    ]
    approved = not blocking_slices

    return {
        "model_score": model_score,
        "scoring_cohort": list(primary_key) if primary_key else None,
        "strategy_version": STRATEGY_VERSION,
        "learned_win_rate": round(learned_win_rate * 100, 1),
        "learned_avg_R": round(learned_avg_r, 2),
        "sample_size": sample_size,
        "confidence": "active" if enough_sample else "warming_up",
        "approved": approved,
        "min_score": LEARNING_MODEL_MIN_SCORE if sample_size >= LEARNING_MODEL_BLOCK_MIN_SAMPLE else None,
        "block_min_sample": LEARNING_MODEL_BLOCK_MIN_SAMPLE,
        "blocking_slices": blocking_slices,
    }
