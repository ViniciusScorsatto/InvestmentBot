from __future__ import annotations

from collections import Counter
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


def detect_bearish_market_alignment(daily_bars: list[dict[str, Any]]) -> int:
    closes = [bar["close"] for bar in daily_bars]
    if len(closes) < 50:
        return 10
    ema20 = ema(closes, 20)[-1]
    ema50 = ema(closes, 50)[-1]
    last_close = closes[-1]
    score = 0
    if last_close < ema50:
        score += 10
    if ema20 < ema50:
        score += 10
    if last_close < ema20:
        score += 10
    return min(score, 20)


def empty_debug_counter() -> Counter[str]:
    return Counter()


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
    bars: list[dict[str, Any]],
    asset: str,
    asset_class: str,
    timeframe: str,
    market_alignment: int,
    debug_counter: Counter[str] | None = None,
) -> dict[str, Any] | None:
    if len(bars) < 60:
        if debug_counter is not None:
            debug_counter["trend_pullback_insufficient_bars"] += 1
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

    price_above_ema50 = price > ema50_value
    ema_stack_ok = ema20_value > ema50_value
    near_ema20 = abs(price - ema20_value) / price <= 0.02
    valid_rsi = rsi_value is not None and 40 <= rsi_value <= 55

    if not price_above_ema50:
        if debug_counter is not None:
            debug_counter["trend_pullback_price_below_ema50"] += 1
        return None
    if not ema_stack_ok:
        if debug_counter is not None:
            debug_counter["trend_pullback_ema_stack_failed"] += 1
        return None
    if not near_ema20:
        if debug_counter is not None:
            debug_counter["trend_pullback_not_near_ema20"] += 1
        return None
    if not valid_rsi:
        if debug_counter is not None:
            debug_counter["trend_pullback_rsi_out_of_range"] += 1
        return None

    stop = recent_low
    risk = price - stop
    if risk <= 0:
        if debug_counter is not None:
            debug_counter["trend_pullback_invalid_risk"] += 1
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
    bars: list[dict[str, Any]],
    asset: str,
    asset_class: str,
    timeframe: str,
    market_alignment: int,
    debug_counter: Counter[str] | None = None,
) -> dict[str, Any] | None:
    if len(bars) < 60:
        if debug_counter is not None:
            debug_counter["breakout_insufficient_bars"] += 1
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
    volume_above_average = volumes[-1] > avg_volume
    price_above_ema50 = price > ema50_series[-1]

    if not breakout:
        if debug_counter is not None:
            debug_counter["breakout_not_above_20_high"] += 1
        return None
    if not volume_above_average:
        if debug_counter is not None:
            debug_counter["breakout_volume_below_average"] += 1
        return None
    if not price_above_ema50:
        if debug_counter is not None:
            debug_counter["breakout_price_below_ema50"] += 1
        return None

    stop = min(prior_20_high * 0.985, recent_low)
    risk = price - stop
    if risk <= 0:
        if debug_counter is not None:
            debug_counter["breakout_invalid_risk"] += 1
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


def evaluate_bearish_pullback(
    bars: list[dict[str, Any]],
    asset: str,
    asset_class: str,
    timeframe: str,
    market_alignment: int,
    debug_counter: Counter[str] | None = None,
) -> dict[str, Any] | None:
    if len(bars) < 60:
        if debug_counter is not None:
            debug_counter["bearish_pullback_insufficient_bars"] += 1
        return None

    closes = [bar["close"] for bar in bars]
    highs = [bar["high"] for bar in bars]
    volumes = [bar["volume"] for bar in bars]
    ema20_series = ema(closes, 20)
    ema50_series = ema(closes, 50)
    rsi_series = rsi(closes, 14)

    price = closes[-1]
    ema20_value = ema20_series[-1]
    ema50_value = ema50_series[-1]
    rsi_value = rsi_series[-1]
    recent_high = max(highs[-5:])

    price_below_ema50 = price < ema50_value
    ema_stack_ok = ema20_value < ema50_value
    near_ema20 = abs(price - ema20_value) / price <= 0.02
    valid_rsi = rsi_value is not None and 45 <= rsi_value <= 60

    if not price_below_ema50:
        if debug_counter is not None:
            debug_counter["bearish_pullback_price_above_ema50"] += 1
        return None
    if not ema_stack_ok:
        if debug_counter is not None:
            debug_counter["bearish_pullback_ema_stack_failed"] += 1
        return None
    if not near_ema20:
        if debug_counter is not None:
            debug_counter["bearish_pullback_not_near_ema20"] += 1
        return None
    if not valid_rsi:
        if debug_counter is not None:
            debug_counter["bearish_pullback_rsi_out_of_range"] += 1
        return None

    stop = recent_high
    risk = stop - price
    if risk <= 0:
        if debug_counter is not None:
            debug_counter["bearish_pullback_invalid_risk"] += 1
        return None
    target = price - (2 * risk)

    components = _score_components(
        price=ema50_value + (ema50_value - price),
        ema20=ema50_value + (ema50_value - ema20_value),
        ema50=ema50_value,
        current_rsi=100 - rsi_value if rsi_value is not None else None,
        average_volume=sum(volumes[-20:]) / 20,
        volume=volumes[-1],
        breakout=False,
    )
    total_score = components["trend"] + components["momentum"] + components["setup_quality"] + market_alignment

    return {
        "asset": asset,
        "asset_class": asset_class,
        "strategy": "Bearish Pullback",
        "timeframe": timeframe,
        "entry_price": round(price, 4),
        "stop_loss": round(stop, 4),
        "target_price": round(target, 4),
        "R_multiple": round((price - target) / risk, 2),
        "score": int(min(total_score, 100)),
        "setup_notes": f"RSI {rsi_value:.1f}, EMA20 resistance, risk {risk:.2f}",
        "components": components | {"market_alignment": market_alignment},
    }


def evaluate_breakdown(
    bars: list[dict[str, Any]],
    asset: str,
    asset_class: str,
    timeframe: str,
    market_alignment: int,
    debug_counter: Counter[str] | None = None,
) -> dict[str, Any] | None:
    if len(bars) < 60:
        if debug_counter is not None:
            debug_counter["breakdown_insufficient_bars"] += 1
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
    prior_20_low = min(lows[-21:-1])
    recent_high = max(highs[-5:])
    avg_volume = avg_volume_series[-1] or volumes[-1]
    breakdown = price < prior_20_low
    volume_above_average = volumes[-1] > avg_volume
    price_below_ema50 = price < ema50_series[-1]

    if not breakdown:
        if debug_counter is not None:
            debug_counter["breakdown_not_below_20_low"] += 1
        return None
    if not volume_above_average:
        if debug_counter is not None:
            debug_counter["breakdown_volume_below_average"] += 1
        return None
    if not price_below_ema50:
        if debug_counter is not None:
            debug_counter["breakdown_price_above_ema50"] += 1
        return None

    stop = max(prior_20_low * 1.015, recent_high)
    risk = stop - price
    if risk <= 0:
        if debug_counter is not None:
            debug_counter["breakdown_invalid_risk"] += 1
        return None
    target = price - (2.5 * risk)

    current_rsi = rsi_series[-1]
    components = _score_components(
        price=ema50_series[-1] + (ema50_series[-1] - price),
        ema20=ema50_series[-1] + (ema50_series[-1] - ema20_series[-1]),
        ema50=ema50_series[-1],
        current_rsi=100 - current_rsi if current_rsi is not None else None,
        average_volume=avg_volume,
        volume=volumes[-1],
        breakout=True,
    )
    total_score = components["trend"] + components["momentum"] + components["setup_quality"] + market_alignment

    return {
        "asset": asset,
        "asset_class": asset_class,
        "strategy": "Breakdown",
        "timeframe": timeframe,
        "entry_price": round(price, 4),
        "stop_loss": round(stop, 4),
        "target_price": round(target, 4),
        "R_multiple": round((price - target) / risk, 2),
        "score": int(min(total_score, 100)),
        "setup_notes": f"20-bar breakdown, volume expansion, risk {risk:.2f}",
        "components": components | {"market_alignment": market_alignment},
    }
