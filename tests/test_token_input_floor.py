"""Offline fixtures for explicit est_input_tokens / est_output_tokens flooring behaviour.

Acceptance criteria from the task spec:
  A) Explicit 200000 in + 6000 out; learned total 65540 => floored to 206000,
     split input>=200000 and output>=6000; cost at $3/$15 per M >= 0.69.
  B) Larger learned total 412000 remains 412000; retains basis; splits 400000/12000.
  C) Neither field explicit; learned 900 stays 900 (no default floor).
  D) Only-input-explicit (200000): retain the implicit output default and ratio (202000 total).
  E) Only-output-explicit (6000): retain the implicit input default and ratio (26000 total).
  F) Estimate path (no learned stats): explicit sum returned as-is (no double-floor).
"""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/fast-delegate/scripts/fdel.py"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_fast_delegate_skill import cand, entry


def load_fdel():
    spec = importlib.util.spec_from_file_location("fdel", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TokenFloorTests(unittest.TestCase):
    """Explicit est_input/est_output tokens define minimums for learned estimates."""

    def setUp(self):
        self.fdel = load_fdel()

    # --- A: learned total below explicit sum is floored -----------------------

    def test_a_learned_below_explicit_sum_floors_to_sum(self):
        task = {"est_input_tokens": 200000, "est_output_tokens": 6000, "family": "f"}
        stats = {("f", "cand"): [65540]}
        total, basis = self.fdel.estimate_tokens(task, "f", "cand", stats)
        self.assertEqual(total, 206000)
        self.assertEqual(basis, "candidate")

    def test_a_family_median_below_explicit_sum_floors_to_sum(self):
        task = {"est_input_tokens": 200000, "est_output_tokens": 6000, "family": "f"}
        stats = {("f", "other-cand"): [65540]}
        total, basis = self.fdel.estimate_tokens(task, "f", "cand", stats)
        self.assertEqual(total, 206000)
        self.assertEqual(basis, "family")

    def test_a_split_respects_per_side_minimums(self):
        task = {"est_input_tokens": 200000, "est_output_tokens": 6000, "family": "f"}
        total = 206000
        inp, out = self.fdel.split_tokens(total, task)
        self.assertEqual(inp, 200000)
        self.assertEqual(out, 6000)

    def test_a_cost_at_sonnet_3_15_per_m_meets_floor(self):
        """At $3/$15 per M for floored 206000 total the cost must be >= 0.69."""
        task = {"est_input_tokens": 200000, "est_output_tokens": 6000, "family": "f"}
        stats = {("f", "cand"): [65540]}
        total, _ = self.fdel.estimate_tokens(task, "f", "cand", stats)
        inp, out = self.fdel.split_tokens(total, task)
        cost = inp * 3 / 1e6 + out * 15 / 1e6
        self.assertGreaterEqual(round(cost, 8), 0.69)

    def test_a_rank_candidates_cost_meets_floor(self):
        """rank_candidates uses floored token estimate; cost >= 0.69 at 3/15."""
        fdel = self.fdel
        e = entry(3, 15, ctx=1000000)
        c = cand("sonnet-like", 3, 15, ctx=1000000, catalog_entry=e)
        task = {"est_input_tokens": 200000, "est_output_tokens": 6000, "family": "f"}
        stats = {("f", "sonnet-like"): [65540]}
        ranked = fdel.rank_candidates([c], task, {}, token_stats=stats)
        self.assertGreaterEqual(ranked[0]["cost_usd"], 0.69)
        self.assertEqual(ranked[0]["token_basis"], "candidate")
        self.assertEqual(ranked[0]["token_floor"], {"applied": True, "required_total": 206000})

    # --- B: larger learned total is preserved ---------------------------------

    def test_b_larger_learned_total_not_capped(self):
        task = {"est_input_tokens": 200000, "est_output_tokens": 6000, "family": "f"}
        stats = {("f", "cand"): [412000]}
        total, basis = self.fdel.estimate_tokens(task, "f", "cand", stats)
        self.assertEqual(total, 412000)
        self.assertEqual(basis, "candidate")

    def test_b_split_preserves_ratio_for_larger_learned(self):
        """412000 splits ~400000/12000 at the 200000:6000 ratio."""
        task = {"est_input_tokens": 200000, "est_output_tokens": 6000, "family": "f"}
        inp, out = self.fdel.split_tokens(412000, task)
        # ratio is 200000/206000 ~= 0.9709; 412000 * 0.9709 ~= 400000
        self.assertAlmostEqual(inp, 400000, delta=2)
        self.assertAlmostEqual(out, 12000, delta=2)

    def test_b_family_median_also_preserved_when_larger(self):
        task = {"est_input_tokens": 200000, "est_output_tokens": 6000, "family": "f"}
        stats = {("f", "other-cand"): [412000]}
        total, basis = self.fdel.estimate_tokens(task, "f", "cand", stats)
        self.assertEqual(total, 412000)
        self.assertEqual(basis, "family")

    # --- C: no explicit fields => no floor ------------------------------------

    def test_c_neither_explicit_no_floor_applied(self):
        """Without est_input_tokens/est_output_tokens, learned 900 is returned as-is."""
        task = {"family": "f"}  # no explicit fields
        stats = {("f", "cand"): [900]}
        total, basis = self.fdel.estimate_tokens(task, "f", "cand", stats)
        self.assertEqual(total, 900)
        self.assertEqual(basis, "candidate")

    def test_c_explicit_zero_is_present_but_does_not_create_positive_floor(self):
        task = {"est_input_tokens": 0, "family": "f"}
        stats = {("f", "cand"): [900]}
        self.assertEqual(self.fdel._explicit_token_floor(task), (0, None))
        self.assertEqual(self.fdel.estimate_tokens(task, "f", "cand", stats), (900, "candidate"))

    def test_c_family_median_900_not_floored_by_defaults(self):
        task = {}  # completely absent
        stats = {("f", "other"): [900]}
        total, basis = self.fdel.estimate_tokens(task, "f", "cand", stats)
        self.assertEqual(total, 900)
        self.assertEqual(basis, "family")

    # --- D: only input explicit -----------------------------------------------

    def test_d_only_input_explicit_floors_total_to_explicit_in(self):
        """The implicit 2000 output default remains in the ratio when input is explicit."""
        task = {"est_input_tokens": 200000, "family": "f"}
        stats = {("f", "cand"): [65000]}
        total, basis = self.fdel.estimate_tokens(task, "f", "cand", stats)
        self.assertEqual(total, 202000)
        self.assertEqual(basis, "candidate")

    def test_d_only_input_explicit_split_satisfies_input_min(self):
        task = {"est_input_tokens": 200000, "family": "f"}
        inp, out = self.fdel.split_tokens(202000, task)
        self.assertEqual(inp, 200000)
        self.assertEqual(out, 2000)

    def test_d_only_input_floor_is_disclosed_in_ranked_estimate(self):
        e = entry(3, 15, ctx=1000000)
        c = cand("sonnet-like", 3, 15, ctx=1000000, catalog_entry=e)
        task = {"est_input_tokens": 200000, "family": "f"}
        stats = {("f", "sonnet-like"): [65000]}
        ranked = self.fdel.rank_candidates([c], task, {}, token_stats=stats)
        self.assertEqual(ranked[0]["token_basis"], "candidate")
        self.assertEqual(ranked[0]["est_tokens"], 202000)
        self.assertEqual(ranked[0]["token_floor"], {"applied": True, "required_total": 202000})

    def test_d_only_input_explicit_does_not_suppress_larger_learned(self):
        task = {"est_input_tokens": 200000, "family": "f"}
        stats = {("f", "cand"): [350000]}
        total, _ = self.fdel.estimate_tokens(task, "f", "cand", stats)
        self.assertEqual(total, 350000)

    # --- E: only output explicit -----------------------------------------------

    def test_e_only_output_explicit_floors_total_to_explicit_out(self):
        """The implicit 20000 input default remains in the ratio when output is explicit."""
        task = {"est_output_tokens": 6000, "family": "f"}
        stats = {("f", "cand"): [3000]}
        total, basis = self.fdel.estimate_tokens(task, "f", "cand", stats)
        self.assertEqual(total, 26000)
        self.assertEqual(basis, "candidate")

    def test_e_only_output_explicit_split_satisfies_output_min(self):
        task = {"est_output_tokens": 6000, "family": "f"}
        inp, out = self.fdel.split_tokens(26000, task)
        self.assertEqual(inp, 20000)
        self.assertEqual(out, 6000)

    def test_e_only_output_floor_is_disclosed_in_ranked_estimate(self):
        e = entry(3, 15, ctx=1000000)
        c = cand("sonnet-like", 3, 15, ctx=1000000, catalog_entry=e)
        task = {"est_output_tokens": 6000, "family": "f"}
        stats = {("f", "sonnet-like"): [3000]}
        ranked = self.fdel.rank_candidates([c], task, {}, token_stats=stats)
        self.assertEqual(ranked[0]["token_basis"], "candidate")
        self.assertEqual(ranked[0]["est_tokens"], 26000)
        self.assertEqual(ranked[0]["token_floor"], {"applied": True, "required_total": 26000})

    def test_e_only_output_explicit_does_not_suppress_larger_learned(self):
        task = {"est_output_tokens": 6000, "family": "f"}
        stats = {("f", "cand"): [50000]}
        total, _ = self.fdel.estimate_tokens(task, "f", "cand", stats)
        self.assertEqual(total, 50000)

    # --- F: estimate path (no learned stats) - no double-floor ----------------

    def test_f_estimate_path_returns_explicit_sum_unchanged(self):
        """When no learned stats exist, estimate_tokens returns est_in+est_out directly."""
        task = {"est_input_tokens": 200000, "est_output_tokens": 6000}
        total, basis = self.fdel.estimate_tokens(task, "f", "cand", {})
        self.assertEqual(total, 206000)
        self.assertEqual(basis, "estimate")

    def test_f_estimate_path_no_learned_no_explicit_uses_defaults(self):
        task = {}
        total, basis = self.fdel.estimate_tokens(task, "f", "cand", {})
        self.assertEqual(total, 22000)  # 20000 + 2000
        self.assertEqual(basis, "estimate")


if __name__ == "__main__":
    unittest.main()
