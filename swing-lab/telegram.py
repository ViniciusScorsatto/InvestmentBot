from __future__ import annotations

from datetime import datetime, timezone
from db import get_db
from outbox import enqueue
from trade_utils import get_trade_direction


def new_trade_message(trade: dict) -> str:
    return "\n".join([
        "NEW SIMULATED SETUP", "", f"Asset: {trade['asset']}",
        f"Direction: {get_trade_direction(trade['strategy'])}",
        f"Strategy: {trade['strategy']}", f"Timeframe: {trade['timeframe']}",
        f"Score: {trade['score']}", f"Signal price: {trade['entry_price']}",
        "Entry: next hourly bar open, with configured slippage",
        f"Stop: {trade['stop_loss']}", f"Target: {trade['target_price']}",
        f"RR: {trade['R_multiple']}",
    ])


def closed_trade_message(trade: dict, status: str, result_r: float) -> str:
    return "\n".join(["SIMULATED TRADE CLOSED", "", f"Asset: {trade['asset']}",
                      f"Direction: {get_trade_direction(trade['strategy'])}",
                      f"Result: {status}", f"R: {result_r:.2f}"])


def notify_new_trade(trade: dict, connection=None) -> None:
    key = f"trade:{trade['id']}:opened"
    if connection is not None:
        enqueue(connection, key, new_trade_message(trade))
    else:
        with get_db() as connection:
            enqueue(connection, key, new_trade_message(trade))


def notify_trade_closed(trade: dict, status: str, result_r: float, connection=None) -> None:
    key = f"trade:{trade['id']}:closed"
    if connection is not None:
        enqueue(connection, key, closed_trade_message(trade, status, result_r))
    else:
        with get_db() as connection:
            enqueue(connection, key, closed_trade_message(trade, status, result_r))


def notify_daily_summary(summary: dict) -> None:
    day = datetime.now(timezone.utc).date().isoformat()
    message = "\n".join(["DAILY SUMMARY", "", f"Trades: {summary['total_trades']}",
                           f"Win Rate: {summary['win_rate']}%", f"Avg R: {summary['avg_R']}",
                           f"Total R: {summary['total_R']}"])
    with get_db() as connection:
        enqueue(connection, f"daily-summary:{day}", message)
