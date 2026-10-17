"""Paired comparisons must not count pending rows or silently mismatch cohorts."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / 'script'))
from compare_caption_first_arms import paired


class PairedCaptionTests(unittest.TestCase):
    def test_pending_rows_are_excluded_and_all_four_paired_outcomes_are_distinct(self):
        a = {str(i): {'completed': True, 'correct': i in (0, 1, 4)} for i in range(5)}
        b = {str(i): {'completed': i != 4, 'correct': i in (0, 2)} for i in range(5)}
        self.assertEqual(paired(a, b), {'completed_pairs': 4, 'both_correct': 1,
                         'coverage_only': 1, 'guided_only': 1, 'both_wrong': 1})

    def test_different_cohorts_cannot_be_reported_as_matched(self):
        with self.assertRaises(ValueError):
            paired({'a': {}}, {'b': {}})


if __name__ == '__main__':
    unittest.main()
