"""Acceptance checks for the routing-only recommendation API."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fast_delegate_skill as fixtures


class RoutingOnlyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.FastDelegateSkillTests()
        self.fixture.setUp()
        self.fdel = self.fixture.load()

    def tearDown(self):
        self.fixture.tearDown()

    def assert_recommendation_only(self, result, selector, model_id, candidate_id):
        self.assertEqual(result["decision"], "delegate")
        candidate = result["candidate"]
        self.assertEqual(candidate["id"], candidate_id)
        self.assertEqual(candidate["model_selector"], selector)
        self.assertEqual(candidate["model_id"], model_id)
        forbidden = {"handoff", "handoff_path", "prompt", "task_name", "fork_turns", "message", "spawn"}
        self.assertFalse(forbidden.intersection(result))
        self.assertFalse(forbidden.intersection(candidate))
        brief = self.fdel.brief_output(result)
        self.assertFalse(forbidden.intersection(brief))
        self.assertFalse(forbidden.intersection(brief.get("candidate", {})))
        self.assertIn("route_id", result)
        self.assertFalse((self.fdel.LEDGER.parent / "handoffs").exists())

    def test_claude_returns_selector_and_underlying_model_without_handoff(self):
        harness = {**fixtures.FAKE_HARNESS, "builtin": [{"id": "worker", "model_selector": "short-worker",
                                                  "model_name": "model-worker"}],
                   "default_lead": "lead-model"}
        self.fdel.load_harness = lambda: harness
        self.fixture.write_catalog({"model-worker": fixtures.entry(1, 1), "lead-model": fixtures.entry(5, 5)})
        result = self.fdel.route({"deliverable": "x", "acceptance": ["pytest passes"]},
                                 use_jev=False, harness="claude", spawnable=["short-worker"], lead="lead-model")
        self.assert_recommendation_only(result, "short-worker", "model-worker", "worker")

    def test_codex_returns_selector_and_underlying_model_without_handoff(self):
        self.fixture.write_catalog({"model-worker": fixtures.entry(1, 1), "lead-model": fixtures.entry(5, 5)})
        result = self.fdel.route({"deliverable": "x", "acceptance": ["pytest passes"]},
                                 use_jev=False, harness="codex", spawnable=["model-worker"], lead="lead-model")
        self.assert_recommendation_only(result, "model-worker", "model-worker", "model-worker")


    def test_placeholder_selector_keeps_underlying_model_identity(self):
        self.fdel.load_harness = lambda: {**fixtures.FAKE_HARNESS, "model_id_placeholders": ["haiku"]}
        self.fixture.write_agent("custom-haiku", model="haiku", description="Delegate work to provider/qwen-model for parsing.")
        self.fixture.write_agent("real-haiku", model="provider/haiku-model", description="Genuine Haiku model.")
        self.fixture.write_catalog({"provider/qwen-model": fixtures.entry(1, 1),
                                   "provider/haiku-model": fixtures.entry(2, 2),
                                   "model-x": fixtures.entry(5, 5)})
        candidates, _warnings = self.fdel.discover()
        by_id = {candidate["id"]: candidate for candidate in candidates}
        self.assertEqual(by_id["custom-haiku"]["model_selector"], "custom-haiku")
        self.assertEqual(by_id["custom-haiku"]["model_id"], "provider/qwen-model")
        self.assertEqual(by_id["real-haiku"]["model_id"], "provider/haiku-model")
        self.assertEqual(by_id["custom-haiku"]["price"], {"input_per_m": 1.0, "output_per_m": 1.0})


    def test_reasoning_effort_is_model_configuration_not_spawn_data(self):
        self.fixture.write_catalog({"model-worker": fixtures.entry(1, 1), "lead-model": fixtures.entry(5, 5)})
        self.fdel.load_harness = lambda: {**fixtures.FAKE_HARNESS, "harnesses": {"codex": {
            "model_configuration": {"model-worker": {"reasoning_effort": "high"}}}}}
        result = self.fdel.route({"deliverable": "x", "acceptance": ["pytest passes"]},
                                 use_jev=False, harness="codex", spawnable=["model-worker"], lead="lead-model")
        candidate = result["candidate"]
        self.assertEqual(candidate["model_configuration"], {"reasoning_effort": "high"})
        self.assertNotIn("spawn", candidate)
        self.assertNotIn("spawn_agent", json.dumps(result))

    def ranked_route(self, fits, extra=None):
        catalog = {"model-x": fixtures.entry(100, 100)}
        for index, model in enumerate(fits):
            self.fixture.write_agent(model, model=model)
            catalog[model] = fixtures.entry(index + 1, index + 1)
        self.fixture.write_catalog(catalog)
        self.fdel = self.fixture.load(harness={**fixtures.FAKE_HARNESS, "default_lead": "builtin-x"})
        self.fixture.stub_jev(self.fdel, fits, extra=extra)
        return self.fdel.route({"deliverable": "x", "acceptance": ["pytest passes"]})

    def test_all_judged_score_ranked_not_cheapest_or_capped(self):
        result = self.ranked_route({"cheap": (2.5, {2: 1}), "middle": (3, {3: 1}),
                                   "costlier": (3.5, {3: .5, 4: .5}), "expensive": (4, {4: 1})})
        rows = result["recommendations"]
        self.assertEqual([r["id"] for r in rows], ["expensive", "costlier", "middle", "cheap"])
        self.assertEqual(result["candidate"]["id"], "cheap")
        self.assertEqual(self.fdel.brief_output(result)["recommendations"], rows)
        self.assertTrue(all(r["review_required"] for r in rows))
        for row in rows:
            self.assertEqual(row["model_selector"], row["id"])
            self.assertEqual(row["model_id"], row["id"])
            self.assertIn("model_configuration", row)
            self.assertIn("price", row)
            self.assertIn("fit_probabilities", row)

    def test_low_scores_and_history_are_advisory_evidence(self):
        self.fixture.record(self.fdel, "zero", "general", "rejected", 4)
        result = self.ranked_route({"good": (3, {3: 1}), "low": (.1, {0: .9, 4: .1}), "zero": (0, {0: 1})})
        rows = {r["id"]: r for r in result["recommendations"]}
        self.assertEqual(set(rows), {"good", "low", "zero"})
        self.assertEqual(rows["zero"]["family_history"]["accepted"], 0)
        self.assertEqual(rows["zero"]["family_history"]["total"], 4)
        self.assertEqual(rows["zero"]["family_history"]["acceptance_rate"], 0)
        self.assertTrue(all(r["review_required"] for r in rows.values()))
        for r in rows.values():
            self.assertFalse({"required_fit", "fit_qualified", "fit_status", "fit_mass", "fit_mass_floor", "fit_mass_gate"}.intersection(r))

    def test_probability_only_score_zero_and_tie_order(self):
        f = self.fdel
        candidates = [{"id": cid, "model_id": cid, "catalog_match": "jev",
                       "price": {"input_per_m": 1, "output_per_m": 1},
                       "context": 10000, "supports_tools": True, "cost_usd": cost,
                       "effective_cost": cost} for cid, cost in [("z", 2), ("b", 1), ("a", 1), ("zero", 3)]]
        slots = f.jev_slots(candidates)
        semantic = {}
        for slot, c in slots:
            semantic[f"fit_{slot}_probs"] = {2: 1}
            if c["id"] != "z":
                semantic[f"fit_{slot}"] = 0 if c["id"] == "zero" else 2
        rows = f.recommendation_records({}, candidates, semantic,
                                       {"decision": "delegate", "required_fit": 2}, {},
                                       f.price_bands(candidates), .5, .25)
        self.assertEqual([r["id"] for r in rows], ["a", "b", "z", "zero"])
        self.assertEqual(rows[2]["fit_score"], 2)
        self.assertEqual(rows[2]["fit_score_basis"], "probability_expectation")
        self.assertEqual(rows[-1]["fit_score"], 0)
        self.assertEqual(rows[-1]["fit_score_basis"], "jev_raw_score")
        self.assertTrue(rows[-1]["review_required"])

    def test_caller_consumes_list_after_availability_failure_without_rejudging(self):
        result = self.ranked_route({"next": (3, {3: 1}), "first": (4, {4: 1}),
                                   "review": (2, {0: .7, 3: .3})})
        def unexpected_jev_call(body):
            self.fail("Caller substitution must reuse the existing ranked evidence")
        self.fdel.jev = unexpected_jev_call

        # Test-only caller simulation: availability failures arrive from actual calls;
        # the router neither probes nor executes workers.
        def caller_select(unavailable, accepted):
            return next((r["id"] for r in result["recommendations"]
                         if r["id"] not in unavailable and r["id"] in accepted), None)

        self.assertIsNone(caller_select(set(), set()))  # No automatic acceptance, even at score four.
        self.assertEqual(caller_select(set(), {"review"}), "review")  # Lead can accept lower fit.
        self.assertEqual(caller_select(set(), {"first", "next"}), "first")
        self.assertEqual(caller_select({"first"}, {"first", "next"}), "next")
        self.assertIsNone(caller_select({"first", "next"}, {"first", "next"}))
        self.assertIsNone(caller_select({r["id"] for r in result["recommendations"]}, {"first", "next"}))

    def test_unjudged_and_incomplete_metadata_are_not_added(self):
        f = self.fdel
        candidates = [{"id": cid, "catalog_match": match, "model_id": cid,
                       "price": {"input_per_m": 1, "output_per_m": 1},
                       "context": 10000, "supports_tools": True, "cost_usd": 1,
                       "effective_cost": 1} for cid, match in
                      [("verified", "jev"), ("override", "none"), ("unknown", "none"), ("unjudged", "jev")]]
        semantic = {}
        for slot, c in f.jev_slots(candidates):
            if c["id"] != "unjudged":
                semantic[f"fit_{slot}"] = 3
                semantic[f"fit_{slot}_probs"] = {3: 1}
        decision = {"decision": "delegate", "required_fit": 2,
                    "model_information_override": {"candidate_id": "override", "reason": "local evidence"}}
        rows = f.recommendation_records({}, candidates, semantic, decision, {},
                                       f.price_bands(candidates), .5, .25)
        self.assertEqual({r["id"] for r in rows}, {"verified", "override"})
        by_id = {r["id"]: r for r in rows}
        self.assertTrue(by_id["override"]["review_required"])
        self.assertTrue(by_id["verified"]["review_required"])
        self.assertNotIn("model_information_override", by_id["verified"])

    def test_low_independence_and_zero_fit_remain_advisory(self):
        for fits, extra in [({"worker": (4, {4: 1})}, {"independent": .1}),
                            ({"worker": (0, {0: 1})}, {})]:
            result = self.ranked_route(fits, extra)
            self.assertEqual(result["decision"], "delegate")
            self.assertEqual(len(result["recommendations"]), 1)
            self.assertTrue(result["recommendations"][0]["review_required"])

    def test_heuristic_outage_is_explicit_without_invented_fit(self):
        self.fixture.write_catalog({"model-worker": fixtures.entry(1, 1), "lead-model": fixtures.entry(5, 5)})
        self.fdel.jev = lambda body: (None, "unavailable")
        result = self.fdel.route({"deliverable": "x", "acceptance": ["pytest passes"]},
                                 harness="codex", spawnable=["model-worker"], lead="lead-model")
        row = result["recommendations"][0]
        self.assertEqual(row["ranking_basis"], "heuristic")
        self.assertEqual(row["heuristic_basis"], "metadata_safe_cost")
        self.assertIsNone(row["fit_score"])
        self.assertNotIn("fit_qualified", row)
        self.assertIsNone(row["fit_probabilities"])

    def test_no_handoff_artifact_helpers_or_execution_fields_in_shipped_config(self):
        source = (Path(__file__).resolve().parents[1] / "skills/fast-delegate/scripts/fdel.py").read_text()
        for obsolete in ("def handoff(", "def write_handoff(", "handoff_path", "task_name_from_deliverable"):
            self.assertNotIn(obsolete, source)
        harness = json.loads((Path(__file__).resolve().parents[1] / "skills/fast-delegate/harness.json").read_text())
        self.assertNotIn("spawn_model_placeholder", harness)
        self.assertEqual(harness["model_id_placeholders"], ["haiku"])
        self.assertNotIn("spawn_agent call", harness["note"])
        self.assertIn("model configuration metadata", harness["note"])
        for candidate in harness["builtin"]:
            self.assertNotIn("subagent_type", candidate)
            self.assertNotIn("tool", candidate)
            self.assertIn("model_selector", candidate)


if __name__ == "__main__":
    unittest.main()
