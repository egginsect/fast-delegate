#!/usr/bin/env python3
"""Tests for the recommend-only benchmark (route_tasks, run_recommend, summarize_recommend)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BENCH = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BENCH))
import route_tasks
import run_recommend
import summarize_recommend


class TestRouteTasks(unittest.TestCase):
    def test_all_tasks_defined(self):
        for task_id in ("A", "B", "C", "D", "E"):
            self.assertIn(task_id, route_tasks.TASKS)
    
    def test_task_json_schema(self):
        for task_id, spec in route_tasks.TASKS.items():
            with self.subTest(task=task_id):
                self.assertIn("deliverable", spec)
                self.assertIn("acceptance", spec)
                self.assertIn("context", spec)
                self.assertIsInstance(spec["deliverable"], str)
                self.assertIsInstance(spec["acceptance"], list)
                self.assertTrue(len(spec["acceptance"]) > 0)
                self.assertIn("files", spec["context"])
                self.assertIn("verifiable", spec["context"])
                self.assertIn("independence", spec["context"])
                self.assertIsInstance(spec["context"]["verifiable"], bool)
                self.assertIsInstance(spec["context"]["independence"], bool)
    
    def test_runnable_tasks_are_verifiable_and_independent(self):
        for task_id in ("A", "B", "E"):
            spec = route_tasks.TASKS[task_id]
            self.assertTrue(spec["context"]["verifiable"], f"{task_id} should be verifiable")
            self.assertTrue(spec["context"]["independence"], f"{task_id} should be independent")
    
    def test_judgment_tasks_are_not_verifiable(self):
        for task_id in ("C", "D"):
            spec = route_tasks.TASKS[task_id]
            self.assertFalse(spec["context"]["verifiable"], f"{task_id} should not be verifiable")
    
    def test_generate_writes_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = route_tasks.generate_task_json("A", tmp)
            self.assertTrue(path.exists())
            data = json.loads(path.read_text())
            self.assertEqual(data["deliverable"], route_tasks.TASKS["A"]["deliverable"])
            self.assertEqual(data["acceptance"], route_tasks.TASKS["A"]["acceptance"])


class TestRunRecommend(unittest.TestCase):
    def test_parse_decision(self):
        self.assertEqual(run_recommend.parse_decision("DECISION: ACCEPT\nBecause..."), "ACCEPT")
        self.assertEqual(run_recommend.parse_decision("DECISION: REJECT\nReason..."), "REJECT")
        self.assertIsNone(run_recommend.parse_decision("I think we should accept it"))
        self.assertEqual(run_recommend.parse_decision("First DECISION: ACCEPT then DECISION: REJECT"), "REJECT")
    
    def test_load_task_json_fails_without_file(self):
        with self.assertRaises(run_recommend.BenchError):
            run_recommend.load_task_json("NONEXISTENT")
    
    def test_build_review_prompt_contains_key_elements(self):
        task_spec = {"deliverable": "Do X", "acceptance": ["A", "B"]}
        rec = {"id": "haiku", "decision": "delegate", "cost_usd": 0.05}
        prompt = run_recommend.build_review_prompt(task_spec, rec)
        self.assertIn("Do X", prompt)
        self.assertIn("haiku", prompt)
        self.assertIn("DECISION: ACCEPT", prompt)
        self.assertIn("DECISION: REJECT", prompt)
    
    def test_dry_run_counts_jobs(self):
        import subprocess
        res = subprocess.run([sys.executable, str(BENCH / "run_recommend.py"),
                             "--strategies", "lead-alone", "--tasks", "A,B",
                             "--runs", "2", "--dry-run"],
                            capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("4", res.stdout)  # 1 strategy x 2 tasks x 2 runs = 4
        self.assertIn("not executing", res.stdout)
    
    def test_fails_without_task_json(self):
        import subprocess
        # Use an existing task dir but move task.json temporarily
        task_json = BENCH / "tasks" / "A" / "task.json"
        backup = task_json.with_suffix(".json.bak")
        try:
            task_json.rename(backup)
            res = subprocess.run([sys.executable, str(BENCH / "run_recommend.py"),
                                 "--tasks", "A", "--dry-run"],
                                capture_output=True, text=True)
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("task.json not found", res.stderr)
        finally:
            if backup.exists():
                backup.rename(task_json)


class TestSummarizeRecommend(unittest.TestCase):
    def _mk_run(self, strategy, task, run, total_cost, accepted, decided=None, executed=None,
                review_decision=None, rec_accepted=None):
        r = {"strategy": strategy, "task": task, "run": run, "total_cost_usd": total_cost,
             "grade": {"accepted": accepted}, "routing": {"cost_usd": 0.001},
             "review": None, "execution": {"cost_usd": total_cost - 0.001},
             "decided_model": decided or "opus", "executed_model": executed or "opus"}
        if review_decision:
            r["review"] = {"decision": review_decision, "cost_usd": 0.01}
            r["execution"]["cost_usd"] -= 0.01
        return r
    
    def test_strategy_stats(self):
        runs = [
            self._mk_run("lead-alone", "A", 0, 1.0, True),
            self._mk_run("lead-alone", "A", 1, 1.0, False),
            self._mk_run("lead-alone", "B", 0, 0.8, True),
        ]
        stats = summarize_recommend.strategy_stats(runs, "lead-alone")
        self.assertEqual(stats["A"]["n"], 2)
        self.assertEqual(stats["A"]["accepted"], 1)
        self.assertAlmostEqual(stats["A"]["accept_rate"], 0.5)
        self.assertAlmostEqual(stats["A"]["total_cost"], 2.0)
        self.assertAlmostEqual(stats["A"]["cost_per_accepted"], 2.0)
        self.assertIsNone(stats["A"]["recommendation_acceptance_rate"])
    
    def test_recommendation_acceptance_and_success(self):
        runs = [
            self._mk_run("route-jev", "A", 0, 0.5, True, "haiku", "haiku", "ACCEPT", True),
            self._mk_run("route-jev", "A", 1, 0.5, False, "haiku", "haiku", "ACCEPT", False),
            self._mk_run("route-jev", "A", 2, 1.0, True, "opus", "opus", "REJECT", None),
        ]
        stats = summarize_recommend.strategy_stats(runs, "route-jev")
        self.assertAlmostEqual(stats["A"]["recommendation_acceptance_rate"], 2/3)  # 2 ACCEPT out of 3
        self.assertAlmostEqual(stats["A"]["recommended_success_rate"], 0.5)  # 1 success out of 2 accepts
    
    def test_aggregate(self):
        per_task = {
            "A": {"total_cost": 1.0, "accepted": 2, "recommendation_acceptance_rate": 0.8,
                  "recommended_success_rate": 0.9},
            "B": {"total_cost": 0.5, "accepted": 1, "recommendation_acceptance_rate": 0.7,
                  "recommended_success_rate": 0.8},
        }
        agg = summarize_recommend.aggregate(per_task)
        self.assertEqual(agg["tasks"], 2)
        self.assertAlmostEqual(agg["total_cost"], 1.5)
        self.assertEqual(agg["total_accepted"], 3)
        self.assertAlmostEqual(agg["cost_per_accepted"], 0.5)
        self.assertAlmostEqual(agg["mean_recommendation_acceptance_rate"], 0.75)
        self.assertAlmostEqual(agg["mean_recommended_success_rate"], 0.85)
    
    def test_aggregate_with_filter(self):
        per_task = {"A": {"total_cost": 1.0, "accepted": 1, "recommendation_acceptance_rate": None,
                          "recommended_success_rate": None},
                    "C": {"total_cost": 0.5, "accepted": 1, "recommendation_acceptance_rate": None,
                          "recommended_success_rate": None}}
        agg_runnable = summarize_recommend.aggregate(per_task, ("A", "B", "E"))
        self.assertEqual(agg_runnable["tasks"], 1)
        self.assertAlmostEqual(agg_runnable["total_cost"], 1.0)
    
    def test_to_markdown(self):
        runs = [self._mk_run("lead-alone", "A", 0, 1.0, True)]
        summary = summarize_recommend.build_summary(runs)
        md = summarize_recommend.to_markdown(summary)
        self.assertIn("lead-alone", md)
        self.assertIn("Strategy Comparison", md)
        self.assertIn("Per-Task Breakdown", md)


class TestTaskJsonFiles(unittest.TestCase):
    """Test that generated task.json files exist and are valid."""
    
    def test_all_task_json_exist(self):
        for task_id in ("A", "B", "C", "D", "E"):
            path = BENCH / "tasks" / task_id / "task.json"
            self.assertTrue(path.exists(), f"task.json missing for {task_id}")
            data = json.loads(path.read_text())
            self.assertIn("deliverable", data)
            self.assertIn("acceptance", data)
            self.assertIn("context", data)


if __name__ == "__main__":
    import os
    unittest.main()
