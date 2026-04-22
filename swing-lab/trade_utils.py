from __future__ import annotations


SHORT_STRATEGIES = {"Bearish Pullback", "Breakdown"}
MEGA_CAP_TECH = {"AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "META", "GOOGL"}
BROAD_ETFS = {"SPY", "QQQ", "VOO", "IWM"}
SECTOR_ETFS = {"SMH", "XLF", "XLK"}


def get_trade_direction(strategy: str) -> str:
    return "Short" if strategy in SHORT_STRATEGIES else "Long"


def get_correlation_group(asset: str, asset_class: str) -> str:
    if asset_class == "crypto":
        return "crypto"
    if asset in MEGA_CAP_TECH:
        return "mega_cap_tech"
    if asset in BROAD_ETFS:
        return "broad_etf"
    if asset in SECTOR_ETFS:
        return "sector_etf"
    return "other_stock"
