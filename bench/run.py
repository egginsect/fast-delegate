#!/usr/bin/env python3
"""Benchmark runner: workers x tasks x runs, graded, lead-verified, priced."""

import argparse
import concurrent.futures as cf
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
FDEL = Path(os.environ.get("BENCH_FDEL") or Path.home() / ".claude" / "skills" / "fast-delegate" / "scripts" / "fdel.py")
TIMEOUT_S = 900
GRADE_TIMEOUT_S = 90
VERIFY_WORKER = "opus"
CLAUDE_WORKER_TOOLS = "Read,Edit,Write,Bash,Glob,Grep"
CLAUDE_VERIFY_TOOLS = "Read,Bash,Glob,Grep"
KEY_MODES_SIDECAR = BENCH_DIR / ".key-modes.json"

VERDICT_RE = re.compile(r"VERDICT:\s*(ACCEPT|REJECT)\b")


class BenchError(RuntimeError):
    """A condition that makes results untrustworthy; fail rather than record zeros."""


# --------------------------------------------------------------------------- config

def load_workers():
    with open(BENCH_DIR / "workers.json") as f:
        return json.load(f)


# --------------------------------------------------------------------------- prices

def fetch_discover(fdel=FDEL, run=subprocess.run):
    """Run `fdel.py discover --json` and return the parsed catalog."""
    if not Path(fdel).exists():
        raise BenchError(f"fast-delegate not found at {fdel}; cannot price runs")
    res = run([sys.executable, str(fdel), "discover", "--json"],
              capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        raise BenchError(f"fdel discover failed (rc={res.returncode}): {res.stderr[:300]}")
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError as e:
        raise BenchError(f"fdel discover returned invalid JSON: {e}")


def price_table(discover):
    """{candidate_id: per-token prices} from the discover output.

    Per-token rates come from catalog_entry; when absent, fall back to the
    per-million `price`. cache_read/cache_creation are None when the catalog
    has no such rate (compute_cost then falls back to the input rate).
    """
    table = {}
    for c in discover.get("candidates", []):
        entry = c.get("catalog_entry") or {}
        price = c.get("price") or {}
        inp = entry.get("input_cost_per_token")
        out = entry.get("output_cost_per_token")
        if inp is None and price.get("input_per_m") is not None:
            inp = price["input_per_m"] / 1e6
        if out is None and price.get("output_per_m") is not None:
            out = price["output_per_m"] / 1e6
        if inp is None or out is None:
            continue
        table[c["id"]] = {
            "model_id": c.get("model_id"),
            "input": inp,
            "output": out,
            "cache_read": entry.get("cache_read_input_token_cost"),
            "cache_creation": entry.get("cache_creation_input_token_cost"),
        }
    return table


def compute_cost(tokens, price):
    """USD for normalised tokens: input is UNCACHED input only."""
    inp = price["input"]
    cache_read = price.get("cache_read")
    cache_creation = price.get("cache_creation")
    cache_read = inp if cache_read is None else cache_read
    cache_creation = inp if cache_creation is None else cache_creation
    return (tokens.get("input", 0) * inp
            + tokens.get("cached_input", 0) * cache_read
            + tokens.get("cache_write", 0) * cache_creation
            + tokens.get("output", 0) * price["output"])


def normalize_claude(output):
    """Claude usage excludes cache fields from input_tokens."""
    u = output.get("usage") or {}
    return {
        "input": u.get("input_tokens", 0) or 0,
        "cached_input": u.get("cache_read_input_tokens", 0) or 0,
        "cache_write": u.get("cache_creation_input_tokens", 0) or 0,
        "output": u.get("output_tokens", 0) or 0,
        "reasoning": 0,  # thinking tokens are already inside output_tokens
    }


def parse_jsonl(text):
    events = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return events


def normalize_codex(events):
    """Sum turn.completed usage. Codex input_tokens INCLUDES cached (and cache
    write) tokens, so uncached input = input - cached - cache_write."""
    tot = {"input": 0, "cached_input": 0, "cache_write": 0, "output": 0, "reasoning": 0}
    for ev in events:
        if ev.get("type") != "turn.completed":
            continue
        u = ev.get("usage") or {}
        inp = u.get("input_tokens", 0) or 0
        cached = u.get("cached_input_tokens", 0) or 0
        write = u.get("cache_write_input_tokens", 0) or 0
        tot["input"] += max(inp - cached - write, 0)
        tot["cached_input"] += cached
        tot["cache_write"] += write
        tot["output"] += u.get("output_tokens", 0) or 0
        tot["reasoning"] += u.get("reasoning_output_tokens", 0) or 0
    return tot


# --------------------------------------------------------------------------- key dirs

def _snapshot_modes(root):
    """[(path, mode)] parents before children; symlinks skipped."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        out.append((dirpath, os.stat(dirpath).st_mode & 0o7777))
        for n in dirnames + filenames:
            p = os.path.join(dirpath, n)
            if not os.path.islink(p):
                out.append((p, os.lstat(p).st_mode & 0o7777))
    return out


def _restore_modes(entries):
    for path, mode in entries:  # parents first so children become reachable
        if os.path.lexists(path):
            os.chmod(path, mode)


def recover_stale_lock(sidecar=KEY_MODES_SIDECAR):
    """A previous run killed before its finally leaves modes recorded here."""
    if sidecar.exists():
        _restore_modes(json.loads(sidecar.read_text()))
        sidecar.unlink()


@contextlib.contextmanager
def locked_keys(key_dirs, sidecar=KEY_MODES_SIDECAR):
    """chmod 000 every path under key_dirs; restore ORIGINAL modes in finally."""
    recover_stale_lock(sidecar)
    entries = []
    for k in key_dirs:
        if Path(k).exists():
            entries.extend(_snapshot_modes(k))
    sidecar.write_text(json.dumps(entries))
    try:
        for path, _ in reversed(entries):  # children first: parents stay traversable
            os.chmod(path, 0o000)
        yield
    finally:
        _restore_modes(entries)
        with contextlib.suppress(FileNotFoundError):
            sidecar.unlink()


def copy_keys(task_ids, dest_root):
    """Private copies of key dirs for the graders (the in-tree key is locked)."""
    copies = {}
    for t in task_ids:
        src = BENCH_DIR / "tasks" / t / "key"
        if src.exists():
            dst = Path(dest_root) / t
            shutil.copytree(src, dst)
            for p in [dst, *dst.rglob("*")]:
                if not p.is_symlink():
                    p.chmod(p.stat().st_mode | 0o700)
            copies[t] = dst
    return copies


# --------------------------------------------------------------------------- clean configs

def setup_claude_config(root):
    """Only a symlink to the credentials and an empty settings.json."""
    d = Path(root)
    d.mkdir(parents=True, exist_ok=True)
    (d / "settings.json").write_text("{}")
    cred = Path.home() / ".claude" / ".credentials.json"
    if cred.exists():
        (d / ".credentials.json").symlink_to(cred)
    return d


def setup_codex_config(root):
    """Only auth.json (symlink) and config.toml (copy); no AGENTS.md."""
    d = Path(root)
    d.mkdir(parents=True, exist_ok=True)
    auth = Path.home() / ".codex" / "auth.json"
    if auth.exists():
        (d / "auth.json").symlink_to(auth)
    cfg = Path.home() / ".codex" / "config.toml"
    if cfg.exists():
        shutil.copy(cfg, d / "config.toml")
    return d


# --------------------------------------------------------------------------- subprocess

def run_cmd(cmd, cwd, env, timeout):
    """Run in its own process group so a timeout kills grandchildren too.
    Returns (returncode, stdout, stderr, timed_out)."""
    p = subprocess.Popen(cmd, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, start_new_session=True)
    try:
        out, err = p.communicate(timeout=timeout)
        return p.returncode, out, err, False
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(p.pid, signal.SIGKILL)
        out, err = p.communicate()
        return None, out or "", err or "", True


def claude_cmd(prompt, model, tools):
    cmd = ["claude", "-p", prompt, "--model", model, "--output-format", "json",
           "--allowedTools", tools]
    if tools == CLAUDE_VERIFY_TOOLS:
        cmd += ["--disallowedTools", "Edit,Write"]
    return cmd


def codex_cmd(prompt, model, workdir):
    return ["codex", "exec", "--json", "--skip-git-repo-check", "-m", model,
            "--sandbox", "workspace-write", "-C", str(workdir), prompt]


def run_worker(cfg, workdir, prompt, config_root, timeout=TIMEOUT_S):
    """Run one worker. Returns dict: tokens, cli_cost_usd, error, timeout, log, text."""
    env = os.environ.copy()
    if cfg["cli"] == "claude":
        env["CLAUDE_CONFIG_DIR"] = str(setup_claude_config(config_root))
        cmd = claude_cmd(prompt, cfg["model"], CLAUDE_WORKER_TOOLS)
    else:
        env["CODEX_HOME"] = str(setup_codex_config(config_root))
        cmd = codex_cmd(prompt, cfg["model"], workdir)
    rc, out, err, timed_out = run_cmd(cmd, workdir, env, timeout)
    log = f"$ {cfg['cli']} (model {cfg['model']}) rc={rc} timeout={timed_out}\n" \
          f"--- stdout ---\n{out}\n--- stderr ---\n{err}\n"
    res = {"tokens": {"input": 0, "cached_input": 0, "cache_write": 0, "output": 0, "reasoning": 0},
           "cli_cost_usd": None, "error": None, "timeout": timed_out, "log": log, "text": ""}
    if timed_out:
        res["error"] = f"timeout after {timeout}s"
        return res
    if cfg["cli"] == "claude":
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            res["error"] = f"invalid claude JSON (rc={rc}): {(out or err)[:300]}"
            return res
        res["tokens"] = normalize_claude(data)
        res["cli_cost_usd"] = data.get("total_cost_usd")
        res["text"] = data.get("result") or ""
        if data.get("is_error"):
            res["error"] = f"claude is_error: {res['text'][:300]}"
    else:
        events = parse_jsonl(out)
        res["tokens"] = normalize_codex(events)
        msgs = [e["item"].get("text", "") for e in events
                if e.get("type") == "item.completed" and (e.get("item") or {}).get("type") == "agent_message"]
        res["text"] = msgs[-1] if msgs else ""
        if not any(e.get("type") == "turn.completed" for e in events):
            res["error"] = f"codex produced no turn.completed (rc={rc}): {(err or out)[-300:]}"
    return res


# --------------------------------------------------------------------------- verify

def build_verify_prompt(task_prompt):
    return (
        "You are the lead reviewing a delegated worker's result. The worker was given the "
        "task below and worked in the current directory; its output is whatever is in this "
        "directory now. Check whether the worker's result actually satisfies the task. You "
        "may read files and run commands such as the tests, but do not modify anything. "
        "End your reply with exactly one final line, either `VERDICT: ACCEPT` or "
        "`VERDICT: REJECT`.\n\n=== TASK GIVEN TO THE WORKER ===\n" + task_prompt)


def parse_verdict(text):
    m = VERDICT_RE.findall(text or "")
    return m[-1] if m else None


def run_verify(workdir, task_prompt, config_root, price, timeout=TIMEOUT_S):
    """Opus lead-verify pass in the workdir with its own clean config."""
    cfg = {"cli": "claude", "model": "opus"}
    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR"] = str(setup_claude_config(config_root))
    cmd = claude_cmd(build_verify_prompt(task_prompt), cfg["model"], CLAUDE_VERIFY_TOOLS)
    start = time.time()
    rc, out, err, timed_out = run_cmd(cmd, workdir, env, timeout)
    rec = {"verdict": "REJECT", "tokens": {"input": 0, "cached_input": 0, "cache_write": 0,
                                           "output": 0, "reasoning": 0},
           "cost_usd": 0.0, "cli_cost_usd": None, "wall_s": round(time.time() - start, 2),
           "error": None}
    if timed_out:
        rec["error"] = f"verify timeout after {timeout}s"
        return rec
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        rec["error"] = f"invalid verify JSON (rc={rc}): {(out or err)[:300]}"
        return rec
    rec["tokens"] = normalize_claude(data)
    rec["cost_usd"] = compute_cost(rec["tokens"], price)
    rec["cli_cost_usd"] = data.get("total_cost_usd")
    verdict = parse_verdict(data.get("result"))
    if verdict is None:
        rec["error"] = "no VERDICT line in verify output; treated as REJECT"
    else:
        rec["verdict"] = verdict
    if data.get("is_error"):
        rec["error"] = f"claude is_error: {(data.get('result') or '')[:300]}"
    return rec


# --------------------------------------------------------------------------- grading / leaks

def grade_task(task_id, workdir, key_copy=None):
    script = BENCH_DIR / "tasks" / task_id / "grade.py"
    if not script.exists():
        return {"accepted": False, "score": 0.0, "detail": "Grader not found"}
    env = os.environ.copy()
    if key_copy is not None:
        env["BENCH_KEY_DIR"] = str(key_copy)
    try:
        res = subprocess.run([sys.executable, str(script), str(workdir)], capture_output=True,
                             text=True, timeout=GRADE_TIMEOUT_S, env=env)
        return json.loads(res.stdout)
    except Exception as e:
        return {"accepted": False, "score": 0.0, "detail": f"Grading error: {e}"}


def check_for_key_leak(log_text):
    return "/key" in log_text or "bench/tasks" in log_text


# --------------------------------------------------------------------------- one run

def prepare_workdir(out_dir, task_id, worker_id, run_num):
    workdir = Path(out_dir) / f"{task_id}_{worker_id}_{run_num}"
    if workdir.exists():
        shutil.rmtree(workdir)
    task_dir = BENCH_DIR / "tasks" / task_id
    shutil.copytree(task_dir / "fixture", workdir, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(task_dir / "prompt.md", workdir / "prompt.md")
    return workdir


def execute_run(job, ctx):
    task_id, worker_id, run_num = job
    cfg = ctx["workers"][worker_id]
    workdir = prepare_workdir(ctx["out_dir"], task_id, worker_id, run_num)
    prompt = (workdir / "prompt.md").read_text()
    cfg_root = Path(ctx["tmp"]) / f"{task_id}_{worker_id}_{run_num}"
    start = time.time()
    res = run_worker(cfg, workdir, prompt, cfg_root / "worker")
    wall = time.time() - start

    log_path = Path(ctx["out_dir"]) / "logs" / f"{task_id}_{worker_id}_{run_num}.log"
    log_path.parent.mkdir(exist_ok=True)
    log_path.write_text(res["log"])

    grade = grade_task(task_id, workdir, ctx["key_copies"].get(task_id))  # before verify touches anything

    verify = None
    if worker_id != VERIFY_WORKER and not ctx["no_verify"]:
        verify = run_verify(workdir, prompt, cfg_root / "verify", ctx["prices"][VERIFY_WORKER])

    price = ctx["prices"][worker_id]
    return {
        "task": task_id, "worker": worker_id, "run": run_num,
        "wall_s": round(wall, 2),
        "tokens": res["tokens"],
        "cost_usd": compute_cost(res["tokens"], price),
        "cli_cost_usd": res["cli_cost_usd"],
        "grade": grade,
        "verify": verify,
        "leak": check_for_key_leak(res["log"]),
        "timeout": res["timeout"],
        "error": res["error"],
    }


def load_completed(runs_file):
    done = set()
    if Path(runs_file).exists():
        for line in Path(runs_file).read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                done.add((r["task"], r["worker"], r["run"]))
    return done


# --------------------------------------------------------------------------- main

def build_parser():
    p = argparse.ArgumentParser(description="Run delegation benchmarks")
    p.add_argument("--workers", default="luna,haiku,sonnet,terra,sol,opus")
    p.add_argument("--tasks", default="A,B,C,D,E")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--parallel", type=int, default=6)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--no-verify", action="store_true", help="Skip the opus lead-verify pass")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))  # let finally blocks run
    workers = [w for w in args.workers.split(",") if w]
    tasks = [t for t in args.tasks.split(",") if t]
    workers_config = load_workers()

    for w in workers:
        if w not in workers_config:
            print(f"Error: Unknown worker '{w}'", file=sys.stderr)
            return 1
    for t in tasks:
        if not (BENCH_DIR / "tasks" / t).exists():
            print(f"Error: Unknown task '{t}'", file=sys.stderr)
            return 1

    total = len(workers) * len(tasks) * args.runs
    print(f"Planned: {len(tasks)} tasks x {len(workers)} workers x {args.runs} runs = {total} total")
    if args.dry_run:
        print("(dry-run mode: not executing)")
        return 0

    out_dir = args.out or BENCH_DIR / "results" / datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    needed = {workers_config[w]["candidate"] for w in workers}
    needed.add(workers_config[VERIFY_WORKER]["candidate"])
    table = price_table(fetch_discover())
    missing = sorted(needed - set(table))
    if missing:
        raise BenchError(f"no catalog price for candidates: {missing}")
    prices = {w: table[workers_config[w]["candidate"]] for w in workers_config
              if workers_config[w]["candidate"] in table}
    (out_dir / "prices.json").write_text(json.dumps(
        {"timestamp": datetime.now().isoformat(), "source": "fdel.py discover --json",
         "prices": prices}, indent=2))

    runs_file = out_dir / "runs.jsonl"
    done = load_completed(runs_file)
    jobs = [(t, w, r) for t in tasks for w in workers for r in range(args.runs)
            if (t, w, r) not in done]
    print(f"{len(done)} already completed; running {len(jobs)}")

    lock = threading.Lock()
    with tempfile.TemporaryDirectory(prefix="bench-") as tmp:
        key_copies = copy_keys(tasks, Path(tmp) / "keys")
        ctx = {"workers": workers_config, "out_dir": out_dir, "tmp": tmp, "prices": prices,
               "key_copies": key_copies, "no_verify": args.no_verify}

        def one(job):
            print(f"Running {'/'.join(map(str, job))}...", flush=True)
            try:
                rec = execute_run(job, ctx)
            except Exception as e:  # record the failure, keep the batch going
                rec = {"task": job[0], "worker": job[1], "run": job[2], "wall_s": 0.0,
                       "tokens": {}, "cost_usd": 0.0, "cli_cost_usd": None,
                       "grade": {"accepted": False, "score": 0.0, "detail": "harness error"},
                       "verify": None, "leak": False, "timeout": False,
                       "error": f"harness error: {type(e).__name__}: {e}"}
            with lock:
                with open(runs_file, "a") as f:
                    f.write(json.dumps(rec) + "\n")
            v = rec["verify"]
            print(f"  {rec['task']}/{rec['worker']}/{rec['run']}: accepted={rec['grade'].get('accepted')} "
                  f"cost=${rec['cost_usd']:.4f} verify={(v or {}).get('verdict')}", flush=True)

        with locked_keys([BENCH_DIR / "tasks" / t / "key" for t in tasks]):
            with cf.ThreadPoolExecutor(max_workers=max(1, args.parallel)) as ex:
                list(ex.map(one, jobs))

    print(f"Results saved to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
