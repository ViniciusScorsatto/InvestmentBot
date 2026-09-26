from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from config import STRATEGY_VERSION, MAX_TRADES_PER_DAY, PREFERRED_TOP_SETUPS
from execution import execution_settings, replay_bars, net_result, exit_fill
from db import execute, fetch_all, fetch_one, get_db
import portfolio
from signals import initial_state, record_signals, signal_id
from learning_model import clear_learning_cache
from scanner import fetch_asset_data, select_best_setups
from telegram import notify_trade_closed, notify_new_trade
from trade_utils import get_correlation_group, get_trade_direction


LOGGER = logging.getLogger(__name__)

STATUS_LABELS = {
    "open": "Open",
    "stopped": "Stopped",
    "target_hit": "Target Hit",
    "closed": "Timed Exit",
    "cancelled": "Entry Cancelled",
}
DISPLAY_NOTIONAL_USD = 100.0


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat()


def _coerce_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _row_to_trade(row: Any) -> dict[str, Any]:
    trade = dict(row)
    for canonical, folded in (
        ("R_multiple", "r_multiple"),
        ("result_R", "result_r"),
        ("partial_result_R", "partial_result_r"),
    ):
        if canonical not in trade and folded in trade:
            trade[canonical] = trade[folded]
    metadata_json = trade.get("metadata_json")
    trade["metadata"] = json.loads(metadata_json) if metadata_json else {}
    for key in ("date_opened", "date_closed", "partial_taken_at", "runner_activated_at"):
        if trade.get(key) is not None and isinstance(trade[key], datetime):
            trade[key] = trade[key].isoformat()
    return trade


def get_open_trade(asset: str, timeframe: str) -> dict[str, Any] | None:
    row = fetch_one(
        """
        SELECT * FROM trades
        WHERE asset = %s AND timeframe = %s AND status = 'open'
        ORDER BY id DESC
        LIMIT 1
        """,
        (asset, timeframe),
    )
    return _row_to_trade(row) if row else None


def _create_trade(connection, setup, now):
    if setup.get("model_feedback") and not setup["model_feedback"]["approved"]:
        return None
    day_start = now.replace(hour=0,minute=0,second=0,microsecond=0)
    if connection.execute("SELECT count(*) AS n FROM trades WHERE date_opened >= %s", (day_start,)).fetchone()["n"] >= MAX_TRADES_PER_DAY:
        return None
    if connection.execute("SELECT id FROM trades WHERE (asset=%s AND timeframe=%s AND status='open') OR signal_id=%s",
                          (setup["asset"],setup["timeframe"],signal_id(setup))).fetchone():
        return None
    account = portfolio.lock_account(connection)
    values = portfolio.book_values(account,portfolio._position_rows(connection),now)
    plan = portfolio.allocation_plan(setup,values)
    if plan is None:
        return None
    state = initial_state(setup,now)
    row = connection.execute(
        """
        INSERT INTO trades(asset,asset_class,strategy,timeframe,entry_price,stop_loss,target_price,current_price,
                           R_multiple,score,date_opened,status,setup_notes,metadata_json,effective_stop_loss,
                           partial_taken,partial_result_R,runner_activated,signal_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'open',%s,%s,%s,false,0,false,%s)
        ON CONFLICT DO NOTHING RETURNING id
        """,
        (setup["asset"],setup["asset_class"],setup["strategy"],setup["timeframe"],setup["entry_price"],
         setup["stop_loss"],setup["target_price"],setup["entry_price"],setup["R_multiple"],setup["score"],
         now,setup.get("setup_notes",""),json.dumps(state["metadata"]),setup["stop_loss"],state["signal_id"]),
    ).fetchone()
    if not row:
        return None
    trade_id = row["id"]
    portfolio.reserve(connection,trade_id,plan)
    connection.execute("UPDATE signals SET selected_trade_id=%s WHERE signal_id=%s", (trade_id,state["signal_id"]))
    notify_new_trade(dict(state,id=trade_id), connection=connection)
    return trade_id


def create_trade(setup: dict[str, Any]) -> int | None:
    created = create_trades_from_candidates([setup],limit=1)
    return created[0] if created else None


def create_trades_from_candidates(candidates: list[dict[str, Any]], limit: int | None = None,
                                  completed_windows: list[dict[str, Any]] | None = None) -> list[int]:
    now = _now_utc()
    created = []
    from model_research import prepare_batch, record_batch
    research = prepare_batch(candidates, now)
    with get_db() as connection:
        # One account row serializes eligibility, reservations, and the daily limit.
        portfolio.lock_account(connection)
        portfolio.adopt_legacy_positions(connection)
        fresh = record_signals(connection,candidates,now)
        record_batch(connection, research, fresh)
        fresh.sort(key=lambda t:(t.get("combined_score",t["score"]),t["score"],t["R_multiple"]),reverse=True)
        cap = min(PREFERRED_TOP_SETUPS, limit if limit is not None else PREFERRED_TOP_SETUPS)
        for setup in fresh:
            if len(created) >= cap:
                break
            trade_id = _create_trade(connection,setup,now)
            if trade_id is not None:
                created.append(trade_id)
        for window in completed_windows or []:
            connection.execute("""
                INSERT INTO scan_progress(asset,asset_class,timeframe,bar_end) VALUES (%s,%s,%s,%s)
                ON CONFLICT(asset,asset_class,timeframe) DO UPDATE
                SET bar_end=GREATEST(scan_progress.bar_end,excluded.bar_end)
            """, (window["asset"],window["asset_class"],window["timeframe"],window["bar_end"]))
        portfolio.snapshot(connection,now)
    return created


def list_trades(
    status: str | None = None,
    strategy: str | None = None,
    asset: str | None = None,
    asset_classes: list[str] | None = None,
    direction: str | None = None,
) -> list[dict[str, Any]]:
    query = "SELECT * FROM trades WHERE 1=1"
    params: list[Any] = []
    if status:
        if status == "closed":
            query += " AND status != 'open'"
        else:
            query += " AND status = %s"
            params.append(status)
    if strategy:
        query += " AND strategy = %s"
        params.append(strategy)
    if asset:
        query += " AND asset = %s"
        params.append(asset)
    if asset_classes:
        placeholders = ", ".join("%s" for _ in asset_classes)
        query += f" AND asset_class IN ({placeholders})"
        params.extend(asset_classes)
    query += " ORDER BY date_opened DESC"
    trades = [_row_to_trade(row) for row in fetch_all(query, tuple(params))]
    if direction:
        normalized_direction = direction.strip().lower()
        trades = [trade for trade in trades if get_trade_direction(trade["strategy"]).lower() == normalized_direction]
    return trades


def get_trade(trade_id: int) -> dict[str, Any] | None:
    row = fetch_one("SELECT * FROM trades WHERE id = %s", (trade_id,))
    return _row_to_trade(row) if row else None


def _compute_result_r_for_trade(trade: dict[str, Any]) -> float | None:
    current_price = trade.get("current_price")
    entry_price = trade.get("entry_price")
    stop_loss = trade.get("stop_loss")
    target_price = trade.get("target_price")
    status = trade.get("status")
    direction = get_trade_direction(trade["strategy"])

    if None in (entry_price, stop_loss):
        return None

    if direction == "Long":
        risk = entry_price - stop_loss
    else:
        risk = stop_loss - entry_price
    if risk <= 0:
        return None

    runner_activated = bool(trade.get("runner_activated"))
    partial_taken = bool(trade.get("partial_taken"))
    partial_result_r = float(trade.get("partial_result_R") or 0)
    if (trade.get("metadata", {}).get("execution") and status in ("stopped", "target_hit", "closed")
            and current_price is not None):
        return net_result(trade, current_price)
    if status == "stopped":
        if runner_activated:
            return 0.0
        return partial_result_r if partial_taken else -1.0
    if status == "target_hit" and target_price is not None:
        if direction == "Long":
            final_leg_r = (target_price - entry_price) / risk
        else:
            final_leg_r = (entry_price - target_price) / risk
        if partial_taken:
            return round(partial_result_r + (0.5 * final_leg_r), 2)
        return round(final_leg_r, 2)
    if status == "closed" and current_price is not None:
        if direction == "Long":
            current_leg_r = (current_price - entry_price) / risk
        else:
            current_leg_r = (entry_price - current_price) / risk
        if partial_taken:
            return round(partial_result_r + (0.5 * current_leg_r), 2)
        return round(current_leg_r, 2)
    return None


def resolve_result_r(trade: dict[str, Any]) -> float | None:
    stored_result = trade.get("result_R")
    if stored_result is not None:
        return float(stored_result)
    return _compute_result_r_for_trade(trade)


def update_open_trades(asset_classes: list[str] | None = None) -> list[dict[str, Any]]:
    updated: list[dict[str, Any]] = []
    now = _now_utc()
    for original in list_trades(status="open", asset_classes=asset_classes):
        dataset = fetch_asset_data(original["asset"], original["asset_class"])
        trade = replay_bars(original, dataset.get("execution", []), now)
        if trade == original:
            continue
        # Persist fills, state and cursor together; a failed write is safe to replay.
        with get_db() as connection:
            portfolio.lock_account(connection)
            # Compare-and-swap under the same transaction as cash and outbox writes.
            row = connection.execute(
                """
                UPDATE trades SET entry_price=%s,R_multiple=%s,status=%s,current_price=%s,date_closed=%s,result_R=%s,
                    partial_taken=%s,partial_taken_at=%s,partial_price=%s,partial_result_R=%s,effective_stop_loss=%s,
                    runner_activated=%s,runner_activated_at=%s,metadata_json=%s
                WHERE id=%s AND status='open' AND metadata_json IS NOT DISTINCT FROM %s RETURNING id
                """,
                (trade["entry_price"],trade["R_multiple"],trade["status"],trade.get("current_price"),trade.get("date_closed"),
                 trade.get("result_R"),bool(trade.get("partial_taken")),trade.get("partial_taken_at"),trade.get("partial_price"),
                 trade.get("partial_result_R",0),trade.get("effective_stop_loss",trade["stop_loss"]),
                 bool(trade.get("runner_activated")),trade.get("runner_activated_at"),json.dumps(trade["metadata"]),
                 trade["id"],original.get("metadata_json")),
            ).fetchone()
            if row is None:
                continue
            portfolio.apply_events(connection,trade)
            if trade["status"] != "open" and trade["result_R"] is not None:
                notify_trade_closed(trade,trade["status"],trade["result_R"],connection=connection)
        if trade["status"] != "open":
            clear_learning_cache()
        updated.append(trade)
    with get_db() as connection:
        portfolio.snapshot(connection,now)
    return updated


def compute_unrealized_r(trade: dict[str, Any]) -> float | None:
    execution = trade.get("metadata", {}).get("execution")
    if execution:
        if execution.get("pending_entry") or trade.get("status") == "cancelled":
            return None
        if trade.get("status") != "open":
            return resolve_result_r(trade)
        if trade.get("current_price") is None:
            return None
        return round(net_result(trade, exit_fill(trade, trade["current_price"])), 4)
    current_price = trade.get("current_price")
    direction = get_trade_direction(trade["strategy"])
    if direction == "Long":
        risk = trade["entry_price"] - trade["stop_loss"]
        pnl = current_price - trade["entry_price"] if current_price is not None else None
    else:
        risk = trade["stop_loss"] - trade["entry_price"]
        pnl = trade["entry_price"] - current_price if current_price is not None else None
    if current_price is None or risk <= 0:
        return None
    current_leg_r = pnl / risk
    if trade.get("runner_activated"):
        return round(current_leg_r, 2)
    if trade.get("partial_taken"):
        return round(float(trade.get("partial_result_R") or 0) + (0.5 * current_leg_r), 2)
    return round(current_leg_r, 2)


def compute_notional_pnl_usd(trade: dict[str, Any], notional_usd: float = DISPLAY_NOTIONAL_USD) -> float | None:
    entry = trade.get("entry_price")
    stop = trade.get("stop_loss")
    if not entry or stop is None or trade.get("status") == "cancelled":
        return None
    result = (resolve_result_r(trade) if trade.get("status") != "open"
              else compute_unrealized_r(trade))
    if result is None:
        return None
    # result_R already weights partial fills; use the same ledger for dollars and R.
    return round(notional_usd * abs(entry - stop) / entry * result, 2)


def _result_label(result_r: float | None, status: str) -> str:
    if status == "cancelled":
        return "Cancelled"
    if status == "open":
        return "Open"
    if result_r is None:
        if status == "target_hit":
            return "Win"
        if status == "stopped":
            return "Loss"
        if status == "closed":
            return "Flat"
        return "Closed"
    if result_r > 0:
        return "Win"
    if result_r < 0:
        return "Loss"
    return "Flat"


def _result_tone(result_r: float | None, status: str) -> str:
    if status == "open":
        return "neutral"
    if result_r is None:
        if status == "target_hit":
            return "positive"
        if status == "stopped":
            return "negative"
        return "neutral"
    if result_r > 0:
        return "positive"
    if result_r < 0:
        return "negative"
    return "neutral"


def enrich_trade_for_display(trade: dict[str, Any]) -> dict[str, Any]:
    display = dict(trade)
    display["direction"] = get_trade_direction(trade["strategy"])
    display["unrealized_R"] = compute_unrealized_r(trade)
    display["pnl_usd_100"] = compute_notional_pnl_usd(trade)
    display["pnl_usd_100_label"] = f"{display['pnl_usd_100']:+.2f}" if display["pnl_usd_100"] is not None else "-"
    display["effective_stop_loss"] = trade.get("effective_stop_loss") or trade["stop_loss"]
    display["runner_activated"] = bool(trade.get("runner_activated"))
    display["partial_taken"] = bool(trade.get("partial_taken"))
    if display["runner_activated"]:
        display["partial_status"] = "Runner / breakeven active" if trade["status"] == "open" else "Runner triggered"
    elif display["partial_taken"] and trade["status"] == "open":
        display["partial_status"] = "Half size after 1R"
    elif display["partial_taken"]:
        display["partial_status"] = "Partial taken"
    else:
        display["partial_status"] = "Full size"
    display["partial_result_R"] = float(trade.get("partial_result_R") or 0)
    display["status_label"] = STATUS_LABELS.get(trade["status"], trade["status"].replace("_", " ").title())
    if trade.get("metadata", {}).get("execution", {}).get("pending_entry"):
        display["status_label"] = "Awaiting Next Bar Entry"
    if trade.get("metadata", {}).get("execution", {}).get("data_gap"):
        display["status_label"] = "Data Gap: Awaiting Missing Bars"
    result_r = resolve_result_r(trade)
    display["result_label"] = _result_label(result_r, trade["status"])
    display["result_tone"] = _result_tone(result_r, trade["status"])
    opened_at = _coerce_datetime(trade["date_opened"])
    display["days_open"] = (_now_utc() - opened_at).days
    return display


def trades_opened_today() -> int:
    day_start = _now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
    row = fetch_one(
        "SELECT COUNT(*) AS total FROM trades WHERE date_opened >= %s",
        (day_start,),
    )
    return int(row["total"]) if row else 0


def backfill_missing_trade_results() -> int:
    updated = 0
    legacy_closed = fetch_all(
        """
        SELECT * FROM trades
        WHERE status != 'open' AND result_R IS NULL
        ORDER BY id ASC
        """
    )
    for trade in (_row_to_trade(row) for row in legacy_closed):
        result_r = _compute_result_r_for_trade(trade)
        if result_r is None:
            continue
        execute(
            """
            UPDATE trades
            SET result_R = %s
            WHERE id = %s
            """,
            (result_r, trade["id"]),
        )
        updated += 1
    if updated:
        clear_learning_cache()
        LOGGER.info("Backfilled result_R for %s legacy closed trades", updated)
    return updated
