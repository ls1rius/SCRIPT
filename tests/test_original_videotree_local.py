"""Cohort identity and fixed-denominator reporting for the upstream baseline."""
from pathlib import Path
import tempfile
import unittest

from script import run_original_videotree_local as run


class OriginalVideoTreeTests(unittest.TestCase):
    def test_100_and_300_retain_reference_order_and_500_covers_every_example(self):
        with tempfile.TemporaryDirectory() as folder:
            reference = Path(folder)
            ids = [f'video{i:03d}' for i in range(500)]
            reference_ids = list(reversed(ids[50:350]))
            run.write(reference/'cohort.json', reference_ids)
            cohorts = run.aligned_cohorts(dict.fromkeys(ids), reference)
            self.assertEqual(cohorts['100'], reference_ids[:100])
            self.assertEqual(cohorts['300'], reference_ids)
            self.assertEqual(cohorts['500'][:300], reference_ids)
            self.assertEqual(set(cohorts['500']), set(ids))
            self.assertEqual(len(cohorts['500']), 500)

    def test_invalid_cohort_is_rejected_instead_of_resampled(self):
        with tempfile.TemporaryDirectory() as folder:
            reference = Path(folder)
            run.write(reference/'cohort.json', ['v']*300)
            with self.assertRaisesRegex(ValueError, 'distinct'):
                run.aligned_cohorts(dict.fromkeys(['v']+[str(i) for i in range(499)]), reference)

    def test_partial_report_never_claims_final_accuracy_and_errors_count_wrong(self):
        truth = dict(a=1, b=2, c=3)
        results = dict(a=dict(video_id='a', prediction=1, error=None),
                       b=dict(video_id='b', prediction=2, error={'message': 'failed'}))
        partial = run.summarize(list(truth), results, truth)
        self.assertIsNone(partial['accuracy'])
        self.assertEqual(partial['errors'], 1)
        self.assertEqual(partial['completed_accuracy'], .5)
        results['c'] = dict(video_id='c', prediction=0, error=None)
        final = run.summarize(list(truth), results, truth)
        self.assertEqual(final['accuracy'], 1/3)
        self.assertTrue(final['complete'])

    def test_paired_comparison_uses_same_ids_and_counts_both_sides_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run.write(root/'arms/local/runs/a/result.json', dict(answer=1, error=None))
            run.write(root/'arms/local/runs/b/result.json', dict(answer=2, error={'message': 'failed'}))
            results = dict(a=dict(video_id='a', prediction=0, error=None),
                           b=dict(video_id='b', prediction=2, error=None),
                           c=dict(video_id='c', prediction=3, error=None))
            report = run.comparison(['a', 'b', 'c'], results, root, 'local', dict(a=1, b=2, c=3))
            self.assertEqual(report['paired_completed'], 2)
            self.assertEqual(report['videotree_only_correct'], 1)
            self.assertEqual(report['reference_only_correct'], 1)
            self.assertEqual([r['video_id'] for r in report['rows']], ['a', 'b'])

    def test_inference_question_has_no_ground_truth(self):
        row = dict(question='Which?', truth=4, **{f'option {i}': str(i) for i in range(5)})
        question = run.question('v', row)
        self.assertNotIn('truth', question)
        self.assertEqual(question['options'], ['0', '1', '2', '3', '4'])


if __name__ == '__main__':
    unittest.main()
