# Task E: Debug and Fix

The topological sort implementation in `topo_sort.py` raises false `CycleError` exceptions on some acyclic graphs, for example diamond-shaped dependency graphs.

Your task is to **fix the bug** in `topo_sort.py` so that:
1. All tests in `test_topo_sort.py` pass (`python3 -m unittest test_topo_sort`)
2. Diamond dependencies (where multiple nodes depend on a common node) are handled correctly
3. Real cycles are still detected and raise `CycleError`

Do not modify the tests.

**Acceptance criteria**: The tests in `test_topo_sort.py` pass and the function behaves correctly on other acyclic and cyclic graphs (a hidden check is also run).
