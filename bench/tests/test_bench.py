#!/usr/bin/env python3
"""Tests for the benchmark harness (graders, fixtures, pricing, verify, key modes, summary)."""

import ast
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

BENCH = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BENCH))
import run as bench_run  # noqa: E402
import summarize as bench_sum  # noqa: E402

TASKS = ["A", "B", "C", "D", "E"]


def grade_dir(task, files):
    """Grade a dict {name: text} placed in a scratch workdir."""
    with tempfile.TemporaryDirectory() as tmp:
        for name, text in files.items():
            (Path(tmp) / name).write_text(text)
        res = subprocess.run([sys.executable, str(BENCH / "tasks" / task / "grade.py"), tmp],
                             capture_output=True, text=True, timeout=60)
        return json.loads(res.stdout)


def grade_key(task, kind):
    src = BENCH / "tasks" / task / "key" / kind
    with tempfile.TemporaryDirectory() as tmp:
        for item in src.iterdir():
            if item.is_file():
                shutil.copy(item, tmp)
        res = subprocess.run([sys.executable, str(BENCH / "tasks" / task / "grade.py"), tmp],
                             capture_output=True, text=True, timeout=60)
        return json.loads(res.stdout)


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestGradersRefBad(unittest.TestCase):
    def test_ref_accepted_bad_rejected(self):
        for t in TASKS:
            with self.subTest(task=t, kind="ref"):
                r = grade_key(t, "ref")
                self.assertTrue(r["accepted"], r)
            with self.subTest(task=t, kind="bad"):
                self.assertFalse(grade_key(t, "bad")["accepted"])


class TestGraderSoundness(unittest.TestCase):
    """Graders must be sound beyond ref/bad."""

    def _findings(self, task, items):
        return grade_dir(task, {"findings.json": json.dumps(items)})

    def _ref(self, task):
        return json.loads((BENCH / "tasks" / task / "key" / "ref" / "findings.json").read_text())

    def test_C_parse_duration_is_neutral(self):
        ref = self._ref("C")
        extra = ref + [{"function": "parse_duration", "line": 70, "description": "arguable"}]
        r = self._findings("C", extra)
        self.assertTrue(r["accepted"], r)
        self.assertEqual(r["false_positives"], 0)
        # ...and does not substitute for a required bug
        r = self._findings("C", ref[:2] + [extra[-1]])
        self.assertFalse(r["accepted"])

    def test_C_false_positives_counted(self):
        ref = self._ref("C")
        r = self._findings("C", ref + [{"function": "nonexistent", "line": 3, "description": "x"}])
        self.assertFalse(r["accepted"])
        self.assertEqual(r["false_positives"], 1)
        # right function, line outside the function
        wrong = [dict(ref[0], line=90)] + ref[1:]
        r = self._findings("C", wrong)
        self.assertFalse(r["accepted"])
        self.assertEqual(r["matched"], 2)

    def test_C_junk_does_not_crash(self):
        for junk in ("[1, 2]", '[{"function": "chunk", "line": "x"}]', '{"a": 1}', "not json"):
            r = grade_dir("C", {"findings.json": junk})
            self.assertFalse(r["accepted"], junk)

    def test_D_circuit_breaker_is_false_positive(self):
        ref = self._ref("D")
        r = self._findings("D", ref + [{"function": "CircuitBreaker", "line": 60, "description": "x"}])
        self.assertFalse(r["accepted"])
        self.assertEqual(r["false_positives"], 1)
        # flagged by line under a method name is also a FP
        r = self._findings("D", ref + [{"function": "call", "line": 75, "description": "x"}])
        self.assertFalse(r["accepted"])

    def test_D_needs_distinct_bugs(self):
        ref = self._ref("D")
        r = self._findings("D", [ref[0], ref[0], ref[0]])
        self.assertFalse(r["accepted"])
        self.assertEqual(r["matched"], 1)

    def test_A_rejects_wrong_values_and_lines(self):
        ref = json.loads((BENCH / "tasks/A/key/ref/answer.json").read_text())
        bad = json.loads(json.dumps(ref))
        bad["subcommands"][0]["line"] += 5
        self.assertFalse(grade_dir("A", {"answer.json": json.dumps(bad)})["accepted"])
        bad = json.loads(json.dumps(ref))
        bad["exit_codes"][0]["value"] = False  # bool must not equal int 0
        self.assertFalse(grade_dir("A", {"answer.json": json.dumps(bad)})["accepted"])
        bad = json.loads(json.dumps(ref))
        bad["exit_codes"][0]["line"] += 1  # +-1 tolerance
        self.assertTrue(grade_dir("A", {"answer.json": json.dumps(bad)})["accepted"])
        self.assertFalse(grade_dir("A", {"answer.json": '{"subcommands": [1]}'})["accepted"])

    def test_B_restores_pristine_tests(self):
        fixture = BENCH / "tasks/B/fixture"
        files = {
            "intervals.py": "def merge_intervals(i):\n    return []\n\ndef total_covered(i):\n    return 0\n",
            # worker tampered with the tests so they trivially pass
            "test_intervals.py": "import unittest\nclass T(unittest.TestCase):\n    def test_x(self):\n        pass\n",
        }
        self.assertFalse(grade_dir("B", files)["accepted"])
        files["test_intervals.py"] = (fixture / "test_intervals.py").read_text()
        self.assertFalse(grade_dir("B", files)["accepted"])

    def test_B_and_E_grade_missing_files(self):
        self.assertFalse(grade_dir("B", {})["accepted"])
        self.assertFalse(grade_dir("E", {})["accepted"])

    def test_E_hidden_test_catches_visible_only_fix(self):
        src = (BENCH / "tasks/E/key/ref/topo_sort.py").read_text()
        cheat = src.replace("        for dep in graph.get(node, []):\n            dfs(dep)",
                            "        for dep in graph.get(node, []):\n            if dep == node:\n                continue\n            dfs(dep)")
        self.assertNotEqual(cheat, src)
        tests = (BENCH / "tasks/E/fixture/test_topo_sort.py").read_text()
        r = grade_dir("E", {"topo_sort.py": cheat, "test_topo_sort.py": tests})
        self.assertFalse(r["accepted"], r)
        # the same cheat passes the visible tests alone
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "topo_sort.py").write_text(cheat)
            (Path(tmp) / "test_topo_sort.py").write_text(tests)
            res = subprocess.run([sys.executable, "-m", "unittest", "test_topo_sort"], cwd=tmp,
                                 capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stderr)

    def test_E_restores_pristine_tests(self):
        files = {"topo_sort.py": (BENCH / "tasks/E/fixture/topo_sort.py").read_text(),
                 "test_topo_sort.py": "import unittest\nclass T(unittest.TestCase):\n    def test_x(self):\n        pass\n"}
        self.assertFalse(grade_dir("E", files)["accepted"])


class TestFixturesPlantedBugs(unittest.TestCase):
    """Each fixture really contains the planted bugs, without giving them away."""

    def test_no_giveaway_comments_or_docstrings(self):
        for path in (BENCH / "tasks").glob("*/fixture/*.py"):
            if path.name.startswith("test_"):
                continue
            text = path.read_text()
            for word in ("BUG", "ARGUABLE", "STILL HERE"):
                self.assertNotIn(word, text, f"{path} leaks '{word}'")

    def test_prompts_do_not_give_bugs_away(self):
        e = (BENCH / "tasks/E/prompt.md").read_text().lower()
        for phrase in ("visited", "on the stack", "single `visited`", "dfs"):
            self.assertNotIn(phrase, e)
        d = (BENCH / "tasks/D/prompt.md").read_text().lower()
        for phrase in ("decoy", "circuitbreaker", "exponent", "jitter", "sleep"):
            self.assertNotIn(phrase, d)
        for t in TASKS:
            self.assertNotIn("fixture/", (BENCH / "tasks" / t / "prompt.md").read_text())

    def test_C_planted_bugs(self):
        import datetime
        u = load_module(BENCH / "tasks/C/fixture/utils.py", "c_utils")
        self.assertEqual(list(u.chunk([1, 2, 3, 4, 5], 2)), [[1, 2], [3, 4]])  # drops trailing partial
        d = datetime.date
        self.assertEqual(u.business_days_between(d(2024, 1, 1), d(2024, 1, 2)), 2)  # end-inclusive
        self.assertEqual(u.weekday_name(d(2024, 1, 6)), "business_day")  # Saturday counted
        self.assertEqual(u.parse_duration("1h30m"), 5400)

    def test_D_planted_bugs(self):
        r = load_module(BENCH / "tasks/D/fixture/retry.py", "d_retry")
        sleeps = []

        def fail():
            raise ValueError("x")

        with mock.patch.object(r.time, "sleep", sleeps.append):
            with self.assertRaises(ValueError):
                r.retry(fail, attempts=3, base_delay=1, max_delay=100, jitter=False)
        self.assertEqual(sleeps[0], 2)          # exponent off by one (first retry should be base_delay)
        self.assertEqual(len(sleeps), 3)        # sleeps after the final attempt
        sleeps.clear()
        with mock.patch.object(r.time, "sleep", sleeps.append), \
                mock.patch.object(r.random, "uniform", lambda a, b: b):
            with self.assertRaises(ValueError):
                r.retry(fail, attempts=2, base_delay=10, max_delay=5, jitter=True)
        self.assertGreater(sleeps[0], 5)        # jitter after the cap exceeds max_delay

    def test_D_circuit_breaker_is_correct(self):
        r = load_module(BENCH / "tasks/D/fixture/retry.py", "d_retry2")
        cb = r.CircuitBreaker(2, 100)

        def fail():
            raise ValueError

        for _ in range(2):
            with self.assertRaises(ValueError):
                cb.call(fail)
        with self.assertRaises(RuntimeError):
            cb.call(lambda: 1)

    def test_E_planted_bug(self):
        t = load_module(BENCH / "tasks/E/fixture/topo_sort.py", "e_topo")
        with self.assertRaises(t.CycleError):
            t.topological_sort({"A": ["B", "C"], "B": ["D"], "C": ["D"], "D": []})

    def test_B_fixture_is_stub(self):
        m = load_module(BENCH / "tasks/B/fixture/intervals.py", "b_int")
        self.assertIsNone(m.merge_intervals([(1, 2)]))

    def test_key_ranges_match_fixture_lines(self):
        def spans(path):
            return {n.name: [n.lineno, n.end_lineno]
                    for n in ast.parse(path.read_text()).body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        c = json.loads((BENCH / "tasks/C/key/ranges.json").read_text())
        s = spans(BENCH / "tasks/C/fixture/utils.py")
        for name, rng in {**c["buggy"], **c["neutral"]}.items():
            self.assertEqual(rng, s[name], name)
        d = json.loads((BENCH / "tasks/D/key/ranges.json").read_text())
        s = spans(BENCH / "tasks/D/fixture/retry.py")
        self.assertEqual(d["retry"], s["retry"])
        self.assertEqual(d["circuit_breaker"], s["CircuitBreaker"])
        lines = (BENCH / "tasks/D/fixture/retry.py").read_text().splitlines()
        self.assertIn("attempt + 1", lines[d["bugs"]["backoff_exponent"][0] - 1])
        self.assertIn("time.sleep", lines[d["bugs"]["sleep_after_final_attempt"][0] - 1])
        self.assertIn("random.uniform", lines[d["bugs"]["jitter_after_cap"][1] - 1])

    def test_A_key_matches_fixture(self):
        ref = json.loads((BENCH / "tasks/A/key/ref/answer.json").read_text())
        lines = (BENCH / "tasks/A/fixture/cli.py").read_text().splitlines()
        for sc in ref["subcommands"]:
            self.assertIn(f'add_parser("{sc["name"]}"', lines[sc["line"] - 1])
        for ec in ref["exit_codes"]:
            self.assertRegex(lines[ec["line"] - 1], rf"^{ec['name']} = {ec['value']}$")


class TestWorkersJson(unittest.TestCase):
    def test_workers(self):
        workers = json.loads((BENCH / "workers.json").read_text())
        self.assertEqual(set(workers), {"haiku", "sonnet", "opus", "luna", "terra", "sol"})
        for w in ("haiku", "sonnet", "opus"):
            self.assertEqual(workers[w]["cli"], "claude")
        for w in ("luna", "terra", "sol"):
            self.assertEqual(workers[w]["cli"], "codex")
            self.assertEqual(workers[w]["model"], f"gpt-5.6-{w}")
        for cfg in workers.values():
            for forbidden in ("astra", "fable", "gpt-5.5"):
                self.assertNotIn(forbidden, cfg["model"] + cfg["candidate"])

    def test_codex_command_shape(self):
        cmd = bench_run.codex_cmd("P", "gpt-5.6-luna", "/w")
        self.assertEqual(cmd[:4], ["codex", "exec", "--json", "--skip-git-repo-check"])
        self.assertIn("workspace-write", cmd)
        self.assertEqual(cmd[cmd.index("-C") + 1], "/w")
        self.assertEqual(cmd[cmd.index("-m") + 1], "gpt-5.6-luna")


class TestDryRun(unittest.TestCase):
    def test_dry_run_counts_cells(self):
        res = subprocess.run([sys.executable, str(BENCH / "run.py"), "--dry-run"],
                             capture_output=True, text=True)
        self.assertIn("90", res.stdout)
        self.assertIn("not executing", res.stdout)


# ----------------------------------------------------------------- pricing (defect 1)

DISCOVER = {"candidates": [
    {"id": "haiku", "model_id": "claude-haiku-4-5", "price": {"input_per_m": 1.0, "output_per_m": 5.0},
     "catalog_entry": {"input_cost_per_token": 1e-6, "output_cost_per_token": 5e-6,
                       "cache_read_input_token_cost": 1e-7, "cache_creation_input_token_cost": 1.25e-6}},
    {"id": "opus", "model_id": "claude-opus-5-5", "price": {"input_per_m": 4.0, "output_per_m": 20.0},
     "catalog_entry": {"input_cost_per_token": 4e-6, "output_cost_per_token": 2e-5,
                       "cache_read_input_token_cost": 2e-7, "cache_creation_input_token_cost": 5e-6}},
    {"id": "proxy-gpt-5-6-luna", "model_id": "gpt-5.6-luna", "price": {"input_per_m": 0.2, "output_per_m": 1.2},
     "catalog_entry": {"input_cost_per_token": 2e-7, "output_cost_per_token": 1.2e-6,
                       "cache_read_input_token_cost": 2e-8}},  # no cache-creation rate
    {"id": "nocatalog", "model_id": "x", "price": {"input_per_m": 3.0, "output_per_m": 6.0}, "catalog_entry": {}},
    {"id": "proxy-self", "model_id": "opus", "price": None, "catalog_entry": {}},
]}


class TestPricing(unittest.TestCase):
    def test_price_table(self):
        t = bench_run.price_table(DISCOVER)
        self.assertEqual(t["haiku"]["cache_read"], 1e-7)
        self.assertIsNone(t["proxy-gpt-5-6-luna"]["cache_creation"])
        self.assertAlmostEqual(t["nocatalog"]["input"], 3e-6)  # falls back to per-M price
        self.assertNotIn("proxy-self", t)

    def test_claude_cost_uses_cache_rates(self):
        out = {"usage": {"input_tokens": 10, "cache_read_input_tokens": 13689,
                         "cache_creation_input_tokens": 7159, "output_tokens": 310}}
        tok = bench_run.normalize_claude(out)
        cost = bench_run.compute_cost(tok, bench_run.price_table(DISCOVER)["haiku"])
        self.assertAlmostEqual(cost, 10 * 1e-6 + 13689 * 1e-7 + 7159 * 1.25e-6 + 310 * 5e-6)
        self.assertGreater(cost, 0)

    def test_codex_input_includes_cached_no_double_count(self):
        events = [{"type": "turn.completed", "usage": {
            "input_tokens": 27682, "cached_input_tokens": 18944, "cache_write_input_tokens": 0,
            "output_tokens": 303, "reasoning_output_tokens": 161}}]
        tok = bench_run.normalize_codex(events)
        self.assertEqual(tok["input"], 27682 - 18944)
        self.assertEqual(tok["cached_input"], 18944)
        cost = bench_run.compute_cost(tok, bench_run.price_table(DISCOVER)["proxy-gpt-5-6-luna"])
        self.assertAlmostEqual(cost, (27682 - 18944) * 2e-7 + 18944 * 2e-8 + 303 * 1.2e-6)  # reasoning is inside output_tokens

    def test_cache_fallbacks_to_input_rate(self):
        price = {"input": 2e-6, "output": 1e-5, "cache_read": None, "cache_creation": None}
        tok = {"input": 1, "cached_input": 2, "cache_write": 3, "output": 0, "reasoning": 0}
        self.assertAlmostEqual(bench_run.compute_cost(tok, price), 6 * 2e-6)

    def test_codex_sums_multiple_turns_and_ignores_other_events(self):
        u = {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 5, "reasoning_output_tokens": 1}
        tok = bench_run.normalize_codex([{"type": "turn.started"}, {"type": "turn.completed", "usage": u},
                                         {"type": "turn.completed", "usage": u}])
        self.assertEqual((tok["input"], tok["cached_input"], tok["output"]), (120, 80, 10))

    def test_fetch_discover_fails_loudly(self):
        bad = mock.Mock(return_value=mock.Mock(returncode=1, stdout="", stderr="boom"))
        with self.assertRaises(bench_run.BenchError):
            bench_run.fetch_discover(fdel=Path(__file__), run=bad)
        with self.assertRaises(bench_run.BenchError):
            bench_run.fetch_discover(fdel=Path("/nonexistent/fdel.py"))

    def test_real_catalog_prices_all_workers(self):
        if not bench_run.FDEL.exists():
            self.skipTest("fdel not installed")
        table = bench_run.price_table(bench_run.fetch_discover())
        for cfg in bench_run.load_workers().values():
            self.assertIn(cfg["candidate"], table)
            self.assertGreater(table[cfg["candidate"]]["output"], 0)


# ----------------------------------------------------------------- key modes (defect 3)

class TestKeyModes(unittest.TestCase):
    def _tree(self, root):
        (root / "key" / "ref").mkdir(parents=True)
        (root / "key" / "bad").mkdir()
        (root / "key" / "ref" / "a.json").write_text("{}")
        (root / "key" / "bad" / "a.json").write_text("{}")
        os.chmod(root / "key" / "ref" / "a.json", 0o640)
        os.chmod(root / "key" / "ref", 0o750)
        os.chmod(root / "key" / "bad", 0o700)
        os.chmod(root / "key", 0o755)
        return root / "key"

    def _modes(self, key):
        return {str(p.relative_to(key)): stat.S_IMODE(p.stat().st_mode) for p in [key, *key.rglob("*")]}

    def test_locked_then_restored_to_original_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = self._tree(Path(tmp))
            before = self._modes(key)
            sidecar = Path(tmp) / "sidecar.json"
            with bench_run.locked_keys([key], sidecar):
                self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0)
                self.assertFalse(os.access(key / "ref", os.R_OK))
                self.assertTrue(sidecar.exists())
                os.chmod(key, 0o700)  # peek: nested dirs and files must be 000 too
                self.assertEqual(stat.S_IMODE((key / "ref").stat().st_mode), 0)
                os.chmod(key / "ref", 0o700)
                self.assertEqual(stat.S_IMODE((key / "ref" / "a.json").stat().st_mode), 0)
                os.chmod(key / "ref", 0)
                os.chmod(key, 0)
            self.assertEqual(self._modes(key), before)
            self.assertFalse(sidecar.exists())

    def test_restored_after_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = self._tree(Path(tmp))
            before = self._modes(key)
            with self.assertRaises(RuntimeError):
                with bench_run.locked_keys([key], Path(tmp) / "s.json"):
                    raise RuntimeError("boom")
            self.assertEqual(self._modes(key), before)

    def test_stale_lock_is_recovered_not_recorded_as_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = self._tree(Path(tmp))
            before = self._modes(key)
            sidecar = Path(tmp) / "s.json"
            # simulate a kill -9 mid-batch: modes zeroed, sidecar left behind
            entries = bench_run._snapshot_modes(key)
            sidecar.write_text(json.dumps(entries))
            for p, _ in reversed(entries):
                os.chmod(p, 0)
            with bench_run.locked_keys([key], sidecar):
                pass
            self.assertEqual(self._modes(key), before)

    def test_repo_key_dirs_not_left_locked(self):
        for t in TASKS:
            key = BENCH / "tasks" / t / "key"
            for p in [key, *key.rglob("*")]:
                self.assertNotEqual(stat.S_IMODE(p.stat().st_mode), 0, str(p))


# ----------------------------------------------------------------- clean configs

class TestCleanConfigs(unittest.TestCase):
    def test_claude_and_codex_configs_are_minimal(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as tmp:
            (Path(home) / ".claude").mkdir()
            (Path(home) / ".claude" / ".credentials.json").write_text("{}")
            (Path(home) / ".claude" / "CLAUDE.md").write_text("SECRET GUIDELINES")
            (Path(home) / ".codex").mkdir()
            (Path(home) / ".codex" / "auth.json").write_text("{}")
            (Path(home) / ".codex" / "config.toml").write_text("model = 'x'\n")
            (Path(home) / ".codex" / "AGENTS.md").write_text("SECRET GUIDELINES")
            with mock.patch.object(Path, "home", return_value=Path(home)):
                c = bench_run.setup_claude_config(Path(tmp) / "c")
                x = bench_run.setup_codex_config(Path(tmp) / "x")
            self.assertEqual(sorted(p.name for p in c.iterdir()), [".credentials.json", "settings.json"])
            self.assertTrue((c / ".credentials.json").is_symlink())
            self.assertEqual(json.loads((c / "settings.json").read_text()), {})
            self.assertEqual(sorted(p.name for p in x.iterdir()), ["auth.json", "config.toml"])
            self.assertTrue((x / "auth.json").is_symlink())
            self.assertFalse((x / "config.toml").is_symlink())


# ----------------------------------------------------------------- end to end with fake CLIs

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
prompt = args[args.index("-p") + 1]
model = args[args.index("--model") + 1]
tools = args[args.index("--allowedTools") + 1]
log = os.environ["FAKE_LOG"]
key = os.environ["FAKE_KEYDIR"]
with open(log, "a") as f:
    f.write(json.dumps({"who": "claude", "model": model, "tools": tools, "cwd": os.getcwd(),
        "config": sorted(os.listdir(os.environ["CLAUDE_CONFIG_DIR"])),
        "key_readable": os.access(key, os.R_OK), "cwd_files": sorted(os.listdir("."))}) + "\n")
if "=== TASK GIVEN TO THE WORKER" in prompt:
    text = "Looked at it. All tests pass.\nVERDICT: ACCEPT"
else:
    open("intervals.py", "w").write(SOLUTION)
    text = "done"
print(json.dumps({"type": "result", "is_error": False, "result": text, "total_cost_usd": 0.0123,
    "usage": {"input_tokens": 10, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 5000, "output_tokens": 200}}))
'''
SOLUTION_SRC = '''
def merge_intervals(intervals):
    out = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out

def total_covered(intervals):
    return sum(e - s for s, e in merge_intervals(intervals))
'''
FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
cwd = args[args.index("-C") + 1]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps({"who": "codex", "config": sorted(os.listdir(os.environ["CODEX_HOME"])),
        "key_readable": os.access(os.environ["FAKE_KEYDIR"], os.R_OK), "cwd": cwd}) + "\n")
open(os.path.join(cwd, "intervals.py"), "w").write(SOLUTION)
print(json.dumps({"type": "thread.started", "thread_id": "t"}))
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10000, "cached_input_tokens": 8000,
    "cache_write_input_tokens": 0, "output_tokens": 300, "reasoning_output_tokens": 100}}))
'''
FAKE_FDEL = "import json, sys\nprint(json.dumps(%r))\n" % (DISCOVER,)


class TestEndToEnd(unittest.TestCase):
    """Drive run.py with fake claude/codex binaries on PATH."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        bindir = cls.tmp / "bin"
        bindir.mkdir()
        for name, body in (("claude", FAKE_CLAUDE), ("codex", FAKE_CODEX)):
            p = bindir / name
            p.write_text(body.replace("SOLUTION", repr(SOLUTION_SRC)))
            p.chmod(0o755)
        (cls.tmp / "fdel.py").write_text(FAKE_FDEL)
        cls.bindir = bindir

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _run(self, extra, out):
        log = self.tmp / f"{out.name}.calls"
        env = dict(os.environ, PATH=f"{self.bindir}:{os.environ['PATH']}", BENCH_FDEL=str(self.tmp / "fdel.py"),
                   FAKE_LOG=str(log), FAKE_KEYDIR=str(BENCH / "tasks/B/key"))
        res = subprocess.run([sys.executable, str(BENCH / "run.py"), "--workers", "luna,haiku",
                              "--tasks", "B", "--runs", "1", "--out", str(out), *extra],
                             capture_output=True, text=True, env=env, timeout=300)
        calls = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
        return res, calls

    def _records(self, out):
        return {r["worker"]: r for r in map(json.loads, (out / "runs.jsonl").read_text().splitlines())}

    def test_cost_verify_and_key_modes(self):
        keydir = BENCH / "tasks/B/key"
        os.chmod(keydir / "ref", 0o750)  # a non-default original mode must survive
        try:
            before = {str(p): stat.S_IMODE(p.stat().st_mode) for p in [keydir, *keydir.rglob("*")]}
            out = self.tmp / "out1"
            res, calls = self._run(["--parallel", "2"], out)
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            after = {str(p): stat.S_IMODE(p.stat().st_mode) for p in [keydir, *keydir.rglob("*")]}
            self.assertEqual(before, after)  # defect 3
        finally:
            os.chmod(keydir / "ref", 0o775)
        recs = self._records(out)
        # defect 1: nonzero cost, computed from catalog prices
        haiku, luna = recs["haiku"], recs["luna"]
        self.assertAlmostEqual(haiku["cost_usd"], 10 * 1e-6 + 5000 * 1e-7 + 1000 * 1.25e-6 + 200 * 5e-6)
        self.assertEqual(haiku["cli_cost_usd"], 0.0123)
        self.assertAlmostEqual(luna["cost_usd"], 2000 * 2e-7 + 8000 * 2e-8 + 300 * 1.2e-6)
        self.assertGreater(luna["cost_usd"], 0)
        # defect 2: verify ran for both, with verdict, tokens and cost
        for r in (haiku, luna):
            self.assertEqual(r["verify"]["verdict"], "ACCEPT")
            self.assertGreater(r["verify"]["cost_usd"], 0)
            self.assertEqual(r["verify"]["tokens"]["output"], 200)
            self.assertTrue(r["grade"]["accepted"], r["grade"])
            self.assertFalse(r["leak"])
        prices = json.loads((out / "prices.json").read_text())["prices"]
        self.assertEqual(prices["luna"]["cache_read"], 2e-8)
        self.assertIn("opus", prices)
        # verify call: opus, restricted tools, clean config, in the workdir
        verify_calls = [c for c in calls if c["who"] == "claude" and c["model"] == "opus"]
        self.assertEqual(len(verify_calls), 2)
        for c in verify_calls:
            self.assertEqual(c["tools"], "Read,Bash,Glob,Grep")
            self.assertTrue(c["cwd"].startswith(str(out.resolve())))
        # workers saw the key locked, a clean config and only fixture + prompt
        for c in calls:
            self.assertFalse(c["key_readable"])
        worker_calls = [c for c in calls if c["who"] == "claude" and c["model"] == "haiku"]
        self.assertLessEqual(set(worker_calls[0]["config"]), {".credentials.json", "settings.json"})
        self.assertIn("settings.json", worker_calls[0]["config"])
        self.assertEqual(worker_calls[0]["cwd_files"], ["intervals.py", "prompt.md", "test_intervals.py"])
        codex_calls = [c for c in calls if c["who"] == "codex"]
        self.assertLessEqual(set(codex_calls[0]["config"]), {"auth.json", "config.toml"})

    def test_no_verify_skips_verify_and_resume_skips_done(self):
        out = self.tmp / "out2"
        res, calls = self._run(["--no-verify"], out)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        for r in self._records(out).values():
            self.assertIsNone(r["verify"])
        self.assertFalse([c for c in calls if c["who"] == "claude" and c["model"] == "opus"])
        (self.tmp / "out2.calls").unlink()
        res2, calls2 = self._run(["--no-verify"], out)
        self.assertEqual(calls2, [])
        self.assertEqual(len((out / "runs.jsonl").read_text().splitlines()), 2)

    def test_opus_worker_is_not_verified(self):
        out = self.tmp / "out3"
        log = self.tmp / "out3.calls"
        env = dict(os.environ, PATH=f"{self.bindir}:{os.environ['PATH']}", BENCH_FDEL=str(self.tmp / "fdel.py"),
                   FAKE_LOG=str(log), FAKE_KEYDIR=str(BENCH / "tasks/B/key"))
        res = subprocess.run([sys.executable, str(BENCH / "run.py"), "--workers", "opus", "--tasks", "B",
                              "--runs", "1", "--out", str(out)], capture_output=True, text=True, env=env)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIsNone(self._records(out)["opus"]["verify"])
        self.assertEqual(len(log.read_text().splitlines()), 1)

    def test_missing_prices_fail_loudly(self):
        out = self.tmp / "out4"
        env = dict(os.environ, BENCH_FDEL=str(self.tmp / "nope.py"))
        res = subprocess.run([sys.executable, str(BENCH / "run.py"), "--workers", "haiku", "--tasks", "B",
                              "--runs", "1", "--out", str(out)], capture_output=True, text=True, env=env)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("cannot price", res.stderr)

    def test_verdict_parsing(self):
        self.assertEqual(bench_run.parse_verdict("x\nVERDICT: REJECT\n"), "REJECT")
        self.assertEqual(bench_run.parse_verdict("VERDICT: ACCEPT then later VERDICT: REJECT"), "REJECT")
        self.assertIsNone(bench_run.parse_verdict("looks fine"))

    def test_leak_flag(self):
        self.assertTrue(bench_run.check_for_key_leak("cat /x/tasks/B/key/ref"))
        self.assertTrue(bench_run.check_for_key_leak("ls bench/tasks"))
        self.assertFalse(bench_run.check_for_key_leak("all good"))


# ----------------------------------------------------------------- summarize

def mk(task, worker, run, cost, acc, verdict=None, vcost=0.0, score=None):
    return {"task": task, "worker": worker, "run": run, "wall_s": 10.0, "tokens": {"input": 100, "output": 50},
            "cost_usd": cost, "cli_cost_usd": None, "grade": {"accepted": acc, "score": 1.0 if acc else 0.0 if score is None else score},
            "verify": None if verdict is None else {"verdict": verdict, "cost_usd": vcost, "tokens": {}},
            "leak": False, "timeout": False, "error": None}


class TestSummarize(unittest.TestCase):
    def setUp(self):
        self.runs = [
            # opus cell on A: cost 1.0, accepted 1 of 2
            mk("A", "opus", 0, 1.0, True), mk("A", "opus", 1, 1.0, False),
            # haiku on A: run0 verify ACCEPT + grader accept; run1 verify ACCEPT but grader rejects (escape);
            # run2 verify REJECT (escalate)
            mk("A", "haiku", 0, 0.1, True, "ACCEPT", 0.2),
            mk("A", "haiku", 1, 0.1, False, "ACCEPT", 0.2),
            mk("A", "haiku", 2, 0.1, False, "REJECT", 0.2),
        ]

    def test_cells(self):
        s = bench_sum.build_summary(self.runs)
        c = s["cells"]["A_haiku"]
        self.assertEqual((c["n"], c["accepted"]), (3, 1))
        self.assertAlmostEqual(c["accept_rate"], 1 / 3)
        self.assertAlmostEqual(c["cost_mean"], 0.1)
        self.assertEqual((c["verify_accept"], c["verify_reject"]), (2, 1))
        self.assertAlmostEqual(s["cells"]["A_opus"]["score_sd"], 0.7071067811865476)

    def test_fixed_strategy_escalation_and_escape(self):
        s = bench_sum.build_summary(self.runs)
        t = s["strategies"]["fixed-haiku"]["tasks"]["A"]
        # costs per run: 0.3, 0.3, 0.3 + opus mean 1.0 => mean (0.3+0.3+1.3)/3
        self.assertAlmostEqual(t["cost"], (0.3 + 0.3 + 1.3) / 3)
        # accepted: 1, 0 (escape), 0.5 (escalation, opus accept-rate 0.5)
        self.assertAlmostEqual(t["accept_rate"], (1 + 0 + 0.5) / 3)
        self.assertAlmostEqual(t["escape_rate"], 1 / 3)
        self.assertAlmostEqual(t["escalation_rate"], 1 / 3)
        agg = s["strategies"]["fixed-haiku"]["aggregate"]
        self.assertAlmostEqual(agg["runnable"]["cost_per_accepted"], t["cost"] / t["accept_rate"])
        self.assertIsNone(agg["judgment"])
        lead = s["strategies"]["lead-alone"]["tasks"]["A"]
        self.assertAlmostEqual(lead["cost"], 1.0)
        self.assertAlmostEqual(lead["accept_rate"], 0.5)

    def test_router_and_split(self):
        runs = self.runs + [mk("C", "opus", 0, 2.0, True), mk("C", "haiku", 0, 0.1, True, "ACCEPT", 0.2)]
        routers, rc = {"router": {"A": "haiku", "C": "opus"}}, {"router": 0.05}
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "routes.json").write_text(json.dumps({"router": routers["router"], "routing_cost_usd": rc}))
            r, c = bench_sum.load_routes(tmp)
            s = bench_sum.build_summary(runs, r, c)
        st = s["strategies"]["router:router"]
        self.assertAlmostEqual(st["tasks"]["A"]["cost"], (0.3 + 0.3 + 1.3) / 3 + 0.05)
        self.assertAlmostEqual(st["tasks"]["C"]["cost"], 2.0 + 0.05)
        self.assertEqual(st["aggregate"]["judgment"]["tasks"], 1)
        self.assertEqual(st["aggregate"]["runnable"]["tasks"], 1)
        self.assertEqual(st["aggregate"]["all"]["tasks"], 2)

    def test_cli_writes_markdown_and_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "runs.jsonl").write_text("\n".join(json.dumps(r) for r in self.runs) + "\n")
            (Path(tmp) / "routes.json").write_text(json.dumps({"router": {"A": "haiku"}, "routing_cost_usd": {"router": 0.0}}))
            res = subprocess.run([sys.executable, str(BENCH / "summarize.py"), tmp], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("fixed-haiku", res.stdout)
            self.assertIn("router:router", res.stdout)
            self.assertIn("runnable (A,B,E)", res.stdout)
            data = json.loads((Path(tmp) / "summary.json").read_text())
            self.assertIn("A_haiku", data["cells"])
            self.assertIn("lead-alone", data["strategies"])


if __name__ == "__main__":
    unittest.main()
