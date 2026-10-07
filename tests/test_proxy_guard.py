"""Proxy guard and served-model check for fdel.py (stdlib only, offline)."""

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/fast-delegate/scripts/fdel.py"

PROXY_ROUTE = "proxy-claude-native--worker-model"
PROXIED_ENV = {"ANTHROPIC_BASE_URL": "http://127.0.0.1:1", "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1"}
TASK = {"deliverable": "x", "acceptance": ["pytest passes"]}


def catalog_entry(price):
    return {"max_input_tokens": 100000, "supports_function_calling": True,
            "input_cost_per_token": price / 1e6, "output_cost_per_token": price / 1e6}


class ProxyGuardTestCase(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        self._cwd = os.getcwd()
        self.tmp = Path(tempfile.mkdtemp())
        self.agents = self.tmp / "config" / "agents"
        self.agents.mkdir(parents=True)
        os.chdir(self.tmp)
        for name in list(os.environ):
            if name.startswith("TYPESAFE_") or name in (
                    "ANTHROPIC_BASE_URL", "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY", "FAST_DELEGATE_PROXY"):
                del os.environ[name]
        os.environ["FAST_DELEGATE_STATE"] = str(self.tmp / "state")
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.tmp / "config")
        os.environ["FAST_DELEGATE_CATALOG"] = str(self.tmp / "catalog.json")
        (self.tmp / "catalog.json").write_text(json.dumps({
            "worker-model": catalog_entry(1), "plain-model": catalog_entry(2), "lead-model": catalog_entry(50)}))
        self.write_agent("proxy-w", PROXY_ROUTE)
        self.write_agent("plain-w", "plain-model")

    def tearDown(self):
        os.chdir(self._cwd)
        os.environ.clear()
        os.environ.update(self._env)

    def write_agent(self, name, model):
        (self.agents / f"{name}.md").write_text(
            f'---\nname: {name}\ndescription: "Test agent."\nmodel: {model}\n---\n\nBody.\n')

    def load(self):
        spec = importlib.util.spec_from_file_location("fdel_proxy_guard", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        def no_network(req, timeout=None):
            raise module.urllib.error.URLError("network disabled in tests")
        module.urllib.request.urlopen = no_network
        module.load_harness = lambda: {"model_id_placeholders": ["ph"]}
        discover = module.discover

        def verified_discover(*args, **kwargs):
            candidates, warnings = discover(*args, **kwargs)
            for c in candidates:  # catalog identity is fixture-confirmed (no Jev key offline)
                if c.get("catalog_match", "").startswith("unverified-"):
                    c.update(catalog_match="jev", match_score=1.0, match_entries=[])
            return candidates, warnings
        module.discover = verified_discover
        return module

    def route(self, fdel, task=None):
        return fdel.route(task or TASK, use_jev=False, lead="lead-model")

    def run_cli(self, *args):
        env = {k: v for k, v in os.environ.items() if k != "FAST_DELEGATE_PROXY"}
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                              env=env, cwd=self.tmp)

    def record(self, *extra, candidate="proxy-w"):
        return self.run_cli("record", "--candidate", candidate, "--family", "f", "--outcome", "accepted", *extra)

    def ledger_rows(self):
        path = self.tmp / "state" / "outcomes.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.is_file() else []

    def write_transcript(self, *lines):
        path = self.tmp / "agent.jsonl"
        path.write_text("\n".join(lines) + "\n")
        return str(path)


def assistant(model):
    return json.dumps({"type": "assistant", "message": {"role": "assistant", "model": model, "content": []}})


class DetectionTests(ProxyGuardTestCase):
    def test_plain_session_is_not_proxied(self):
        fdel = self.load()
        self.assertFalse(fdel.proxy_active({}))
        self.assertFalse(fdel.proxy_active({"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"}))

    def test_gateway_session_is_proxied(self):
        fdel = self.load()
        self.assertTrue(fdel.proxy_active(dict(PROXIED_ENV)))
        self.assertTrue(fdel.proxy_active({"ANTHROPIC_BASE_URL": "http://h",
                                           "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1"}))

    def test_a_single_signal_is_not_enough(self):
        fdel = self.load()
        self.assertFalse(fdel.proxy_active({"ANTHROPIC_BASE_URL": "http://some-other-gateway"}))
        self.assertFalse(fdel.proxy_active({"CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1"}))

    def test_override_wins_either_way(self):
        fdel = self.load()
        self.assertTrue(fdel.proxy_active({"FAST_DELEGATE_PROXY": "1"}))
        self.assertFalse(fdel.proxy_active({**PROXIED_ENV, "FAST_DELEGATE_PROXY": "0"}))

    def test_invalid_override_is_a_descriptive_error(self):
        fdel = self.load()
        with self.assertRaisesRegex(SystemExit, "FAST_DELEGATE_PROXY"):
            fdel.proxy_active({"FAST_DELEGATE_PROXY": "maybe"})

    def test_defaults_to_the_process_environment(self):
        fdel = self.load()
        self.assertFalse(fdel.proxy_active())
        os.environ.update(PROXIED_ENV)
        self.assertTrue(fdel.proxy_active())

    def test_proxy_route_uses_the_prefix_strip_proxy_prefix_removes(self):
        fdel = self.load()
        self.assertEqual(fdel.proxy_route(PROXY_ROUTE), PROXY_ROUTE)
        self.assertEqual(fdel.strip_proxy_prefix(PROXY_ROUTE), "worker-model")
        for plain in ("haiku", "plain-model", "", None, "provider/model"):
            self.assertIsNone(fdel.proxy_route(plain))


class RouteTests(ProxyGuardTestCase):
    def test_inactive_proxy_drops_proxy_routed_candidate_before_ranking(self):
        os.environ["FAST_DELEGATE_PROXY"] = "0"
        result = self.route(self.load())
        self.assertEqual(result["decision"], "delegate")
        self.assertEqual(result["candidate"]["id"], "plain-w")  # proxy-w is cheaper but was dropped
        dropped = [r for r in result["reasons"] if r.startswith("drop proxy-w:")]
        self.assertEqual(len(dropped), 1)
        self.assertIn("placeholder model", dropped[0])
        self.assertIn("worker-model", dropped[0])
        self.assertFalse([r for r in result["reasons"] if r.startswith("drop plain-w")])

    def test_active_proxy_keeps_proxy_routed_candidate(self):
        os.environ["FAST_DELEGATE_PROXY"] = "1"
        result = self.route(self.load())
        self.assertEqual(result["candidate"]["id"], "proxy-w")
        self.assertFalse([r for r in result["reasons"] if "placeholder" in r])

    def test_detected_proxy_keeps_proxy_routed_candidate(self):
        os.environ.update(PROXIED_ENV)
        self.assertEqual(self.route(self.load())["candidate"]["id"], "proxy-w")

    def test_every_candidate_proxy_routed_is_direct_with_a_truthful_reason(self):
        (self.agents / "plain-w.md").unlink()
        os.environ["FAST_DELEGATE_PROXY"] = "0"
        result = self.route(self.load())
        self.assertEqual(result["decision"], "direct")
        self.assertTrue(any(r.startswith("drop proxy-w:") for r in result["reasons"]))
        self.assertIn("proxy not active", result["reasons"][-1])

    def test_override_of_a_proxy_dropped_candidate_is_refused_with_the_cause(self):
        os.environ["FAST_DELEGATE_PROXY"] = "0"
        override = {"candidate_id": "proxy-w", "reason": "test"}
        with self.assertRaisesRegex(SystemExit, "proxy"):
            self.load().route(TASK, use_jev=False, lead="lead-model", model_information_override=override)


class DiscoverTests(ProxyGuardTestCase):
    def flags(self, fdel):
        candidates, _ = fdel.discover()
        return {c["id"]: (c["proxy_routed"], c["proxy_active"]) for c in candidates}

    def test_candidates_are_flagged_with_proxy_state(self):
        fdel = self.load()
        os.environ["FAST_DELEGATE_PROXY"] = "0"
        self.assertEqual(self.flags(fdel), {"proxy-w": (True, False), "plain-w": (False, False)})
        os.environ["FAST_DELEGATE_PROXY"] = "1"
        self.assertEqual(self.flags(fdel), {"proxy-w": (True, True), "plain-w": (False, True)})

    def test_proxy_route_and_model_id_are_reported(self):
        candidates, _ = self.load().discover()
        proxied = next(c for c in candidates if c["id"] == "proxy-w")
        self.assertEqual((proxied["proxy_route"], proxied["model_id"]), (PROXY_ROUTE, "worker-model"))

    def test_json_output_carries_the_flags(self):
        fdel = self.load()
        os.environ["FAST_DELEGATE_PROXY"] = "0"
        out = io.StringIO()
        fdel.print_discovery(*fdel.discover(), as_json=True, out=out)
        payload = json.loads(out.getvalue())
        self.assertIs(payload["proxy_active"], False)
        self.assertEqual({c["id"]: c["proxy_routed"] for c in payload["candidates"]},
                         {"proxy-w": True, "plain-w": False})

    def test_cli_discover_json_reflects_override(self):
        env = {**os.environ, "FAST_DELEGATE_PROXY": "1"}
        proc = subprocess.run([sys.executable, str(SCRIPT), "discover", "--json", "--harness", "claude"],
                              capture_output=True, text=True, env=env, cwd=self.tmp, check=True)
        payload = json.loads(proc.stdout)
        self.assertIs(payload["proxy_active"], True)
        self.assertTrue(next(c for c in payload["candidates"] if c["id"] == "proxy-w")["proxy_routed"])


class ServedModelTests(ProxyGuardTestCase):
    def test_normalization_ignores_prefix_provider_date_case_and_dots(self):
        fdel = self.load()
        for served in (PROXY_ROUTE, "worker-model", "Worker-Model", "anthropic/worker-model",
                       "worker-model-20260101", "worker-model[1m]"):
            self.assertEqual(fdel.served_model_mismatches("worker-model", [served]), [], served)
        self.assertEqual(fdel.served_model_mismatches("gpt-5.6-luna", ["proxy-claude-native--gpt-5-6-luna"]), [])
        self.assertEqual(fdel.served_model_mismatches("claude-haiku-4-5", ["claude-haiku-4-5-20251001"]), [])
        self.assertEqual(fdel.served_model_mismatches("gpt-6-luna", ["claude-haiku-4-5"]), ["claude-haiku-4-5"])

    def test_family_drops_version_and_date_tokens_only(self):
        fdel = self.load()
        family = fdel.served_model_family
        self.assertEqual(family("claude-sonnet-5"), "claude-sonnet")
        self.assertEqual(family("claude-sonnet-5-5"), "claude-sonnet")
        self.assertEqual(family("claude-haiku-4-5-20251001"), "claude-haiku")
        self.assertEqual(family("claude-3-5-sonnet"), "claude-sonnet")
        self.assertEqual(family("gpt-6-luna"), family("gpt-6.1-luna"))
        self.assertEqual(family("proxy-claude-native--gpt-6-luna"), "gpt-luna")
        self.assertEqual(family("gpt-4o"), "gpt-4o")  # letters+digits is part of the name
        self.assertNotEqual(family("gpt-6-luna"), family("gpt-6-sol"))
        self.assertEqual(family("2026"), "2026")  # nothing but digits is its own family

    def test_version_difference_in_a_family_is_drift_not_mismatch(self):
        fdel = self.load()
        self.assertEqual(fdel.served_model_mismatches("claude-sonnet-5", ["claude-sonnet-5-5"]), [])
        self.assertEqual(fdel.served_model_drifts("claude-sonnet-5", ["claude-sonnet-5-5"]), ["claude-sonnet-5-5"])
        self.assertEqual(fdel.served_model_drifts("claude-sonnet-5", ["claude-sonnet-5-20260101"]), [])
        self.assertEqual(fdel.served_model_drifts("claude-sonnet-5", ["claude-haiku-4-5"]), [])
        self.assertEqual(fdel.served_model_mismatches("gpt-6-luna", ["claude-haiku-4-5-20251001"]),
                         ["claude-haiku-4-5-20251001"])
        self.assertEqual(fdel.served_model_drifts("gpt-6-luna", ["gpt-6.1-luna"]), ["gpt-6.1-luna"])

    def test_declared_model_ids_cover_agents_and_builtins(self):
        fdel = self.load()
        fdel.load_harness = lambda: {"model_id_placeholders": ["ph"],
                                     "builtin": [{"id": "b", "model_name": "builtin-model"}]}
        ids = fdel.declared_model_ids()
        self.assertEqual((ids["proxy-w"], ids["plain-w"], ids["b"]), ("worker-model", "plain-model", "builtin-model"))

    def test_record_accepts_a_matching_served_model_and_stores_it(self):
        proc = self.record("--served-model", PROXY_ROUTE)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["recorded"]["served_model"], PROXY_ROUTE)
        rows = self.ledger_rows()
        self.assertEqual([r["served_model"] for r in rows], [PROXY_ROUTE])
        self.assertNotIn("served_model_mismatch", rows[0])

    def test_route_only_codex_candidate_checks_saved_identity(self):
        fdel = self.load()
        result = fdel.route(TASK, use_jev=False, lead="lead-model", harness="codex",
                            spawnable=["worker-model", "plain-model"])
        route_id = result["route_id"]
        saved = fdel.find_route_record(route_id)
        self.assertEqual(saved["candidate_model_ids"]["worker-model"], "worker-model")
        self.assertNotIn("worker-model", fdel.declared_model_ids())
        proc = self.record("--route", route_id, "--served-model", "worker-model", candidate="worker-model")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.record("--route", route_id, "--served-model", "claude-haiku-4-5", candidate="worker-model")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("served model mismatch", proc.stderr)
        self.assertEqual(len(self.ledger_rows()), 1)

    def test_route_identity_survives_later_agent_changes(self):
        os.environ["FAST_DELEGATE_PROXY"] = "1"
        fdel = self.load()
        route_id = self.route(fdel)["route_id"]
        self.write_agent("proxy-w", "different-family")
        proc = self.record("--route", route_id, "--served-model", "worker-model")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.record("--route", route_id, "--served-model", "different-family")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("served model mismatch", proc.stderr)

    def test_route_uses_confirmed_catalog_identity_for_descriptive_model(self):
        fdel = self.load()
        candidate = {"id": "descriptive-agent", "model_id": "The worker model",
                     "catalog_id": "provider/worker-model", "catalog_match": "jev"}
        route_id = fdel.write_route_record(TASK, {"pick": candidate}, None)
        proc = self.record("--route", route_id, "--served-model", "worker-model", candidate="descriptive-agent")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.record("--route", route_id, "--served-model", "claude-haiku-4-5", candidate="descriptive-agent")
        self.assertNotEqual(proc.returncode, 0)

    def test_unverified_catalog_identity_does_not_replace_declared_model(self):
        fdel = self.load()
        candidate = {"id": "custom", "model_id": "worker-model",
                     "catalog_id": "other-family", "catalog_match": "unverified-normalized"}
        route_id = fdel.write_route_record(TASK, {"pick": candidate}, None)
        self.assertEqual(fdel.find_route_record(route_id)["candidate_model_ids"], {"custom": "worker-model"})

    def test_route_resolves_builtin_alias_even_without_confirmed_catalog(self):
        fdel = self.load()
        fdel.load_harness = lambda: {"builtin": [{"id": "worker-alias", "model_name": "worker-model"}]}
        candidate = {"id": "custom", "model_id": "worker-alias", "catalog_id": None}
        route_id = fdel.write_route_record(TASK, {"pick": candidate}, None)
        self.assertEqual(fdel.find_route_record(route_id)["candidate_model_ids"], {"custom": "worker-model"})
        proc = self.record("--route", route_id, "--served-model", "worker-model", candidate="custom")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_custom_agent_builtin_alias_accepts_its_served_family(self):
        self.write_agent("reviewer", "sonnet")
        proc = self.record("--served-model", "claude-sonnet-5", candidate="reviewer")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.record("--served-model", "claude-sonnet-5-5", candidate="reviewer")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIs(self.ledger_rows()[-1]["served_model_version_drift"], True)
        proc = self.record("--served-model", "claude-haiku-4-5", candidate="reviewer")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("served model mismatch", proc.stderr)

    def test_legacy_route_without_identity_uses_current_declaration(self):
        fdel = self.load()
        route_id = fdel.write_route_record(TASK, {"pick": {"id": "proxy-w"}}, None)
        proc = self.record("--route", route_id, "--served-model", "worker-model")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_record_refuses_a_mismatched_served_model(self):
        proc = self.record("--served-model", "claude-haiku-4-5-20251001")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("served model mismatch", proc.stderr)
        self.assertIn("worker-model", proc.stderr)
        self.assertIn("claude-haiku-4-5-20251001", proc.stderr)
        self.assertEqual(self.ledger_rows(), [])

    def test_force_records_a_mismatch_and_marks_it(self):
        proc = self.record("--served-model", "claude-haiku-4-5", "--force")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = self.ledger_rows()[0]
        self.assertEqual(row["served_model"], "claude-haiku-4-5")
        self.assertIs(row["served_model_mismatch"], True)

    def test_record_without_served_flags_is_unchanged(self):
        self.assertEqual(self.record().returncode, 0)
        self.assertNotIn("served_model", self.ledger_rows()[0])

    def test_empty_served_model_is_refused(self):
        proc = self.record("--served-model", " ")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--served-model is empty", proc.stderr)

    def test_transcript_models_are_distinct_ordered_and_skip_noise(self):
        fdel = self.load()
        path = self.write_transcript(
            json.dumps({"type": "user", "message": {"role": "user", "content": "hi", "model": "ignored"}}),
            "not json", "[1, 2]", json.dumps({"type": "assistant", "message": "text"}),
            assistant("model-a"), assistant("<synthetic>"), assistant("model-b"), assistant("model-a"),
            json.dumps({"type": "assistant", "message": {"role": "assistant", "content": []}}))
        self.assertEqual(fdel.transcript_models(path), ["model-a", "model-b"])

    def test_unreadable_transcript_is_a_descriptive_error(self):
        with self.assertRaisesRegex(SystemExit, "unreadable"):
            self.load().transcript_models(str(self.tmp / "missing.jsonl"))

    def test_record_from_a_matching_transcript(self):
        path = self.write_transcript(assistant(PROXY_ROUTE), assistant(PROXY_ROUTE))
        proc = self.record("--transcript", path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.ledger_rows()[0]["served_model"], PROXY_ROUTE)

    def test_record_refuses_a_transcript_served_by_the_placeholder_model(self):
        path = self.write_transcript(assistant("claude-haiku-4-5-20251001"))
        proc = self.record("--transcript", path)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("served model mismatch", proc.stderr)
        self.assertEqual(self.ledger_rows(), [])

    def test_one_mismatching_model_in_a_transcript_refuses(self):
        path = self.write_transcript(assistant(PROXY_ROUTE), assistant("claude-haiku-4-5"))
        self.assertNotEqual(self.record("--transcript", path).returncode, 0)

    def test_transcript_without_models_is_refused_unless_forced(self):
        path = self.write_transcript(json.dumps({"type": "user", "message": {"role": "user"}}))
        proc = self.record("--transcript", path)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("no assistant message.model", proc.stderr)
        self.assertEqual(self.record("--transcript", path, "--force").returncode, 0)
        self.assertNotIn("served_model", self.ledger_rows()[0])

    def test_served_model_and_transcript_are_combined(self):
        path = self.write_transcript(assistant("claude-haiku-4-5"))
        proc = self.record("--served-model", PROXY_ROUTE, "--transcript", path)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("claude-haiku-4-5", proc.stderr)

    def test_same_family_new_version_records_with_drift_flag_and_warning(self):
        proc = self.record("--served-model", "claude-sonnet-5-5", candidate="sonnet")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = self.ledger_rows()[0]
        self.assertEqual(row["served_model"], "claude-sonnet-5-5")
        self.assertIs(row["served_model_version_drift"], True)
        self.assertNotIn("served_model_mismatch", row)
        warning = [line for line in proc.stderr.splitlines() if line.startswith("warning:")]
        self.assertEqual(len(warning), 1)
        for text in ("claude-sonnet-5", "claude-sonnet-5-5", "pricing", "declared catalog entry"):
            self.assertIn(text, warning[0])

    def test_drift_from_a_transcript_records_with_the_flag(self):
        path = self.write_transcript(assistant("claude-sonnet-5-5"))
        proc = self.record("--transcript", path, candidate="sonnet")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIs(self.ledger_rows()[0]["served_model_version_drift"], True)

    def test_cross_family_gpt_candidate_served_by_haiku_is_still_refused(self):
        self.write_agent("proxy-luna", "proxy-claude-native--gpt-6-luna")
        proc = self.record("--served-model", "claude-haiku-4-5-20251001", candidate="proxy-luna")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("served model mismatch", proc.stderr)
        self.assertEqual(self.ledger_rows(), [])
        self.assertEqual(self.record("--served-model", "gpt-6.1-luna", candidate="proxy-luna").returncode, 0)
        self.assertIs(self.ledger_rows()[0]["served_model_version_drift"], True)

    def test_builtin_alias_served_by_another_family_is_refused(self):
        for alias, other in (("sonnet", "claude-haiku-4-5"), ("haiku", "claude-sonnet-5-5"),
                             ("opus", "claude-sonnet-5-5"), ("fable", "claude-opus-5-5")):
            with self.subTest(alias=alias):
                proc = self.record("--served-model", other, candidate=alias)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("served model mismatch", proc.stderr)
        self.assertEqual(self.ledger_rows(), [])

    def test_builtin_alias_families_accept_newer_versions_with_drift(self):
        for alias, served in (("haiku", "claude-haiku-4-6-20260301"), ("sonnet", "claude-sonnet-5-5"),
                              ("opus", "claude-opus-5-6"), ("fable", "claude-fable-5-2")):
            with self.subTest(alias=alias):
                proc = self.record("--served-model", served, candidate=alias)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIs(self.ledger_rows()[-1]["served_model_version_drift"], True)
                self.assertIn("warning:", proc.stderr)

    def test_exact_and_normalized_matches_have_no_drift_flag_or_warning(self):
        for served in ("claude-sonnet-5", "claude-sonnet-5-20260101", "anthropic/claude-sonnet-5"):
            with self.subTest(served=served):
                proc = self.record("--served-model", served, candidate="sonnet")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertNotIn("served_model_version_drift", self.ledger_rows()[-1])
                self.assertEqual(proc.stderr, "")

    def test_builtin_candidates_are_checked_against_their_model_name(self):
        proc = self.record("--served-model", "claude-haiku-4-5-20251001", candidate="haiku")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotEqual(self.record("--served-model", "claude-sonnet-5", candidate="haiku").returncode, 0)


if __name__ == "__main__":
    unittest.main()
