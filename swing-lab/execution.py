"""Deterministic hourly OHLC simulator. No broker orders are placed."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

from config import SIM_FEE_BPS, SIM_SLIPPAGE_BPS, strategy_max_trade_duration_days
from market_bars import as_datetime, next_hour_start
from trade_utils import get_trade_direction

EXECUTION_VERSION = 2


def execution_settings(at: datetime, strategy: str, *, pending_entry: bool = True) -> dict[str, Any]:
    return {"version": EXECUTION_VERSION, "cursor": at.isoformat(),
            "pending_entry": pending_entry, "fee_bps": SIM_FEE_BPS,
            "slippage_bps": SIM_SLIPPAGE_BPS, "entry_fee_r": 0.0,
            "max_duration_days": strategy_max_trade_duration_days(strategy), "events": []}


def side(trade: dict[str, Any]) -> int:
    return 1 if get_trade_direction(trade["strategy"]) == "Long" else -1


def risk(trade: dict[str, Any]) -> float:
    return side(trade) * (trade["entry_price"] - trade["stop_loss"])


def exit_fill(trade: dict[str, Any], price: float, *, limit: bool = False) -> float:
    # Resting target/partial limits fill at their limit, without assumed improvement.
    slip = 0 if limit else trade["metadata"]["execution"]["slippage_bps"] / 10000
    return price * (1 - side(trade) * slip)


def net_result(trade: dict[str, Any], fill: float) -> float:
    settings = trade["metadata"]["execution"]
    remaining = 0.5 if trade.get("partial_taken") else 1.0
    partial = float(trade.get("partial_result_R") or 0)
    fee = remaining * fill * settings["fee_bps"] / 10000 / risk(trade)
    return (partial + remaining * side(trade) * (fill - trade["entry_price"]) / risk(trade)
            - fee - settings.get("entry_fee_r", 0.0))


def replay_bars(original: dict[str, Any], bars: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    trade = deepcopy(original)
    metadata = trade.setdefault("metadata", {})
    settings = metadata.get("execution")
    if not settings:
        # Old trades have no replay cursor: do not apply their current stop to old bars.
        settings = execution_settings(now, trade["strategy"], pending_entry=False)
        settings["migration_note"] = "Legacy position; replay begins at upgrade, prior fills preserved."
        metadata["execution"] = settings
        return trade
    if trade["status"] != "open":
        return trade
    cursor = as_datetime(settings["cursor"])

    def event(kind: str, fill: float, at: datetime, size: float) -> None:
        settings["events"].append({"kind": kind, "price": fill, "at": at.isoformat(), "size": size})

    def close(status: str, price: float, at: datetime, *, limit: bool = False) -> None:
        fill = exit_fill(trade, price, limit=limit)
        trade.update(status=status, current_price=fill, date_closed=at.isoformat(),
                     result_R=net_result(trade, fill))
        event(status, fill, at, 0.5 if trade.get("partial_taken") else 1.0)

    for bar in sorted(bars, key=lambda b: as_datetime(b["timestamp"])):
        start, end = as_datetime(bar["timestamp"]), as_datetime(bar["end_timestamp"])
        # Never use pre-entry high/low, unfinished candles, or replay the same bar twice.
        if start < cursor or end > now or end <= start:
            continue
        expected = next_hour_start(cursor, trade["asset_class"])
        if start > expected:
            settings["data_gap"] = {"expected_at": expected.isoformat(), "next_available_at": start.isoformat()}
            break
        settings.pop("data_gap", None)
        if settings["pending_entry"]:
            fill = float(bar["open"]) * (1 + side(trade) * settings["slippage_bps"] / 10000)
            trade["entry_price"] = fill
            if risk(trade) <= 0 or side(trade) * (trade["target_price"] - fill) <= 0:
                trade.update(status="cancelled", date_closed=start.isoformat(), result_R=None)
                settings.update(pending_entry=False, cursor=end.isoformat())
                event("entry_cancelled", fill, start, 0)
                break
            settings.update(pending_entry=False, entry_at=start.isoformat(),
                            entry_fee_r=fill * settings["fee_bps"] / 10000 / risk(trade))
            trade["R_multiple"] = side(trade) * (trade["target_price"] - fill) / risk(trade)
            event("entry", fill, start, 1.0)
        if risk(trade) <= 0:
            raise ValueError(f"Invalid risk on trade {trade.get('id')}")
        expiry = as_datetime(settings.get("entry_at", trade["date_opened"])) + timedelta(days=settings["max_duration_days"])
        stop = trade.get("effective_stop_loss") or trade["stop_loss"]
        target = trade["target_price"]
        trigger = trade["entry_price"] + side(trade) * risk(trade)
        op, high, low = float(bar["open"]), float(bar["high"]), float(bar["low"])
        adverse = low if side(trade) == 1 else high
        favorable = high if side(trade) == 1 else low
        stop_gap = side(trade) * (op - stop) <= 0
        target_gap = side(trade) * (op - target) >= 0

        def activate(at: datetime) -> None:
            # A gapped entry can leave the target nearer than +1R; the target then
            # exits the position before any partial/runner trigger can be reached.
            if side(trade) * (target - trigger) < 0:
                return
            if trade.get("runner_activated") or trade.get("partial_taken"):
                return
            if trade["strategy"] == "Breakout":
                trade.update(runner_activated=True, runner_activated_at=at.isoformat())
                event("runner_activated", trigger, at, 0)
            else:
                fill = exit_fill(trade, trigger, limit=True)
                partial_r = 0.5 * (side(trade) * (fill - trade["entry_price"])
                                   - fill * settings["fee_bps"] / 10000) / risk(trade)
                trade.update(partial_taken=True, partial_taken_at=at.isoformat(),
                             partial_price=fill, partial_result_R=partial_r)
                event("partial", fill, at, 0.5)
            trade["effective_stop_loss"] = trade["entry_price"]

        # Opening prices establish chronology for gaps. Intrabar ordering is conservative:
        # the existing stop wins a stop/target tie, then a newly raised stop wins a tie.
        if stop_gap:
            close("stopped", op, start)
        elif target_gap:
            activate(start)
            close("target_hit", target, start, limit=True)
        elif start >= expiry:
            close("closed", op, start)
        else:
            if side(trade) * (op - trigger) >= 0:
                activate(start)
            stop = trade.get("effective_stop_loss") or trade["stop_loss"]
            if side(trade) * (adverse - stop) <= 0:
                close("stopped", stop, end)
            else:
                if side(trade) * (favorable - trigger) >= 0:
                    activate(end)
                stop = trade.get("effective_stop_loss") or trade["stop_loss"]
                if side(trade) * (adverse - stop) <= 0:
                    close("stopped", stop, end)
                elif side(trade) * (favorable - target) >= 0:
                    close("target_hit", target, end, limit=True)
                elif end >= expiry:
                    close("closed", float(bar["close"]), end)
                else:
                    trade["current_price"] = float(bar["close"])
        settings["cursor"] = end.isoformat()
        cursor = end
        if trade["status"] != "open":
            break
    return trade
