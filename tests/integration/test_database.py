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

    def test_analysis_archive_has_complete_joinable_history_and_null_pending_results(self):
        import csv, io, zipfile
        import report_export
        self.create([setup(), setup("ETH", approved=False)])
        self.update([bar(low=85)])
        # Shadow signals remain pending until their independent update arrives.
        output, filename, size = report_export.build_report(report_export.export_context("2030-01-01"))
        with output, zipfile.ZipFile(output) as archive:
            self.assertTrue(filename.endswith(".zip"))
            self.assertGreater(size, 100)
            metadata = json.loads(archive.read("metadata.json"))
            self.assertFalse(metadata["analytics_filter_context"]["applied_to_export_rows"])
            self.assertEqual(metadata["files"]["signals.csv"]["rows"], 2)
            self.assertEqual(set(archive.namelist()), {name+".csv" for name in report_export.TABLES} | {"metadata.json", "README.txt"})
            def rows(name):
                return list(csv.DictReader(io.StringIO(archive.read(name+".csv").decode())))
            exported_signals = rows("signals")
            predictions = rows("model_predictions")
            snapshots = rows("model_snapshots")
            exported_trades = rows("trades")
            self.assertEqual({r['signal_id'] for r in predictions}, {r['signal_id'] for r in exported_signals})
            self.assertTrue(all(r['snapshot_id'] in {m['snapshot_id'] for m in snapshots} for r in predictions))
            self.assertTrue(all(r['opportunity_result_R'] == '' for r in exported_signals))
            self.assertEqual({r['model_approved'] for r in exported_signals}, {'true','false'})
            self.assertEqual(len(exported_trades), 1)
            self.assertEqual(exported_trades[0]['outcome_state'], 'resolved_filled')
            self.assertLess(float(exported_trades[0]['net_result_R']), 0)
            self.assertTrue(rows('portfolio_ledger'))
            self.assertTrue(rows('portfolio_snapshots'))
            self.assertTrue(rows('signal_experiments'))
            self.assertNotIn('DATABASE_URL', metadata['current_settings'])
            self.assertNotIn('TELEGRAM_BOT_TOKEN', metadata['current_settings'])

    def test_analysis_archive_is_consistent_during_concurrent_signal_write(self):
        import csv, io, zipfile
        import report_export
        self.create([setup()])
        original = report_export.export_row
        inserted = False
        def concurrent_write(table, row, at):
            nonlocal inserted
            if table == 'signals' and not inserted:
                inserted = True
                self.create([setup('ETH')], at=T0+timedelta(hours=1))
            return original(table, row, at)
        with patch.object(report_export, 'export_row', side_effect=concurrent_write):
            output, _, _ = report_export.build_report(report_export.export_context())
        with output, zipfile.ZipFile(output) as archive:
            def rows(name):
                return list(csv.DictReader(io.StringIO(archive.read(name+'.csv').decode())))
            self.assertEqual(len(rows('signals')), 1)
            self.assertEqual(len(rows('model_predictions')), 1)
            self.assertEqual(len(rows('trades')), 1)
        self.assertEqual(db.fetch_one('SELECT count(*) AS n FROM signals')['n'], 2)

    def test_analysis_download_response_headers_empty_files_and_validation(self):
        import asyncio, io, zipfile
        from fastapi import HTTPException
        import api
        response = api.analysis_report_export()
        async def consume():
            return b''.join([chunk async for chunk in response.body_iterator])
        content = asyncio.run(consume())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['content-type'], 'application/zip')
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertIn('attachment;', response.headers['content-disposition'])
        self.assertEqual(int(response.headers['content-length']), len(content))
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            self.assertTrue(archive.read('signals.csv').startswith(b'signal_id,'))
            self.assertEqual(len(archive.read('signals.csv').splitlines()), 1)
        for start, end in [('bad', None), ('2030-01-01', '2026-01-01')]:
            with self.assertRaises(HTTPException) as caught:
                api.analysis_report_export(start, end)
            self.assertEqual(caught.exception.status_code, 422)
        with patch('report_export.build_report', side_effect=RuntimeError('private connection details')):
            with self.assertRaises(HTTPException) as caught:
                api.analysis_report_export()
            self.assertEqual(caught.exception.status_code, 503)
            self.assertNotIn('private connection', caught.exception.detail)

    def test_feature_challenger_fits_persisted_history_and_records_real_prediction(self):
        # Synthetic relationship exercises the whole DB -> fit -> frozen prediction path.
        # It is not a market-performance test.
        values=[]
        for day in range(120):
            observed=T0-timedelta(days=120-day)
            for j,rsi in enumerate((35,45,55,65)):
                candidate=setup(asset=f"TEST{j}",approved=False,end=observed)
                candidate["components"]={"features":{"rsi":rsi,"volume_ratio":1.3,"distance_ema20_pct":.01,"ema_gap_pct":.02,"atr":4}}
                state=signals.initial_state(candidate,observed)
                state.update(status="closed",result_R=(rsi-50)/10,date_closed=(observed+timedelta(hours=6)).isoformat())
                state["metadata"]["execution"]["pending_entry"]=False
                values.append((f"history-{day}-{j}",config.STRATEGY_VERSION,candidate["asset"],"crypto","Breakout","4h",
                               observed,observed,False,json.dumps(candidate),json.dumps(state),"closed",observed+timedelta(hours=7)))
        with db.get_db() as connection:
            with connection.cursor() as cursor:
                cursor.executemany("""INSERT INTO signals(signal_id,strategy_version,asset,asset_class,strategy,timeframe,
                    bar_end,observed_at,model_approved,setup_json,shadow_state,shadow_status,label_available_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s)""",values)
        candidate=setup();candidate["components"]={"features":{"rsi":65,"volume_ratio":1.3,"distance_ema20_pct":.01,"ema_gap_pct":.02,"atr":4}}
        self.assertEqual(len(self.create([candidate])),1)
        row=db.fetch_one("SELECT * FROM model_predictions")
        artifact=db.fetch_one("SELECT artifact FROM model_snapshots")["artifact"]
        self.assertEqual(artifact["raw"]["training_rows"],480)
        self.assertEqual(artifact["contract"]["strategy_version"],config.STRATEGY_VERSION)
        self.assertTrue(row["prediction"]["ranking_ready"])
        self.assertGreater(row["prediction"]["feature_weight"],0)
        self.assertEqual(row["prediction"]["feature_status"],"validated")
        self.assertEqual(row["prediction"]["snapshot_id"],artifact["snapshot_id"])
        self.assertEqual(row["prediction"]["prediction_basis"],"per_signal_opportunity")

    def test_entry_challenger_persists_frozen_opportunity_predictions(self):
        values = []
        for day in range(140):
            observed = T0-timedelta(days=142-day)
            for j, rsi in enumerate((35,40,45,50,55,60,65,70)):
                candidate = setup(asset=f"ENTRY{j}", approved=False, end=observed)
                candidate["components"] = {"features": {"rsi": rsi, "volume_ratio": 1.3,
                    "distance_ema20_pct": .01, "ema_gap_pct": .02, "atr": 4}}
                state = signals.initial_state(candidate, observed)
                state.update(status="cancelled" if rsi<55 else "closed", result_R=None if rsi<55 else 1.,
                             date_closed=(observed+timedelta(hours=6)).isoformat())
                values.append((f"entry-{day}-{j}", config.STRATEGY_VERSION, candidate["asset"], "crypto", "Breakout", "4h",
                               observed, observed, False, json.dumps(candidate), json.dumps(state), state["status"],
                               observed+timedelta(hours=7)))
        with db.get_db() as connection:
            with connection.cursor() as cursor:
                cursor.executemany("""INSERT INTO signals(signal_id,strategy_version,asset,asset_class,strategy,timeframe,
                    bar_end,observed_at,model_approved,setup_json,shadow_state,shadow_status,label_available_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s)""", values)
        candidate = setup()
        candidate["components"] = {"features": {"rsi": 70, "volume_ratio": 1.3,
            "distance_ema20_pct": .01, "ema_gap_pct": .02, "atr": 4}}
        self.assertEqual(len(self.create([candidate])), 1)
        row = db.fetch_one("SELECT * FROM model_predictions")
        artifact = db.fetch_one("SELECT artifact FROM model_snapshots")["artifact"]
        self.assertEqual(artifact["entry"]["training_rows"], 1120)
        self.assertEqual(artifact["raw"]["training_rows"], 560)
        self.assertTrue(artifact["validation"]["entry"]["validated"])
        prediction = row["prediction"]
        self.assertEqual(prediction["entry_status"], "validated")
        self.assertEqual(prediction["prediction_basis"], "per_signal_opportunity")
        self.assertAlmostEqual(prediction["expected_net_R"], prediction["entry_probability"]*prediction["conditional_net_R"])
        self.create([candidate])
        self.assertEqual(row, db.fetch_one("SELECT * FROM model_predictions"))

    def test_model_prediction_snapshot_is_atomic_and_rescans_preserve_it(self):
        self.create([setup(),setup("ETH",approved=False)])
        rows=db.fetch_all("SELECT * FROM model_predictions ORDER BY signal_id")
        self.assertEqual(len(rows),2)
        self.assertEqual(len({r["snapshot_id"] for r in rows}),1)
        self.assertTrue(all(not r["prediction"]["ranking_ready"] for r in rows))
        self.assertTrue(all(r["champion_selected"]==r["challenger_selected"] for r in rows))
        self.create([setup(),setup("ETH",approved=True)])
        db.initialize_db()
        self.assertEqual(rows,db.fetch_all("SELECT * FROM model_predictions ORDER BY signal_id"))
        self.assertEqual(db.fetch_one("SELECT count(*) AS n FROM model_snapshots")["n"],1)

    def test_model_write_failure_rolls_back_signal_and_portfolio(self):
        import model_research
        with patch.object(model_research,"record_batch",side_effect=RuntimeError("write failure")):
            with self.assertRaises(RuntimeError):self.create([setup()])
        for table in ("signals","trades","model_predictions","model_snapshots","signal_experiments"):
            self.assertEqual(db.fetch_one(f"SELECT count(*) AS n FROM {table}")["n"],0)
        self.assertEqual(float(db.fetch_one("SELECT reserved_cash FROM portfolio_accounts")["reserved_cash"]),0)

    def test_model_failure_keeps_funded_approval_and_persists_unavailability(self):
        import model_research
        with patch.object(model_research,"read_signal_rows",side_effect=RuntimeError("read failed")):
            ids=self.create([setup(),setup("ETH",approved=False)])
        self.assertEqual(len(ids),1)
        rows=db.fetch_all("SELECT * FROM model_predictions")
        self.assertEqual(len(rows),2)
        self.assertTrue(all(r["snapshot_id"] is None and r["prediction"]["feature_status"]=="unavailable" for r in rows))
        self.assertTrue(all(r["champion_selected"]==r["challenger_selected"] for r in rows))

    def test_database_arrival_timestamp_is_not_backdated_to_bar_close(self):
        self.create([setup()])
        before=datetime.now(timezone.utc)
        with patch.object(scanner,"fetch_asset_data",return_value={"execution":[bar(low=80)]}):
            signals.update_shadow_trades(T0+timedelta(hours=2))
        row=db.fetch_one("SELECT * FROM signals")
        self.assertGreaterEqual(row["label_available_at"],before)
        original=row["label_available_at"]
        with patch.object(scanner,"fetch_asset_data",return_value={"execution":[bar(low=80)]}):
            signals.update_shadow_trades()
        db.initialize_db()
        self.assertEqual(db.fetch_one("SELECT label_available_at FROM signals")["label_available_at"],original)

    def test_migration_backfills_old_availability_at_upgrade_only(self):
        self.create([setup()])
        with patch.object(scanner,"fetch_asset_data",return_value={"execution":[bar(low=80)]}):signals.update_shadow_trades(T0+timedelta(hours=2))
        db.execute("UPDATE signals SET label_available_at=NULL")
        before=datetime.now(timezone.utc)
        db.initialize_db()
        row=db.fetch_one("SELECT * FROM signals")
        self.assertGreaterEqual(row["label_available_at"],before)
        self.assertEqual(row["shadow_state"]["status"],"stopped")

    def test_challenger_trains_all_baseline_labels_not_experiment_duplicates(self):
        import model_dataset,model_research
        self.create([setup(),setup("ETH",approved=False),setup("SOL")])
        with patch.object(scanner,"fetch_asset_data",return_value={"execution":[bar(low=80)]}):signals.update_shadow_trades(T0+timedelta(hours=2))
        now=datetime.now(timezone.utc)+timedelta(seconds=1)
        rows=model_dataset.read_signal_rows()
        observations,coverage=model_dataset.dataset(rows,now)
        self.assertEqual(len(model_dataset.training_rows(observations,now)),3)
        self.assertEqual(coverage["model_rejected"],1)
        self.assertEqual(coverage["unselected"],2)
        self.assertEqual(db.fetch_one("SELECT count(*) AS n FROM signal_experiments")["n"],15)
        candidate=setup("XRP",end=now)
        prepared=model_research.prepare_batch([candidate],now)
        self.assertEqual(prepared["artifact"]["raw"]["training_rows"],3)
        self.assertEqual(len(prepared["artifact"]["training_ids"]),3)

    def test_concurrent_model_predictions_are_unique(self):
        with patch.object(trades,"_now_utc",return_value=T0):
            with ThreadPoolExecutor(max_workers=3) as pool:
                list(pool.map(lambda _:trades.create_trades_from_candidates([setup()]),range(3)))
        self.assertEqual(db.fetch_one("SELECT count(*) AS n FROM model_predictions")["n"],1)
        self.assertEqual(db.fetch_one("SELECT count(*) AS n FROM model_snapshots")["n"],1)

    def test_model_endpoints_render_frozen_and_chronological_evidence(self):
        import api
        self.create([setup(),setup("ETH",approved=False)])
        payload=json.loads(api.model_research_payload().body)
        self.assertEqual(payload["mode"],"shadow_only")
        self.assertEqual(payload["comparison"]["recorded_batches"],1)
        self.assertEqual(payload["comparison"]["pending_batches"],1)
        self.assertEqual(len(payload["recent_predictions"]),2)
        retrospective=json.loads(api.model_chronological_payload().body)
        self.assertEqual(retrospective["mode"],"retrospective_research")
        self.assertEqual(retrospective["comparison"]["matched_batches"],0)

    def test_quality_gap_cancel_and_shadow_match_release_all_cash(self):
        self.create([setup()])
        bars=[bar(op=120,high=125,low=119)]
        self.update(bars)
        with patch.object(scanner,"fetch_asset_data",return_value={"execution":bars}):
            signals.update_shadow_trades(T0+timedelta(hours=1))
        account=db.fetch_one("SELECT * FROM portfolio_accounts")
        self.assertEqual(float(account["cash"]),config.PORTFOLIO_INITIAL_CASH)
        self.assertEqual(float(account["reserved_cash"]),0)
        self.assertEqual(trades.list_trades()[0]["status"],"cancelled")
        state=db.fetch_one("SELECT shadow_state FROM signals")["shadow_state"]
        self.assertEqual(state["metadata"]["execution"]["cancel_reason"],"entry_net_r_below_minimum")
        self.assertEqual(db.fetch_one("SELECT count(*) AS n FROM portfolio_ledger WHERE kind='entry'")["n"],0)

    def test_experiments_are_atomic_frozen_and_idempotent(self):
        import experiments
        candidate=setup();candidate["components"]={"features":{"atr":4}}
        self.create([candidate])
        original=db.fetch_all("SELECT * FROM signal_experiments ORDER BY variant")
        self.assertEqual(len(original),5)
        candidate["components"]["features"]["atr"]=100
        self.create([candidate])
        db.initialize_db()
        self.assertEqual(original,db.fetch_all("SELECT * FROM signal_experiments ORDER BY variant"))
        other=setup("ETH")
        with patch.object(experiments,"record_experiments",side_effect=RuntimeError("experiment write failed")):
            with self.assertRaises(RuntimeError):self.create([other])
        self.assertEqual(db.fetch_one("SELECT count(*) AS n FROM signals")["n"],1)

    def test_experiment_replay_preserves_baseline_and_has_no_cash_effect(self):
        import experiments
        candidate=setup();candidate["components"]={"features":{"atr":4}}
        self.create([candidate])
        before=db.fetch_one("SELECT * FROM portfolio_accounts")
        bars=[bar(op=105,high=118,low=104,close=116)]
        with patch.object(scanner,"fetch_asset_data",return_value={"execution":bars}):
            signals.update_shadow_trades(T0+timedelta(hours=1))
            frozen=db.fetch_all("SELECT * FROM signal_experiments ORDER BY variant")
            signals.update_shadow_trades(T0+timedelta(hours=1))
        self.assertEqual(frozen,db.fetch_all("SELECT * FROM signal_experiments ORDER BY variant"))
        self.assertEqual(before,db.fetch_one("SELECT * FROM portfolio_accounts"))
        baseline=db.fetch_one("SELECT state FROM signal_experiments WHERE variant='baseline'")["state"]
        shadow=db.fetch_one("SELECT shadow_state FROM signals")["shadow_state"]
        self.assertEqual(baseline["result_R"],shadow["result_R"])
        self.assertEqual(baseline["metadata"]["execution"],shadow["metadata"]["execution"])

    def test_experiments_follow_stopped_baselines_after_shadow_closes(self):
        import experiments
        self.create([setup()])
        with patch.object(scanner,"fetch_asset_data",return_value={"execution":[bar(low=80)]}):
            signals.update_shadow_trades(T0+timedelta(hours=1))
        self.assertEqual(db.fetch_one("SELECT status FROM signal_experiments WHERE variant='baseline'")["status"],"monitoring")
        with patch.object(scanner,"fetch_asset_data",return_value={"execution":[bar(T0+timedelta(hours=1),high=140)]}):
            signals.update_shadow_trades(T0+timedelta(hours=2))
        row=next(r for r in experiments.experiment_report()["variants"] if r["variant"]=="baseline")
        self.assertEqual(row["stops_later_reaching_target"],1)
        self.assertEqual(row["stops_observed_to_resolution"],1)
        self.assertEqual(row["stop_followups_pending"],0)

    def test_earnings_experiment_coverage_is_visible_and_does_not_gate_portfolio(self):
        import experiments
        candidate=setup("AAPL","stock");candidate["components"]={"earnings":{"status":"blocked"}}
        self.create([candidate,setup("MSFT","stock")])
        self.assertEqual(len(trades.list_trades()),1)
        row=next(r for r in experiments.experiment_report()["variants"] if r["variant"]=="earnings_blackout")
        self.assertEqual(row["skipped"],1)
        self.assertEqual(row["unavailable"],1)
        self.assertEqual(row["matched_opportunities"],0)

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
        self.update([bar(op=102,high=104,low=101,close=103)])
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
        self.assertIn(b"Controlled Strategy Experiments",response.body)
        report=json.loads(api.experiment_analytics_payload().body)
        self.assertEqual(len(report["variants"]),5)
        self.assertEqual(report["variants"][0]["matched_opportunities"],2)

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
