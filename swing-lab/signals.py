"""Immutable signal decisions and prospective counterfactual simulations."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from config import STRATEGY_VERSION
from db import get_db, fetch_all
from execution import execution_settings, replay_bars
from evaluation import outcome_summary
from market_bars import as_datetime


def signal_id(setup):
    end = as_datetime(setup["signal_bar_end"]).isoformat()
    parts = [STRATEGY_VERSION, setup["asset_class"], setup["asset"],setup["strategy"],setup["timeframe"],end]
    return hashlib.sha256(json.dumps(parts,separators=(",",":")).encode()).hexdigest()


def initial_state(setup, observed_at):
    metadata = dict(setup.get("components",{}))
    metadata.update(strategy_version=STRATEGY_VERSION,execution=execution_settings(observed_at,setup["strategy"]),
                    signal_bar_end=setup["signal_bar_end"])
    return {key:setup[key] for key in ("asset","asset_class","strategy","timeframe","entry_price","stop_loss",
                                       "target_price","R_multiple","score")} | {
        "id":signal_id(setup),"signal_id":signal_id(setup),"metadata":metadata,
        "current_price":setup["entry_price"],"date_opened":observed_at.isoformat(),"date_closed":None,
        "status":"open","result_R":None,"partial_taken":False,"partial_result_R":0.,
        "runner_activated":False,"effective_stop_loss":setup["stop_loss"],
        "setup_notes":setup.get("setup_notes","")}


def record_signals(connection, candidates, observed_at):
    fresh = []
    for setup in candidates:
        state = initial_state(setup,observed_at)
        approved = not setup.get("model_feedback") or bool(setup["model_feedback"]["approved"])
        row = connection.execute("""
            INSERT INTO signals(signal_id,strategy_version,asset,asset_class,strategy,timeframe,bar_end,
                                observed_at,model_approved,setup_json,shadow_state)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)
            ON CONFLICT DO NOTHING RETURNING signal_id
        """, (state["signal_id"],STRATEGY_VERSION,setup["asset"],setup["asset_class"],setup["strategy"],setup["timeframe"],
                setup["signal_bar_end"],observed_at,approved,json.dumps(setup),json.dumps(state))).fetchone()
        if row:
            fresh.append(setup)
    return fresh


def update_shadow_trades(now=None):
    from scanner import fetch_asset_data
    now = now or datetime.now(timezone.utc)
    rows = fetch_all("SELECT signal_id,asset,asset_class,shadow_state FROM signals WHERE shadow_status='open' ORDER BY observed_at")
    datasets = {}
    count = 0
    for row in rows:
        key = (row["asset"],row["asset_class"])
        if key not in datasets:
            datasets[key] = fetch_asset_data(*key)
        old = row["shadow_state"]
        state = replay_bars(old,datasets[key].get("execution",[]),now)
        if state == old:
            continue
        with get_db() as connection:
            result = connection.execute("""
                UPDATE signals SET shadow_state=%s::jsonb,shadow_status=%s
                WHERE signal_id=%s AND shadow_state=%s::jsonb RETURNING signal_id
            """, (json.dumps(state),state["status"],row["signal_id"],json.dumps(old))).fetchone()
        count += bool(result)
    return count


def shadow_report():
    rows = fetch_all("SELECT model_approved,selected_trade_id,shadow_state FROM signals WHERE strategy_version=%s", (STRATEGY_VERSION,))
    closed = [r for r in rows if r["shadow_state"]["result_R"] is not None]
    return {"strategy_version":STRATEGY_VERSION,"signals":len(rows),
            "open":sum(r["shadow_state"]["status"]=="open" for r in rows),
            "cancelled":sum(r["shadow_state"]["status"]=="cancelled" for r in rows),
            "data_gaps":sum(bool(r["shadow_state"]["metadata"]["execution"].get("data_gap")) for r in rows),
            "baseline_all_qualifying":outcome_summary([r["shadow_state"] for r in closed]),
            "overlay_approved":outcome_summary([r["shadow_state"] for r in closed if r["model_approved"]]),
            "overlay_rejected":outcome_summary([r["shadow_state"] for r in closed if not r["model_approved"]]),
            "selected":outcome_summary([r["shadow_state"] for r in closed if r["selected_trade_id"] is not None]),
            "basis":"Prospectively recorded qualifying signals, frozen model verdicts, identical fills and costs. Per-signal comparison, not two independently funded portfolio returns. Earlier unrecorded signals cannot be reconstructed."}
