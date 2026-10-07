#!/usr/bin/env python3
"""Summarize benchmark results: per-cell stats and delegation strategies."""

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

LEAD = "opus"
RUNNABLE = ("A", "B", "E")
JUDGMENT = ("C", "D")


def load_runs(runs_file):
    runs = []
    if Path(runs_file).exists():
        for line in Path(runs_file).read_text().splitlines():
            if line.strip():
                runs.append(json.loads(line))
    return runs


def mean_sd(values):
    if not values:
        return None, None
    return statistics.mean(values), (statistics.stdev(values) if len(values) > 1 else 0.0)


def accepted(run):
    return bool((run.get("grade") or {}).get("accepted"))


def verify_of(run):
    v = run.get("verify")
    return v.get("verdict") if isinstance(v, dict) else None


def cell_stats(cell_runs):
    scores = [(r.get("grade") or {}).get("score", 0.0) for r in cell_runs]
    costs = [r.get("cost_usd") or 0.0 for r in cell_runs]
    tokens = [sum((r.get("tokens") or {}).values()) for r in cell_runs]
    walls = [r.get("wall_s") or 0.0 for r in cell_runs]
    vcosts = [(r.get("verify") or {}).get("cost_usd") or 0.0 for r in cell_runs]
    n = len(cell_runs)
    acc = sum(accepted(r) for r in cell_runs)
    ms, ss = mean_sd(scores)
    return {
        "n": n, "accepted": acc, "accept_rate": acc / n,
        "score_mean": ms, "score_sd": ss,
        "cost_mean": mean_sd(costs)[0], "verify_cost_mean": mean_sd(vcosts)[0],
        "tokens_mean": mean_sd(tokens)[0], "wall_mean": mean_sd(walls)[0],
        "verify_accept": sum(verify_of(r) == "ACCEPT" for r in cell_runs),
        "verify_reject": sum(verify_of(r) == "REJECT" for r in cell_runs),
        "leaks": sum(bool(r.get("leak")) for r in cell_runs),
        "timeouts": sum(bool(r.get("timeout")) for r in cell_runs),
        "errors": sum(bool(r.get("error")) for r in cell_runs),
    }


def lead_alone(cells, task):
    c = cells.get((task, LEAD))
    if not c:
        return None
    return {"cost": c["cost_mean"], "accept_rate": c["accept_rate"],
            "escape_rate": 0.0, "escalation_rate": 0.0}


def fixed_worker(cell_runs, opus_cell, notes, task, worker):
    """Expected cost and acceptance of: run worker, lead-verify, escalate on REJECT.

    verify REJECT: pay the mean lead-alone cost and accept with the lead's accept rate.
    verify ACCEPT: the grader decides; grader-rejected is an escaped defect.
    No verify record: nothing is filtered, the grader decides.
    """
    if opus_cell is None:
        notes.append(f"task {task}: no {LEAD} cell; escalation after REJECT costs 0 and yields nothing")
    esc_cost = opus_cell["cost_mean"] if opus_cell else 0.0
    esc_acc = opus_cell["accept_rate"] if opus_cell else 0.0
    costs, accs, escapes, escalations = [], [], 0, 0
    for r in cell_runs:
        cost = (r.get("cost_usd") or 0.0) + ((r.get("verify") or {}).get("cost_usd") or 0.0)
        if verify_of(r) == "REJECT":
            escalations += 1
            cost += esc_cost
            acc = esc_acc
        else:
            acc = 1.0 if accepted(r) else 0.0
            if verify_of(r) == "ACCEPT" and not accepted(r):
                escapes += 1
        costs.append(cost)
        accs.append(acc)
    n = len(cell_runs)
    return {"cost": statistics.mean(costs), "accept_rate": statistics.mean(accs),
            "escape_rate": escapes / n, "escalation_rate": escalations / n}


def aggregate(per_task):
    """Cost per accepted result overall and for runnable-check vs judgment tasks."""
    out = {}
    groups = {"all": None, "runnable": RUNNABLE, "judgment": JUDGMENT}
    for name, members in groups.items():
        rows = [v for t, v in per_task.items() if members is None or t in members]
        if not rows:
            out[name] = None
            continue
        cost = sum(v["cost"] for v in rows)
        acc = sum(v["accept_rate"] for v in rows)
        out[name] = {
            "tasks": len(rows), "total_cost": cost, "expected_accepted": acc,
            "cost_per_accepted": cost / acc if acc > 0 else None,
            "escape_rate": statistics.mean(v["escape_rate"] for v in rows),
        }
    return out


def load_routes(out_dir):
    p = Path(out_dir) / "routes.json"
    if not p.exists():
        return {}, {}
    data = json.loads(p.read_text())
    routers = data.get("router", {})
    if routers and all(isinstance(v, str) for v in routers.values()):
        routers = {"router": routers}  # {"router": {"A": "terra"}} is the single-router form
    # Sibling top-level pick maps such as {"router-jev": {"A": "luna"}} are further routers.
    for name, picks in data.items():
        if (name.startswith("router-") and isinstance(picks, dict) and picks
                and all(isinstance(v, str) for v in picks.values())):
            routers.setdefault(name, picks)
    return routers, data.get("routing_cost_usd", {})


def build_summary(runs, routers=None, routing_cost=None):
    routers = routers or {}
    routing_cost = routing_cost or {}
    by_cell = defaultdict(list)
    for r in runs:
        by_cell[(r["task"], r["worker"])].append(r)
    cells = {k: cell_stats(v) for k, v in by_cell.items()}
    tasks = sorted({t for t, _ in cells})
    workers = sorted({w for _, w in cells})
    notes = []

    def strategy_for(task, worker):
        if worker == LEAD:
            return lead_alone(cells, task)
        if (task, worker) not in by_cell:
            return None
        return fixed_worker(by_cell[(task, worker)], cells.get((task, LEAD)), notes, task, worker)

    strategies = {}
    strategies["lead-alone"] = {t: s for t in tasks if (s := lead_alone(cells, t))}
    for w in workers:
        if w != LEAD:
            strategies[f"fixed-{w}"] = {t: s for t in tasks if (s := strategy_for(t, w))}
    for rname, picks in routers.items():
        per = {}
        for t in tasks:
            pick = picks.get(t)
            s = strategy_for(t, pick) if pick else None
            if s is None:
                notes.append(f"router {rname}: no data for task {t} pick {pick}")
                continue
            s = dict(s)
            s["cost"] += float((routing_cost or {}).get(rname, 0.0))
            s["pick"] = pick
            per[t] = s
        strategies[f"router:{rname}"] = per

    result = {"cells": {f"{t}_{w}": c for (t, w), c in cells.items()}, "strategies": {}, "notes": sorted(set(notes))}
    for name, per in strategies.items():
        result["strategies"][name] = {"tasks": per, "aggregate": aggregate(per)}
    return result


def fmt(x, spec):
    return "n/a" if x is None else format(x, spec)


def to_markdown(summary):
    L = ["# Benchmark Results", "", "## Results by Cell", "",
         "| Task | Worker | N | Accept | Score | Cost $ | Tokens | Wall s | Verify A/R | Leaks | Timeouts | Errors |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for key in sorted(summary["cells"]):
        c = summary["cells"][key]
        t, w = key.split("_", 1)
        L.append(f"| {t} | {w} | {c['n']} | {c['accepted']}/{c['n']} ({c['accept_rate']:.0%}) | "
                 f"{c['score_mean']:.2f}±{c['score_sd']:.2f} | {c['cost_mean']:.4f} | {c['tokens_mean']:.0f} | "
                 f"{c['wall_mean']:.1f} | {c['verify_accept']}/{c['verify_reject']} | {c['leaks']} | "
                 f"{c['timeouts']} | {c['errors']} |")
    L += ["", "## Strategies (expected cost per task, verify cost included)", "",
          "| Strategy | Scope | Tasks | Total cost $ | Expected accepted | Cost per accepted $ | Escape rate |",
          "|---|---|---|---|---|---|---|"]
    for name, s in summary["strategies"].items():
        for scope in ("all", "runnable", "judgment"):
            a = s["aggregate"][scope]
            if a is None:
                continue
            label = {"all": "all", "runnable": "runnable (A,B,E)", "judgment": "judgment (C,D)"}[scope]
            L.append(f"| {name} | {label} | {a['tasks']} | {a['total_cost']:.4f} | {a['expected_accepted']:.2f} | "
                     f"{fmt(a['cost_per_accepted'], '.4f')} | {a['escape_rate']:.0%} |")
    if summary["notes"]:
        L += ["", "## Notes", ""] + [f"- {n}" for n in summary["notes"]]
    return "\n".join(L) + "\n"


def use_billed_cost(runs):
    """Prefer the CLI-billed cost (e.g. claude's total_cost_usd, which prices its 1h cache
    writes) over the catalog estimate; runs without one (codex) keep the catalog cost."""
    for r in runs:
        for rec in (r, r.get("verify") or {}):
            if rec.get("cli_cost_usd") is not None:
                rec["cost_usd"] = rec["cli_cost_usd"]
    return runs


def summarize(out_dir, basis="catalog"):
    out_dir = Path(out_dir)
    runs = load_runs(out_dir / "runs.jsonl")
    if basis == "billed":
        runs = use_billed_cost(runs)
    if not runs:
        print("No runs found")
        return None
    routers, routing_cost = load_routes(out_dir)
    summary = build_summary(runs, routers, routing_cost)
    md = to_markdown(summary)
    stem = "summary" if basis == "catalog" else f"summary-{basis}"
    summary["cost_basis"] = basis
    (out_dir / f"{stem}.json").write_text(json.dumps(summary, indent=2))
    (out_dir / f"{stem}.md").write_text(md)
    print(md)
    print(f"Summary saved to {out_dir / (stem + '.json')}")
    return summary


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--cost-basis=")]
    basis = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--cost-basis=")), "catalog")
    if len(args) != 1 or basis not in ("catalog", "billed"):
        print("Usage: summarize.py <output_dir> [--cost-basis=catalog|billed]")
        sys.exit(1)
    summarize(args[0], basis)
