#!/usr/bin/env python3
"""Summarize recommend-only benchmark results.

Aggregates: cost per accepted, recommendation acceptance rate, success when followed.
"""

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

RUNNABLE = ("A", "B", "E")
JUDGMENT = ("C", "D")


def load_runs(runs_file):
    runs = []
    if Path(runs_file).exists():
        for line in Path(runs_file).read_text().splitlines():
            if line.strip():
                runs.append(json.loads(line))
    return runs


def accepted(run):
    return bool((run.get("grade") or {}).get("accepted"))


def strategy_stats(runs, strategy):
    """Compute stats for one strategy across all runs."""
    strat_runs = [r for r in runs if r["strategy"] == strategy]
    if not strat_runs:
        return None
    
    by_task = defaultdict(list)
    for r in strat_runs:
        by_task[r["task"]].append(r)
    
    tasks = {}
    for task, task_runs in by_task.items():
        n = len(task_runs)
        acc_count = sum(accepted(r) for r in task_runs)
        total_cost = sum(r.get("total_cost_usd", 0.0) for r in task_runs)
        
        # Recommendation acceptance (only for route-* strategies)
        rec_accept = 0
        rec_total = 0
        rec_success = 0
        rec_success_total = 0
        
        for r in task_runs:
            if r.get("review"):
                rec_total += 1
                if r["review"].get("decision") == "ACCEPT":
                    rec_accept += 1
                    rec_success_total += 1
                    if accepted(r):
                        rec_success += 1
        
        routing_cost = sum((r.get("routing") or {}).get("cost_usd", 0.0) for r in task_runs)
        review_cost = sum((r.get("review") or {}).get("cost_usd", 0.0) for r in task_runs)
        exec_cost = sum((r.get("execution") or {}).get("cost_usd", 0.0) for r in task_runs)
        
        tasks[task] = {
            "n": n,
            "accepted": acc_count,
            "accept_rate": acc_count / n if n > 0 else 0.0,
            "total_cost": total_cost,
            "cost_per_run": total_cost / n if n > 0 else 0.0,
            "cost_per_accepted": total_cost / acc_count if acc_count > 0 else None,
            "routing_cost": routing_cost,
            "review_cost": review_cost,
            "execution_cost": exec_cost,
            "recommendation_acceptance_rate": rec_accept / rec_total if rec_total > 0 else None,
            "recommended_success_rate": rec_success / rec_success_total if rec_success_total > 0 else None,
        }
    
    return tasks


def aggregate(per_task, task_filter=None):
    """Aggregate across tasks (all, runnable, or judgment)."""
    if task_filter:
        rows = {t: v for t, v in per_task.items() if t in task_filter}
    else:
        rows = per_task
    
    if not rows:
        return None
    
    total_cost = sum(v["total_cost"] for v in rows.values())
    total_acc = sum(v["accepted"] for v in rows.values())
    
    rec_accept_rates = [v["recommendation_acceptance_rate"] for v in rows.values()
                        if v["recommendation_acceptance_rate"] is not None]
    rec_success_rates = [v["recommended_success_rate"] for v in rows.values()
                         if v["recommended_success_rate"] is not None]
    
    return {
        "tasks": len(rows),
        "total_cost": total_cost,
        "total_accepted": total_acc,
        "cost_per_accepted": total_cost / total_acc if total_acc > 0 else None,
        "mean_recommendation_acceptance_rate": statistics.mean(rec_accept_rates) if rec_accept_rates else None,
        "mean_recommended_success_rate": statistics.mean(rec_success_rates) if rec_success_rates else None,
    }


def build_summary(runs):
    strategies = sorted({r["strategy"] for r in runs})
    result = {"strategies": {}}
    
    for strat in strategies:
        per_task = strategy_stats(runs, strat)
        if per_task is None:
            continue
        
        result["strategies"][strat] = {
            "tasks": per_task,
            "aggregate": {
                "all": aggregate(per_task),
                "runnable": aggregate(per_task, RUNNABLE),
                "judgment": aggregate(per_task, JUDGMENT),
            }
        }
    
    return result


def to_markdown(summary):
    lines = ["# Recommend-Only Benchmark Results", ""]
    
    # Strategy comparison table
    lines += ["## Strategy Comparison", "",
              "| Strategy | Scope | Tasks | Total Cost $ | Accepted | Cost per Accepted $ | Rec. Accept % | Rec. Success % |",
              "|---|---|---|---|---|---|---|---|"]
    
    for strat, data in summary["strategies"].items():
        for scope_name in ("all", "runnable", "judgment"):
            agg = data["aggregate"][scope_name]
            if agg is None:
                continue
            
            label = {"all": "all", "runnable": "runnable (A,B,E)", "judgment": "judgment (C,D)"}[scope_name]
            rec_accept = f"{agg['mean_recommendation_acceptance_rate']:.0%}" if agg["mean_recommendation_acceptance_rate"] is not None else "n/a"
            rec_success = f"{agg['mean_recommended_success_rate']:.0%}" if agg["mean_recommended_success_rate"] is not None else "n/a"
            cpa = f"{agg['cost_per_accepted']:.4f}" if agg["cost_per_accepted"] is not None else "n/a"
            
            lines.append(f"| {strat} | {label} | {agg['tasks']} | {agg['total_cost']:.4f} | "
                        f"{agg['total_accepted']} | {cpa} | {rec_accept} | {rec_success} |")
    
    # Per-task breakdown
    lines += ["", "## Per-Task Breakdown", ""]
    for strat, data in summary["strategies"].items():
        lines += [f"### {strat}", "",
                  "| Task | N | Accept | Total $ | $/Run | $/Accepted | Route $ | Review $ | Exec $ |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for task in sorted(data["tasks"]):
            t = data["tasks"][task]
            cpa = f"{t['cost_per_accepted']:.4f}" if t["cost_per_accepted"] is not None else "n/a"
            lines.append(f"| {task} | {t['n']} | {t['accepted']}/{t['n']} ({t['accept_rate']:.0%}) | "
                        f"{t['total_cost']:.4f} | {t['cost_per_run']:.4f} | {cpa} | "
                        f"{t['routing_cost']:.4f} | {t['review_cost']:.4f} | {t['execution_cost']:.4f} |")
        lines.append("")
    
    return "\n".join(lines) + "\n"


def summarize(out_dir, basis="catalog"):
    out_dir = Path(out_dir)
    runs = load_runs(out_dir / "runs.jsonl")
    
    if basis == "billed":
        # Use CLI-reported costs when available
        for r in runs:
            for key in ("routing", "review", "execution"):
                rec = r.get(key)
                if rec and rec.get("cli_cost_usd") is not None:
                    rec["cost_usd"] = rec["cli_cost_usd"]
            # Recompute total
            r["total_cost_usd"] = sum(r.get(k, {}).get("cost_usd", 0.0)
                                     for k in ("routing", "review", "execution"))
    
    if not runs:
        print("No runs found")
        return None
    
    summary = build_summary(runs)
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
        print("Usage: summarize_recommend.py <output_dir> [--cost-basis=catalog|billed]")
        sys.exit(1)
    summarize(args[0], basis)
