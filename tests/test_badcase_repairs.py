"""Semantic-boundary regressions from the completed matched 300-question run."""
from copy import deepcopy
import itertools
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from script_core.answering import prepare_answer_inputs, restore_answer_order, reconcile_answer
from script_core.caption_context import caption_context
from script_core.config import DEFAULTS
from script_core.controller import answer_query
from script_core.materialize import Materializer
from script_core.schemas import output_schema, validate_output, decoder_schema, restore_empty_arrays
from script_core.selective import normalize_plan
from script_core.state import obj
from test_pilot_repairs import long_base
from test_script_core import FixtureBackend, fixture_base


class BadcaseRepairTests(unittest.TestCase):
    def test_option_permutation_preserves_presented_semantics_and_restores_original_label(self):
        options = ['Rearrange furniture', 'Exercise', 'Make a mess', 'Play a game', 'Clean the floor']
        presentations = set()
        for permuted in itertools.permutations(options):
            inputs = {'question': 'What is the main goal?', 'answer_mode': 'choice',
                      'options': list(permuted), 'option_ids': list('01234'),
                      'stop_decision': {'evidence_status': 'insufficient', 'candidate_option': '0'}}
            original = deepcopy(inputs)
            shown, mapping = prepare_answer_inputs(inputs)
            presentations.add(tuple(shown['options']))
            presented = str(shown['options'].index('Clean the floor'))
            answer = restore_answer_order({'selected_option': presented}, mapping)
            self.assertEqual(inputs['options'][int(answer['selected_option'])], 'Clean the floor')
            self.assertNotIn('candidate_option', shown['stop_decision'])
            self.assertEqual(inputs, original)
        self.assertEqual(len(presentations), 1)

    def test_short_answer_decoder_does_not_require_five_self_confirming_comparisons(self):
        from lmformatenforcer import JsonSchemaParser
        inputs = {'answer_strategy': 'direct', 'answer_mode': 'choice', 'options': ['a', 'b'],
                  'option_ids': ['0', '1'], 'claims': [], 'evidence': []}
        answer = {'text': 'A pen draws the pattern before embroidery.', 'selected_option': '1'}
        schema = output_schema('answer', inputs)
        parser = JsonSchemaParser(decoder_schema(schema))
        for character in json.dumps(answer):
            self.assertIn(character, parser.get_allowed_characters())
            parser = parser.add_character(character)
        self.assertTrue(parser.can_end())
        restore_empty_arrays(answer, schema)
        validate_output(answer, schema)
        self.assertEqual(reconcile_answer(answer, inputs)['selected_option'], '1')
        self.assertNotIn('option_alignment', schema['properties'])

    def test_open_answer_with_optional_choices_has_no_label_mapping(self):
        shown, mapping = prepare_answer_inputs({'answer_mode': 'open', 'question': 'Describe the action',
            'options': ['a', 'b'], 'option_ids': ['0', '1']})
        self.assertEqual(mapping, [])
        self.assertIsNone(restore_answer_order({'selected_option': None}, mapping)['selected_option'])

    def test_empty_remaining_read_budget_cannot_schedule_an_inspection(self):
        plan = {'reason': 'Check tools', 'checks': [{'window_id': 'w', 'goal': 'Inspect', 'reason': 'Tools'}]}
        normalized = normalize_plan(plan, {'max_reads': 1, 'remaining_reads': 0,
            'coarse_context': {'windows': [{'id': 'w', 'interval': [0., 8.]}]}})
        self.assertEqual(normalized['checks'], [])

    def test_model_purpose_and_reason_never_become_observation_acceptance_conditions(self):
        base, metadata = long_base()
        class BiasedPlanner(FixtureBackend):
            seen = []
            def call(self, operation, inputs, timeout):
                if operation == 'read_plan':
                    if inputs['inspected_windows']:
                        return {'data': {'reason': 'Nothing else to inspect', 'checks': []}}
                    return {'data': {'reason': 'It must be pizza', 'checks': [{
                        'window_id': inputs['coarse_context']['windows'][0]['id'],
                        'goal': 'To create a delicious pizza from scratch', 'reason': 'Pizza is the goal'}]}}
                if operation == 'observe':
                    self.seen.append(deepcopy(inputs))
                return super().call(operation, inputs, timeout)
        backend = BiasedPlanner(base)
        cfg = deepcopy(DEFAULTS)
        cfg['query']['reading_policy'] = 'script_guided'
        with tempfile.TemporaryDirectory() as folder:
            result = answer_query(base, {'question_id': 'q', 'video_id': 'v',
                'question': 'What is the overall purpose?', 'options': ['Make pizza', 'Bake bread']},
                metadata, cfg, backend, folder)
        self.assertIsNone(result['error'])
        seen = backend.seen[0]
        self.assertNotIn('pizza', seen['goal'].lower())
        self.assertNotIn('Pizza is the goal', seen['acceptance_checks'])
        self.assertEqual(seen['options'], ['Make pizza', 'Bake bread'])
        self.assertIn('pizza', result['diagnostics']['reading']['plan']['checks'][0]['proposed_goal'])

    def test_replan_receives_failed_verification_and_moves_to_later_window(self):
        base, metadata = long_base()
        class FeedbackPlanner(FixtureBackend):
            plans = []
            def call(self, operation, inputs, timeout):
                if operation == 'read_plan':
                    self.plans.append(deepcopy(inputs))
                    inspected = inputs['inspected_windows']
                    chosen = next(w for w in inputs['coarse_context']['windows']
                                  if w['id'] == ('w0' if not inspected else 'w20')) if len(inspected) < 2 else None
                    return {'data': {'reason': 'Locate the missing tool in another stage', 'checks':
                        [{'window_id': chosen['id'], 'goal': 'Inspect tool', 'reason': 'Resolve tool identity'}]
                        if chosen else []}}
                if operation == 'observe':
                    result = super().call(operation, inputs, timeout)
                    result['data']['uncertainties'] = ['The tool is obscured'] if inputs['interval'][0] == 0 else []
                    return result
                result = super().call(operation, inputs, timeout)
                if operation == 'verify_claims' and len(self.plans) == 1:
                    for check in result['data']['checks'].values():
                        check.update(result='unresolved', whole_claim_supported=False)
                return result
        backend = FeedbackPlanner(base)
        cfg = deepcopy(DEFAULTS)
        cfg['query']['reading_policy'] = 'script_guided'
        with tempfile.TemporaryDirectory() as folder:
            result = answer_query(base, {'question_id': 'q', 'video_id': 'v',
                'question': 'What tools are used throughout the video?', 'options': ['Pliers', 'Wrench']},
                metadata, cfg, backend, folder)
            steps = [json.loads(line) for line in (Path(folder)/'steps.jsonl').read_text().splitlines()]
        self.assertIsNone(result['error'])
        self.assertEqual([s['interval'][0] for s in steps], [0., 140.])
        self.assertEqual(backend.plans[1]['verified_claims'], [])
        self.assertEqual(backend.plans[1]['observation_gaps'][0]['reason'], 'The tool is obscured')
        self.assertNotIn('w0', [w['id'] for w in backend.plans[1]['coarse_context']['windows']])
        self.assertTrue(backend.plans[2]['verified_claims'])
        self.assertEqual(result['stop_decision']['evidence_status'], 'insufficient')
        self.assertFalse(result['cost_summary']['budget_violation'])

    def test_both_reading_policies_keep_the_same_full_final_caption_context(self):
        base, _ = long_base()
        base.put('requirements', obj('r', predicate='other', roles={}, scope=[0., 180.], quantifier='global',
            prerequisite_ids=[], acceptable_modalities=['visual'], answer_slot='activity', support_sets=[], status='unresolved'))
        query = {'question': 'What is the overall activity?', 'options': ['a', 'b'], 'scope': [0., 180.]}
        cfg = deepcopy(DEFAULTS)
        original = caption_context(base, query, cfg)
        cfg['query']['reading_policy'] = 'script_guided'
        guided = caption_context(base, query, cfg)
        self.assertEqual(original['windows'], guided['windows'])
        self.assertEqual(guided['omitted_windows'], 0)

    def test_partial_compound_support_cannot_create_a_supported_claim(self):
        base, _ = fixture_base()
        evidence = base.get('ev')
        frames = [f['frame_id'] for f in evidence['frame_inputs'][:4]]
        candidate = {'predicate': 'other', 'roles': {}, 'interval': [0., 6.], 'observed_at': None,
                     'source_type': 'visual', 'source_frame_ids': frames, 'fact_type': 'action',
                     'description': 'Right hand picks up, passes left, then right hand plants.'}
        report = {'result': 'supported', 'whole_claim_supported': False,
                  'distinguishing_facts': ['Right hand picks up and passes to the left'],
                  'unresolved_conflicts': [], 'source_frame_ids': frames, 'process_visible': True}
        with tempfile.TemporaryDirectory() as folder:
            materializer = Materializer(base, Mock(), deepcopy(DEFAULTS), 'q', folder)
            claim = materializer.verify_and_add_claim({'id': 'task', 'scene_ids': ['s0'],
                'extraction_scope': {'predicates': ['other']}}, candidate, evidence, [], report)
        self.assertEqual(claim['status'], 'unresolved')
        self.assertFalse(claim['verification']['whole_claim_supported'])
        self.assertFalse(base.is_valid(claim['id']))


if __name__ == '__main__':
    unittest.main()
