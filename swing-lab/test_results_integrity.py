from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch, MagicMock
from contextlib import nullcontext
import signals

# Reuse the existing project's isolated web/database stubs.
import test_strategy_adjustments
import config
import execution
import evaluation
import learning_model
import market_bars
import metrics
import scanner
import trades

UTC = timezone.utc
T0 = datetime(2026, 9, 21, 0, tzinfo=UTC)


def bar(at=T0, op=100, high=104, low=99, close=102, hours=1):
    return dict(timestamp=at.isoformat(), end_timestamp=(at + timedelta(hours=hours)).isoformat(),
                open=op, high=high, low=low, close=close, volume=100)


def position(strategy="Breakout", short=False, pending=False, costs=0):
    settings = execution.execution_settings(T0, strategy, pending_entry=pending)
    settings.update(fee_bps=costs, slippage_bps=costs)
    return dict(id=1, asset="BTC", asset_class="crypto", strategy=strategy, timeframe="4h",
                entry_price=100., stop_loss=110. if short else 90., target_price=70. if short else 130.,
                current_price=100., R_multiple=3., score=80, date_opened=T0.isoformat(),
                date_closed=None, status="open", result_R=None,
                effective_stop_loss=110. if short else 90., partial_taken=False, partial_result_R=0.,
                runner_activated=False, metadata_json=None,
                metadata={"strategy_version":config.STRATEGY_VERSION, "execution":settings})


def history(result=1., opened=T0, closed=None, version=None):
    metadata = {"strategy_version": version or config.STRATEGY_VERSION,
                "execution": {"fee_bps":config.SIM_FEE_BPS,"slippage_bps":config.SIM_SLIPPAGE_BPS},
                "features":{"rsi":62, "volume_ratio":1.3,"ema_gap_pct":0.02}}
    return dict(id=1, asset="BTC", asset_class="crypto", strategy="Breakout", timeframe="4h",
                date_opened=opened.isoformat(), date_closed=(closed or opened+timedelta(hours=1)).isoformat(),
                result_R=result, metadata_json=json.dumps(metadata))


class AccountingTests(unittest.TestCase):
    def test_partial_is_not_halved_twice(self):
        trade=position("Trend Pullback")
        trade.pop("metadata")
        trade.update(partial_taken=True, partial_result_R=.5, current_price=100.)
        self.assertEqual(trades.compute_notional_pnl_usd(trade),5.)

    def test_closed_dollars_use_recorded_r_not_overshoot(self):
        trade=position()
        trade.update(status="target_hit", result_R=3., current_price=140.)
        self.assertEqual(trades.compute_notional_pnl_usd(trade),30.)
        trade.update(status="stopped",result_R=0.,current_price=99.)
        self.assertEqual(trades.compute_notional_pnl_usd(trade),0.)

    def test_all_empty_date_breakdowns_stay_empty(self):
        with patch.object(metrics,"list_trades", side_effect=AssertionError("History leaked")):
            for func in (metrics.analytics_by_strategy, metrics.analytics_by_asset_class,
                         metrics.analytics_by_direction, metrics.analytics_by_setup_slice):
                self.assertEqual(func([]),[])

    def test_empty_range_payload_is_consistent(self):
        trade=position(); trade.update(status="stopped",result_R=-1.)
        with patch.object(metrics,"list_trades",return_value=[trade]):
            result=metrics.analytics_payload(start_date="2027-01-01")
        self.assertEqual(result["summary"]["total_trades"],0)
        self.assertEqual(result["setup_slice_stats"],[])

    def test_cancelled_and_unknown_outcomes_not_counted_as_losses(self):
        trade=position(); trade.update(status="cancelled")
        unknown=dict(trade,status="unknown")
        result=metrics._summary_from_trades([trade,unknown])
        self.assertEqual(result["closed_trades"],0)
        self.assertEqual(result["cancelled_trades"],1)
        self.assertEqual(result["unresolved_closed_trades"],1)

    def test_version_summary_excludes_legacy(self):
        current=position(); current.update(status="stopped",result_R=-1.)
        legacy=deepcopy(current); legacy["metadata"]={}; legacy["result_R"]=10
        with patch.object(metrics,"list_trades",return_value=[current,legacy]):
            self.assertEqual(metrics.analytics_since_strategy_change()["summary"]["total_R"],-1.)


class CandleTests(unittest.TestCase):
    def test_crypto_unfinished_daily_and_hourly_excluded(self):
        now=T0+timedelta(hours=2,minutes=30)
        raw=[bar(T0+timedelta(hours=h)) for h in range(4)]
        self.assertEqual(len(market_bars.completed_bars(raw,60,"crypto",now)),2)
        self.assertEqual(market_bars.completed_bars([bar()],1440,"crypto",now),[])

    def test_crypto_buckets_do_not_shift_with_window(self):
        raw=[bar(T0+timedelta(hours=h)) for h in range(12)]
        all_bars=market_bars.aggregate_bars(raw)
        shifted=market_bars.aggregate_bars(raw[1:])
        self.assertEqual(shifted,all_bars[1:])
        missing=market_bars.aggregate_bars(raw[:5]+raw[6:])
        self.assertEqual([b["timestamp"] for b in missing],[raw[0]["timestamp"],raw[8]["timestamp"]])

    def test_equity_sessions_do_not_cross_days_and_keep_final_short_bar(self):
        raw=[]
        for day in ("2026-09-21","2026-09-22"):
            op,end=market_bars.session_bounds(day)
            for i in range(7):
                b=bar(op+timedelta(hours=i))
                b["end_timestamp"]=min(op+timedelta(hours=i+1),end).isoformat()
                raw.append(b)
        agg=market_bars.aggregate_bars(raw,asset_class="stock")
        self.assertEqual(len(agg),4)
        self.assertEqual(agg[1]["end_timestamp"],market_bars.session_bounds("2026-09-21")[1].isoformat())
        self.assertEqual(agg[2]["timestamp"],market_bars.session_bounds("2026-09-22")[0].isoformat())

    def test_early_close_holiday_and_dst(self):
        self.assertIsNone(market_bars.session_bounds("2026-11-26"))
        op,end=market_bars.session_bounds("2026-11-27")
        self.assertEqual(end-op,timedelta(hours=3,minutes=30))
        self.assertEqual(market_bars.session_bounds("2026-03-06")[0].hour,14)
        self.assertEqual(market_bars.session_bounds("2026-03-09")[0].hour,13)
        raw=[bar(op+timedelta(hours=i)) for i in range(4)]
        complete=market_bars.completed_bars(raw,60,"stock",end)
        self.assertEqual(len(market_bars.aggregate_bars(complete,asset_class="stock")),1)
        self.assertEqual(len(market_bars.completed_bars([bar(op)],1440,"stock",end-timedelta(minutes=1))),0)
        self.assertEqual(len(market_bars.completed_bars([bar(op)],1440,"stock",end)),1)

    def test_old_cache_format_is_invalidated(self):
        row={"payload_json":json.dumps({"4h":[],"1d":[]}),"fetched_at":datetime.now(UTC)}
        with patch.object(scanner,"fetch_one",return_value=row):
            self.assertIsNone(scanner._cached_market_data("BTC","crypto"))
            self.assertIsNone(scanner._most_recent_cached_market_data("BTC","crypto"))

    def test_failed_fetch_does_not_offer_stale_signals(self):
        cached={"4h":[bar()],"1d":[bar()],"execution":[bar()]}
        with patch.object(scanner,"_cached_market_data",return_value=None), patch.object(scanner,"_fetch_kraken_chart",side_effect=scanner.requests.RequestException()), patch.object(scanner,"_most_recent_cached_market_data",return_value=cached):
            result=scanner.fetch_asset_data("BTC","crypto")
        self.assertEqual(result,{"4h":[],"1d":[],"execution":[bar()]})


class ExecutionTests(unittest.TestCase):
    def replay(self,trade,bars,now=None):
        return execution.replay_bars(trade,bars,now or T0+timedelta(days=20))

    def test_intrabar_stop_even_if_close_recovers(self):
        result=self.replay(position(),[bar(low=89,close=102)])
        self.assertEqual((result["status"],result["current_price"],result["result_R"]),("stopped",90,-1))

    def test_both_levels_uses_existing_stop_first(self):
        result=self.replay(position(),[bar(high=135,low=85)])
        self.assertEqual(result["result_R"],-1)
        self.assertFalse(result["runner_activated"])

    def test_breakout_runner_then_breakeven_next_bar(self):
        result=self.replay(position(),[bar(op=105,high=112,low=104,close=111)])
        self.assertEqual(result["status"],"open")
        self.assertTrue(result["runner_activated"])
        result=self.replay(result,[bar(T0+timedelta(hours=1),op=103,high=105,low=99,close=103)])
        self.assertEqual((result["status"],result["result_R"]),("stopped",0))

    def test_new_breakeven_tie_is_conservative(self):
        result=self.replay(position(),[bar(high=135,low=99)])
        self.assertEqual(result["result_R"],0)

    def test_gap_through_stop_is_not_capped_at_minus_one(self):
        result=self.replay(position(),[bar(op=80,high=84,low=79,close=83)])
        self.assertEqual(result["result_R"],-2)
        self.assertEqual(result["current_price"],80)
        self.assertEqual(trades.compute_notional_pnl_usd(result),-20.)

    def test_gap_target_fills_limit_before_later_low(self):
        result=self.replay(position(),[bar(op=140,high=141,low=80,close=90)])
        self.assertEqual((result["status"],result["result_R"]),("target_hit",3))

    def test_short_stop_and_gap(self):
        result=self.replay(position("Breakdown",short=True),[bar(op=120,high=125,low=98)])
        self.assertEqual(result["result_R"],-2)
        result=self.replay(position("Breakdown",short=True),[bar(op=97,high=98,low=68,close=70)])
        self.assertEqual(result["result_R"],2.)  # half at +1R and half at +3R
        result=self.replay(position("Breakdown",short=True),[bar(op=97,high=101,low=68,close=70)])
        self.assertEqual(result["result_R"],.5)

    def test_partial_and_target_profit_agree_with_dollars(self):
        trade=position("Trend Pullback"); trade["target_price"]=120
        result=self.replay(trade,[bar(op=112,high=125,low=111,close=123)])
        self.assertEqual(result["result_R"],1.5)
        self.assertEqual(result["partial_price"],110)
        self.assertEqual(trades.compute_notional_pnl_usd(result),15)

    def test_costs_reduce_r_and_dollars_together(self):
        result=self.replay(position(costs=10),[bar(op=80,high=85,low=75)])
        self.assertAlmostEqual(result["current_price"],79.92)
        self.assertAlmostEqual(result["result_R"],-2.015992)
        self.assertEqual(trades.compute_notional_pnl_usd(result),-20.16)

    def test_pending_entry_uses_next_bar_open_and_entry_cost(self):
        trade=position(pending=True,costs=10)
        trade["metadata"]["execution"]["cursor"]=(T0+timedelta(minutes=20)).isoformat()
        result=self.replay(trade,[bar(),bar(T0+timedelta(hours=1),op=102,high=104,low=101)])
        self.assertAlmostEqual(result["entry_price"],102.102)
        self.assertFalse(result["metadata"]["execution"]["pending_entry"])
        self.assertGreater(result["metadata"]["execution"]["entry_fee_r"],0)
        self.assertEqual(result["metadata"]["execution"]["events"][0]["at"],(T0+timedelta(hours=1)).isoformat())

    def test_gapped_entry_near_target_cannot_take_partial_beyond_target(self):
        trade=position("Trend Pullback",pending=True)
        result=self.replay(trade,[bar(op=125,high=140,low=124,close=135)])
        self.assertEqual(result["status"],"target_hit")
        self.assertFalse(result["partial_taken"])
        self.assertAlmostEqual(result["result_R"],5/35)

    def test_gap_beyond_target_cancels_unfilled_entry(self):
        result=self.replay(position(pending=True),[bar(op=140)])
        self.assertEqual(result["status"],"cancelled")
        self.assertIsNone(result["result_R"])
        self.assertIsNone(trades.compute_notional_pnl_usd(result))

    def test_replay_is_chronological_and_idempotent(self):
        bars=[bar(T0+timedelta(hours=1),low=80),bar()]
        result=self.replay(position(),bars)
        again=self.replay(result,bars)
        self.assertEqual(again,result)
        self.assertEqual(result["result_R"],-1)
        open_result=self.replay(position(),[bar()])
        self.assertEqual(self.replay(open_result,[bar()]),open_result)

    def test_pre_entry_and_unfinished_bars_are_ignored(self):
        trade=position(); trade["metadata"]["execution"]["cursor"]=(T0+timedelta(minutes=20)).isoformat()
        self.assertEqual(self.replay(trade,[bar(low=80)],T0+timedelta(hours=2)),trade)
        self.assertEqual(self.replay(position(),[bar(low=80)],T0+timedelta(minutes=20)),position())

    def test_strategy_specific_expiry_and_stale_bars(self):
        at=T0+timedelta(days=10)
        bars=[bar(at,op=102,high=104,low=101,close=103)]
        breakout=position(); breakout["metadata"]["execution"]["cursor"]=at.isoformat()
        trend=position("Trend Pullback"); trend["metadata"]["execution"]["cursor"]=at.isoformat()
        self.assertEqual(self.replay(breakout,bars)["status"],"open")
        self.assertEqual(self.replay(trend,bars)["status"],"closed")
        breakout["metadata"]["execution"]["cursor"]=(T0+timedelta(days=15)).isoformat()
        self.assertEqual(self.replay(breakout,[bar(T0+timedelta(days=15),op=102)])["status"],"closed")
        # Wall-clock age alone never creates an exit using a stale historical price.
        self.assertEqual(self.replay(position(),[bar()])["status"],"open")

    def test_gap_in_execution_history_is_flagged_without_fabricating_exit(self):
        result=self.replay(position(),[bar(T0+timedelta(hours=2),low=80)])
        self.assertEqual(result["status"],"open")
        self.assertIn("data_gap",result["metadata"]["execution"])
        recovered=self.replay(result,[bar(),bar(T0+timedelta(hours=1)),bar(T0+timedelta(hours=2),low=80)])
        self.assertEqual(recovered["status"],"stopped")
        self.assertNotIn("data_gap",recovered["metadata"]["execution"])

    def test_legacy_adoption_preserves_state_and_does_not_replay_old_bars(self):
        trade=position(); trade["metadata"]={}
        trade.update(partial_taken=True,partial_result_R=.5,effective_stop_loss=100)
        result=self.replay(trade,[bar(low=80)])
        self.assertEqual(result["status"],"open")
        self.assertEqual(result["partial_result_R"],.5)
        self.assertIn("migration_note",result["metadata"]["execution"])

    def test_updater_persists_state_with_ledger_and_outbox(self):
        trade=position()
        connection=MagicMock()
        connection.execute.return_value.fetchone.return_value={"id":1}
        with patch.object(trades,"list_trades",return_value=[trade]), patch.object(trades,"fetch_asset_data",return_value={"execution":[bar(low=80)]}), patch.object(trades,"_now_utc",return_value=T0+timedelta(hours=2)), patch.object(trades,"get_db",side_effect=lambda:nullcontext(connection)), patch.object(trades.portfolio,"lock_account"), patch.object(trades.portfolio,"apply_events") as ledger, patch.object(trades.portfolio,"snapshot"), patch.object(trades,"notify_trade_closed") as notify:
            result=trades.update_open_trades()
        self.assertEqual(len(result),1)
        self.assertEqual(connection.execute.call_count,1)
        ledger.assert_called_once()
        self.assertIs(notify.call_args.kwargs["connection"],connection)

    def test_signal_initial_state_freezes_costs_and_version(self):
        state=signals.initial_state(dict(position(),signal_bar_end=T0.isoformat()),T0)
        self.assertEqual(state["metadata"]["strategy_version"],config.STRATEGY_VERSION)
        self.assertTrue(state["metadata"]["execution"]["pending_entry"])
        self.assertEqual(state["metadata"]["execution"]["fee_bps"],config.SIM_FEE_BPS)

    def test_equity_replay_crosses_weekend_without_false_gap(self):
        trade=position(); trade["asset_class"]="stock"
        op,end=market_bars.session_bounds("2026-09-18")
        trade["metadata"]["execution"]["cursor"]=end.isoformat()
        monday,_=market_bars.session_bounds("2026-09-21")
        result=self.replay(trade,[bar(monday)])
        self.assertNotIn("data_gap",result["metadata"]["execution"])
        self.assertEqual(result["metadata"]["execution"]["cursor"],(monday+timedelta(hours=1)).isoformat())

    def test_failed_compare_and_swap_does_not_notify(self):
        connection=MagicMock()
        connection.execute.return_value.fetchone.return_value=None
        with patch.object(trades,"list_trades",return_value=[position()]), patch.object(trades,"fetch_asset_data",return_value={"execution":[bar(low=80)]}), patch.object(trades,"_now_utc",return_value=T0+timedelta(hours=2)), patch.object(trades,"get_db",side_effect=lambda:nullcontext(connection)), patch.object(trades.portfolio,"lock_account"), patch.object(trades.portfolio,"snapshot"), patch.object(trades,"notify_trade_closed") as notify:
            self.assertEqual(trades.update_open_trades(),[])
        notify.assert_not_called()


class LearningTests(unittest.TestCase):
    def test_positive_expectancy_low_win_rate_scores_above_neutral(self):
        stats=learning_model.SliceStats(16,5,.25)
        self.assertGreater(learning_model._score_from_slice(stats),50)

    def test_win_rate_does_not_change_score_at_same_expectancy(self):
        self.assertEqual(learning_model._score_from_slice(learning_model.SliceStats(20,5,.25)),
                         learning_model._score_from_slice(learning_model.SliceStats(20,15,.25)))

    def test_legacy_and_different_costs_are_excluded(self):
        old=history(version="old")
        different=history(); metadata=json.loads(different["metadata_json"]); metadata["execution"]["fee_bps"]=999; different["metadata_json"]=json.dumps(metadata)
        stats=learning_model.build_stats([history(),old,different])
        self.assertEqual(stats[("all",)].trades,1)

    def test_overlapping_slices_do_not_multiply_confidence(self):
        rows=[history(result=1) for _ in range(8)]
        stats=learning_model.build_stats(rows)
        feedback=learning_model.score_setup(dict(rows[0],components=json.loads(rows[0]["metadata_json"])),stats)
        self.assertEqual(feedback["sample_size"],8)
        self.assertEqual(feedback["model_score"],learning_model._score_from_slice(learning_model.SliceStats(8,8,1)))

    def test_warmup_does_not_penalize_candidates(self):
        rows=[history(result=-1) for _ in range(7)]
        feedback=learning_model.score_setup(dict(rows[0],components={}),learning_model.build_stats(rows))
        self.assertEqual(feedback["model_score"],50)
        self.assertEqual(feedback["confidence"],"warming_up")

    def test_walk_forward_never_uses_future_or_overlapping_outcomes(self):
        rows=[history(-1,opened=T0-timedelta(days=2),closed=T0+timedelta(days=2)),
              history(3,opened=T0,closed=T0+timedelta(days=1)),
              history(-1,opened=T0+timedelta(days=3),closed=T0+timedelta(days=4))]
        result=evaluation.walk_forward_evaluation(rows)
        self.assertEqual([r["training_trades"] for r in result["decisions"]],[0,0,2])
        self.assertEqual(result["observed_baseline"]["total_R"],1)
        self.assertIn("not an unbiased",result["limitation"])

    def test_walk_forward_negative_history_rejects_later_candidate(self):
        rows=[history(-1,opened=T0+timedelta(days=i),closed=T0+timedelta(days=i,hours=1)) for i in range(17)]
        result=evaluation.walk_forward_evaluation(rows)
        self.assertEqual(result["overlay_accepted"]["closed_trades"],16)
        self.assertEqual(result["overlay_rejected"]["closed_trades"],1)
        self.assertEqual(result["decisions"][-1]["training_trades"],16)

    def test_failed_history_read_not_cached_as_empty(self):
        learning_model.clear_learning_cache()
        with patch.object(learning_model,"fetch_all",side_effect=[RuntimeError("offline"),[history()]]):
            with self.assertRaises(RuntimeError): learning_model.learned_stats()
            self.assertEqual(learning_model.learned_stats()[("all",)].trades,1)
        learning_model.clear_learning_cache()


if __name__ == "__main__":
    unittest.main()
