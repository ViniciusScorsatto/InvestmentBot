"""Consistent, read-only analysis archive; no market fetches or model refitting."""
from __future__ import annotations

import csv
from datetime import date, datetime, timezone
from decimal import Decimal
import io
import json
import math
import tempfile
import zipfile

import config
from db import get_db
from feature_model import DEFINITION
from model_dataset import contract_definition

# Explicit allowlist: never export credentials, notification messages or market caches.
TABLES = {
    "signals": "signal_id strategy_version asset asset_class strategy timeframe bar_end observed_at label_available_at model_approved selected_trade_id shadow_status setup_json shadow_state",
    "model_predictions": "signal_id model_version snapshot_id batch_id predicted_at champion_rank challenger_rank champion_selected challenger_selected prediction",
    "model_snapshots": "snapshot_id model_version strategy_version training_cutoff created_at artifact",
    "trades": "id signal_id asset asset_class strategy timeframe entry_price stop_loss target_price current_price r_multiple score date_opened status date_closed result_r partial_taken partial_taken_at partial_price partial_result_r effective_stop_loss runner_activated runner_activated_at setup_notes metadata_json",
    "portfolio_accounts": "id initial_cash cash reserved_cash peak_equity max_drawdown_pct created_at",
    "portfolio_positions": "trade_id correlation_group planned_quantity quantity remaining_quantity entry_fill reserved_cash risk_budget applied_events legacy",
    "portfolio_ledger": "id event_key trade_id occurred_at kind cash_delta quantity_delta price",
    "portfolio_snapshots": "id at cash equity reserved_cash gross_exposure risk_exposure drawdown_pct stale_positions",
    "signal_experiments": "signal_id variant definition status state",
}
KEYS = {
    "signals": ["signal_id"], "model_predictions": ["signal_id", "model_version"],
    "model_snapshots": ["snapshot_id"], "trades": ["id"], "portfolio_accounts": ["id"],
    "portfolio_positions": ["trade_id"], "portfolio_ledger": ["id"],
    "portfolio_snapshots": ["id"], "signal_experiments": ["signal_id", "variant"],
}
OUTCOME_COLUMNS = ["outcome_state", "net_result_R", "opportunity_result_R", "outcome_closed_at", "pending_entry",
                   "execution_version", "fee_bps", "slippage_bps", "entry_at", "entry_fee_R", "cancel_reason", "execution_events_json"]
EXTRA = {
    "signals": ["portfolio_selected", "signal_entry_price", "signal_stop_loss", "signal_target_price", "rule_score", "combined_score",
                "rsi", "volume_ratio", "distance_ema20_pct", "ema_gap_pct", "atr"] + OUTCOME_COLUMNS,
    "trades": ["strategy_version"] + OUTCOME_COLUMNS,
    "model_predictions": ["training_cutoff", "prediction_basis", "expected_net_R", "conditional_net_R", "entry_probability",
                          "entry_status", "feature_status", "feature_weight", "ranking_ready"],
    "signal_experiments": OUTCOME_COLUMNS,
}
SETTINGS = ("STRATEGY_VERSION", "LAST_STRATEGY_CHANGE_AT", "STRATEGY_SETTINGS", "WATCHLIST", "MIN_SCORE", "MIN_R_MULTIPLE",
            "MIN_ENTRY_NET_R", "PREFERRED_TOP_SETUPS", "MAX_TRADES_PER_DAY", "LEARNING_MODEL_ENABLED", "LEARNING_MODEL_WEIGHT",
            "LEARNING_MODEL_MIN_SAMPLE", "LEARNING_MODEL_BLOCK_MIN_SAMPLE", "LEARNING_MODEL_MIN_SCORE", "LEARNING_MODEL_MIN_WEEKS",
            "PORTFOLIO_INITIAL_CASH", "PORTFOLIO_RISK_PER_TRADE", "PORTFOLIO_MAX_RISK", "PORTFOLIO_MAX_GROSS",
            "PORTFOLIO_MAX_POSITION", "PORTFOLIO_MAX_GROUP", "PORTFOLIO_MAX_GROUP_POSITIONS")

GUIDE = """InvestmentBot analysis report — schema version 1

SCOPE
All recorded strategy/model versions and all statuses in one read-only PostgreSQL
REPEATABLE READ snapshot. No date filtering, row limits, model refits or market
fetches. Analytics date filters in metadata.json are context only. This prevents
partial model-comparison batches and preserves portfolio cash history. This is a
snapshot of currently persisted state, not a historical as-of reconstruction.

FILES / JOINS
signals.csv: one original qualifying baseline signal per signal_id, including
rejected and unselected signals. setup_json is the immutable original feature
snapshot; shadow_state is its latest simulated baseline outcome.
model_predictions.csv: frozen decisions; unique (signal_id, model_version). Join
signals on signal_id and model_snapshots on snapshot_id. A blank snapshot_id means
model fitting was unavailable. Group by model_version AND batch_id for comparisons.
model_snapshots.csv: complete frozen training artifacts, definitions and cutoffs.
trades.csv: funded simulated trades; id joins portfolio_positions.trade_id and
portfolio_ledger.trade_id. signal_id joins signals where available. Legacy trades
can have no signal_id. Do not add trade results to signal results: they overlap.
portfolio_accounts.csv: current persisted balances, not recomputed equity.
portfolio_positions.csv: quantities/reservations, including closed positions.
portfolio_ledger.csv: all recorded cash/quantity changes; event_key is unique.
portfolio_snapshots.csv: sampled equity history across concurrent positions.
signal_experiments.csv: separate outcomes keyed by (signal_id, variant). Never
combine these variants with baseline training labels.

METRICS / MISSING DATA
net_result_R is a completed filled outcome after the frozen execution costs.
One R is the simulated entry-to-initial-stop price risk. Legacy cost rules can
differ; inspect frozen metadata/execution_version. Slippage is embedded in fills;
fees affect net R and ledger cash deltas. execution_events_json preserves fills,
sizes and timestamps; fee_bps/slippage_bps are per-record frozen assumptions.
opportunity_result_R equals net_result_R for a resolved fill and zero for a
resolved cancelled entry. Pending, unavailable and invalid outcomes stay blank,
never zero losses. Pending entries and filled open positions are distinguished.
Signal outcomes also require a persisted label_available_at before snapshot time.
Raw state JSON is retained even when an outcome is not yet eligible as a label.
Win rate: filled completed wins (net_result_R > 0) / filled completed outcomes;
zero-R fills remain in the denominator, cancelled and pending signals do not.
Mean net R: average over completed fills. Mean opportunity R: include resolved
cancellations as zero. Compare models only on fully resolved entire batches and
separate ranking_ready=false fallback decisions. Do not treat shadow top-k R as
portfolio returns: it omits funding, exposure and correlation constraints.
Portfolio returns/drawdown must use the equity history and capital/cash flows;
sampled drawdown does not capture intrabar lows. Legacy adoption may lack older
fills/history. Stale marks are indicated by stale_positions in equity snapshots.
expected_net_R is defined by prediction_basis (v2: per_signal_opportunity;
older versions may be conditional_on_entry). Never mix these without conversion.
Training and calibration must use features and labels available before each
training cutoff. Preserve signal_id, observed_at and label_available_at.

FORMAT
CSV is UTF-8, with headers even when empty. Blank = null/missing; booleans are
true/false; numbers are unrounded. SQL timestamps use UTC ISO 8601. Nested JSON
preserves original timestamps and keys. Financial decimals retain decimal text.
Text cells starting with =, +, -, @, tab, CR or LF (also after whitespace) are
prefixed with an apostrophe for spreadsheet safety. Numeric negatives are not
escaped. Machine readers may remove exactly that leading apostrophe when present
before a dangerous text prefix. JSON columns contain full nested detail.
metadata.json lists columns, keys, row counts, export time and current settings.
Current settings are not historical settings; use frozen artifacts/contracts.
"""


def json_default(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, (date, Decimal)):
        return str(value)
    raise TypeError(type(value).__name__)


def cell(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=json_default, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if isinstance(value, (datetime, date, Decimal)):
        return json_default(value)
    if isinstance(value, str) and (value.startswith(("\t", "\r", "\n")) or value.lstrip().startswith(("=", "+", "-", "@"))):
        return "'" + value
    return value


def object_value(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def outcome(state, snapshot_at, available_at=None, require_arrival=False):
    settings = object_value(object_value(state.get("metadata")).get("execution"))
    status = state.get("status")
    value = state.get("result_R", state.get("result_r"))
    valid = isinstance(value, (float, int, Decimal)) and math.isfinite(value)
    arrived = not require_arrival or (available_at is not None and available_at < snapshot_at)
    closed = state.get("date_closed")
    try:
        closed_at = closed if isinstance(closed, datetime) else datetime.fromisoformat(closed.replace("Z", "+00:00"))
        if closed_at.tzinfo is None:
            closed_at = closed_at.replace(tzinfo=timezone.utc)
        valid_closed = closed_at <= snapshot_at and (not require_arrival or available_at is None or closed_at <= available_at)
    except (AttributeError, TypeError, ValueError):
        valid_closed = False
    result = None
    opportunity = None
    if status == "open":
        label = "pending_entry" if settings.get("pending_entry") else "open_filled"
    elif status == "cancelled" and valid_closed:
        label = "cancelled" if arrived else "label_pending"
        opportunity = 0 if arrived else None
    elif status in ("closed", "stopped", "target_hit") and valid_closed and valid:
        label = "resolved_filled" if arrived else "label_pending"
        result = value if arrived else None
        opportunity = result
    else:
        label = "unavailable_or_invalid"
    return dict(outcome_state=label, net_result_R=result, opportunity_result_R=opportunity,
                outcome_closed_at=closed, pending_entry=settings.get("pending_entry"),
                execution_version=settings.get("version"), fee_bps=settings.get("fee_bps"),
                slippage_bps=settings.get("slippage_bps"), entry_at=settings.get("entry_at"),
                entry_fee_R=settings.get("entry_fee_r"), cancel_reason=settings.get("cancel_reason"),
                execution_events_json=settings.get("events"))


def export_row(table, row, snapshot_at):
    row = dict(row)
    if table == "signals":
        setup = object_value(row["setup_json"])
        features = object_value(object_value(setup.get("components")).get("features"))
        row.update({key: features.get(key) for key in ("rsi", "volume_ratio", "distance_ema20_pct", "ema_gap_pct", "atr")})
        row.update(portfolio_selected=row["selected_trade_id"] is not None,
                   signal_entry_price=setup.get("entry_price"), signal_stop_loss=setup.get("stop_loss"),
                   signal_target_price=setup.get("target_price"), rule_score=setup.get("score"), combined_score=setup.get("combined_score"))
        row.update(outcome(object_value(row["shadow_state"]), snapshot_at, row["label_available_at"], True))
    elif table == "trades":
        metadata = object_value(row["metadata_json"])
        row["strategy_version"] = metadata.get("strategy_version")
        row.update(outcome(dict(row, metadata=metadata), snapshot_at))
    elif table == "signal_experiments":
        row.update(outcome(object_value(row["state"]), snapshot_at))
    elif table == "model_predictions":
        prediction = object_value(row["prediction"])
        row.update({key: prediction.get(key) for key in EXTRA[table]})
    return row


def export_context(start_date=None, end_date=None, current_strategy_only=False):
    """Validate metadata inputs; these intentionally do not restrict archive rows."""
    parsed = [date.fromisoformat(value) if value else None for value in (start_date, end_date)]
    if all(parsed) and parsed[0] > parsed[1]:
        raise ValueError("Start date must not follow end date")
    return {"start_date": parsed[0].isoformat() if parsed[0] else None,
            "end_date": parsed[1].isoformat() if parsed[1] else None,
            "current_strategy_only": current_strategy_only, "applied_to_export_rows": False}


def build_report(context):
    """Spool large archives to disk; server cursors keep table reads bounded."""
    from psycopg import sql
    output = tempfile.SpooledTemporaryFile(max_size=8*1024*1024, mode="w+b")
    try:
        with get_db() as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            snapshot_at = connection.execute("SELECT transaction_timestamp() AS at").fetchone()["at"]
            metadata = {"schema_version": 1, "exported_at": snapshot_at, "timezone": "UTC", "scope": "all_recorded_history",
                        "consistency": "PostgreSQL REPEATABLE READ, READ ONLY", "analytics_filter_context": context,
                        "app_version": config.APP_VERSION, "current_settings": {key: getattr(config, key) for key in SETTINGS},
                        "current_execution_contract": contract_definition(), "current_challenger_definition": DEFINITION,
                        "files": {}}
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                for table, columns in TABLES.items():
                    names = columns.split()
                    headers = names + EXTRA.get(table, [])
                    query = sql.SQL("SELECT {} FROM {} ORDER BY {}").format(
                        sql.SQL(",").join(map(sql.Identifier, names)), sql.Identifier(table),
                        sql.SQL(",").join(map(sql.Identifier, KEYS[table])))
                    count = 0
                    with connection.cursor(name="report_"+table) as cursor:
                        cursor.execute(query)
                        with archive.open(table+".csv", "w", force_zip64=True) as member:
                            with io.TextIOWrapper(member, encoding="utf-8", newline="") as stream:
                                writer = csv.writer(stream)
                                writer.writerow(headers)
                                for record in cursor:
                                    row = export_row(table, record, snapshot_at)
                                    writer.writerow([cell(row.get(key)) for key in headers])
                                    count += 1
                    metadata["files"][table+".csv"] = {"rows": count, "columns": headers, "primary_key": KEYS[table]}
                archive.writestr("README.txt", GUIDE)
                archive.writestr("metadata.json", json.dumps(metadata, default=json_default, indent=2, allow_nan=False))
        size = output.tell()
        output.seek(0)
        return output, "investmentbot-analysis-"+snapshot_at.strftime("%Y%m%dT%H%M%SZ")+".zip", size
    except BaseException:
        output.close()
        raise


def report_chunks(output):
    try:
        while chunk := output.read(64*1024):
            yield chunk
    finally:
        output.close()
