"""Check the actual observer boundary, cache identity and uncertainty feedback."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from script_core.budget import BudgetLedger, Meter
from script_core.config import DEFAULTS
from script_core.context import model_view
from script_core.controller import answer_query
from script_core.materialize import Materializer
from script_core.navigation import delivered_feedback, text_feedback
from script_core.query_policy import observation_focus, query_observation_gaps
from script_core.state import obj
from test_script_core import FixtureBackend, fixture_base


class FocusBackend(FixtureBackend):
    def __init__(self, memory):
        super().__init__(memory)
        self.inputs = []

    def call(self, operation, inputs, timeout):
        if operation in ('observe', 'extract', 'verify_claims', 'verify_claim', 'answer', 'gate'):
            self.inputs.append((operation, deepcopy(inputs)))
        output = super().call(operation, inputs, timeout)
        if operation in ('observe', 'extract'):
            output['data']['uncertainties'] = ['Hand transfer is occluded in this interval']
        return output


class ObservationFocusTests(unittest.TestCase):
    def task(self, mode='goal'):
        return obj('task', candidate_id=None, scene_ids=['s0'], allowed_interval=[0., 6.], requirement_ids=['r'],
            target_binding_ids=[], goal='Observe the planting steps', question='How is the seedling planted?',
            options=['Take with the left hand, transfer to the right, then plant.',
                     'Take with the right hand, transfer to the left, then plant.'],
            acceptance_checks=['Record hand identity and transfer order when visible'],
            observation_gaps=[], route='summary', basis_refs=['w0'], relevant_versions={}, max_new_observations=1,
            cost_cap=60., extraction_scope={'predicates': ['other'], 'target_occurrence_ids': [],
            'include_context': True, 'mode': mode}, target_signature='same-target', variables=[])

    def setup_materializer(self, folder):
        base, _ = fixture_base()
        base.put('requirements', obj('r', predicate='other', roles={}, scope=[0., 6.], quantifier='exists',
            prerequisite_ids=[], acceptable_modalities=['visual'], answer_slot=None, support_sets=[], status='unresolved'))
        backend = FocusBackend(base)
        meter = Meter(backend, BudgetLedger(600.), deepcopy(DEFAULTS), Path(folder)/'raw_calls')
        self.addCleanup(meter.close)
        return base, backend, Materializer(base, meter, deepcopy(DEFAULTS), 'q', folder)

    def test_direct_observer_receives_all_alternatives_and_check_targets_but_verifier_does_not(self):
        with tempfile.TemporaryDirectory() as folder:
            base, backend, materializer = self.setup_materializer(folder)
            task = self.task()
            base.put('tasks', task)
            result = materializer.execute(task, [{'interval': [0., 6.]}], interpret=False)
            observed = next(inputs for op, inputs in backend.inputs if op == 'observe')
            view = model_view(observed)
            self.assertEqual(observed['options'], task['options'])
            self.assertEqual(observed['question'], task['question'])
            self.assertEqual(observed['acceptance_checks'], task['acceptance_checks'])
            self.assertEqual([o['option_id'] for o in view['options']], ['0', '1'])
            self.assertTrue(all(row['difference_text'] for row in view['option_distinctions']))
            verifier = next(inputs for op, inputs in backend.inputs if op == 'verify_claims')
            self.assertNotIn('options', verifier)
            self.assertEqual(len(observed['frames']), len(verifier['frames']))
            self.assertEqual(observed['max_claims'], 3)
            self.assertEqual(result['observation_gaps'][0]['interval'], [0., 6.])
            self.assertIn('occluded', result['observation_gaps'][0]['reason'])
            base.validate()

    def test_entity_extractor_receives_same_focus_and_retains_uncertainty(self):
        with tempfile.TemporaryDirectory() as folder:
            base, backend, materializer = self.setup_materializer(folder)
            task = self.task(mode='all')
            base.put('tasks', task)
            result = materializer.execute(task, [{'interval': [0., 6.]}], interpret=False)
            inputs = next(inputs for op, inputs in backend.inputs if op == 'extract')
            self.assertEqual(inputs['options'], task['options'])
            self.assertEqual(inputs['acceptance_checks'], task['acceptance_checks'])
            self.assertTrue(result['observation_gaps'])

    def test_identical_focus_reuses_cache_and_preserves_gap_provenance(self):
        with tempfile.TemporaryDirectory() as folder:
            base, backend, materializer = self.setup_materializer(folder)
            task = self.task()
            base.put('tasks', task)
            first = materializer.execute(task, [{'interval': [0., 6.]}], interpret=False)
            count = backend.operations.count('observe')
            again = materializer.execute(task, [{'interval': [0., 6.]}], interpret=False)
            self.assertEqual(backend.operations.count('observe'), count)
            self.assertTrue(again['cache_hit_ids'])
            self.assertEqual(first['observation_gaps'], again['observation_gaps'])

    def test_different_options_goal_or_checks_cannot_reuse_generic_observation(self):
        for field, value in [('options', ['Use a shovel', 'Use a stick']),
                             ('goal', 'Observe tool identity'),
                             ('acceptance_checks', ['Record the tool shape'])]:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as folder:
                base, backend, materializer = self.setup_materializer(folder)
                task = self.task()
                base.put('tasks', task)
                materializer.execute(task, [{'interval': [0., 6.]}], interpret=False)
                count = backend.operations.count('observe')
                changed = dict(task, **{field: value})
                materializer.execute(changed, [{'interval': [0., 6.]}], interpret=False)
                self.assertEqual(backend.operations.count('observe'), count+1)

    def test_focus_is_label_free_and_open_questions_do_not_get_fake_options(self):
        task = dict(self.task(), truth='1', correct_answer='right hand')
        focused = observation_focus(task)
        self.assertNotIn('truth', focused)
        self.assertNotIn('correct_answer', focused)
        focused['options'].reverse()
        self.assertNotEqual(task['options'], focused['options'])
        del task['options']
        self.assertEqual(observation_focus(task)['options'], [])

    def test_uncertainty_feedback_is_masked_by_existing_ablation(self):
        result = {'task_status': 'partial', 'unresolved': [], 'next_leads': [],
                  'observation_gaps': [{'interval': [0., 6.], 'reason': 'Hand transfer occluded'}]}
        cfg = deepcopy(DEFAULTS)
        self.assertIn('occluded', text_feedback(delivered_feedback(result, cfg)))
        cfg['ablations']['feedback_unresolved'] = False
        masked = delivered_feedback(result, cfg)
        self.assertNotIn('occluded', text_feedback(masked))
        self.assertTrue(result['observation_gaps'])

    def test_controller_delivers_the_previous_window_gap_to_next_observation(self):
        base, metadata = fixture_base()
        backend = FocusBackend(base)
        with tempfile.TemporaryDirectory() as folder:
            result = answer_query(base, {'question_id': 'q', 'video_id': 'v',
                'question': 'What is the overall planting process?',
                'options': self.task()['options']}, metadata, deepcopy(DEFAULTS), backend, folder)
        observed = [inputs for op, inputs in backend.inputs if op == 'observe']
        self.assertIsNone(result['error'])
        self.assertGreaterEqual(len(observed), 2)
        self.assertEqual(observed[0]['prior_observation_gaps'], [])
        self.assertTrue(observed[1]['prior_observation_gaps'])
        self.assertIn('occluded', observed[1]['prior_observation_gaps'][0]['reason'])
        final_inputs = next(inputs for op, inputs in backend.inputs if op == 'answer')
        self.assertTrue(final_inputs['observation_gaps'])
        self.assertTrue(all(0. <= gap['interval'][0] < gap['interval'][1] <= 12.
                            for gap in final_inputs['observation_gaps']))
        self.assertFalse(result['cost_summary']['budget_violation'])

    def test_other_queries_observation_limits_do_not_leak_into_this_query(self):
        base, _ = fixture_base()
        for namespace in ('q', 'other'):
            base.put('build_records', obj(namespace+'-build', query_id=namespace, interval=[0., 6.],
                observation_gaps=[namespace+' gap']))
        gaps = query_observation_gaps(base, 'q')
        self.assertEqual(gaps, [{'interval': [0., 6.], 'reason': 'q gap'}])


if __name__ == '__main__':
    unittest.main()
