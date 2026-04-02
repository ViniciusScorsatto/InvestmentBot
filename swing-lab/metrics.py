from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from config import APP_VERSION, CRYPTO_SYMBOL_TO_KRAKEN_PAIR, UNSUPPORTED_CRYPTO_WATCHLIST
from db import ping_database
from runtime_status import get_status
from trades import enrich_trade_for_display, list_trades
from trade_utils import get_trade_direction


def _safe_pct(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return round((numerator / denominator) * 100, 2)


def _format_timestamp(value: str | None) -> str:
    if not value:
        return "Not run yet"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    return parsed.strftime("%b %d, %Y %H:%M UTC")


def calculate_summary() -> dict[str, Any]:
    trades = list_trades()
    closed = [trade for trade in trades if trade["status"] != "open"]
    open_trades = [enrich_trade_for_display(trade) for trade in trades if trade["status"] == "open"]

    wins = [trade for trade in closed if (trade.get("result_R") or 0) > 0]
    total_r = round(sum(trade.get("result_R") or 0 for trade in closed), 2)
    avg_r = round(total_r / len(closed), 2) if closed else 0.0

    return {
        "total_trades": len(trades),
        "win_rate": _safe_pct(len(wins), len(closed)),
        "avg_R": avg_r,
        "total_R": total_r,
        "open_trades_count": len(open_trades),
        "open_trades": open_trades,
    }


def analytics_by_strategy() -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in list_trades():
        grouped[trade["strategy"]].append(trade)

    analytics: list[dict[str, Any]] = []
    for strategy, trades in grouped.items():
        closed = [trade for trade in trades if trade["status"] != "open"]
        wins = [trade for trade in closed if (trade.get("result_R") or 0) > 0]
        total_r = round(sum(trade.get("result_R") or 0 for trade in closed), 2)
        analytics.append(
            {
                "strategy": strategy,
                "total_trades": len(trades),
                "win_rate": _safe_pct(len(wins), len(closed)),
                "avg_R": round(total_r / len(closed), 2) if closed else 0.0,
            }
        )
    return sorted(analytics, key=lambda item: item["total_trades"], reverse=True)


def analytics_by_asset_class() -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in list_trades():
        grouped[trade["asset_class"]].append(trade)

    analytics: list[dict[str, Any]] = []
    for asset_class, trades in grouped.items():
        closed = [trade for trade in trades if trade["status"] != "open"]
        wins = [trade for trade in closed if (trade.get("result_R") or 0) > 0]
        total_r = round(sum(trade.get("result_R") or 0 for trade in closed), 2)
        analytics.append(
            {
                "asset_class": asset_class,
                "total_trades": len(trades),
                "win_rate": _safe_pct(len(wins), len(closed)),
                "avg_R": round(total_r / len(closed), 2) if closed else 0.0,
                "total_R": total_r,
            }
        )
    return sorted(analytics, key=lambda item: item["total_trades"], reverse=True)


def analytics_by_direction() -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in list_trades():
        grouped[get_trade_direction(trade["strategy"])].append(trade)

    analytics: list[dict[str, Any]] = []
    for direction, trades in grouped.items():
        closed = [trade for trade in trades if trade["status"] != "open"]
        wins = [trade for trade in closed if (trade.get("result_R") or 0) > 0]
        total_r = round(sum(trade.get("result_R") or 0 for trade in closed), 2)
        analytics.append(
            {
                "direction": direction,
                "total_trades": len(trades),
                "win_rate": _safe_pct(len(wins), len(closed)),
                "avg_R": round(total_r / len(closed), 2) if closed else 0.0,
                "total_R": total_r,
            }
        )
    return sorted(analytics, key=lambda item: item["total_trades"], reverse=True)


def calculate_system_status() -> dict[str, Any]:
    runtime = get_status()
    formatted_runtime = dict(runtime)
    for key in ("started_at", "last_scan_at", "last_update_at", "last_summary_at"):
        formatted_runtime[f"{key}_display"] = _format_timestamp(runtime.get(key))
    if runtime.get("last_error"):
        formatted_runtime["last_error"]["at_display"] = _format_timestamp(runtime["last_error"].get("at"))
    return {
        "db_healthy": ping_database(),
        "crypto_provider": "Kraken",
        "equity_provider": "Yahoo Finance",
        "supported_crypto_symbols": sorted(CRYPTO_SYMBOL_TO_KRAKEN_PAIR.keys()),
        "unsupported_crypto_symbols": UNSUPPORTED_CRYPTO_WATCHLIST,
        "runtime": formatted_runtime,
        "app_version": APP_VERSION[:8] if APP_VERSION != "local" else "local",
    }
