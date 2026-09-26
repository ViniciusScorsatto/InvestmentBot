"""Real Postgres tests. Every case uses a fresh, isolated schema, never production tables."""
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/"swing-lab"))
import psycopg
from psycopg.rows import dict_row
from psycopg import sql
import db, trades, signals, portfolio, outbox, execution, config, scanner, learning_model

T0=datetime(2026,9,21,tzinfo=timezone.utc)
URL=os.getenv("TEST_DATABASE_URL")


def setup(asset="BTC",asset_class="crypto",score=90,approved=True,end=T0):
    return dict(asset=asset,asset_class=asset_class,strategy="Breakout",timeframe="4h",entry_price=100.,
                stop_loss=90.,target_price=130.,R_multiple=3.,score=score,combined_score=score,
                signal_bar_end=end.isoformat(),components={},model_feedback={"approved":approved,"model_score":50})


def bar(at=T0,op=100.,high=104.,low=99.,close=102.):
    return dict(timestamp=at.isoformat(),end_timestamp=(at+timedelta(hours=1)).isoformat(),
                open=op,high=high,low=low,close=close,volume=100.)


@unittest.skipUnless(URL,"TEST_DATABASE_URL is required for real database tests")
class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.schema="test_"+uuid4().hex
        with psycopg.connect(URL,autocommit=True) as c:
            c.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.connection_patch=patch.object(db,"get_connection",side_effect=lambda:psycopg.connect(URL,row_factory=dict_row,options=f"-c search_path={self.schema}"))
        self.connection_patch.start()
        db.initialize_db()
        learning_model.clear_learning_cache()

    def tearDown(self):
        self.connection_patch.stop()
        with psycopg.connect(URL,autocommit=True) as c:
            c.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    def create(self,rows,at=T0):
        with patch.object(trades,"_now_utc",return_value=at):
            return trades.create_trades_from_candidates(rows)

    def update(self,bars,now=None):
        with patch.object(trades,"_now_utc",return_value=now or T0+timedelta(hours=2)),patch.object(trades,"fetch_asset_data",return_value={"execution":bars}):
            return trades.update_open_trades()

    def test_migration_repeatable_without_resetting_cash(self):
        self.create([setup()]); self.update([bar()])
        before=db.fetch_one("SELECT * FROM portfolio_accounts")
        db.initialize_db()
        after=db.fetch_one("SELECT * FROM portfolio_accounts")
        self.assertEqual(before["cash"],after["cash"])
        self.assertEqual(before["initial_cash"],after["initial_cash"])

    def test_signal_position_reservation_and_outbox_are_atomic(self):
        ids=self.create([setup()])
        self.assertEqual(len(ids),1)
        trade=trades.get_trade(ids[0])
        self.assertTrue(trade["metadata"]["execution"]["pending_entry"])
        self.assertEqual(trade["metadata"]["strategy_version"],config.STRATEGY_VERSION)
        account=db.fetch_one("SELECT * FROM portfolio_accounts")
        self.assertGreater(account["reserved_cash"],0)
        self.assertEqual(float(account["cash"]),config.PORTFOLIO_INITIAL_CASH)
        self.assertEqual(db.fetch_one("SELECT count(*) AS n FROM notification_outbox")["n"],1)
        self.assertEqual(db.fetch_one("SELECT selected_trade_id FROM signals")["selected_trade_id"],ids[0])

    def test_failed_outbox_insert_rolls_back_signal_trade_and_reservation(self):
        with patch.object(trades,"notify_new_trade",side_effect=RuntimeError("outbox failure")):
            with self.assertRaises(RuntimeError): self.create([setup()])
        self.assertEqual(db.fetch_one("SELECT count(*) AS n FROM trades")["n"],0)
        self.assertEqual(db.fetch_one("SELECT count(*) AS n FROM signals")["n"],0)
        self.assertEqual(float(db.fetch_one("SELECT reserved_cash FROM portfolio_accounts")["reserved_cash"]),0)

    def test_concurrent_same_signal_creates_one_position(self):
        with patch.object(trades,"_now_utc",return_value=T0):
            with ThreadPoolExecutor(max_workers=4) as pool:
                results=list(pool.map(lambda _:trades.create_trades_from_candidates([setup()]),range(4)))
        self.assertEqual(sum(len(r) for r in results),1)
        self.assertEqual(db.fetch_one("SELECT count(*) AS n FROM signals")["n"],1)
        self.assertEqual(db.fetch_one("SELECT count(*) AS n FROM notification_outbox")["n"],1)

    def test_database_unique_active_position_and_signal(self):
        self.create([setup()])
        with self.assertRaises(psycopg.errors.UniqueViolation):
            db.execute("""INSERT INTO trades(asset,asset_class,strategy,timeframe,entry_price,stop_loss,target_price,
                       R_multiple,score,date_opened,status) VALUES ('BTC','crypto','Breakout','4h',100,90,130,3,90,now(),'open')""")
        with self.assertRaises(psycopg.errors.UniqueViolation):
            db.execute("""INSERT INTO trades(asset,asset_class,strategy,timeframe,entry_price,stop_loss,target_price,
                       R_multiple,score,date_opened,status,signal_id) SELECT 'ETH',asset_class,strategy,timeframe,entry_price,
                       stop_loss,target_price,R_multiple,score,date_opened,'closed',signal_id FROM trades LIMIT 1""")

    def test_diversification_after_full_pool_and_skip_existing_asset(self):
        rows=[setup(asset=a,score=100-i) for i,a in enumerate(["BTC","ETH","SOL","ADA","XRP"])]
        rows.extend([setup("AAPL","stock",80),setup("SPY","etf",78)])
        ids=self.create(rows)
        self.assertEqual([trades.get_trade(i)["asset"] for i in ids],["BTC","AAPL","SPY"])
        self.assertEqual(db.fetch_one("SELECT count(*) AS n FROM signals")["n"],7)

    def test_group_exposure_applies_across_days(self):
        self.create([setup()])
        ids=self.create([setup("ETH",end=T0+timedelta(days=1))],at=T0+timedelta(days=1))
        self.assertEqual(ids,[])
        self.assertEqual(db.fetch_one("SELECT count(*) AS n FROM signals")["n"],2)

    def test_daily_limit_still_records_shadow_candidates(self):
        # Fill the daily quota with distinct groups, releasing each before the next.
        with patch.object(trades,"MAX_TRADES_PER_DAY",1):
            self.create([setup()])
            self.create([setup("AAPL","stock")])
        self.assertEqual(db.fetch_one("SELECT count(*) AS n FROM trades")["n"],1)
        self.assertEqual(db.fetch_one("SELECT count(*) AS n FROM signals")["n"],2)

    def test_shadow_rejection_frozen_and_same_execution_as_selected(self):
        self.create([setup(),setup("ETH",approved=False)])
        with patch.object(scanner,"fetch_asset_data",return_value={"execution":[bar(low=80)]}):
            signals.update_shadow_trades(T0+timedelta(hours=2))
        self.update([bar(low=80)])
        report=signals.shadow_report()
        self.assertEqual(report["baseline_all_qualifying"]["closed_trades"],2)
        self.assertEqual(report["overlay_rejected"]["closed_trades"],1)
        actual=trades.list_trades()[0]
        shadow=db.fetch_one("SELECT shadow_state FROM signals WHERE selected_trade_id=%s",(actual["id"],))["shadow_state"]
        self.assertEqual(actual["result_R"],shadow["result_R"])
        self.assertEqual(self.create([setup("ETH",approved=True)]),[])
        self.assertFalse(db.fetch_one("SELECT model_approved FROM signals WHERE asset='ETH'")["model_approved"])

    def test_fills_ledger_pnl_and_retries(self):
        ids=self.create([setup()])
        bars=[bar(),bar(T0+timedelta(hours=1),op=85,high=88,low=83,close=87)]
        self.update(bars)
        before=db.fetch_one("SELECT * FROM portfolio_accounts")
        count=db.fetch_one("SELECT count(*) AS n FROM portfolio_ledger")["n"]
        self.update(bars)
        after=db.fetch_one("SELECT * FROM portfolio_accounts")
        self.assertEqual(before["cash"],after["cash"])
        self.assertEqual(count,db.fetch_one("SELECT count(*) AS n FROM portfolio_ledger")["n"])
        self.assertEqual(float(after["reserved_cash"]),0)
        p=db.fetch_one("SELECT * FROM portfolio_positions")
        t=trades.get_trade(ids[0])
        expected=config.PORTFOLIO_INITIAL_CASH+float(p["quantity"])*abs(t["entry_price"]-t["stop_loss"])*t["result_R"]
        self.assertAlmostEqual(float(after["cash"]),expected,places=6)
        self.assertEqual(db.fetch_one("SELECT count(*) AS n FROM notification_outbox")["n"],2)

    def test_gap_entry_resizes_to_reserved_cash_and_risk(self):
        self.create([setup()])
        p=db.fetch_one("SELECT * FROM portfolio_positions")
        self.update([bar(op=120,high=122,low=119,close=121)])
        current=db.fetch_one("SELECT * FROM portfolio_positions")
        self.assertLess(current["quantity"],p["planned_quantity"])
        self.assertLessEqual(current["quantity"]*(current["entry_fill"]-90),float(p["risk_budget"])+1e-6)
        self.assertGreaterEqual(float(db.fetch_one("SELECT cash FROM portfolio_accounts")["cash"]),0)

    def test_cancelled_entry_releases_reservation(self):
        self.create([setup()]); self.update([bar(op=140,high=145,low=135,close=142)])
        account=db.fetch_one("SELECT * FROM portfolio_accounts")
        self.assertEqual(float(account["cash"]),config.PORTFOLIO_INITIAL_CASH)
        self.assertEqual(float(account["reserved_cash"]),0)
        self.assertEqual(trades.list_trades()[0]["status"],"cancelled")

    def test_mark_to_market_includes_concurrent_positions(self):
        self.create([setup(),setup("AAPL","stock")])
        # Align stock execution with its session; keep both open and mark lower.
        for t in trades.list_trades():
            state=t["metadata"]["execution"]
            state.update(pending_entry=False,entry_at=T0.isoformat())
            state["events"]=[{"kind":"entry","price":100,"at":T0.isoformat(),"size":1}]
            t["metadata"]["execution"]=state
            with db.get_db() as c:
                portfolio.lock_account(c)
                c.execute("UPDATE trades SET current_price=95,metadata_json=%s WHERE id=%s",(json.dumps(t["metadata"]),t["id"]))
                portfolio.apply_events(c,t)
        with db.get_db() as c: values=portfolio.snapshot(c,T0+timedelta(hours=1))
        self.assertLess(values["equity"],config.PORTFOLIO_INITIAL_CASH)
        self.assertGreater(db.fetch_one("SELECT max_drawdown_pct FROM portfolio_accounts")["max_drawdown_pct"],0)
        self.assertEqual(len(values["groups"]),2)

    def test_trade_write_failure_rolls_back_portfolio_and_outbox(self):
        self.create([setup()]); before=db.fetch_one("SELECT * FROM portfolio_accounts")
        with patch.object(portfolio,"apply_events",side_effect=RuntimeError("ledger failure")):
            with self.assertRaises(RuntimeError):self.update([bar(low=80)])
        self.assertEqual(trades.list_trades()[0]["status"],"open")
        self.assertEqual(db.fetch_one("SELECT cash FROM portfolio_accounts")["cash"],before["cash"])
        self.assertEqual(db.fetch_one("SELECT count(*) AS n FROM notification_outbox")["n"],1)

    def test_scan_progress_commits_with_signals(self):
        window=dict(asset="BTC",asset_class="crypto",timeframe="4h",bar_end=T0.isoformat())
        with patch.object(trades,"_now_utc",return_value=T0):
            trades.create_trades_from_candidates([setup()],completed_windows=[window])
        self.assertEqual(db.fetch_one("SELECT bar_end FROM scan_progress")["bar_end"],T0)

    def test_outbox_transport_failure_retries_without_secret_leak(self):
        self.create([setup()])
        with patch.object(outbox,"TELEGRAM_BOT_TOKEN","test-token"),patch.object(outbox,"TELEGRAM_CHAT_ID","test-chat"),patch.object(outbox.requests,"post",side_effect=outbox.requests.ConnectionError("secret-url")):
            self.assertEqual(outbox.deliver_pending(),0)
        row=db.fetch_one("SELECT * FROM notification_outbox")
        self.assertEqual(row["attempts"],1)
        self.assertNotIn("secret",row["last_error"])
        self.assertIsNone(row["lease_token"])
        db.execute("UPDATE notification_outbox SET next_attempt_at=now()")
        class Response:
            ok=True
            def json(self):return {"ok":True}
        with patch.object(outbox,"TELEGRAM_BOT_TOKEN","test-token"),patch.object(outbox,"TELEGRAM_CHAT_ID","test-chat"),patch.object(outbox.requests,"post",return_value=Response()) as post:
            self.assertEqual(outbox.deliver_pending(),1)
            self.assertEqual(outbox.deliver_pending(),0)
        self.assertEqual(post.call_count,1)

    def test_outbox_claims_are_exclusive_and_expired_lease_recovers(self):
        self.create([setup()])
        db.execute("UPDATE notification_outbox SET lease_token='crashed',lease_until=now()-interval '1 second'")
        class Response:
            ok=True
            def json(self):return {"ok":True}
        with patch.object(outbox,"TELEGRAM_BOT_TOKEN","test-token"),patch.object(outbox,"TELEGRAM_CHAT_ID","test-chat"),patch.object(outbox.requests,"post",return_value=Response()) as post:
            with ThreadPoolExecutor(max_workers=3) as pool:
                sent=list(pool.map(lambda _:outbox.deliver_pending(),range(3)))
        self.assertEqual(sum(sent),1)
        self.assertEqual(post.call_count,1)

    def test_duplicate_migration_does_not_destroy_history(self):
        self.create([setup()])
        with db.get_db() as c:
            c.execute("DROP INDEX uq_active_position")
            c.execute("""INSERT INTO trades(asset,asset_class,strategy,timeframe,entry_price,stop_loss,target_price,
                         R_multiple,score,date_opened,status) VALUES ('BTC','crypto','Breakout','4h',100,90,130,3,90,now(),'open')""")
        with self.assertRaisesRegex(RuntimeError,"duplicates must be reconciled"):
            db.initialize_db()
        self.assertEqual(db.fetch_one("SELECT count(*) AS n FROM trades")["n"],2)

    def test_legacy_adoption_uses_mark_and_only_happens_once(self):
        db.execute("""INSERT INTO trades(asset,asset_class,strategy,timeframe,entry_price,stop_loss,target_price,
                     current_price,R_multiple,score,date_opened,status) VALUES ('BTC','crypto','Breakout','4h',100,90,130,110,3,90,now(),'open')""")
        db.initialize_db(); db.initialize_db()
        self.assertEqual(db.fetch_one("SELECT count(*) AS n FROM portfolio_ledger WHERE kind='legacy_adoption'")["n"],1)
        self.assertEqual(float(db.fetch_one("SELECT cash FROM portfolio_accounts")["cash"]),config.PORTFOLIO_INITIAL_CASH-110)

    def test_partial_exit_cash_matches_net_r_and_leaves_no_units(self):
        candidate=setup();candidate.update(strategy="Trend Pullback",target_price=120.,R_multiple=2.)
        ids=self.create([candidate])
        self.update([bar(),bar(T0+timedelta(hours=1),op=112,high=122,low=111,close=121)])
        t=trades.get_trade(ids[0]);p=db.fetch_one("SELECT * FROM portfolio_positions")
        cash=float(db.fetch_one("SELECT cash FROM portfolio_accounts")["cash"])
        self.assertEqual(p["remaining_quantity"],0)
        self.assertTrue(t["partial_taken"])
        self.assertAlmostEqual(cash,config.PORTFOLIO_INITIAL_CASH+p["quantity"]*abs(t["entry_price"]-t["stop_loss"])*t["result_R"],places=6)
        self.assertEqual(db.fetch_one("SELECT count(*) AS n FROM portfolio_ledger WHERE kind='partial'")["n"],1)

    def test_cash_secured_short_exit_releases_collateral_and_profit(self):
        candidate=setup();candidate.update(strategy="Breakdown",stop_loss=110.,target_price=70.)
        ids=self.create([candidate])
        self.update([bar(op=100,high=102,low=98,close=99),bar(T0+timedelta(hours=1),op=85,high=88,low=68,close=70)])
        t=trades.get_trade(ids[0]);p=db.fetch_one("SELECT * FROM portfolio_positions")
        self.assertEqual(t["status"],"target_hit")
        cash=float(db.fetch_one("SELECT cash FROM portfolio_accounts")["cash"])
        self.assertAlmostEqual(cash,config.PORTFOLIO_INITIAL_CASH+p["quantity"]*abs(t["entry_price"]-t["stop_loss"])*t["result_R"],places=6)
        self.assertGreater(cash,config.PORTFOLIO_INITIAL_CASH)

    def test_closed_signal_cannot_be_reentered_and_new_candle_can(self):
        self.create([setup()]);self.update([bar(low=80)])
        self.assertEqual(self.create([setup()]),[])
        self.assertEqual(len(self.create([setup(end=T0+timedelta(hours=4))],at=T0+timedelta(hours=4))),1)

    def test_concurrent_different_signals_cannot_overallocate_same_group(self):
        with patch.object(trades,"_now_utc",return_value=T0):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(lambda a:trades.create_trades_from_candidates([setup(a)]),["BTC","ETH"]))
        self.assertEqual(sum(len(r) for r in results),1)
        self.assertEqual(db.fetch_one("SELECT count(*) AS n FROM signals")["n"],2)

    def test_429_response_retains_event_and_respects_retry_hint(self):
        self.create([setup()])
        class Response:
            ok=False
            status_code=429
            def json(self):return {"ok":False,"parameters":{"retry_after":600}}
        with patch.object(outbox,"TELEGRAM_BOT_TOKEN","test-token"),patch.object(outbox,"TELEGRAM_CHAT_ID","test-chat"),patch.object(outbox.requests,"post",return_value=Response()):
            self.assertEqual(outbox.deliver_pending(),0)
        row=db.fetch_one("SELECT * FROM notification_outbox")
        self.assertIsNone(row["sent_at"])
        self.assertGreater((row["next_attempt_at"]-datetime.now(timezone.utc)).total_seconds(),590)

    def test_portfolio_and_shadow_api_render_real_responses(self):
        import api
        from starlette.requests import Request
        self.create([setup(),setup("ETH",approved=False)])
        self.update([bar(low=80)])
        with patch.object(scanner,"fetch_asset_data",return_value={"execution":[bar(low=80)]}):signals.update_shadow_trades(T0+timedelta(hours=2))
        data=json.loads(api.portfolio_analytics_payload().body)
        self.assertLess(data["equity"],config.PORTFOLIO_INITIAL_CASH)
        self.assertGreater(data["max_drawdown_pct"],0)
        self.assertEqual(json.loads(api.shadow_analytics_payload().body)["overlay_rejected"]["closed_trades"],1)
        request=Request({"type":"http","method":"GET","path":"/analytics","headers":[],"query_string":b""})
        response=api.analytics_page(request,start_date="2030-01-01",end_date=None)
        self.assertEqual(response.status_code,200)
        self.assertIn(b"Simulated Portfolio",response.body)
        self.assertIn(b"Model rejected",response.body)

    def test_occupied_top_candidates_do_not_block_lower_eligible_groups(self):
        self.create([setup()])
        later=T0+timedelta(hours=4)
        self.update([bar(T0+timedelta(hours=i)) for i in range(4)],now=later)
        rows=[setup(score=100,end=later),setup("ETH",score=99,end=later),
              setup("AAPL","stock",80,end=later),setup("SPY","etf",78,end=later)]
        ids=self.create(rows,at=later)
        self.assertEqual([trades.get_trade(i)["asset"] for i in ids],["AAPL","SPY"])

    def test_concurrent_groups_respect_daily_quota(self):
        with patch.object(trades,"_now_utc",return_value=T0),patch.object(trades,"MAX_TRADES_PER_DAY",1):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(lambda row:trades.create_trades_from_candidates([row]),[setup(),setup("AAPL","stock")]))
        self.assertEqual(sum(len(r) for r in results),1)
        self.assertEqual(db.fetch_one("SELECT count(*) AS n FROM signals")["n"],2)
