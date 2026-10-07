"""Structure and decision tests for the fast-delegate Skill v2 (stdlib only)."""

import contextlib
import importlib.util
import inspect
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "skills/fast-delegate"
SCRIPT = SKILL_DIR / "scripts/fdel.py"


def skill_docs_text():
    """SKILL.md (operational core) plus REFERENCE.md (everything moved out of it)."""
    return (SKILL_DIR / "SKILL.md").read_text() + "\n" + (SKILL_DIR / "REFERENCE.md").read_text()


def lint_skill(text):
    """Validate Skill markdown structure: YAML frontmatter, fence closure, no TODOs."""
    match = re.match(r"\A---\n(.*?)\n---\n", text, re.S)
    if not match:
        raise ValueError("missing YAML frontmatter delimiters")
    # The maintained frontmatter uses single-line scalars plus metadata. No
    # YAML package is needed for this deliberately small structural contract.
    fields = dict(re.findall(r"^([a-z][a-z-]*):\s*(.*?)$", match[1], re.M))
    name = fields.get("name", "")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 64:
        raise ValueError("invalid Skill name")
    description = fields.get("description", "").strip()
    if not description or len(description) > 1024 or any(c in description for c in "<>"):
        raise ValueError("invalid Skill description")
    if set(fields) - {"name", "description", "metadata", "license", "allowed-tools"}:
        raise ValueError("unsupported frontmatter field")
    fence = None
    for line in text[match.end():].splitlines():
        marker = re.match(r"^(`{3,}|~{3,})(.*)$", line)
        if marker:
            if fence is None:
                fence = marker[1]
            elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
        elif fence is None and re.match(r"\s*\[TODO:", line):
            raise ValueError("unfinished Skill scaffold")
    if fence:
        raise ValueError("unclosed code fence")
    return fields

FAKE_HARNESS = {
    "builtin": [{"id": "builtin-x", "subagent_type": "general-purpose", "model": "short-x", "catalog_id": "model-x"}],
    "spawn_model_placeholder": "ph",
    "placeholder_models": ["ph"],
    "catalog_provider_preference": ["prov1"],
    "disabled": [{"id": "agent-off", "why": "test"}],
    "default_lead": "builtin-x",
}


def entry(inp, out, ctx=100000, tools=True, mode=None, max_output=None, deprecation_date=None,
          cache_read=None, above_200k_in=None, above_200k_out=None, above_272k_in=None, above_272k_out=None):
    e = {"max_input_tokens": ctx, "supports_function_calling": tools}
    if inp is not None:
        e.update(input_cost_per_token=inp / 1e6, output_cost_per_token=out / 1e6)
    if mode is not None:
        e["mode"] = mode
    if max_output is not None:
        e["max_output_tokens"] = max_output
    if deprecation_date is not None:
        e["deprecation_date"] = deprecation_date
    if cache_read is not None:
        e["cache_read_input_token_cost"] = cache_read / 1e6
    if above_200k_in is not None:
        e["input_cost_per_token_above_200k_tokens"] = above_200k_in / 1e6
    if above_200k_out is not None:
        e["output_cost_per_token_above_200k_tokens"] = above_200k_out / 1e6
    if above_272k_in is not None:
        e["input_cost_per_token_above_272k_tokens"] = above_272k_in / 1e6
    if above_272k_out is not None:
        e["output_cost_per_token_above_272k_tokens"] = above_272k_out / 1e6
    return e


def cand(cid, inp=None, out=None, ctx=100000, tools=True, state="enabled", description="", catalog_entry=None,
         billing=None):
    """Synthetic discovered candidate (price per million tokens)."""
    return {"id": cid, "spawn": {"tool": "Agent", "subagent_type": cid, "model": "ph"}, "model_id": cid,
            "catalog_id": cid, "catalog_match": "jev",
            "price": None if inp is None else {"input_per_m": inp, "output_per_m": out},
            "context": ctx, "supports_tools": tools, "enabled": state != "disabled", "state": state,
            "description": description, "catalog_entry": catalog_entry, "source": "test", "billing": billing}


def contains_number(value):
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, dict):
        return any(contains_number(v) for v in value.values())
    if isinstance(value, list):
        return any(contains_number(v) for v in value)
    return False


class FastDelegateSkillTests(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        self._cwd = os.getcwd()
        self.tmp = Path(tempfile.mkdtemp())
        self.agents = self.tmp / "config" / "agents"
        self.agents.mkdir(parents=True)
        os.chdir(self.tmp)
        os.environ["FAST_DELEGATE_STATE"] = str(self.tmp / "state")
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.tmp / "config")
        os.environ["FAST_DELEGATE_CATALOG"] = str(self.tmp / "catalog.json")
        os.environ.pop("TYPESAFE_API_KEY", None)
        os.environ.pop("TYPESAFE_API_KEY_OP_REF", None)
        os.environ.pop("TYPESAFE_OP_TIMEOUT", None)
        self.write_catalog({})
        # Create a test override harness with a default_lead for CLI tests
        # This simulates a user's personal override file; use a real builtin from shipped harness
        state_dir = Path(os.environ["FAST_DELEGATE_STATE"])
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "harness.json").write_text(json.dumps({"default_lead": "opus"}))

    def tearDown(self):
        os.chdir(self._cwd)
        os.environ.clear()
        os.environ.update(self._env)

    def write_catalog(self, catalog):
        # Always include the default lead (builtin-x = model-x) in the catalog so tests work
        if "model-x" not in catalog:
            catalog = {**catalog, "model-x": entry(5, 6)}
        (self.tmp / "catalog.json").write_text(json.dumps(catalog))

    def write_agent(self, name, model=None, description="Test agent."):
        lines = ["---", f"name: {name}", f'description: "{description}"']
        if model is not None:
            lines.append(f"model: {model}")
        (self.agents / f"{name}.md").write_text("\n".join(lines + ["---", "", "Body."]) + "\n")

    def load(self, harness=FAKE_HARNESS):
        spec = importlib.util.spec_from_file_location("fdel", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # Never let a test hit the live TypeSafe API: section L now runs catalog matching for
        # every candidate under --jev, not just previously-unmatched ones, so any test that sets
        # a (fake) TYPESAFE_API_KEY without stubbing anything would otherwise reach the network
        # here. Block at urlopen (jev_post already turns any exception into a safe error tuple),
        # so tests that stub urlopen or jev_post themselves afterward still work unchanged.

        def _no_network(req, timeout=None):
            raise module.urllib.error.URLError("network disabled in tests")
        module.urllib.request.urlopen = _no_network
        if harness is not None:
            module.load_harness = lambda: json.loads(json.dumps(harness))
        route_impl = module.route
        module.route_actual = route_impl

        def route_with_complete_fixture_metadata(*args, **kwargs):
            """Legacy route tests model fixtures whose catalog identity is already confirmed."""
            discover = module.discover

            def verified_discover(*dargs, **dkwargs):
                candidates, warnings = discover(*dargs, **dkwargs)
                for candidate in candidates:
                    price = candidate.get("price")
                    if (candidate.get("catalog_match", "").startswith("unverified-")
                            and isinstance(price, dict) and candidate.get("context")
                            and isinstance(candidate.get("supports_tools"), bool)):
                        candidate["catalog_match"] = "jev"
                        candidate.setdefault("match_score", 1.0)
                        candidate.setdefault("match_entries", [])
                return candidates, warnings

            module.discover = verified_discover
            try:
                return route_impl(*args, **kwargs)
            finally:
                module.discover = discover

        module.route = route_with_complete_fixture_metadata
        return module

    # --- TypeSafe key resolution: env, then 1Password via `op` (no network, no real op) ---
    OP_REF = "op://vault/item/field"

    def stub_op(self, fdel, stdout="opkey-SECRET\n", returncode=0, exc=None):
        calls = []

        def fake_run(argv, **kw):
            calls.append((argv, kw))
            if exc:
                raise exc
            return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="stderr-LEAK")
        patcher = mock.patch.object(subprocess, "run", fake_run)  # shared module: restore after the test
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def stub_post(self, fdel, rejected=("envkey-SECRET",)):
        keys = []

        def fake_post(body, key, timeout):
            keys.append(key)
            return (None, "typesafe http 401") if key in rejected else ({"answers": {}}, None)
        fdel.jev_post = fake_post
        return keys

    def test_key_env_only_never_calls_op(self):
        fdel = self.load()
        os.environ["TYPESAFE_API_KEY"] = "envkey-SECRET"
        os.environ["TYPESAFE_API_KEY_OP_REF"] = self.OP_REF
        calls = self.stub_op(fdel)
        self.assertEqual(fdel.typesafe_key(), ("envkey-SECRET", "env", None))
        self.assertEqual(calls, [])

    def test_key_op_only_uses_argv_timeout_and_cache(self):
        fdel = self.load()
        os.environ["TYPESAFE_API_KEY_OP_REF"] = self.OP_REF
        os.environ["TYPESAFE_OP_TIMEOUT"] = "3"
        calls = self.stub_op(fdel)
        self.assertEqual(fdel.typesafe_key(), ("opkey-SECRET", "op", None))
        fdel.typesafe_key()
        self.assertEqual(len(calls), 1)  # cached per process
        self.assertEqual(calls[0][0], ["op", "read", "--", self.OP_REF])
        self.assertEqual(calls[0][1]["timeout"], 3.0)
        self.assertFalse(calls[0][1].get("shell"))

    def test_catalog_matching_and_task_judgment_share_op_resolution(self):
        fdel = self.load()
        os.environ["TYPESAFE_API_KEY_OP_REF"] = self.OP_REF
        calls = self.stub_op(fdel)
        requests = []

        def fake_post(body, key, timeout):
            requests.append((body, key, timeout))
            answers = {qid: {"noul": 1.0} for qid in body["questions"]}
            return {"answers": answers}, None

        fdel.jev_post = fake_post
        fdel.match_shortlist = lambda *args, **kwargs: ["acme/model"]
        fdel.match_cache_path = lambda: self.tmp / "matches.json"
        result, warnings, unjudged = fdel.jev_match_models(
            ["acme-model"], {"acme/model": entry(1, 1)}, {}
        )
        self.assertFalse(warnings)
        self.assertFalse(unjudged)
        self.assertEqual(result["acme-model"]["catalog_id"], "acme/model")
        payload, err = fdel.jev_request({"questions": {}}, 2)
        self.assertIsNone(err)
        self.assertEqual(payload["answers"], {})
        self.assertEqual([item[1] for item in requests], ["opkey-SECRET", "opkey-SECRET"])
        self.assertEqual(len(calls), 1)

    def test_key_unset_keeps_existing_wording(self):
        fdel = self.load()
        self.assertEqual(fdel.typesafe_key(), (None, None, "TYPESAFE_API_KEY unset"))
        self.assertEqual(fdel.jev({"questions": {}}), (None, "TYPESAFE_API_KEY unset"))

    def test_key_op_failure_modes(self):
        os.environ["TYPESAFE_API_KEY_OP_REF"] = self.OP_REF
        cases = [
            (dict(exc=FileNotFoundError("op")), "op not installed"),
            (dict(exc=subprocess.TimeoutExpired("op", 10)), "timed out"),
            (dict(returncode=1), "exit 1"),
            (dict(stdout="  \n"), "empty output"),
        ]
        for kw, expect in cases:
            fdel = self.load()
            self.stub_op(fdel, **kw)
            key, source, err = fdel.typesafe_key()
            self.assertIsNone(key)
            self.assertTrue(err.startswith("TYPESAFE_API_KEY unset"), err)
            self.assertIn(expect, err)
            self.assertNotIn("LEAK", err)
        fdel = self.load()
        os.environ["TYPESAFE_API_KEY_OP_REF"] = "vault/item/field"
        calls = self.stub_op(fdel)
        _, _, err = fdel.typesafe_key()
        self.assertIn("invalid", err)
        self.assertEqual(calls, [])

    def test_op_timeout_must_be_finite_positive_and_bounded(self):
        for value in ("0", "-1", "inf", "nan", "61"):
            fdel = self.load()
            os.environ["TYPESAFE_API_KEY_OP_REF"] = self.OP_REF
            os.environ["TYPESAFE_OP_TIMEOUT"] = value
            calls = self.stub_op(fdel)
            _, _, err = fdel.typesafe_key()
            self.assertIn("TYPESAFE_OP_TIMEOUT", err)
            self.assertEqual(calls, [])

    def test_op_multiline_and_control_credentials_are_rejected_safely(self):
        for stdout in ("first\nsecond\n", "key\tvalue\n", "key\t\n", "key\n\n"):
            fdel = self.load()
            os.environ["TYPESAFE_API_KEY_OP_REF"] = self.OP_REF
            self.stub_op(fdel, stdout=stdout)
            key, _, err = fdel.typesafe_key()
            self.assertIsNone(key)
            self.assertIn("invalid credential format", err)
            self.assertNotIn("first", err)
            self.assertNotIn("value", err)

    def test_rejected_credentials_are_not_retried_across_callers(self):
        fdel = self.load()
        os.environ["TYPESAFE_API_KEY"] = "envkey-SECRET"
        os.environ["TYPESAFE_API_KEY_OP_REF"] = self.OP_REF
        calls = self.stub_op(fdel)
        keys = self.stub_post(fdel, rejected=("envkey-SECRET", "opkey-SECRET"))
        _, err = fdel.jev_request({}, 1)
        self.assertIn("op key also rejected", err)
        _, err = fdel.jev_request({}, 1)
        self.assertIn("rejected previously", err)
        self.assertEqual(keys, ["envkey-SECRET", "opkey-SECRET"])
        self.assertEqual(len(calls), 1)

    def test_rejected_op_is_not_retried_after_env_rotation(self):
        fdel = self.load()
        os.environ["TYPESAFE_API_KEY"] = "envkey-A"
        os.environ["TYPESAFE_API_KEY_OP_REF"] = self.OP_REF
        self.stub_op(fdel)
        keys = self.stub_post(fdel, rejected=("envkey-A", "opkey-SECRET", "envkey-B"))
        fdel.jev_request({}, 1)
        os.environ["TYPESAFE_API_KEY"] = "envkey-B"
        _, err = fdel.jev_request({}, 1)
        self.assertIn("op fallback credential previously rejected", err)
        self.assertEqual(keys, ["envkey-A", "opkey-SECRET", "envkey-B"])

    def test_configuring_op_after_env_401_recovers_in_same_process(self):
        fdel = self.load()
        os.environ["TYPESAFE_API_KEY"] = "envkey-A"
        keys = self.stub_post(fdel, rejected=("envkey-A",))
        _, err = fdel.jev_request({}, 1)
        self.assertIn("op fallback not configured", err)
        os.environ["TYPESAFE_API_KEY_OP_REF"] = self.OP_REF
        self.stub_op(fdel)
        payload, err = fdel.jev_request({}, 1)
        self.assertIsNone(err)
        self.assertEqual(payload, {"answers": {}})
        self.assertEqual(keys, ["envkey-A", "opkey-SECRET"])

    def test_identity_and_task_requests_reject_invalid_timeout_before_auth(self):
        values = ("nan", "inf", "0", "-1", "61")
        fdel = self.load()
        os.environ["TYPESAFE_API_KEY_OP_REF"] = self.OP_REF
        op_calls = self.stub_op(fdel)
        post = mock.Mock(return_value=({"answers": {}}, None))
        fdel.jev_post = post
        fdel.match_shortlist = lambda *args, **kwargs: ["acme/model"]
        fdel.match_cache_path = lambda: self.tmp / "matches-invalid-timeout.json"
        for value in values:
            os.environ["TYPESAFE_TIMEOUT"] = value
            _, warnings, _ = fdel.jev_match_models(
                ["acme-model"], {"acme/model": entry(1, 1)}, {}
            )
            self.assertIn("TYPESAFE_TIMEOUT must be finite", warnings[0])
            result, err = fdel.jev({"questions": {}})
            self.assertIsNone(result)
            self.assertIn("TYPESAFE_TIMEOUT must be finite", err)
        self.assertEqual(post.call_count, 0)
        self.assertEqual(op_calls, [])

    def test_env_401_retries_with_op_and_sticks(self):
        fdel = self.load()
        os.environ["TYPESAFE_API_KEY"] = "envkey-SECRET"
        os.environ["TYPESAFE_API_KEY_OP_REF"] = self.OP_REF
        self.stub_op(fdel)
        keys = self.stub_post(fdel)
        payload, err = fdel.jev_request({}, 1)
        self.assertEqual((payload, err), ({"answers": {}}, None))
        self.assertEqual(keys, ["envkey-SECRET", "opkey-SECRET"])
        fdel.jev_request({}, 1)
        self.assertEqual(keys[2:], ["opkey-SECRET"])  # op key used for the rest of the process

    def test_env_401_without_ref_names_env_source(self):
        fdel = self.load()
        os.environ["TYPESAFE_API_KEY"] = "envkey-SECRET"
        self.stub_post(fdel)
        _, err = fdel.jev_request({}, 1)
        self.assertIn("typesafe http 401: TYPESAFE_API_KEY from env rejected", err)
        self.assertIn("op fallback not configured", err)
        out, jerr = fdel.jev({"questions": {}})
        self.assertIsNone(out)
        self.assertIn("from env rejected", jerr)

    def test_env_401_then_op_failure_or_rejection_is_reported(self):
        os.environ["TYPESAFE_API_KEY"] = "envkey-SECRET"
        os.environ["TYPESAFE_API_KEY_OP_REF"] = self.OP_REF
        fdel = self.load()
        self.stub_op(fdel, returncode=1)
        self.stub_post(fdel)
        _, err = fdel.jev_request({}, 1)
        self.assertIn("from env rejected", err)
        self.assertIn("op fallback failed: op read failed (exit 1)", err)
        fdel = self.load()
        self.stub_op(fdel)
        self.stub_post(fdel, rejected=("envkey-SECRET", "opkey-SECRET"))
        _, err = fdel.jev_request({}, 1)
        self.assertIn("from env rejected", err)
        self.assertIn("op key also rejected", err)

    def test_key_values_never_in_messages(self):
        os.environ["TYPESAFE_API_KEY"] = "envkey-SECRET"
        os.environ["TYPESAFE_API_KEY_OP_REF"] = self.OP_REF
        fdel = self.load()
        self.stub_op(fdel, stdout="opkey-SECRET")
        self.stub_post(fdel, rejected=("envkey-SECRET", "opkey-SECRET"))
        _, err = fdel.jev_request({}, 1)
        for secret in ("envkey-SECRET", "opkey-SECRET", "LEAK"):
            self.assertNotIn(secret, err)
        fdel = self.load()
        self.stub_op(fdel, returncode=2)
        self.stub_post(fdel)
        _, err = fdel.jev_request({}, 1)
        for secret in ("envkey-SECRET", "opkey-SECRET", "LEAK"):
            self.assertNotIn(secret, err)

    def record(self, fdel, cid, family, outcome, times=1):
        fdel.LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with fdel.LEDGER.open("a") as f:
            for _ in range(times):
                f.write(json.dumps({"candidate": cid, "family": family, "outcome": outcome}) + "\n")

    # ------------------------------------------------------------ structure

    def test_frontmatter_and_local_links(self):
        text = (SKILL_DIR / "SKILL.md").read_text()
        self.assertEqual(lint_skill(text)["name"], SKILL_DIR.name)
        for doc in (text, (SKILL_DIR / "REFERENCE.md").read_text()):
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", doc):
                if "://" not in target and not target.startswith("#"):
                    self.assertTrue((SKILL_DIR / target).is_file(), target)

    def test_skill_md_is_a_short_operational_core_linking_reference(self):
        raw = (SKILL_DIR / "SKILL.md").read_bytes()
        self.assertLessEqual(len(raw), 6000)
        text = raw.decode()
        self.assertIn("(REFERENCE.md)", text)
        self.assertTrue((SKILL_DIR / "REFERENCE.md").is_file())
        # Order: when to use, task JSON, route command, output, verify, record, proxy guard.
        markers = ("## When to use", "## 1. Describe the task",
                   "python3 <this skill dir>/scripts/fdel.py route --task FILE --lead MODEL [--brief]",
                   "`recommendations`", "model_selector", "## 4. Verify before accepting",
                   "python3 <this skill dir>/scripts/fdel.py record --candidate", "Proxy guard")
        positions = [text.find(m) for m in markers]
        self.assertNotIn(-1, positions, dict(zip(markers, positions)))
        self.assertEqual(positions, sorted(positions))
        for token in ("proxy-*", "--served-model"):
            self.assertIn(token, text, token)

    def test_shipped_harness_is_valid(self):
        harness = json.loads((SKILL_DIR / "harness.json").read_text())
        self.assertEqual(harness["schema"], "fast-delegate/harness/v3")
        self.assertNotIn("spawn_model_placeholder", harness)
        for b in harness["builtin"]:
            self.assertTrue({"id", "model_selector", "model_name"} <= set(b))
            self.assertFalse({"subagent_type", "tool"} & set(b))
        # Shipped harness is neutral: empty disabled and probation, no default_lead, no proxy- ids
        disabled_ids = {d["id"] for d in harness.get("disabled", [])}
        self.assertEqual(disabled_ids, set())
        probation_ids = {d["id"] for d in harness.get("probation", [])}
        self.assertEqual(probation_ids, set())
        self.assertNotIn("default_lead", harness)
        builtin_ids = {b["id"] for b in harness["builtin"]}
        self.assertIn("fable", builtin_ids)
        # No proxy- prefixed ids in shipped builtins (they're for local overrides)
        for bid in builtin_ids:
            self.assertFalse(bid.startswith("proxy-"))
        fable = next(b for b in harness["builtin"] if b["id"] == "fable")
        self.assertEqual(fable["model_name"], "claude-fable-5-1")
        self.assertTrue(fable.get("description"))
        self.assertFalse((SKILL_DIR / "roster.json").exists())

    def test_skill_record_example_uses_a_real_candidate_id(self):
        text = (SKILL_DIR / "SKILL.md").read_text()
        ids = re.findall(r"record --candidate (\S+)", text)
        self.assertIn("record --candidate haiku", text)
        harness = json.loads((SKILL_DIR / "harness.json").read_text())
        builtin_ids = {b["id"] for b in harness["builtin"]}
        self.assertTrue(set(ids) <= builtin_ids, ids)

    def test_routing_code_names_no_model_vendor_or_tier(self):
        fdel = self.load()
        pattern = re.compile(r"opus|sonnet|haiku|gpt-|claude|anthropic|openai|gemini|economy|balanced|frontier", re.I)
        for fn in (fdel.route, fdel.decide, fdel.filter_candidates, fdel.rank_candidates, fdel.price_bands,
                   fdel.advisory_fit, fdel.fallback_pick, fdel.jev_slots, fdel.build_jev_request,
                   fdel.parse_jev_answers, fdel.match_catalog, fdel.extract_model_id, fdel.discover,
                   fdel.candidate_state, fdel.candidate_semantic_state, fdel.task_semantic_state,
                   fdel.resolve_lead_price, fdel.filter_cheaper_than_lead, fdel.blended_price,
                   fdel.estimate_tokens, fdel.brief_output, fdel.name_tokens,
                   fdel.largest_size, fdel.match_shortlist, fdel.pick_price_entry, fdel.jev_match_models):
            self.assertIsNone(pattern.search(inspect.getsource(fn)), fn.__name__)

    # ------------------------------------------------------------ discovery and catalog


    def test_discovery_prints_nothing(self):
        self.write_agent("agent-a", model="model-a")
        fdel = self.load()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fdel.discover()
        self.assertEqual(buf.getvalue(), "")

    def test_catalog_matching_preferences(self):
        fdel = self.load()
        m = fdel.match_catalog
        self.assertEqual(m("m", {"m": {}, "prov1/m": {}}), ("m", "exact"))
        self.assertEqual(m("m", {"prov2/m": {}, "prov1/m": {}, "a/b/m": {}}, ["prov1"]), ("prov1/m", "normalized"))
        self.assertEqual(m("m", {"prov2/m": {}, "a/b/m": {}}), ("prov2/m", "normalized"))
        self.assertEqual(m("m", {"m-2025-01-01": {}, "prov1/m-20250202": {}}, ["prov1"]), ("m-2025-01-01", "normalized"))
        self.assertEqual(m("m-2025-03-03", {"m-20240101": {}, "m": {}}), ("m", "normalized"))
        self.assertEqual(m("m", {"m-mini": {}, "xm": {}, "m-v2": {}}), (None, "none"))
        self.assertEqual(m("m", {}), (None, "none"))

    def test_unknown_price_is_null_not_invented(self):
        fdel = self.load()
        self.assertEqual(fdel.get_price_and_context({"max_tokens": 5}), (None, 5, None))
        price, _, _ = fdel.get_price_and_context({"input_cost_per_token": 0, "output_cost_per_token": 0})
        self.assertEqual(price, {"input_per_m": 0, "output_per_m": 0})

    # ------------------------------------------------------------ cost, bands, ranking

    def test_cost_and_unknown_price_sorts_last(self):
        fdel = self.load()
        ranked = fdel.rank_candidates([cand("unk"), cand("b", 2, 10), cand("a", 1, 2)],
                                      {"est_input_tokens": 1000, "est_output_tokens": 500}, {})
        self.assertEqual([c["id"] for c in ranked], ["a", "b", "unk"])
        self.assertAlmostEqual(ranked[0]["cost_usd"], (1000 * 1 + 500 * 2) / 1e6)
        self.assertIsNone(ranked[2]["cost_usd"])

    def test_price_bands(self):
        fdel = self.load()
        rank = lambda cs: fdel.price_bands(fdel.rank_candidates(cs, {}, {}))
        self.assertEqual(rank([cand("a", 1, 1), cand("u")]), {"a": "only", "u": "unknown"})
        self.assertEqual(rank([cand("a", 1, 1), cand("b", 2, 2)]), {"a": "cheapest", "b": "most expensive"})
        five = rank([cand(f"k{i}", i, i) for i in range(5)])
        self.assertEqual([five[f"k{i}"] for i in range(5)], fdel.PRICE_BANDS)
        self.assertEqual(five["k0"], "cheapest")  # a $0 known price is a known price
        tie = rank([cand("a", 1, 1), cand("b", 1, 1), cand("c", 9, 9)])
        self.assertEqual((tie["a"], tie["b"], tie["c"]), ("cheapest", "cheapest", "most expensive"))

    # ------------------------------------------------------------ filters and ledger

    def test_filters_and_ledger_skip(self):
        fdel = self.load()
        self.record(fdel, "twice", "fam", "rejected", 2)
        self.record(fdel, "once", "fam", "rejected", 1)
        self.record(fdel, "mixed", "fam", "rejected", 1)
        self.record(fdel, "mixed", "fam", "accepted", 1)
        cs = [cand("twice", 1, 1), cand("once", 1, 1), cand("mixed", 1, 1), cand("notools", 1, 1, tools=False),
              cand("small", 1, 1, ctx=1000), cand("gone", 1, 1)]
        kept, reasons = fdel.filter_candidates(cs, {"family": "fam", "exclude": ["gone"]}, fdel.ledger_stats())
        self.assertEqual([c["id"] for c in kept], ["twice", "once", "mixed"])
        self.assertFalse(any("ledger" in r for r in reasons))
        kept, _ = fdel.filter_candidates(cs, {"family": "other", "need_tools": False}, fdel.ledger_stats())
        self.assertIn("notools", [c["id"] for c in kept])
        self.assertIn("twice", [c["id"] for c in kept])

    def test_malformed_ledger_lines_are_skipped(self):
        fdel = self.load()
        fdel.LEDGER.parent.mkdir(parents=True, exist_ok=True)
        fdel.LEDGER.write_text('not json\n{"worker": "old", "family": "f", "outcome": "accepted"}\n'
                               '{"candidate": "c", "family": "f", "outcome": "accepted"}\n')
        self.assertEqual(fdel.ledger_stats(), {("f", "c"): {"accepted": 1, "rejected": 0}})

    # ------------------------------------------------------------ decisions


    def fit_setup(self):
        # Cost order at 20k in / 2k out: A (in 1, out 20) = $0.06 < B (in 5, out 5) = $0.11,
        # while summed per-million price orders them the other way (A 21 > B 10).
        self.write_agent("agent-a", model="model-a")
        self.write_agent("agent-b", model="model-b")
        # Lead is builtin-x (expensive), so both agents are cheaper
        self.write_catalog({"model-a": entry(1, 20), "model-b": entry(5, 5), "model-x": entry(100, 100)})
        # Keep builtin-x as a builtin but set it as the lead so it's expensive compared to agents
        fdel = self.load(harness={**FAKE_HARNESS, "default_lead": "builtin-x"})
        fdel.os.environ["TYPESAFE_API_KEY"] = "test"
        return fdel

    def stub_jev(self, fdel, fits_by_model, extra=None):
        sent = []

        def fake(body):
            sent.append(body)
            ans = {"independent": 0.9, **(extra or {})}
            for cid, c in body["state"]["candidates"].items():
                fit = fits_by_model.get(c["model"])
                if isinstance(fit, tuple):  # (expected score, level probabilities)
                    ans[f"fit_{cid}"], ans[f"fit_{cid}_probs"] = fit
                elif fit is not None:
                    ans[f"fit_{cid}"] = fit
            return ans, None
        fdel.jev = fake
        return sent

    def test_fit_selection_uses_the_same_numbering_as_the_request(self):
        fdel = self.fit_setup()
        self.stub_jev(fdel, {"model-a": 1, "model-b": 4})
        result = fdel.route({"deliverable": "x"}, use_jev=True)
        self.assertEqual(result["candidate"]["id"], "agent-a")
        self.stub_jev(fdel, {"model-a": 4, "model-b": 4})
        result = fdel.route({"deliverable": "x"}, use_jev=True)
        self.assertEqual(result["candidate"]["id"], "agent-a")  # cheapest qualifying
        self.assertEqual(result["candidate"]["price_band"], "cheapest")
        self.assertEqual(result["fallbacks"], ["agent-b"])

    def test_verified_model_information_prefers_cheapest_in_tier_after_fit_gate(self):
        fdel = self.load()
        ranked = [cand("cheap-unverified", 1, 1), cand("verified-cheapest", 2, 2), cand("verified", 3, 3)]
        ranked[0].update(cost_usd=1, catalog_match="unverified-normalized")
        ranked[1].update(cost_usd=2, catalog_match="jev", context=100000, supports_tools=True)
        # Complete Jev-confirmed information wins even though the unverified option is cheaper.
        # Among verified candidates, price order wins despite a difference in fit scores.
        ranked[2].update(cost_usd=3, catalog_match="jev", context=100000, supports_tools=True)
        semantic = {"fit_c0": 3, "fit_c1": 2.5, "fit_c2": 3}
        d = fdel.decide({"deliverable": "x"}, ranked, semantic, {}, fdel.price_bands(ranked))
        self.assertEqual(d["pick"]["id"], "verified-cheapest")
        self.assertTrue(d.get("review_required"))
        self.assertIn("cheapest metadata-safe candidate", d["reasons"][0])

    def test_verified_unsuitable_candidate_is_filtered_before_metadata_priority(self):
        fdel = self.load()
        ranked = [cand("verified-unsuitable", 1, 1), cand("qualified", 3, 3)]
        ranked[0].update(cost_usd=1, catalog_match="jev", context=100000, supports_tools=True)
        ranked[1].update(cost_usd=3, catalog_match="unverified-normalized")
        d = fdel.decide({"deliverable": "x"}, ranked,
                        {"fit_c0": 1, "fit_c1": 3}, {}, fdel.price_bands(ranked),
                        model_information_override={"candidate_id": "qualified", "reason": "lead knows the interface"})
        self.assertEqual(d["pick"]["id"], "qualified")

    def test_real_tool_context_and_quota_filters_precede_metadata_tier(self):
        fdel = self.load()
        verified_no_tools = cand("verified-no-tools", 1, 1, tools=False)
        verified_no_tools.update(catalog_match="jev", context=10000,
                                 billing={"mode": "subscription", "pool": "limited"})
        verified_small_context = cand("verified-small-context", 1, 1, ctx=1000)
        verified_small_context.update(catalog_match="jev", context=1000)
        verified_quota_blocked = cand("verified-quota-blocked", 1, 1)
        verified_quota_blocked.update(catalog_match="jev", context=10000,
                                      billing={"mode": "subscription", "pool": "limited"})
        eligible_unverified = cand("eligible-unverified", 2, 2)
        eligible_unverified.update(catalog_match="unverified-normalized", context=10000)
        filtered, reasons = fdel.filter_candidates(
            [verified_no_tools, verified_small_context, verified_quota_blocked, eligible_unverified],
            {"need_tools": True, "est_input_tokens": 2000, "est_output_tokens": 100}, {},
            quota_by_pool={"limited": {"status": "exhausted", "pool": "limited", "resets_at": time.time()}})
        self.assertEqual([c["id"] for c in filtered], ["eligible-unverified"])
        self.assertEqual(len(reasons), 3)
        ranked = fdel.rank_candidates(filtered, {"est_input_tokens": 2000, "est_output_tokens": 100}, {})
        decision = fdel.decide({"deliverable": "x"}, ranked, {"fit_c0": 3}, {}, fdel.price_bands(ranked),
                               model_information_override={"candidate_id": "eligible-unverified",
                                                          "reason": "lead knows the task environment"})
        self.assertEqual(decision["pick"]["id"], "eligible-unverified")
        with self.assertRaisesRegex(ValueError, "not eligible"):
            fdel.decide({"deliverable": "x"}, ranked, None, {}, fdel.price_bands(ranked),
                        model_information_override={"candidate_id": "verified-no-tools", "reason": "needed"})

    def test_model_information_override_validation_is_named_and_nonblank(self):
        fdel = self.load()
        for value in ({"candidate_id": " ", "reason": "needed"},
                      {"candidate_id": "candidate", "reason": " \n "},
                      {"candidate_id": "candidate", "reason": "needed", "other": True}):
            with self.assertRaises(ValueError):
                fdel.validate_model_information_override(value)
        self.assertEqual(fdel.validate_model_information_override(
            {"candidate_id": "candidate", "reason": "because"}),
            {"candidate_id": "candidate", "reason": "because"})
        with self.assertRaisesRegex(ValueError, "exactly candidate_id and reason"):
            fdel.validate_model_information_override({"candidate_id": "candidate", "reason": "why", "extra": 1})

    def test_semantic_without_fit_answers_uses_metadata_fallback(self):
        fdel = self.load()
        ranked = fdel.rank_candidates([cand("verified", 1, 1), cand("incomplete", 2, 2)], {}, {})
        ranked[1]["catalog_match"] = "none"
        override = {"candidate_id": "incomplete", "reason": "lead compatibility evidence"}
        decision = fdel.decide({}, ranked, {"independent": .01}, {}, fdel.price_bands(ranked), model_information_override=override)
        self.assertEqual(decision["pick"]["id"], "incomplete")
        self.assertIn("fallback", " ".join(decision["reasons"]))
        self.assertTrue(decision["review_required"])

    def test_override_is_metadata_scoped_and_not_fit_scoped(self):
        fdel = self.load()
        ranked = fdel.rank_candidates([cand("verified", 1, 1), cand("incomplete", 2, 2)], {}, {})
        ranked[1]["catalog_match"] = "none"
        override = {"candidate_id": "incomplete", "reason": "lead compatibility evidence"}
        for semantic in ({"fit_c0": 4, "fit_c1": 0}, {"fit_c0": 0, "fit_c1": 0}, None):
            d = fdel.decide({}, ranked, semantic, {}, fdel.price_bands(ranked), model_information_override=override)
            self.assertEqual(d["pick"]["id"], "incomplete")
            self.assertTrue(d["review_required"])


    def test_semantic_without_fit_and_without_override_requires_complete_metadata(self):
        fdel = self.load()
        ranked = [cand("incomplete", 1, 1)]
        ranked = fdel.rank_candidates(ranked, {"deliverable": "x"}, {})
        ranked[0]["catalog_match"] = "none"
        decision = fdel.decide({"deliverable": "x"}, ranked, {"independent": .9}, {},
                               fdel.price_bands(ranked))
        self.assertEqual(decision["decision"], "direct")

    def test_route_override_cannot_select_excluded_or_unknown_candidates(self):
        self.write_agent("agent-a", model="model-a")
        self.write_catalog({"model-a": entry(1, 2), "model-x": entry(100, 100)})
        fdel = self.load()
        base = {"deliverable": "x", "acceptance": ["pytest passes"]}
        with self.assertRaisesRegex(SystemExit, "nonblank justification"):
            fdel.route_actual(base, model_information_override={"candidate_id": "agent-a", "reason": "  "})
        with self.assertRaisesRegex(SystemExit, "unknown or unavailable"):
            fdel.route_actual(base, model_information_override={"candidate_id": "missing", "reason": "why"})
        with self.assertRaisesRegex(SystemExit, "does not pass enabled, exclude"):
            fdel.route_actual({**base, "exclude": ["agent-a"]},
                       model_information_override={"candidate_id": "agent-a", "reason": "why"})

    def test_fallback_list_excludes_incomplete_models_without_individual_authorization(self):
        fdel = self.load()
        ranked = [cand("verified-pick", 1, 1), cand("verified-alt", 2, 2),
                  cand("incomplete-qualified", 3, 3), cand("incomplete-floor", 4, 4)]
        ranked = fdel.rank_candidates(ranked, {"est_input_tokens": 1000, "est_output_tokens": 1000}, {})
        for c in ranked[:2]:
            c.update(catalog_match="jev", context=100000, supports_tools=True)
        ranked[2].update(catalog_match="unverified-normalized")
        ranked[3].update(catalog_match="none")
        semantic = {
            "fit_c0": 3, "fit_c0_probs": {0: .0, 1: .1, 2: .0, 3: .9},
            "fit_c1": 3, "fit_c1_probs": {0: .0, 1: .1, 2: .0, 3: .9},
            "fit_c2": 2, "fit_c2_probs": {0: .1, 1: .5, 2: .4},
            "fit_c3": 2, "fit_c3_probs": {0: .2, 1: .5, 2: .3}}
        decision = fdel.decide({"deliverable": "x"}, ranked, semantic, {}, fdel.price_bands(ranked))
        self.assertEqual(decision["pick"]["id"], "verified-pick")
        self.assertEqual(decision["fallbacks"], ["verified-alt"])

    def test_no_fit_gate_clearance_keeps_highest_mass_best_effort(self):
        fdel = self.load()
        ranked = [cand("verified-low", 1, 1), cand("unverified-high", 3, 3)]
        ranked[0].update(cost_usd=1, catalog_match="jev", context=100000, supports_tools=True)
        ranked[1].update(cost_usd=3, catalog_match="none")
        semantic = {"fit_c0": 2, "fit_c0_probs": {0: .6, 1: .1, 2: .3},
                    "fit_c1": 2, "fit_c1_probs": {0: .3, 1: .1, 2: .6}}
        d = fdel.decide({"deliverable": "x"}, ranked, semantic, {}, fdel.price_bands(ranked),
                        model_information_override={"candidate_id": "unverified-high", "reason": "lead knows the task environment"})
        self.assertEqual(d["pick"]["id"], "unverified-high")
        self.assertTrue(d["review_required"])


    # Live jev-latest answers from 2026-09-24 (EVIDENCE.md): spread five-level distributions.
    SPREAD_OK = (2.14, {0: .04, 1: .15, 2: .49, 3: .27, 4: .05})     # mass at >=2 is 0.81, at >=3 is 0.32
    SPREAD_LOW = (1.93, {0: .05, 1: .23, 2: .49, 3: .20, 4: .03})    # mass at >=3 is 0.23
    STRONG = (3.15, {0: .0, 1: .02, 2: .15, 3: .49, 4: .34})         # mass at >=3 is 0.83

    def test_advisory_semantics_do_not_change_eligibility_or_acceptance(self):
        fdel = self.fit_setup()
        for score in (0, .01, 1.93, 4):
            for independent in (0, .34, 1):
                self.stub_jev(fdel, {"model-a": (score, {0: .9, 4: .1}), "model-b": self.STRONG},
                              {"independent": independent, "difficulty": 4, "verifiability": 0})
                os.environ["TYPESAFE_FIT_FLOOR"] = "invalid obsolete setting"
                os.environ["TYPESAFE_FIT_MASS"] = "1"
                result = fdel.route({"deliverable": "x", "difficulty": 4})
                self.assertEqual(result["candidate"]["id"], "agent-a")
                self.assertEqual({r["id"] for r in result["recommendations"]}, {"agent-a", "agent-b"})
                self.assertTrue(all(r["review_required"] for r in result["recommendations"]))
                self.assertEqual(result["semantic"]["fit_c0_probs"], {0: .9, 4: .1})
                self.assertNotIn("required_fit", result)


    def test_live_confidence_defaults(self):
        fdel = self.fit_setup()
        body = fdel.build_jev_request({"deliverable": "x"}, fdel.jev_slots([cand("a", 1, 1)]), {"a": "only"}, {})
        reply = {"answers": {"difficulty": {"score": 3.0, "confidence": 0.45},
                             "verifiability": {"score": 1.0, "confidence": 0.35},
                             "fit_c0": {"score": 2.14, "confidence": 0.1, "probabilities": {"0": .04, "1": .15, "2": .49, "3": .27, "4": .05}}}}

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        fdel.urllib.request.urlopen = lambda req, timeout=None: Resp(json.dumps(reply).encode())
        semantic, err = fdel.jev(body)
        self.assertIsNone(err)
        self.assertEqual(semantic["difficulty"], 3.0)        # 0.45 >= default 0.4
        self.assertNotIn("verifiability", semantic)          # 0.35 < 0.4
        self.assertEqual(semantic["fit_c0"], 2.14)           # fit answers are not confidence-gated
        self.assertEqual(semantic["fit_c0_probs"][2], .49)

    def test_all_judged_unfit_is_advisory(self):
        fdel = self.fit_setup()
        self.stub_jev(fdel, {"model-a": (0, {0: 1}), "model-b": (0, {0: 1})})
        result = fdel.route({"deliverable": "x"})
        self.assertEqual(result["decision"], "delegate")
        self.assertEqual(len(result["recommendations"]), 2)
        self.assertTrue(result["review_required"])


    def test_stubbed_semantic_without_fit_uses_explicit_fallback(self):
        fdel = self.fit_setup()
        self.stub_jev(fdel, {})
        result = fdel.route({"deliverable": "x"})
        self.assertIsNone(result["semantic"])
        self.assertIn("no usable fit evidence", " ".join(result["reasons"]))
        self.assertTrue(all(r["fit_score"] is None and r["ranking_basis"] == "heuristic" for r in result["recommendations"]))

    def test_fallback_is_cheapest_with_no_invented_suitability(self):
        fdel = self.load()
        ranked = fdel.rank_candidates([cand("cheap", 1, 1), cand("dear", 9, 9)], {}, {})
        for task in ({}, {"acceptance": ["pytest passes"]}, {"acceptance": ["looks right"]}):
            pick, why = fdel.fallback_pick(ranked, task, {("general", "cheap"): {"accepted": 0, "rejected": 10}})
            self.assertEqual(pick["id"], "cheap")
            self.assertIn("lead acceptance required", why)


    def test_jev_unavailable_falls_back_with_reason(self):
        fdel = self.fit_setup()
        del fdel.os.environ["TYPESAFE_API_KEY"]
        result = fdel.route({"deliverable": "x", "acceptance": ["pytest passes"]}, use_jev=True)
        self.assertEqual(result["candidate"]["id"], "agent-a")
        self.assertIsNone(result["semantic"])
        self.assertTrue(any("TYPESAFE_API_KEY unset" in r for r in result["reasons"]))

    # ------------------------------------------------------------ Jev request and parsing

    def test_jev_request_shape(self):
        fdel = self.load()
        ranked = fdel.rank_candidates([cand(f"k{i}", i + 1, i + 1) for i in range(10)] + [cand("u")], {}, {})
        slots = fdel.jev_slots(ranked)
        body = fdel.build_jev_request({"deliverable": "x", "acceptance": ["pytest passes"], "difficulty": 3},
                                      slots, fdel.price_bands(ranked), {})
        # 11 candidates fit comfortably under the cap; all are judged.
        fits = sorted((q for q in body["questions"] if q.startswith("fit_")), key=lambda q: int(q[len("fit_c"):]))
        self.assertEqual(fits, [f"fit_c{i}" for i in range(11)])
        self.assertEqual([body["state"]["candidates"][f"c{i}"]["model"] for i in range(11)], [f"k{i}" for i in range(10)] + ["u"])
        for qid, q in body["questions"].items():
            self.assertIn(q["type"], {"noul", "score", "choice"}, qid)
            self.assertTrue(q["instructions"] and q["criteria"], qid)
            if q["type"] == "score":
                self.assertTrue(2 <= len(q["criteria"]) <= 10, qid)
        for cid in body["state"]["candidates"]:
            self.assertIn(f"candidates.{cid}", body["questions"][f"fit_{cid}"]["instructions"])
        self.assertEqual(body["state"]["task"]["complexity"], "unknown")
        self.assertEqual(body["state"]["task"]["difficulty"], 3)

    def test_jev_max_candidates_is_derived_from_the_documented_question_cap(self):
        """B: cap is a named constant computed from the total question budget, not a bare 8."""
        fdel = self.load()
        self.assertEqual(fdel.JEV_MAX_CANDIDATES, fdel.JEV_MAX_QUESTIONS - len(fdel.QUESTIONS))
        many = fdel.rank_candidates([cand(f"k{i}", i + 1, i + 1) for i in range(fdel.JEV_MAX_CANDIDATES + 10)], {}, {})
        self.assertEqual(len(fdel.jev_slots(many)), fdel.JEV_MAX_CANDIDATES)
        few = fdel.rank_candidates([cand("a", 1, 1), cand("b", 2, 2)], {}, {})
        self.assertEqual(len(fdel.jev_slots(few)), 2)

    def test_answer_parsing_is_defensive(self):
        fdel = self.load()
        qs = {"n": {"type": "noul"}, "s": {"type": "score"}, "t": {"type": "score"}}
        parse = lambda p: fdel.parse_jev_answers(p, qs, 0.6)
        self.assertEqual(parse({"answers": {"n": {"noul": 0.7}, "s": {"score": 3, "confidence": 0.9},
                                            "t": {"score": 4, "confidence": 0.3}}}), ({"n": 0.7, "s": 3}, None))
        self.assertEqual(parse({"answers": {"s": {"score": 3, "confidence": None}}}), ({}, None))
        self.assertEqual(parse({"answers": {"s": {"score": 3}}}), ({}, None))
        self.assertEqual(parse({"answers": {"s": {"score": "3", "confidence": 0.9}, "n": {"noul": True}}}), ({}, None))
        self.assertEqual(parse({"answers": [{"id": "s", "score": 2, "confidence": 0.8}, 7]}), ({"s": 2}, None))
        self.assertEqual(parse({"answers": {"s": [1], "n": None}}), ({}, None))
        for bad in (None, [], "x", {"answers": 3}, {}):
            out, err = parse(bad)
            self.assertEqual(out, {})
            self.assertTrue(err)

    # ------------------------------------------------------------ CLI output

    def run_cli(self, *args, stdin=""):
        cli_args = [*args]
        if cli_args and cli_args[0] in {"route", "discover"}:
            cli_args[1:1] = ["--harness", "claude"]
        if cli_args and cli_args[0] == "route":
            cli_args[1:1] = ["--model-information-override-candidate", "agent-a",
                             "--model-information-override-reason", "test fixture lead rationale"]
        return subprocess.run([sys.executable, str(SCRIPT), *cli_args], input=stdin, capture_output=True,
                              text=True, env=os.environ.copy(), cwd=self.tmp, check=True)

    def test_route_and_record_print_exactly_one_json_value(self):
        self.write_agent("agent-a", model="model-a")
        # The shipped harness.json (real file, not mocked by these subprocess CLI tests) sets
        # default_lead "opus" (catalog_id claude-opus-5-5); price it well above agent-a so the
        # cheaper-than-lead rule (section I) does not change which candidate wins here.
        self.write_catalog({"model-a": entry(1, 2), "claude-opus-5-5": entry(50, 100)})
        out = self.run_cli("route", stdin=json.dumps({"deliverable": "x", "acceptance": ["pytest passes"]})).stdout
        result = json.loads(out)
        self.assertEqual(result["decision"], "delegate")
        self.assertEqual(result["candidate"]["id"], "agent-a")
        rec = json.loads(self.run_cli("record", "--candidate", "agent-a", "--family", "f", "--outcome", "accepted").stdout)
        self.assertEqual(rec["recorded"]["candidate"], "agent-a")
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_cli("record", "--candidate", "nope", "--family", "f", "--outcome", "accepted")

    # ------------------------------------------------------------ fixes for issue #1-4

    def test_typesafe_fit_mass_not_parsed_without_jev(self):
        """Issue 1: TYPESAFE_FIT_MASS should not be parsed without --jev."""
        fdel = self.load()
        self.write_agent("agent-a", model="model-a")
        self.write_catalog({"model-a": entry(1, 2)})
        # Set a malformed TYPESAFE_FIT_MASS; it should not cause error without --jev
        os.environ["TYPESAFE_FIT_MASS"] = "not_a_number"
        # This should succeed (direct path) without parsing TYPESAFE_FIT_MASS
        result = fdel.route({"deliverable": "x", "acceptance": ["pytest passes"]}, use_jev=False)
        self.assertEqual(result["decision"], "delegate")
        self.assertIsNone(result["semantic"])

    def test_non_object_fast_delegate_catalog_is_ignored_with_warning(self):
        """Issue 2: A non-object FAST_DELEGATE_CATALOG should be treated like remote: ignored with warning."""
        fdel = self.load()
        # Write a non-object JSON (an array) to the catalog
        (self.tmp / "catalog.json").write_text(json.dumps([1, 2, 3]))
        candidates, warnings = fdel.discover()
        # Should get an empty catalog with a warning
        self.assertTrue(any("is not a JSON object" in w for w in warnings))
        # No price facts should be discovered
        for c in candidates:
            self.assertIsNone(c["price"])

    def test_fit_mass_omitted_for_score_only_answers(self):
        """Issue 3: fit_cN_mass should be omitted when null (score-only answers without probabilities)."""
        fdel = self.fit_setup()
        # Stub Jev to return a fit answer with score only, no probabilities
        self.stub_jev(fdel, {"model-a": 4, "model-b": 4})  # just a score, no tuple = no probabilities
        result = fdel.route({"deliverable": "x"}, use_jev=True)
        # When there are no probabilities, fit_cN_mass should not be in semantic
        if result["semantic"]:
            for key in result["semantic"]:
                self.assertFalse(key.endswith("_mass"), f"fit_cN_mass should be omitted for score-only: {key}")


    # ------------------------------------------------------------ A: lean route output


    def test_brief_output_direct_omits_spawn_and_prompt_fields(self):
        fdel = self.load()
        result = {"decision": "direct", "reasons": ["no candidate"], "warnings": []}
        brief = fdel.brief_output(result)
        for key in ("id", "subagent_type", "model", "cost_usd", "fallbacks", "handoff_path", "prompt"):
            self.assertNotIn(key, brief)
        self.assertEqual(brief["decision"], "direct")


    # ------------------------------------------------------------ C: token learning

    def test_record_tokens_cli_stores_tokens_field(self):
        self.write_agent("agent-a", model="model-a")
        self.write_catalog({"model-a": entry(1, 2)})
        out = json.loads(self.run_cli("record", "--candidate", "agent-a", "--family", "f",
                                      "--outcome", "accepted", "--tokens", "1234").stdout)
        self.assertEqual(out["recorded"]["tokens"], 1234)
        line = json.loads(Path(out["ledger"]).read_text().splitlines()[-1])
        self.assertEqual(line["tokens"], 1234)

    def test_estimate_tokens_prefers_candidate_then_family_then_task_estimate(self):
        fdel = self.load()
        task = {"est_input_tokens": 1000, "est_output_tokens": 200}
        self.assertEqual(fdel.estimate_tokens(task, "fam", "a", {}), (1200, "estimate"))
        token_stats = {("fam", "b"): [3000, 5000]}
        self.assertEqual(fdel.estimate_tokens(task, "fam", "a", token_stats), (4000, "family"))
        token_stats[("fam", "a")] = [900]
        self.assertEqual(fdel.estimate_tokens(task, "fam", "a", token_stats), (1200, "candidate"))

    def test_route_reports_token_basis_and_learns_from_the_ledger(self):
        self.write_agent("agent-a", model="model-a")
        self.write_catalog({"model-a": entry(1, 2)})
        fdel = self.load()
        result = fdel.route({"deliverable": "x", "acceptance": ["pytest passes"], "family": "fam"})
        self.assertEqual(result["candidate"]["token_basis"], "estimate")
        self.record(fdel, "agent-a", "fam", "accepted")
        fdel.LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with fdel.LEDGER.open("a") as f:
            f.write(json.dumps({"candidate": "agent-a", "family": "fam", "outcome": "accepted", "tokens": 5000}) + "\n")
        result2 = fdel.route({"deliverable": "x", "acceptance": ["pytest passes"], "family": "fam"})
        self.assertEqual(result2["candidate"]["token_basis"], "candidate")

    def test_stats_shows_median_tokens_per_family_and_candidate(self):
        self.write_agent("agent-a", model="model-a")
        self.write_catalog({"model-a": entry(1, 2)})
        self.run_cli("record", "--candidate", "agent-a", "--family", "fam", "--outcome", "accepted", "--tokens", "100")
        self.run_cli("record", "--candidate", "agent-a", "--family", "fam", "--outcome", "accepted", "--tokens", "300")
        out = self.run_cli("stats").stdout
        self.assertIn("median_tokens=200", out)

    # ------------------------------------------------------------ E: docs

    def test_skill_md_documents_brief_tokens_lead_probation_and_fable(self):
        text = skill_docs_text()
        for token in ("--brief", "--tokens", "--lead", "probation", "fable", "default_lead", "prompt"):
            self.assertIn(token, text, token)

    # ------------------------------------------------------------ F: probation and disabled

    def test_probation_candidate_is_judged_and_can_win(self):
        self.write_agent("agent-p", model="model-p")
        self.write_agent("agent-q", model="model-q")
        # Set agent-q as expensive lead, agent-p as cheaper candidate
        self.write_catalog({"model-p": entry(1, 1), "model-q": entry(10, 10)})
        # Set lead to agent-q (expensive) so agent-p can be routed
        harness = {**FAKE_HARNESS, "builtin": [], "probation": [{"id": "agent-p", "why": "test"}], "default_lead": "agent-q"}
        fdel = self.load(harness=harness)
        by_id = {c["id"]: c for c in fdel.discover()[0]}
        self.assertEqual(by_id["agent-p"]["state"], "probation")
        fdel.os.environ["TYPESAFE_API_KEY"] = "test"
        sent = self.stub_jev(fdel, {"model-p": 4, "model-q": 1})
        result = fdel.route({"deliverable": "x"}, use_jev=True)
        self.assertEqual(result["candidate"]["id"], "agent-p")
        self.assertIn("probation candidate: verify strictly and record the outcome", result["warnings"])
        cands = sent[0]["state"]["candidates"]
        p_state = next(c for c in cands.values() if c["model"] == "model-p")
        # Fix 1: probation shows real evidence, not canned message
        self.assertEqual(p_state["evidence"], "no record")
        self.assertEqual(p_state["probation"], True)

    def test_disabled_candidate_never_appears_in_the_jev_request(self):
        self.write_agent("agent-off", model="model-off")
        self.write_agent("agent-a", model="model-a")
        self.write_catalog({"model-a": entry(1, 1), "model-off": entry(1, 1)})
        fdel = self.load()  # FAKE_HARNESS disables "agent-off"
        self.assertEqual({c["id"]: c["state"] for c in fdel.discover()[0]}["agent-off"], "disabled")
        fdel.os.environ["TYPESAFE_API_KEY"] = "test"
        sent = self.stub_jev(fdel, {"model-a": 4, "model-off": 4})
        fdel.route({"deliverable": "x"}, use_jev=True)
        models_sent = [c["model"] for c in sent[0]["state"]["candidates"].values()]
        self.assertNotIn("model-off", models_sent)

    def test_probation_operator_state_is_not_changed_by_history(self):
        self.write_agent("agent-p", model="model-p")
        harness = {**FAKE_HARNESS, "probation": [{"id": "agent-p", "why": "test"}]}
        fdel = self.load(harness=harness)
        state = lambda: {c["id"]: c["state"] for c in fdel.discover()[0]}["agent-p"]
        self.assertEqual(state(), "probation")
        self.record(fdel, "agent-p", "fam1", "accepted", 1)
        self.record(fdel, "agent-p", "fam2", "accepted", 1)
        self.assertEqual(state(), "probation")  # Explicit operator state remains.
        self.record(fdel, "agent-p", "fam3", "rejected", 3)
        self.assertEqual(state(), "probation")  # History remains advisory.

    # ------------------------------------------------------------ G (route-side): richer context

    def test_context_files_are_read_truncated_capped_and_missing_files_warn(self):
        fdel = self.load()
        big = self.tmp / "big.txt"
        big.write_text("a" * (fdel.CONTEXT_FILE_CAP + 500))
        small = self.tmp / "small.txt"
        small.write_text("hello world")
        files, warnings = fdel.read_context_files([str(big), str(small), str(self.tmp / "missing.txt")])
        self.assertEqual(len(files[0]["text"]), fdel.CONTEXT_FILE_CAP)
        self.assertTrue(files[0]["truncated"])
        self.assertEqual(files[1]["text"], "hello world")
        self.assertFalse(files[1]["truncated"])
        self.assertTrue(any("missing.txt" in w and "unreadable" in w for w in warnings))

    def test_context_files_respect_the_total_cap_across_files(self):
        fdel = self.load()
        paths = []
        for i in range(5):
            p = self.tmp / f"f{i}.txt"
            p.write_text("x" * 19000)
            paths.append(str(p))
        files, _warnings = fdel.read_context_files(paths)
        total = sum(len(f["text"]) for f in files)
        self.assertLessEqual(total, fdel.CONTEXT_TOTAL_CAP)
        self.assertTrue(any(f["truncated"] for f in files))


    def test_candidate_semantic_state_includes_prices_description_and_recent_records(self):
        self.write_agent("agent-a", model="model-a", description="Great at refactors.")
        self.write_catalog({"model-a": {"input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6,
                                        "max_input_tokens": 100000, "max_output_tokens": 8000,
                                        "supports_function_calling": True, "supports_vision": False}})
        fdel = self.load()
        self.record(fdel, "agent-a", "fam", "accepted", 1)
        self.record(fdel, "agent-a", "fam", "rejected", 1)
        stats = fdel.ledger_stats()
        c = next(c for c in fdel.discover()[0] if c["id"] == "agent-a")
        ranked = fdel.rank_candidates([c], {"family": "fam"}, stats)
        bands = fdel.price_bands(ranked)
        state = fdel.candidate_semantic_state(ranked[0], "fam", bands, stats)
        self.assertEqual(state["input_price_per_m"], 1.0)
        self.assertEqual(state["output_price_per_m"], 2.0)
        self.assertEqual(state["context_window"], 100000)
        self.assertEqual(state["max_output_tokens"], 8000)
        self.assertEqual(state["supports"], {"supports_function_calling": True, "supports_vision": False})
        self.assertEqual(state["description"], "Great at refactors.")
        self.assertEqual(state["family_accepted_total"], "1/2")
        self.assertEqual(len(state["recent_records"]), 2)

    def test_jev_usage_and_cost_reported_from_stubbed_response(self):
        fdel = self.fit_setup()
        body = fdel.build_jev_request({"deliverable": "x"}, fdel.jev_slots([cand("a", 1, 1)]), {"a": "only"}, {})
        reply = {"answers": {"fit_c0": {"type": "score", "score": 4, "probabilities": {"4": 1}}}, "usage": {"input_tokens": 10000, "output_tokens": 500}}

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        fdel.urllib.request.urlopen = lambda req, timeout=None: Resp(json.dumps(reply).encode())
        semantic, err = fdel.jev(body)
        self.assertIsNone(err)
        self.assertEqual(semantic["jev_usage"], {"input_tokens": 10000, "output_tokens": 500})
        self.assertAlmostEqual(semantic["jev_cost_usd"], 10000 * 0.042 / 1e6)

    # ------------------------------------------------------------ H: fable built-in and descriptions


    def test_builtin_description_used_in_candidate_state(self):
        harness = {**FAKE_HARNESS, "builtin": [{"id": "builtin-d", "subagent_type": "general-purpose",
                                                "model": "short-d", "catalog_id": "model-d",
                                                "description": "Cheap builtin worker."}]}
        self.write_catalog({"model-d": entry(1, 1)})
        fdel = self.load(harness=harness)
        c = next(c for c in fdel.discover()[0] if c["id"] == "builtin-d")
        self.assertEqual(c["description"], "Cheap builtin worker.")
        state = fdel.candidate_semantic_state(c, "fam", {"builtin-d": "only"}, {})
        self.assertEqual(state["description"], "Cheap builtin worker.")

    # ------------------------------------------------------------ I: lead only delegates to cheaper models

    def test_opus_lead_excludes_opus_fable_and_equal_priced_candidates(self):
        harness = {**FAKE_HARNESS, "builtin": [
            {"id": "opus", "subagent_type": "general-purpose", "model": "opus", "catalog_id": "model-opus"},
            {"id": "fable", "subagent_type": "general-purpose", "model": "fable", "catalog_id": "model-fable"},
            {"id": "cheap", "subagent_type": "general-purpose", "model": "cheap", "catalog_id": "model-cheap"},
        ], "default_lead": "opus"}
        self.write_catalog({"model-opus": entry(10, 10), "model-fable": entry(10, 10), "model-cheap": entry(1, 1)})
        fdel = self.load(harness=harness)
        result = fdel.route({"deliverable": "x", "acceptance": ["pytest passes"]})
        self.assertEqual(result["candidate"]["id"], "cheap")
        self.assertEqual(result["lead"], {"id": "opus", "catalog_id": "model-opus", "blended_price_per_m": 10.0})
        for dropped in ("opus", "fable"):
            self.assertTrue(any(f"drop {dropped}:" in r for r in result["reasons"]), dropped)

    def test_lead_with_only_pricier_candidates_is_direct(self):
        harness = {**FAKE_HARNESS, "builtin": [
            {"id": "haiku", "subagent_type": "general-purpose", "model": "haiku", "catalog_id": "model-haiku"},
            {"id": "pricey", "subagent_type": "general-purpose", "model": "pricey", "catalog_id": "model-pricey"},
        ], "default_lead": "haiku"}
        self.write_catalog({"model-haiku": entry(1, 1), "model-pricey": entry(5, 5)})
        fdel = self.load(harness=harness)
        result = fdel.route({"deliverable": "x", "acceptance": ["pytest passes"]})
        self.assertEqual(result["decision"], "direct")
        self.assertTrue(any("no candidate cheaper than lead haiku" in r for r in result["reasons"]))
        self.assertEqual(result["lead"]["id"], "haiku")

    def test_unknown_lead_is_a_hard_error(self):
        fdel = self.load()
        with self.assertRaises(SystemExit):
            fdel.route({"deliverable": "x"}, lead="totally-unknown-model-id")

    def test_unknown_price_candidate_is_dropped_by_the_lead_filter(self):
        fdel = self.load()
        kept, reasons = fdel.filter_cheaper_than_lead([cand("known", 1, 1), cand("mystery")],
                                                       {"input_per_m": 5, "output_per_m": 5}, (0.5, 0.5))
        self.assertEqual([c["id"] for c in kept], ["known"])
        self.assertTrue(any("drop mystery: unknown price" in r for r in reasons))

    def test_route_lead_argument_and_env_var_select_the_lead(self):
        harness = {**FAKE_HARNESS, "builtin": [
            {"id": "opus", "subagent_type": "general-purpose", "model": "opus", "catalog_id": "model-opus"},
            {"id": "cheap", "subagent_type": "general-purpose", "model": "cheap", "catalog_id": "model-cheap"},
        ]}  # no default_lead here
        self.write_catalog({"model-opus": entry(10, 10), "model-cheap": entry(1, 1)})
        fdel = self.load(harness=harness)
        # no lead configured anywhere -> error (section I: hard rule)
        with self.assertRaises(SystemExit):
            fdel.route({"deliverable": "x", "acceptance": ["pytest passes"]})
        # --lead argument activates the rule
        result = fdel.route({"deliverable": "x", "acceptance": ["pytest passes"]}, lead="opus")
        self.assertEqual(result["candidate"]["id"], "cheap")
        # FAST_DELEGATE_LEAD env var also activates it
        fdel.os.environ["FAST_DELEGATE_LEAD"] = "opus"
        result = fdel.route({"deliverable": "x", "acceptance": ["pytest passes"]})
        self.assertEqual(result["candidate"]["id"], "cheap")


    # ------------------------------------------------------------ Fixes: probation, context_files, lead

    def test_fix_1_probation_shows_real_evidence_not_canned_message(self):
        """Fix 1: probation candidate shows real evidence from ledger, not canned 'no accepted work'."""
        self.write_agent("agent-p", model="model-p")
        self.write_catalog({"model-p": entry(1, 1)})
        harness = {**FAKE_HARNESS, "probation": [{"id": "agent-p", "why": "test"}], "default_lead": "builtin-x"}
        fdel = self.load(harness=harness)

        # Initially: probation with no record -> evidence should be "no record"
        stats = fdel.ledger_stats()
        c = next(c for c in fdel.discover()[0] if c["id"] == "agent-p")
        ranked = fdel.rank_candidates([c], {}, stats)
        bands = fdel.price_bands(ranked)
        state = fdel.candidate_semantic_state(ranked[0], "fam", bands, stats)
        self.assertEqual(state["probation"], True)
        self.assertEqual(state["evidence"], "no record")

        # Graduate: 2 accepted in one family
        self.record(fdel, "agent-p", "fam1", "accepted", 2)
        stats = fdel.ledger_stats()
        state = fdel.candidate_semantic_state(ranked[0], "fam1", bands, stats)
        self.assertEqual(state["probation"], True)  # still probation (only 2 accepted, need to check rate later)
        self.assertEqual(state["evidence"], "accepted 2 of 2 similar tasks")
        self.assertEqual(state["family_accepted_total"], "2/2")

        # Regress: add 3 rejected, now 2 accepted / 5 total = 40% acceptance
        self.record(fdel, "agent-p", "fam1", "rejected", 3)
        stats = fdel.ledger_stats()
        state = fdel.candidate_semantic_state(ranked[0], "fam1", bands, stats)
        self.assertEqual(state["probation"], True)
        self.assertEqual(state["evidence"], "accepted 2 of 5 similar tasks")


    def test_fix_3_no_lead_configured_is_a_hard_error(self):
        """Fix 3: when no lead is configured anywhere, route must exit with error."""
        # Create a harness with NO default_lead (override FAKE_HARNESS which has one)
        harness = {k: v for k, v in FAKE_HARNESS.items() if k != "default_lead"}
        harness["builtin"] = [
            {"id": "a", "subagent_type": "general-purpose", "model": "a", "catalog_id": "model-a"},
        ]
        self.write_catalog({"model-a": entry(1, 1), "model-x": entry(5, 6)})
        fdel = self.load(harness=harness)

        # Without lead, route should error
        with self.assertRaises(SystemExit) as ctx:
            fdel.route({"deliverable": "x", "acceptance": ["ok"]})
        self.assertIn("no lead configured", str(ctx.exception))

    # ------------------------------------------------ J: fuzzy catalog matching

    QWEN_CATALOG = {
        "together_ai/Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8": entry(2, 2),
        "prov1/qwen3-coder-480b-a35b-instruct": entry(0.5, 1.5),
        "hostz/qwen3-coder-480b": entry(0.4, 1.6),
        "hostz/qwen3-coder-30b-a3b-instruct": entry(0.1, 0.3),
        "hostz/Qwen2.5-Coder-7B-Instruct": entry(0.05, 0.05),
        "hostz/Qwen2.5-7B-Instruct": entry(0.04, 0.04),
        "hostz/qwen3-coder": entry(0.3, 1.2),
    }

    def stub_match_post(self, fdel, same):
        """Stub jev_post: answer each noul 0.9 when its entry is in `same`, else 0.1. Records bodies."""
        sent = []

        def fake(body, key, timeout):
            sent.append(body)
            answers = {}
            for qid in body["questions"]:
                m, j = qid.split("_e")
                entry_key = body["state"]["models"][m]["entries"][int(j)]
                answers[qid] = {"type": "noul", "noul": 0.9 if entry_key in same else 0.1}
            return {"answers": answers}, None
        fdel.jev_post = fake
        return sent

    def test_j_name_tokens_read_2p5_as_2_5_and_split_letter_digit_boundaries(self):
        fdel = self.load()
        self.assertEqual(fdel.name_tokens("qwen2p5-coder-7b"), fdel.name_tokens("Qwen2.5-Coder-7B"))
        self.assertTrue({"qwen", "3", "coder", "480", "b"} <= fdel.name_tokens("Qwen3-Coder-480B"))
        self.assertEqual(fdel.largest_size("qwen3-coder-480b-a35b-instruct"), (1, 480.0))
        self.assertIsNone(fdel.largest_size("hostz/qwen3-coder"))

    def test_fix_1_moe_size_distinct_from_active_param_and_plain_size(self):
        """fixes-j.md #1: "8x7b" must not equal "8x22b" nor "7b"; the size filter must separate them."""
        fdel = self.load()
        self.assertEqual(fdel.largest_size("mixtral-8x7b-instruct-v0.1"), (8, 7.0))
        self.assertEqual(fdel.largest_size("mixtral-8x22b-instruct-v0.1"), (8, 22.0))
        self.assertEqual(fdel.largest_size("some-7b-model"), (1, 7.0))
        self.assertNotEqual(fdel.largest_size("mixtral-8x7b"), fdel.largest_size("mixtral-8x22b"))
        self.assertNotEqual(fdel.largest_size("mixtral-8x7b"), fdel.largest_size("some-7b-model"))
        moe_catalog = {"prov/mixtral-8x7b-instruct-v0.1": entry(1, 1), "prov/mixtral-8x22b-instruct-v0.1": entry(2, 2)}
        sl = fdel.match_shortlist("mixtral-8x7b-instruct-v0.1", moe_catalog, fdel.DEFAULT_SPECIALIZATION_TAGS)
        self.assertIn("prov/mixtral-8x7b-instruct-v0.1", sl)
        self.assertNotIn("prov/mixtral-8x22b-instruct-v0.1", sl)  # was wrongly kept on HEAD (both -> None)

    def test_fix_2_generic_tokens_do_not_count_as_shared(self):
        """fixes-j.md #2: a shared-only-generic-token key (e.g. via "instruct") must not shortlist."""
        fdel = self.load()
        catalog = {"meta.llama3-1-8b-instruct-v1:0": entry(1, 1)}
        sl = fdel.match_shortlist("local-mixtral-8x7b-instruct", catalog, fdel.DEFAULT_SPECIALIZATION_TAGS)
        self.assertEqual(sl, [])  # only "instruct" (a generic token) was shared on HEAD
        # A real shared non-generic token still shortlists.
        catalog2 = {"prov/mixtral-8x7b-instruct": entry(1, 1)}
        sl2 = fdel.match_shortlist("local-mixtral-8x7b-instruct", catalog2, fdel.DEFAULT_SPECIALIZATION_TAGS)
        self.assertIn("prov/mixtral-8x7b-instruct", sl2)

    def test_fix_3_sample_spec_excluded_from_catalog_matching(self):
        """fixes-j.md #3: the LiteLLM doc key "sample_spec" must never be treated as a real model."""
        fdel = self.load()
        catalog = {"sample_spec": entry(1, 1), "model-a": entry(2, 2)}
        self.assertEqual(fdel.match_catalog("sample_spec", catalog), (None, "none"))
        sl = fdel.match_shortlist("sample-spec-model", {"sample_spec": entry(1, 1)}, fdel.DEFAULT_SPECIALIZATION_TAGS)
        self.assertEqual(sl, [])

    def test_j_shortlist_size_filter_drops_other_sizes_and_keeps_sizeless_keys(self):
        fdel = self.load()
        sl = fdel.match_shortlist("Qwen3-Coder-480B", self.QWEN_CATALOG, fdel.DEFAULT_SPECIALIZATION_TAGS)
        self.assertIn("together_ai/Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8", sl)
        self.assertIn("hostz/qwen3-coder", sl)                        # no stated size: Jev decides
        self.assertNotIn("hostz/qwen3-coder-30b-a3b-instruct", sl)    # different size: code drops

    def test_j_specialization_filter_drops_the_general_model_for_a_coder(self):
        fdel = self.load()
        sl = fdel.match_shortlist("qwen2.5-coder-7b", self.QWEN_CATALOG, fdel.DEFAULT_SPECIALIZATION_TAGS)
        self.assertIn("hostz/Qwen2.5-Coder-7B-Instruct", sl)
        self.assertNotIn("hostz/Qwen2.5-7B-Instruct", sl)
        sl = fdel.match_shortlist("qwen2.5-7b-instruct", self.QWEN_CATALOG, fdel.DEFAULT_SPECIALIZATION_TAGS)
        self.assertNotIn("hostz/Qwen2.5-Coder-7B-Instruct", sl)       # and the reverse

    def test_j_discover_with_jev_prices_an_unmatched_open_model(self):
        self.write_catalog(dict(self.QWEN_CATALOG))
        self.write_agent("open-big", model="Qwen3-Coder-480B")
        fdel = self.load()
        os.environ["TYPESAFE_API_KEY"] = "test"
        same = {"together_ai/Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8", "prov1/qwen3-coder-480b-a35b-instruct",
                "hostz/qwen3-coder-480b"}
        sent = self.stub_match_post(fdel, same)
        cands, _ = fdel.discover(use_jev=True)
        c = next(c for c in cands if c["id"] == "open-big")
        self.assertEqual(c["catalog_match"], "jev")
        self.assertEqual(c["catalog_id"], "prov1/qwen3-coder-480b-a35b-instruct")   # preferred provider
        self.assertEqual(c["price"]["input_per_m"], 0.5)
        self.assertEqual(sorted(c["match_entries"]), sorted(same))
        self.assertEqual(len(sent), 1)
        q = next(iter(sent[0]["questions"].values()))
        self.assertEqual(q["type"], "noul")
        self.assertIn("same model", q["instructions"])
        self.assertNotIn("open-big", json.dumps(sent[0]["questions"]))  # meaning is in state, not ids

    def test_j_without_credentials_keeps_exact_seed_unverified(self):
        self.write_catalog({"Qwen3-Coder-480B": entry(1, 2), **self.QWEN_CATALOG})
        self.write_agent("open-big", model="Qwen3-Coder-480B")
        fdel = self.load()
        sent = self.stub_match_post(fdel, set())
        c = next(c for c in fdel.discover()[0] if c["id"] == "open-big")
        self.assertEqual((c["catalog_match"], c["price"]["input_per_m"]), ("unverified-exact", 1.0))
        self.assertEqual(sent, [])

    def test_j_threshold_rejects_low_scores_and_price_stays_unknown(self):
        self.write_catalog(dict(self.QWEN_CATALOG))
        self.write_agent("open-big", model="Qwen3-Coder-480B")
        fdel = self.load()
        os.environ["TYPESAFE_API_KEY"] = "test"
        self.stub_match_post(fdel, set())                     # every entry scored 0.1
        c = next(c for c in fdel.discover(use_jev=True)[0] if c["id"] == "open-big")
        self.assertEqual((c["catalog_match"], c["price"]), ("none", None))
        fdel = self.load()
        os.environ["TYPESAFE_MATCH_THRESHOLD"] = "0.05"      # env override; cache is per model, so clear it
        fdel.match_cache_path().unlink()
        self.stub_match_post(fdel, set())
        c = next(c for c in fdel.discover(use_jev=True)[0] if c["id"] == "open-big")
        self.assertEqual(c["catalog_match"], "jev")

    def test_j_price_pick_prefers_provider_then_bare_then_median(self):
        fdel = self.load()
        cat = {"a/x": {"input_cost_per_token": 3}, "b/x": {"input_cost_per_token": 1},
               "c/x": {"input_cost_per_token": 2}, "x": {"input_cost_per_token": 9}}
        self.assertEqual(fdel.pick_price_entry(["a/x", "b/x", "x"], cat, ["b"]), "b/x")
        self.assertEqual(fdel.pick_price_entry(["a/x", "b/x", "x"], cat, ["zz"]), "x")
        self.assertEqual(fdel.pick_price_entry(["a/x", "b/x", "c/x"], cat, []), "c/x")

    def test_j_cache_hit_sends_no_request(self):
        self.write_catalog(dict(self.QWEN_CATALOG))
        self.write_agent("open-big", model="Qwen3-Coder-480B")
        fdel = self.load()
        os.environ["TYPESAFE_API_KEY"] = "test"
        sent = self.stub_match_post(fdel, {"hostz/qwen3-coder-480b"})
        fdel.discover(use_jev=True)
        fdel.discover(use_jev=True)
        self.assertEqual(len(sent), 1)
        c = next(c for c in fdel.discover(use_jev=True)[0] if c["id"] == "open-big")
        self.assertEqual(c["catalog_id"], "hostz/qwen3-coder-480b")

    def test_j_default_discovery_verifies_exact_seed_and_reuses_cache(self):
        self.write_catalog({"model-y": entry(1, 2)})
        self.write_agent("exact", model="model-y")
        fdel = self.load()
        os.environ["TYPESAFE_API_KEY"] = "test"
        sent = self.stub_match_post(fdel, {"model-y"})
        first, _ = fdel.discover()
        self.assertEqual(next(c for c in first if c["id"] == "exact")["catalog_match"], "jev")
        second, _ = fdel.discover()
        self.assertEqual(next(c for c in second if c["id"] == "exact")["catalog_id"], "model-y")
        self.assertEqual(len(sent), 1)

    def test_j_diagnostic_task_opt_out_keeps_matching_independent(self):
        fdel = self.fit_setup()
        sent = self.stub_match_post(fdel, {"model-a", "model-b", "model-x"})
        result = fdel.route({"deliverable": "x", "acceptance": ["pytest passes"]}, use_jev=False)
        self.assertEqual(len(sent), 1)  # one model identity request, no fit request
        self.assertIsNone(result["semantic"])

    def test_j_default_matching_reuses_verified_cache_without_credentials(self):
        self.write_catalog({"model-y": entry(1, 2)})
        self.write_agent("exact", model="model-y")
        fdel = self.load()
        os.environ["TYPESAFE_API_KEY"] = "test"
        sent = self.stub_match_post(fdel, {"model-y"})
        fdel.discover()
        del os.environ["TYPESAFE_API_KEY"]
        c = next(c for c in fdel.discover()[0] if c["id"] == "exact")
        self.assertEqual(c["catalog_match"], "jev")
        self.assertEqual(len(sent), 1)

    def test_j_raw_lead_price_uses_jev_matching_and_cache(self):
        self.write_catalog({"model-y": entry(1, 2)})
        fdel = self.load()
        os.environ["TYPESAFE_API_KEY"] = "test"
        sent = self.stub_match_post(fdel, {"model-y"})
        resolved = fdel.resolve_lead_price("model-y", [])
        self.assertEqual(resolved[:3], ("model-y", "model-y", {"input_per_m": 1.0, "output_per_m": 2.0}))
        self.assertEqual(len(sent), 1)
        del os.environ["TYPESAFE_API_KEY"]
        resolved_again = fdel.resolve_lead_price("model-y", [])
        self.assertEqual(resolved_again[:3], resolved[:3])
        self.assertEqual(len(sent), 1)

    def test_j_errors_warn_and_keep_exact_seed_unverified(self):
        self.write_catalog({"Qwen3-Coder-480B": entry(1, 2), "model-y": entry(1, 2), **self.QWEN_CATALOG})
        self.write_agent("open-big", model="Qwen3-Coder-480B")
        fdel = self.load()
        os.environ["TYPESAFE_API_KEY"] = "test"
        fdel.jev_post = lambda body, key, timeout: (None, "typesafe http 503")
        cands, warnings = fdel.discover(use_jev=True)
        failed = next(c for c in cands if c["id"] == "open-big")
        self.assertEqual(failed["catalog_match"], "unverified-exact")
        self.assertIsNotNone(failed["price"])
        self.assertTrue(any("503" in w for w in warnings))

    def test_j_route_reports_the_jev_catalog_match(self):
        self.write_catalog(dict(self.QWEN_CATALOG))
        self.write_agent("open-big", model="Qwen3-Coder-480B")
        fdel = self.load()
        os.environ["TYPESAFE_API_KEY"] = "test"
        # L1: builtin-x's exact code match ("model-x") is only a seed now; the stub must confirm
        # it too, or the lead's price would come back unknown and route() would refuse to run.
        self.stub_match_post(fdel, {"hostz/qwen3-coder-480b", "model-x"})
        fdel.jev = lambda body: (None, "stubbed")
        result = fdel.route({"deliverable": "x", "acceptance": ["pytest passes"]}, use_jev=True)
        # T5: the per-candidate line collapsed into one summary in `reasons`; detail moved to
        # the full result's `catalog_matches`.
        self.assertIn("catalog: 2 matched by jev", result["reasons"])
        ids = {m["id"] for m in result["catalog_matches"]}
        self.assertIn("open-big", ids)
        open_big = next(m for m in result["catalog_matches"] if m["id"] == "open-big")
        self.assertEqual(open_big["catalog_id"], "hostz/qwen3-coder-480b")

    # ------------------------------------------------ L: no exact model match as a decision

    def test_l1_exact_match_is_seeded_first_but_not_accepted_outright(self):
        self.write_catalog({"model-y": entry(1, 2)})
        self.write_agent("exact", model="model-y")
        fdel = self.load()
        os.environ["TYPESAFE_API_KEY"] = "test"
        sent = self.stub_match_post(fdel, set())   # every noul scores 0.1: Jev rejects the seed
        cands, _ = fdel.discover(use_jev=True)
        c = next(c for c in cands if c["id"] == "exact")
        # The exact key was sent to Jev (not skipped)...
        self.assertTrue(sent)
        model_y_state = next(v for req in sent for v in req["state"]["models"].values()
                             if v["model_id"] == "model-y")
        self.assertEqual(model_y_state["entries"][0], "model-y")   # ranked first
        # ...and, since Jev rejected it, it is NOT accepted outright: no "exact" catalog_match.
        self.assertEqual(c["catalog_match"], "none")
        self.assertIsNone(c["price"])

    def test_l1_exact_match_confirmed_by_jev_becomes_jev_not_exact(self):
        self.write_catalog({"model-y": entry(1, 2)})
        self.write_agent("exact", model="model-y")
        fdel = self.load()
        os.environ["TYPESAFE_API_KEY"] = "test"
        self.stub_match_post(fdel, {"model-y"})
        cands, _ = fdel.discover(use_jev=True)
        c = next(c for c in cands if c["id"] == "exact")
        self.assertEqual(c["catalog_match"], "jev")   # never "exact" when --jev is on

    def test_l2_builtin_without_catalog_id_resolves_via_model_name(self):
        harness = {**FAKE_HARNESS, "builtin": [{"id": "b", "subagent_type": "general-purpose",
                                                 "model": "ph", "model_name": "model-named"}]}
        self.write_catalog({"model-named": entry(3, 4)})
        fdel = self.load(harness=harness)
        c = next(c for c in fdel.discover()[0] if c["id"] == "b")
        self.assertEqual(c["model_id"], "model-named")
        self.assertEqual(c["catalog_id"], "model-named")

    def test_l3_no_jev_path_labels_unverified_and_warns(self):
        self.write_agent("agent-a", model="model-a")
        self.write_catalog({"model-a": entry(1, 2)})
        fdel = self.load()
        cands, warnings = fdel.discover()   # no use_jev
        c = next(c for c in cands if c["id"] == "agent-a")
        self.assertEqual(c["catalog_match"], "unverified-exact")
        self.assertTrue(any("unverified" in w and "TYPESAFE_API_KEY" in w for w in warnings))

    def test_l4_second_discover_sends_no_request(self):
        self.write_catalog({"model-y": entry(1, 2)})
        self.write_agent("exact", model="model-y")
        fdel = self.load()
        os.environ["TYPESAFE_API_KEY"] = "test"
        sent = self.stub_match_post(fdel, {"model-y"})
        fdel.discover(use_jev=True)
        self.assertEqual(len(sent), 1)
        fdel.discover(use_jev=True)
        self.assertEqual(len(sent), 1)   # cache hit: no second request

    # ------------------------------------------------ K: judgment hooks and calibration
    # K1 (best-effort pick above the floor when nothing clears the gate) is exercised by
    # test_low_mass_fit_is_excluded and test_k1_below_floor_still_falls_back_to_direct above.


    def test_k3_candidate_state_separates_family_and_other_evidence(self):
        self.write_agent("agent-a", model="model-a")
        self.write_catalog({"model-a": entry(1, 1)})
        fdel = self.load()
        self.record(fdel, "agent-a", "other-fam", "accepted", 2)
        stats = fdel.ledger_stats()
        c = next(c for c in fdel.discover()[0] if c["id"] == "agent-a")
        ranked = fdel.rank_candidates([c], {"family": "fam"}, stats)
        bands = fdel.price_bands(ranked)
        state = fdel.candidate_semantic_state(ranked[0], "fam", bands, stats)
        self.assertEqual(state["family"], "0/0")
        self.assertEqual(state["other"], "2/2")

    def test_k4_route_returns_route_id_and_writes_calibration_ledger(self):
        self.write_agent("agent-a", model="model-a")
        self.write_catalog({"model-a": entry(1, 2), "claude-opus-5-5": entry(50, 100)})
        out = json.loads(self.run_cli("route", stdin=json.dumps(
            {"deliverable": "x", "acceptance": ["pytest passes"], "family": "fam"})).stdout)
        self.assertIn("route_id", out)
        routes_path = self.tmp / "state" / "routes.jsonl"
        self.assertTrue(routes_path.is_file())
        rec = json.loads(routes_path.read_text().splitlines()[-1])
        self.assertEqual(rec["route_id"], out["route_id"])
        self.assertEqual(rec["picked"], "agent-a")
        self.assertEqual(rec["family"], "fam")
        self.assertNotIn("required_fit", rec)
        self.assertNotIn("required_fit", out)
        brief = json.loads(self.run_cli("route", "--brief", stdin=json.dumps(
            {"deliverable": "x", "acceptance": ["pytest passes"], "family": "fam"})).stdout)
        self.assertIn("route_id", brief)   # A: brief output carries route_id too

    def test_k4_record_route_links_verdict_and_copies_mass(self):
        fdel = self.fit_setup()
        self.stub_jev(fdel, {"model-a": 1, "model-b": (2.9, {0: 0, 1: .1, 2: .9})})
        result = fdel.route({"deliverable": "x", "family": "fam"}, use_jev=True)
        route_id = result["route_id"]
        picked = result["candidate"]["id"]
        self.assertEqual(picked, "agent-a")
        out = json.loads(self.run_cli("record", "--candidate", picked, "--family", "fam",
                                      "--outcome", "accepted", "--route", route_id).stdout)
        self.assertEqual(out["recorded"]["route_id"], route_id)
        self.assertEqual(out["recorded"]["fit"], 1)
        line = json.loads(fdel.LEDGER.read_text().splitlines()[-1])
        self.assertEqual(line["fit"], out["recorded"]["fit"])

    def test_k4_record_unknown_route_id_is_an_error(self):
        self.write_agent("agent-a", model="model-a")
        self.write_catalog({"model-a": entry(1, 1)})
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_cli("record", "--candidate", "agent-a", "--family", "f", "--outcome", "accepted",
                         "--route", "no-such-route-id")

    def test_k4_stats_calibration_shows_bucket_and_family_rates(self):
        fdel = self.load()
        fdel.LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with fdel.LEDGER.open("a") as f:
            for mass, outcome, family in [(0.1, "rejected", "fam1"), (0.4, "accepted", "fam1"),
                                          (0.6, "accepted", "fam2"), (0.9, "accepted", "fam2")]:
                f.write(json.dumps({"candidate": "x", "family": family, "outcome": outcome, "mass": mass}) + "\n")
        out = self.run_cli("stats", "--calibration").stdout
        self.assertIn("<0.25", out)
        self.assertIn("0/1", out)
        self.assertIn("0.25-0.5", out)
        self.assertIn("fam1", out)
        self.assertIn("1/2", out)
        self.assertIn("fam2", out)
        self.assertIn("2/2", out)
        self.assertIn("Overrides:", out)

    def test_k5_override_stores_both_ids_and_counts_separately(self):
        fdel = self.fit_setup()
        self.stub_jev(fdel, {"model-a": 1, "model-b": (2.9, {0: 0, 1: .1, 2: .9})})
        result = fdel.route({"deliverable": "x", "family": "fam"}, use_jev=True)
        route_id = result["route_id"]
        picked = result["candidate"]["id"]
        self.assertEqual(picked, "agent-a")
        dispatched = "agent-b"
        out = json.loads(self.run_cli("record", "--candidate", dispatched, "--family", "fam", "--outcome", "accepted",
                                      "--route", route_id, "--override", "cheaper; Jev under-rated it").stdout)
        self.assertEqual(out["recorded"]["override"], "cheaper; Jev under-rated it")
        self.assertEqual(out["recorded"]["route_picked"], picked)
        self.assertEqual(out["recorded"]["dispatched"], dispatched)
        _bucket_rows, _family_rows, (o_acc, o_n) = fdel.calibration_stats()
        self.assertEqual((o_acc, o_n), (1, 1))
        stats_out = self.run_cli("stats", "--calibration").stdout
        self.assertIn("Overrides: 1/1", stats_out)

    def test_k6_skill_md_documents_evidence_not_verdict_section(self):
        text = skill_docs_text()
        self.assertIn("evidence", text.lower())
        for token in ("review_required", "advisory", "--route", "--override", "TYPESAFE_FIT_FLOOR",
                     "routes.jsonl", "--calibration"):
            self.assertIn(token, text, token)

    # ------------------------------------------------------------ M. Open-source hygiene
    # load_harness() keeps its original signature (dict only, no tuple/path churn — review
    # finding M.b); the override path is a separate lookup, harness_override_path().

    def fresh_module(self):
        """Import fdel without the test helper's load_harness stub, for real-override tests."""
        spec = importlib.util.spec_from_file_location("fdel", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_m_shipped_harness_is_neutral(self):
        harness = json.loads((SKILL_DIR / "harness.json").read_text())
        self.assertNotIn("default_lead", harness)
        self.assertEqual(harness.get("disabled", []), [])
        self.assertEqual(harness.get("probation", []), [])
        for b in harness["builtin"]:
            self.assertTrue({"id", "model_selector", "model_name"} <= set(b))
            self.assertNotIn("catalog_id", b)
            self.assertFalse(b["id"].startswith("proxy-"))

    def test_m_load_harness_with_no_override_returns_shipped_dict(self):
        # setUp seeds a default_lead override so CLI/subprocess tests have a lead without
        # threading --lead everywhere; remove it here to test the true no-override path.
        (Path(os.environ["FAST_DELEGATE_STATE"]) / "harness.json").unlink()
        module = self.fresh_module()
        harness = module.load_harness()
        self.assertIsInstance(harness, dict)
        self.assertNotIn("default_lead", harness)
        self.assertIsNone(module.harness_override_path())

    def test_m_load_harness_merges_override_from_fast_delegate_state(self):
        override = {
            "default_lead": "custom-lead",
            "disabled": [{"id": "agent-x", "why": "test"}],
            "probation": [{"id": "agent-y", "why": "test"}],
        }
        state_dir = Path(os.environ["FAST_DELEGATE_STATE"])
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "harness.json").write_text(json.dumps(override))
        module = self.fresh_module()
        harness = module.load_harness()
        self.assertEqual(harness["default_lead"], "custom-lead")
        disabled_ids = {d["id"] for d in harness["disabled"]}
        probation_ids = {p["id"] for p in harness["probation"]}
        self.assertIn("agent-x", disabled_ids)
        self.assertIn("agent-y", probation_ids)
        self.assertEqual(module.harness_override_path(), state_dir / "harness.json")

    def test_m_load_harness_merge_by_id_keeps_unrelated_shipped_builtins(self):
        # Override replaces only the builtin it names by id; other shipped builtins survive.
        override = {"builtin": [{"id": "haiku", "subagent_type": "general-purpose",
                                 "model": "haiku", "model_name": "custom-haiku-id"}]}
        state_dir = Path(os.environ["FAST_DELEGATE_STATE"])
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "harness.json").write_text(json.dumps(override))
        module = self.fresh_module()
        harness = module.load_harness()
        by_id = {b["id"]: b for b in harness["builtin"]}
        self.assertEqual(by_id["haiku"]["model_name"], "custom-haiku-id")
        self.assertIn("sonnet", by_id)   # untouched shipped builtin still present

    def test_m_load_harness_respects_fast_delegate_harness_env(self):
        override = {"default_lead": "override-lead"}
        custom_path = self.tmp / "custom.json"
        custom_path.write_text(json.dumps(override))
        os.environ["FAST_DELEGATE_HARNESS"] = str(custom_path)
        module = self.fresh_module()
        harness = module.load_harness()
        self.assertEqual(harness["default_lead"], "override-lead")
        self.assertEqual(module.harness_override_path(), custom_path)

    def test_m_fix_a_malformed_override_is_a_hard_error_naming_the_file(self):
        # A non-JSON override file must fail closed (SystemExit), not be silently ignored:
        # silently ignoring it would fail open and re-enable ids the override meant to disable.
        state_dir = Path(os.environ["FAST_DELEGATE_STATE"])
        state_dir.mkdir(parents=True, exist_ok=True)
        bad_path = state_dir / "harness.json"
        bad_path.write_text("{not valid json")
        module = self.fresh_module()
        with self.assertRaises(SystemExit) as ctx:
            module.load_harness()
        self.assertIn(str(bad_path), str(ctx.exception))

    def test_m_fix_a_non_object_override_is_a_hard_error_naming_the_file(self):
        state_dir = Path(os.environ["FAST_DELEGATE_STATE"])
        state_dir.mkdir(parents=True, exist_ok=True)
        bad_path = state_dir / "harness.json"
        bad_path.write_text(json.dumps(["not", "an", "object"]))
        module = self.fresh_module()
        with self.assertRaises(SystemExit) as ctx:
            module.load_harness()
        self.assertIn(str(bad_path), str(ctx.exception))

    def test_m_discover_reports_override_via_warning_and_keeps_two_tuple(self):
        override = {"default_lead": "override-lead"}
        state_dir = Path(os.environ["FAST_DELEGATE_STATE"])
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "harness.json").write_text(json.dumps(override))
        module = self.fresh_module()
        result = module.discover()
        self.assertEqual(len(result), 2)   # fix b: discover() stays (candidates, warnings)
        candidates, warnings = result
        self.assertTrue(any(str(state_dir / "harness.json") in w for w in warnings))
        buf = io.StringIO()
        module.print_discovery(candidates, warnings, as_json=True, out=buf)
        output = json.loads(buf.getvalue())
        self.assertTrue(any(str(state_dir / "harness.json") in w for w in output["warnings"]))

    def test_m_route_errors_with_no_lead_mentioning_override_file(self):
        state_dir = Path(os.environ["FAST_DELEGATE_STATE"])
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "harness.json").write_text(json.dumps({}))   # no default_lead
        module = self.fresh_module()
        with self.assertRaises(SystemExit) as ctx:
            module.route({"deliverable": "test"})
        self.assertIn(str(state_dir / "harness.json"), str(ctx.exception))

    def test_m_no_home_paths_in_tracked_files(self):
        result = subprocess.run(["git", "ls-files", "skills/fast-delegate/"],
                                 cwd=ROOT, capture_output=True, text=True)
        tracked = result.stdout.strip().split("\n") if result.stdout.strip() else []
        for path in tracked:
            full_path = ROOT / path
            if full_path.is_file():
                content = full_path.read_text()
                self.assertNotIn("/home/", content,
                    f"File {path} contains /home/ path; should use relative paths or environment variables")

    def test_m_no_ledger_note_format_in_tracked_files(self):
        result = subprocess.run(["git", "ls-files", "skills/fast-delegate/"],
                                 cwd=ROOT, capture_output=True, text=True)
        tracked = result.stdout.strip().split("\n") if result.stdout.strip() else []
        ledger_pattern = re.compile(r'"candidate"\s*:\s*"[^"]+"\s*,\s*"family"\s*:', re.MULTILINE)
        for path in tracked:
            full_path = ROOT / path
            if full_path.is_file() and not path.endswith(".py"):
                content = full_path.read_text()
                self.assertFalse(ledger_pattern.search(content),
                    f"File {path} contains ledger note format; should not hardcode test data")

    def test_m_fix_d_skill_md_names_no_personal_agent_ids(self):
        # Shipped docs may use the generic wildcard `proxy-*`. Concrete local worker ids
        # such as proxy-gpt-... must stay out of SKILL.md and REFERENCE.md.
        for name in ("SKILL.md", "REFERENCE.md"):
            text = (SKILL_DIR / name).read_text()
            self.assertIsNone(re.search(r"proxy-gpt-", text), name)

    # ================================================================
    # 2026-09-25 fixes: S1-S3 (bugs), B1-B3 (bugs), T1-T7 (tool feedback), N1-N7 (harness-aware)
    # ================================================================

    # ------------------------------------------------------------ S1: catalog fingerprint

    def test_s1_fingerprint_is_content_based_not_key_count_and_mtime(self):
        fdel = self.load()
        fp_a = fdel.catalog_fingerprint({"alpha-7b": entry(1, 2)})
        fp_b = fdel.catalog_fingerprint({"beta-7b": entry(1, 2)})
        # Same key count, same price shape -> the old "key count + truncated mtime" fingerprint
        # could collide; a content hash of keys+prices must not.
        self.assertNotEqual(fp_a, fp_b)
        self.assertEqual(fp_a, fdel.catalog_fingerprint({"alpha-7b": entry(1, 2)}))

    def test_s1_stale_cached_catalog_id_is_dropped_not_keyerror(self):
        """S1: a cached catalog_id from a stale/collided fingerprint must be dropped and
        rematched, never dereferenced -- reproduces the sol lead's KeyError repro directly
        against jev_match_models's cache-loading step."""
        fdel = self.load()
        catalog2 = {"beta-7b": entry(1, 2)}
        fp2 = fdel.catalog_fingerprint(catalog2)
        cache_path = fdel.match_cache_path()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        # Poison the cache: under catalog2's own fingerprint, claim model-7b resolved to
        # "alpha-7b", a key that does not exist in catalog2 (simulates a collision/stale write).
        cache_path.write_text(json.dumps({fp2: {"model-7b": {"catalog_id": "alpha-7b",
                                                              "entries": ["alpha-7b"], "score": 0.99}}}))
        results, warnings, unjudged = fdel.jev_match_models(["model-7b"], catalog2, {})
        # The poisoned entry (catalog_id "alpha-7b", absent from catalog2) must never surface;
        # the model gets a fresh resolution instead (here: no shortlist at all against catalog2,
        # so a definitive "no match", not a crash and not the stale price).
        self.assertNotEqual(results.get("model-7b", {}).get("catalog_id"), "alpha-7b")

    def test_s1_discover_survives_a_catalog_swap_that_collided_under_the_old_scheme(self):
        """Full-flow version of the sol repro: catalog A resolves and caches a match, then the
        catalog is swapped for one that no longer has that key; a second --jev discover must not
        KeyError and must not silently keep the stale price."""
        self.write_agent("open-x", model="alpha-7b")   # exact-match model id, seeds the shortlist
        self.write_catalog({"alpha-7b": entry(1, 2)})
        fdel = self.load()
        os.environ["TYPESAFE_API_KEY"] = "test"
        self.stub_match_post(fdel, {"alpha-7b"})
        cands, _ = fdel.discover(use_jev=True)
        self.assertEqual(next(c for c in cands if c["id"] == "open-x")["catalog_id"], "alpha-7b")
        # Swap the catalog for one where "alpha-7b" no longer exists.
        self.write_catalog({"beta-7b": entry(3, 4)})
        fdel2 = self.load()
        os.environ["TYPESAFE_API_KEY"] = "test"
        self.stub_match_post(fdel2, set())   # beta-7b is not judged as a match for alpha-7b
        cands2, _ = fdel2.discover(use_jev=True)   # must not raise KeyError
        c2 = next(c for c in cands2 if c["id"] == "open-x")
        self.assertIsNone(c2["price"])   # no stale alpha-7b price survives the swap

    # ------------------------------------------------------------ S2: out-of-domain fit levels

    def test_s2_out_of_domain_level_is_dropped_from_probabilities(self):
        fdel = self.load()
        probs = fdel.parse_probabilities({"999": 1}, valid_levels=fdel.FIT_LEVELS)
        self.assertEqual(probs, {})   # 999 is outside the 0..4 fit scale
        probs2 = fdel.parse_probabilities({"2": 1.0, "999": 1.0, "1": 2.0}, valid_levels=fdel.FIT_LEVELS)
        self.assertEqual(probs2, {2: 1.0})   # "1": 2.0 is out of [0,1], also dropped

    def test_malformed_advisory_distribution_has_no_usable_score(self):
        fdel = self.load()
        for probs in ({999: 1}, {0: .2}, {0: -1}, {0: float("nan")}):
            self.assertEqual(fdel.advisory_fit({"fit_c0": 0, "fit_c0_probs": probs}, "c0"), (None, None, None))


    def test_s2_parse_jev_answers_applies_the_fit_level_domain(self):
        fdel = self.load()
        questions = {"fit_c0": fdel.fit_question("c0")}
        payload = {"answers": {"fit_c0": {"score": 0, "probabilities": {"999": 1.0}}}}
        out, err = fdel.parse_jev_answers(payload, questions, 0.4)
        self.assertIsNone(err)
        self.assertNotIn("fit_c0_probs", out)   # no valid levels survived
        self.assertEqual(out["fit_c0"], 0)

    # ------------------------------------------------------------ S3: record --route override required

    def test_s3_record_route_mismatch_requires_override(self):
        fdel = self.fit_setup()
        self.stub_jev(fdel, {"model-a": 1, "model-b": (2.9, {0: 0, 1: .1, 2: .9})})
        result = fdel.route({"deliverable": "x", "family": "fam"}, use_jev=True)
        route_id = result["route_id"]
        picked = result["candidate"]["id"]
        self.assertEqual(picked, "agent-a")
        with self.assertRaises(subprocess.CalledProcessError) as ctx:
            self.run_cli("record", "--candidate", "agent-b", "--family", "fam",
                         "--outcome", "accepted", "--route", route_id)
        self.assertIn("--override", ctx.exception.stderr)
        # With --override it succeeds (already covered by test_k5_..., re-verify the error text
        # doesn't fire when candidate == picked).
        out = json.loads(self.run_cli("record", "--candidate", picked, "--family", "fam",
                                      "--outcome", "accepted", "--route", route_id).stdout)
        self.assertEqual(out["recorded"]["candidate"], picked)

    # ------------------------------------------------------------ B1: partial Jev outage

    def test_b1_only_the_failed_batch_is_unjudged_not_every_candidate(self):
        fdel = self.load()
        self.write_catalog({"shared-exact": entry(1, 2), "a1": entry(1, 1), "a2": entry(1, 1),
                            "a3": entry(1, 1), "zzz": entry(1, 1)})
        catalog, _ = fdel.fetch_litellm_catalog()
        calls = []

        def fake_post(body, key, timeout):
            calls.append(body)
            if len(calls) == 1:
                # First batch (a1, a2, a3, shared-exact) succeeds; Jev rejects shared-exact.
                answers = {}
                for qid, q in body["questions"].items():
                    m, j = qid.split("_e")
                    entry_key = body["state"]["models"][m]["entries"][int(j)]
                    answers[qid] = {"noul": 0.9 if entry_key != "shared-exact" else 0.05}
                return {"answers": answers}, None
            return None, "typesafe http 503"   # second batch (zzz) fails outright

        fdel.jev_post = fake_post
        os.environ["TYPESAFE_API_KEY"] = "test"
        os.environ["TYPESAFE_MATCH_THRESHOLD"] = "0.5"
        # Every model has an exact priced catalog key (mirrors the sol repro's "5 builtins, each
        # with an exact priced catalog key"); seed each with itself so all get a real shortlist
        # and reach a Jev batch instead of resolving as an immediate no-match.
        hints = {m: m for m in ("a1", "a2", "a3", "shared-exact", "zzz")}
        results, warnings, unjudged = fdel.jev_match_models(
            ["a1", "a2", "a3", "shared-exact", "zzz"], catalog, {}, hints=hints)
        self.assertNotIn("shared-exact", unjudged)   # its own batch was judged
        self.assertIn("zzz", unjudged)                # its batch failed

    def test_b1_discover_does_not_revive_a_jev_rejected_exact_match(self):
        """Reproduces the opus lead's B1 finding directly through discover(): a candidate whose
        own batch Jev judged and rejected must stay catalog_match "none"/unpriced, not fall back
        to "unverified-exact" just because a different batch failed."""
        self.write_agent("shared", model="shared-exact")
        self.write_agent("zzz", model="zzz-model")
        self.write_agent("dummy-a", model="aaa-model")
        self.write_agent("dummy-b", model="bbb-model")
        self.write_catalog({"shared-exact": entry(1, 2), "zzz-model": entry(3, 4), "model-x": entry(5, 6),
                            "aaa-model": entry(1, 1), "bbb-model": entry(1, 1)})
        fdel = self.load()
        os.environ["TYPESAFE_API_KEY"] = "test"
        calls = []

        def fake_post(body, key, timeout):
            calls.append(body)
            names = {v["model_id"] for v in body["state"]["models"].values()}
            if "shared-exact" in names or "model-x" in names:
                answers = {}
                for qid, q in body["questions"].items():
                    m, j = qid.split("_e")
                    entry_key = body["state"]["models"][m]["entries"][int(j)]
                    answers[qid] = {"noul": 0.05}   # Jev explicitly rejects everything it judges
                return {"answers": answers}, None
            return None, "typesafe http 503"        # zzz-model's batch fails outright

        fdel.jev_post = fake_post
        cands, warnings = fdel.discover(use_jev=True)
        shared = next(c for c in cands if c["id"] == "shared")
        zzz = next(c for c in cands if c["id"] == "zzz")
        # shared-exact was judged (and rejected): must not be revived as unverified-exact.
        self.assertEqual(shared["catalog_match"], "none")
        self.assertIsNone(shared["price"])
        # zzz-model's batch failed outright: falls back to unverified-normalized/-exact.
        self.assertTrue(zzz["catalog_match"].startswith("unverified-"))

    # ------------------------------------------------------------ B2: unpriced seed vs priced key

    def test_b2_pick_price_entry_prefers_a_priced_key_over_an_unpriced_seed(self):
        fdel = self.load()
        catalog = {"aaa-foo-7b": {"max_input_tokens": 1000}, "zzz-foo-7b": entry(1, 2)}
        # "aaa-foo-7b" is alphabetically first and unpriced (a realistic seed with no price data);
        # the priced "zzz-foo-7b" must still win.
        self.assertEqual(fdel.pick_price_entry(["aaa-foo-7b", "zzz-foo-7b"], catalog, []), "zzz-foo-7b")

    def test_b2_match_shortlist_seed_does_not_block_the_priced_match(self):
        """Full path: an unpriced exact-match seed must not leave the candidate unpriced when a
        same-model priced key exists and Jev confirms both."""
        self.write_agent("open-y", model="aaa-foo-7b")
        self.write_catalog({"aaa-foo-7b": {"max_input_tokens": 1000}, "zzz-foo-7b": entry(1, 2),
                            "model-x": entry(5, 6)})
        fdel = self.load()
        os.environ["TYPESAFE_API_KEY"] = "test"
        self.stub_match_post(fdel, {"aaa-foo-7b", "zzz-foo-7b", "model-x"})
        cands, _ = fdel.discover(use_jev=True)
        c = next(c for c in cands if c["id"] == "open-y")
        self.assertIsNotNone(c["price"])
        self.assertEqual(c["catalog_id"], "zzz-foo-7b")

    # ------------------------------------------------------------ B3: override mass fields

    def test_b3_override_stores_dispatched_mass_and_route_picked_mass(self):
        fdel = self.fit_setup()
        # Both candidates need level probabilities (not just a bare score) so both get a defined
        # mass -- agent-a's low mass must not equal agent-b's, or the "distinct mass" assertion
        # below would be trivially true for the wrong reason (both None).
        self.stub_jev(fdel, {"model-a": (1.0, {0: .6, 1: .3, 2: .1}),
                             "model-b": (2.9, {0: 0, 1: .1, 2: .9})})
        result = fdel.route({"deliverable": "x", "family": "fam"}, use_jev=True)
        route_id = result["route_id"]
        picked = result["candidate"]["id"]
        self.assertEqual(picked, "agent-a")
        out = json.loads(self.run_cli("record", "--candidate", "agent-b", "--family", "fam",
                                      "--outcome", "accepted", "--route", route_id,
                                      "--override", "cheaper; still fine").stdout)
        rec = out["recorded"]
        self.assertEqual(rec["dispatched"], "agent-b")
        self.assertEqual(rec["route_picked"], "agent-a")
        self.assertEqual(rec["fit"], 2.9)
        self.assertEqual(rec["fit_probabilities"], {"0": 0, "1": .1, "2": .9})
        self.assertNotIn("mass", rec)
        self.assertNotIn("route_picked_mass", rec)

    # ------------------------------------------------------------ T1: isolation line + FAST_DELEGATE_CACHE


    def test_t1_fast_delegate_cache_overrides_match_cache_path(self):
        fdel = self.load()
        custom = self.tmp / "custom-cache.json"
        os.environ["FAST_DELEGATE_CACHE"] = str(custom)
        self.assertEqual(fdel.match_cache_path(), custom)

    # ------------------------------------------------------------ T2: fallback filling


    # ------------------------------------------------------------ T3: return_budget


    # ------------------------------------------------------------ T4: judged keyed by candidate id

    def test_judged_is_keyed_by_candidate_with_distribution_evidence(self):
        fdel = self.fit_setup()
        self.stub_jev(fdel, {"model-a": (0, {0: 1}), "model-b": self.STRONG})
        out = fdel.route({"deliverable": "x"})
        by_id = {r["id"]: r for r in out["judged"]}
        self.assertEqual(by_id["agent-a"], {"id": "agent-a", "fit": 0, "fit_probabilities": {0: 1}})
        self.assertEqual(fdel.brief_output(out)["judged"], out["judged"])


    # ------------------------------------------------------------ T5: collapsed catalog reasons

    def test_t5_catalog_match_reasons_collapse_to_one_summary_line(self):
        # Covered end-to-end in test_j_route_reports_the_jev_catalog_match (updated above); this
        # adds the boundary case of zero jev matches -> no summary line at all.
        fdel = self.load()
        result = fdel.route_actual({"deliverable": "x", "acceptance": ["pytest passes"]}, use_jev=False)
        self.assertFalse(any(r.startswith("catalog:") for r in result["reasons"]))
        self.assertNotIn("catalog_matches", result)

    # ------------------------------------------------------------ T6: margin side naming


    # ------------------------------------------------------------ T7: --tokens doc hint

    def test_t7_skill_md_explains_where_tokens_comes_from(self):
        text = (SKILL_DIR / "SKILL.md").read_text()
        self.assertIn("--tokens", text)
        self.assertIn("usage notification", text)

    # ------------------------------------------------------------ N: harness-aware candidates

    def _clear_harness_env(self):
        for k in ("CLAUDECODE", "CODEX_THREAD_ID", "CODEX_SESSION_ID"):
            os.environ.pop(k, None)

    def test_n1_auto_detect_prefers_claude_over_codex(self):
        fdel = self.load()
        self._clear_harness_env()
        os.environ["CLAUDECODE"] = "1"
        os.environ["CODEX_THREAD_ID"] = "t1"
        self.assertEqual(fdel.detect_harness("auto"), fdel.HARNESS_NATIVE)

    def test_n1_auto_detect_falls_back_to_codex(self):
        fdel = self.load()
        self._clear_harness_env()
        os.environ["CODEX_SESSION_ID"] = "s1"
        self.assertEqual(fdel.detect_harness("auto"), "codex")

    def test_n1_auto_detect_errors_with_neither_marker(self):
        fdel = self.load()
        self._clear_harness_env()
        with self.assertRaises(SystemExit) as ctx:
            fdel.detect_harness("auto")
        self.assertIn("--harness", str(ctx.exception))

    def test_n1_route_reports_harness_in_full_and_brief_output(self):
        self.write_agent("agent-a", model="model-a")
        self.write_catalog({"model-a": entry(1, 2), "model-x": entry(50, 100)})
        fdel = self.load()
        result = fdel.route({"deliverable": "x", "acceptance": ["pytest passes"]},
                            harness=fdel.HARNESS_NATIVE, spawnable=["agent-a"])
        self.assertEqual(result["harness"], fdel.HARNESS_NATIVE)
        brief = fdel.brief_output(result)
        self.assertEqual(brief["harness"], fdel.HARNESS_NATIVE)

    def test_n3_codex_without_spawnable_is_a_descriptive_error(self):
        fdel = self.load()
        with self.assertRaises(SystemExit) as ctx:
            fdel.discover(harness_name="codex")
        self.assertIn("--spawnable", str(ctx.exception))

    def test_n3_claude_spawnable_filter_drops_unlisted_candidates(self):
        self.write_agent("agent-a", model="model-a")
        self.write_agent("agent-b", model="model-b")
        self.write_catalog({"model-a": entry(1, 2), "model-b": entry(3, 4)})
        fdel = self.load()
        cands, warnings = fdel.discover(harness_name=fdel.HARNESS_NATIVE, spawnable=["agent-a", "builtin-x"])
        ids = {c["id"] for c in cands}
        self.assertIn("agent-a", ids)
        self.assertNotIn("agent-b", ids)
        self.assertTrue(any("not spawnable in" in w and "agent-b" in w for w in warnings))

    def test_n3_non_spawnable_candidates_never_reach_the_jev_request(self):
        self.write_agent("agent-a", model="model-a")
        self.write_agent("agent-b", model="model-b")
        self.write_catalog({"model-a": entry(1, 2), "model-b": entry(3, 4), "model-x": entry(5, 6)})
        fdel = self.load()
        os.environ["TYPESAFE_API_KEY"] = "test"
        sent = self.stub_match_post(fdel, {"model-a", "model-b", "model-x"})
        fdel.discover(use_jev=True, harness_name=fdel.HARNESS_NATIVE, spawnable=["agent-a", "builtin-x"])
        for body in sent:
            names = {v["model_id"] for v in body["state"]["models"].values()}
            self.assertNotIn("model-b", names)


    def test_n4_lead_accepts_a_spawnable_name(self):
        self.write_catalog({"gpt-6-astra": entry(1, 2), "gpt-6-sol": entry(5, 6), "model-x": entry(50, 100)})
        fdel = self.load()
        result = fdel.route({"deliverable": "x", "acceptance": ["pytest passes"]},
                            harness="codex", spawnable=["gpt-6-astra", "gpt-6-sol"], lead="gpt-6-sol")
        self.assertEqual(result["decision"], "delegate")
        self.assertEqual(result["candidate"]["id"], "gpt-6-astra")


    # ------------------------------------------------------------ FIX 1: record --route validation

    def test_fix1_record_route_accepts_non_discovered_candidate(self):
        """Fix 1: record --route validates against route record's judged candidates, not discover().

        Real bug: when a route comes from codex with a candidate like 'gpt-6-luna', record
        --candidate gpt-6-luna --route <id> fails with 'unknown candidate' on b372350
        because that id is not in discover(). With the fix, it succeeds because the route
        record has it in judged candidates."""
        fdel = self.load()
        # Manually create a route record with a non-discovered candidate id (like codex would)
        route_id = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + "-test1234"
        route_rec = {
            "route_id": route_id,
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "family": "fam",
            "picked": "gpt-6-luna",  # This id does not exist in discover()
            "required_fit": 2.0,
            "candidates": [{"id": "gpt-6-luna", "mass": 0.9, "fit": 2.9}],
            "difficulty": None,
            "independence": None
        }
        fdel.ROUTES_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with fdel.ROUTES_LEDGER.open("a") as f:
            f.write(json.dumps(route_rec) + "\n")

        # Without the fix, this fails with "unknown candidate 'gpt-6-luna'"
        # With the fix, it succeeds because gpt-6-luna is in the route's judged candidates
        out = json.loads(self.run_cli("record", "--candidate", "gpt-6-luna", "--family", "fam",
                                      "--outcome", "accepted", "--route", route_id).stdout)
        self.assertEqual(out["recorded"]["candidate"], "gpt-6-luna")
        self.assertEqual(out["recorded"]["route_id"], route_id)

    # ------------------------------------------------------------ FIX 2: handoff security line


    # ------------------------------------------------------------ FIX 3: brief spawn object


    # ================================================================
    # P: catalog provenance, billing mode, eligibility, richer pricing
    # ================================================================

    class _Resp(io.BytesIO):
        """A stubbed urlopen response usable as a context manager, with an optional .headers
        dict for ETag fallback tests (section 1)."""
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _use_real_catalog_cache(self, subdir):
        """Point FAST_DELEGATE_CATALOG away (so fetch_litellm_catalog_meta hits the real
        cache/network path) and XDG_CACHE_HOME at a fresh temp dir, then load a fresh module so
        its LITELLM_CACHE/LITELLM_META/CACHE_DIR constants resolve under that temp dir."""
        os.environ.pop("FAST_DELEGATE_CATALOG", None)
        os.environ["XDG_CACHE_HOME"] = str(self.tmp / subdir)
        return self.load(harness=FAKE_HARNESS)

    def test_p1_refresh_writes_full_provenance_meta(self):
        fdel = self._use_real_catalog_cache("cache-p1a")
        payload = json.dumps({f"model-{i}": entry(1, 2) for i in range(60)}).encode()

        def fake_urlopen(req, timeout=None):
            if "api.github.com" in req.full_url:
                return self._Resp(json.dumps({"sha": "a" * 40}).encode())
            return self._Resp(payload)

        fdel.urllib.request.urlopen = fake_urlopen
        catalog, meta, warnings = fdel.fetch_litellm_catalog_meta(refresh=True)
        self.assertEqual(len(catalog), 60)
        self.assertTrue(fdel.LITELLM_META.is_file())
        saved = json.loads(fdel.LITELLM_META.read_text())
        for key in ("repo", "commit", "path", "sha256", "fetched_at"):
            self.assertIn(key, saved, key)
            self.assertIn(key, meta, key)
        self.assertEqual(saved["commit"], "a" * 40)
        self.assertEqual(saved["repo"], fdel.LITELLM_REPO)
        self.assertEqual(saved["path"], fdel.LITELLM_PATH)
        self.assertEqual(json.loads(fdel.LITELLM_CACHE.read_text()), catalog)

    def test_p1_etag_fallback_when_commit_api_unavailable(self):
        fdel = self._use_real_catalog_cache("cache-p1b")
        payload = json.dumps({f"model-{i}": entry(1, 2) for i in range(60)}).encode()

        class RespWithEtag(self._Resp):
            headers = {"ETag": '"xyz789"'}

        def fake_urlopen(req, timeout=None):
            if "api.github.com" in req.full_url:
                raise Exception("rate limited")
            return RespWithEtag(payload)

        fdel.urllib.request.urlopen = fake_urlopen
        catalog, meta, warnings = fdel.fetch_litellm_catalog_meta(refresh=True)
        self.assertEqual(len(catalog), 60)
        self.assertNotIn("commit", meta)
        self.assertEqual(meta.get("etag"), '"xyz789"')
        self.assertRegex(meta["sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(any("github commit api unavailable" in w.lower() for w in warnings))

    def test_p1_validate_catalog_bytes_rejects_bad_shape_or_json(self):
        fdel = self.load()
        catalog, err = fdel.validate_catalog_bytes(json.dumps({"a": 1, "b": 2}).encode())
        self.assertIsNone(catalog)
        self.assertIn("shape", err)
        catalog2, err2 = fdel.validate_catalog_bytes(b"not json at all")
        self.assertIsNone(catalog2)
        self.assertIsNotNone(err2)
        good = json.dumps({f"m{i}": entry(1, 2) for i in range(60)}).encode()
        catalog3, err3 = fdel.validate_catalog_bytes(good)
        self.assertIsNotNone(catalog3)
        self.assertIsNone(err3)

    def test_p1_bad_download_never_replaces_cached_snapshot(self):
        fdel = self._use_real_catalog_cache("cache-p1c")
        good_catalog = {f"model-{i}": entry(1, 2) for i in range(60)}
        good_meta = {"repo": fdel.LITELLM_REPO, "commit": "b" * 40, "path": fdel.LITELLM_PATH,
                    "sha256": "deadbeef", "fetched_at": time.time()}
        fdel.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        fdel.LITELLM_CACHE.write_text(json.dumps(good_catalog))
        fdel.LITELLM_META.write_text(json.dumps(good_meta))

        # Case A: an oversized download must never replace the last-known-good snapshot.
        fdel.CATALOG_MAX_BYTES = 100

        def fake_urlopen_big(req, timeout=None):
            if "api.github.com" in req.full_url:
                return self._Resp(json.dumps({"sha": "c" * 40}).encode())
            return self._Resp(json.dumps(good_catalog).encode())  # far over the 100-byte bound

        fdel.urllib.request.urlopen = fake_urlopen_big
        catalog_a, meta_a, warnings_a = fdel.fetch_litellm_catalog_meta(refresh=True)
        self.assertEqual(catalog_a, good_catalog)
        self.assertEqual(meta_a["commit"], "b" * 40)  # last-known-good, not the new "c"*40
        self.assertEqual(json.loads(fdel.LITELLM_CACHE.read_text()), good_catalog)
        self.assertEqual(json.loads(fdel.LITELLM_META.read_text())["commit"], "b" * 40)
        self.assertTrue(any("byte" in w for w in warnings_a))

        # Case B: malformed JSON must never replace it either.
        fdel.CATALOG_MAX_BYTES = 30 * 1024 * 1024

        def fake_urlopen_bad_json(req, timeout=None):
            if "api.github.com" in req.full_url:
                return self._Resp(json.dumps({"sha": "d" * 40}).encode())
            return self._Resp(b"not json at all")

        fdel.urllib.request.urlopen = fake_urlopen_bad_json
        catalog_b, meta_b, warnings_b = fdel.fetch_litellm_catalog_meta(refresh=True)
        self.assertEqual(catalog_b, good_catalog)
        self.assertEqual(meta_b["commit"], "b" * 40)
        self.assertEqual(json.loads(fdel.LITELLM_CACHE.read_text()), good_catalog)
        self.assertTrue(any("JSON" in w for w in warnings_b))

    def test_p2_route_reports_catalog_provenance_and_stale_prices_flag(self):
        self.write_agent("agent-a", model="model-a")
        fdel = self._use_real_catalog_cache("cache-p2")
        stale_meta = {"repo": fdel.LITELLM_REPO, "commit": "e" * 40, "path": fdel.LITELLM_PATH,
                     "sha256": "deadbeef", "fetched_at": time.time() - fdel.CATALOG_HARD_STALE_S - 3600}
        catalog = {"model-a": entry(1, 2), "model-x": entry(5, 6)}
        fdel.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        fdel.LITELLM_CACHE.write_text(json.dumps(catalog))
        fdel.LITELLM_META.write_text(json.dumps(stale_meta))
        # Network stays blocked (self.load()'s default _no_network stub): a repeated failed
        # refresh attempt must still fall back to this same last-known-good snapshot, not fail
        # silently -- exactly the "keeps failing to download" case section 5 guards against.
        result = fdel.route({"deliverable": "x", "acceptance": ["pytest passes"]})
        self.assertEqual(result["decision"], "delegate")
        self.assertEqual(result["candidate"]["id"], "agent-a")
        self.assertIn("catalog", result)
        self.assertEqual(result["catalog"]["commit"], "e" * 40)
        self.assertTrue(result["catalog"]["stale"])
        self.assertTrue(result["stale_prices"])
        self.assertTrue(any("hard-stale" in w for w in result["warnings"]))
        brief = fdel.brief_output(result)
        self.assertTrue(brief["stale_prices"])

    def test_p3_subscription_billing_shows_shadow_cost_not_cost_usd(self):
        harness = {**FAKE_HARNESS, "builtin": [
            {"id": "builtin-x", "subagent_type": "general-purpose", "model": "short-x", "catalog_id": "model-x"},
            {"id": "sub-worker", "subagent_type": "general-purpose", "model": "ph", "catalog_id": "model-sub",
             "billing": {"mode": "subscription", "quota_weight": 2}},
        ], "default_lead": "builtin-x"}
        self.write_catalog({"model-x": entry(50, 50), "model-sub": entry(1, 1)})
        fdel = self.load(harness=harness)
        result = fdel.route({"deliverable": "x", "acceptance": ["pytest passes"]})
        self.assertEqual(result["decision"], "delegate")
        self.assertEqual(result["candidate"]["id"], "sub-worker")
        self.assertNotIn("cost_usd", result["candidate"])
        self.assertIn("shadow_cost", result["candidate"])
        self.assertEqual(result["candidate"]["billing_mode"], "subscription")
        self.assertTrue(any("shadow_cost" in w for w in result["warnings"]))
        self.assertTrue(any("shadow_cost" in r for r in result["reasons"]))  # cheaper-than-lead says so
        brief = fdel.brief_output(result)
        self.assertIn("shadow_cost", brief)
        self.assertNotIn("cost_usd", brief)
        self.assertEqual(brief["billing_mode"], "subscription")

    def test_p3b_price_override_sets_flat_rate_ignoring_catalog_fields(self):
        harness = {**FAKE_HARNESS, "builtin": [
            {"id": "local-worker", "subagent_type": "general-purpose", "model": "ph", "catalog_id": "model-local",
             "billing": {"mode": "local", "price_override": 3.0}},
        ]}
        self.write_catalog({"model-local": entry(1, 1, cache_read=0.1, above_200k_in=9, above_200k_out=9)})
        fdel = self.load(harness=harness)
        lw = next(c for c in fdel.discover()[0] if c["id"] == "local-worker")
        self.assertEqual(lw["price"], {"input_per_m": 3.0, "output_per_m": 3.0})
        self.assertEqual(lw["billing"], {"mode": "local", "price_override": 3.0, "quota_weight": None})
        ranked = fdel.rank_candidates([lw], {"est_input_tokens": 1000, "est_output_tokens": 1000}, {})
        self.assertAlmostEqual(ranked[0]["cost_usd"], (1000 * 3.0 + 1000 * 3.0) / 1e6)
        self.assertEqual(ranked[0]["price_basis"], ["price_override"])

    def test_p3c_unrecognized_billing_mode_defaults_to_metered(self):
        fdel = self.load()
        self.assertEqual(fdel.normalize_billing({"mode": "bogus"}), fdel.normalize_billing(None))
        self.assertEqual(fdel.normalize_billing(None)["mode"], "metered")
        self.assertEqual(fdel.cost_output_key({"billing": None}), "cost_usd")
        self.assertEqual(fdel.cost_output_key({"billing": {"mode": "subscription"}}), "shadow_cost")

    def test_p4_embedding_mode_key_never_prices_a_candidate(self):
        catalog = {"prov1/qwen3-embed-8b": entry(1, 1, mode="embedding"), "model-x": entry(5, 6)}
        self.write_catalog(catalog)
        self.write_agent("open-embed", model="qwen3-embed-8b")
        fdel = self.load()
        # Base-name match would normally succeed ("qwen3-embed-8b" == base of the prefixed key),
        # but its catalog mode isn't "chat" -- code eligibility must reject it outright.
        self.assertEqual(fdel.match_catalog("qwen3-embed-8b", catalog), (None, "none"))
        c = next(c for c in fdel.discover()[0] if c["id"] == "open-embed")
        self.assertEqual(c["catalog_match"], "none")
        self.assertIsNone(c["price"])
        sl = fdel.match_shortlist("qwen3-embed-8b", catalog, fdel.DEFAULT_SPECIALIZATION_TAGS)
        self.assertNotIn("prov1/qwen3-embed-8b", sl)

    def test_p5_max_output_tokens_filter_skips_with_machine_readable_reason(self):
        fdel = self.load()
        cs = [cand("small-out", 1, 1, catalog_entry=entry(1, 1, max_output=500)),
              cand("big-out", 1, 1, catalog_entry=entry(1, 1, max_output=8000)),
              cand("unknown-out", 1, 1, catalog_entry=entry(1, 1))]
        kept, reasons = fdel.filter_candidates(cs, {"est_output_tokens": 4000}, fdel.ledger_stats())
        self.assertEqual(sorted(c["id"] for c in kept), ["big-out", "unknown-out"])
        self.assertTrue(any("skip small-out: max_output_tokens=500 < est_output_tokens=4000" in r for r in reasons))

    def test_p6_tiered_above_threshold_rate_used_for_300k_input(self):
        fdel = self.load()
        e = entry(1, 2, above_200k_in=5, above_200k_out=6)
        c = cand("big-ctx", 1, 2, ctx=1000000, catalog_entry=e)
        ranked = fdel.rank_candidates([c], {"est_input_tokens": 300000, "est_output_tokens": 1000}, {})
        self.assertIn("input_cost_per_token_above_200k_tokens", ranked[0]["price_basis"])
        self.assertIn("output_cost_per_token_above_200k_tokens", ranked[0]["price_basis"])
        expected = round(300000 * (5 / 1e6) + 1000 * (6 / 1e6), 8)
        self.assertAlmostEqual(ranked[0]["cost_usd"], expected)
        # Below the threshold, the plain rate applies instead.
        ranked_small = fdel.rank_candidates([c], {"est_input_tokens": 1000, "est_output_tokens": 1000}, {})
        self.assertIn("input_cost_per_token", ranked_small[0]["price_basis"])
        self.assertNotIn("input_cost_per_token_above_200k_tokens", ranked_small[0]["price_basis"])

    def test_p6_cached_input_share_uses_cache_read_rate(self):
        fdel = self.load()
        e = entry(1, 2, cache_read=0.5)
        c = cand("cached", 1, 2, catalog_entry=e)
        task = {"est_input_tokens": 1000, "est_output_tokens": 0, "est_cached_input_share": 0.5}
        ranked = fdel.rank_candidates([c], task, {})
        self.assertIn("cache_read_input_token_cost", ranked[0]["price_basis"])
        expected = round(500 * (0.5 / 1e6) + 500 * (1 / 1e6), 8)
        self.assertAlmostEqual(ranked[0]["cost_usd"], expected)
        # est_cached_input_share defaults to 0: no cache field used, cost matches plain pricing.
        ranked_default = fdel.rank_candidates([c], {"est_input_tokens": 1000, "est_output_tokens": 0}, {})
        self.assertNotIn("cache_read_input_token_cost", ranked_default[0]["price_basis"])
        self.assertAlmostEqual(ranked_default[0]["cost_usd"], round(1000 * (1 / 1e6), 8))

    def test_p6_metered_cost_usd_unchanged_when_entry_has_no_new_fields(self):
        """Regression: a plain entry (no cache/tiered fields) and cached_share=0 must cost
        exactly what price_tokens (the prior pricing function) computes."""
        fdel = self.load()
        e = entry(3, 7)
        c = cand("plain", 3, 7, catalog_entry=e)
        task = {"est_input_tokens": 12345, "est_output_tokens": 678}
        ranked = fdel.rank_candidates([c], task, {})
        old = fdel.price_tokens(c["price"], 12345, 678)
        self.assertAlmostEqual(ranked[0]["cost_usd"], old)

    def test_p7_deprecation_date_past_warns_future_does_not(self):
        self.write_agent("old-model", model="model-old")
        self.write_catalog({"model-old": entry(1, 1, deprecation_date="2020-01-01")})
        fdel = self.load()
        _cands, warnings = fdel.discover()
        self.assertTrue(any("old-model" in w and "2020-01-01" in w for w in warnings))

        self.write_agent("new-model", model="model-new")
        self.write_catalog({"model-old": entry(1, 1, deprecation_date="2020-01-01"),
                            "model-new": entry(1, 1, deprecation_date="2099-01-01")})
        fdel2 = self.load()
        _cands2, warnings2 = fdel2.discover()
        self.assertFalse(any("new-model" in w for w in warnings2))

    def test_p8_skill_md_documents_provenance_billing_and_pricing_fields(self):
        text = skill_docs_text()
        for token in ("stale_prices", "shadow_cost", "billing_mode", "price_override", "price_basis",
                     "cache_read_input_token_cost", "deprecation_date", "max_output_tokens",
                     "est_cached_input_share", "quota_weight", "hard-stale",
                     "embedding, image, audio, or rerank"):
            self.assertIn(token, text, token)

    def test_p9_harness_json_documents_billing_field(self):
        note = json.loads((SKILL_DIR / "harness.json").read_text())["note"]
        for token in ("billing", "subscription", "price_override", "quota_weight"):
            self.assertIn(token, note, token)

    # ================================================================
    # R: independent-review fixes on catalog pricing
    # ================================================================

    # ------------------------------------------------------------ R1: price_override vs Jev

    def test_r1_price_override_survives_jev_hit_and_miss(self):
        """Finding 1 (reproduced): the --jev branch used to discard billing.price_override --
        a Jev hit overwrote `price` with the catalog price, a miss wiped it to None. An explicit
        override must survive either branch."""
        harness = {**FAKE_HARNESS, "builtin": [
            {"id": "builtin-x", "subagent_type": "general-purpose", "model": "short-x", "catalog_id": "model-x"},
            {"id": "ov-worker", "subagent_type": "general-purpose", "model": "ph", "catalog_id": "model-ov",
             "billing": {"mode": "local", "price_override": 3.0}},
        ], "default_lead": "builtin-x"}
        self.write_catalog({"model-x": entry(50, 50), "model-ov": entry(9, 9)})
        fdel = self.load(harness=harness)
        os.environ["TYPESAFE_API_KEY"] = "test"
        # Case A: Jev confirms the exact-match seed -> the "hit" branch overwrites price from
        # the catalog entry (9, 9); the override must still win afterward.
        self.stub_match_post(fdel, {"model-ov"})
        cands, _ = fdel.discover(use_jev=True)
        ov = next(c for c in cands if c["id"] == "ov-worker")
        self.assertEqual(ov["price"], {"input_per_m": 3.0, "output_per_m": 3.0})
        self.assertEqual(ov["catalog_match"], "jev")
        self.assertEqual(ov["billing"]["price_override"], 3.0)

        # Case B: Jev rejects everything -> the "no match" branch wipes price to None; the
        # override must still win afterward. Clear the match cache first so this run actually
        # re-judges "model-ov" instead of reusing Case A's cached "confirmed" result.
        fdel2 = self.load(harness=harness)
        os.environ["TYPESAFE_API_KEY"] = "test"
        if fdel2.match_cache_path().exists():
            fdel2.match_cache_path().unlink()
        self.stub_match_post(fdel2, set())
        cands2, _ = fdel2.discover(use_jev=True)
        ov2 = next(c for c in cands2 if c["id"] == "ov-worker")
        self.assertEqual(ov2["catalog_match"], "none")
        self.assertEqual(ov2["price"], {"input_per_m": 3.0, "output_per_m": 3.0})

    # ------------------------------------------------------------ R2: id-keyed billing map

    def test_r2a_top_level_billing_map_reaches_agent_file_and_codex_candidates(self):
        """Finding 2: only harness.json builtins could carry billing (inline). An agent-file or
        codex --spawnable candidate has no harness.json entry of its own to carry an inline
        `billing` field, so the top-level id-keyed map is the only way to reach them."""
        self.write_agent("agent-a", model="model-a")
        harness = {**FAKE_HARNESS, "billing": {"agent-a": {"mode": "subscription", "price_override": 4.0},
                                               "spawn-model": {"mode": "local", "price_override": 2.5}}}
        self.write_catalog({"model-a": entry(1, 1), "spawn-model": entry(9, 9)})
        fdel = self.load(harness=harness)
        a = next(c for c in fdel.discover()[0] if c["id"] == "agent-a")
        self.assertEqual(a["billing"]["mode"], "subscription")
        self.assertEqual(a["price"], {"input_per_m": 4.0, "output_per_m": 4.0})

        codex_cands, _ = fdel.discover(harness_name="codex", spawnable=["spawn-model"])
        sm = next(c for c in codex_cands if c["id"] == "spawn-model")
        self.assertEqual(sm["billing"]["mode"], "local")
        self.assertEqual(sm["price"], {"input_per_m": 2.5, "output_per_m": 2.5})

    def test_r2b_id_keyed_map_wins_over_builtin_inline_billing(self):
        harness = {**FAKE_HARNESS, "builtin": [
            {"id": "dual", "subagent_type": "general-purpose", "model": "ph", "catalog_id": "model-dual",
             "billing": {"mode": "subscription", "price_override": 1.0}},
        ], "billing": {"dual": {"mode": "local", "price_override": 9.0}}}
        self.write_catalog({"model-dual": entry(2, 2)})
        fdel = self.load(harness=harness)
        d = next(c for c in fdel.discover()[0] if c["id"] == "dual")
        self.assertEqual(d["billing"]["mode"], "local")
        self.assertEqual(d["price"], {"input_per_m": 9.0, "output_per_m": 9.0})

    def test_r2c_billing_map_merges_per_id_override_wins_and_shipped_ids_survive(self):
        """The top-level `billing` map merges like builtin/disabled/probation (per id, override
        wins, unmatched shipped ids survive) -- not a wholesale scalar replace. Also verifies
        normalize_billing preserves an unknown extra key (e.g. a future `pool` feature)."""
        module = self.fresh_module()
        fake_shipped_dir = self.tmp / "fake_shipped"
        fake_shipped_dir.mkdir()
        (fake_shipped_dir / "harness.json").write_text(json.dumps({
            "schema": "fast-delegate/harness/v3", "spawn_model_placeholder": "ph", "builtin": [],
            "billing": {"shipped-only": {"mode": "subscription", "price_override": 1.0},
                       "both": {"mode": "subscription", "price_override": 1.0}}}))
        module.HERE = fake_shipped_dir
        state_dir = Path(os.environ["FAST_DELEGATE_STATE"])
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "harness.json").write_text(json.dumps(
            {"billing": {"both": {"mode": "local", "price_override": 9.0, "pool": "team-a"},
                        "override-only": {"mode": "local", "price_override": 2.0}}}))
        harness = module.load_harness()
        self.assertEqual(harness["billing"]["shipped-only"], {"mode": "subscription", "price_override": 1.0})
        self.assertEqual(harness["billing"]["both"], {"mode": "local", "price_override": 9.0, "pool": "team-a"})
        self.assertEqual(harness["billing"]["override-only"], {"mode": "local", "price_override": 2.0})
        normalized = module.normalize_billing(harness["billing"]["both"])
        self.assertEqual(normalized["pool"], "team-a")
        self.assertEqual(normalized["price_override"], 9.0)

    def test_r2d_malformed_billing_override_field_is_a_hard_error(self):
        state_dir = Path(os.environ["FAST_DELEGATE_STATE"])
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "harness.json").write_text(json.dumps({"billing": ["not", "an", "object"]}))
        module = self.fresh_module()
        with self.assertRaises(SystemExit) as ctx:
            module.load_harness()
        self.assertIn("billing", str(ctx.exception))

    # ------------------------------------------------------------ R3: $0 is never "free"

    def test_r3_zero_catalog_price_is_unpriced_not_free(self):
        """Finding 3 (reproduced): a literal $0 LiteLLM price used to be returned and kept by
        filter_cheaper_than_lead as a real (free) price."""
        self.write_agent("free-ish", model="model-free")
        self.write_catalog({"model-free": entry(0, 5), "model-x": entry(5, 6)})
        fdel = self.load()
        cands, warnings = fdel.discover()
        c = next(c for c in cands if c["id"] == "free-ish")
        self.assertIsNone(c["price"])
        self.assertTrue(any("free-ish" in w and "catalog price is 0" in w for w in warnings))
        # filter_cheaper_than_lead must therefore drop it as unpriced, not keep it as "free".
        kept, reasons = fdel.filter_cheaper_than_lead([c], {"input_per_m": 5, "output_per_m": 5}, (0.5, 0.5))
        self.assertEqual(kept, [])
        self.assertTrue(any("unknown price" in r for r in reasons))

    def test_r3b_price_override_wins_over_zero_price_safeguard(self):
        harness = {**FAKE_HARNESS, "builtin": [
            {"id": "zero-but-overridden", "subagent_type": "general-purpose", "model": "ph",
             "catalog_id": "model-zero", "billing": {"mode": "local", "price_override": 1.5}},
        ]}
        self.write_catalog({"model-zero": entry(0, 0)})
        fdel = self.load(harness=harness)
        c = next(c for c in fdel.discover()[0] if c["id"] == "zero-but-overridden")
        self.assertEqual(c["price"], {"input_per_m": 1.5, "output_per_m": 1.5})

    def test_r3c_zero_price_lead_via_discovered_candidate_is_a_hard_error(self):
        harness = {**FAKE_HARNESS, "builtin": [
            {"id": "zero-lead", "subagent_type": "general-purpose", "model": "ph", "catalog_id": "model-zero"},
            {"id": "cheap", "subagent_type": "general-purpose", "model": "ph2", "catalog_id": "model-cheap"},
        ], "default_lead": "zero-lead"}
        self.write_catalog({"model-zero": entry(0, 0), "model-cheap": entry(1, 1)})
        fdel = self.load(harness=harness)
        with self.assertRaises(SystemExit) as ctx:
            fdel.route({"deliverable": "x", "acceptance": ["pytest passes"]})
        self.assertIn("unknown price", str(ctx.exception))

    def test_r3d_zero_price_raw_catalog_id_lead_is_a_hard_error(self):
        harness = {**FAKE_HARNESS, "builtin": [
            {"id": "cheap", "subagent_type": "general-purpose", "model": "ph2", "catalog_id": "model-cheap"},
        ]}
        self.write_catalog({"model-cheap": entry(1, 1), "raw-zero-model": entry(0, 0)})
        fdel = self.load(harness=harness)
        with self.assertRaises(SystemExit) as ctx:
            fdel.route({"deliverable": "x", "acceptance": ["pytest passes"]}, lead="raw-zero-model")
        self.assertIn("unknown price", str(ctx.exception))

    # ------------------------------------------------------------ R4: atomic cache promotion

    def test_r4_meta_write_failure_keeps_last_known_good_and_leaves_no_temp_files(self):
        """Finding 4: cache and meta used to be written as two independent plain writes. Force
        the second (meta) temp write to fail and verify the last-known-good pair on disk is
        untouched and no stray .tmp files are left behind."""
        fdel = self._use_real_catalog_cache("cache-r4")
        good_catalog = {f"model-{i}": entry(1, 2) for i in range(60)}
        good_meta = {"repo": fdel.LITELLM_REPO, "commit": "f" * 40, "path": fdel.LITELLM_PATH,
                    "sha256": "cafef00d", "fetched_at": time.time()}
        fdel.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        fdel.LITELLM_CACHE.write_text(json.dumps(good_catalog))
        fdel.LITELLM_META.write_text(json.dumps(good_meta))

        def fake_urlopen(req, timeout=None):
            if "api.github.com" in req.full_url:
                return self._Resp(json.dumps({"sha": "1" * 40}).encode())
            new_catalog = {f"new-{i}": entry(9, 9) for i in range(60)}
            return self._Resp(json.dumps(new_catalog).encode())

        fdel.urllib.request.urlopen = fake_urlopen
        real_write_text = fdel.Path.write_text
        meta_tmp_suffix = ".meta.json.tmp" + str(os.getpid())

        def flaky_write_text(self, data, *a, **kw):
            if str(self).endswith(meta_tmp_suffix):
                raise OSError("simulated meta write failure")
            return real_write_text(self, data, *a, **kw)

        fdel.Path.write_text = flaky_write_text
        try:
            catalog, meta, warnings = fdel.fetch_litellm_catalog_meta(refresh=True)
        finally:
            fdel.Path.write_text = real_write_text

        self.assertEqual(catalog, good_catalog)
        self.assertEqual(meta["commit"], "f" * 40)
        self.assertEqual(json.loads(fdel.LITELLM_CACHE.read_text()), good_catalog)
        self.assertEqual(json.loads(fdel.LITELLM_META.read_text())["commit"], "f" * 40)
        self.assertEqual(list(fdel.CACHE_DIR.glob("*.tmp*")), [])
        self.assertTrue(any("not written" in w for w in warnings))

    # ------------------------------------------------------------ R5: bounded catalog reads

    def test_r5_read_bounded_aborts_on_size_cap_without_buffering_everything(self):
        """Finding 5: resp.read() used to be unbounded. read_bounded must abort as soon as the
        cap is exceeded, not after reading an entire (possibly huge) body."""
        fdel = self.load()
        calls = {"n": 0}

        class InfiniteResp:
            def read(self, n):
                calls["n"] += 1
                return b"x" * n  # never signals EOF -- an unbounded stream

        with self.assertRaises(ValueError):
            fdel.read_bounded(InfiniteResp(), max_bytes=1000, deadline_s=100)
        # Aborted after a small, bounded number of chunks, not by exhausting an infinite stream.
        self.assertLess(calls["n"], 10)

    def test_r5_read_bounded_aborts_on_wall_clock_deadline(self):
        """A slow-drip response (each individual .read() returns quickly) must not be able to
        hold the connection open past the wall-clock deadline."""
        fdel = self.load()

        class SlowDripResp:
            def read(self, n):
                return b"x"  # one byte per call -- would take "forever" to hit any size cap

        ticks = iter([0, 0.1, 0.2, 50, 100])
        with self.assertRaises(TimeoutError):
            fdel.read_bounded(SlowDripResp(), max_bytes=10**9, deadline_s=10,
                              clock=lambda: next(ticks, 999))

    def test_r5_fetch_snapshot_uses_read_bounded(self):
        """Wiring check: fetch_litellm_snapshot's own download goes through read_bounded, not a
        bare resp.read() -- an oversized response is rejected with a byte-bound message, not
        silently buffered in full first."""
        fdel = self.load()
        fdel.CATALOG_MAX_BYTES = 10

        def fake_urlopen(req, timeout=None):
            if "api.github.com" in req.full_url:
                return self._Resp(json.dumps({"sha": "2" * 40}).encode())
            return self._Resp(b"x" * 10000)

        fdel.urllib.request.urlopen = fake_urlopen
        catalog, meta, warnings = fdel.fetch_litellm_snapshot()
        self.assertIsNone(catalog)
        self.assertTrue(any("byte" in w for w in warnings))

    def test_default_route_invokes_task_jev_and_unavailable_falls_back(self):
        fdel = self.fit_setup()
        calls = []
        def unavailable(body):
            calls.append(body)
            return None, "fixture outage"
        fdel.jev = unavailable
        result = fdel.route({"deliverable": "rename helper", "acceptance": ["pytest passes"]})
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["candidate"]["id"], "agent-a")
        self.assertIn("jev unavailable (fixture outage)", " ".join(result["reasons"]))
        result = fdel.route({"deliverable": "rename helper", "acceptance": ["pytest passes"]}, use_jev=False)
        self.assertEqual(len(calls), 1)
        self.assertIn("disabled explicitly", " ".join(result["reasons"]))

    def test_zero_scores_are_advisory_with_override(self):
        fdel = self.fit_setup()
        self.stub_jev(fdel, {"model-a": (0, {0: 1}), "model-b": (0, {0: 1})})
        result = fdel.route({"deliverable": "x"})
        self.assertEqual(result["decision"], "delegate")
        self.assertEqual(len(result["recommendations"]), 2)
        self.assertTrue(result["review_required"])


    def test_malformed_fit_is_unavailable_but_normalized_rejection_is_valid(self):
        fdel = self.load()
        os.environ["TYPESAFE_API_KEY"] = "fixture-not-a-key"
        body = fdel.build_jev_request({"deliverable": "x"}, [("c0", cand("a", 1, 1))], {"a": "only"}, {})
        for probs in ({}, {"3": .2}, {"999": 1}):
            fdel.jev_post = lambda *a: ({"answers": {"fit_c0": {"score": 3, "probabilities": probs}}}, None)
            semantic, error = fdel.jev(body)
            self.assertIsNone(semantic)
            self.assertIn("malformed", error)
        fdel.jev_post = lambda *a: ({"model": "fixture", "answers": {"fit_c0": {"score": 0, "probabilities": {"0": 1}}}}, None)
        semantic, error = fdel.jev(body)
        self.assertIsNone(error)
        self.assertEqual(semantic["fit_c0_probs"], {0: 1})

    def test_enriched_evidence_keeps_unknowns_cost_and_availability_distinct(self):
        fdel = self.load()
        c = cand("worker-v1", 1, 2, billing={"mode": "subscription", "runtime": "proxy", "provider": "nvidia", "pool": "proxy:nvidia"})
        c.update(match_score=.8, catalog_id="other-host/worker-v1")
        body = fdel.build_jev_request({"deliverable": "x", "family": "race", "complexity": "hard", "est_input_tokens": 123}, [("c0", c)], {c["id"]: "only"}, {},
            quota_sources={"proxy:nvidia": {"freshness": "stale", "live": False}}, quota_by_pool={"proxy:nvidia": {"status": "ok"}})
        state = body["state"]["candidates"]["c0"]
        self.assertEqual(state["identity"]["worker_model"], "worker-v1")
        self.assertEqual(state["identity"]["canonical_model_id"], "unknown")
        self.assertEqual(state["pricing"]["cost_kind"], "shadow_cost")
        self.assertFalse(state["pricing"]["strength_signal"])
        self.assertIsNone(state["pricing"]["actual_billed_cost"])
        self.assertEqual(state["quota"]["source"]["freshness"], "stale")
        self.assertEqual(state["observed_family_profile"]["total"], 0)
        self.assertEqual(state["strength_benchmarks"]["status"], "unknown")
        self.assertEqual(body["state"]["task"]["token_estimates"]["est_input_tokens"], 123)

    def test_documented_strength_requires_version_effort_provenance_and_freshness(self):
        fdel = self.load()
        profile = {"source": "offline fixture only", "date": time.strftime("%Y-%m-%d", time.gmtime()),
                   "model_version": "worker-v1", "configuration": "high", "evidence": "fixture, not real model performance"}
        c = cand("worker-v1", 1, 1, billing={"strength_profile": profile, "reasoning_effort": "high"})
        self.assertTrue(fdel.documented_strength_profile(c)["configuration_match"])
        for field, bad in (("source", ""), ("date", "2000-01-01"), ("model_version", "worker-v0"), ("configuration", "low")):
            saved = profile[field]
            profile[field] = bad
            self.assertEqual(fdel.documented_strength_profile(c)["status"], "unknown")
            profile[field] = saved

    def test_task_recommendations_not_cached_between_tasks(self):
        fdel = self.fit_setup()
        calls = []
        def inference(body):
            calls.append(body["state"]["task"]["deliverable"])
            return {"fit_c0_probs": {4: 1}}, None
        fdel.jev = inference
        for deliverable in ("rename helper", "repair race"):
            fdel.route({"deliverable": deliverable, "acceptance": ["pytest passes"]})
        self.assertEqual(calls, ["rename helper", "repair race"])

    def test_cli_jev_alias_and_diagnostic_opt_out(self):
        self.write_agent("agent-a", model="model-a")
        self.write_catalog({"model-a": entry(1, 1), "claude-opus-5-5": entry(50, 100)})
        task = json.dumps({"deliverable": "rename", "acceptance": ["pytest passes"]})
        alias = json.loads(self.run_cli("route", "--jev", stdin=task).stdout)
        diagnostic = json.loads(self.run_cli("route", "--no-task-jev", stdin=task).stdout)
        self.assertIn("jev unavailable", " ".join(alias["reasons"]))
        self.assertIn("disabled explicitly", " ".join(diagnostic["reasons"]))

    def test_saved_experiment_replays_current_gates_and_decisions_without_network(self):
        report_path = SKILL_DIR / "examples/routing-experiment-20261002.json"
        if not report_path.exists():
            self.skipTest('routing-experiment-20261002.json not present (regenerate with experiment script)')
        fdel = self.load()
        report = json.loads(report_path.read_text())
        self.assertEqual(report["live_routing_calls"], 6)
        self.assertEqual({s["name"]: s["selection_changed"] for s in report["scenarios"]},
                         {"simple-unknown": False, "hard-stale": True, "hard-exhausted": False})
        for scenario in report["scenarios"]:
            picks = [scenario["runs"][variant]["composed_selection"]["pick"]["id"]
                     for variant in ("baseline", "enriched")]
            self.assertEqual(scenario["selection_changed"], picks[0] != picks[1])
            task = scenario["task"]
            kept, reasons = fdel.filter_candidates(scenario["candidate_profiles"], task, {}, scenario["quota_policy"])
            if scenario["name"] == "hard-exhausted":
                self.assertEqual(len(kept), 2)
                self.assertTrue(any("exhausted" in r for r in reasons))
            else:
                self.assertEqual(len(kept), 3)
            ranked = fdel.rank_candidates(kept, task, {}, {}, scenario["quota_policy"])
            bands = fdel.price_bands(ranked)
            for run in scenario["runs"].values():
                semantic, error = fdel.parse_jev_answers(run["response"], run["request"]["questions"], .4)
                self.assertIsNone(error)
                decision = fdel.decide(task, ranked, semantic, {}, bands)
                saved = run["composed_selection"]
                self.assertEqual(decision["pick"]["id"], ranked[0]["id"])
                self.assertTrue(decision["review_required"])
                self.assertEqual(len(decision["judged"]), len(ranked))
                self.assertIn("fit_mass", saved)  # Immutable report documents its old evaluator.


if __name__ == "__main__":
    unittest.main()
