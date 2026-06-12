from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from config import strategy_max_trade_duration_days
from db import execute, fetch_all, fetch_one
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


def create_trade(setup: dict[str, Any]) -> int | None:
    if get_open_trade(setup["asset"], setup["timeframe"]):
        return None
    trade_id = execute(
        """
        INSERT INTO trades (
            asset, asset_class, strategy, timeframe,
            entry_price, stop_loss, target_price, current_price,
            R_multiple, score, date_opened, status,
            setup_notes, metadata_json, effective_stop_loss,
            partial_taken, partial_result_R, runner_activated
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'open', %s, %s, %s, false, 0, false)
        RETURNING id
        """,
        (
            setup["asset"],
            setup["asset_class"],
            setup["strategy"],
            setup["timeframe"],
            setup["entry_price"],
            setup["stop_loss"],
            setup["target_price"],
            setup["entry_price"],
            setup["R_multiple"],
            setup["score"],
            _now_utc(),
            setup.get("setup_notes", ""),
            json.dumps(setup.get("components", {})),
            setup["stop_loss"],
        ),
    )
    notify_new_trade(setup)
    LOGGER.info("Created trade %s %s %s", trade_id, setup["asset"], setup["strategy"])
    return trade_id


def create_trades_from_candidates(candidates: list[dict[str, Any]], limit: int | None = None) -> list[int]:
    created_ids: list[int] = []
    used_groups = _correlation_groups_opened_today()
    eligible_candidates = [
        setup
        for setup in candidates
        if (setup.get("correlation_group") or get_correlation_group(setup["asset"], setup["asset_class"])) not in used_groups
    ]
    for setup in select_best_setups(eligible_candidates):
        if limit is not None and len(created_ids) >= limit:
            break
        group = setup.get("correlation_group") or get_correlation_group(setup["asset"], setup["asset_class"])
        trade_id = create_trade(setup)
        if trade_id:
            created_ids.append(trade_id)
            used_groups.add(group)
    return created_ids


def _correlation_groups_opened_today() -> set[str]:
    day_start = _now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
    rows = fetch_all(
        """
        SELECT asset, asset_class
        FROM trades
        WHERE date_opened >= %s
        """,
        (day_start,),
    )
    return {get_correlation_group(row["asset"], row["asset_class"]) for row in rows}


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


def _current_price_for_trade(trade: dict[str, Any]) -> float | None:
    dataset = fetch_asset_data(trade["asset"], trade["asset_class"])
    bars = dataset["4h"] if trade["timeframe"] == "4h" else dataset["1d"]
    if not bars:
        return None
    return float(bars[-1]["close"])


def _close_trade(trade: dict[str, Any], status: str, current_price: float, result_r: float) -> None:
    execute(
        """
        UPDATE trades
        SET status = %s, current_price = %s, date_closed = %s, result_R = %s
        WHERE id = %s
        """,
        (status, current_price, _now_utc(), round(result_r, 2), trade["id"]),
    )
    clear_learning_cache()
    notify_trade_closed(trade, status, result_r)
    LOGGER.info("Closed trade %s with status %s", trade["id"], status)


def _mark_legacy_partial_taken(trade: dict[str, Any], current_price: float) -> dict[str, Any]:
    execute(
        """
        UPDATE trades
        SET partial_taken = true,
            partial_taken_at = %s,
            partial_price = %s,
            partial_result_R = 0.5,
            effective_stop_loss = entry_price,
            current_price = %s
        WHERE id = %s
        """,
        (_now_utc(), current_price, current_price, trade["id"]),
    )
    updated = get_trade(trade["id"]) or trade
    LOGGER.info("Partial profit taken for trade %s at %s", trade["id"], current_price)
    return updated


def _mark_runner_activated(trade: dict[str, Any], current_price: float) -> dict[str, Any]:
    execute(
        """
        UPDATE trades
        SET runner_activated = true,
            runner_activated_at = %s,
            effective_stop_loss = entry_price,
            current_price = %s
        WHERE id = %s
        """,
        (_now_utc(), current_price, trade["id"]),
    )
    updated = get_trade(trade["id"]) or trade
    LOGGER.info("Runner mode activated for trade %s at %s", trade["id"], current_price)
    return updated


def _is_breakout_runner_strategy(strategy: str) -> bool:
    return strategy == "Breakout"


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
    for trade in list_trades(status="open", asset_classes=asset_classes):
        current_price = _current_price_for_trade(trade)
        if current_price is None:
            continue

        execute("UPDATE trades SET current_price = %s WHERE id = %s", (current_price, trade["id"]))
        direction = get_trade_direction(trade["strategy"])
        if direction == "Long":
            risk = trade["entry_price"] - trade["stop_loss"]
            partial_trigger = trade["entry_price"] + risk
        else:
            risk = trade["stop_loss"] - trade["entry_price"]
            partial_trigger = trade["entry_price"] - risk
        if risk <= 0:
            continue

        runner_activated = bool(trade.get("runner_activated"))
        partial_taken = bool(trade.get("partial_taken"))
        partial_result_r = float(trade.get("partial_result_R") or 0)
        effective_stop = trade.get("effective_stop_loss") or trade["stop_loss"]
        days_open = (_now_utc() - _coerce_datetime(trade["date_opened"])).days
        max_duration_days = strategy_max_trade_duration_days(trade["strategy"])
        if direction == "Long":
            if (
                _is_breakout_runner_strategy(trade["strategy"])
                and not runner_activated
                and not partial_taken
                and current_price >= partial_trigger
            ):
                trade = _mark_runner_activated(trade, current_price)
                runner_activated = True
                effective_stop = trade["entry_price"]
            elif not _is_breakout_runner_strategy(trade["strategy"]) and not partial_taken and current_price >= partial_trigger:
                trade = _mark_legacy_partial_taken(trade, current_price)
                partial_taken = True
                partial_result_r = 0.5
                effective_stop = trade["entry_price"]

            if current_price <= effective_stop:
                if runner_activated:
                    result_r = 0.0
                else:
                    result_r = partial_result_r if partial_taken else -1.0
                _close_trade(trade, "stopped", current_price, result_r)
            elif current_price >= trade["target_price"]:
                final_leg_r = (trade["target_price"] - trade["entry_price"]) / risk
                result_r = partial_result_r + (0.5 * final_leg_r) if partial_taken else final_leg_r
                _close_trade(trade, "target_hit", current_price, result_r)
            elif days_open >= max_duration_days:
                current_leg_r = (current_price - trade["entry_price"]) / risk
                result_r = partial_result_r + (0.5 * current_leg_r) if partial_taken else current_leg_r
                _close_trade(trade, "closed", current_price, result_r)
        else:
            if not partial_taken and current_price <= partial_trigger:
                trade = _mark_legacy_partial_taken(trade, current_price)
                partial_taken = True
                partial_result_r = 0.5
                effective_stop = trade["entry_price"]

            if current_price >= effective_stop:
                result_r = partial_result_r if partial_taken else -1.0
                _close_trade(trade, "stopped", current_price, result_r)
            elif current_price <= trade["target_price"]:
                final_leg_r = (trade["entry_price"] - trade["target_price"]) / risk
                result_r = partial_result_r + (0.5 * final_leg_r) if partial_taken else final_leg_r
                _close_trade(trade, "target_hit", current_price, result_r)
            elif days_open >= max_duration_days:
                current_leg_r = (trade["entry_price"] - current_price) / risk
                result_r = partial_result_r + (0.5 * current_leg_r) if partial_taken else current_leg_r
                _close_trade(trade, "closed", current_price, result_r)
        updated.append(get_trade(trade["id"]) or trade)
    return updated


def compute_unrealized_r(trade: dict[str, Any]) -> float | None:
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
    current_price = trade.get("current_price")
    entry_price = trade.get("entry_price")
    if current_price is None or entry_price in (None, 0):
        return None

    direction = get_trade_direction(trade["strategy"])
    if direction == "Long":
        pnl_fraction = (current_price - entry_price) / entry_price
    else:
        pnl_fraction = (entry_price - current_price) / entry_price
    if trade.get("runner_activated"):
        return round(notional_usd * pnl_fraction, 2)
    if trade.get("partial_taken"):
        partial_r = float(trade.get("partial_result_R") or 0)
        stop_loss = trade.get("stop_loss")
        if stop_loss is None:
            return round(notional_usd * pnl_fraction, 2)
        original_risk_fraction = abs(entry_price - stop_loss) / entry_price
        realized_partial = notional_usd * 0.5 * partial_r * original_risk_fraction
        remaining_pnl = notional_usd * 0.5 * pnl_fraction
        return round(realized_partial + remaining_pnl, 2)
    return round(notional_usd * pnl_fraction, 2)


def _result_label(result_r: float | None, status: str) -> str:
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
