"""Controlled migrations retain the original route and honest paired denominators."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / 'script'))
from script_core.config import DEFAULTS, digest, load_config
from script_core.migration import original_call, scripted_call, distinguishing_claims, accept_guard, STAGES
from run_migration import run_stage, score, write
from script_core.budget import ToolFailure


def packet():
    return {'question_id': 'q', 'question': 'Which tool is used?', 'options': ['A wrench', 'A pliers', 'A pen', 'A hammer', 'A brush'],
            'duration': 3, 'captions': ['A tool is held', 'A pliers touches the bolt', 'The hand moves'],
            'selected_seconds': [0, 2], 'coarse_context': {'windows': [{'id': 'w', 'interval': [0, 3], 'caption': 'A tool is held'}]},
            'script_outline': {'scenes': []}, 'facts': {a: {'claims': [], 'evidence': [], 'historical_acquisition_seconds': 10.}
                                                       for a in ('script_guided', 'coverage_first')}}


def add_claim(p, kind='action', count=3):
    p['facts']['script_guided']['claims'] = [{'id': 'c', 'status': 'supported', 'source_type': 'visual', 'description': 'C uses a pliers to move the bolt.',
        'fact_type': kind, 'interval': [0, 3], 'evidence_ids': ['e'], 'source_frame_ids': [f'f{i}' for i in range(count)],
        'verification': {'whole_claim_supported': True, 'process_visible': True, 'unresolved_conflicts': []}}]
    p['facts']['script_guided']['evidence'] = [{'id': 'e', 'frame_inputs': [{'frame_id': f'f{i}', 'pts': i} for i in range(count)]}]


class Backend:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def call(self, operation, inputs, timeout):
        self.calls.append((operation, deepcopy(inputs)))
        if operation == 'videotree_qa':
            return {'data': {'selected_option': '0', 'text': 'prediction: A'}, 'usage': {'input_tokens': 100}}
        if operation == 'migration_guard':
            if self.fail:
                raise ToolFailure('Guard failed')
            return {'data': {'accept': True, 'resolves_distinction': True, 'claim_ids': ['c'], 'reason': 'The tool is visible'}}
        if operation == 'caption_review':
            return {'data': {'text': 'A tool is visible', 'selected_option': '0', 'caption_ids': ['C1'], 'checks': []}}
        raise AssertionError(operation)


class MigrationTests(unittest.TestCase):
    def test_serialized_worker_config_preserves_scientific_notation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            expected = load_config()
            write(path, expected)
            self.assertEqual(load_config(path), expected)
            self.assertIsInstance(load_config(path)['segmentation']['tie_epsilon'], float)

    def test_original_factory_is_retained_and_only_retrieval_changes(self):
        p = packet()
        before = deepcopy(p)
        selected, full = original_call(p), original_call(p, all_seconds=True)
        self.assertEqual(selected['head'], full['head'])
        self.assertIn('Here are a few examples. \nNone ', selected['prompts'][0])
        self.assertNotIn('Frame 1 (1s)', selected['prompts'][0])
        self.assertIn('Frame 1 (1s)', full['prompts'][0])
        self.assertIn('A: A wrench', selected['prompts'][0])
        self.assertEqual(p, before)

    def test_no_facts_means_no_input_difference_or_answer_drift(self):
        p = packet()
        a, _ = scripted_call(p, 's4')
        b, _ = scripted_call(p, 's5')
        c, _ = scripted_call(p, 's6')
        self.assertEqual(digest(a), digest(b))
        self.assertEqual(digest(b), digest(c))
        backend = Backend()
        previous = {'s4': {'stage': 's4', 'input_sha256': digest(a), 'prediction': '1',
                            'answer': {'selected_option': '1'}, 'error': None, 'standalone_qa_seconds': 2.}}
        with tempfile.TemporaryDirectory() as folder:
            result = run_stage(Path(folder), 'q', p, 's5', backend, deepcopy(DEFAULTS), previous)
        self.assertEqual(result['prediction'], '1')
        self.assertEqual(result['reused_identical_input'], 's4')
        self.assertEqual(backend.calls, [])

    def test_extensions_disabled_runs_exact_original_qa_call(self):
        p, backend = packet(), Backend()
        with tempfile.TemporaryDirectory() as folder:
            r = run_stage(Path(folder), 'q', p, 'v0', backend, deepcopy(DEFAULTS), {})
        self.assertEqual(backend.calls, [('videotree_qa', original_call(p))])
        self.assertTrue(r['extensions_disabled'])
        self.assertEqual(r['prediction'], '0')

    def test_joint_review_is_an_isolated_decision_without_visual_execution(self):
        p, backend = packet(), Backend()
        with tempfile.TemporaryDirectory() as folder:
            result = run_stage(Path(folder), 'q', p, 'c8', backend, deepcopy(DEFAULTS), {})
        self.assertIsNone(result['error'])
        self.assertEqual([op for op, _ in backend.calls], ['caption_review'])
        self.assertEqual(result['requested_reads'], 0)
        self.assertEqual(result['fresh_visual_reads'], 0)

    def test_guard_without_distinguishing_facts_keeps_baseline_without_a_model_call(self):
        p, backend = packet(), Backend()
        prev = {'v0': {'prediction': '0', 'answer': {}, 'error': None, 'standalone_qa_seconds': 1.},
                's5': {'prediction': '1', 'answer': {}, 'error': None, 'standalone_qa_seconds': 1.}}
        with tempfile.TemporaryDirectory() as folder:
            r = run_stage(Path(folder), 'q', p, 'g7', backend, deepcopy(DEFAULTS), prev)
        self.assertEqual(r['prediction'], '0')
        self.assertFalse(r['accepted'])
        self.assertEqual(backend.calls, [])

    def test_guard_rejects_single_frame_dynamic_state_and_unseen_citations(self):
        p = packet()
        add_claim(p, kind='state', count=1)
        self.assertEqual(distinguishing_claims(p, 0, 1), [])
        add_claim(p)
        eligible = distinguishing_claims(p, 0, 1)
        self.assertEqual(len(eligible), 1)
        self.assertFalse(accept_guard({'accept': True, 'resolves_distinction': True, 'claim_ids': ['invented']}, eligible))
        self.assertFalse(accept_guard({'accept': True, 'resolves_distinction': False, 'claim_ids': ['c']}, eligible))
        self.assertTrue(accept_guard({'accept': True, 'resolves_distinction': True, 'claim_ids': ['c']}, eligible))

    def test_guard_failure_is_recorded_and_preserves_valid_baseline(self):
        p, backend = packet(), Backend(fail=True)
        add_claim(p)
        cfg = deepcopy(DEFAULTS)
        cfg['query']['tool_transient_retries'] = 0
        previous = {'v0': {'prediction': '0', 'answer': {}, 'error': None, 'standalone_qa_seconds': 1.},
                    's5': {'prediction': '1', 'answer': {}, 'error': None, 'standalone_qa_seconds': 1.}}
        with tempfile.TemporaryDirectory() as folder:
            result = run_stage(Path(folder), 'q', p, 'g7', backend, cfg, previous)
        self.assertEqual(result['prediction'], '0')
        self.assertIsNone(result['error'])
        self.assertIn('extension_error', result)

    def test_scoring_keeps_errors_in_denominator_and_pending_accuracy_null(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            write(out / 'cohort.json', ['a', 'b'])
            write(out / 'evaluation_only.json', {uid: {'truth': '1', 'original_prediction': '1'} for uid in ['a', 'b']})
            write(out / 'experiment.json', {'cost_notes': ['QA-only replay']})
            for stage in STAGES:
                write(out / 'results/a' / stage / 'result.json', {'prediction': '1', 'error': None,
                    'replay_wall_seconds': 2., 'standalone_qa_seconds': 2., 'historical_acquisition_seconds': 0., 'cost_summary': {}})
            report = score(out)
            self.assertIsNone(report['groups']['v0']['accuracy'])
            write(out / 'results/b/v0/result.json', {'prediction': None, 'error': 'Original missing tree',
                'replay_wall_seconds': 0., 'standalone_qa_seconds': 0., 'historical_acquisition_seconds': 0., 'cost_summary': {}})
            report = score(out)
            self.assertEqual(report['groups']['v0']['accuracy'], .5)
            self.assertEqual(report['groups']['v0']['errors'], 1)
            self.assertEqual(report['paired']['v0_to_v1']['completed_pairs'], 1)


if __name__ == '__main__':
    unittest.main()
