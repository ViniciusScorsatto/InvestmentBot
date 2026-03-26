from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from config import DATA_DIR, DB_PATH


def ensure_data_dir() -> None:
    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)


def get_connection() -> sqlite3.Connection:
    ensure_data_dir()
    connection = sqlite3.connect(DB_PATH, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return connection


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
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
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset TEXT NOT NULL,
                asset_class TEXT NOT NULL,
                strategy TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                entry_price REAL NOT NULL,
                stop_loss REAL NOT NULL,
                target_price REAL NOT NULL,
                current_price REAL,
                R_multiple REAL NOT NULL,
                score INTEGER NOT NULL,
                date_opened TEXT NOT NULL,
                status TEXT NOT NULL,
                date_closed TEXT,
                result_R REAL,
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
                fetched_at TEXT NOT NULL,
                PRIMARY KEY (asset, asset_class)
            )
            """
        )


def fetch_all(query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    with get_db() as connection:
        cursor = connection.execute(query, params)
        return cursor.fetchall()


def fetch_one(query: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    with get_db() as connection:
        cursor = connection.execute(query, params)
        return cursor.fetchone()


def execute(query: str, params: tuple[Any, ...] = ()) -> int:
    with get_db() as connection:
        cursor = connection.execute(query, params)
        return int(cursor.lastrowid)
