import json
from pathlib import Path
import tempfile
import unittest

from script.run_compact_evidence_experiment import ARMS, eligible, next_range, report


class CompactExperimentTests(unittest.TestCase):
    def test_free_memory_on_a_busy_gpu_is_not_an_idle_card(self):
        policy = dict(minimum_free_mib=43008, maximum_idle_utilization=10)
        row = dict(free_mib=65000, utilization=0, compute_pids=[])
        self.assertTrue(eligible(row, policy))
        self.assertFalse(eligible(dict(row, compute_pids=['123']), policy))
        self.assertFalse(eligible(dict(row, utilization=94), policy))
        self.assertFalse(eligible(dict(row, free_mib=42000), policy))

    def test_waits_for_sealed_inputs_and_skips_successful_answers(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p/'cohort.json').write_text(json.dumps(['q0', 'q1', 'q2']))
            (p/'input_seals').mkdir()
            (p/'input_seals/q0.json').write_text('{}')
            self.assertEqual(next_range(p), (0, 1))
            for arm in ARMS:
                f = p/'runs'/arm/'q0/result.json'
                f.parent.mkdir(parents=True)
                f.write_text('{}')
            self.assertIsNone(next_range(p))
            (p/'input_seals/q2.json').write_text('{}')
            self.assertEqual(next_range(p), (2, 3))

    def test_partial_scores_are_paired_and_extraction_cost_is_not_free(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p/'cohort.json').write_text(json.dumps(['q0', 'q1']))
            (p/'evaluation_only.json').write_text(json.dumps(dict(q0='0', q1='1')))
            (p/'input_seals').mkdir()
            (p/'input_seals/q0.json').write_text(json.dumps(dict(inherited_extraction_seconds=100)))
            for arm, answer, error in [('A_original', '0', None), ('B_compact_text', None, {'message':'format'}),
                                       ('C_compact_text', '0', None)]:
                f = p/'runs'/arm/'q0/result.json'
                f.parent.mkdir(parents=True)
                f.write_text(json.dumps(dict(selected_option=answer, error=error, seconds=1, input_tokens=10)))
            r = report(p)
            self.assertFalse(r['complete'])
            self.assertIsNone(r['arms']['A_original']['accuracy'])
            self.assertEqual(r['common_completed'], 1)
            self.assertEqual(r['arms']['B_compact_text']['errors'], 1)
            self.assertEqual(r['inherited_extraction_seconds_completed_C'], 100)
            self.assertEqual(r['normalization_model_calls'], 0)


if __name__ == '__main__':
    unittest.main()
