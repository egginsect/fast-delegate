"""Tests for interval merging and coverage utilities."""

import unittest
from intervals import merge_intervals, total_covered


class TestMergeIntervals(unittest.TestCase):
    """Tests for merge_intervals function."""

    def test_no_overlap(self):
        """Intervals with no overlap should be returned sorted."""
        result = merge_intervals([(5, 10), (1, 3)])
        self.assertEqual(result, [(1, 3), (5, 10)])

    def test_complete_overlap(self):
        """An interval completely inside another should be removed."""
        result = merge_intervals([(1, 10), (3, 5)])
        self.assertEqual(result, [(1, 10)])

    def test_partial_overlap(self):
        """Overlapping intervals should be merged."""
        result = merge_intervals([(1, 5), (3, 8)])
        self.assertEqual(result, [(1, 8)])

    def test_touching_intervals(self):
        """Intervals that touch at endpoints should be merged."""
        result = merge_intervals([(1, 3), (3, 5)])
        self.assertEqual(result, [(1, 5)])

    def test_empty_list(self):
        """Empty list should return empty list."""
        result = merge_intervals([])
        self.assertEqual(result, [])

    def test_single_interval(self):
        """Single interval should be returned unchanged."""
        result = merge_intervals([(1, 5)])
        self.assertEqual(result, [(1, 5)])

    def test_multiple_merges(self):
        """Multiple intervals requiring cascading merges."""
        result = merge_intervals([(1, 3), (2, 5), (4, 8), (7, 10)])
        self.assertEqual(result, [(1, 10)])

    def test_duplicate_intervals(self):
        """Duplicate intervals should be merged."""
        result = merge_intervals([(1, 5), (1, 5)])
        self.assertEqual(result, [(1, 5)])


class TestTotalCovered(unittest.TestCase):
    """Tests for total_covered function."""

    def test_no_overlap(self):
        """Non-overlapping intervals sum their lengths."""
        result = total_covered([(1, 3), (5, 8)])
        self.assertEqual(result, 5)  # (3-1) + (8-5) = 2 + 3 = 5

    def test_complete_overlap(self):
        """Overlapping intervals count overlap once."""
        result = total_covered([(1, 10), (3, 5)])
        self.assertEqual(result, 9)  # Coverage from 1 to 10 = 9

    def test_partial_overlap(self):
        """Partial overlap should be counted once."""
        result = total_covered([(1, 5), (3, 8)])
        self.assertEqual(result, 7)  # Coverage from 1 to 8 = 7

    def test_empty_list(self):
        """Empty list should return 0."""
        result = total_covered([])
        self.assertEqual(result, 0)

    def test_single_interval(self):
        """Single interval coverage."""
        result = total_covered([(2, 7)])
        self.assertEqual(result, 5)  # 7 - 2 = 5

    def test_touching_intervals(self):
        """Touching intervals should merge coverage."""
        result = total_covered([(1, 3), (3, 5)])
        self.assertEqual(result, 4)  # Coverage from 1 to 5 = 4

    def test_multiple_gaps(self):
        """Multiple non-overlapping intervals."""
        result = total_covered([(1, 2), (4, 5), (7, 10)])
        self.assertEqual(result, 5)  # 1 + 1 + 3 = 5

    def test_duplicate_intervals(self):
        """Duplicate intervals count once."""
        result = total_covered([(1, 5), (1, 5)])
        self.assertEqual(result, 4)  # 5 - 1 = 4


if __name__ == "__main__":
    unittest.main()
