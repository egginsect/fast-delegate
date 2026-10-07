"""Topological sorting with cycle detection."""


class CycleError(Exception):
    """Raised when a cycle is detected in the dependency graph."""
    pass


def topological_sort(graph):
    """Sort dependencies in topological order using DFS.

    Args:
        graph: dict mapping node -> list of dependencies (nodes it depends on)

    Returns:
        list of nodes in topological order (dependencies first)

    Raises:
        CycleError: If a cycle is detected
    """
    visited = set()
    result = []

    def dfs(node):
        """Depth-first search for cycle detection and ordering."""
        if node in visited:
            # BUG STILL HERE: Still treats all visited nodes as cycles
            raise CycleError(f"Cycle detected involving {node}")

        visited.add(node)

        for dep in graph.get(node, []):
            dfs(dep)

        result.append(node)

    for node in graph:
        if node not in visited:
            dfs(node)

    return result
