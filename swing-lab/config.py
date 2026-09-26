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
STRATEGY_VERSION = "quality-v2-experiments-v1"
LAST_STRATEGY_CHANGE_LABEL = "Entry Quality and Controlled Experiments"
LAST_STRATEGY_CHANGE_AT = "2026-09-27T00:00:00+12:00"
LAST_STRATEGY_CHANGE_NOTE = (
    "Cost-aware entry checks, session-normalized volume, conservative learning, and matched shadow experiments. "
    "Current-version results exclude legacy trades."
)
# Illustrative per-fill costs, frozen in each new trade; calibrate to the intended venue.
SIM_FEE_BPS = float(os.getenv("SWING_LAB_SIM_FEE_BPS", "5"))
SIM_SLIPPAGE_BPS = float(os.getenv("SWING_LAB_SIM_SLIPPAGE_BPS", "5"))
if not (0 <= SIM_FEE_BPS < 10000 and 0 <= SIM_SLIPPAGE_BPS < 10000):
    raise ValueError("Simulation costs must be finite and between 0 and 10000 bps")


# Simulation limits; persisted initial cash is not reset by later environment changes.
PORTFOLIO_INITIAL_CASH = float(os.getenv("SWING_LAB_INITIAL_CASH", "10000"))
PORTFOLIO_RISK_PER_TRADE = float(os.getenv("SWING_LAB_RISK_PER_TRADE", "0.01"))
PORTFOLIO_MAX_RISK = float(os.getenv("SWING_LAB_MAX_PORTFOLIO_RISK", "0.05"))
PORTFOLIO_MAX_GROSS = float(os.getenv("SWING_LAB_MAX_GROSS_EXPOSURE", "0.80"))
PORTFOLIO_MAX_POSITION = float(os.getenv("SWING_LAB_MAX_POSITION_EXPOSURE", "0.20"))
PORTFOLIO_MAX_GROUP = float(os.getenv("SWING_LAB_MAX_GROUP_EXPOSURE", "0.30"))
PORTFOLIO_MAX_GROUP_POSITIONS = int(os.getenv("SWING_LAB_MAX_GROUP_POSITIONS", "1"))
SCAN_GRACE_SECONDS = 120
SCAN_MAX_LAG_MINUTES = 60
if not (0 < PORTFOLIO_INITIAL_CASH < 1e15):
    raise ValueError("Initial simulation cash must be positive and finite")
if not all(0 < value <= 1 for value in (PORTFOLIO_RISK_PER_TRADE, PORTFOLIO_MAX_RISK,
                                      PORTFOLIO_MAX_GROSS, PORTFOLIO_MAX_POSITION, PORTFOLIO_MAX_GROUP)):
    raise ValueError("Portfolio fractions must be in (0, 1]")
if PORTFOLIO_MAX_GROUP_POSITIONS < 1:
    raise ValueError("Group position limit must be positive")


MAX_TRADES_PER_DAY = 5
PREFERRED_TOP_SETUPS = 3
MIN_SCORE = 75
MIN_R_MULTIPLE = 2.0
DEFAULT_MAX_TRADE_DURATION_DAYS = 10
LEARNING_MODEL_ENABLED = os.getenv("SWING_LAB_LEARNING_MODEL_ENABLED", "true").lower() == "true"
LEARNING_MODEL_WEIGHT = float(os.getenv("SWING_LAB_LEARNING_MODEL_WEIGHT", "0.35"))
LEARNING_MODEL_MIN_SAMPLE = int(os.getenv("SWING_LAB_LEARNING_MODEL_MIN_SAMPLE", "30"))
LEARNING_MODEL_BLOCK_MIN_SAMPLE = int(os.getenv("SWING_LAB_LEARNING_MODEL_BLOCK_MIN_SAMPLE", "60"))
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

# A 2R gross pullback needs room for fees/slippage; cancel below 1.8 net payoff/risk.
MIN_ENTRY_NET_R = float(os.getenv("SWING_LAB_MIN_ENTRY_NET_R", "1.8"))
LEARNING_MODEL_MIN_WEEKS = 4
EARNINGS_CALENDAR_PATH = os.getenv("SWING_LAB_EARNINGS_CALENDAR_PATH", "")
EARNINGS_API_KEY = os.getenv("SWING_LAB_EARNINGS_API_KEY", "")
EARNINGS_BLACKOUT_DAYS = 2
if not 0 < MIN_ENTRY_NET_R <= 10:
    raise ValueError("Entry net reward/risk must be finite and in (0, 10]")
if LEARNING_MODEL_MIN_SAMPLE < 30 or LEARNING_MODEL_BLOCK_MIN_SAMPLE < max(60, LEARNING_MODEL_MIN_SAMPLE):
    raise ValueError("Learning requires at least 30 ranking and 60 blocking observations")
