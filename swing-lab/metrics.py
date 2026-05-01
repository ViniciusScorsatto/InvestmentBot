from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from config import (
    APP_VERSION,
    CRYPTO_SYMBOL_TO_KRAKEN_PAIR,
    LAST_STRATEGY_CHANGE_AT,
    LAST_STRATEGY_CHANGE_LABEL,
    LAST_STRATEGY_CHANGE_NOTE,
    UNSUPPORTED_CRYPTO_WATCHLIST,
    strategy_status_rows,
)
from db import ping_database
from runtime_status import get_status
from trades import enrich_trade_for_display, list_trades, resolve_result_r
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


def _parse_date_filter(value: str | None, end_of_day: bool = False) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(hour=0, minute=0, second=0, microsecond=0)
        if end_of_day:
            parsed = parsed + timedelta(days=1) - timedelta(microseconds=1)
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _trade_opened_at(trade: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(trade["date_opened"])


def _filter_trades_by_date(
    trades: list[dict[str, Any]],
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict[str, Any]]:
    start_dt = _parse_date_filter(start_date)
    end_dt = _parse_date_filter(end_date, end_of_day=True)
    filtered = trades
    if start_dt is not None:
        filtered = [trade for trade in filtered if _trade_opened_at(trade) >= start_dt]
    if end_dt is not None:
        filtered = [trade for trade in filtered if _trade_opened_at(trade) <= end_dt]
    return filtered


def _summary_from_trades(trades: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [trade for trade in trades if trade["status"] != "open"]
    open_trades = [enrich_trade_for_display(trade) for trade in trades if trade["status"] == "open"]
    wins = [trade for trade in closed if (resolve_result_r(trade) or 0) > 0]
    total_r = round(sum(resolve_result_r(trade) or 0 for trade in closed), 2)
    avg_r = round(total_r / len(closed), 2) if closed else 0.0
    return {
        "total_trades": len(trades),
        "closed_trades": len(closed),
        "win_rate": _safe_pct(len(wins), len(closed)),
        "avg_R": avg_r,
        "total_R": total_r,
        "open_trades_count": len(open_trades),
        "open_trades": open_trades,
    }


def calculate_summary() -> dict[str, Any]:
    return _summary_from_trades(list_trades())


def analytics_by_strategy(trades: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in (trades or list_trades()):
        grouped[trade["strategy"]].append(trade)

    analytics: list[dict[str, Any]] = []
    for strategy, trades in grouped.items():
        closed = [trade for trade in trades if trade["status"] != "open"]
        wins = [trade for trade in closed if (resolve_result_r(trade) or 0) > 0]
        total_r = round(sum(resolve_result_r(trade) or 0 for trade in closed), 2)
        analytics.append(
            {
                "strategy": strategy,
                "total_trades": len(trades),
                "win_rate": _safe_pct(len(wins), len(closed)),
                "avg_R": round(total_r / len(closed), 2) if closed else 0.0,
            }
        )
    return sorted(analytics, key=lambda item: item["total_trades"], reverse=True)


def analytics_by_asset_class(trades: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in (trades or list_trades()):
        grouped[trade["asset_class"]].append(trade)

    analytics: list[dict[str, Any]] = []
    for asset_class, trades in grouped.items():
        closed = [trade for trade in trades if trade["status"] != "open"]
        wins = [trade for trade in closed if (resolve_result_r(trade) or 0) > 0]
        total_r = round(sum(resolve_result_r(trade) or 0 for trade in closed), 2)
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


def analytics_by_direction(trades: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in (trades or list_trades()):
        grouped[get_trade_direction(trade["strategy"])].append(trade)

    analytics: list[dict[str, Any]] = []
    for direction, trades in grouped.items():
        closed = [trade for trade in trades if trade["status"] != "open"]
        wins = [trade for trade in closed if (resolve_result_r(trade) or 0) > 0]
        total_r = round(sum(resolve_result_r(trade) or 0 for trade in closed), 2)
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


def analytics_by_setup_slice(trades: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for trade in (trades or list_trades()):
        key = (trade["strategy"], trade["timeframe"], get_trade_direction(trade["strategy"]))
        grouped[key].append(trade)

    analytics: list[dict[str, Any]] = []
    for (strategy, timeframe, direction), trades in grouped.items():
        closed = [trade for trade in trades if trade["status"] != "open"]
        wins = [trade for trade in closed if (resolve_result_r(trade) or 0) > 0]
        total_r = round(sum(resolve_result_r(trade) or 0 for trade in closed), 2)
        analytics.append(
            {
                "strategy": strategy,
                "timeframe": timeframe,
                "direction": direction,
                "total_trades": len(trades),
                "closed_trades": len(closed),
                "win_rate": _safe_pct(len(wins), len(closed)),
                "avg_R": round(total_r / len(closed), 2) if closed else 0.0,
                "total_R": total_r,
            }
        )
    return sorted(
        analytics,
        key=lambda item: (item["closed_trades"], item["total_trades"], item["total_R"]),
        reverse=True,
    )


def analytics_payload(start_date: str | None = None, end_date: str | None = None) -> dict[str, Any]:
    trades = _filter_trades_by_date(list_trades(), start_date=start_date, end_date=end_date)
    return {
        "summary": _summary_from_trades(trades),
        "strategy_stats": analytics_by_strategy(trades),
        "asset_class_stats": analytics_by_asset_class(trades),
        "direction_stats": analytics_by_direction(trades),
        "setup_slice_stats": analytics_by_setup_slice(trades),
        "strategy_status": strategy_status_rows(),
    }


def analytics_since_strategy_change() -> dict[str, Any]:
    payload = analytics_payload(start_date=LAST_STRATEGY_CHANGE_AT[:10])
    payload["label"] = LAST_STRATEGY_CHANGE_LABEL
    payload["since_at"] = LAST_STRATEGY_CHANGE_AT
    payload["since_at_display"] = _format_timestamp(LAST_STRATEGY_CHANGE_AT)
    payload["note"] = LAST_STRATEGY_CHANGE_NOTE
    return payload


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
