from __future__ import annotations

import json
import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"

APP_NAME = "Swing Lab Auto"
APP_HOST = os.getenv("SWING_LAB_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("PORT", os.getenv("SWING_LAB_PORT", "8000")))
DATABASE_URL = os.getenv("DATABASE_URL", "")
APP_VERSION = os.getenv("RAILWAY_GIT_COMMIT_SHA", os.getenv("RAILWAY_DEPLOYMENT_ID", "local"))
LAST_STRATEGY_CHANGE_LABEL = "Learning Model Overlay"
LAST_STRATEGY_CHANGE_AT = "2026-05-30T00:00:00+12:00"
LAST_STRATEGY_CHANGE_NOTE = (
    "Candidates keep using live market APIs and deterministic strategy rules, then receive a learned edge score from closed trade outcomes. "
    "The model only blocks setups after enough similar historical trades exist."
)

MAX_TRADES_PER_DAY = 5
PREFERRED_TOP_SETUPS = 3
MIN_SCORE = 75
MIN_R_MULTIPLE = 2.0
DEFAULT_MAX_TRADE_DURATION_DAYS = 10
LEARNING_MODEL_ENABLED = os.getenv("SWING_LAB_LEARNING_MODEL_ENABLED", "true").lower() == "true"
LEARNING_MODEL_WEIGHT = float(os.getenv("SWING_LAB_LEARNING_MODEL_WEIGHT", "0.35"))
LEARNING_MODEL_MIN_SAMPLE = int(os.getenv("SWING_LAB_LEARNING_MODEL_MIN_SAMPLE", "8"))
LEARNING_MODEL_MIN_SCORE = int(os.getenv("SWING_LAB_LEARNING_MODEL_MIN_SCORE", "45"))

STRATEGY_SETTINGS = {
    "Trend Pullback": {
        "enabled": True,
        "max_trade_duration_days": DEFAULT_MAX_TRADE_DURATION_DAYS,
        "allowed_timeframes": ["4h", "1d"],
        "status_note": "Primary long pullback setup with extra daily confirmation on 4h entries.",
    },
    "Breakout": {
        "enabled": True,
        "max_trade_duration_days": 15,
        "allowed_timeframes": ["4h"],
        "status_note": "Runner-style long breakout with breakeven after +1R, limited to 4h.",
    },
    "Bearish Pullback": {
        "enabled": False,
        "max_trade_duration_days": DEFAULT_MAX_TRADE_DURATION_DAYS,
        "allowed_timeframes": ["4h", "1d"],
        "status_note": "Disabled while the system stays long-only.",
    },
    "Breakdown": {
        "enabled": False,
        "max_trade_duration_days": DEFAULT_MAX_TRADE_DURATION_DAYS,
        "allowed_timeframes": ["4h", "1d"],
        "status_note": "Disabled while the system stays long-only.",
    },
}

SCAN_INTERVAL_HOURS = 4
UPDATE_INTERVAL_MINUTES = 20
DAILY_SUMMARY_HOUR = 18
US_MARKET_TIMEZONE = "America/New_York"
US_MARKET_OPEN_HOUR = 9
US_MARKET_OPEN_MINUTE = 30
US_MARKET_CLOSE_HOUR = 16
US_MARKET_CLOSE_MINUTE = 0

MARKET_DATA_CACHE_TTL_SECONDS = {
    "crypto": 900,
    "stock": 900,
    "etf": 900,
}
MARKET_DATA_CACHE_RETENTION_DAYS = 28
CRYPTO_REQUEST_DELAY_SECONDS = 1.2

TELEGRAM_BOT_TOKEN = os.getenv("SWING_LAB_TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("SWING_LAB_TELEGRAM_CHAT_ID", "")

ETF_WATCHLIST = ["SPY", "QQQ", "VOO", "IWM", "SMH", "XLF", "XLK"]
STOCK_WATCHLIST = ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "META", "GOOGL"]
CRYPTO_WATCHLIST = ["BTC", "ETH", "SOL", "XRP", "ADA", "AVAX", "LINK"]
UNSUPPORTED_CRYPTO_WATCHLIST = ["BNB"]

WATCHLIST = {
    "etf": ETF_WATCHLIST,
    "stock": STOCK_WATCHLIST,
    "crypto": CRYPTO_WATCHLIST,
}

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"

CRYPTO_SYMBOL_TO_KRAKEN_PAIR = {
    "BTC": "BTC/USD",
    "ETH": "ETH/USD",
    "SOL": "SOL/USD",
    "XRP": "XRP/USD",
    "ADA": "ADA/USD",
    "AVAX": "AVAX/USD",
    "LINK": "LINK/USD",
}


def cache_ttl_for(asset_class: str) -> int:
    return int(MARKET_DATA_CACHE_TTL_SECONDS.get(asset_class, 900))


def default_cached_dataset() -> str:
    return json.dumps({"4h": [], "1d": []})


def strategy_settings(strategy: str) -> dict[str, object]:
    return dict(
        STRATEGY_SETTINGS.get(
            strategy,
            {
                "enabled": True,
                "max_trade_duration_days": DEFAULT_MAX_TRADE_DURATION_DAYS,
                "allowed_timeframes": ["4h", "1d"],
                "status_note": "",
            },
        )
    )


def strategy_enabled(strategy: str) -> bool:
    return bool(strategy_settings(strategy).get("enabled", True))


def strategy_max_trade_duration_days(strategy: str) -> int:
    return int(strategy_settings(strategy).get("max_trade_duration_days", DEFAULT_MAX_TRADE_DURATION_DAYS))


def strategy_allows_timeframe(strategy: str, timeframe: str) -> bool:
    allowed_timeframes = strategy_settings(strategy).get("allowed_timeframes", ["4h", "1d"])
    return timeframe in allowed_timeframes


def strategy_status_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for strategy, settings in STRATEGY_SETTINGS.items():
        rows.append(
            {
                "strategy": strategy,
                "enabled": bool(settings.get("enabled", True)),
                "status_label": "Enabled" if settings.get("enabled", True) else "Disabled",
                "max_trade_duration_days": int(
                    settings.get("max_trade_duration_days", DEFAULT_MAX_TRADE_DURATION_DAYS)
                ),
                "allowed_timeframes": ", ".join(settings.get("allowed_timeframes", ["4h", "1d"])),
                "status_note": str(settings.get("status_note", "")),
            }
        )
    return rows
