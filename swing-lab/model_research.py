"""Prospective challenger predictions and nested chronological model evaluation."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging

from config import STRATEGY_VERSION, PREFERRED_TOP_SETUPS
from db import fetch_all
from evaluation import outcome_summary
from feature_model import MODEL_VERSION, fit_model, predict
from market_bars import as_datetime
from model_dataset import dataset, read_signal_rows

LOGGER = logging.getLogger(__name__)
BASIS = ("Shadow model research on prospectively recorded qualifying baseline signals only. "
         "The champion uses its frozen approval and rule/model score; the challenger ranks estimated net R per signal opportunity, including entry probability. "
         "Top-k is a matched ranking experiment, without portfolio cash, exposure or correlation constraints. "
         "Skipped opportunities contribute zero; cancelled entries have zero return and train entry probability, not the return regressor. "
         "Only fully resolved batches enter matched results. Drawdown is closed-trade R, not portfolio equity. "
         "No automatic model promotion; positive results alone do not establish reliable improvement.")


def rankings(entries, predictions, top_k=PREFERRED_TOP_SETUPS):
    ordered = sorted(entries, key=lambda r: (-float(r["champion_score"]), -float(r["setup"].get("score", 0)),
                                            -float(r["setup"].get("R_multiple", 0)), r["signal_id"]))
    champion = [r for r in ordered if r["champion_approved"]]
    ready = bool(entries) and all(predictions.get(r["signal_id"], {}).get("ranking_ready", False) for r in entries)
    if ready:
        challenger = sorted(entries, key=lambda r: (-predictions[r["signal_id"]]["expected_net_R"],
                                                    -float(r["champion_score"]), r["signal_id"]))
        challenger = [r for r in challenger if predictions[r["signal_id"]]["approved"]]
    else:
        challenger = champion
    cr = {r["signal_id"]: i + 1 for i, r in enumerate(champion)}
    nr = {r["signal_id"]: i + 1 for i, r in enumerate(challenger)}
    return {r["signal_id"]: {"champion_rank": cr.get(r["signal_id"]), "challenger_rank": nr.get(r["signal_id"]),
                              "champion_selected": cr.get(r["signal_id"], top_k + 1) <= top_k,
                              "challenger_selected": nr.get(r["signal_id"], top_k + 1) <= top_k,
                              "ranking_ready": ready} for r in entries}


def prepare_batch(candidates, at):
    from signals import signal_id
    if not candidates:
        return None
    unique = {}
    for setup in candidates:
        unique.setdefault(signal_id(setup), setup)
    candidates = list(unique.values())
    try:
        observations, coverage = dataset(read_signal_rows(), at)
        artifact = fit_model(observations, at)
        predictions = {signal_id(s): predict(artifact, s) for s in candidates}
        error = None
    except Exception as exc:
        # A research outage cannot change funded approvals, ranks or allocations.
        LOGGER.warning("Challenger preparation unavailable (%s)", type(exc).__name__)
        artifact, coverage, error = None, {}, type(exc).__name__
        predictions = {signal_id(s): {"model_version": MODEL_VERSION, "mode": "shadow_only",
                                      "ranking_ready": False, "feature_status": "unavailable", "error": error}
                       for s in candidates}
    entries = [{"signal_id": signal_id(s), "setup": s,
                "champion_score": s.get("combined_score", s["score"]),
                "champion_approved": not s.get("model_feedback") or bool(s["model_feedback"]["approved"])} for s in candidates]
    batch_id = hashlib.sha256(json.dumps([at.isoformat(), sorted(r["signal_id"] for r in entries)]).encode()).hexdigest()
    return {"artifact": artifact, "coverage": coverage, "predictions": predictions,
            "batch_id": batch_id, "predicted_at": at.isoformat(), "error": error}


def record_batch(connection, research, fresh):
    from signals import signal_id
    if not research or not fresh:
        return
    artifact = research["artifact"]
    if artifact:
        connection.execute("""INSERT INTO model_snapshots(snapshot_id,model_version,strategy_version,training_cutoff,artifact)
                              VALUES (%s,%s,%s,%s,%s::jsonb) ON CONFLICT DO NOTHING""",
                           (artifact["snapshot_id"], MODEL_VERSION, STRATEGY_VERSION, artifact["cutoff"], json.dumps(artifact, allow_nan=False)))
    entries = [{"signal_id": signal_id(s), "setup": s,
                "champion_score": s.get("combined_score", s["score"]),
                "champion_approved": not s.get("model_feedback") or bool(s["model_feedback"]["approved"])} for s in fresh]
    # Rank only newly recorded signals; rescans cannot rewrite earlier decisions.
    ranks = rankings(entries, research["predictions"])
    for entry in entries:
        identity = entry["signal_id"]
        rank = ranks[identity]
        prediction = research["predictions"][identity] | {"ranking_ready": rank["ranking_ready"]}
        connection.execute("""INSERT INTO model_predictions(signal_id,model_version,snapshot_id,batch_id,predicted_at,
                                  prediction,champion_rank,challenger_rank,champion_selected,challenger_selected)
                              VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                           (identity, MODEL_VERSION, artifact["snapshot_id"] if artifact else None,
                            research["batch_id"], research["predicted_at"], json.dumps(prediction, allow_nan=False),
                            rank["champion_rank"], rank["challenger_rank"], rank["champion_selected"], rank["challenger_selected"]))


def compare_batches(entries):
    batches = defaultdict(list)
    for entry in entries:
        batches[entry["batch_id"]].append(entry)
    matched, pending = [], 0
    for batch in batches.values():
        if all(r["observation"] is not None and r["observation"]["resolved"] for r in batch):
            matched.extend(batch)
        else:
            pending += 1
    outcomes = {}
    for name in ("champion", "challenger"):
        selected = [r["observation"] for r in matched if r[name + "_selected"]]
        closed = [{"result_R": r["target"], "date_closed": r["closed_at"]} for r in selected if r["target"] is not None]
        outcomes[name] = outcome_summary(closed) | {"selected_opportunities": len(selected),
                                                   "cancelled": sum(r["cancelled"] for r in selected)}
    deltas = []
    for batch in batches.values():
        if not all(r["observation"] is not None and r["observation"]["resolved"] for r in batch):
            continue
        delta = sum((r["observation"]["target"] or 0) * (int(r["challenger_selected"]) - int(r["champion_selected"])) for r in batch)
        deltas.append(delta)
    scored = [r for r in matched if r["prediction"].get("ranking_ready")
              and "expected_net_R" in r["prediction"]]
    forecast_error = (sum(abs(r["prediction"]["expected_net_R"] - (r["observation"]["target"] or 0)) for r in scored) / len(scored)) if scored else None
    return {"prediction_count": len(scored), "prediction_mae_R": forecast_error,
            "recorded_batches": len(batches), "matched_batches": len(deltas), "pending_batches": pending,
            "matched_signals": len(matched), **outcomes, "total_delta_R": sum(deltas),
            "mean_delta_R_per_batch": sum(deltas) / len(deltas) if deltas else None,
            "ready_batches": sum(all(r["prediction"].get("ranking_ready", False) for r in batch) for batch in batches.values())}


def prospective_report(as_of=None):
    as_of = as_datetime(as_of or datetime.now(timezone.utc))
    observations, coverage = dataset(read_signal_rows(), as_of)
    by_id = {r["signal_id"]: r for r in observations}
    rows = fetch_all("""SELECT p.* FROM model_predictions p JOIN signals s USING(signal_id)
                        WHERE p.model_version=%s AND s.strategy_version=%s ORDER BY p.predicted_at,p.signal_id""", (MODEL_VERSION, STRATEGY_VERSION))
    entries = [dict(r, observation=by_id.get(r["signal_id"])) for r in rows if as_datetime(r["predicted_at"]) < as_of]
    latest = fetch_all("""SELECT artifact FROM model_snapshots WHERE model_version=%s AND strategy_version=%s
                          AND training_cutoff < %s ORDER BY training_cutoff DESC LIMIT 1""", (MODEL_VERSION, STRATEGY_VERSION, as_of))
    artifact = latest[0]["artifact"] if latest else None
    return {"model_version": MODEL_VERSION, "strategy_version": STRATEGY_VERSION, "mode": "shadow_only",
            "coverage": coverage, "comparison": compare_batches(entries), "basis": BASIS,
            "latest_model": ({"cutoff": artifact["cutoff"], "snapshot_id": artifact["snapshot_id"],
                              "training_rows": artifact["raw"]["training_rows"],
                              "effective_units": artifact["raw"]["effective_units"],
                              "feature_rows": artifact["raw"]["feature_rows"], "entry_training_rows": artifact["entry"]["training_rows"],
                              "entry_effective_units": artifact["entry"]["effective_units"], "validation": artifact["validation"]} if artifact else None),
            "unavailable_predictions": sum(r["prediction"].get("feature_status") == "unavailable" for r in entries),
            "recent_predictions": [{"signal_id": r["signal_id"], "predicted_at": as_datetime(r["predicted_at"]).isoformat(),
                                    "champion_rank": r["champion_rank"], "challenger_rank": r["challenger_rank"],
                                    "prediction": r["prediction"]} for r in entries[-20:]]}


def chronological_evaluation(rows=None, as_of=None):
    """Weekly expanding fits with nested earlier validation, never random splits.

    This is retrospective research, explicitly distinct from frozen prospective
    predictions. All candidate batches are predicted before examining their labels.
    """
    as_of = as_datetime(as_of or datetime.now(timezone.utc))
    observations, coverage = dataset(read_signal_rows() if rows is None else rows, as_of)
    entries, folds = [], []
    weeks = sorted({(as_datetime(r["observed_at"]) - timedelta(days=as_datetime(r["observed_at"]).weekday())).replace(
                        hour=0,minute=0,second=0,microsecond=0) for r in observations})
    for start in weeks:
        stop = start + timedelta(weeks=1)
        test = [r for r in observations if start <= as_datetime(r["observed_at"]) < stop]
        artifact = fit_model(observations, start)
        batches = defaultdict(list)
        for row in test:
            batches[row["observed_at"]].append(row)
        fold_entries = []
        for batch_id, batch in batches.items():
            predictions = {r["signal_id"]: predict(artifact, r["setup"]) for r in batch}
            ranks = rankings(batch, predictions)
            for row in batch:
                prediction = predictions[row["signal_id"]]
                prediction["ranking_ready"] = ranks[row["signal_id"]]["ranking_ready"]
                entry = {"batch_id": batch_id, "observation": row, "prediction": prediction, **ranks[row["signal_id"]]}
                fold_entries.append(entry)
        summary = compare_batches(fold_entries)
        # A still-open calendar week is explicitly provisional even if its signals closed.
        complete = stop < as_of and summary["pending_batches"] == 0
        folds.append({"start": start.isoformat(), "end": stop.isoformat(), "complete": complete,
                      "training_rows": artifact["raw"]["training_rows"], "training_hash": artifact["training_hash"],
                      "training_cutoff": artifact["cutoff"], "feature_skill": artifact["validation"]["skill"], **summary})
        if complete:
            entries.extend(fold_entries)
    return {"model_version": MODEL_VERSION, "mode": "retrospective_research", "as_of": as_of.isoformat(),
            "coverage": coverage, "folds": folds, "complete_periods": sum(f["complete"] for f in folds),
            "comparison": compare_batches(entries), "basis": BASIS,
            "validation": "Fixed weekly expanding training windows; labels must have been observed before the fold cutoff. Feature scaling, age weighting, hierarchy, nonlinear ridge, entry classifier and inner validation use earlier data only. Aggregate results exclude incomplete weeks. Parameters are fixed, not selected using these test results. Historical labels without arrival timestamps become available at migration time, never backdated."}
