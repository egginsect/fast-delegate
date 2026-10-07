#!/usr/bin/env python3
"""Lead-picks routing arm: ask the lead model to choose a worker for each task.

Measures what routing costs when the lead decides, for comparison with a
fdel route --jev call. The lead sees the same task JSON fdel gets and the
candidates strictly cheaper than itself, with catalog prices.

Usage: python3 bench/lead_route.py --tasks-dir <dir with A.json..E.json> \
           --prices <out>/prices.json --out <out>/lead_routes.jsonl [--runs 3] [--model opus]
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Candidates the no-Jev and Jev routers could pick (strictly cheaper than the opus lead).
CANDIDATES = ["luna", "haiku", "sonnet", "terra"]

PROMPT = """You are the lead agent deciding which worker to delegate a task to.
Pick the cheapest worker you expect to complete the task so that it passes the
acceptance check. You will verify its result afterwards; a rejected result costs
a retry by you.

Task (JSON):
{task}

Candidate workers (USD per million tokens):
{table}

Reply with only one JSON object on one line: {{"pick": "<worker id>", "why": "<one sentence>"}}"""


def clean_config_dir(root):
    cfg = Path(root) / "claude-config"
    cfg.mkdir(parents=True, exist_ok=True)
    creds = Path.home() / ".claude" / ".credentials.json"
    link = cfg / ".credentials.json"
    if creds.exists() and not link.exists():
        link.symlink_to(creds)
    (cfg / "settings.json").write_text("{}")
    return cfg


def candidate_table(prices):
    rows = []
    for c in CANDIDATES:
        p = prices["prices"][c]
        rows.append(f"- {c} ({p['model_id']}): input ${p['input'] * 1e6:.2f}, output ${p['output'] * 1e6:.2f}")
    return "\n".join(rows)


def parse_pick(text):
    for line in reversed(text.strip().splitlines()):
        line = line.strip().strip("`")
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks-dir", required=True)
    ap.add_argument("--prices", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--model", default="opus")
    args = ap.parse_args()

    prices = json.loads(Path(args.prices).read_text())
    table = candidate_table(prices)
    done = set()
    out = Path(args.out)
    if out.exists():
        done = {(r["task"], r["rep"]) for r in map(json.loads, out.read_text().splitlines())}

    with tempfile.TemporaryDirectory(prefix="lead-route-") as tmp:
        env = dict(os.environ, CLAUDE_CONFIG_DIR=str(clean_config_dir(tmp)))
        for task_file in sorted(Path(args.tasks_dir).glob("[A-Z].json")):
            task = task_file.stem
            for rep in range(1, args.runs + 1):
                if (task, rep) in done:
                    continue
                prompt = PROMPT.format(task=task_file.read_text().strip(), table=table)
                proc = subprocess.run(
                    ["claude", "-p", prompt, "--model", args.model, "--output-format", "json"],
                    cwd=tmp, env=env, capture_output=True, text=True, timeout=300)
                if proc.returncode != 0:
                    sys.exit(f"claude -p failed for {task} rep {rep}: {proc.stderr.strip()[:300]}")
                res = json.loads(proc.stdout)
                usage = res.get("usage", {})
                pick = parse_pick(res.get("result", ""))
                rec = {"task": task, "rep": rep, "pick": pick.get("pick"), "why": pick.get("why"),
                       "cost_usd": res.get("total_cost_usd"),
                       "tokens": {"input": usage.get("input_tokens", 0),
                                  "cached_input": usage.get("cache_read_input_tokens", 0),
                                  "cache_write": usage.get("cache_creation_input_tokens", 0),
                                  "output": usage.get("output_tokens", 0)},
                       "wall_ms": res.get("duration_ms")}
                with out.open("a") as f:
                    f.write(json.dumps(rec) + "\n")
                print(json.dumps(rec))


if __name__ == "__main__":
    main()
