from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch, Mock

import test_strategy_adjustments
from test_strategy_adjustments import make_bars
import config, market_bars, scanner, scheduler, portfolio, signals

UTC=timezone.utc


def setup(asset="BTC",asset_class="crypto",end="2026-09-21T00:00:00+00:00"):
    return dict(asset=asset,asset_class=asset_class,strategy="Breakout",timeframe="4h",entry_price=100.,stop_loss=90.,
                target_price=130.,R_multiple=3.,score=90,signal_bar_end=end,components={})


class ScanScheduleTests(unittest.TestCase):
    def test_holiday_is_closed(self):
        now=datetime(2026,11,26,16,tzinfo=UTC)
        self.assertFalse(scheduler.SwingLabScheduler()._is_us_market_open(now))
        self.assertFalse(any(k[0] in ("stock","etf") for k in market_bars.scan_windows(now)))

    def test_after_early_close_daily_and_short_final_bar_are_due(self):
        _,end=market_bars.session_bounds("2026-11-27")
        windows=market_bars.scan_windows(end+timedelta(minutes=2))
        self.assertEqual(windows[("stock","4h")],end)
        self.assertEqual(windows[("stock","1d")],end)
        self.assertFalse(scheduler.SwingLabScheduler()._is_us_market_open(end+timedelta(minutes=1)))

    def test_session_aligned_4h_boundary_and_grace(self):
        op,_=market_bars.session_bounds("2026-09-21")
        end=op+timedelta(hours=4)
        self.assertNotIn(("stock","4h"),market_bars.scan_windows(end+timedelta(seconds=119)))
        self.assertEqual(market_bars.scan_windows(end+timedelta(seconds=120))[("stock","4h")],end)
        self.assertNotIn(("stock","1d"),market_bars.scan_windows(end+timedelta(minutes=2)))
        self.assertNotIn(("stock","4h"),market_bars.scan_windows(end+timedelta(hours=2)))

    def test_crypto_daily_and_4h_at_midnight_and_dst(self):
        now=datetime(2026,3,9,0,2,tzinfo=UTC)
        windows=market_bars.scan_windows(now)
        self.assertEqual(windows[("crypto","4h")],now-timedelta(minutes=2))
        self.assertEqual(windows[("crypto","1d")],now-timedelta(minutes=2))
        op,_=market_bars.session_bounds("2026-03-09")
        self.assertEqual(market_bars.scan_windows(op+timedelta(hours=4,minutes=2))[("stock","4h")].hour,17)

    def test_repeat_candle_and_not_yet_published_candle_are_not_evaluated(self):
        bars=make_bars()
        end=datetime(2026,9,21,tzinfo=UTC)
        bars[-1]["end_timestamp"]=end.isoformat()
        dataset={"4h":bars,"1d":bars}
        evaluator=Mock(return_value=None)
        windows={("crypto","4h"):end}
        progress={(a,"crypto","4h"):end for a in config.CRYPTO_WATCHLIST}
        with patch.object(scanner,"_regime_by_asset_class",return_value={"crypto":"bullish"}),patch.object(scanner,"fetch_asset_data",return_value=dataset),patch.object(scanner,"LONG_EVALUATORS",(("Breakout",evaluator),)):
            result=scanner.scan_market(["crypto"],windows=windows,progress=progress)
            self.assertEqual(result[3]["completed_windows"],[])
            self.assertEqual(evaluator.call_count,0)
            result=scanner.scan_market(["crypto"],windows={("crypto","4h"):end+timedelta(hours=4)})
            self.assertEqual(evaluator.call_count,0)
            self.assertEqual(result[3]["completed_windows"],[])

    def test_rejected_qualifying_setups_can_be_recorded(self):
        dataset={"4h":make_bars(),"1d":make_bars()}
        def evaluator(bars,asset,asset_class,timeframe,*args,**kwargs):return setup(asset,asset_class)
        feedback={"approved":False,"confidence":"active","model_score":20}
        with patch.object(scanner,"_regime_by_asset_class",return_value={"crypto":"bullish"}),patch.object(scanner,"fetch_asset_data",return_value=dataset),patch.object(scanner,"LONG_EVALUATORS",(("Breakout",evaluator),)),patch.object(scanner,"score_setup",return_value=feedback):
            rows,_,_,rejections=scanner.scan_market(["crypto"],include_rejected=True)
        self.assertEqual(len(rows),len(config.CRYPTO_WATCHLIST))
        self.assertTrue(all(not r["model_feedback"]["approved"] for r in rows))
        self.assertEqual(rejections["filtered_by_learning_model"],len(rows))


class AllocationTests(unittest.TestCase):
    def values(self):return dict(equity=10000.,available_cash=10000.,gross_exposure=0.,risk_exposure=0.,groups={},stale_positions=0)

    def test_limit_caps_and_no_cash(self):
        values=self.values(); plan=portfolio.allocation_plan(setup(),values)
        self.assertLessEqual(plan["risk_budget"],100)
        self.assertLessEqual(plan["reserved_cash"],2000)
        values["available_cash"]=0
        self.assertIsNone(portfolio.allocation_plan(setup(),values))

    def test_group_limit_and_portfolio_risk_cap(self):
        values=self.values(); values["groups"]={"crypto":{"positions":1,"gross":100}}
        self.assertIsNone(portfolio.allocation_plan(setup(),values))
        values=self.values();values["risk_exposure"]=500
        self.assertIsNone(portfolio.allocation_plan(setup(),values))
        values=self.values();values["gross_exposure"]=8000
        self.assertIsNone(portfolio.allocation_plan(setup(),values))

    def test_stale_marks_suspend_new_allocations(self):
        values=self.values();values["stale_positions"]=1
        self.assertIsNone(portfolio.allocation_plan(setup(),values))

    def test_signal_identity_includes_candle_but_not_rescan_score(self):
        first=setup()
        self.assertEqual(signals.signal_id(first),signals.signal_id(dict(first,score=76)))
        self.assertNotEqual(signals.signal_id(first),signals.signal_id(dict(first,signal_bar_end="2026-09-21T04:00:00+00:00")))
