"""Quota-aware worker selection: fixture tests mapped to the acceptance bullets, plus
pure-function coverage for the pieces route() composes.

Isolation: every test points FAST_DELEGATE_STATE, XDG_CACHE_HOME, CODEX_HOME and HOME at a fresh
temp dir (setUp), and never lets fdel.py touch the network (urlopen is stubbed to raise)."""

import importlib.util
import inspect
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "skills/fast-delegate"
SCRIPT = SKILL_DIR / "scripts/fdel.py"
STATUSLINE_SCRIPT = SKILL_DIR / "scripts/quota_statusline.py"


def skill_docs_text():
    """SKILL.md (operational core) plus REFERENCE.md (everything moved out of it)."""
    return (SKILL_DIR / "SKILL.md").read_text() + "\n" + (SKILL_DIR / "REFERENCE.md").read_text()


def entry(inp, out, ctx=100000, tools=True):
    """Minimal synthetic LiteLLM catalog entry (input/output $ per million tokens)."""
    return {"max_input_tokens": ctx, "supports_function_calling": tools,
            "input_cost_per_token": inp / 1e6, "output_cost_per_token": out / 1e6}


def builtin(cid, model_name, billing=None):
    b = {"id": cid, "subagent_type": "general-purpose", "model": "ph", "model_name": model_name}
    if billing is not None:
        b["billing"] = billing
    return b


def make_harness(builtins, default_lead="lead", quota_cfg=None):
    h = {"builtin": builtins, "spawn_model_placeholder": "ph", "placeholder_models": ["ph"],
         "catalog_provider_preference": [], "disabled": [], "probation": [],
         "default_lead": default_lead, "harnesses": {"claude": {}, "codex": {"reasoning_effort": {}}}}
    if quota_cfg is not None:
        h["quota"] = quota_cfg
    return h


def task(**overrides):
    t = {"deliverable": "do the thing", "acceptance": ["cd /repo && pytest -q"], "family": "fam",
         "est_input_tokens": 1000, "est_output_tokens": 200}
    t.update(overrides)
    return t


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        self._cwd = os.getcwd()
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "config" / "agents").mkdir(parents=True)
        os.chdir(self.tmp)
        home = self.tmp / "home"
        home.mkdir(parents=True, exist_ok=True)
        os.environ["HOME"] = str(home)
        os.environ["FAST_DELEGATE_STATE"] = str(self.tmp / "state")
        os.environ["XDG_CACHE_HOME"] = str(self.tmp / "cache")
        os.environ["CODEX_HOME"] = str(self.tmp / "codex_home")
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.tmp / "config")
        os.environ["FAST_DELEGATE_CATALOG"] = str(self.tmp / "catalog.json")
        os.environ.pop("TYPESAFE_API_KEY", None)
        os.environ.pop("FAST_DELEGATE_QUOTA", None)
        os.environ.pop("FAST_DELEGATE_LEAD", None)
        Path(os.environ["FAST_DELEGATE_STATE"]).mkdir(parents=True, exist_ok=True)
        self.write_catalog({"model-lead": entry(1000, 1000)})

    def tearDown(self):
        os.chdir(self._cwd)
        os.environ.clear()
        os.environ.update(self._env)

    def write_catalog(self, catalog):
        full = {"model-lead": entry(1000, 1000), **catalog}
        (self.tmp / "catalog.json").write_text(json.dumps(full))

    def load(self, harness=None):
        spec = importlib.util.spec_from_file_location("fdel_quota", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        def _no_network(req, timeout=None):
            raise module.urllib.error.URLError("network disabled in tests")
        module.urllib.request.urlopen = _no_network
        if harness is not None:
            module.load_harness = lambda: json.loads(json.dumps(harness))
        route_impl = module.route
        module.route_actual = route_impl

        def route_with_complete_fixture_metadata(*args, **kwargs):
            """Quota tests assume model identity is established so they isolate quota policy."""
            discover = module.discover

            def verified_discover(*dargs, **dkwargs):
                candidates, warnings = discover(*dargs, **dkwargs)
                for candidate in candidates:
                    if (candidate.get("catalog_match", "").startswith("unverified-")
                            and candidate.get("price") and candidate.get("context")
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

    def quota_path(self, pool):
        d = Path(os.environ["FAST_DELEGATE_STATE"]) / "quota"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{pool}.json"

    def write_quota_file(self, pool, five_hour=None, seven_day=None, basis="manual",
                         observed_at=None, rate_limit_reached_type=None):
        snap = {"pool": pool, "basis": basis,
               "observed_at": observed_at if observed_at is not None else time.time()}
        if five_hour is not None:
            snap["five_hour"] = five_hour
        if seven_day is not None:
            snap["seven_day"] = seven_day
        if rate_limit_reached_type is not None:
            snap["rate_limit_reached_type"] = rate_limit_reached_type
        self.quota_path(pool).write_text(json.dumps(snap))

    def write_rollout(self, subdir, filename, lines, mtime):
        d = Path(os.environ["CODEX_HOME"]) / "sessions" / subdir
        d.mkdir(parents=True, exist_ok=True)
        p = d / filename
        p.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
        os.utime(p, (mtime, mtime))
        return p

    @staticmethod
    def rollout_line(primary=None, secondary=None):
        rl = None
        if primary is not None or secondary is not None:
            rl = {}
            if primary is not None:
                rl["primary"] = primary
            if secondary is not None:
                rl["secondary"] = secondary
        return {"type": "event_msg", "payload": {"msg": {"type": "token_count", "rate_limits": rl}}}

    # ================================================================
    # Acceptance bullet 1: comfortable quota -> same pick as today, quota field shows both windows
    # ================================================================

    def test_bullet1_comfortable_quota_same_pick_and_quota_field(self):
        harness = make_harness([builtin("lead", "model-lead"), builtin("strong", "model-strong",
                                billing={"mode": "subscription", "pool": "claude"})])
        self.write_catalog({"model-strong": entry(50, 50)})
        t = task()

        fdel_baseline = self.load(harness=harness)
        baseline = fdel_baseline.route(t)
        self.assertEqual(baseline["decision"], "delegate")
        self.assertEqual(baseline["candidate"]["id"], "strong")

        now = time.time()
        five = {"used_percentage": 23, "resets_at": now + 0.77 * 300 * 60}
        seven = {"used_percentage": 13, "resets_at": now + 0.87 * 10080 * 60}
        self.write_quota_file("claude", five, seven, basis="claude-statusline", observed_at=now)
        fdel = self.load(harness=harness)
        result = fdel.route(t)
        self.assertEqual(result["decision"], "delegate")
        self.assertEqual(result["candidate"]["id"], baseline["candidate"]["id"], "quota unchanged the pick")
        self.assertEqual(result["quota"]["basis"], "claude-statusline")
        self.assertEqual(result["quota"]["five_hour"]["used_percentage"], 23)
        self.assertEqual(result["quota"]["seven_day"]["used_percentage"], 13)
        self.assertEqual(result["quota"]["status"], "ok")
        self.assertFalse(any("quota warning" in w or "unknown for" in w for w in result["warnings"]))

    # ================================================================
    # Acceptance bullet 2: ahead-of-pace pressure flips the pick to the cheapest qualifying one
    # ================================================================

    def test_bullet2_ahead_of_pace_pressure_prefers_cheapest_over_stronger(self):
        harness = make_harness([
            builtin("lead", "model-lead"),
            builtin("strong", "model-strong", billing={"mode": "subscription", "pool": "claude"}),
            builtin("cheap", "model-cheap", billing={"mode": "metered"}),
        ], quota_cfg={"reserve": 5})  # keep 92% used (8% remaining) out of "reserve", into "warn"
        self.write_catalog({"model-strong": entry(100, 100), "model-cheap": entry(15, 15)})
        t = task()

        now = time.time()
        # 23%/13%, on pace -> pressure ~eps -> strong's effective_cost stays far below cheap's.
        self.write_quota_file("claude", {"used_percentage": 23, "resets_at": now + 0.77 * 300 * 60},
                              {"used_percentage": 13, "resets_at": now + 0.87 * 10080 * 60},
                              basis="claude-statusline", observed_at=now)
        fdel1 = self.load(harness=harness)
        result1 = fdel1.route(t)
        self.assertEqual(result1["decision"], "delegate")
        self.assertEqual(result1["candidate"]["id"], "strong")

        # 92%/40%, well ahead of pace (only 10% of the 5h window elapsed) -> pressure rises
        # steeply enough that strong's effective_cost now exceeds cheap's real cost.
        self.write_quota_file("claude", {"used_percentage": 92, "resets_at": now + 0.9 * 300 * 60},
                              {"used_percentage": 40, "resets_at": now + 0.6 * 10080 * 60},
                              basis="claude-statusline", observed_at=now)
        fdel2 = self.load(harness=harness)
        result2 = fdel2.route(t)
        self.assertEqual(result2["decision"], "delegate")
        self.assertEqual(result2["candidate"]["id"], "cheap")
        self.assertTrue(any("quota warning" in w for w in result2["warnings"]), result2["warnings"])
        self.assertNotIn("required_fit", result1)
        self.assertNotIn("required_fit", result2)

    # ================================================================
    # Acceptance bullet 3: below reserve, no other pool -> direct with quota reason + wait_until
    # ================================================================

    def test_bullet3_below_reserve_no_other_pool_direct_with_wait_until(self):
        harness = make_harness([builtin("lead", "model-lead"), builtin("strong", "model-strong",
                                billing={"mode": "subscription", "pool": "claude"})])
        self.write_catalog({"model-strong": entry(50, 50)})
        t = task()
        now = time.time()
        resets_at = now + 1000
        self.write_quota_file("claude", {"used_percentage": 95, "resets_at": resets_at},
                              {"used_percentage": 20, "resets_at": now + 500000},
                              basis="claude-statusline", observed_at=now)
        fdel = self.load(harness=harness)
        result = fdel.route(t)
        self.assertEqual(result["decision"], "direct")
        self.assertTrue(any(r.startswith("quota: claude reserve") for r in result["reasons"]), result["reasons"])
        self.assertIn(fdel.human_local_time(resets_at), " ".join(result["reasons"]))
        self.assertAlmostEqual(result["wait_until"], resets_at, delta=1)
        self.assertEqual(result["quota"]["status"], "reserve")
        brief = fdel.brief_output(result)
        self.assertAlmostEqual(brief["wait_until"], resets_at, delta=1)
        self.assertEqual(brief["quota"]["status"], "reserve")

    # ================================================================
    # Acceptance bullet 4: Codex rollout reader -- newest-null-falls-back, all-null -> unknown
    # ================================================================

    def test_bullet4a_codex_rollout_newest_null_falls_back_to_older_valid(self):
        fdel = self.load()
        now = time.time()
        older = self.rollout_line(primary={"used_percent": 45.0, "resets_at": now + 3600, "plan_type": "pro"},
                                  secondary={"used_percent": 10.0, "resets_at": now + 500000})
        self.write_rollout("s1", "rollout-A.jsonl", [older], mtime=now - 100)
        newest_null = self.rollout_line()  # rate_limits: null
        self.write_rollout("s2", "rollout-B.jsonl", [newest_null], mtime=now)
        snap = fdel.read_codex_quota(now=now)
        self.assertIsNotNone(snap)
        self.assertEqual(snap["basis"], "codex-rollout")
        self.assertEqual(snap["pool"], "codex")
        self.assertEqual(snap["five_hour"]["used_percentage"], 45.0)
        self.assertEqual(snap["seven_day"]["used_percentage"], 10.0)
        self.assertEqual(snap["plan_type"], "pro")

    def test_bullet4b_codex_all_null_is_unknown_quota_and_todays_behaviour(self):
        harness = make_harness([builtin("lead", "model-lead"), builtin("cx", "model-cx",
                                billing={"mode": "subscription", "pool": "codex"})])
        self.write_catalog({"model-cx": entry(20, 20)})
        now = time.time()
        self.write_rollout("s1", "rollout-A.jsonl", [self.rollout_line()], mtime=now - 100)
        self.write_rollout("s2", "rollout-B.jsonl", [self.rollout_line()], mtime=now)
        fdel = self.load(harness=harness)
        self.assertIsNone(fdel.read_codex_quota(now=now))
        result = fdel.route(task())
        self.assertEqual(result["decision"], "delegate")
        self.assertEqual(result["candidate"]["id"], "cx")
        self.assertTrue(any("quota unknown for codex" in w for w in result["warnings"]), result["warnings"])
        self.assertNotIn("quota", result)

    def test_codex_rollout_files_honours_codex_home_and_missing_dir(self):
        fdel = self.load()
        self.assertEqual(fdel.codex_rollout_files(), [])
        now = time.time()
        p = self.write_rollout("s1", "rollout-A.jsonl", [self.rollout_line(primary={"used_percent": 1})],
                               mtime=now)
        self.assertEqual(fdel.codex_rollout_files(), [p])

    # ================================================================
    # Acceptance bullet 5: resets_at in the past -> reset; stale snapshot flagged
    # ================================================================

    def test_bullet5_reset_window_and_stale_snapshot(self):
        fdel = self.load()
        now = 1_000_000.0
        reset = fdel.normalize_window({"used_percentage": 90, "resets_at": now - 10}, now, 300)
        self.assertEqual(reset, {"used_percentage": 0.0, "resets_at": now - 10, "elapsed_share": None})

        thresholds = fdel.default_quota_thresholds({})
        fresh_snap = {"pool": "claude", "basis": "claude-statusline", "observed_at": now,
                     "five_hour": {"used_percentage": 10, "resets_at": now + 1000}, "seven_day": None}
        fresh = fdel.pool_status(fresh_snap, thresholds, now)
        self.assertFalse(fresh["stale"])

        stale_snap = {**fresh_snap, "observed_at": now - thresholds["stale_five_hour_s"] - 1}
        stale = fdel.pool_status(stale_snap, thresholds, now)
        self.assertTrue(stale["stale"])
        self.assertTrue(stale["five_hour"]["stale"])

    # ================================================================
    # Acceptance bullet 6: metered (no budget) and local candidates are never quota-gated
    # ================================================================

    def test_bullet6_metered_and_local_unaffected_by_quota(self):
        harness = make_harness([
            builtin("lead", "model-lead"),
            builtin("met", "model-met", billing={"mode": "metered", "pool": "claude"}),
            builtin("loc", "model-loc", billing={"mode": "local", "pool": "claude"}),
        ])
        self.write_catalog({"model-met": entry(20, 20), "model-loc": entry(30, 30)})
        # "claude" pool is exhausted -- would hard-block any *subscription* candidate.
        self.write_quota_file("claude", {"used_percentage": 100, "resets_at": time.time() + 1000},
                              {"used_percentage": 50, "resets_at": time.time() + 500000},
                              basis="claude-statusline")
        fdel = self.load(harness=harness)
        candidates, _ = fdel.discover()
        quota_by_pool = {"claude": fdel.pool_status(fdel.load_quota_file("claude"),
                                                     fdel.default_quota_thresholds({}), time.time())}
        kept, _ = fdel.filter_candidates(candidates, task(), fdel.ledger_stats(), quota_by_pool, False)
        self.assertEqual({c["id"] for c in kept} & {"met", "loc"}, {"met", "loc"})
        ranked = fdel.rank_candidates(kept, task(), fdel.ledger_stats(), None, quota_by_pool, False)
        met = next(c for c in ranked if c["id"] == "met")
        loc = next(c for c in ranked if c["id"] == "loc")
        self.assertEqual(met["effective_cost"], met["cost_usd"])
        self.assertEqual(loc["effective_cost"], loc["cost_usd"])
        result = fdel.route(task())
        self.assertEqual(result["decision"], "delegate")
        self.assertFalse(any("unknown for" in w for w in result["warnings"]))

    # ================================================================
    # Acceptance bullet 7: every fallback unaffordable under reserve -> escalation_blocked
    # ================================================================

    def test_bullet7_every_fallback_blocked_by_quota_sets_escalation_blocked(self):
        harness = make_harness([
            builtin("lead", "model-lead"),
            builtin("cheap", "model-cheap", billing={"mode": "metered"}),
            builtin("strong1", "model-strong1", billing={"mode": "subscription", "pool": "claude"}),
            builtin("strong2", "model-strong2", billing={"mode": "subscription", "pool": "claude"}),
        ])
        self.write_catalog({"model-cheap": entry(10, 10), "model-strong1": entry(50, 50),
                            "model-strong2": entry(60, 60)})
        self.write_quota_file("claude", {"used_percentage": 95, "resets_at": time.time() + 1000},
                              {"used_percentage": 20, "resets_at": time.time() + 500000},
                              basis="claude-statusline")
        fdel = self.load(harness=harness)
        result = fdel.route(task())
        self.assertEqual(result["decision"], "delegate")
        self.assertEqual(result["candidate"]["id"], "cheap")
        self.assertEqual(result["fallbacks"], [])
        self.assertEqual(result.get("escalation_blocked"), "quota")
        self.assertTrue(result.get("review_required"))
        brief = fdel.brief_output(result)
        self.assertEqual(brief["escalation_blocked"], "quota")

    # ================================================================
    # Acceptance bullet 8: --ignore-quota honoured and recorded; routes.jsonl gains fields
    # ================================================================

    def test_bullet8_ignore_quota_overrides_and_is_recorded(self):
        harness = make_harness([builtin("lead", "model-lead"), builtin("strong", "model-strong",
                                billing={"mode": "subscription", "pool": "claude"})])
        self.write_catalog({"model-strong": entry(50, 50)})
        self.write_quota_file("claude", {"used_percentage": 95, "resets_at": time.time() + 1000},
                              {"used_percentage": 20, "resets_at": time.time() + 500000},
                              basis="claude-statusline")
        fdel = self.load(harness=harness)
        blocked = fdel.route(task())
        self.assertEqual(blocked["decision"], "direct")

        fdel2 = self.load(harness=harness)
        result = fdel2.route(task(), ignore_quota=True)
        self.assertEqual(result["decision"], "delegate")
        self.assertTrue(result["ignore_quota"])
        brief = fdel2.brief_output(result)
        self.assertTrue(brief["ignore_quota"])

        routes_path = fdel2.ROUTES_LEDGER
        last = json.loads(routes_path.read_text().splitlines()[-1])
        self.assertEqual(last["route_id"], result["route_id"])
        self.assertIn("est_tokens", last)
        self.assertTrue("cost_usd" in last or "shadow_cost" in last)
        self.assertTrue(last.get("ignore_quota"))

    def test_bullet8_old_route_record_without_new_fields_still_parses(self):
        fdel = self.load()
        fdel.ROUTES_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        old = {"route_id": "old-route-1", "time": "2020-01-01T00:00:00Z", "family": "fam",
               "picked": "haiku", "required_fit": 2.0, "candidates": [], "difficulty": None,
               "independence": None}
        with fdel.ROUTES_LEDGER.open("a") as f:
            f.write(json.dumps(old) + "\n")
        rec = fdel.find_route_record("old-route-1")
        self.assertEqual(rec["picked"], "haiku")
        self.assertNotIn("est_tokens", rec)
        self.assertNotIn("quota", rec)

    def test_routes_jsonl_gains_est_tokens_and_cost_for_a_normal_delegate(self):
        harness = make_harness([builtin("lead", "model-lead"), builtin("strong", "model-strong",
                                billing={"mode": "subscription", "pool": "claude"})])
        self.write_catalog({"model-strong": entry(50, 50)})
        fdel = self.load(harness=harness)
        result = fdel.route(task())
        self.assertEqual(result["decision"], "delegate")
        last = json.loads(fdel.ROUTES_LEDGER.read_text().splitlines()[-1])
        self.assertEqual(last["route_id"], result["route_id"])
        self.assertIn("est_tokens", last)
        self.assertIn("shadow_cost", last)
        self.assertNotIn("ignore_quota", last)

    # ================================================================
    # Acceptance bullet 9: no network calls, stdlib-only
    # ================================================================

    def test_quota_functions_never_reference_network_modules(self):
        fdel = self.load()
        for fn in (fdel.load_quota_file, fdel.read_codex_quota, fdel.gather_quota_snapshots,
                  fdel.manual_quota_snapshots, fdel.pool_status, fdel.pressure, fdel.normalize_window,
                  fdel.codex_rollout_files, fdel.extract_rate_limits_from_line, fdel.parse_quota_specs):
            src = inspect.getsource(fn)
            self.assertNotIn("urllib", src, fn.__name__)
            self.assertNotIn("requests", src, fn.__name__)
            self.assertNotIn("http.client", src, fn.__name__)

    def test_quota_statusline_script_is_stdlib_only_no_network(self):
        text = STATUSLINE_SCRIPT.read_text()
        for token in ("urllib", "requests", "http.client", "socket"):
            self.assertNotIn(token, text)
        import ast
        tree = ast.parse(text)
        allowed = {"json", "os", "subprocess", "sys", "time", "pathlib"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name.split(".")[0], allowed, alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                self.assertIn(node.module.split(".")[0], allowed, node.module)

    # ================================================================
    # Pure-function coverage: pressure(), normalize_window(), pool assignment, manual --quota
    # ================================================================

    def test_pressure_pure_function(self):
        fdel = self.load()
        self.assertIsNone(fdel.pressure(None))
        self.assertAlmostEqual(fdel.pressure(0.23, 0.23), fdel.QUOTA_PRESSURE_EPS)
        self.assertAlmostEqual(fdel.pressure(0.0, 0.0), fdel.QUOTA_PRESSURE_EPS)
        self.assertAlmostEqual(fdel.pressure(0.5, 0.2),
                               round(fdel.QUOTA_PRESSURE_EPS + (1 - fdel.QUOTA_PRESSURE_EPS) * 0.3 ** 2, 6))
        self.assertEqual(fdel.pressure(1.0, 0.0), 1.0)
        self.assertGreater(fdel.pressure(0.9, 0.1), fdel.pressure(0.5, 0.1))

    def test_normalize_window_used_percent_alias_and_malformed_input(self):
        fdel = self.load()
        now = 1_000_000.0
        w = fdel.normalize_window({"used_percent": 40}, now, 300)
        self.assertEqual(w["used_percentage"], 40)
        self.assertIsNone(fdel.normalize_window({}, now, 300))
        self.assertIsNone(fdel.normalize_window(None, now, 300))
        self.assertIsNone(fdel.normalize_window("nonsense", now, 300))

    def test_normalize_window_elapsed_share_from_resets_at(self):
        fdel = self.load()
        now = 1_000_000.0
        w = fdel.normalize_window({"used_percentage": 23, "resets_at": now + 0.77 * 300 * 60}, now, 300)
        self.assertAlmostEqual(w["elapsed_share"], 0.23, places=3)

    def test_resolve_pool_defaults_and_apply_pool_omits_unresolved(self):
        fdel = self.load()
        self.assertEqual(fdel.resolve_pool({}, "spawnable", "claude"), "codex")
        self.assertEqual(fdel.resolve_pool({}, "spawnable", None), "codex")
        self.assertEqual(fdel.resolve_pool({}, "builtin", "claude"), "claude")
        self.assertEqual(fdel.resolve_pool({}, "builtin", "codex"), "codex")
        self.assertIsNone(fdel.resolve_pool({}, "builtin", None))
        self.assertIsNone(fdel.resolve_pool({}, "agent-file", "claude"))
        self.assertEqual(fdel.resolve_pool({"pool": "custom"}, "agent-file", "claude"), "custom")

        b = {"mode": "metered"}
        fdel.apply_pool(b, "agent-file", "claude")
        self.assertNotIn("pool", b)
        b2 = {"mode": "subscription"}
        fdel.apply_pool(b2, "builtin", "claude")
        self.assertEqual(b2["pool"], "claude")

    def test_discover_defaults_builtin_pool_to_active_harness_name(self):
        harness = make_harness([builtin("lead", "model-lead"),
                                builtin("b", "model-b", billing={"mode": "subscription"})])
        self.write_catalog({"model-b": entry(1, 1)})
        fdel = self.load(harness=harness)
        cands, _ = fdel.discover(harness_name="claude", spawnable=["lead", "b"])
        b = next(c for c in cands if c["id"] == "b")
        self.assertEqual(b["billing"]["pool"], "claude")

    def test_manual_quota_cli_wins_over_env(self):
        fdel = self.load()
        os.environ["FAST_DELEGATE_QUOTA"] = "cursor=5h:40,7d:10"
        snaps, _warnings = fdel.manual_quota_snapshots(["cursor=5h:99,7d:1"])
        self.assertEqual(snaps["cursor"]["five_hour"]["used_percentage"], 99.0)
        self.assertEqual(snaps["cursor"]["seven_day"]["used_percentage"], 1.0)
        self.assertEqual(snaps["cursor"]["basis"], "manual")

    def test_manual_quota_env_used_when_no_cli_override(self):
        fdel = self.load()
        os.environ["FAST_DELEGATE_QUOTA"] = "cursor=5h:40,7d:10"
        snaps, _warnings = fdel.manual_quota_snapshots(None)
        self.assertEqual(snaps["cursor"]["five_hour"]["used_percentage"], 40.0)

    def test_parse_quota_specs_malformed_warns_never_raises(self):
        fdel = self.load()
        parsed, warnings = fdel.parse_quota_specs(["nocolon", "pool=nothingusable"])
        self.assertEqual(parsed, {})
        self.assertEqual(len(warnings), 2)
        parsed2, warnings2 = fdel.parse_quota_specs(["cursor=5h:40"])
        self.assertEqual(parsed2["cursor"], {"five_hour": {"used_percentage": 40.0}})
        self.assertEqual(warnings2, [])

    def test_cli_quota_flag_reaches_route_for_an_undeclared_pool(self):
        harness = make_harness([builtin("lead", "model-lead"),
                                builtin("cursor-worker", "model-cursor",
                                       billing={"mode": "subscription", "pool": "cursor"})])
        self.write_catalog({"model-cursor": entry(20, 20)})
        fdel = self.load(harness=harness)
        result = fdel.route(task(), quota_specs=["cursor=5h:95,7d:10"])
        self.assertEqual(result["decision"], "direct")
        self.assertTrue(any("cursor" in r for r in result["reasons"]), result["reasons"])

    # ================================================================
    # Documentation coverage
    # ================================================================

    def test_skill_md_documents_quota_section(self):
        text = skill_docs_text()
        for token in ("quota_statusline.py", "--quota", "--ignore-quota", "FAST_DELEGATE_QUOTA",
                     "escalation_blocked", "wait_until", "pressure", "codex-rollout",
                     "claude-statusline", "reserve", "warn_at", "statusLine"):
            self.assertIn(token, text, token)

    def test_harness_json_documents_quota_object(self):
        harness = json.loads((SKILL_DIR / "harness.json").read_text())
        self.assertIn("quota", harness)
        for key in ("warn_at", "reserve", "stale_five_hour_s", "stale_seven_day_s",
                   "budget_usd_per_day", "max_concurrent"):
            self.assertIn(key, harness["quota"])
        self.assertIn("pool", harness["note"])

    # ================================================================
    # quota_statusline.py itself: writes the snapshot and chains the wrapped command
    # ================================================================

    def test_quota_statusline_writes_snapshot_and_chains(self):
        payload = {"rate_limits": {"five_hour": {"used_percentage": 42, "resets_at": 1234567890},
                                  "seven_day": {"used_percentage": 7, "resets_at": 1234599999}},
                  "some_other_field": "x"}
        chain_script = self.tmp / "chain.py"
        chain_script.write_text(
            "import sys, json\n"
            "data = json.loads(sys.stdin.read())\n"
            "print('STATUSLINE:' + data.get('some_other_field', ''))\n")
        result = subprocess.run(
            [sys.executable, str(STATUSLINE_SCRIPT), "--", sys.executable, str(chain_script)],
            input=json.dumps(payload), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("STATUSLINE:x", result.stdout)
        snap = json.loads((Path(os.environ["FAST_DELEGATE_STATE"]) / "quota" / "claude.json").read_text())
        self.assertEqual(snap["pool"], "claude")
        self.assertEqual(snap["basis"], "claude-statusline")
        self.assertEqual(snap["five_hour"]["used_percentage"], 42)
        self.assertIn("observed_at", snap)

    def test_review_itemB_no_chain_command_prints_a_minimal_line_not_nothing(self):
        """Review item B: a bare invocation (no chained command) must still print something --
        an empty statusLine reads as broken, not 'no command configured yet'."""
        result = subprocess.run([sys.executable, str(STATUSLINE_SCRIPT)],
                                input=json.dumps({"rate_limits": {}}), capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertNotEqual(result.stdout.strip(), "")

        result2 = subprocess.run([sys.executable, str(STATUSLINE_SCRIPT)],
                                 input=json.dumps({"rate_limits": {"five_hour": {"used_percentage": 42}}}),
                                 capture_output=True, text=True, timeout=10)
        self.assertIn("42", result2.stdout)

    def test_quota_statusline_no_rate_limits_does_not_write_file(self):
        qfile = Path(os.environ["FAST_DELEGATE_STATE"]) / "quota" / "claude.json"
        result = subprocess.run([sys.executable, str(STATUSLINE_SCRIPT)],
                                input=json.dumps({"model": "x"}), capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(qfile.exists())

    # ================================================================
    # Quota review fixes: A, B, 1-5
    # ================================================================

    # ---- Review item A: shipped builtins (no explicit billing) must be inferred subscription
    # ---- once their pool has a real quota snapshot; an explicit mode always wins.

    def test_reviewA_shipped_builtin_defaults_to_subscription_when_pool_has_quota_snapshot(self):
        """Reproduced: a shipped builtin ships with no `billing` config at all (defaults to
        metered), so `route --quota ...` used to be a complete no-op for it -- same pick,
        `quota: null` -- even with a real snapshot for its pool. No explicit mode + a pool with a
        snapshot must be treated as subscription instead, flagged `billing_source:
        "inferred-from-quota"`, with a warning."""
        harness = make_harness([
            builtin("lead", "model-lead"),
            builtin("haiku", "model-haiku"),
            builtin("sonnet", "model-sonnet"),
        ])
        self.write_catalog({"model-haiku": entry(5, 5), "model-sonnet": entry(50, 50)})
        now = time.time()
        self.write_quota_file("claude", {"used_percentage": 23, "resets_at": now + 0.77 * 300 * 60},
                              {"used_percentage": 13, "resets_at": now + 0.87 * 10080 * 60},
                              basis="claude-statusline", observed_at=now)
        fdel = self.load(harness=harness)
        result = fdel.route(task(), harness="claude", spawnable=["lead", "haiku", "sonnet"])
        self.assertEqual(result["decision"], "delegate")
        self.assertEqual(result["candidate"]["id"], "haiku")
        self.assertIsNotNone(result.get("quota"), "quota must be reported for the picked pool")
        self.assertEqual(result["quota"]["basis"], "claude-statusline")
        self.assertEqual(result["candidate"].get("billing_source"), "inferred-from-quota")
        self.assertTrue(any("inferred-from-quota" in w or "treated as subscription" in w
                            for w in result["warnings"]), result["warnings"])

    def test_reviewA_inferred_subscription_gets_quota_gated_at_reserve(self):
        harness = make_harness([
            builtin("lead", "model-lead"),
            builtin("haiku", "model-haiku"),
            builtin("sonnet", "model-sonnet"),
        ])
        self.write_catalog({"model-haiku": entry(5, 5), "model-sonnet": entry(50, 50)})
        now = time.time()
        self.write_quota_file("claude", {"used_percentage": 97, "resets_at": now + 1000},
                              {"used_percentage": 40, "resets_at": now + 500000},
                              basis="claude-statusline", observed_at=now)
        fdel = self.load(harness=harness)
        result = fdel.route(task(), harness="claude", spawnable=["lead", "haiku", "sonnet"])
        self.assertEqual(result["decision"], "direct")
        self.assertTrue(any(r.startswith("quota: claude reserve") for r in result["reasons"]), result["reasons"])
        self.assertEqual(result["quota"]["status"], "reserve")

    def test_reviewA_explicit_metered_billing_always_wins_over_inference(self):
        harness = make_harness([
            builtin("lead", "model-lead"),
            builtin("haiku", "model-haiku", billing={"mode": "metered"}),
        ])
        self.write_catalog({"model-haiku": entry(5, 5)})
        now = time.time()
        self.write_quota_file("claude", {"used_percentage": 95, "resets_at": now + 1000},
                              {"used_percentage": 20, "resets_at": now + 500000},
                              basis="claude-statusline", observed_at=now)
        fdel = self.load(harness=harness)
        result = fdel.route(task(), harness="claude", spawnable=["lead", "haiku"])
        # An explicit "metered" mode is never overridden, so a tight "claude" pool never gates it.
        self.assertEqual(result["decision"], "delegate")
        self.assertEqual(result["candidate"]["id"], "haiku")
        self.assertIn("cost_usd", result["candidate"])
        self.assertNotIn("billing_source", result["candidate"])

    # ---- Review item B: SKILL.md's statusLine snippet must use an absolute path, and document
    # ---- how to find the script for a plugin install. (quota_statusline.py's own no-chained-
    # ---- command minimal-line behaviour is already covered above.)

    def test_reviewB_skill_md_statusline_snippet_uses_absolute_path(self):
        text = skill_docs_text()
        self.assertNotIn('"command": "python3 skills/fast-delegate/scripts/quota_statusline.py',
                         text, "statusLine snippet must not use a repo-relative path")
        self.assertIn("~/.claude/skills/fast-delegate/scripts/quota_statusline.py", text)
        self.assertIn("find ~/.claude/plugins -name quota_statusline.py", text)

    # ---- Review item 1: read_codex_quota must tail rollout files in bounded, growing chunks from
    # ---- EOF (never a whole-file read), and cap how many rollout files it ever opens.

    def test_reviewfix1_tail_chunks_bounded_first_read_and_grows(self):
        fdel = self.load()
        p = self.tmp / "big.jsonl"
        line = json.dumps({"x": "y" * 100}) + "\n"
        p.write_text(line * 2000)
        self.assertGreater(p.stat().st_size, fdel.CODEX_TAIL_CHUNK_BYTES)
        chunks = list(fdel.tail_chunks(p))
        self.assertGreater(len(chunks), 1, "a file bigger than one chunk must yield more than one growing chunk")
        first_text, first_is_whole = chunks[0]
        self.assertEqual(len(first_text.encode("utf-8")), fdel.CODEX_TAIL_CHUNK_BYTES)
        self.assertFalse(first_is_whole)
        for text, _ in chunks:
            self.assertLessEqual(len(text.encode("utf-8")), fdel.CODEX_TAIL_MAX_BYTES)

    def test_reviewfix1_tail_chunks_stops_at_max_bytes_cap(self):
        fdel = self.load()
        p = self.tmp / "big2.jsonl"
        line = json.dumps({"x": "y" * 50}) + "\n"
        p.write_text(line * 5000)
        cap = 20000
        self.assertGreater(p.stat().st_size, cap)
        chunks = list(fdel.tail_chunks(p, chunk_size=4096, max_bytes=cap))
        largest = max(len(t.encode("utf-8")) for t, _ in chunks)
        self.assertEqual(largest, cap, "growth must stop exactly at the configured total cap")

    def test_reviewfix1_finds_data_beyond_the_first_tail_chunk(self):
        """The valid rate_limits line sits well past the first 64 KB tail chunk -- this only
        succeeds if read_codex_quota actually grows the chunk backward instead of giving up after
        the first bounded read."""
        fdel = self.load()
        now = time.time()
        valid = self.rollout_line(primary={"used_percent": 61.0, "resets_at": now + 3600},
                                  secondary={"used_percent": 9.0, "resets_at": now + 500000})
        lines = [self.rollout_line() for _ in range(20)] + [valid] + [self.rollout_line() for _ in range(1000)]
        path = self.write_rollout("s1", "rollout-A.jsonl", lines, mtime=now)
        self.assertGreater(path.stat().st_size, fdel.CODEX_TAIL_CHUNK_BYTES)
        snap = fdel.read_codex_quota(now=now)
        self.assertIsNotNone(snap)
        self.assertEqual(snap["five_hour"]["used_percentage"], 61.0)

    def test_reviewfix1_caps_number_of_rollout_files_examined(self):
        """Only the newest CODEX_TAIL_MAX_FILES rollout files are ever examined -- valid data
        sitting only in an older file beyond that cap must not be found (today's behaviour scanned
        every file, unbounded)."""
        fdel = self.load()
        now = time.time()
        valid = self.rollout_line(primary={"used_percent": 70.0, "resets_at": now + 3600},
                                  secondary={"used_percent": 15.0, "resets_at": now + 500000})
        self.write_rollout("old", "rollout-old.jsonl", [valid], mtime=now - 100000)
        for i in range(fdel.CODEX_TAIL_MAX_FILES):
            self.write_rollout(f"n{i}", f"rollout-n{i}.jsonl", [self.rollout_line()], mtime=now - i)
        snap = fdel.read_codex_quota(now=now)
        self.assertIsNone(snap, "valid data beyond the newest-N-files cap must not be found")

    # ---- Review item 2: escalation_blocked: "quota" only when a quota-blocked candidate is a
    # ---- plausible escalation target (costlier, by pre-quota price, than the pick).

    def test_reviewfix2_escalation_blocked_only_when_blocked_candidate_is_costlier(self):
        harness = make_harness([
            builtin("lead", "model-lead"),
            builtin("cheap", "model-cheap", billing={"mode": "metered"}),
            builtin("strong", "model-strong", billing={"mode": "subscription", "pool": "claude"}),
        ])
        self.write_catalog({"model-cheap": entry(10, 10), "model-strong": entry(80, 80)})
        self.write_quota_file("claude", {"used_percentage": 95, "resets_at": time.time() + 1000},
                              {"used_percentage": 20, "resets_at": time.time() + 500000}, basis="claude-statusline")
        fdel = self.load(harness=harness)
        result = fdel.route(task())
        self.assertEqual(result["decision"], "delegate")
        self.assertEqual(result["candidate"]["id"], "cheap")
        self.assertEqual(result["fallbacks"], [])
        self.assertEqual(result.get("escalation_blocked"), "quota")
        self.assertTrue(any("fit was not judged" in w for w in result["warnings"]), result["warnings"])

    def test_reviewfix2_escalation_not_blocked_when_blocked_candidate_is_cheaper(self):
        """Reproduced: a quota-blocked candidate used to force escalation_blocked: "quota"
        regardless of cost -- even one that was never a plausible escalation target (cheaper than
        the pick actually made) tripped it, misattributing a Jev-fit-gate miss to quota."""
        harness = make_harness([
            builtin("lead", "model-lead"),
            builtin("cheap", "model-cheap", billing={"mode": "metered"}),
            builtin("cheaper-blocked", "model-cheaper-blocked",
                    billing={"mode": "subscription", "pool": "claude"}),
        ])
        self.write_catalog({"model-cheap": entry(10, 10), "model-cheaper-blocked": entry(5, 5)})
        self.write_quota_file("claude", {"used_percentage": 95, "resets_at": time.time() + 1000},
                              {"used_percentage": 20, "resets_at": time.time() + 500000}, basis="claude-statusline")
        fdel = self.load(harness=harness)
        result = fdel.route(task())
        self.assertEqual(result["decision"], "delegate")
        self.assertEqual(result["candidate"]["id"], "cheap")
        self.assertEqual(result["fallbacks"], [])
        self.assertNotIn("escalation_blocked", result)

    # ---- Review item 3: a FAST_DELEGATE_QUOTA parse error must be labeled with its own source,
    # ---- not misreported as a --quota flag problem.

    def test_reviewfix3_env_quota_parse_error_labeled_with_its_own_source(self):
        fdel = self.load()
        os.environ["FAST_DELEGATE_QUOTA"] = "nocolon"
        snaps, warnings = fdel.manual_quota_snapshots(None)
        self.assertEqual(snaps, {})
        self.assertTrue(any(w.startswith("FAST_DELEGATE_QUOTA ") for w in warnings), warnings)
        self.assertFalse(any(w.startswith("--quota ") for w in warnings), warnings)

        snaps2, warnings2 = fdel.manual_quota_snapshots(["alsobad"])
        self.assertTrue(any(w.startswith("--quota ") for w in warnings2), warnings2)

    # ---- Review item 4: pin today's reserve-boundary behaviour (remaining exactly == reserve,
    # ---- and just below) -- deliberately deferred (SKILL.md), but must not drift by accident.

    def test_reviewfix4_reserve_boundary_pinned_at_and_just_below(self):
        fdel = self.load()
        thresholds = fdel.default_quota_thresholds({})  # reserve=10, warn_at=80 (defaults)
        now = time.time()
        at_boundary = {"pool": "claude", "basis": "manual", "observed_at": now,
                       "five_hour": {"used_percentage": 100 - thresholds["reserve"], "resets_at": now + 1000},
                       "seven_day": None}
        status_at = fdel.pool_status(at_boundary, thresholds, now)
        self.assertEqual(status_at["status"], "warn",
                         "remaining exactly == reserve is not (yet) gated -- boundary is strict '<'")

        just_below = {**at_boundary,
                     "five_hour": {"used_percentage": 100 - thresholds["reserve"] + 0.01, "resets_at": now + 1000}}
        status_below = fdel.pool_status(just_below, thresholds, now)
        self.assertEqual(status_below["status"], "reserve")

    # ---- Review item 5: jev_slots() must pick the JEV_MAX_CANDIDATES fit-gate slots by pre-quota
    # ---- cost, so quota pressure only re-weights among candidates that already reached the gate.

    def test_reviewfix5_jev_slots_selects_by_pre_quota_cost_not_effective_cost(self):
        fdel = self.load()
        N = fdel.JEV_MAX_CANDIDATES
        # w0 is pre-quota cheapest (cost_usd=1) but heavily quota-pressured (effective_cost huge)
        # -- naive post-quota truncation would drop it from the Jev fit gate entirely.
        others = [{"id": f"w{i}", "cost_usd": i + 2, "effective_cost": i + 2, "ledger_rate": None}
                  for i in range(1, N + 5)]
        pressured = {"id": "w0", "cost_usd": 1, "effective_cost": 9999, "ledger_rate": None}
        ranked = sorted(others + [pressured], key=lambda c: c["effective_cost"])  # rank_candidates' own order
        slots = fdel.jev_slots(ranked)
        self.assertEqual(len(slots), N)
        slot_ids = {c["id"] for _cid, c in slots}
        self.assertIn("w0", slot_ids, "pre-quota-cheapest candidate must still reach the Jev fit gate")
        self.assertNotIn("w0", {c["id"] for c in ranked[:N]}, "sanity: naive post-quota truncation would drop it")


if __name__ == "__main__":
    unittest.main()
