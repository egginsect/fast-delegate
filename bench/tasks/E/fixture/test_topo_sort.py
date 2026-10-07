"""Tests for topological sorting."""

import unittest
from topo_sort import topological_sort, CycleError


class TestTopologicalSort(unittest.TestCase):
    """Tests for the topological_sort function."""

    def test_simple_chain(self):
        """A -> B -> C should give [C, B, A]."""
        graph = {"A": ["B"], "B": ["C"], "C": []}
        result = topological_sort(graph)
        self.assertEqual(result, ["C", "B", "A"])

    def test_diamond_dependency(self):
        """Diamond: A depends on B and C, both depend on D.

        This should NOT raise a CycleError.
        Expected order: D, C, B, A or D, B, C, A (C and B can be in any order)
        """
        graph = {"A": ["B", "C"], "B": ["D"], "C": ["D"], "D": []}
        result = topological_sort(graph)
        # D should come first (no dependencies)
        self.assertEqual(result[0], "D")
        # A should come last (depends on B and C)
        self.assertEqual(result[-1], "A")

    def test_real_cycle(self):
        """A -> B -> C -> A should raise CycleError."""
        graph = {"A": ["B"], "B": ["C"], "C": ["A"]}
        with self.assertRaises(CycleError):
            topological_sort(graph)

    def test_single_node(self):
        """Single node with no dependencies."""
        graph = {"A": []}
        result = topological_sort(graph)
        self.assertEqual(result, ["A"])

    def test_independent_components(self):
        """Multiple independent components.

        A -> B and C -> D should both be valid orderings.
        """
        graph = {"A": ["B"], "B": [], "C": ["D"], "D": []}
        result = topological_sort(graph)
        # Check ordering constraints
        self.assertLess(result.index("B"), result.index("A"))
        self.assertLess(result.index("D"), result.index("C"))


if __name__ == "__main__":
    unittest.main()
