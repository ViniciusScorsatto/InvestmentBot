"""Chronological, observed-trade evaluation; never interprets in-sample fit as uplift."""
from __future__ import annotations

from typing import Any
from learning_model import _closed_trade_rows, _metadata, eligible_history, build_stats, score_setup
from market_bars import as_datetime
from config import STRATEGY_VERSION


def outcome_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    results = [float(r.get("result_R", r.get("result_r"))) for r in sorted(rows, key=lambda r: as_datetime(r["date_closed"]))]
    total = sum(results)
    profit = sum(r for r in results if r > 0)
    loss = -sum(r for r in results if r < 0)
    equity = peak = drawdown = 0.0
    for r in results:
        equity += r
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {"closed_trades": len(rows), "total_R": round(total, 4),
            "avg_R": round(total / len(rows), 4) if rows else None,
            "win_rate": round(sum(r > 0 for r in results) / len(rows) * 100, 2) if rows else None,
            "profit_factor": round(profit / loss, 4) if loss else None,
            "max_closed_drawdown_R": round(drawdown, 4)}


def walk_forward_evaluation(rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    rows = _closed_trade_rows() if rows is None else rows
    cohort = [r for r in rows if eligible_history(r) and r.get("date_opened") and r.get("date_closed")
              and r.get("result_R", r.get("result_r")) is not None]
    accepted, rejected, decisions = [], [], []
    for row in sorted(cohort, key=lambda r: as_datetime(r["date_opened"])):
        # Only outcomes known BEFORE the decision; overlapping positions are excluded.
        history = [r for r in cohort if as_datetime(r["date_closed"]) < as_datetime(row["date_opened"])]
        setup = dict(row, components=_metadata(row))
        feedback = score_setup(setup, build_stats(history))
        (accepted if feedback["approved"] else rejected).append(row)
        decisions.append({"id": row.get("id"), "training_trades": len(history),
                          "approved": feedback["approved"], "model_score": feedback["model_score"]})
    return {"strategy_version": STRATEGY_VERSION, "observed_baseline": outcome_summary(cohort),
            "overlay_accepted": outcome_summary(accepted), "overlay_rejected": outcome_summary(rejected),
            "decisions": decisions,
            "limitation": "Observed executed trades only. Historical rejected candidates and portfolio re-ranking outcomes are unavailable; this is not an unbiased full-strategy uplift estimate."}
