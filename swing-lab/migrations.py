"""Additive schema changes for idempotent signals, portfolio books, and an outbox."""
from config import PORTFOLIO_INITIAL_CASH


def upgrade(connection):
    # Serialize startup migrations across overlapping deployment processes.
    connection.execute("SELECT pg_advisory_xact_lock(79312001)")
    connection.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS signal_id TEXT")
    duplicates = connection.execute("""
        SELECT asset, timeframe, array_agg(id ORDER BY id) AS ids FROM trades
        WHERE status = 'open' GROUP BY asset, timeframe HAVING count(*) > 1
    """).fetchall()
    if duplicates:
        raise RuntimeError(f"Active-position duplicates must be reconciled before migration: {duplicates}")
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_active_position ON trades(asset, timeframe) WHERE status = 'open'")
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_trade_signal ON trades(signal_id) WHERE signal_id IS NOT NULL")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS scan_progress (
            asset TEXT NOT NULL, asset_class TEXT NOT NULL, timeframe TEXT NOT NULL,
            bar_end TIMESTAMPTZ NOT NULL, PRIMARY KEY(asset, asset_class, timeframe)
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            signal_id TEXT PRIMARY KEY, strategy_version TEXT NOT NULL,
            asset TEXT NOT NULL, asset_class TEXT NOT NULL, strategy TEXT NOT NULL,
            timeframe TEXT NOT NULL, bar_end TIMESTAMPTZ NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL, model_approved BOOLEAN NOT NULL,
            setup_json JSONB NOT NULL, shadow_state JSONB NOT NULL,
            shadow_status TEXT NOT NULL DEFAULT 'open', selected_trade_id BIGINT REFERENCES trades(id)
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_signals_open ON signals(observed_at) WHERE shadow_status = 'open'")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_signals_version ON signals(strategy_version, observed_at)")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS portfolio_accounts (
            id INTEGER PRIMARY KEY CHECK(id=1), initial_cash NUMERIC(24,8) NOT NULL,
            cash NUMERIC(24,8) NOT NULL, reserved_cash NUMERIC(24,8) NOT NULL DEFAULT 0,
            peak_equity NUMERIC(24,8) NOT NULL, max_drawdown_pct DOUBLE PRECISION NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    connection.execute("""
        INSERT INTO portfolio_accounts(id, initial_cash, cash, peak_equity)
        VALUES (1, %s, %s, %s) ON CONFLICT DO NOTHING
    """, (PORTFOLIO_INITIAL_CASH,) * 3)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS portfolio_positions (
            trade_id BIGINT PRIMARY KEY REFERENCES trades(id), correlation_group TEXT NOT NULL,
            planned_quantity DOUBLE PRECISION NOT NULL, quantity DOUBLE PRECISION NOT NULL DEFAULT 0,
            remaining_quantity DOUBLE PRECISION NOT NULL DEFAULT 0, entry_fill DOUBLE PRECISION,
            reserved_cash NUMERIC(24,8) NOT NULL DEFAULT 0, risk_budget NUMERIC(24,8) NOT NULL,
            applied_events INTEGER NOT NULL DEFAULT 0, legacy BOOLEAN NOT NULL DEFAULT FALSE
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS portfolio_ledger (
            id BIGSERIAL PRIMARY KEY, event_key TEXT NOT NULL UNIQUE,
            trade_id BIGINT REFERENCES trades(id), occurred_at TIMESTAMPTZ NOT NULL,
            kind TEXT NOT NULL, cash_delta NUMERIC(24,8) NOT NULL,
            quantity_delta DOUBLE PRECISION NOT NULL, price DOUBLE PRECISION
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS portfolio_snapshots (
            id BIGSERIAL PRIMARY KEY, at TIMESTAMPTZ NOT NULL,
            cash NUMERIC(24,8) NOT NULL, equity NUMERIC(24,8) NOT NULL,
            reserved_cash NUMERIC(24,8) NOT NULL, gross_exposure NUMERIC(24,8) NOT NULL,
            risk_exposure NUMERIC(24,8) NOT NULL, drawdown_pct DOUBLE PRECISION NOT NULL,
            stale_positions INTEGER NOT NULL DEFAULT 0
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_at ON portfolio_snapshots(at DESC)")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS notification_outbox (
            id BIGSERIAL PRIMARY KEY, event_key TEXT NOT NULL UNIQUE, message TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(), sent_at TIMESTAMPTZ,
            attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            lease_token TEXT, lease_until TIMESTAMPTZ, last_error TEXT
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_outbox_pending ON notification_outbox(next_attempt_at) WHERE sent_at IS NULL")

    connection.execute("""
        CREATE TABLE IF NOT EXISTS signal_experiments (
            signal_id TEXT NOT NULL REFERENCES signals(signal_id), variant TEXT NOT NULL,
            definition JSONB NOT NULL, state JSONB NOT NULL,
            status TEXT NOT NULL, PRIMARY KEY(signal_id, variant)
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_experiments_active ON signal_experiments(signal_id) WHERE status IN ('open','monitoring')")
