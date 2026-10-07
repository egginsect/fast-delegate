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
    on_stack = set()
    result = []

    def dfs(node):
        """Depth-first search for cycle detection and ordering.

        Uses two sets: 'visited' for completely processed nodes,
        and 'on_stack' for nodes currently being processed.
        """
        if node in on_stack:
            # This node is currently being processed (cycle!)
            raise CycleError(f"Cycle detected involving {node}")

        if node in visited:
            # Already fully processed
            return

        on_stack.add(node)

        for dep in graph.get(node, []):
            dfs(dep)

        on_stack.remove(node)
        visited.add(node)
        result.append(node)

    for node in graph:
        if node not in visited:
            dfs(node)

    return result
