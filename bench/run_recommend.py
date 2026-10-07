#!/usr/bin/env python3
"""Recommend-only benchmark: router recommends, lead decides, then execute.

Measures recommendation quality and decision economics, not delegation savings.
"""

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
LEAD_MODEL = "opus"
CLAUDE_WORKER_TOOLS = "Read,Edit,Write,Bash,Glob,Grep"

DECISION_RE = re.compile(r"DECISION:\s*(ACCEPT|REJECT)\b")

# Import from run.py for shared utilities
sys.path.insert(0, str(BENCH_DIR))
import run as bench_run


class BenchError(RuntimeError):
    """A condition that makes results untrustworthy."""


def load_task_json(task_id):
    """Load task.json for a task."""
    path = BENCH_DIR / "tasks" / task_id / "task.json"
    if not path.exists():
        raise BenchError(f"task.json not found for {task_id}; run route_tasks.py first")
    return json.loads(path.read_text())


def call_route(task_json_path, lead, harness, spawnable, use_jev, timeout=60):
    """Call fdel.py route and return parsed output + cost."""
    if not FDEL.exists():
        raise BenchError(f"fast-delegate not found at {FDEL}")
    
    cmd = [sys.executable, str(FDEL), "route",
           "--task", str(task_json_path),
           "--lead", lead,
           "--harness", harness,
           "--spawnable", spawnable,
           "--brief"]
    
    if not use_jev:
        cmd.append("--no-task-jev")
    
    start = time.time()
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    elapsed = time.time() - start
    
    if res.returncode != 0:
        raise BenchError(f"fdel route failed (rc={res.returncode}): {res.stderr[:300]}")
    
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError as e:
        raise BenchError(f"fdel route returned invalid JSON: {e}")
    
    return {"output": data, "elapsed_s": round(elapsed, 2),
            "cost_usd": data.get("routing_cost_usd", 0.0)}


def build_review_prompt(task_spec, recommendation):
    """Build the lead review prompt."""
    rec_text = json.dumps(recommendation, indent=2)
    return f"""You are deciding whether to delegate a task to a cheaper model or do it yourself.

Task:
{json.dumps(task_spec, indent=2)}

Recommendation from fast-delegate router:
{rec_text}

Consider:
1. Task complexity and acceptance criteria
2. Recommended model's capabilities and evidence (fit score, family history if present)
3. Risk: if the recommended model fails, you'll need to redo it yourself
4. Cost: recommended model is cheaper, but a failed attempt wastes that cost

Reply with exactly one line: `DECISION: ACCEPT` or `DECISION: REJECT`, followed by one sentence explaining why.
"""


def parse_decision(text):
    """Extract ACCEPT or REJECT from lead review output."""
    m = DECISION_RE.findall(text or "")
    return m[-1] if m else None


def lead_review_decision(task_spec, recommendation, config_root, timeout=300):
    """Lead reviews recommendation and decides ACCEPT or REJECT."""
    cfg = {"cli": "claude", "model": LEAD_MODEL}
    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR"] = str(bench_run.setup_claude_config(config_root))
    
    prompt = build_review_prompt(task_spec, recommendation)
    cmd = bench_run.claude_cmd(prompt, cfg["model"], CLAUDE_WORKER_TOOLS)
    
    start = time.time()
    rc, out, err, timed_out = bench_run.run_cmd(cmd, Path.cwd(), env, timeout)
    elapsed = time.time() - start
    
    rec = {"decision": "REJECT", "tokens": {}, "cost_usd": 0.0, "cli_cost_usd": None,
           "wall_s": round(elapsed, 2), "error": None, "text": ""}
    
    if timed_out:
        rec["error"] = f"review timeout after {timeout}s"
        return rec
    
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        rec["error"] = f"invalid review JSON (rc={rc}): {(out or err)[:300]}"
        return rec
    
    rec["tokens"] = bench_run.normalize_claude(data)
    rec["cost_usd"] = 0.0  # will be computed by caller with price
    rec["cli_cost_usd"] = data.get("total_cost_usd")
    rec["text"] = data.get("result") or ""
    
    decision = parse_decision(rec["text"])
    if decision is None:
        rec["error"] = "no DECISION line in review output; treated as REJECT"
    else:
        rec["decision"] = decision
    
    if data.get("is_error"):
        rec["error"] = f"claude is_error: {rec['text'][:300]}"
    
    return rec


def execute_task(task_id, worker_id, workdir, config_root, workers_config, timeout=TIMEOUT_S):
    """Execute one task with a specific worker."""
    cfg = workers_config[worker_id]
    prompt = (workdir / "prompt.md").read_text()
    return bench_run.run_worker(cfg, workdir, prompt, config_root, timeout)


def run_one_strategy(job, ctx):
    """Run one (strategy, task, run) cell.
    
    Returns dict: strategy, task, run, routing, review, execution, grade, total_cost_usd.
    """
    strategy, task_id, run_num = job
    task_spec = load_task_json(task_id)
    task_json_path = BENCH_DIR / "tasks" / task_id / "task.json"
    
    workdir = Path(ctx["tmp"]) / f"{strategy}_{task_id}_{run_num}_work"
    if workdir.exists():
        shutil.rmtree(workdir)
    task_dir = BENCH_DIR / "tasks" / task_id
    shutil.copytree(task_dir / "fixture", workdir, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(task_dir / "prompt.md", workdir / "prompt.md")
    
    cfg_root = Path(ctx["tmp"]) / f"{strategy}_{task_id}_{run_num}_cfg"
    
    rec = {"strategy": strategy, "task": task_id, "run": run_num, "routing": None,
           "review": None, "execution": None, "grade": None, "total_cost_usd": 0.0,
           "decided_model": None, "executed_model": None, "error": None}
    
    try:
        # Step 1: Routing (if not lead-alone)
        if strategy == "lead-alone":
            rec["decided_model"] = LEAD_MODEL
            rec["executed_model"] = LEAD_MODEL
        else:
            use_jev = (strategy == "route-jev")
            routing = call_route(task_json_path, LEAD_MODEL, "claude",
                                "haiku,sonnet,opus", use_jev)
            rec["routing"] = routing
            rec["total_cost_usd"] += routing["cost_usd"]
            
            # Extract recommendation
            recommendation = routing["output"]
            if recommendation.get("decision") == "direct":
                rec["decided_model"] = LEAD_MODEL
                rec["executed_model"] = LEAD_MODEL
            else:
                recommended_id = recommendation.get("id")
                
                # Step 2: Lead review
                review = lead_review_decision(task_spec, recommendation,
                                             cfg_root / "review")
                rec["review"] = review
                price = ctx["prices"].get(LEAD_MODEL)
                if price and review.get("cli_cost_usd") is None:
                    review["cost_usd"] = bench_run.compute_cost(review["tokens"], price)
                if review.get("cli_cost_usd") is not None:
                    review["cost_usd"] = review["cli_cost_usd"]
                rec["total_cost_usd"] += review.get("cost_usd", 0.0)
                
                if review.get("decision") == "ACCEPT":
                    rec["decided_model"] = recommended_id
                    rec["executed_model"] = recommended_id
                else:
                    rec["decided_model"] = LEAD_MODEL
                    rec["executed_model"] = LEAD_MODEL
        
        # Step 3: Execute
        exec_result = execute_task(task_id, rec["executed_model"], workdir,
                                   cfg_root / "exec", ctx["workers"], TIMEOUT_S)
        rec["execution"] = exec_result
        price = ctx["prices"].get(rec["executed_model"])
        if price and exec_result.get("cli_cost_usd") is None:
            exec_result["cost_usd"] = bench_run.compute_cost(exec_result["tokens"], price)
        if exec_result.get("cli_cost_usd") is not None:
            exec_result["cost_usd"] = exec_result["cli_cost_usd"]
        rec["total_cost_usd"] += exec_result.get("cost_usd", 0.0)
        
        # Step 4: Grade
        grade = bench_run.grade_task(task_id, workdir, ctx["key_copies"].get(task_id))
        rec["grade"] = grade
        
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {e}"
        rec["grade"] = {"accepted": False, "score": 0.0, "detail": "harness error"}
    
    return rec


def load_completed(runs_file):
    """Load already-completed runs."""
    done = set()
    if Path(runs_file).exists():
        for line in Path(runs_file).read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                done.add((r["strategy"], r["task"], r["run"]))
    return done


def build_parser():
    p = argparse.ArgumentParser(description="Run recommend-only benchmark")
    p.add_argument("--strategies", default="lead-alone,route-heuristic,route-jev",
                   help="Comma-separated: lead-alone, route-heuristic, route-jev")
    p.add_argument("--tasks", default="A,B,C,D,E")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--parallel", type=int, default=3)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--out", type=Path, default=None)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    tasks = [t.strip() for t in args.tasks.split(",") if t.strip()]
    
    for t in tasks:
        if not (BENCH_DIR / "tasks" / t).exists():
            print(f"Error: Unknown task '{t}'", file=sys.stderr)
            return 1
        if not (BENCH_DIR / "tasks" / t / "task.json").exists():
            print(f"Error: task.json not found for '{t}'; run route_tasks.py first",
                  file=sys.stderr)
            return 1
    
    total = len(strategies) * len(tasks) * args.runs
    print(f"Planned: {len(tasks)} tasks x {len(strategies)} strategies x {args.runs} runs = {total} total")
    
    if args.dry_run:
        print("(dry-run mode: not executing)")
        return 0
    
    out_dir = args.out or BENCH_DIR / "results" / f"recommend_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    
    # Fetch prices
    workers_config = bench_run.load_workers()
    needed = {workers_config[w]["candidate"] for w in [LEAD_MODEL, "haiku", "sonnet"]}
    table = bench_run.price_table(bench_run.fetch_discover())
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
    jobs = [(s, t, r) for s in strategies for t in tasks for r in range(args.runs)
            if (s, t, r) not in done]
    print(f"{len(done)} already completed; running {len(jobs)}")
    
    lock = threading.Lock()
    with tempfile.TemporaryDirectory(prefix="bench-recommend-") as tmp:
        key_copies = bench_run.copy_keys(tasks, Path(tmp) / "keys")
        ctx = {"workers": workers_config, "tmp": tmp, "prices": prices,
               "key_copies": key_copies}
        
        def one(job):
            print(f"Running {'/'.join(map(str, job))}...", flush=True)
            rec = run_one_strategy(job, ctx)
            with lock:
                with open(runs_file, "a") as f:
                    f.write(json.dumps(rec) + "\n")
            print(f"  {rec['strategy']}/{rec['task']}/{rec['run']}: "
                  f"decided={rec['decided_model']} executed={rec['executed_model']} "
                  f"accepted={rec['grade'].get('accepted')} cost=${rec['total_cost_usd']:.4f}",
                  flush=True)
        
        with bench_run.locked_keys([BENCH_DIR / "tasks" / t / "key" for t in tasks]):
            with cf.ThreadPoolExecutor(max_workers=max(1, args.parallel)) as ex:
                list(ex.map(one, jobs))
    
    print(f"Results saved to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
