from __future__ import annotations

import logging

import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from trade_utils import get_trade_direction


LOGGER = logging.getLogger(__name__)


def send_message(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        LOGGER.info("Telegram not configured, skipping message: %s", text.splitlines()[0])
        return False

    response = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data={"chat_id": TELEGRAM_CHAT_ID, "text": text},
        timeout=20,
    )
    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        LOGGER.warning("Telegram send failed: %s", exc)
        return False
    return True


def notify_new_trade(trade: dict) -> None:
    direction = get_trade_direction(trade["strategy"])
    send_message(
        "\n".join(
            [
                "🚀 NEW TRADE",
                "",
                f"Asset: {trade['asset']}",
                f"Direction: {direction}",
                f"Strategy: {trade['strategy']}",
                f"Timeframe: {trade['timeframe']}",
                f"Score: {trade['score']}",
                "",
                f"Entry: {trade['entry_price']}",
                f"Stop: {trade['stop_loss']}",
                f"Target: {trade['target_price']}",
                f"RR: {trade['R_multiple']}",
            ]
        )
    )


def notify_trade_closed(trade: dict, status: str, result_r: float) -> None:
    label = {"stopped": "Stop Loss", "target_hit": "Target Hit", "closed": "Timed Exit"}.get(status, status)
    direction = get_trade_direction(trade["strategy"])
    send_message(
        "\n".join(
            [
                "❌ TRADE CLOSED",
                "",
                f"Asset: {trade['asset']}",
                f"Direction: {direction}",
                f"Result: {label}",
                f"R: {round(result_r, 2)}",
            ]
        )
    )


def notify_daily_summary(summary: dict) -> None:
    send_message(
        "\n".join(
            [
                "📊 DAILY SUMMARY",
                "",
                f"Trades: {summary['total_trades']}",
                f"Win Rate: {summary['win_rate']}%",
                f"Avg R: {summary['avg_R']}",
                f"Total R: {summary['total_R']}",
            ]
        )
    )
