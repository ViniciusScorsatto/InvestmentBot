"""Signal quality, statistical safeguards, and counterfactual execution regressions."""
import json
import unittest
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch, Mock

import test_strategy_adjustments
from test_results_integrity import T0, bar, position, history
from test_portfolio_signals import setup
import config, earnings, experiments, execution, learning_model, market_bars, signals, strategies


class EntryQualityTests(unittest.TestCase):
    def test_tiny_stop_rejected_despite_high_net_reward_risk(self):
        trade = position(pending=True, costs=5)
        trade['stop_loss'] = trade['effective_stop_loss'] = 99.99
        result = execution.replay_bars(trade, [bar()], T0+timedelta(hours=1))
        settings = result['metadata']['execution']
        self.assertEqual(result['status'], 'cancelled')
        self.assertGreater(settings['entry_net_r'], 1.8)
        self.assertGreater(settings['entry_cost_r'], 2)
        self.assertEqual(settings['cancel_reason'], 'entry_cost_r_above_maximum')
        self.assertIsNone(result['result_R'])
        self.assertEqual([e['kind'] for e in settings['events']], ['entry_cancelled'])

    def test_cost_cap_is_frozen_and_legacy_contract_is_not_retrofitted(self):
        trade = position(pending=True, costs=5)
        trade['stop_loss'] = trade['effective_stop_loss'] = 99.99
        trade['metadata']['execution'].pop('max_entry_cost_r')
        trade['metadata']['execution']['version'] = 3
        result = execution.replay_bars(trade, [bar()], T0+timedelta(hours=1))
        self.assertEqual(result['status'], 'stopped')
        self.assertLess(result['result_R'], -3)
        trade = position(pending=True, costs=5)
        trade['metadata']['execution']['max_entry_cost_r'] = .001
        with patch.object(execution, 'MAX_ENTRY_COST_R', 1):
            result = execution.replay_bars(trade, [bar()], T0+timedelta(hours=1))
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['metadata']['execution']['max_entry_cost_r'], .001)

    def test_cost_guard_short_symmetry_zero_cost_and_exact_boundary(self):
        for short in (False, True):
            trade = position('Breakdown' if short else 'Breakout', short=short, pending=True, costs=5)
            trade['stop_loss'] = trade['effective_stop_loss'] = 100.01 if short else 99.99
            result = execution.replay_bars(trade, [bar()], T0+timedelta(hours=1))
            self.assertEqual(result['metadata']['execution']['cancel_reason'], 'entry_cost_r_above_maximum')
        trade = position(pending=True, costs=0)
        result = execution.replay_bars(trade, [bar()], T0+timedelta(hours=1))
        self.assertEqual(result['metadata']['execution']['entry_cost_r'], 0)
        self.assertEqual(result['status'], 'open')
        trade = position(pending=True, costs=5)
        first = execution.replay_bars(trade, [bar()], T0+timedelta(hours=1))
        trade['metadata']['execution']['max_entry_cost_r'] = first['metadata']['execution']['entry_cost_r']
        self.assertEqual(execution.replay_bars(trade, [bar()], T0+timedelta(hours=1))['status'], 'open')

    def test_gap_with_positive_payoff_still_cancels_below_floor(self):
        result = execution.replay_bars(position(pending=True), [bar(op=120, high=125, low=119)], T0+timedelta(hours=1))
        self.assertEqual(result["status"], "cancelled")
        self.assertIsNone(result["result_R"])
        self.assertEqual(result["metadata"]["execution"]["cancel_reason"], "entry_net_r_below_minimum")
        self.assertEqual([e["kind"] for e in result["metadata"]["execution"]["events"]], ["entry_cancelled"])

    def test_net_costs_can_reject_gross_two_r(self):
        trade = position(pending=True, costs=50)
        trade["target_price"] = 120
        result = execution.replay_bars(trade, [bar()], T0+timedelta(hours=1))
        self.assertEqual(result["status"], "cancelled")
        self.assertLess(result["metadata"]["execution"]["entry_net_r"], 1.8)

    def test_ordinary_two_r_with_default_costs_is_eligible(self):
        trade = position("Trend Pullback", pending=True, costs=5)
        trade["target_price"] = 120
        result = execution.replay_bars(trade, [bar()], T0+timedelta(hours=1))
        self.assertEqual(result["status"], "open")
        self.assertGreater(result["metadata"]["execution"]["entry_net_r"], 1.8)

    def test_legacy_pending_contract_keeps_old_entry_semantics(self):
        trade=position(pending=True,costs=50)
        trade["target_price"]=100.6
        trade["metadata"]["execution"].pop("min_entry_net_r")
        result=execution.replay_bars(trade,[bar(op=100,high=101,low=100.6,close=101)],T0+timedelta(hours=1))
        self.assertEqual(result["status"],"target_hit")
        self.assertLess(result["result_R"],0)

    def test_short_gap_checks_symmetric_payoff_and_frozen_floor(self):
        trade = position("Breakdown", short=True, pending=True)
        result = execution.replay_bars(trade, [bar(op=80, high=81, low=79)], T0+timedelta(hours=1))
        self.assertEqual(result["status"], "cancelled")
        trade["metadata"]["execution"]["min_entry_net_r"] = .2
        result = execution.replay_bars(trade, [bar(op=80, high=81, low=79)], T0+timedelta(hours=1))
        self.assertNotEqual(result["status"], "cancelled")


class VolumeTests(unittest.TestCase):
    def equity_bars(self, last_day="2026-09-22"):
        rows = []
        day = T0.date() - timedelta(days=65)
        while day.isoformat() <= last_day:
            bounds = market_bars.session_bounds(day.isoformat())
            if bounds:
                op, end = bounds
                midpoint = min(op+timedelta(hours=4), end)
                rows.append(dict(bar(op), end_timestamp=midpoint.isoformat(), volume=400))
                if midpoint < end:
                    rows.append(dict(bar(midpoint), end_timestamp=end.isoformat(), volume=100))
            day += timedelta(days=1)
        return rows

    def test_equity_short_segment_is_compared_only_to_same_segment(self):
        rows = self.equity_bars()
        rows[-1]["volume"] = 150
        self.assertEqual(strategies.volume_baseline(rows, "stock", "4h"), 100)
        self.assertEqual(strategies.volume_baseline(rows[:-1], "stock", "4h"), 400)
        rows[-1]["volume"] = 100000
        self.assertEqual(strategies.volume_baseline(rows, "stock", "4h"), 100)

    def test_early_close_does_not_compare_to_full_four_hour_bars(self):
        rows = self.equity_bars("2026-11-25")
        op, end = market_bars.session_bounds("2026-11-27")
        rows.append(dict(bar(op), end_timestamp=end.isoformat()))
        self.assertIsNone(strategies.volume_baseline(rows, "stock", "4h"))

    def test_crypto_prior_twenty_excludes_signal_and_bad_volumes(self):
        rows = [bar(T0+timedelta(hours=i*4)) for i in range(21)]
        rows[-1]["volume"] = 100000
        self.assertEqual(strategies.volume_baseline(rows, "crypto", "4h"), 100)
        rows[-1]["volume"] = float("inf")
        self.assertIsNone(strategies.volume_baseline(rows, "crypto", "4h"))
        rows[-1]["volume"] = 100
        rows[0]["volume"] = float("nan")
        self.assertIsNone(strategies.volume_baseline(rows, "crypto", "4h"))

    def test_missing_session_times_reject_instead_of_mixing_bars(self):
        rows = self.equity_bars()
        rows[-1].pop("end_timestamp")
        self.assertIsNone(strategies.volume_baseline(rows, "stock", "4h"))

    def test_normalized_volume_drives_real_breakout_signal(self):
        rows = self.equity_bars()
        rows[-1].update(close=110, high=111, low=100, volume=150)
        trade = strategies.evaluate_breakout(rows, "AAPL", "stock", "4h", 20)
        self.assertIsNotNone(trade)
        self.assertEqual(trade["components"]["features"]["volume_ratio"], 1.5)


class EvidenceTests(unittest.TestCase):
    def feedback(self, values, weeks=None):
        weeks = weeks or [i//8 for i in range(len(values))]
        rows = [history(v, opened=T0+timedelta(weeks=w), closed=T0+timedelta(weeks=w,hours=1)) for v,w in zip(values,weeks)]
        return learning_model.score_setup(dict(rows[0],components={}), learning_model.build_stats(rows))

    def test_many_same_week_losses_do_not_block_or_rank(self):
        result = self.feedback([-1]*80, [0]*80)
        self.assertTrue(result["approved"])
        self.assertEqual(result["model_score"],50)
        self.assertEqual(result["weeks"],1)

    def test_consistently_negative_separate_periods_can_block(self):
        result = self.feedback([-1]*64)
        self.assertFalse(result["approved"])
        self.assertTrue(result["stable_negative"])
        self.assertEqual(len(result["blocking_slices"]),1)
        self.assertLess(result["mean_r_interval"][1],0)

    def test_uncertain_mixed_history_stays_neutral(self):
        result = self.feedback([-1,1]*40)
        self.assertTrue(result["approved"])
        self.assertEqual(result["model_score"],50)
        self.assertEqual(result["confidence"],"uncertain")

    def test_regime_reversal_cannot_block_on_old_losses(self):
        result = self.feedback([-2]*32+[1]*32)
        self.assertTrue(result["approved"])
        self.assertFalse(result["stable_negative"])

    def test_descriptive_bucket_cannot_veto_positive_primary(self):
        stats = {("setup_slice","crypto","Breakout","4h"): learning_model.SliceStats(80,60,1,10,.7,1.3),
                 ("volume_bucket","Breakout","> 1.5"): learning_model.SliceStats(80,0,-1,10,-1.3,-.7,True)}
        result=learning_model.score_setup(dict(setup(),components={"features":{"volume_ratio":2}}),stats)
        self.assertTrue(result["approved"])
        self.assertGreater(result["model_score"],50)

    def test_walk_forward_large_history_only_blocks_after_evidence(self):
        rows=[history(-1,opened=T0+timedelta(days=i),closed=T0+timedelta(days=i,hours=1)) for i in range(71)]
        from evaluation import walk_forward_evaluation
        decisions=walk_forward_evaluation(rows)["decisions"]
        self.assertTrue(all(r["approved"] for r in decisions[:60]))
        self.assertFalse(decisions[-1]["approved"])


class EarningsTests(unittest.TestCase):
    def calendar(self, event="2026-09-22", fetched=T0):
        return {"source":"test", "fetched_at":fetched.isoformat(), "events":{"AAPL":[event]}}

    def test_blackout_includes_day_and_two_prior_calendar_days(self):
        for event in ("2026-09-20","2026-09-21","2026-09-22"):
            self.assertEqual(earnings.earnings_snapshot("AAPL","stock",T0,self.calendar(event))["status"],"blocked")
        self.assertEqual(earnings.earnings_snapshot("AAPL","stock",T0,self.calendar("2026-09-23"))["status"],"clear")

    def test_stale_future_missing_or_bad_coverage_is_never_clear(self):
        for calendar in ({}, self.calendar(fetched=T0-timedelta(days=2)), self.calendar(fetched=T0+timedelta(seconds=1)),
                         self.calendar(event="bad"),self.calendar(event="2026-01-01")):
            self.assertEqual(earnings.earnings_snapshot("AAPL","stock",T0,calendar)["status"],"unknown")
        self.assertEqual(earnings.earnings_snapshot("MSFT","stock",T0,self.calendar())["status"],"unknown")
        self.assertEqual(earnings.earnings_snapshot("SPY","etf",T0,{})["status"],"not_applicable")

    def test_provider_error_body_not_a_calendar(self):
        with self.assertRaises(ValueError): earnings.parse_calendar_csv('{"Note":"Rate limit"}',T0)
        parsed=earnings.parse_calendar_csv("symbol,reportDate\nAAPL,2026-09-22\n",T0)
        self.assertEqual(parsed["events"]["AAPL"],["2026-09-22"])

    def test_rate_limit_backoff_and_stale_last_good_not_used_as_clear(self):
        response=Mock(text='{ "Information": "rate limited" }')
        with patch.object(earnings,"EARNINGS_API_KEY","test"),patch.object(earnings,"EARNINGS_CALENDAR_PATH",""),patch.object(earnings,"_cache",self.calendar(fetched=T0-timedelta(days=2))),patch.object(earnings,"_retry_at",None),patch.object(earnings.requests,"get",return_value=response) as get:
            snapshot=earnings.load_calendar(T0)
            earnings.load_calendar(T0+timedelta(minutes=1))
        self.assertEqual(get.call_count,1)
        self.assertEqual(earnings.earnings_snapshot("AAPL","stock",T0,snapshot)["status"],"unknown")


class ExperimentTests(unittest.TestCase):
    def variants(self, asset_class="crypto", earnings_state=None):
        baseline=signals.initial_state(dict(setup(asset_class=asset_class),components={"features":{"atr":4},"earnings":earnings_state or {}}),T0)
        return {name:state for name,_,state,_ in experiments.variant_states(baseline)}

    def test_atr_includes_gap_and_buffer_preserves_target(self):
        bars=[bar(op=100,high=102,low=98,close=100) for _ in range(15)]
        bars[-1].update(high=112,low=108,close=110)
        self.assertAlmostEqual(strategies.atr(bars), (13*4+12)/14)
        variants=self.variants()
        self.assertEqual(variants["atr_stop"]["stop_loss"],88)
        self.assertEqual(variants["atr_stop"]["target_price"],130)
        self.assertEqual(variants["baseline"]["stop_loss"],90)
        self.assertEqual(variants["atr_trailing"]["stop_loss"],90)

    def test_delayed_breakeven_survives_baseline_stop(self):
        base=position()
        delayed=deepcopy(base); delayed["metadata"]["execution"]["breakeven_at_r"]=1.5
        bars=[bar(op=105,high=112,low=104,close=111),bar(T0+timedelta(hours=1),op=104,high=108,low=99,close=104)]
        self.assertEqual(execution.replay_bars(base,bars,T0+timedelta(hours=2))["status"],"stopped")
        result=execution.replay_bars(delayed,bars,T0+timedelta(hours=2))
        self.assertEqual(result["status"],"open")
        self.assertEqual(result["effective_stop_loss"],90)
        result=execution.replay_bars(result,[bar(T0+timedelta(hours=2),op=110,high=116,low=109)],T0+timedelta(hours=3))
        self.assertEqual(result["effective_stop_loss"],100)

    def test_delayed_exit_keeps_partial_at_one_r(self):
        delayed=position("Trend Pullback"); delayed["metadata"]["execution"]["breakeven_at_r"]=1.5
        result=execution.replay_bars(delayed,[bar(op=105,high=112,low=99,close=110)],T0+timedelta(hours=1))
        self.assertTrue(result["partial_taken"])
        self.assertEqual(result["status"],"open")
        self.assertEqual(result["effective_stop_loss"],90)

    def test_trail_applies_to_next_bar_not_bar_used_to_compute_it(self):
        trade=position();trade["metadata"]["execution"]["trailing_atr"]=4
        result=execution.replay_bars(trade,[bar(op=105,high=115,low=102,close=114)],T0+timedelta(hours=1))
        self.assertEqual(result["status"],"open")
        self.assertEqual(result["effective_stop_loss"],110)
        result=execution.replay_bars(result,[bar(T0+timedelta(hours=1),op=108,high=114,low=107,close=112)],T0+timedelta(hours=2))
        self.assertEqual(result["current_price"],108)
        self.assertEqual(result["status"],"stopped")

    def test_stop_then_target_ignores_same_bar_and_tracks_later(self):
        state,status=experiments.advance_state(position(),[bar(high=140,low=80)],T0+timedelta(hours=1))
        self.assertEqual(status,"monitoring")
        self.assertFalse(state["stop_followup"]["target_revisited"])
        state,status=experiments.advance_state(state,[bar(T0+timedelta(hours=1),high=140,low=90)],T0+timedelta(hours=2))
        self.assertEqual(status,"stopped")
        self.assertTrue(state["stop_followup"]["target_revisited"])
        self.assertEqual(state["result_R"],-1)
        self.assertEqual(experiments.advance_state(state,[],T0+timedelta(days=20))[0],state)

    def test_followup_data_gaps_remain_pending_and_recover(self):
        state,_=experiments.advance_state(position(),[bar(low=80)],T0+timedelta(hours=1))
        result,status=experiments.advance_state(state,[bar(T0+timedelta(hours=2),high=140)],T0+timedelta(hours=3))
        self.assertEqual(status,"monitoring")
        self.assertTrue(result["stop_followup"]["data_gap"])
        result,status=experiments.advance_state(result,[bar(T0+timedelta(hours=1)),bar(T0+timedelta(hours=2),high=140)],T0+timedelta(hours=3))
        self.assertTrue(result["stop_followup"]["target_revisited"])
        self.assertNotIn("data_gap",result["stop_followup"])

    def test_next_open_rechecks_blackout_using_frozen_calendar(self):
        snapshot=earnings.earnings_snapshot("AAPL","stock",T0,{
            "source":"test","fetched_at":T0.isoformat(),"events":{"AAPL":["2026-09-23"]}})
        self.assertEqual(snapshot["status"],"clear")  # Sunday evening New York.
        variant=self.variants("stock",snapshot)["earnings_blackout"]
        monday,_=market_bars.session_bounds("2026-09-21")
        result=execution.replay_bars(variant,[bar(monday)],monday+timedelta(hours=1))
        self.assertEqual(result["status"],"skipped")
        self.assertIsNone(result["result_R"])
        self.assertEqual(result["metadata"]["execution"]["events"],[])

    def test_stale_calendar_at_entry_is_unavailable_not_zero_return(self):
        snapshot={"status":"clear","source":"test","fetched_at":(T0-timedelta(days=1)).isoformat(),
                  "next_report_date":"2026-10-01"}
        variant=self.variants("stock",snapshot)["earnings_blackout"]
        monday,_=market_bars.session_bounds("2026-09-21")
        result=execution.replay_bars(variant,[bar(monday)],monday+timedelta(hours=1))
        self.assertEqual(result["status"],"unavailable")
        self.assertFalse(experiments.resolved(result))

    def test_short_atr_trail_tightens_without_lookahead(self):
        trade=position("Breakdown",short=True);trade["metadata"]["execution"]["trailing_atr"]=4
        result=execution.replay_bars(trade,[bar(op=95,high=98,low=85,close=86)],T0+timedelta(hours=1))
        self.assertEqual(result["status"],"open")
        self.assertEqual(result["effective_stop_loss"],90)
        self.assertTrue(result["partial_taken"])
        result=execution.replay_bars(result,[bar(T0+timedelta(hours=1),op=92,high=94,low=88,close=89)],T0+timedelta(hours=2))
        self.assertEqual(result["status"],"stopped")
        self.assertEqual(result["current_price"],92)

    def test_followup_expiry_does_not_count_later_target(self):
        trade=position();trade["metadata"]["execution"]["max_duration_days"]=1/24
        state,status=experiments.advance_state(trade,[bar(low=80),bar(T0+timedelta(hours=1),high=140)],T0+timedelta(hours=2))
        self.assertEqual(status,"stopped")
        self.assertTrue(state["stop_followup"]["complete"])
        self.assertFalse(state["stop_followup"]["target_revisited"])

    def test_earnings_skip_unknown_are_separate_and_frozen(self):
        self.assertEqual(self.variants("stock",{"status":"blocked"})["earnings_blackout"]["status"],"skipped")
        self.assertEqual(self.variants("stock")["earnings_blackout"]["status"],"unavailable")
        self.assertEqual(self.variants("stock",{"status":"clear"})["earnings_blackout"]["status"],"open")

    def test_paired_report_excludes_unresolved_and_unknown_counts_skip_as_zero(self):
        baseline=position(); baseline.update(status="stopped",result_R=-1,date_closed=(T0+timedelta(hours=1)).isoformat())
        skipped=dict(baseline,status="skipped",result_R=None)
        unknown=dict(baseline,status="unavailable",result_R=None)
        rows=[dict(signal_id="a",variant="baseline",state=baseline),dict(signal_id="a",variant="earnings_blackout",state=skipped),
              dict(signal_id="b",variant="baseline",state=baseline),dict(signal_id="b",variant="earnings_blackout",state=unknown),
              dict(signal_id="c",variant="baseline",state=position()),dict(signal_id="c",variant="earnings_blackout",state=skipped)]
        report=experiments.report_rows(rows)
        row=next(r for r in report["variants"] if r["variant"]=="earnings_blackout")
        self.assertEqual(row["matched_opportunities"],1)
        self.assertEqual(row["mean_delta_R"],1)
        self.assertEqual(row["unavailable"],1)
        self.assertEqual(row["matched_variant"]["closed_trades"],0)


if __name__ == "__main__":
    unittest.main()
