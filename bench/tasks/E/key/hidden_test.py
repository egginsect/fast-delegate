"""Hidden extra checks for Task E (never shown to workers)."""

import unittest
from topo_sort import topological_sort, CycleError


def valid_order(graph, result):
    pos = {n: i for i, n in enumerate(result)}
    for node, deps in graph.items():
        for dep in deps:
            if pos[dep] > pos[node]:
                return False
    return len(pos) == len(result)


class TestHidden(unittest.TestCase):
    def test_wide_diamond_lattice(self):
        graph = {
            "top": ["l", "m", "r"],
            "l": ["x", "y"], "m": ["x", "y"], "r": ["y", "z"],
            "x": ["base"], "y": ["base"], "z": ["base"], "base": [],
        }
        result = topological_sort(graph)
        self.assertTrue(valid_order(graph, result))
        self.assertEqual(sorted(result), sorted(graph))

    def test_self_loop(self):
        with self.assertRaises(CycleError):
            topological_sort({"A": ["A"]})

    def test_cycle_behind_diamond(self):
        graph = {"A": ["B", "C"], "B": ["D"], "C": ["D"], "D": ["E"], "E": ["C"]}
        with self.assertRaises(CycleError):
            topological_sort(graph)

    def test_dependency_only_node(self):
        graph = {"A": ["B"]}
        self.assertEqual(topological_sort(graph), ["B", "A"])

    def test_repeated_call_is_independent(self):
        graph = {"A": ["B", "C"], "B": ["D"], "C": ["D"], "D": []}
        self.assertEqual(topological_sort(graph), topological_sort(graph))


if __name__ == "__main__":
    unittest.main()
