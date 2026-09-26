"""Cash-secured paper portfolio. All mutations use the caller's transaction."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from math import floor

from config import (PORTFOLIO_RISK_PER_TRADE, PORTFOLIO_MAX_RISK, PORTFOLIO_MAX_GROSS,
                    PORTFOLIO_MAX_POSITION, PORTFOLIO_MAX_GROUP, PORTFOLIO_MAX_GROUP_POSITIONS,
                    SIM_FEE_BPS, SIM_SLIPPAGE_BPS)
from execution import side
from market_bars import as_datetime, next_hour_start
from trade_utils import get_correlation_group
from db import get_db


def lock_account(connection):
    return connection.execute("SELECT * FROM portfolio_accounts WHERE id=1 FOR UPDATE").fetchone()


def _position_rows(connection):
    return connection.execute("""
        SELECT p.*, t.asset, t.asset_class, t.strategy, t.status, t.current_price,
               t.entry_price, t.stop_loss, t.metadata_json
        FROM portfolio_positions p JOIN trades t ON t.id=p.trade_id WHERE t.status='open'
    """).fetchall()


def book_values(account, rows, now=None):
    now = now or datetime.now(timezone.utc)
    cash, reserved = float(account["cash"]), float(account["reserved_cash"])
    equity, gross, risk_exposure, stale = cash, 0.0, 0.0, 0
    groups = {}
    for row in rows:
        group = groups.setdefault(row["correlation_group"], {"positions": 0, "gross": 0.0})
        group["positions"] += 1
        pending = float(row["reserved_cash"]) > 0
        metadata = json.loads(row["metadata_json"] or "{}")
        settings = metadata.get("execution", {})
        if pending:
            exposure = float(row["reserved_cash"])
            risk_exposure += float(row["risk_budget"])
        else:
            quantity = float(row["remaining_quantity"])
            price = float(row["current_price"] or row["entry_fill"])
            slip, fee = settings.get("slippage_bps", 0) / 10000, settings.get("fee_bps", 0) / 10000
            liquidation = price * (1 - side(row) * slip)
            value = quantity * (liquidation if side(row) == 1 else 2 * row["entry_fill"] - liquidation)
            equity += value - quantity * liquidation * fee
            exposure = quantity * price
            ratio = quantity / float(row["quantity"]) if row["quantity"] else 0
            risk_exposure += float(row["risk_budget"]) * ratio
        gross += exposure
        group["gross"] += exposure
        if settings.get("data_gap"):
            stale += 1
        elif settings.get("cursor"):
            expected = next_hour_start(as_datetime(settings["cursor"]), row["asset_class"])
            if now > expected + timedelta(hours=1, minutes=30):
                stale += 1
    return {"cash": cash, "reserved_cash": reserved, "available_cash": cash-reserved,
            "equity": equity, "gross_exposure": gross, "risk_exposure": risk_exposure,
            "groups": groups, "stale_positions": stale}


def allocation_plan(setup, values):
    equity = values["equity"]
    if equity <= 0 or values["stale_positions"]:
        return None
    group = get_correlation_group(setup["asset"], setup["asset_class"])
    current = values["groups"].get(group, {"positions": 0, "gross": 0})
    if current["positions"] >= PORTFOLIO_MAX_GROUP_POSITIONS:
        return None
    entry = float(setup["entry_price"]) * (1 + side(setup) * SIM_SLIPPAGE_BPS / 10000)
    per_unit_risk = side(setup) * (entry-float(setup["stop_loss"]))
    if per_unit_risk <= 0:
        return None
    budget = min(equity*PORTFOLIO_RISK_PER_TRADE, equity*PORTFOLIO_MAX_RISK-values["risk_exposure"])
    notional = min(values["available_cash"], equity*PORTFOLIO_MAX_POSITION,
                   equity*PORTFOLIO_MAX_GROSS-values["gross_exposure"],
                   equity*PORTFOLIO_MAX_GROUP-current["gross"])
    # Reserve entry notional plus fee. Fractional units are allowed in this simulator.
    unit_cost = entry*(1+SIM_FEE_BPS/10000)
    quantity = floor(max(0.0, min(budget/per_unit_risk, notional/unit_cost))*1e8)/1e8
    if quantity <= 0:
        return None
    return {"planned_quantity": quantity, "reserved_cash": quantity*unit_cost,
            "risk_budget": quantity*per_unit_risk, "correlation_group": group}


def reserve(connection, trade_id, plan):
    connection.execute("""
        INSERT INTO portfolio_positions(trade_id, correlation_group, planned_quantity, reserved_cash, risk_budget)
        VALUES (%s,%s,%s,%s,%s)
    """, (trade_id, plan["correlation_group"], plan["planned_quantity"], plan["reserved_cash"], plan["risk_budget"]))
    connection.execute("UPDATE portfolio_accounts SET reserved_cash=reserved_cash+%s WHERE id=1", (plan["reserved_cash"],))


def adopt_legacy_positions(connection):
    """Start the portfolio at adoption marks; don't invent pre-upgrade portfolio P/L."""
    account = lock_account(connection)
    rows = connection.execute("""
        SELECT t.* FROM trades t LEFT JOIN portfolio_positions p ON p.trade_id=t.id
        WHERE t.status='open' AND p.trade_id IS NULL ORDER BY t.id
    """).fetchall()
    available = float(account["cash"])-float(account["reserved_cash"])
    for row in rows:
        metadata = json.loads(row["metadata_json"] or "{}")
        settings = metadata.get("execution", {})
        pending = settings.get("pending_entry", False)
        price = float(row["current_price"] or row["entry_price"])
        quantity = 100.0 / float(row["entry_price"])
        remaining = quantity * (0.5 if row.get("partial_taken") else 1)
        amount = remaining*price
        if amount > available:
            raise RuntimeError("Initial portfolio cash cannot fund existing positions; configure SWING_LAB_INITIAL_CASH before first migration")
        available -= amount
        connection.execute("""
            INSERT INTO portfolio_positions(trade_id, correlation_group, planned_quantity, quantity,
                remaining_quantity, entry_fill, reserved_cash, risk_budget, applied_events, legacy)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,true)
        """, (row["id"], get_correlation_group(row["asset"],row["asset_class"]), quantity,
                0 if pending else quantity, 0 if pending else remaining, None if pending else price,
                amount if pending else 0, quantity*abs(row["entry_price"]-row["stop_loss"]),
                len(settings.get("events",[]))))
        if pending:
            connection.execute("UPDATE portfolio_accounts SET reserved_cash=reserved_cash+%s WHERE id=1", (amount,))
        else:
            connection.execute("UPDATE portfolio_accounts SET cash=cash-%s WHERE id=1", (amount,))
            connection.execute("""
                INSERT INTO portfolio_ledger(event_key,trade_id,occurred_at,kind,cash_delta,quantity_delta,price)
                VALUES (%s,%s,now(),'legacy_adoption',%s,%s,%s) ON CONFLICT DO NOTHING
            """, (f"legacy:{row['id']}",row["id"],-amount,remaining,price))


def apply_events(connection, trade):
    row = connection.execute("SELECT * FROM portfolio_positions WHERE trade_id=%s FOR UPDATE", (trade["id"],)).fetchone()
    if row is None:
        raise RuntimeError("Trade has no funded portfolio position")
    settings = trade["metadata"]["execution"]
    events = settings["events"]
    quantity, remaining = float(row["quantity"]), float(row["remaining_quantity"])
    reserved, entry_fill = float(row["reserved_cash"]), row["entry_fill"]
    for index in range(row["applied_events"],len(events)):
        event = events[index]
        price, kind = float(event["price"]), event["kind"]
        delta, units = 0., 0.
        fee_rate = settings["fee_bps"]/10000
        if kind == "entry":
            per_unit_risk = abs(price-trade["stop_loss"])
            quantity = floor(min(float(row["planned_quantity"]),float(row["risk_budget"])/per_unit_risk,
                                 reserved/(price*(1+fee_rate)))*1e8)/1e8
            remaining, entry_fill = quantity, price
            delta, units = -quantity*price*(1+fee_rate), quantity
        elif kind in ("partial","stopped","target_hit","closed"):
            sold = min(remaining, quantity*float(event["size"]))
            delta = sold*(price if side(trade)==1 else 2*entry_fill-price) - sold*price*fee_rate
            remaining -= sold
            units = -sold
        if kind in ("entry","entry_cancelled"):
            connection.execute("UPDATE portfolio_accounts SET reserved_cash=GREATEST(0,reserved_cash-%s) WHERE id=1", (reserved,))
            reserved = 0
        connection.execute("""
            INSERT INTO portfolio_ledger(event_key,trade_id,occurred_at,kind,cash_delta,quantity_delta,price)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
        """, (f"trade:{trade['id']}:event:{index}",trade["id"],event["at"],kind,delta,units,price))
        connection.execute("UPDATE portfolio_accounts SET cash=cash+%s WHERE id=1", (delta,))
    connection.execute("""
        UPDATE portfolio_positions SET quantity=%s,remaining_quantity=%s,entry_fill=%s,
            reserved_cash=%s,applied_events=%s WHERE trade_id=%s
    """, (quantity,remaining,entry_fill,reserved,len(events),trade["id"]))


def snapshot(connection, now=None):
    now = now or datetime.now(timezone.utc)
    account = lock_account(connection)
    values = book_values(account,_position_rows(connection),now)
    peak = max(float(account["peak_equity"]),values["equity"])
    drawdown = (peak-values["equity"])/peak*100 if peak else 0
    connection.execute("UPDATE portfolio_accounts SET peak_equity=%s,max_drawdown_pct=GREATEST(max_drawdown_pct,%s) WHERE id=1", (peak,drawdown))
    connection.execute("""
        INSERT INTO portfolio_snapshots(at,cash,equity,reserved_cash,gross_exposure,risk_exposure,drawdown_pct,stale_positions)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
    """, (now, values["cash"],values["equity"],values["reserved_cash"],values["gross_exposure"],values["risk_exposure"],drawdown,values["stale_positions"]))
    return values


def portfolio_payload():
    with get_db() as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        account = connection.execute("SELECT * FROM portfolio_accounts WHERE id=1").fetchone()
        values = book_values(account,_position_rows(connection))
        history = connection.execute("SELECT * FROM portfolio_snapshots ORDER BY at DESC, id DESC LIMIT 500").fetchall()
    return values | {"initial_cash":float(account["initial_cash"]),"peak_equity":float(account["peak_equity"]),
                     "max_drawdown_pct":account["max_drawdown_pct"],
                     "history":[{k:(v.isoformat() if isinstance(v,datetime) else float(v) if hasattr(v,'as_tuple') else v)
                                 for k,v in row.items()} for row in reversed(history)],
                     "drawdown_basis":"Sampled net liquidation equity across concurrent positions; stale marks are flagged."}
