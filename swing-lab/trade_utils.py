from __future__ import annotations


SHORT_STRATEGIES = {"Bearish Pullback", "Breakdown"}


def get_trade_direction(strategy: str) -> str:
    return "Short" if strategy in SHORT_STRATEGIES else "Long"
