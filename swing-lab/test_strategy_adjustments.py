from __future__ import annotations

import json
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

fake_fastapi = types.ModuleType("fastapi")


class _FakeRouter:
    def get(self, *_args, **_kwargs):
        def decorator(func):
            return func

        return decorator


fake_fastapi.APIRouter = _FakeRouter
fake_fastapi.Query = lambda default=None, **_kwargs: default
fake_fastapi.Request = object
sys.modules.setdefault("fastapi", fake_fastapi)

fake_fastapi_responses = types.ModuleType("fastapi.responses")
fake_fastapi_responses.JSONResponse = object
fake_fastapi_responses.HTMLResponse = object
fake_fastapi_responses.RedirectResponse = object
sys.modules.setdefault("fastapi.responses", fake_fastapi_responses)

fake_fastapi_templating = types.ModuleType("fastapi.templating")


class _FakeTemplates:
    def __init__(self, *args, **kwargs):
        pass

    def TemplateResponse(self, *args, **kwargs):
        return None


fake_fastapi_templating.Jinja2Templates = _FakeTemplates
sys.modules.setdefault("fastapi.templating", fake_fastapi_templating)

import scanner
import strategies
import trades
import api
import learning_model


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

    def test_trend_pullback_4h_requires_daily_confirmation(self) -> None:
        bars = make_bars()
        prior_lows = [90.0, 91.0, 92.0, 93.0, 94.0]
        recent_lows = [95.0, 96.0, 97.0, 98.0, 99.0]
        for offset, low in enumerate(prior_lows, start=10):
            bars[-offset]["low"] = low
        for offset, low in enumerate(recent_lows, start=5):
            bars[-offset]["low"] = low
        daily_bars = make_bars(length=70, close=100.0)

        def fake_ema(values: list[float], period: int) -> list[float]:
            if len(values) == len(daily_bars):
                return [101.0 if period == 20 else 95.0] * len(values)
            return [99.0 if period == 20 else 95.0] * len(values)

        with patch.object(strategies, "ema", side_effect=fake_ema), patch.object(
            strategies, "rsi", side_effect=[[52.0] * len(bars), [49.0] * len(daily_bars)]
        ):
            trade = strategies.evaluate_trend_pullback(bars, "SPY", "etf", "4h", 20, daily_bars=daily_bars)

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

    def test_breakout_one_day_is_not_evaluated_when_timeframe_disabled(self) -> None:
        dataset = {"4h": make_bars(), "1d": make_bars()}
        fake_breakout = Mock(return_value=None)

        with patch.object(scanner, "_regime_by_asset_class", return_value={"stock": "bullish"}), patch.object(
            scanner, "fetch_asset_data", return_value=dataset
        ), patch.object(scanner, "LONG_EVALUATORS", (("Breakout", fake_breakout),)):
            scanner.scan_market(asset_classes=["stock"])

        self.assertEqual(fake_breakout.call_count, 7)

    def test_analytics_page_defaults_to_last_strategy_change(self) -> None:
        with patch.object(api, "analytics_payload", return_value={"strategy_stats": [], "strategy_status": [], "asset_class_stats": [], "direction_stats": [], "setup_slice_stats": [], "summary": {}}) as payload_mock, patch.object(
            api, "analytics_since_strategy_change",
            return_value={"summary": {}, "label": "x", "since_at_display": "y", "note": "z"},
        ), patch.object(api.templates, "TemplateResponse", return_value="ok"):
            response = api.analytics_page(request=object(), start_date=None, end_date=None)

        self.assertEqual(response, "ok")
        self.assertEqual(payload_mock.call_args.kwargs["start_date"], "2026-05-30")

    def test_learning_model_blocks_repeated_bad_slice(self) -> None:
        rows = [
            {
                "asset": "AAPL",
                "asset_class": "stock",
                "strategy": "Breakout",
                "timeframe": "4h",
                "result_R": -1.0,
                "metadata_json": '{"features": {"rsi": 62, "volume_ratio": 1.3, "ema_gap_pct": 0.02}}',
            }
            for _ in range(8)
        ]
        setup = {
            "asset": "AAPL",
            "asset_class": "stock",
            "strategy": "Breakout",
            "timeframe": "4h",
            "components": {"features": {"rsi": 62, "volume_ratio": 1.3, "ema_gap_pct": 0.02}},
        }

        learning_model.clear_learning_cache()
        with patch.object(learning_model, "fetch_all", return_value=rows):
            feedback = learning_model.score_setup(setup)
        learning_model.clear_learning_cache()

        self.assertEqual(feedback["confidence"], "active")
        self.assertFalse(feedback["approved"])
        self.assertLess(feedback["model_score"], 45)

    def test_learning_model_reads_postgres_folded_result_column(self) -> None:
        rows = [
            {
                "asset": "AAPL",
                "asset_class": "stock",
                "strategy": "Breakout",
                "timeframe": "4h",
                "result_r": -1.0,
                "metadata_json": '{"features": {"rsi": 62, "volume_ratio": 1.3, "ema_gap_pct": 0.02}}',
            }
            for _ in range(8)
        ]

        learning_model.clear_learning_cache()
        with patch.object(learning_model, "fetch_all", return_value=rows):
            stats = learning_model.learned_stats()
            feedback = learning_model.score_setup(
                {
                    "asset": "AAPL",
                    "asset_class": "stock",
                    "strategy": "Breakout",
                    "timeframe": "4h",
                    "components": {"features": {"rsi": 62, "volume_ratio": 1.3, "ema_gap_pct": 0.02}},
                }
            )
        learning_model.clear_learning_cache()

        self.assertEqual(stats[("all",)].trades, 8)
        self.assertEqual(feedback["confidence"], "active")
        self.assertFalse(feedback["approved"])

    def test_trade_rows_normalize_folded_r_columns(self) -> None:
        trade = trades._row_to_trade(
            {
                "asset": "AAPL",
                "asset_class": "stock",
                "strategy": "Breakout",
                "timeframe": "4h",
                "date_opened": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "r_multiple": 3.0,
                "result_r": 1.2,
                "partial_result_r": 0.5,
            }
        )

        self.assertEqual(trade["R_multiple"], 3.0)
        self.assertEqual(trade["result_R"], 1.2)
        self.assertEqual(trade["partial_result_R"], 0.5)

    def test_scanner_rejects_candidate_when_learning_model_disapproves(self) -> None:
        dataset = {"4h": make_bars(), "1d": make_bars()}

        def fake_breakout(
            bars: list[dict[str, object]],
            asset: str,
            asset_class: str,
            timeframe: str,
            market_alignment: int,
            **_: object,
        ) -> dict[str, object]:
            return {
                "asset": asset,
                "asset_class": asset_class,
                "strategy": "Breakout",
                "timeframe": timeframe,
                "entry_price": 100.0,
                "stop_loss": 95.0,
                "target_price": 115.0,
                "R_multiple": 3.0,
                "score": 82,
                "components": {"features": {"rsi": 62, "volume_ratio": 1.3, "ema_gap_pct": 0.02}},
            }

        feedback = {
            "model_score": 20,
            "learned_win_rate": 0.0,
            "learned_avg_R": -1.0,
            "sample_size": 8,
            "confidence": "active",
            "approved": False,
            "min_score": 45,
        }
        with patch.object(scanner, "_regime_by_asset_class", return_value={"stock": "bullish"}), patch.object(
            scanner, "fetch_asset_data", return_value=dataset
        ), patch.object(scanner, "LONG_EVALUATORS", (("Breakout", fake_breakout),)), patch.object(scanner, "score_setup", return_value=feedback):
            candidates, _, near_misses, rejection_counts = scanner.scan_market(asset_classes=["stock"])

        self.assertEqual(candidates, [])
        self.assertEqual(rejection_counts["filtered_by_learning_model"], 7)
        self.assertTrue(all(item["model_feedback"]["approved"] is False for item in near_misses))

    def test_scanner_allows_candidate_when_learning_model_errors(self) -> None:
        dataset = {"4h": make_bars(), "1d": make_bars()}

        def fake_breakout(
            bars: list[dict[str, object]],
            asset: str,
            asset_class: str,
            timeframe: str,
            market_alignment: int,
            **_: object,
        ) -> dict[str, object]:
            return {
                "asset": asset,
                "asset_class": asset_class,
                "strategy": "Breakout",
                "timeframe": timeframe,
                "entry_price": 100.0,
                "stop_loss": 95.0,
                "target_price": 115.0,
                "R_multiple": 3.0,
                "score": 82,
                "components": {"features": {"rsi": 62, "volume_ratio": 1.3, "ema_gap_pct": 0.02}},
            }

        with patch.object(scanner, "_regime_by_asset_class", return_value={"stock": "bullish"}), patch.object(
            scanner, "fetch_asset_data", return_value=dataset
        ), patch.object(scanner, "LONG_EVALUATORS", (("Breakout", fake_breakout),)), patch.object(
            scanner, "score_setup", side_effect=KeyError("result_R")
        ):
            candidates, _, _, rejection_counts = scanner.scan_market(asset_classes=["stock"])

        self.assertEqual(len(candidates), 5)
        self.assertEqual(rejection_counts["filtered_by_learning_model"], 0)
        self.assertTrue(all(candidate["model_feedback"]["approved"] for candidate in candidates))
        self.assertTrue(all(candidate["model_feedback"]["confidence"] == "unavailable" for candidate in candidates))

    def test_learning_model_payload_groups_stances(self) -> None:
        rows = [
            {"stance": "favored", "slice": "Breakout", "model_score": 72},
            {"stance": "penalized", "slice": "Trend Pullback", "model_score": 32},
            {"stance": "warming_up", "slice": "RSI <= 60", "model_score": 51},
        ]

        with patch.object(api, "learning_model_rows", return_value=rows), patch.object(
            api,
            "JSONResponse",
            side_effect=lambda payload, **_: types.SimpleNamespace(body=json.dumps(payload, separators=(",", ":")).encode()),
        ):
            response = api.learning_model_payload()

        self.assertEqual(response.body, b'{"favored":[{"stance":"favored","slice":"Breakout","model_score":72}],"penalized":[{"stance":"penalized","slice":"Trend Pullback","model_score":32}],"all":[{"stance":"favored","slice":"Breakout","model_score":72},{"stance":"penalized","slice":"Trend Pullback","model_score":32},{"stance":"warming_up","slice":"RSI <= 60","model_score":51}]}')


if __name__ == "__main__":
    unittest.main()
