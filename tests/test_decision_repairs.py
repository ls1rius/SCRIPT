"""Regression checks for V5's lost facts, option drift and premature locks."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from test_script_core import FixtureBackend, fixture_base
from script_core.config import DEFAULTS
from script_core.context import decision_inputs, model_view
from script_core.controller import answer_query
from script_core.materialize import Materializer
from script_core.query_policy import checked_choice
from script_core.schemas import output_schema, validate_output


class DecisionRepairTests(unittest.TestCase):
    def test_options_keep_their_exact_ids_beside_text(self):
        inputs = {'options': ['creating art', 'studying'], 'option_ids': ['0', '1']}
        self.assertEqual(model_view(inputs)['options'], [
            {'option_id': '0', 'text': 'creating art'}, {'option_id': '1', 'text': 'studying'}])
        self.assertEqual(inputs['options'], ['creating art', 'studying'])

    def test_claims_with_the_same_frames_keep_distinct_actions(self):
        base, _ = fixture_base()
        evidence = base.get('ev')
        frames = [f['frame_id'] for f in evidence['frame_inputs'][:4]]
        report = {'result': 'supported', 'distinguishing_facts': ['visible'],
                  'unresolved_conflicts': [], 'source_frame_ids': frames, 'process_visible': True}
        task = {'id': 'task', 'scene_ids': ['s0'], 'extraction_scope': {'predicates': ['other']}}
        with tempfile.TemporaryDirectory() as folder:
            materializer = Materializer(base, Mock(), DEFAULTS, 'q', folder)
            records = []
            for text in ('C cuts the cucumber.', 'C mixes ingredients in the bowl.', 'C cuts the cucumber.'):
                candidate = {'predicate': 'other', 'roles': {}, 'interval': [0., 6.],
                             'source_frame_ids': frames, 'description': text}
                records.append(materializer.verify_and_add_claim(task, candidate, evidence, [], deepcopy(report)))
        self.assertNotEqual(records[0]['id'], records[1]['id'])
        self.assertEqual(records[0]['id'], records[2]['id'])
        self.assertEqual(len(base.tables['claims']), 2)
        base.validate()

    def test_batch_checks_are_independent_and_cannot_borrow_other_frames(self):
        inputs = {'frames': [{'frame_id': 'f'}], 'candidates': {'0': {}, '1': {}}}
        schema = output_schema('verify_claims', inputs)
        report = {'result': 'supported', 'distinguishing_facts': ['visible'],
                  'unresolved_conflicts': [], 'source_frame_ids': ['f'], 'process_visible': False}
        output = {'checks': {'0': report, '1': dict(report, result='unresolved')}}
        validate_output(output, schema)
        with self.assertRaisesRegex(ValueError, 'missing fields'):
            validate_output({'checks': {'0': report}}, schema)
        with self.assertRaises(ValueError):
            validate_output({'checks': {'0': report, '1': dict(report, source_frame_ids=['unread'])}}, schema)

    def test_point_times_use_source_pts_and_decoder_rejects_frame_indices(self):
        from lmformatenforcer import JsonSchemaParser
        inputs = {'frames': [{'frame_id': 'f', 'pts': 14.0}, {'frame_id': 'g', 'pts': 14.5}],
                  'interval': [14., 22.]}
        schema = output_schema('observe', inputs)
        point_schema = schema['properties']['claims']['items']['anyOf'][0]['properties']['observed_at']
        for point in (14.0, 14.5, None):
            validate_output(point, point_schema)
            parser = JsonSchemaParser(point_schema)
            for character in json.dumps(point):
                self.assertIn(character, parser.get_allowed_characters())
                parser = parser.add_character(character)
            self.assertTrue(parser.can_end())
        for point in (0, 1, 2):
            with self.assertRaises(ValueError):
                validate_output(point, point_schema)
        self.assertNotIn('0', JsonSchemaParser(point_schema).get_allowed_characters())

    def test_no_evidence_is_not_a_refutation_even_with_valid_citations(self):
        gate = {'selected_option': '0', 'option_checks': [
            {'option_id': '0', 'result': 'supported', 'basis': 'direct_support', 'claim_ids': ['c'], 'reason': 'Visible action'},
            {'option_id': '1', 'result': 'refuted', 'basis': 'not_observed', 'claim_ids': ['c'], 'reason': 'No evidence of preparing a meal'}]}
        self.assertFalse(checked_choice(gate, ['cleaning', 'preparing a meal'], [{'id': 'c'}]))
        gate['option_checks'][1]['basis'] = 'direct_contradiction'
        self.assertFalse(checked_choice(gate, ['cleaning', 'preparing a meal'], [{'id': 'c'}]))
        gate['option_checks'][1]['reason'] = 'Distinct visible object contradicts this action'
        self.assertTrue(checked_choice(gate, ['cleaning', 'preparing a meal'], [{'id': 'c'}]))

    def test_full_timeline_answer_can_correct_gate_and_downgrades_proof(self):
        base, meta = fixture_base()
        class ConflictingBackend(FixtureBackend):
            def call(self, operation, inputs, timeout):
                output = super().call(operation, inputs, timeout)
                if operation == 'answer':
                    self.answer_inputs = inputs
                    output['data']['selected_option'] = '1'
                return output
        backend = ConflictingBackend(base)
        with tempfile.TemporaryDirectory() as folder:
            result = answer_query(base, {'question_id': 'q', 'video_id': 'v',
                'question': 'first', 'options': ['art', 'study']}, meta, deepcopy(DEFAULTS), backend, folder)
            persisted = json.loads((Path(folder)/'stop_decisions.jsonl').read_text().splitlines()[-1])
        self.assertIsNone(result['error'])
        self.assertEqual(result['answer']['selected_option'], '1')
        self.assertEqual(result['stop_decision']['reason'], 'answer_conflict')
        self.assertEqual(result['stop_decision']['evidence_status'], 'insufficient')
        self.assertEqual(persisted['evidence_status'], 'insufficient')
        self.assertEqual(result['diagnostics']['answer_conflict'], {'gate_option': '0', 'answer_option': '1'})
        self.assertNotIn('selected_option', backend.answer_inputs['stop_decision'])

    def test_decision_projection_keeps_all_facts_without_quadratic_histories(self):
        claims = [{'id': f'c{i}', 'predicate': 'other', 'description': f'action {i}',
                   'roles': {}, 'interval': [i, i+1], 'evidence_ids': ['e'], 'status': 'supported',
                   'support_sets': [['e']]*100, 'verification': {'distinguishing_facts': ['repeated']*100}}
                  for i in range(25)]
        inputs = {'claims': claims, 'requirements': [{'id': 'r', 'scope': [0, 25],
                  'quantifier': 'global', 'support_sets': [[c['id'] for c in claims]]*100}]}
        projected = decision_inputs(inputs)
        self.assertEqual([c['description'] for c in projected['claims']], [c['description'] for c in claims])
        self.assertTrue(all(c['evidence_ids'] == ['e'] for c in projected['claims']))
        self.assertEqual(projected['claims'][0]['verification']['distinguishing_facts'], ['repeated'])
        self.assertLess(len(json.dumps(projected)), len(json.dumps(inputs))/5)
        self.assertIn('verification', inputs['claims'][0])


if __name__ == '__main__':
    unittest.main()
