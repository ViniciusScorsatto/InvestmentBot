from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sys
import types
import unittest
from unittest.mock import Mock, patch

fake_psycopg = types.ModuleType("psycopg")
fake_psycopg.connect = lambda *args, **kwargs: None
fake_psycopg.Connection = object
fake_rows = types.ModuleType("psycopg.rows")
fake_rows.dict_row = object()
sys.modules.setdefault("psycopg", fake_psycopg)
sys.modules.setdefault("psycopg.rows", fake_rows)

import scanner
import strategies
import trades


def make_bars(length: int = 60, close: float = 100.0, high: float = 101.0, low: float = 99.0) -> list[dict[str, float | str]]:
    bars: list[dict[str, float | str]] = []
    for index in range(length):
        bars.append(
            {
                "timestamp": f"2026-01-{(index % 28) + 1:02d}T00:00:00+00:00",
                "open": close - 0.5,
                "high": high,
                "low": low,
                "close": close,
                "volume": 100.0,
            }
        )
    return bars


class StrategyAdjustmentTests(unittest.TestCase):
    def test_trend_pullback_rejects_old_proximity_band_candidate(self) -> None:
        bars = make_bars()
        prior_lows = [90.0, 91.0, 92.0, 93.0, 94.0]
        recent_lows = [95.0, 96.0, 97.0, 98.0, 99.0]
        for offset, low in enumerate(prior_lows, start=10):
            bars[-offset]["low"] = low
        for offset, low in enumerate(recent_lows, start=5):
            bars[-offset]["low"] = low

        def fake_ema(_: list[float], period: int) -> list[float]:
            return [98.3 if period == 20 else 95.0] * len(bars)

        with patch.object(strategies, "ema", side_effect=fake_ema), patch.object(strategies, "rsi", return_value=[50.0] * len(bars)):
            trade = strategies.evaluate_trend_pullback(bars, "SPY", "etf", "4h", 20)

        self.assertIsNone(trade)

    def test_trend_pullback_requires_recent_higher_low(self) -> None:
        bars = make_bars()
        flat_lows = [95.0, 96.0, 97.0, 98.0, 99.0]
        for offset, low in enumerate(flat_lows, start=10):
            bars[-offset]["low"] = low
        for offset, low in enumerate(flat_lows, start=5):
            bars[-offset]["low"] = low

        def fake_ema(_: list[float], period: int) -> list[float]:
            return [99.0 if period == 20 else 95.0] * len(bars)

        with patch.object(strategies, "ema", side_effect=fake_ema), patch.object(strategies, "rsi", return_value=[52.0] * len(bars)):
            trade = strategies.evaluate_trend_pullback(bars, "QQQ", "etf", "4h", 20)

        self.assertIsNone(trade)

    def test_breakout_candidate_uses_three_r_target(self) -> None:
        bars = make_bars(close=119.0, high=120.0, low=116.0)
        for bar in bars[:-1]:
            bar["high"] = 120.0
            bar["low"] = 116.0
            bar["volume"] = 100.0
        bars[-1]["close"] = 121.0
        bars[-1]["high"] = 122.0
        bars[-1]["low"] = 118.0
        bars[-1]["volume"] = 180.0

        def fake_ema(_: list[float], period: int) -> list[float]:
            return [115.0 if period == 20 else 110.0] * len(bars)

        with patch.object(strategies, "ema", side_effect=fake_ema), patch.object(strategies, "rsi", return_value=[60.0] * len(bars)), patch.object(
            strategies, "sma", return_value=[100.0] * len(bars)
        ):
            trade = strategies.evaluate_breakout(bars, "AAPL", "stock", "4h", 20)

        self.assertIsNotNone(trade)
        self.assertEqual(trade["target_price"], 130.0)
        self.assertEqual(trade["R_multiple"], 3.0)

    def test_breakout_runner_stops_at_breakeven_after_one_r(self) -> None:
        now = datetime(2026, 5, 1, tzinfo=timezone.utc)
        state = {
            "id": 1,
            "asset": "BTC",
            "asset_class": "crypto",
            "strategy": "Breakout",
            "timeframe": "4h",
            "entry_price": 100.0,
            "stop_loss": 95.0,
            "target_price": 115.0,
            "current_price": 100.0,
            "R_multiple": 3.0,
            "score": 80,
            "date_opened": now.isoformat(),
            "status": "open",
            "effective_stop_loss": 95.0,
            "runner_activated": False,
            "partial_taken": False,
            "partial_result_R": 0.0,
            "setup_notes": "",
            "metadata_json": None,
            "result_R": None,
        }
        prices = iter([105.0, 99.0])

        def fake_list_trades(status: str | None = None, **_: object) -> list[dict[str, object]]:
            if status == "open" and state["status"] == "open":
                return [dict(state)]
            return []

        def fake_execute(query: str, params: tuple[object, ...] = ()) -> int | None:
            if "SET current_price = %s WHERE id = %s" in query:
                state["current_price"] = params[0]
            elif "SET runner_activated = true" in query:
                state["runner_activated"] = True
                state["runner_activated_at"] = params[0].isoformat()
                state["effective_stop_loss"] = state["entry_price"]
                state["current_price"] = params[1]
            elif "SET status = %s, current_price = %s, date_closed = %s, result_R = %s" in query:
                state["status"] = params[0]
                state["current_price"] = params[1]
                state["date_closed"] = params[2].isoformat()
                state["result_R"] = params[3]
            return None

        with patch.object(trades, "_now_utc", return_value=now), patch.object(trades, "list_trades", side_effect=fake_list_trades), patch.object(
            trades, "_current_price_for_trade", side_effect=lambda _: next(prices)
        ), patch.object(trades, "execute", side_effect=fake_execute), patch.object(trades, "get_trade", side_effect=lambda _: dict(state)), patch.object(
            trades, "notify_trade_closed"
        ):
            trades.update_open_trades(asset_classes=["crypto"])
            self.assertTrue(state["runner_activated"])
            self.assertEqual(state["effective_stop_loss"], 100.0)
            self.assertEqual(state["status"], "open")

            trades.update_open_trades(asset_classes=["crypto"])

        self.assertEqual(state["status"], "stopped")
        self.assertEqual(state["result_R"], 0.0)

    def test_strategy_specific_timed_exits(self) -> None:
        now = datetime(2026, 5, 1, tzinfo=timezone.utc)
        breakout = {
            "id": 2,
            "asset": "MSFT",
            "asset_class": "stock",
            "strategy": "Breakout",
            "timeframe": "4h",
            "entry_price": 100.0,
            "stop_loss": 95.0,
            "target_price": 115.0,
            "current_price": 101.0,
            "R_multiple": 3.0,
            "score": 82,
            "date_opened": (now - timedelta(days=10)).isoformat(),
            "status": "open",
            "effective_stop_loss": 95.0,
            "runner_activated": False,
            "partial_taken": False,
            "partial_result_R": 0.0,
            "setup_notes": "",
            "metadata_json": None,
            "result_R": None,
        }
        trend = {
            "id": 3,
            "asset": "SPY",
            "asset_class": "etf",
            "strategy": "Trend Pullback",
            "timeframe": "4h",
            "entry_price": 100.0,
            "stop_loss": 95.0,
            "target_price": 110.0,
            "current_price": 101.0,
            "R_multiple": 2.0,
            "score": 79,
            "date_opened": (now - timedelta(days=10)).isoformat(),
            "status": "open",
            "effective_stop_loss": 95.0,
            "runner_activated": False,
            "partial_taken": False,
            "partial_result_R": 0.0,
            "setup_notes": "",
            "metadata_json": None,
            "result_R": None,
        }
        state = {2: breakout, 3: trend}

        def fake_list_trades(status: str | None = None, **_: object) -> list[dict[str, object]]:
            if status == "open":
                return [dict(trade) for trade in state.values() if trade["status"] == "open"]
            return []

        def fake_current_price(trade: dict[str, object]) -> float:
            return float(state[int(trade["id"])]["current_price"])

        def fake_execute(query: str, params: tuple[object, ...] = ()) -> int | None:
            if "SET current_price = %s WHERE id = %s" in query:
                state[int(params[1])]["current_price"] = params[0]
            elif "SET status = %s, current_price = %s, date_closed = %s, result_R = %s" in query:
                trade_state = state[int(params[4])]
                trade_state["status"] = params[0]
                trade_state["current_price"] = params[1]
                trade_state["date_closed"] = params[2].isoformat()
                trade_state["result_R"] = params[3]
            return None

        with patch.object(trades, "_now_utc", return_value=now), patch.object(trades, "list_trades", side_effect=fake_list_trades), patch.object(
            trades, "_current_price_for_trade", side_effect=fake_current_price
        ), patch.object(trades, "execute", side_effect=fake_execute), patch.object(trades, "get_trade", side_effect=lambda trade_id: dict(state[trade_id])), patch.object(
            trades, "notify_trade_closed"
        ):
            trades.update_open_trades(asset_classes=["stock", "etf"])

        self.assertEqual(state[2]["status"], "open")
        self.assertEqual(state[3]["status"], "closed")

        state[2]["date_opened"] = (now - timedelta(days=15)).isoformat()
        with patch.object(trades, "_now_utc", return_value=now), patch.object(trades, "list_trades", side_effect=fake_list_trades), patch.object(
            trades, "_current_price_for_trade", side_effect=fake_current_price
        ), patch.object(trades, "execute", side_effect=fake_execute), patch.object(trades, "get_trade", side_effect=lambda trade_id: dict(state[trade_id])), patch.object(
            trades, "notify_trade_closed"
        ):
            trades.update_open_trades(asset_classes=["stock"])

        self.assertEqual(state[2]["status"], "closed")

    def test_breakdown_is_not_evaluated_when_disabled(self) -> None:
        dataset = {"4h": make_bars(), "1d": make_bars()}
        fake_breakdown = Mock(return_value={"strategy": "Breakdown"})

        with patch.object(scanner, "_regime_by_asset_class", return_value={"crypto": "bearish"}), patch.object(
            scanner, "fetch_asset_data", return_value=dataset
        ), patch.object(scanner, "SHORT_EVALUATORS", (("Breakdown", fake_breakdown),)):
            candidates, _, _, _ = scanner.scan_market(asset_classes=["crypto"])

        self.assertEqual(fake_breakdown.call_count, 0)
        self.assertFalse(any(candidate["strategy"] == "Breakdown" for candidate in candidates))


if __name__ == "__main__":
    unittest.main()
