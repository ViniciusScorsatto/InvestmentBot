"""Point-in-time baseline labels and original signal features for model research."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import math

from config import STRATEGY_VERSION, SIM_FEE_BPS, SIM_SLIPPAGE_BPS, MIN_ENTRY_NET_R, STRATEGY_SETTINGS, strategy_max_trade_duration_days
from db import fetch_all
from execution import EXECUTION_VERSION
from market_bars import as_datetime
from trade_utils import get_correlation_group

FEATURES = ("rsi", "relative_volume", "ema_distance", "ema_gap", "atr_fraction", "stop_atr")


def feature_vector(setup):
    """Only original signal values; never adjusted entry fills or exit information."""
    try:
        components = setup.get("components") or {}
        features = components.get("features") or {}
        price, stop, atr = float(setup["entry_price"]), float(setup["stop_loss"]), float(features["atr"])
        rsi, volume = float(features["rsi"]), float(features["volume_ratio"])
        distance, gap = float(features["distance_ema20_pct"]), float(features["ema_gap_pct"])
        if price <= 0 or atr <= 0 or not 0 <= rsi <= 100 or volume < 0 or distance < 0:
            return None
        values = [rsi, math.log1p(volume), distance, gap, atr / price, abs(price - stop) / atr]
        return values if all(math.isfinite(x) for x in values) else None
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return None


def contract_definition():
    return {"strategy_version": STRATEGY_VERSION, "execution_version": EXECUTION_VERSION,
            "fee_bps": SIM_FEE_BPS, "slippage_bps": SIM_SLIPPAGE_BPS,
            "min_entry_net_r": MIN_ENTRY_NET_R, "breakeven_at_r": 1.0,
            "max_duration_days": {name: strategy_max_trade_duration_days(name) for name in STRATEGY_SETTINGS}}


def contract_matches(row):
    state = row.get("shadow_state") or {}
    if not isinstance(state, dict):
        return False
    metadata = state.get("metadata") or {}
    if not isinstance(metadata, dict):
        return False
    execution = metadata.get("execution") or {}
    if not isinstance(execution, dict):
        return False
    return (row.get("strategy_version") == STRATEGY_VERSION
            and metadata.get("strategy_version") == STRATEGY_VERSION
            and not state.get("experiment") and not execution.get("earnings_filter")
            and not execution.get("trailing_atr")
            and execution.get("version") == EXECUTION_VERSION
            and execution.get("fee_bps") == SIM_FEE_BPS
            and execution.get("slippage_bps") == SIM_SLIPPAGE_BPS
            and execution.get("min_entry_net_r") == MIN_ENTRY_NET_R
            and execution.get("breakeven_at_r", 1) == 1
            and execution.get("max_duration_days") == strategy_max_trade_duration_days(row.get("strategy", "")))


def read_signal_rows():
    # Never join experimental variants or filter on approval / portfolio selection.
    return fetch_all("""SELECT signal_id,strategy_version,asset,asset_class,strategy,timeframe,
                               observed_at,label_available_at,model_approved,selected_trade_id,
                               setup_json,shadow_state
                        FROM signals WHERE strategy_version=%s ORDER BY observed_at,signal_id""", (STRATEGY_VERSION,))


def dataset(rows, as_of=None):
    as_of = as_datetime(as_of or datetime.now(timezone.utc))
    counts = Counter()
    observations = []
    seen = set()
    for row in rows:
        identity = row.get("signal_id")
        if not identity or identity in seen:
            counts["duplicate_or_missing_id"] += 1
            continue
        seen.add(identity)
        if not contract_matches(row):
            counts["incompatible_contract"] += 1
            continue
        try:
            observed = as_datetime(row["observed_at"])
            if observed >= as_of:
                counts["not_yet_observed"] += 1
                continue
            state, setup = row["shadow_state"], row["setup_json"]
            champion_score = float(setup.get("combined_score", setup.get("score", 0)))
            if not math.isfinite(champion_score):
                raise ValueError("Invalid ranking score")
            available = as_datetime(row["label_available_at"]) if row.get("label_available_at") else None
            closed = as_datetime(state["date_closed"]) if state.get("date_closed") else None
            known = available is not None and observed < available < as_of and closed is not None and observed <= closed <= available
            value = state.get("result_R")
            try:
                target = float(value) if value is not None and state["status"] in ("stopped", "target_hit", "closed") else None
                if target is not None and not math.isfinite(target):
                    raise ValueError("Nonfinite label")
            except (TypeError, ValueError, OverflowError):
                # Keep the original candidate in its batch. A bad future outcome
                # must not remove a past candidate and change historical ranks.
                counts["invalid_label"] += 1
                target, known = None, False
            cancelled = state["status"] == "cancelled"
            resolved = known and (target is not None or cancelled)
            x = feature_vector(setup)
            observations.append({"signal_id": identity, "setup": setup, "observed_at": observed.isoformat(),
                                 "available_at": available.isoformat() if available else None,
                                 "closed_at": closed.isoformat() if closed else None,
                                 "target": target if known else None, "resolved": resolved,
                                 "cancelled": cancelled and known, "features": x,
                                 "selected": row.get("selected_trade_id") is not None,
                                 "champion_approved": bool(row["model_approved"]),
                                 "champion_score": champion_score,
                                 "cluster": observed.date().isoformat() + ":" + get_correlation_group(row["asset"], row["asset_class"])})
            counts["eligible_signals"] += 1
            counts["known_filled_labels"] += int(known and target is not None)
            counts["known_cancelled"] += int(known and cancelled)
            counts["unresolved"] += int(not resolved)
            counts["missing_features"] += int(x is None)
            counts["model_rejected"] += int(not row["model_approved"])
            counts["unselected"] += int(row.get("selected_trade_id") is None)
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            counts["invalid_record"] += 1
    observations.sort(key=lambda r: (r["observed_at"], r["signal_id"]))
    return observations, dict(counts)


def training_rows(observations, cutoff):
    cutoff = as_datetime(cutoff)
    return [row for row in observations if row["target"] is not None and row.get("available_at")
            and as_datetime(row["observed_at"]) < cutoff and as_datetime(row["available_at"]) < cutoff
            and row["closed_at"] and as_datetime(row["closed_at"]) < cutoff]
