from __future__ import annotations

from typing import Any


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    multiplier = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append((value - result[-1]) * multiplier + result[-1])
    return result


def sma(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = []
    for index in range(len(values)):
        if index + 1 < period:
            result.append(None)
        else:
            window = values[index - period + 1 : index + 1]
            result.append(sum(window) / period)
    return result


def rsi(values: list[float], period: int = 14) -> list[float | None]:
    if len(values) < period + 1:
        return [None] * len(values)

    gains: list[float] = []
    losses: list[float] = []
    for index in range(1, len(values)):
        change = values[index] - values[index - 1]
        gains.append(max(change, 0))
        losses.append(abs(min(change, 0)))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    series: list[float | None] = [None] * period

    if avg_loss == 0:
        series.append(100.0)
    else:
        relative_strength = avg_gain / avg_loss
        series.append(100 - (100 / (1 + relative_strength)))

    for index in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[index]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[index]) / period
        if avg_loss == 0:
            series.append(100.0)
        else:
            relative_strength = avg_gain / avg_loss
            series.append(100 - (100 / (1 + relative_strength)))

    while len(series) < len(values):
        series.append(None)
    return series


def detect_market_alignment(daily_bars: list[dict[str, Any]]) -> int:
    closes = [bar["close"] for bar in daily_bars]
    if len(closes) < 50:
        return 10
    ema20 = ema(closes, 20)[-1]
    ema50 = ema(closes, 50)[-1]
    last_close = closes[-1]
    score = 0
    if last_close > ema50:
        score += 10
    if ema20 > ema50:
        score += 10
    if last_close > ema20:
        score += 10
    return min(score, 20)


def _score_components(
    price: float,
    ema20: float,
    ema50: float,
    current_rsi: float | None,
    average_volume: float,
    volume: float,
    breakout: bool,
) -> dict[str, int]:
    trend_score = 0
    momentum_score = 0
    setup_score = 0

    if price > ema50:
        trend_score += 15
    if ema20 > ema50:
        trend_score += 15

    if current_rsi is not None and 45 <= current_rsi <= 65:
        momentum_score += 15
    elif current_rsi is not None and 40 <= current_rsi <= 70:
        momentum_score += 10
    if volume >= average_volume:
        momentum_score += 10

    price_distance = abs(price - ema20) / price if price else 1
    if price_distance <= 0.015:
        setup_score += 15
    elif price_distance <= 0.03:
        setup_score += 8

    if breakout:
        setup_score += 10

    return {
        "trend": min(trend_score, 30),
        "momentum": min(momentum_score, 25),
        "setup_quality": min(setup_score, 25),
    }


def evaluate_trend_pullback(
    bars: list[dict[str, Any]], asset: str, asset_class: str, timeframe: str, market_alignment: int
) -> dict[str, Any] | None:
    if len(bars) < 60:
        return None

    closes = [bar["close"] for bar in bars]
    lows = [bar["low"] for bar in bars]
    volumes = [bar["volume"] for bar in bars]
    ema20_series = ema(closes, 20)
    ema50_series = ema(closes, 50)
    rsi_series = rsi(closes, 14)

    price = closes[-1]
    ema20_value = ema20_series[-1]
    ema50_value = ema50_series[-1]
    rsi_value = rsi_series[-1]
    recent_low = min(lows[-5:])

    near_ema20 = abs(price - ema20_value) / price <= 0.02
    valid_rsi = rsi_value is not None and 40 <= rsi_value <= 55

    if not (price > ema50_value and ema20_value > ema50_value and near_ema20 and valid_rsi):
        return None

    stop = recent_low
    risk = price - stop
    if risk <= 0:
        return None
    target = price + (2 * risk)

    components = _score_components(
        price=price,
        ema20=ema20_value,
        ema50=ema50_value,
        current_rsi=rsi_value,
        average_volume=sum(volumes[-20:]) / 20,
        volume=volumes[-1],
        breakout=False,
    )
    total_score = components["trend"] + components["momentum"] + components["setup_quality"] + market_alignment

    return {
        "asset": asset,
        "asset_class": asset_class,
        "strategy": "Trend Pullback",
        "timeframe": timeframe,
        "entry_price": round(price, 4),
        "stop_loss": round(stop, 4),
        "target_price": round(target, 4),
        "R_multiple": round((target - price) / risk, 2),
        "score": int(min(total_score, 100)),
        "setup_notes": f"RSI {rsi_value:.1f}, EMA20 support, risk {risk:.2f}",
        "components": components | {"market_alignment": market_alignment},
    }


def evaluate_breakout(
    bars: list[dict[str, Any]], asset: str, asset_class: str, timeframe: str, market_alignment: int
) -> dict[str, Any] | None:
    if len(bars) < 60:
        return None

    closes = [bar["close"] for bar in bars]
    highs = [bar["high"] for bar in bars]
    lows = [bar["low"] for bar in bars]
    volumes = [bar["volume"] for bar in bars]
    ema20_series = ema(closes, 20)
    ema50_series = ema(closes, 50)
    rsi_series = rsi(closes, 14)
    avg_volume_series = sma(volumes, 20)

    price = closes[-1]
    prior_20_high = max(highs[-21:-1])
    recent_low = min(lows[-5:])
    avg_volume = avg_volume_series[-1] or volumes[-1]
    breakout = price > prior_20_high

    if not (breakout and volumes[-1] > avg_volume and price > ema50_series[-1]):
        return None

    stop = min(prior_20_high * 0.985, recent_low)
    risk = price - stop
    if risk <= 0:
        return None
    target = price + (2.5 * risk)

    components = _score_components(
        price=price,
        ema20=ema20_series[-1],
        ema50=ema50_series[-1],
        current_rsi=rsi_series[-1],
        average_volume=avg_volume,
        volume=volumes[-1],
        breakout=True,
    )
    total_score = components["trend"] + components["momentum"] + components["setup_quality"] + market_alignment

    return {
        "asset": asset,
        "asset_class": asset_class,
        "strategy": "Breakout",
        "timeframe": timeframe,
        "entry_price": round(price, 4),
        "stop_loss": round(stop, 4),
        "target_price": round(target, 4),
        "R_multiple": round((target - price) / risk, 2),
        "score": int(min(total_score, 100)),
        "setup_notes": f"20-bar breakout, volume expansion, risk {risk:.2f}",
        "components": components | {"market_alignment": market_alignment},
    }
