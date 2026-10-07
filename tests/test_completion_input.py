"""Production completion-first input contracts; no live API calls."""
import json
import unittest

import test_fast_delegate_skill as helpers


class CompletionInputTests(unittest.TestCase):
    setUp = helpers.FastDelegateSkillTests.setUp
    tearDown = helpers.FastDelegateSkillTests.tearDown
    load = helpers.FastDelegateSkillTests.load
    write_agent = helpers.FastDelegateSkillTests.write_agent
    write_catalog = helpers.FastDelegateSkillTests.write_catalog
    fit_setup = helpers.FastDelegateSkillTests.fit_setup
    stub_jev = helpers.FastDelegateSkillTests.stub_jev

    def test_default_route_captures_completion_context_once_and_keeps_low_fit(self):
        fdel = self.fit_setup()
        sent = self.stub_jev(fdel, {"model-a": (0, {0: 1}), "model-b": (4, {4: 1})})
        task = {"deliverable": "Fix parser and verify complete output",
                "acceptance": ["parser tests pass", "output preserves all records"],
                "difficulty": 3, "context": "offline verification only",
                "owned_paths": ["parser.py"], "access": "write"}
        result = fdel.route(task)
        self.assertEqual(len(sent), 1)
        body = sent[0]
        state = body["state"]
        self.assertEqual(state["routing_objective"]["secondary"],
                         ["cost", "fresh quota headroom", "recent route health",
                          "model/provider usage distribution when supplied and fresh"])
        objective = state["routing_objective"]
        self.assertIn("sufficient capability", objective["adequacy"])
        self.assertIn("never random or unqualified", objective["balance"])
        self.assertIn("missing or stale is unknown, not zero", objective["evidence_policy"])
        self.assertIn("no fabricated usage or aggregate headroom", objective["evidence_policy"])
        self.assertIn("do not always choose", objective["selection_policy"])
        self.assertIn("all acceptance", state["routing_objective"]["primary"])
        for key, value in task.items():
            self.assertEqual(state["task"][key], value)
        cache = state["task"]["cached_input_estimate"]
        self.assertIsNone(cache["est_cached_input_share"])
        self.assertEqual(cache["status"], "unknown")
        self.assertFalse(cache["cache_hit_verified"])
        self.assertEqual(len(body["questions"]), len(fdel.QUESTIONS) + 2)
        prompt = body["questions"]["fit_c0"]["instructions"]
        for phrase in ("FULL", "ALL", "repair and verification", "higher-priced",
                       "not actual success probabilities", "main model owns actual selection",
                       "not maximum capability", "not zero", "only when actually supplied",
                       "ties or close adequate choices", "never force random diversity",
                       "unobserved model is not incapable", "1/1 versus 0/1",
                       "bounded, verifiable, low-risk", "what a cheaper alternative lacks",
                       "Unknown quota is not healthy quota", "supplied as fresh facts",
                       "Do not always favor", "not prestige"):
            self.assertIn(phrase, prompt)
        self.assertEqual({r["id"] for r in result["recommendations"]}, {"agent-a", "agent-b"})
        self.assertTrue(all(r["review_required"] for r in result["recommendations"]))
        self.assertEqual(result["candidate"]["id"], "agent-a")
        self.assertEqual(result["recommendations"][0]["id"], "agent-b")
        json.dumps(body, allow_nan=False)

    def test_captured_request_preserves_pool_evidence_without_inventing_usage(self):
        fdel = self.fit_setup()
        sent = self.stub_jev(fdel, {"model-a": (3, {3: 1}), "model-b": (4, {4: 1})})
        original = fdel.build_jev_request
        fresh = {"availability": "available", "freshness": "fresh", "live": True,
                 "snapshot": {"five_hour": {"used_percentage": 30}}}
        stale = {"availability": "unknown", "freshness": "stale", "live": False}

        def build(task, slots, bands, stats, context_files, quota_sources, quota_by_pool, catalog_info):
            scoped = [(cid, dict(candidate, billing={"pool": cid}))
                      for cid, candidate in slots]
            return original(task, scoped, bands, stats, context_files,
                            {"c0": fresh, "c1": stale}, {}, catalog_info)

        fdel.build_jev_request = build
        fdel.route({"deliverable": "Verify bounded parser change", "acceptance": ["tests pass"]})
        self.assertEqual(len(sent), 1)
        state = sent[0]["state"]
        self.assertEqual(state["candidates"]["c0"]["quota"]["source"], fresh)
        self.assertEqual(state["candidates"]["c1"]["quota"]["source"], stale)
        for candidate in state["candidates"].values():
            self.assertEqual(candidate["observed_family_profile"]["total"], 0)
            self.assertNotIn("recent_records", candidate)
            self.assertNotIn("usage_distribution", candidate)
        objective = state["routing_objective"]
        self.assertIn("unknown quota is not healthy", objective["evidence_policy"])
        self.assertIn("unobserved is not incapable", objective["history_policy"])
        self.assertIn("without compromising full acceptance", objective["exploration"])
        self.assertNotIn("aggregate_headroom", state)
        json.dumps(sent[0], allow_nan=False)

    def test_numeric_evidence_bounds_and_unknown_are_json_safe(self):
        fdel = self.load()
        for difficulty, share in ((0, 0), (4, 1), (2.5, .4)):
            state = fdel.task_semantic_state({"difficulty": difficulty, "est_cached_input_share": share})
            self.assertEqual(state["difficulty"], difficulty)
            cache = state["cached_input_estimate"]
            self.assertEqual(cache["est_cached_input_share"], share)
            self.assertEqual(cache["basis"], "caller-supplied pricing assumption")
            self.assertFalse(cache["cache_hit_verified"])
            self.assertFalse(cache["timestamp_is_cache_evidence"])
            json.dumps(state, allow_nan=False)
        for value in (None, True, "3", -1, 5, float("inf"), float("-inf"), float("nan")):
            state = fdel.task_semantic_state({"difficulty": value, "est_cached_input_share": value})
            self.assertIsNone(state["difficulty"])
            self.assertIsNone(state["cached_input_estimate"]["est_cached_input_share"])
            self.assertEqual(state["cached_input_estimate"]["status"], "unknown")
            json.dumps(state, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
