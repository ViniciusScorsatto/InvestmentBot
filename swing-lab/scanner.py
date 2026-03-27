from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from config import (
    CRYPTO_REQUEST_DELAY_SECONDS,
    CRYPTO_SYMBOL_TO_KRAKEN_PAIR,
    KRAKEN_OHLC_URL,
    MAX_TRADES_PER_DAY,
    MIN_R_MULTIPLE,
    MIN_SCORE,
    PREFERRED_TOP_SETUPS,
    WATCHLIST,
    YAHOO_CHART_URL,
    cache_ttl_for,
    default_cached_dataset,
)
from db import execute, fetch_one
from strategies import detect_market_alignment, evaluate_breakout, evaluate_trend_pullback


LOGGER = logging.getLogger(__name__)
_LAST_CRYPTO_REQUEST_AT: datetime | None = None


def _to_iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _aggregate_bars(bars: list[dict[str, Any]], group_size: int) -> list[dict[str, Any]]:
    aggregated: list[dict[str, Any]] = []
    for index in range(0, len(bars), group_size):
        chunk = bars[index : index + group_size]
        if len(chunk) < group_size:
            continue
        aggregated.append(
            {
                "timestamp": chunk[-1]["timestamp"],
                "open": chunk[0]["open"],
                "high": max(item["high"] for item in chunk),
                "low": min(item["low"] for item in chunk),
                "close": chunk[-1]["close"],
                "volume": sum(item["volume"] for item in chunk),
            }
        )
    return aggregated


def _safe_json(response: requests.Response) -> Any:
    response.raise_for_status()
    return response.json()


def _cached_market_data(asset: str, asset_class: str) -> dict[str, list[dict[str, Any]]] | None:
    row = fetch_one(
        """
        SELECT payload_json, fetched_at
        FROM market_data_cache
        WHERE asset = %s AND asset_class = %s
        """,
        (asset, asset_class),
    )
    if not row:
        return None
    fetched_at = row["fetched_at"]
    if isinstance(fetched_at, str):
        fetched_at = datetime.fromisoformat(fetched_at)
    if datetime.now(tz=timezone.utc) - fetched_at > timedelta(seconds=cache_ttl_for(asset_class)):
        return None
    return json.loads(row["payload_json"])


def _store_market_data(asset: str, asset_class: str, payload: dict[str, list[dict[str, Any]]]) -> None:
    execute(
        """
        INSERT INTO market_data_cache (asset, asset_class, payload_json, fetched_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT(asset, asset_class)
        DO UPDATE SET payload_json = excluded.payload_json, fetched_at = excluded.fetched_at
        """,
        (asset, asset_class, json.dumps(payload), datetime.now(tz=timezone.utc)),
    )


def _most_recent_cached_market_data(asset: str, asset_class: str) -> dict[str, list[dict[str, Any]]] | None:
    row = fetch_one(
        """
        SELECT payload_json
        FROM market_data_cache
        WHERE asset = %s AND asset_class = %s
        """,
        (asset, asset_class),
    )
    if not row:
        return None
    return json.loads(row["payload_json"])


def _respect_crypto_rate_limit() -> None:
    global _LAST_CRYPTO_REQUEST_AT
    now = datetime.now(tz=timezone.utc)
    if _LAST_CRYPTO_REQUEST_AT is not None:
        elapsed = (now - _LAST_CRYPTO_REQUEST_AT).total_seconds()
        remaining = CRYPTO_REQUEST_DELAY_SECONDS - elapsed
        if remaining > 0:
            time.sleep(remaining)
    _LAST_CRYPTO_REQUEST_AT = datetime.now(tz=timezone.utc)


def _fetch_yahoo_chart(symbol: str, interval: str, range_value: str) -> list[dict[str, Any]]:
    response = requests.get(
        YAHOO_CHART_URL.format(symbol=symbol),
        params={"interval": interval, "range": range_value, "includePrePost": "false"},
        timeout=20,
        headers={"User-Agent": "SwingLabAuto/1.0"},
    )
    payload = _safe_json(response)
    result = payload["chart"]["result"][0]
    quote = result["indicators"]["quote"][0]
    timestamps = result.get("timestamp", [])

    bars: list[dict[str, Any]] = []
    for index, timestamp in enumerate(timestamps):
        open_price = quote["open"][index]
        high_price = quote["high"][index]
        low_price = quote["low"][index]
        close_price = quote["close"][index]
        volume = quote["volume"][index] or 0
        if None in (open_price, high_price, low_price, close_price):
            continue
        bars.append(
            {
                "timestamp": _to_iso(timestamp),
                "open": float(open_price),
                "high": float(high_price),
                "low": float(low_price),
                "close": float(close_price),
                "volume": float(volume),
            }
        )
    return bars


def _fetch_kraken_chart(symbol: str, interval_minutes: int = 60) -> list[dict[str, Any]]:
    _respect_crypto_rate_limit()
    pair = CRYPTO_SYMBOL_TO_KRAKEN_PAIR.get(symbol)
    if not pair:
        LOGGER.warning("Kraken pair not configured for %s, skipping crypto fetch", symbol)
        return []
    response = requests.get(
        KRAKEN_OHLC_URL,
        params={"pair": pair, "interval": interval_minutes},
        timeout=20,
        headers={"User-Agent": "SwingLabAuto/1.0"},
    )
    payload = _safe_json(response)
    errors = payload.get("error", [])
    if errors:
        raise requests.HTTPError(", ".join(errors), response=response)
    result = payload.get("result", {})
    ohlc_key = next((key for key in result.keys() if key != "last"), None)
    if not ohlc_key:
        return []
    candles = result.get(ohlc_key, [])
    bars: list[dict[str, Any]] = []
    for candle in candles:
        timestamp, open_price, high_price, low_price, close_price, _, volume, _ = candle
        bars.append(
            {
                "timestamp": _to_iso(timestamp),
                "open": float(open_price),
                "high": float(high_price),
                "low": float(low_price),
                "close": float(close_price),
                "volume": float(volume),
            }
        )
    return bars


def fetch_asset_data(asset: str, asset_class: str) -> dict[str, list[dict[str, Any]]]:
    cached = _cached_market_data(asset, asset_class)
    if cached is not None:
        return cached
    try:
        if asset_class == "crypto":
            hourly = _fetch_kraken_chart(asset, interval_minutes=60)
            daily = _aggregate_bars(hourly, 24)
            four_hour = _aggregate_bars(hourly, 4)
        else:
            hourly = _fetch_yahoo_chart(asset, interval="1h", range_value="6mo")
            daily = _fetch_yahoo_chart(asset, interval="1d", range_value="1y")
            four_hour = _aggregate_bars(hourly, 4)
        payload = {"4h": four_hour, "1d": daily}
        _store_market_data(asset, asset_class, payload)
        return payload
    except requests.RequestException as exc:
        LOGGER.warning("Failed to fetch data for %s: %s", asset, exc)
        cached_fallback = _most_recent_cached_market_data(asset, asset_class)
        if cached_fallback is not None:
            LOGGER.info("Using stale cached market data for %s after fetch failure", asset)
            return cached_fallback
        return json.loads(default_cached_dataset())


def generate_trade_candidates(asset_classes: list[str] | None = None) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    enabled_asset_classes = set(asset_classes or WATCHLIST.keys())
    for asset_class, symbols in WATCHLIST.items():
        if asset_class not in enabled_asset_classes:
            continue
        for asset in symbols:
            dataset = fetch_asset_data(asset, asset_class)
            daily_bars = dataset["1d"]
            if len(daily_bars) < 60:
                continue
            alignment = detect_market_alignment(daily_bars)
            for timeframe in ("4h", "1d"):
                bars = dataset[timeframe]
                if len(bars) < 60:
                    continue
                for evaluator in (evaluate_trend_pullback, evaluate_breakout):
                    trade = evaluator(bars, asset, asset_class, timeframe, alignment)
                    if trade and trade["score"] >= MIN_SCORE and trade["R_multiple"] >= MIN_R_MULTIPLE:
                        candidates.append(trade)
    candidates.sort(key=lambda item: (item["score"], item["R_multiple"]), reverse=True)
    return candidates[:MAX_TRADES_PER_DAY]


def select_best_setups(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen_assets: set[tuple[str, str]] = set()
    for trade in candidates:
        key = (trade["asset"], trade["timeframe"])
        if key in seen_assets:
            continue
        selected.append(trade)
        seen_assets.add(key)
        if len(selected) >= PREFERRED_TOP_SETUPS:
            break
    return selected
