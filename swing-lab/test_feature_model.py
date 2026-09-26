"""Model-only regressions: temporal leakage, pooling, uncertainty and frozen ranks."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch

import test_strategy_adjustments
import config, signals, model_dataset, feature_model, model_research

START = datetime(2026, 1, 5, tzinfo=timezone.utc)


def model_setup(rsi=55, asset="BTC", asset_class="crypto", strategy="Breakout", timeframe="4h"):
    return {"asset":asset,"asset_class":asset_class,"strategy":strategy,"timeframe":timeframe,
            "entry_price":100.,"stop_loss":90.,"target_price":130.,"R_multiple":3.,"score":85,
            "combined_score":85,"signal_bar_end":START.isoformat(),
            "components":{"features":{"rsi":rsi,"volume_ratio":1.3,"distance_ema20_pct":.01,"ema_gap_pct":.02,"atr":4}}}


def signal_row(index=0, at=START, result=1., rsi=55, approved=True, selected=False, available=None):
    setup=model_setup(rsi)
    setup["signal_bar_end"]=at.isoformat()
    setup["model_feedback"]={"approved":approved,"model_score":50}
    state=signals.initial_state(setup,at)
    state.update(status="closed" if result is not None else "open",result_R=result,
                 date_closed=(at+timedelta(hours=6)).isoformat() if result is not None else None)
    return {"signal_id":f"signal-{index}","strategy_version":config.STRATEGY_VERSION,
            "asset":"BTC","asset_class":"crypto","strategy":"Breakout","timeframe":"4h",
            "observed_at":at,"label_available_at":available or (at+timedelta(hours=7) if result is not None else None),
            "model_approved":approved,"selected_trade_id":index+1 if selected else None,
            "setup_json":setup,"shadow_state":state}


def synthetic_rows(days=140):
    # Synthetic data proves the estimator can learn a known relationship; not a market backtest.
    return [signal_row(day*8+j,START+timedelta(days=day),result=(rsi-55)/10,rsi=rsi,
                       approved=j%2==0,selected=j==0)
            for day in range(days) for j,rsi in enumerate(range(35,75,5))]


class DatasetTests(unittest.TestCase):
    def test_rejected_unselected_and_selected_each_enter_once(self):
        rows=[signal_row(0,approved=False),signal_row(1),signal_row(2,selected=True)]
        observations,coverage=model_dataset.dataset(rows+[rows[0]],START+timedelta(days=2))
        self.assertEqual(len(observations),3)
        self.assertEqual(len(model_dataset.training_rows(observations,START+timedelta(days=2))),3)
        self.assertEqual(coverage["duplicate_or_missing_id"],1)
        self.assertEqual(coverage["model_rejected"],1)
        self.assertEqual(coverage["unselected"],2)

    def test_label_availability_not_closing_candle_controls_training(self):
        row=signal_row(available=START+timedelta(days=10))
        observations,_=model_dataset.dataset([row],START+timedelta(days=2))
        self.assertIsNone(observations[0]["target"])
        self.assertFalse(observations[0]["resolved"])
        observations,_=model_dataset.dataset([row],START+timedelta(days=11))
        self.assertEqual(model_dataset.training_rows(observations,START+timedelta(days=10)),[])
        self.assertEqual(len(model_dataset.training_rows(observations,START+timedelta(days=11))),1)

    def test_equal_future_and_missing_availability_excluded(self):
        cutoff=START+timedelta(days=2)
        rows=[signal_row(0,available=cutoff),signal_row(1,available=cutoff+timedelta(seconds=1)),signal_row(2)]
        rows[2]["label_available_at"]=None
        observations,_=model_dataset.dataset(rows,cutoff)
        self.assertTrue(all(r["target"] is None for r in observations))

    def test_missing_features_preserve_hierarchy_labels(self):
        row=signal_row();row["setup_json"]["components"]={}
        observations,coverage=model_dataset.dataset([row],START+timedelta(days=2))
        self.assertEqual(coverage["missing_features"],1)
        self.assertIsNone(observations[0]["features"])
        self.assertEqual(observations[0]["target"],1.)

    def test_cancellations_are_zero_opportunities_not_training_losses(self):
        row=signal_row();row["shadow_state"].update(status="cancelled",result_R=None)
        observations,_=model_dataset.dataset([row],START+timedelta(days=2))
        self.assertTrue(observations[0]["resolved"])
        self.assertTrue(observations[0]["cancelled"])
        self.assertEqual(model_dataset.training_rows(observations,START+timedelta(days=2)),[])

    def test_incompatible_execution_and_experimental_outcomes_excluded(self):
        rows=[signal_row(i) for i in range(5)]
        rows[0]["shadow_state"]["metadata"]["execution"]["fee_bps"]=999
        rows[1]["shadow_state"]["metadata"]["execution"]["min_entry_net_r"]=99
        rows[2]["shadow_state"]["experiment"]="atr_stop"
        rows[3]["strategy_version"]="old"
        observations,coverage=model_dataset.dataset(rows,START+timedelta(days=2))
        self.assertEqual(len(observations),1)
        self.assertEqual(coverage["incompatible_contract"],4)

    def test_original_signal_features_never_post_entry_values(self):
        row=signal_row();row["shadow_state"]["entry_price"]=120
        observations,_=model_dataset.dataset([row],START+timedelta(days=2))
        self.assertEqual(observations[0]["features"][-2:],[.04,2.5])

    def test_nonfinite_features_or_labels_do_not_poison_model(self):
        row=signal_row();row["setup_json"]["components"]["features"]["rsi"]=float("nan")
        self.assertIsNone(model_dataset.feature_vector(row["setup_json"]))
        row["shadow_state"]["result_R"]=float("inf")
        observations,coverage=model_dataset.dataset([row],START+timedelta(days=2))
        self.assertEqual(len(observations),1)
        self.assertIsNone(observations[0]["target"])
        self.assertFalse(observations[0]["resolved"])
        self.assertEqual(coverage["invalid_label"],1)


class FeatureModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cutoff=START+timedelta(days=142)
        cls.observations,_=model_dataset.dataset(synthetic_rows(),cls.cutoff)
        cls.artifact=feature_model.fit_model(cls.observations,cls.cutoff)

    def test_known_synthetic_relationship_changes_same_cohort_predictions(self):
        low=feature_model.predict(self.artifact,model_setup(35))
        high=feature_model.predict(self.artifact,model_setup(70))
        self.assertEqual(low["hierarchical_R"],high["hierarchical_R"])
        self.assertGreater(self.artifact["validation"]["skill"],0)
        self.assertGreater(high["expected_net_R"],low["expected_net_R"])
        self.assertGreater(high["feature_weight"],0)
        self.assertLess(high["feature_weight"],1)
        self.assertEqual(high["feature_status"],"validated")

    def test_reproducible_serializable_artifact_and_predictions(self):
        restored=json.loads(json.dumps(self.artifact,allow_nan=False))
        self.assertEqual(feature_model.predict(restored,model_setup()),feature_model.predict(self.artifact,model_setup()))
        repeat=feature_model.fit_model(self.observations,self.cutoff)
        self.assertEqual(repeat["snapshot_id"],self.artifact["snapshot_id"])

    def test_future_labels_and_features_cannot_change_earlier_fit(self):
        cutoff=START+timedelta(days=70)
        first=feature_model.fit_model(self.observations,cutoff)
        changed=deepcopy(self.observations)
        for row in changed:
            if model_dataset.as_datetime(row["available_at"]) >= cutoff:
                row["target"]=999
                row["features"]=[1e10]*6
        second=feature_model.fit_model(changed,cutoff)
        self.assertEqual(first,second)

    def test_delayed_known_label_excluded_even_if_signal_is_old(self):
        cutoff=START+timedelta(days=70)
        changed=deepcopy(self.observations)
        changed[0]["available_at"]=(cutoff+timedelta(days=2)).isoformat()
        changed[0]["target"]=10000
        first=feature_model.fit_model(changed,cutoff)
        second=feature_model.fit_model(changed[1:],cutoff)
        self.assertEqual(first,second)

    def test_scaler_fits_training_only(self):
        cutoff=START+timedelta(days=70)
        artifact=feature_model.fit_model(self.observations,cutoff)
        self.assertAlmostEqual(artifact["raw"]["ridge"]["mean"][0],52.5)
        self.assertTrue(all(model_dataset.as_datetime(r["available_at"])<cutoff for r in self.observations if r["signal_id"] in artifact["training_ids"]))

    def test_small_unseen_group_borrows_parent_evidence(self):
        observations,_=model_dataset.dataset([signal_row(i,START+timedelta(days=i),result=1) for i in range(60)],START+timedelta(days=61))
        raw=feature_model.fit_raw(observations)
        estimate,support,levels=feature_model.pooled_prediction(raw["groups"],model_setup(asset="AAPL",asset_class="stock"))
        self.assertGreater(estimate,0)
        self.assertEqual(levels[-1]["effective_units"],0)
        self.assertGreater(support,0)

    def test_pooling_has_no_30_observation_cohort_switch(self):
        rows=[signal_row(i,START+timedelta(days=i),result=1) for i in range(31)]
        observations,_=model_dataset.dataset(rows,START+timedelta(days=32))
        estimates=[feature_model.pooled_prediction(feature_model.fit_raw(observations[:n])["groups"],model_setup())[0] for n in (29,30,31)]
        self.assertLess(max(estimates)-min(estimates),.05)

    def test_correlated_duplicates_do_not_multiply_effective_support(self):
        observations,_=model_dataset.dataset([signal_row(i,result=1) for i in range(80)],START+timedelta(days=2))
        raw=feature_model.fit_raw(observations)
        self.assertAlmostEqual(raw["effective_units"],1)
        self.assertIsNone(raw["ridge"])

    def test_missing_feature_prediction_uses_hierarchy_without_feature_weight(self):
        setup=model_setup();setup["components"]={}
        prediction=feature_model.predict(self.artifact,setup)
        self.assertEqual(prediction["feature_weight"],0)
        self.assertEqual(prediction["conditional_net_R"],prediction["hierarchical_R"])
        self.assertEqual(prediction["feature_status"],"missing")

    def test_uncertainty_and_extrapolation_reduce_influence(self):
        artifact=deepcopy(self.artifact)
        normal=feature_model.predict(artifact,model_setup(55))["feature_weight"]
        artifact["raw"]["effective_units"]=10
        noisy=feature_model.predict(artifact,model_setup(55))["feature_weight"]
        far=feature_model.predict(self.artifact,model_setup(100))["feature_weight"]
        self.assertLess(noisy,normal)
        self.assertLess(far,normal)

    def test_unresolved_inner_fold_cannot_supply_validation_evidence(self):
        changed=deepcopy(self.observations)
        start=self.artifact["validation"]["folds"][-1]["start"]
        for row in changed:
            if row["observed_at"]>=start:
                row["resolved"]=False
        evidence=feature_model.validation_evidence(changed,self.cutoff)
        self.assertEqual(evidence["folds"][-1]["status"],"unresolved")
        self.assertLess(evidence["effective_units"],self.artifact["validation"]["effective_units"])

    def test_no_history_produces_safe_finite_warmup(self):
        prediction=feature_model.predict(feature_model.fit_model([],self.cutoff),model_setup())
        self.assertFalse(prediction["ranking_ready"])
        self.assertEqual(prediction["expected_net_R"],0)
        json.dumps(prediction,allow_nan=False)


class ModelV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cutoff = START + timedelta(days=142)
        rows = synthetic_rows()
        for row in rows:
            row["shadow_state"]["result_R"] = 1.
            if row["setup_json"]["components"]["features"]["rsi"] < 55:
                row["shadow_state"].update(status="cancelled", result_R=None)
        cls.observations, _ = model_dataset.dataset(rows, cls.cutoff)
        cls.artifact = feature_model.fit_model(cls.observations, cls.cutoff)

    def test_fill_model_learns_cancellations_on_held_out_weeks(self):
        evidence = self.artifact["validation"]["entry"]
        self.assertTrue(evidence["validated"])
        self.assertLess(evidence["brier"], evidence["baseline_brier"])
        self.assertGreater(evidence["selection"]["mean_delta_R_per_batch"], 0)
        self.assertGreater(len(evidence["reliability"]), 1)
        low = feature_model.predict(self.artifact, model_setup(35))
        high = feature_model.predict(self.artifact, model_setup(70))
        self.assertGreater(high["entry_probability"], low["entry_probability"])
        self.assertGreater(high["expected_net_R"], low["expected_net_R"])
        self.assertAlmostEqual(high["expected_net_R"], high["entry_probability"]*high["conditional_net_R"])
        self.assertEqual(high["prediction_basis"], "per_signal_opportunity")
        self.assertEqual(high["entry_status"], "validated")
        restored = json.loads(json.dumps(self.artifact, allow_nan=False))
        self.assertEqual(high, feature_model.predict(restored, model_setup(70)))

    def test_fill_labels_respect_arrival_cutoff_and_future_changes(self):
        cutoff = START + timedelta(days=70)
        before = feature_model.fit_model(self.observations, cutoff)
        changed = deepcopy(self.observations)
        for row in changed:
            if model_dataset.as_datetime(row["available_at"]) >= cutoff:
                row["cancelled"] = not row["cancelled"]
                row["target"] = 999
                row["features"] = [999]*6
        self.assertEqual(before, feature_model.fit_model(changed, cutoff))
        self.assertGreater(len(before["entry_training_ids"]), len(before["training_ids"]))

    def test_unvalidated_fill_model_uses_pooled_probability(self):
        artifact = deepcopy(self.artifact)
        artifact["validation"]["entry"]["validated"] = False
        low = feature_model.predict(artifact, model_setup(35))
        high = feature_model.predict(artifact, model_setup(70))
        self.assertEqual(low["entry_probability"], high["entry_probability"])
        self.assertEqual(low["entry_status"], "pooled")

    def test_better_entry_brier_without_selection_gain_stays_pooled(self):
        with patch.object(feature_model, "PREFERRED_TOP_SETUPS", 100):
            evidence = feature_model.validation_evidence(self.observations, self.cutoff)["entry"]
        self.assertLess(evidence["brier"], evidence["baseline_brier"])
        self.assertEqual(evidence["selection"]["mean_delta_R_per_batch"], 0)
        self.assertFalse(evidence["validated"])

    def test_single_class_and_missing_features_have_finite_fallback(self):
        filled = [r for r in self.observations if not r["cancelled"]]
        entry = feature_model.fit_entry(filled, self.cutoff)
        self.assertIsNone(entry["logistic"])
        self.assertTrue(0 < entry["prior"] < 1)
        setup = model_setup(); setup["components"] = {}
        prediction = feature_model.predict(self.artifact, setup)
        self.assertEqual(prediction["entry_probability"], self.artifact["entry"]["prior"])
        json.dumps(prediction, allow_nan=False)

    def test_recency_half_life_preserves_cluster_cap(self):
        rows, _ = model_dataset.dataset([signal_row(0, START), signal_row(1, START),
            signal_row(2, START+timedelta(days=90))], START+timedelta(days=92))
        weights = feature_model.sample_weights(rows, START+timedelta(days=90))
        self.assertAlmostEqual(sum(weights[:2]), .5)
        self.assertAlmostEqual(weights[2], 1.)
        self.assertAlmostEqual(sum(feature_model.sample_weights(rows)), 2.)

    def test_recency_tracks_reversal_and_stale_history_loses_readiness(self):
        rows = [signal_row(i, START+timedelta(days=i), result=-1 if i<90 else 1) for i in range(180)]
        observations, _ = model_dataset.dataset(rows, START+timedelta(days=182))
        cutoff = START+timedelta(days=182)
        aged = feature_model.fit_raw(observations, cutoff)
        equal = feature_model.fit_raw(observations)
        self.assertGreater(feature_model.pooled_prediction(aged["groups"], model_setup())[0],
                           feature_model.pooled_prediction(equal["groups"], model_setup())[0])
        stale = feature_model.fit_model(observations, cutoff+timedelta(days=1000))
        self.assertIsNone(stale["raw"]["ridge"])
        self.assertFalse(feature_model.predict(stale, model_setup())["ranking_ready"])

    def test_nonlinear_curvature_recovers_hump_shaped_response(self):
        rows = [signal_row(day*5+j, START+timedelta(days=day), rsi=rsi,
                           result=1-((rsi-50)/20)**2)
                for day in range(140) for j,rsi in enumerate((20,35,50,65,80))]
        observations, _ = model_dataset.dataset(rows, self.cutoff)
        artifact = feature_model.fit_model(observations, self.cutoff)
        self.assertGreater(artifact["validation"]["skill"], 0)
        center = feature_model.predict(artifact, model_setup(50))["conditional_net_R"]
        self.assertGreater(center, feature_model.predict(artifact, model_setup(20))["conditional_net_R"])
        self.assertGreater(center, feature_model.predict(artifact, model_setup(80))["conditional_net_R"])
        self.assertEqual(len(artifact["raw"]["ridge"]["coefficients"]), 8)
        vector = feature_model.expanded_features([50, 2, .1, .03, .04, 2.5])
        self.assertEqual(vector[-2:], [0., .06])

    def test_selection_gate_rejects_better_average_with_worse_bad_week(self):
        batches = []
        for week, outcomes in enumerate(((1., 5.), (-1., -2.))):
            batch = []
            for index, target in enumerate(outcomes):
                batch.append({"row": {"target": target, "champion_score": 0, "signal_id": str(index)},
                              "base": 1. if index == 0 else -1., "candidate": 1. if index == 1 else -1.})
            batches.append((str(week), batch))
        evidence = feature_model.selection_evidence(batches, "base", "candidate")
        self.assertGreater(evidence["mean_delta_R_per_batch"], 0)
        self.assertFalse(evidence["passed"])

    def test_lower_forecast_error_alone_cannot_enable_feature_correction(self):
        observations, _ = model_dataset.dataset(synthetic_rows(), self.cutoff)
        # Choosing the same complete opportunity set gives no selection uplift,
        # even though the feature model predicts individual outcomes more accurately.
        with patch.object(feature_model, "PREFERRED_TOP_SETUPS", 100):
            for row in observations:
                row["target"] += 10
            evidence = feature_model.validation_evidence(observations, self.cutoff)
        self.assertLess(evidence["feature_mae_R"], evidence["baseline_mae_R"])
        self.assertEqual(evidence["selection"]["mean_delta_R_per_batch"], 0)
        self.assertEqual(evidence["skill"], 0)


class ResearchEvaluationTests(unittest.TestCase):
    def test_rank_by_net_r_not_arbitrary_combined_score(self):
        observations,_=model_dataset.dataset([signal_row(0),signal_row(1)],START+timedelta(days=2))
        observations[0]["champion_score"]=100
        predictions={"signal-0":{"ranking_ready":True,"expected_net_R":.1,"approved":True},
                     "signal-1":{"ranking_ready":True,"expected_net_R":.9,"approved":True}}
        ranks=model_research.rankings(observations,predictions,top_k=1)
        self.assertTrue(ranks["signal-0"]["champion_selected"])
        self.assertTrue(ranks["signal-1"]["challenger_selected"])
        self.assertFalse(ranks["signal-0"]["challenger_selected"])

    def test_cold_or_unavailable_challenger_falls_back_to_champion(self):
        observations,_=model_dataset.dataset([signal_row(0,approved=False),signal_row(1)],START+timedelta(days=2))
        ranks=model_research.rankings(observations,{})
        self.assertTrue(all(r["champion_selected"]==r["challenger_selected"] and not r["ranking_ready"] for r in ranks.values()))

    def test_pending_candidate_prevents_complete_batch_cherry_picking(self):
        observations,_=model_dataset.dataset([signal_row(0,result=5),signal_row(1,result=None)],START+timedelta(days=2))
        entries=[{"batch_id":"one","observation":r,"prediction":{},"champion_selected":True,"challenger_selected":True} for r in observations]
        result=model_research.compare_batches(entries)
        self.assertEqual(result["pending_batches"],1)
        self.assertEqual(result["matched_batches"],0)
        self.assertEqual(result["challenger"]["total_R"],0)

    def test_opportunity_forecast_error_includes_cancelled_entries(self):
        rows = [signal_row(0), signal_row(1)]
        rows[1]["shadow_state"].update(status="cancelled", result_R=None)
        observations, _ = model_dataset.dataset(rows, START+timedelta(days=2))
        entries = [{"batch_id": "one", "observation": row,
                    "prediction": {"ranking_ready": True, "expected_net_R": .8},
                    "champion_selected": True, "challenger_selected": True} for row in observations]
        report = model_research.compare_batches(entries)
        self.assertEqual(report["prediction_count"], 2)
        self.assertAlmostEqual(report["prediction_mae_R"], .5)
        self.assertEqual(report["challenger"]["cancelled"], 1)

    def test_retrospective_fit_cutoff_precedes_each_test_week(self):
        rows=[signal_row(i,START+timedelta(days=i),result=1) for i in range(45)]
        result=model_research.chronological_evaluation(rows,START+timedelta(days=44,hours=8))
        self.assertEqual(result["mode"],"retrospective_research")
        self.assertEqual(result["folds"][0]["training_rows"],0)
        self.assertFalse(result["folds"][-1]["complete"])
        for fold in result["folds"]:
            self.assertEqual(fold["training_cutoff"],fold["start"])
            self.assertLessEqual(fold["training_rows"],(model_dataset.as_datetime(fold["start"])-START).days)

    def test_duplicate_candidate_predictions_use_first_observation(self):
        first=model_setup(40);later=model_setup(70)
        with patch.object(model_research,"read_signal_rows",return_value=[]), patch.object(model_research,"predict",side_effect=lambda artifact,s:{"rsi":s["components"]["features"]["rsi"]}):
            research=model_research.prepare_batch([first,later],START)
        self.assertEqual(len(research["predictions"]),1)
        self.assertEqual(next(iter(research["predictions"].values()))["rsi"],40)

    def test_research_read_failure_has_no_candidate_mutations(self):
        candidate=model_setup();before=deepcopy(candidate)
        with patch.object(model_research,"read_signal_rows",side_effect=RuntimeError("offline")):
            research=model_research.prepare_batch([candidate],START)
        self.assertEqual(candidate,before)
        self.assertIsNone(research["artifact"])
        self.assertFalse(next(iter(research["predictions"].values()))["ranking_ready"])


if __name__ == "__main__":
    unittest.main()
