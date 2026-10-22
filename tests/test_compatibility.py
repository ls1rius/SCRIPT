"""SCRIPT's common entry reproduces VideoTree and isolates component switches."""
from copy import deepcopy
import json
from pathlib import Path
import re
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]/'script'))
from script_core.config import DEFAULTS, digest, validate_config
from script_core.controller import answer_query
from script_core.compatibility import qa_inputs, add_graph_seconds, protected_claims
from script_core.schemas import output_schema, validate_output
from script_core.state import memory_hash
from script_core.summary_graph import assemble_graph, source_records
from script_core.video import file_hash
from script_core.videotree_tools import run_width, run_depth, call_original
from run_compatibility import PROFILES, profile_config, baseline_comparison, gate, score, write
from test_pilot_repairs import long_base
from test_script_core import FixtureBackend


class CompatBackend(FixtureBackend):
    def __init__(self, memory):
        super().__init__(memory)
        self.seen = []
        self.fail_feedback = False
        self.fail_guard = False
        self.guard_accept = False
    def call(self, operation, inputs, timeout):
        self.seen.append((operation, deepcopy(inputs)))
        if operation == 'videotree_width':
            self.operations.append(operation)
            return {'data': {'width': {'name': 'q', 'cluster_ids_x': [0]*180},
                              'relevance': {'cluster_relevance': [3]}}}
        if operation == 'videotree_depth':
            self.operations.append(operation)
            return {'data': {'selected_seconds': [0, 1, 2]}}
        if operation == 'videotree_qa':
            self.operations.append(operation)
            extended = 'Frame 35 (35s)' in inputs['prompts'][0] or 'Additional local visual observations' in inputs['prompts'][0]
            return {'data': {'selected_option': '1' if extended else '0', 'text': 'prediction: B' if extended else 'prediction: A',
                             'info': {'prompt_tokens': 100}}}
        if operation == 'videotree_feedback':
            self.operations.append(operation)
            if self.fail_feedback:
                from script_core.budget import ToolFailure
                raise ToolFailure('Planner failed')
            return {'data': {'gap': 'Inspect the tool',
                'caption_ids': ['TS36'] if inputs['max_caption_additions'] else [],
                'visual_checks': [{'caption_id': 'TS36', 'question': 'Which tool is visible?'}] if inputs['max_visual_checks'] else []}}
        if operation == 'summary_graph_guard':
            self.operations.append(operation)
            if self.fail_guard:
                from script_core.budget import ToolFailure
                raise ToolFailure('Audit failed')
            return {'data': {'reason': 'Additional tool observation', 'claim_ids': [inputs['claims'][0]['id']],
                             'resolves_distinction': self.guard_accept, 'accept': self.guard_accept}}
        return super().call(operation, inputs, timeout)


class CompatibilityTests(unittest.TestCase):
    def setup_inputs(self, folder):
        base, metadata = long_base()
        path = Path(folder)/'captions.json'
        spans = [{'start': i, 'end': i+1, 'text': 'C uses a grater on cheese.' if i == 35 else 'C moves an object.'} for i in range(180)]
        write(path, {'spans': spans})
        base.manifest.update(caption_uri=str(path), caption_hash=file_hash(path))
        metadata['base_hash'] = memory_hash(base)
        query = {'question_id': 'q', 'video_id': 'v', 'question': 'Which tool is used?',
                 'options': ['knife', 'grater', 'hammer', 'saw', 'brush']}
        graph = assemble_graph('v', file_hash(path), source_records(spans, 180), [{'events': [{
            'caption_ids': ['TS36'], 'actor': 'C', 'action': 'uses', 'object': 'cheese',
            'instrument': 'grater', 'location': '', 'hand': ''}]}])
        return base, metadata, query, graph

    def run_case(self, profile, fail_feedback=False):
        with tempfile.TemporaryDirectory() as folder:
            base, metadata, query, graph = self.setup_inputs(folder)
            cfg = profile_config(deepcopy(DEFAULTS), profile)
            backend = CompatBackend(base)
            backend.fail_feedback = fail_feedback
            result = answer_query(base, query, metadata, cfg, backend, Path(folder)/'run',
                summary_graph=graph if cfg['compatibility']['graph_retrieval'] else None)
            self.assertIsNone(result['error'], result['error'])
            self.assertFalse(result['cost_summary']['budget_violation'])
            self.assertTrue((Path(folder)/'run/state.sqlite3').exists())
            self.assertTrue((Path(folder)/'run/answers.jsonl').exists())
            return result, backend

    def test_extensions_off_uses_only_original_tools_in_the_common_entry(self):
        result, backend = self.run_case('c0')
        self.assertEqual(backend.operations, ['videotree_width', 'videotree_depth', 'videotree_qa'])
        self.assertEqual(result['answer']['selected_option'], '0')
        self.assertEqual(result['compatibility']['engine'], 'videotree_in_script')
        self.assertNotIn('parse', backend.operations)
        self.assertNotIn('observe', backend.operations)

    def test_original_prompt_factory_and_none_examples_are_preserved(self):
        query = {'id': 'q', 'question': 'Which?', 'options': ['a', 'b', 'c', 'd', 'e']}
        captions = source_records([{'start': i, 'end': i+1, 'text': f'caption{i}'} for i in range(3)], 3)
        actual = qa_inputs(query, captions, [0, 2])
        from script_core.migration import original_call
        expected = original_call({'question_id': 'q', 'question': 'Which?', 'options': query['options'],
            'captions': [c['text'] for c in captions], 'duration': 3, 'selected_seconds': [0, 2]})
        self.assertEqual(actual['head'], expected['head'])
        self.assertEqual(actual['prompts'], expected['prompts'])
        self.assertIn('Here are a few examples. \nNone ', actual['prompts'][0])

    def test_graph_only_changes_selected_source_seconds(self):
        result, backend = self.run_case('cg')
        self.assertEqual(backend.operations, ['videotree_width', 'videotree_depth', 'videotree_qa', 'videotree_qa'])
        self.assertEqual(result['compatibility']['graph_added_seconds'], [35])
        calls = [i for op,i in backend.seen if op == 'videotree_qa']
        self.assertEqual(calls[0]['head'], calls[1]['head'])
        self.assertNotIn('summary_graph', calls[1])
        self.assertEqual(result['answer']['selected_option'], '1')

    def test_feedback_only_does_not_observe_pixels_or_run_script_qa(self):
        result, backend = self.run_case('cf')
        self.assertIn('videotree_feedback', backend.operations)
        self.assertNotIn('observe', backend.operations)
        self.assertNotIn('answer', backend.operations)
        self.assertEqual(result['compatibility']['final_selected_seconds'], [0, 1, 2, 35])

    def test_failed_feedback_preserves_the_original_answer(self):
        result, backend = self.run_case('cf', fail_feedback=True)
        self.assertEqual(result['answer']['selected_option'], '0')
        self.assertTrue(result['compatibility']['extension_errors'])
        self.assertEqual(backend.operations.count('videotree_qa'), 1)

    def test_visual_module_uses_script_local_executor_without_caption_feedback(self):
        result, backend = self.run_case('cv')
        self.assertIn('observe', backend.operations)
        self.assertIn('verify_claims', backend.operations)
        self.assertNotIn('parse', backend.operations)
        self.assertNotIn('assess', backend.operations)
        self.assertEqual(result['compatibility']['final_selected_seconds'], [0, 1, 2])
        self.assertTrue(result['compatibility']['qa_context_claim_ids'])
        self.assertEqual(result['diagnostics']['reading']['visual_reads'], 1)

    def test_qa_switch_uses_original_order_and_its_own_decoder_only_when_enabled(self):
        result, backend = self.run_case('cqa')
        calls = [i for op,i in backend.seen if op == 'answer']
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['options'], ['knife', 'grater', 'hammer', 'saw', 'brush'])
        self.assertEqual(result['answer']['selected_option'], '0')

    def test_protection_requires_additional_verified_process_not_caption_rewording(self):
        result, backend = self.run_case('cp')
        # Fixture facts are states, so they cannot justify replacing the baseline.
        self.assertEqual(result['answer']['selected_option'], '0')
        self.assertNotIn('summary_graph_guard', backend.operations)
        self.assertFalse(result['compatibility']['accepted_change'])

    def test_graph_addition_is_bounded_and_empty_graph_keeps_selection(self):
        context = {'events': [{'interval': [i, i+1], 'retrieval_score': 1} for i in range(100)]}
        self.assertEqual(len(add_graph_seconds(context, [0, 1], 12)), 14)
        self.assertEqual(add_graph_seconds(None, [0, 1], 12), [0, 1])

    def test_feedback_schema_does_not_allow_selected_or_out_of_scope_retrieval(self):
        inputs = {'captions': [{'id': 'c1'}, {'id': 'c2'}], 'selected_caption_ids': ['c1'],
                  'max_caption_additions': 1, 'max_visual_checks': 0}
        schema = output_schema('videotree_feedback', inputs)
        validate_output({'gap': 'Tool', 'caption_ids': ['c2'], 'visual_checks': []}, schema)
        with self.assertRaises(ValueError): validate_output({'gap': '', 'caption_ids': ['c1'], 'visual_checks': []}, schema)

    def test_component_profiles_have_only_the_intended_differences(self):
        cfg = deepcopy(DEFAULTS)
        self.assertEqual(profile_config(cfg, 'cgf')['compatibility'],
                         dict(profile_config(cfg, 'cg')['compatibility'], retrieval_feedback=True))
        self.assertEqual(profile_config(cfg, 'cqa')['compatibility'],
                         dict(profile_config(cfg, 'cgfv')['compatibility'], final_qa='script'))
        self.assertEqual(profile_config(cfg, 'corder')['compatibility'], profile_config(cfg, 'cqa')['compatibility'])
        self.assertFalse(cfg['compatibility']['enabled'])

    def test_invalid_switch_types_fail(self):
        cfg = deepcopy(DEFAULTS)
        cfg['compatibility']['graph_retrieval'] = 'yes'
        with self.assertRaises(ValueError): validate_config(cfg)

    def test_original_width_and_depth_functions_run_without_model_reimplementation(self):
        import torch
        import adaptive_breath_expansion as original
        with tempfile.TemporaryDirectory() as folder:
            feature = Path(folder)/'v.pt'
            torch.save(torch.eye(6), feature)
            class Model:
                max_new_tokens = 1024
                def set_post_process_fn(self, function): self.fn = function
                def forward(self, head, prompts):
                    n = len(re.findall(r'Frame \d+ \(', prompts[0]))
                    return [3]*n, {'response': 'frame relevance: '+str([3]*n), 'message': [],
                                   'prompt_tokens': 1, 'completion_tokens': 1}
            query = {'id': 'q', 'question': 'Which?', 'options': ['a','b','c','d','e']}
            before_argv, before_model = list(sys.argv), original.get_model
            result = run_width({'query': query, 'captions': ['action']*6, 'duration': 6,
                                'feature_uri': str(feature)}, Model(), device='cpu')
            self.assertEqual(sys.argv, before_argv)
            self.assertIs(original.get_model, before_model)
            depth = run_depth({'feature_uri': str(feature), **result})
            self.assertEqual(depth['selected_seconds'], list(range(6)))

    def test_exact_input_cache_is_scoped_and_feature_changes_invalidate_it(self):
        with tempfile.TemporaryDirectory() as folder:
            feature = Path(folder)/'feature.pt'; feature.write_bytes(b'one')
            cfg = deepcopy(DEFAULTS); cfg['compatibility']['cache_directory'] = str(Path(folder)/'cache')
            backend = SimpleNamespace(cfg=cfg)
            inputs = {'query_id': 'q', 'feature_uri': str(feature)}
            with patch('script_core.videotree_tools.run_depth', return_value={'selected_seconds': [0]}) as depth:
                first = call_original(backend, 'videotree_depth', inputs)
                second = call_original(backend, 'videotree_depth', inputs)
                self.assertEqual(depth.call_count, 1)
                self.assertFalse(first['usage']['tool_cache']['hit'])
                self.assertTrue(second['usage']['tool_cache']['hit'])
                self.assertEqual(second['usage']['input_tokens'], 0)
                feature.write_bytes(b'changed')
                call_original(backend, 'videotree_depth', inputs)
                self.assertEqual(depth.call_count, 2)
                call_original(backend, 'videotree_depth', dict(inputs, query_id='other'))
                self.assertEqual(depth.call_count, 3)

    def test_cached_dependencies_do_not_create_extra_processing_budget(self):
        from script_core.budget import BudgetLedger
        ledger = BudgetLedger(100., answer_reserve=30.)
        output = {'usage': {'tool_cache': {'hit': True, 'key': 'original', 'source_seconds': 20.}}}
        ledger.record('videotree_width', {}, 70., time.monotonic(), output=output)
        self.assertEqual(ledger.dependency_cost, 20.)
        self.assertLessEqual(ledger.remaining, 50.)
        self.assertLessEqual(ledger.allowance(100., final=True), 80.)
        ledger.record('videotree_width', {}, 50., time.monotonic(), output=output)
        self.assertEqual(ledger.dependency_cost, 20.)
        self.assertGreaterEqual(ledger.summary()['budgeted_total'], 20.)

    def test_cached_work_cannot_bypass_an_operation_deadline(self):
        with tempfile.TemporaryDirectory() as folder:
            feature = Path(folder)/'feature.pt'; feature.write_bytes(b'one')
            cfg = deepcopy(DEFAULTS); cfg['compatibility']['cache_directory'] = str(Path(folder)/'cache')
            backend = SimpleNamespace(cfg=cfg)
            inputs = {'query_id': 'q', 'feature_uri': str(feature)}
            with patch('script_core.videotree_tools.run_depth', return_value={'selected_seconds': [0]}):
                call_original(backend, 'videotree_depth', inputs)
                backend.deadline = time.monotonic()-1
                with self.assertRaisesRegex(ValueError, 'standalone allowance'):
                    call_original(backend, 'videotree_depth', inputs)

    def test_full_baseline_gate_uses_per_question_reproduction_not_accuracy(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            expected = {'prediction': '0', 'selected_seconds': [0], 'head': 'h', 'prompts': ['p'], 'prompt_tokens': 1}
            write(root/'cohort.json', ['q'])
            write(root/'evaluation_only.json', {'q': {'videotree': expected}})
            result = {'error': None, 'compatibility': {'baseline_prediction': '0', 'baseline_selected_seconds': [0],
                'baseline_qa': {'head': 'h', 'prompts': ['p']}, 'baseline_prompt_tokens': 1}}
            path = root/'arms/c0/runs'/digest('q')[:20]/'result.json'
            write(path, result); self.assertTrue(gate(root))
            result['compatibility']['baseline_qa']['prompts'] = ['different']
            write(path, result); self.assertFalse(gate(root))

    def test_historical_failed_case_is_explicit_and_does_not_get_a_fake_prediction(self):
        self.assertTrue(baseline_comparison({'error': 'Failed'}, None)['historical_failure'])

    def test_scores_keep_failures_in_denominator_and_pending_accuracy_null(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write(root/'cohort.json', ['q','r'])
            write(root/'evaluation_only.json', {'q': {'truth': '0'}, 'r': {'truth': '0'}})
            write(root/'experiment.json', {'notes': []})
            path = root/'arms/c0/runs'
            write(path/digest('q')[:20]/'result.json', {'answer': {'selected_option': '0'}, 'error': None, 'cost_summary': {'online': 1.}})
            self.assertIsNone(score(root)['groups']['c0']['accuracy'])
            write(path/digest('r')[:20]/'result.json', {'error': 'Failed', 'cost_summary': {'online': 1.}})
            self.assertEqual(score(root)['groups']['c0']['accuracy'], .5)


if __name__ == '__main__': unittest.main()
