from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

from config import DATABASE_URL, MARKET_DATA_CACHE_RETENTION_DAYS


def _require_database_url() -> str:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is required. Configure Railway/Postgres before starting the app.")
    return DATABASE_URL


def get_connection() -> psycopg.Connection[Any]:
    return psycopg.connect(_require_database_url(), row_factory=dict_row)


@contextmanager
def get_db() -> Iterator[psycopg.Connection[Any]]:
    connection = get_connection()
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def initialize_db() -> None:
    with get_db() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id BIGSERIAL PRIMARY KEY,
                asset TEXT NOT NULL,
                asset_class TEXT NOT NULL,
                strategy TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                entry_price DOUBLE PRECISION NOT NULL,
                stop_loss DOUBLE PRECISION NOT NULL,
                target_price DOUBLE PRECISION NOT NULL,
                current_price DOUBLE PRECISION,
                R_multiple DOUBLE PRECISION NOT NULL,
                score INTEGER NOT NULL,
                date_opened TIMESTAMPTZ NOT NULL,
                status TEXT NOT NULL,
                date_closed TIMESTAMPTZ,
                result_R DOUBLE PRECISION,
                setup_notes TEXT,
                metadata_json TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_trades_status
            ON trades (status)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_trades_asset
            ON trades (asset, timeframe)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS market_data_cache (
                asset TEXT NOT NULL,
                asset_class TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                fetched_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (asset, asset_class)
            )
            """
        )


def ping_database() -> bool:
    try:
        row = fetch_one("SELECT 1 AS ok")
    except Exception:
        return False
    return bool(row and row.get("ok") == 1)


def fetch_all(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with get_db() as connection:
        cursor = connection.execute(query, params)
        return cursor.fetchall()


def fetch_one(query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    with get_db() as connection:
        cursor = connection.execute(query, params)
        return cursor.fetchone()


def execute(query: str, params: tuple[Any, ...] = ()) -> int | None:
    with get_db() as connection:
        cursor = connection.execute(query, params)
        if cursor.description:
            row = cursor.fetchone()
            if row and "id" in row:
                return int(row["id"])
        return None


def purge_old_market_cache(retention_days: int = MARKET_DATA_CACHE_RETENTION_DAYS) -> int:
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=retention_days)
    with get_db() as connection:
        cursor = connection.execute(
            """
            DELETE FROM market_data_cache
            WHERE fetched_at < %s
            """,
            (cutoff,),
        )
        return cursor.rowcount
