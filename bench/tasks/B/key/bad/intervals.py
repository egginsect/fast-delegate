"""Interval merging and coverage calculation utilities."""


def merge_intervals(intervals):
    """Merge overlapping intervals into a list of non-overlapping intervals.

    Args:
        intervals: List of tuples (start, end) representing intervals

    Returns:
        List of merged, non-overlapping intervals sorted by start time
    """
    if not intervals:
        return []

    # Sort by start time
    sorted_intervals = sorted(intervals)

    merged = [sorted_intervals[0]]
    for start, end in sorted_intervals[1:]:
        last_start, last_end = merged[-1]

        # BUG: Only merges if strictly overlapping, not if touching
        if start < last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))

    return merged


def total_covered(intervals):
    """Calculate the total length of the union of intervals.

    Args:
        intervals: List of tuples (start, end) representing intervals

    Returns:
        Total coverage length (may include gaps within intervals)
    """
    if not intervals:
        return 0

    # BUG: Doesn't merge, just sums all interval lengths
    return sum(end - start for start, end in intervals)
