from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from config import MAX_TRADE_DURATION_DAYS
from db import execute, fetch_all, fetch_one
from scanner import fetch_asset_data, select_best_setups
from telegram import notify_trade_closed, notify_new_trade


LOGGER = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _row_to_trade(row: Any) -> dict[str, Any]:
    trade = dict(row)
    metadata_json = trade.get("metadata_json")
    trade["metadata"] = json.loads(metadata_json) if metadata_json else {}
    return trade


def get_open_trade(asset: str, timeframe: str) -> dict[str, Any] | None:
    row = fetch_one(
        """
        SELECT * FROM trades
        WHERE asset = ? AND timeframe = ? AND status = 'open'
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
            setup_notes, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
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
            _now_iso(),
            setup.get("setup_notes", ""),
            json.dumps(setup.get("components", {})),
        ),
    )
    notify_new_trade(setup)
    LOGGER.info("Created trade %s %s %s", trade_id, setup["asset"], setup["strategy"])
    return trade_id


def create_trades_from_candidates(candidates: list[dict[str, Any]], limit: int | None = None) -> list[int]:
    created_ids: list[int] = []
    for setup in select_best_setups(candidates):
        if limit is not None and len(created_ids) >= limit:
            break
        trade_id = create_trade(setup)
        if trade_id:
            created_ids.append(trade_id)
    return created_ids


def list_trades(
    status: str | None = None,
    strategy: str | None = None,
    asset: str | None = None,
    asset_classes: list[str] | None = None,
) -> list[dict[str, Any]]:
    query = "SELECT * FROM trades WHERE 1=1"
    params: list[Any] = []
    if status:
        if status == "closed":
            query += " AND status != 'open'"
        else:
            query += " AND status = ?"
            params.append(status)
    if strategy:
        query += " AND strategy = ?"
        params.append(strategy)
    if asset:
        query += " AND asset = ?"
        params.append(asset)
    if asset_classes:
        placeholders = ", ".join("?" for _ in asset_classes)
        query += f" AND asset_class IN ({placeholders})"
        params.extend(asset_classes)
    query += " ORDER BY date_opened DESC"
    return [_row_to_trade(row) for row in fetch_all(query, tuple(params))]


def get_trade(trade_id: int) -> dict[str, Any] | None:
    row = fetch_one("SELECT * FROM trades WHERE id = ?", (trade_id,))
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
        SET status = ?, current_price = ?, date_closed = ?, result_R = ?
        WHERE id = ?
        """,
        (status, current_price, _now_iso(), round(result_r, 2), trade["id"]),
    )
    notify_trade_closed(trade, status, result_r)
    LOGGER.info("Closed trade %s with status %s", trade["id"], status)


def update_open_trades(asset_classes: list[str] | None = None) -> list[dict[str, Any]]:
    updated: list[dict[str, Any]] = []
    for trade in list_trades(status="open", asset_classes=asset_classes):
        current_price = _current_price_for_trade(trade)
        if current_price is None:
            continue

        execute("UPDATE trades SET current_price = ? WHERE id = ?", (current_price, trade["id"]))
        risk = trade["entry_price"] - trade["stop_loss"]
        if risk <= 0:
            continue

        days_open = (datetime.now(tz=timezone.utc) - datetime.fromisoformat(trade["date_opened"])).days
        if current_price <= trade["stop_loss"]:
            _close_trade(trade, "stopped", current_price, -1.0)
        elif current_price >= trade["target_price"]:
            result_r = (trade["target_price"] - trade["entry_price"]) / risk
            _close_trade(trade, "target_hit", current_price, result_r)
        elif days_open >= MAX_TRADE_DURATION_DAYS:
            result_r = (current_price - trade["entry_price"]) / risk
            _close_trade(trade, "closed", current_price, result_r)
        updated.append(get_trade(trade["id"]) or trade)
    return updated


def compute_unrealized_r(trade: dict[str, Any]) -> float | None:
    current_price = trade.get("current_price")
    risk = trade["entry_price"] - trade["stop_loss"]
    if current_price is None or risk <= 0:
        return None
    return round((current_price - trade["entry_price"]) / risk, 2)


def enrich_trade_for_display(trade: dict[str, Any]) -> dict[str, Any]:
    display = dict(trade)
    display["unrealized_R"] = compute_unrealized_r(trade)
    opened_at = datetime.fromisoformat(trade["date_opened"])
    display["days_open"] = (datetime.now(tz=timezone.utc) - opened_at).days
    return display


def trades_opened_today() -> int:
    day_start = datetime.now(tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    row = fetch_one(
        "SELECT COUNT(*) AS total FROM trades WHERE date_opened >= ?",
        (day_start.isoformat(),),
    )
    return int(row["total"]) if row else 0
