from __future__ import annotations

import json
import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "trades.db"
TEMPLATES_DIR = BASE_DIR / "templates"

APP_NAME = "Swing Lab Auto"
APP_HOST = os.getenv("SWING_LAB_HOST", "127.0.0.1")
APP_PORT = int(os.getenv("SWING_LAB_PORT", "8000"))

MAX_TRADES_PER_DAY = 5
PREFERRED_TOP_SETUPS = 3
MIN_SCORE = 75
MIN_R_MULTIPLE = 2.0
MAX_TRADE_DURATION_DAYS = 10

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
CRYPTO_REQUEST_DELAY_SECONDS = 1.2

TELEGRAM_BOT_TOKEN = os.getenv("SWING_LAB_TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("SWING_LAB_TELEGRAM_CHAT_ID", "")

ETF_WATCHLIST = ["SPY", "QQQ", "VOO", "IWM", "SMH", "XLF", "XLK"]
STOCK_WATCHLIST = ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "META", "GOOGL"]
CRYPTO_WATCHLIST = ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "AVAX", "LINK"]

WATCHLIST = {
    "etf": ETF_WATCHLIST,
    "stock": STOCK_WATCHLIST,
    "crypto": CRYPTO_WATCHLIST,
}

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
COINGECKO_MARKET_CHART_URL = "https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"

CRYPTO_SYMBOL_TO_COINGECKO_ID = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "BNB": "binancecoin",
    "XRP": "ripple",
    "ADA": "cardano",
    "AVAX": "avalanche-2",
    "LINK": "chainlink",
}


def cache_ttl_for(asset_class: str) -> int:
    return int(MARKET_DATA_CACHE_TTL_SECONDS.get(asset_class, 900))


def default_cached_dataset() -> str:
    return json.dumps({"4h": [], "1d": []})
