# Task B: Implement Interval Merging

Implement the two stub functions in `intervals.py`:

1. **`merge_intervals(intervals)`**: Takes a list of tuples (start, end) and returns a sorted list of non-overlapping intervals with all overlaps merged. Intervals that touch (end of one equals start of another) should be merged.

2. **`total_covered(intervals)`**: Takes a list of intervals and returns the total length of all unique coverage (i.e., the sum of the lengths of the merged intervals).

The file `test_intervals.py` contains the test cases that your implementation must pass.

**Acceptance criteria**: All tests in `test_intervals.py` must pass (`python3 -m unittest test_intervals`). The grader runs them against your `intervals.py` with the original test file.

Do not modify the test file; your task is only to implement the two functions.
