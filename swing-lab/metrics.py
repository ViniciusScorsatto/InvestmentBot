from __future__ import annotations

from collections import defaultdict
from typing import Any

from trades import enrich_trade_for_display, list_trades


def _safe_pct(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return round((numerator / denominator) * 100, 2)


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
