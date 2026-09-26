"""Frozen, one-change-at-a-time paper experiments. Never allocate portfolio cash."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import math

from config import STRATEGY_VERSION
from db import fetch_all, get_db
from evaluation import outcome_summary
from execution import replay_bars, side
from market_bars import as_datetime, next_hour_start

# Changing a definition requires a new experiment/strategy version, never rewriting rows.
DEFINITIONS = {
    "baseline": {"label": "Baseline", "version": 1},
    "atr_stop": {"label": "Stop with 0.5 ATR buffer", "version": 1, "buffer_atr": 0.5},
    "delayed_breakeven": {"label": "Breakeven at 1.5R", "version": 1, "breakeven_at_r": 1.5},
    "atr_trailing": {"label": "Trail by 1 signal ATR after 1R", "version": 1, "trail_atr": 1.0},
    "earnings_blackout": {"label": "Avoid earnings within 2 calendar days", "version": 1},
}


def variant_states(baseline):
    for name, definition in DEFINITIONS.items():
        state = deepcopy(baseline)
        settings = state["metadata"]["execution"]
        status = "open"
        if name in ("atr_stop", "atr_trailing"):
            atr = state["metadata"].get("features", {}).get("atr")
            if not isinstance(atr, (int, float)) or not math.isfinite(atr) or atr <= 0:
                status = "unavailable"
                state["experiment_reason"] = "signal_atr_missing"
            elif name == "atr_stop":
                state["stop_loss"] -= side(state) * definition["buffer_atr"] * atr
                if state["stop_loss"] <= 0:
                    status = "unavailable"
                    state["experiment_reason"] = "invalid_atr_stop"
                state["effective_stop_loss"] = state["stop_loss"]
                state["R_multiple"] = side(state) * (state["target_price"] - state["entry_price"]) / abs(state["entry_price"] - state["stop_loss"])
            else:
                settings["trailing_atr"] = definition["trail_atr"] * atr
        elif name == "delayed_breakeven":
            settings["breakeven_at_r"] = definition["breakeven_at_r"]
        elif name == "earnings_blackout":
            earnings = state["metadata"].get("earnings", {})
            if state["asset_class"] != "stock":
                status = "unavailable"
                state["experiment_reason"] = "not_an_individual_stock"
            elif earnings.get("status") == "blocked":
                status = "skipped"
                state["experiment_reason"] = "earnings_blackout"
            elif earnings.get("status") == "clear":
                settings["earnings_filter"] = deepcopy(earnings)
            else:
                status = "unavailable"
                state["experiment_reason"] = earnings.get("reason", "earnings_unknown")
        if status != "open":
            state["status"] = status
        state["experiment"] = name
        yield name, definition, state, status


def record_experiments(connection, baseline):
    for name, definition, state, status in variant_states(baseline):
        connection.execute("""
            INSERT INTO signal_experiments(signal_id,variant,definition,state,status)
            VALUES (%s,%s,%s::jsonb,%s::jsonb,%s) ON CONFLICT DO NOTHING
        """, (baseline["signal_id"], name, json.dumps(definition), json.dumps(state), status))


def follow_stopped_target(original, bars, now):
    """Observe later completed bars until the original expiry; never alter trade P&L.

    Exclude the stopping bar because its high/low ordering is ambiguous. Missing
    bars keep the observation unresolved, including after wall-clock expiry.
    """
    state = deepcopy(original)
    if state["status"] != "stopped":
        return state
    execution = state["metadata"]["execution"]
    followup = state.setdefault("stop_followup", {
        "cursor": execution["cursor"], "target_revisited": False, "complete": False,
        "expiry": (as_datetime(execution.get("entry_at", state["date_opened"]))
                   + timedelta(days=execution["max_duration_days"])).isoformat()})
    if followup["complete"]:
        return state
    cursor, expiry = as_datetime(followup["cursor"]), as_datetime(followup["expiry"])
    if cursor >= expiry:
        followup["complete"] = True
        return state
    for bar in sorted(bars, key=lambda b: as_datetime(b["timestamp"])):
        start, end = as_datetime(bar["timestamp"]), as_datetime(bar["end_timestamp"])
        if start < cursor or end > now or end <= start:
            continue
        if start > next_hour_start(cursor, state["asset_class"]):
            followup["data_gap"] = True
            break
        followup.pop("data_gap", None)
        if start >= expiry:
            followup["complete"] = True
            break
        # The end timestamp at expiry is eligible, consistent with execution.
        if end <= expiry:
            favorable = bar["high"] if side(state) == 1 else bar["low"]
            if side(state) * (favorable - state["target_price"]) >= 0:
                followup.update(target_revisited=True, complete=True, reached_at=end.isoformat())
        elif side(state) * (bar["open"] - state["target_price"]) >= 0:
            followup.update(target_revisited=True, complete=True, reached_at=start.isoformat())
        followup["cursor"] = end.isoformat()
        cursor = end
        if end >= expiry:
            followup["complete"] = True
        if followup["complete"]:
            break
    return state


def advance_state(old, bars, now):
    state = replay_bars(old, bars, now)
    state = follow_stopped_target(state, bars, now)
    status = state["status"]
    if status == "stopped" and not state["stop_followup"]["complete"]:
        status = "monitoring"
    return state, status


def update_experiments(now=None, datasets=None):
    from scanner import fetch_asset_data
    now = now or datetime.now(timezone.utc)
    datasets = datasets if datasets is not None else {}
    rows = fetch_all("""SELECT e.*,s.asset,s.asset_class FROM signal_experiments e
                        JOIN signals s USING(signal_id) WHERE e.status IN ('open','monitoring')""")
    count = 0
    for row in rows:
        key = (row["asset"], row["asset_class"])
        if key not in datasets:
            datasets[key] = fetch_asset_data(*key)
        old = row["state"]
        state, status = advance_state(old, datasets[key].get("execution", []), now)
        if state == old and status == row["status"]:
            continue
        with get_db() as connection:
            result = connection.execute("""
                UPDATE signal_experiments SET state=%s::jsonb,status=%s
                WHERE signal_id=%s AND variant=%s AND state=%s::jsonb RETURNING signal_id
            """, (json.dumps(state), status, row["signal_id"], row["variant"], json.dumps(old))).fetchone()
        count += bool(result)
    return count


def resolved(state):
    return state["status"] in ("skipped", "cancelled") or state.get("result_R") is not None


def opportunity_result(state):
    return float(state["result_R"]) if state.get("result_R") is not None else 0.0


def report_rows(rows):
    baselines = {r["signal_id"]: r["state"] for r in rows if r["variant"] == "baseline"}
    summaries = []
    for name, definition in DEFINITIONS.items():
        cohort = [r["state"] for r in rows if r["variant"] == name]
        closed = [s for s in cohort if s.get("result_R") is not None]
        pairs = [(baselines[r["signal_id"]], r["state"]) for r in rows
                 if r["variant"] == name and r["signal_id"] in baselines
                 and resolved(baselines[r["signal_id"]]) and resolved(r["state"])]
        deltas = [opportunity_result(v) - opportunity_result(b) for b, v in pairs]
        followups = [s["stop_followup"] for s in cohort if s.get("stop_followup")]
        complete = [f for f in followups if f["complete"]]
        summary = outcome_summary(closed)
        paired_baseline = outcome_summary([b for b, _ in pairs if b.get("result_R") is not None])
        paired_variant = outcome_summary([v for _, v in pairs if v.get("result_R") is not None])
        summaries.append({"variant": name, "label": definition["label"], "recorded": len(cohort),
                          "outcomes": summary, "matched_opportunities": len(pairs),
                          "matched_baseline": paired_baseline, "matched_variant": paired_variant,
                          "mean_delta_R": sum(deltas) / len(deltas) if deltas else None,
                          "skipped": sum(s["status"] == "skipped" for s in cohort),
                          "cancelled": sum(s["status"] == "cancelled" for s in cohort),
                          "unavailable": sum(s["status"] == "unavailable" for s in cohort),
                          "open": sum(s["status"] == "open" for s in cohort),
                          "data_gaps": sum(bool(s["metadata"]["execution"].get("data_gap") or s.get("stop_followup", {}).get("data_gap")) for s in cohort),
                          "stops_observed_to_resolution": len(complete),
                          "stops_later_reaching_target": sum(f["target_revisited"] for f in complete),
                          "stop_followups_pending": len(followups) - len(complete)})
    return {"strategy_version": STRATEGY_VERSION, "variants": summaries,
            "basis": "Prospective paired signals with frozen settings and costs. One change per variant. Mean delta uses matched resolved opportunities; skipped/cancelled entries contribute zero, unknown earnings are excluded. Each filled variant uses one unit of its own initial stop risk, with identical risk budgets assumed. These are per-signal R outcomes, not funded portfolio returns. Drawdown is closed-trade R drawdown, not concurrent equity drawdown. No automatic promotion; a positive average alone is not evidence of reliable uplift.",
            "earnings": "Shadow only. Individual stocks require a fresh calendar snapshot; missing coverage is unknown, never clear. Blackout includes the report date and two preceding calendar days.",
            "stop_followup": "Later target touches are monitored until original expiry, excluding the ambiguous stopping bar. Unresolved/missing-bar followups are shown separately."}


def experiment_report():
    return report_rows(fetch_all("""SELECT e.* FROM signal_experiments e JOIN signals s USING(signal_id)
                                   WHERE s.strategy_version=%s ORDER BY s.observed_at,e.variant""", (STRATEGY_VERSION,)))
