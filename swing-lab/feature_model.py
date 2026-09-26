"""Versioned ridge challenger with empirical hierarchical pooling, always shadow-only."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta
import hashlib
import json
import math

import numpy as np

from market_bars import as_datetime
from model_dataset import FEATURES, contract_definition, feature_vector, training_rows
from config import PREFERRED_TOP_SETUPS

MODEL_VERSION = "ridge-entry-v2"
RECENCY_HALF_LIFE_DAYS = 90.0
MODEL_FEATURES = FEATURES + ("rsi_curvature", "trend_volume")
RIDGE_ALPHA = 10.0
POOL_STRENGTH = 20.0
MIN_FEATURE_ROWS = 30
MIN_WEEKS = 4
VALIDATION_WEEKS = 4
MATURITY_DAYS = 17  # Longer than either strategy's maximum holding period.
DEFINITION = {"version": MODEL_VERSION, "features": list(MODEL_FEATURES), "recency_half_life_days": RECENCY_HALF_LIFE_DAYS,
              "selection_gate": "positive_mean_delta_and_no_worse_worst_week", "entry_prior_strength": 20, "ridge_alpha": RIDGE_ALPHA,
              "pool_strength": POOL_STRENGTH, "minimum_feature_rows": MIN_FEATURE_ROWS,
              "minimum_weeks": MIN_WEEKS, "validation_weeks": VALIDATION_WEEKS,
              "maturity_days": MATURITY_DAYS, "top_k": PREFERRED_TOP_SETUPS, "mode": "shadow_only"}


def group_keys(setup):
    return [(), (setup["strategy"],), (setup["strategy"], setup["asset_class"]),
            (setup["strategy"], setup["asset_class"], setup["timeframe"])]


def key_string(key):
    return json.dumps(key, separators=(",", ":"))


def sample_weights(rows, cutoff=None):
    # Each correlation group contributes at most one unit per decision date.
    sizes = Counter(r["cluster"] for r in rows)
    weights = np.asarray([1 / sizes[r["cluster"]] for r in rows], dtype=float)
    if cutoff is not None:
        ages = np.asarray([max(0, (as_datetime(cutoff) - as_datetime(r["observed_at"])).total_seconds() / 86400) for r in rows])
        weights *= np.exp2(-ages / RECENCY_HALF_LIFE_DAYS)
    return weights


def expanded_features(values):
    """Two fixed signal-time interactions, scaled with training data only."""
    rsi, volume, distance, gap, atr, stop = values
    return list(values) + [((rsi - 50) / 50) ** 2, gap * volume]


def pooled_groups(rows, weights):
    groups = defaultdict(lambda: [0.0, 0.0])
    clusters = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
    for row, weight in zip(rows, weights):
        for key in group_keys(row["setup"]):
            k = key_string(key)
            groups[k][0] += float(weight)
            groups[k][1] += float(weight) * row["target"]
            clusters[row["cluster"]][k][0] += float(weight)
            clusters[row["cluster"]][k][1] += float(weight) * row["target"]
    return dict(groups), dict(clusters)


def pooled_prediction(groups, setup, exclude=None):
    estimate, support = 0.0, 0.0
    levels = []
    for key in group_keys(setup):
        k = key_string(key)
        weight, total = groups.get(k, (0.0, 0.0))
        if exclude:
            subtract_weight, subtract_total = exclude.get(k, (0.0, 0.0))
            weight, total = max(0, weight - subtract_weight), total - subtract_total
        parent = estimate
        estimate = (total + POOL_STRENGTH * parent) / (weight + POOL_STRENGTH)
        support = weight if weight > 0 else support
        levels.append({"group": list(key), "effective_units": weight, "estimate_R": estimate})
    return estimate, support, levels


def fit_raw(rows, cutoff=None):
    weights = sample_weights(rows, cutoff)
    groups, clusters = pooled_groups(rows, weights)
    feature_indices = [i for i, r in enumerate(rows) if r["features"] is not None]
    weeks = {as_datetime(r["observed_at"]).date().isocalendar()[:2] for r in rows}
    result = {"groups": groups, "training_rows": len(rows), "effective_units": float(weights.sum()),
              "training_weeks": len(weeks), "feature_rows": len(feature_indices), "ridge": None}
    feature_weeks = {as_datetime(rows[i]["observed_at"]).date().isocalendar()[:2] for i in feature_indices}
    if len(feature_indices) < MIN_FEATURE_ROWS or len(feature_weeks) < MIN_WEEKS or weights[feature_indices].sum() < 10:
        return result
    selected = [rows[i] for i in feature_indices]
    x = np.asarray([expanded_features(r["features"]) for r in selected], dtype=float)
    w = weights[feature_indices]
    mean = np.average(x, axis=0, weights=w)
    scale = np.sqrt(np.average((x - mean) ** 2, axis=0, weights=w))
    scale[scale < 1e-8] = 1.0
    z = np.clip((x - mean) / scale, -5, 5)
    # Residual targets exclude their entire correlated cluster from the hierarchy.
    residuals = np.asarray([r["target"] - pooled_prediction(groups, r["setup"], clusters[r["cluster"]])[0]
                            for r in selected])
    gram = z.T @ (w[:, None] * z) + RIDGE_ALPHA * np.eye(len(MODEL_FEATURES))
    coefficients = np.linalg.solve(gram, z.T @ (w * residuals))
    result["ridge"] = {"mean": mean.tolist(), "scale": scale.tolist(), "coefficients": coefficients.tolist(),
                       "inverse_gram": np.linalg.inv(gram).tolist()}
    return result


def raw_prediction(model, setup):
    prior, support, levels = pooled_prediction(model["groups"], setup)
    values = feature_vector(setup)
    if values is None or model["ridge"] is None:
        return prior, 0.0, support, levels, None
    ridge = model["ridge"]
    raw_z = (np.asarray(expanded_features(values)) - ridge["mean"]) / ridge["scale"]
    z = np.clip(raw_z, -5, 5)
    correction = float(z @ np.asarray(ridge["coefficients"]))
    leverage = float(z @ np.asarray(ridge["inverse_gram"]) @ z)
    novelty = float(max(0.0, np.max(np.abs(raw_z)) - 3))
    return prior, correction, support, levels, {"leverage": max(0, leverage), "novelty": novelty}


def resolved_rows(observations, cutoff):
    cutoff = as_datetime(cutoff)
    return [r for r in observations if r["resolved"] and r.get("available_at") and r.get("closed_at")
            and as_datetime(r["observed_at"]) < cutoff and as_datetime(r["available_at"]) < cutoff
            and as_datetime(r["closed_at"]) < cutoff]


def fit_entry(rows, cutoff):
    """Regularized logistic fill model; cancellations are negative entry labels."""
    weights = sample_weights(rows, cutoff)
    labels = np.asarray([float(not r["cancelled"]) for r in rows])
    units = float(weights.sum())
    prior = float((weights @ labels + 10) / (units + 20))
    result = {"prior": prior, "effective_units": units, "training_rows": len(rows), "logistic": None}
    indices = [i for i, r in enumerate(rows) if r["features"] is not None]
    weeks = {as_datetime(rows[i]["observed_at"]).date().isocalendar()[:2] for i in indices}
    if len(indices) < MIN_FEATURE_ROWS or len(weeks) < MIN_WEEKS or weights[indices].sum() < 10:
        return result
    w, y = weights[indices], labels[indices]
    if min(float(w @ y), float(w @ (1-y))) < 5:
        return result
    x = np.asarray([expanded_features(rows[i]["features"]) for i in indices])
    mean = np.average(x, axis=0, weights=w)
    scale = np.sqrt(np.average((x-mean)**2, axis=0, weights=w))
    scale[scale < 1e-8] = 1
    z = np.column_stack([np.ones(len(x)), np.clip((x-mean)/scale, -5, 5)])
    center = np.zeros(z.shape[1]); center[0] = math.log(prior/(1-prior))
    beta = center.copy()
    penalty = np.full(len(beta), RIDGE_ALPHA)
    converged = False

    def loss(coefficients):
        logits = z @ coefficients
        return float(w @ (np.logaddexp(0, logits)-y*logits) + .5*np.sum(penalty*(coefficients-center)**2))

    # Penalize intercept toward pooled probability to remain finite for sparse classes.
    for _ in range(50):
        probability = 1/(1+np.exp(-np.clip(z @ beta, -30, 30)))
        gradient = z.T @ (w*(probability-y)) + penalty*(beta-center)
        hessian = z.T @ ((w*probability*(1-probability))[:,None]*z) + np.diag(penalty)
        step = np.linalg.solve(hessian, gradient)
        if np.max(np.abs(step)) < 1e-8:
            converged = True
            break
        current_loss = loss(beta)
        rate = 1.
        for _ in range(20):
            proposed = beta-rate*step
            if loss(proposed) <= current_loss:
                beta = proposed
                break
            rate *= .5
        else:
            break
    if not converged:
        return result
    result["logistic"] = {"mean": mean.tolist(), "scale": scale.tolist(), "coefficients": beta.tolist()}
    return result


def entry_probability(model, setup, use_features=True):
    values, fitted = feature_vector(setup), model["logistic"]
    if not use_features or values is None or fitted is None:
        return model["prior"]
    z = np.clip((np.asarray(expanded_features(values))-fitted["mean"])/fitted["scale"], -5, 5)
    logit = float(np.r_[1., z] @ np.asarray(fitted["coefficients"]))
    return float(1/(1+math.exp(-max(-30, min(30, logit)))))


def correction_weight(raw, distance):
    if distance is None:
        return 0.0
    units = raw["effective_units"]
    return units/(units+30)/(1+distance["leverage"]+distance["novelty"])


def selection_evidence(batches, baseline, candidate):
    """Fixed top-k policy, same complete batches; cancelled entries return zero."""
    weekly = defaultdict(lambda: [0., 0.])
    deltas = []
    for week, batch in batches:
        totals = []
        for field in (baseline, candidate):
            ordered = sorted(batch, key=lambda r: (-r[field], -r["row"]["champion_score"], r["row"]["signal_id"]))
            chosen = [r for r in ordered if r[field] > 0][:PREFERRED_TOP_SETUPS]
            totals.append(sum(r["row"]["target"] or 0 for r in chosen))
        weekly[week][0] += totals[0]; weekly[week][1] += totals[1]
        deltas.append(totals[1]-totals[0])
    mean_delta = sum(deltas)/len(deltas) if deltas else 0.
    worst_base = min((v[0] for v in weekly.values()), default=0.)
    worst_candidate = min((v[1] for v in weekly.values()), default=0.)
    return {"batches": len(deltas), "mean_delta_R_per_batch": mean_delta,
            "baseline_worst_week_R": worst_base, "candidate_worst_week_R": worst_candidate,
            "passed": len(weekly) >= 2 and mean_delta > 1e-9 and worst_candidate >= worst_base-1e-9}


def validation_evidence(observations, cutoff):
    """Nested chronological validation of prediction error AND selected opportunities."""
    cutoff = as_datetime(cutoff)
    end = (cutoff - timedelta(days=MATURITY_DAYS)).replace(hour=0, minute=0, second=0, microsecond=0)
    end -= timedelta(days=end.weekday())
    errors, folds, batches, entry_scores = [], [], [], []
    entry_weeks = set()
    for number in range(VALIDATION_WEEKS):
        start = end - timedelta(weeks=VALIDATION_WEEKS - number)
        stop = start + timedelta(weeks=1)
        test = [r for r in observations if start <= as_datetime(r["observed_at"]) < stop]
        train = training_rows(observations, start)
        raw, entry = fit_raw(train, start), fit_entry(resolved_rows(observations, start), start)
        fold = {"start": start.isoformat(), "end": stop.isoformat(), "training_rows": len(train),
                "signals": len(test), "status": "empty", "evaluated": 0}
        if test and any(not r["resolved"] or not r.get("available_at") or as_datetime(r["available_at"]) >= cutoff for r in test):
            fold["status"] = "unresolved"
        elif test and raw["ridge"] is not None:
            fold["status"] = "complete"
            grouped = defaultdict(list)
            for row, weight in zip(test, sample_weights(test)):
                prior, delta, _, _, distance = raw_prediction(raw, row["setup"])
                conditional = prior + correction_weight(raw, distance)*delta
                base_p = entry["prior"]
                p = entry_probability(entry, row["setup"])
                grouped[row["observed_at"]].append({"row": row, "baseline": base_p*prior,
                    "feature": base_p*conditional, "entry_baseline": p*prior, "entry_feature": p*conditional})
                if entry["logistic"] is not None and row["features"] is not None:
                    entry_weeks.add(start.isoformat())
                    entry_scores.append((float(not row["cancelled"]), base_p, p, float(weight)))
                if row["target"] is not None and row["features"] is not None:
                    errors.append((row["target"]-prior, row["target"]-conditional, float(weight)))
                    fold["evaluated"] += 1
            batches.extend((start.isoformat(), batch) for batch in grouped.values())
        elif test:
            fold["status"] = "warming_up"
        folds.append(fold)
    complete = sum(f["status"] == "complete" and f["evaluated"] > 0 for f in folds)
    weight = sum(e[2] for e in errors)
    baseline_mae = sum(abs(e[0])*e[2] for e in errors)/weight if weight else None
    feature_mae = sum(abs(e[1])*e[2] for e in errors)/weight if weight else None
    rmse = math.sqrt(sum(e[1]**2*e[2] for e in errors)/weight) if weight else None
    selection = selection_evidence(batches, "baseline", "feature")
    skill = max(0., min(1., 1-feature_mae/baseline_mae)) if (
        complete >= 2 and weight >= 20 and baseline_mae and feature_mae is not None and selection["passed"]) else 0.
    entry_weight = sum(e[3] for e in entry_scores)
    base_brier = sum((y-b)**2*w for y,b,p,w in entry_scores)/entry_weight if entry_weight else None
    brier = sum((y-p)**2*w for y,b,p,w in entry_scores)/entry_weight if entry_weight else None
    entry_selection = selection_evidence(batches, "feature" if skill else "baseline", "entry_feature" if skill else "entry_baseline")
    reliability = []
    for lo in range(5):
        bucket = [e for e in entry_scores if lo/5 <= e[2] < (lo+1)/5]
        units = sum(e[3] for e in bucket)
        if units:
            reliability.append({"lower": lo/5, "upper": (lo+1)/5, "effective_units": units,
                                "predicted": sum(e[2]*e[3] for e in bucket)/units,
                                "observed": sum(e[0]*e[3] for e in bucket)/units})
    return {"folds": folds, "complete_weeks": complete, "effective_units": weight,
            "baseline_mae_R": baseline_mae, "feature_mae_R": feature_mae,
            "feature_rmse_R": rmse, "skill": skill, "selection": selection,
            "entry": {"complete_weeks": len(entry_weeks), "effective_units": entry_weight, "baseline_brier": base_brier, "brier": brier,
                      "reliability": reliability, "selection": entry_selection,
                      "validated": bool(len(entry_weeks) >= 2 and entry_weight >= 20 and brier is not None
                                        and brier < base_brier and entry_selection["passed"])}}


def fit_model(observations, cutoff):
    cutoff = as_datetime(cutoff)
    rows = training_rows(observations, cutoff)
    raw = fit_raw(rows, cutoff)
    entry_rows = resolved_rows(observations, cutoff)
    entry = fit_entry(entry_rows, cutoff)
    evidence = validation_evidence(observations, cutoff)
    fingerprint_rows = [{key: r[key] for key in ("signal_id", "features", "target", "available_at", "observed_at", "closed_at", "cancelled", "cluster", "setup")} for r in entry_rows]
    fingerprint = hashlib.sha256(json.dumps(fingerprint_rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    artifact = {"definition": DEFINITION, "contract": contract_definition(), "cutoff": cutoff.isoformat(), "training_hash": fingerprint,
                "training_ids": [r["signal_id"] for r in rows], "raw": raw, "entry": entry, "entry_training_ids": [r["signal_id"] for r in entry_rows], "validation": evidence}
    artifact["snapshot_id"] = hashlib.sha256(json.dumps(artifact, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return artifact


def predict(artifact, setup):
    prior, correction, support, levels, distance = raw_prediction(artifact["raw"], setup)
    evidence = artifact["validation"]
    feature_weight = correction_weight(artifact["raw"], distance) if evidence["skill"] > 0 else 0.0
    conditional = prior + feature_weight * correction
    probability = entry_probability(artifact["entry"], setup, evidence["entry"]["validated"])
    expected = probability * conditional
    return {"model_version": MODEL_VERSION, "snapshot_id": artifact["snapshot_id"],
            "training_cutoff": artifact["cutoff"], "expected_net_R": expected,
            "prediction_basis": "per_signal_opportunity",
            "conditional_net_R": conditional, "entry_probability": probability,
            "entry_status": "validated" if (evidence["entry"]["validated"] and artifact["entry"]["logistic"] is not None
                                             and feature_vector(setup) is not None) else "pooled",
            "hierarchical_R": prior, "feature_correction_R": correction,
            "feature_weight": feature_weight, "effective_support": support, "hierarchy": levels,
            "feature_status": "missing" if feature_vector(setup) is None else (
                "warming_up" if artifact["raw"]["ridge"] is None else (
                    "validated" if feature_weight > 0 else "unproven")),
            "ranking_ready": (artifact["raw"]["training_rows"] >= 30 and artifact["raw"]["training_weeks"] >= 4
                              and artifact["raw"]["effective_units"] >= 10),
            "approved": expected > 0, "mode": "shadow_only"}
