"""Behavioral regressions for the observed planning and evidence-flow failures."""
from copy import deepcopy
from contextlib import closing
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import torch
from PIL import Image

from test_script_core import FixtureBackend, fixture_base, fixture_memory
from script_core.backend import LocalModels, deadline_stopping_criteria
from script_core.budget import ModelWorker, ToolFailure, BudgetLedger, Meter
from script_core.materialize import Materializer
from script_core.caption_context import caption_context
from script_core.context import encode_caption_context
from script_core.config import DEFAULTS
from script_core.controller import answer_query
from script_core.query_policy import normalize_plan, plan_uncertainties, strict_question, overview_question
from script_core.state import Store, obj, memory_hash


class PlanningAndEvidenceTests(unittest.TestCase):
    def test_summary_that_rejects_listing_all_actions_is_not_an_exhaustive_query(self):
        for question in ("Summarize the tasks without listing all actions.",
                         "Summarize the steps. Don't list every action narrated.",
                         'What are the key stages and how do they connect to each other?',
                         '总结主要任务，不要列出所有动作。'):
            self.assertFalse(strict_question(question))
            self.assertTrue(overview_question(question))
        for question in ('List all utensils used.', 'Count every opening of the drawer.', '列出所有物体'):
            self.assertTrue(strict_question(question))
    def test_overview_observations_receive_alternatives_without_repeated_goal_generation(self):
        from test_pilot_repairs import long_base
        base, meta = long_base()
        class Backend(FixtureBackend):
            def call(self, operation, inputs, timeout):
                if operation == 'observe':
                    self.observed_options = inputs.get('options')
                return super().call(operation, inputs, timeout)
        backend = Backend(base)
        with tempfile.TemporaryDirectory() as folder:
            result = answer_query(base, {'question_id': 'q', 'video_id': 'v',
                'question': 'Summarize the two primary tasks.', 'options': ['a', 'b']},
                meta, deepcopy(DEFAULTS), backend, folder)
        self.assertIsNone(result['error'])
        self.assertEqual(backend.operations.count('goal'), 0)
        self.assertEqual(backend.observed_options, ['a', 'b'])
        self.assertEqual(result['stop_decision']['reason'], 'protocol_sufficient')
    def test_recorded_missing_observations_are_not_permanent_text_ambiguities(self):
        cases = json.loads((Path(__file__).parent/'fixtures/performance_plans.json').read_text())
        self.assertEqual(len(cases), 21)
        for case in cases:
            blocking, leads = plan_uncertainties(case['plan'], case['question'])
            self.assertEqual(blocking, [])
            self.assertTrue(leads)
        for fragment in ('two primary tasks', 'critical turning points', 'primary collaborative goal', 'vespa scooter'):
            case = next(c for c in cases if fragment in c['question'])
            plan = normalize_plan(case['plan'], case)
            self.assertEqual(len(plan['requirements']), 1)
            self.assertEqual(plan['requirements'][0]['quantifier'], 'global')
            self.assertEqual(plan['requirements'][0]['predicate'], 'other')
            self.assertEqual(plan['requirements'][0]['roles'], {})

    def test_semantic_task_type_handles_new_overview_wording_without_weakening_count(self):
        plan = FixtureBackend(fixture_base()[0]).call('parse', {'scope': [0., 12.]}, 60.)['data']
        plan['task_type'] = 'overview'
        self.assertEqual(normalize_plan(plan, {'question': 'Explain the relationship of these events.', 'scope': [0., 12.]})['requirements'][0]['quantifier'], 'global')
        count_plan = deepcopy(plan)
        count_plan['requirements'][0]['quantifier'] = 'count'
        strict = normalize_plan(count_plan, {'question': 'How many times did the door open?', 'scope': [0., 12.]})
        self.assertEqual(strict['requirements'][0]['quantifier'], 'count')
        attribute = normalize_plan(plan, {'question': 'What is the color of the main fabric?', 'scope': [0., 12.]})
        self.assertEqual(attribute['requirements'][0]['quantifier'], 'exists')

    def test_parse_has_no_options_and_is_not_repeated_for_missing_observations(self):
        base, meta = fixture_base()
        class UncertainBackend(FixtureBackend):
            def call(self, operation, inputs, timeout):
                if operation == 'parse':
                    self.assert_no_options = 'options' not in inputs
                    out = super().call(operation, inputs, timeout)
                    out['data']['evidence_gaps'] = ['Need to inspect the action.']
                    return out
                return super().call(operation, inputs, timeout)
        backend = UncertainBackend(base)
        with tempfile.TemporaryDirectory() as folder:
            result = answer_query(base, {'question_id': 'q', 'video_id': 'v', 'question': 'first', 'options': ['a', 'b']},
                meta, deepcopy(DEFAULTS), backend, folder)
        self.assertIsNone(result['error'])
        self.assertEqual(backend.operations.count('parse'), 1)
        self.assertTrue(backend.assert_no_options)
        self.assertEqual(result['stop_decision']['reason'], 'protocol_sufficient')
        self.assertEqual(result['diagnostics']['evidence_gaps'], ['Need to inspect the action.'])

    def test_real_text_ambiguity_can_be_resolved_by_the_gate_with_fact_citations(self):
        for resolved in (False, True):
            base, meta = fixture_base()
            class AmbiguousBackend(FixtureBackend):
                def call(self, operation, inputs, timeout):
                    out = super().call(operation, inputs, timeout)
                    if operation == 'parse':
                        out['data']['ambiguities'] = [{'kind': 'reference', 'question_span': 'it',
                            'alternatives': ['the bag', 'the box'], 'description': 'Which object is referred to?'}]
                    if operation == 'gate':
                        out['data']['ambiguity_checks'] = [{'ambiguity_index': '0', 'resolved': resolved,
                            'claim_ids': [c['id'] for c in inputs['claims'][:1]], 'reason': 'Observed referent' if resolved else 'Referent unknown'}]
                    return out
            with tempfile.TemporaryDirectory() as folder:
                result = answer_query(base, {'question_id': 'q', 'video_id': 'v', 'question': 'Where is it?', 'options': ['a', 'b']},
                    meta, deepcopy(DEFAULTS), AmbiguousBackend(base), folder)
            self.assertIsNone(result['error'])
            self.assertEqual(result['stop_decision']['evidence_status'], 'sufficient' if resolved else 'insufficient')
            self.assertEqual(bool(result['diagnostics']['resolved_ambiguities']), resolved)

    def test_non_overview_answer_gets_chronological_captions_when_no_claim_survives(self):
        base, meta = fixture_base()
        class NoFacts(FixtureBackend):
            def call(self, operation, inputs, timeout):
                if operation == 'observe':
                    return {'data': {'claims': [], 'uncertainties': []}}
                if operation == 'answer':
                    self.answer_inputs = inputs
                return super().call(operation, inputs, timeout)
        backend = NoFacts(base)
        with tempfile.TemporaryDirectory() as folder:
            result = answer_query(base, {'question_id': 'q', 'video_id': 'v', 'question': 'Which object is used?', 'options': ['a', 'b']},
                meta, deepcopy(DEFAULTS), backend, folder)
        self.assertIsNone(result['error'])
        self.assertEqual(backend.answer_inputs['claims'], [])
        windows = backend.answer_inputs['coarse_context']['windows']
        self.assertTrue(windows)
        self.assertEqual(windows, sorted(windows, key=lambda w: (w['interval'][0], w['id'])))
        self.assertEqual(result['stop_decision']['evidence_status'], 'insufficient')

    def test_caption_selection_does_not_read_beyond_query_scope(self):
        base, _ = fixture_base()
        context = caption_context(base, {'question': 'first', 'options': [], 'scope': [0., 6.]}, DEFAULTS)
        self.assertTrue(context['windows'])
        self.assertTrue(all(w['interval'][1] <= 6. for w in context['windows']))

    def test_exact_context_fitting_omits_whole_captions_without_dropping_verified_facts(self):
        class Processor:
            def __call__(self, text, **kwargs):
                return SimpleNamespace(input_ids=torch.zeros((1, len(text[0])), dtype=torch.long),
                                       get=lambda key: None)
        inputs = {'claims': [{'id': 'fact', 'description': 'verified'}], 'coarse_context': {
            'eligible_windows': 4, 'windows': [{'id': str(i), 'interval': [i, i+1], 'caption': 'x'*40} for i in range(4)]}}
        rendered = []
        def render(candidate):
            self.assertEqual(candidate['claims'], inputs['claims'])
            rendered.append(candidate)
            return 'verified facts' + ''.join(w['caption'] for w in candidate['coarse_context']['windows'])
        encoded, selection = encode_caption_context(Processor(), render, inputs, 100)
        self.assertLessEqual(encoded.input_ids.shape[-1], 100)
        self.assertEqual(selection['requested_windows'], 4)
        self.assertEqual(len(selection['selected_window_ids']), 2)
        self.assertEqual(len(inputs['coarse_context']['windows']), 4)


class WorkerAndCheckpointTests(unittest.TestCase):
    def test_local_observation_leaves_paid_allowance_for_assessment(self):
        base, _ = fixture_base()
        base.put('requirements', obj('r', predicate='other', roles={}, scope=[0., 6.], quantifier='exists',
            prerequisite_ids=[], acceptable_modalities=['visual'], answer_slot='answer', support_sets=[], status='unresolved'))
        task = obj('task', requirement_ids=['r'], goal='question', options=['a', 'b'], variables=[],
            cost_cap=60., allowed_interval=[0., 6.], max_new_observations=1)
        base.put('tasks', task)
        clock = [100.]
        class SimulatedObservation(Materializer):
            def observe(self, task, request):
                ledger = self.meter.ledger
                self.observation_cap = ledger.task_cap
                started = clock[0]
                clock[0] += 40.
                ledger.record('observe', {}, self.observation_cap, started, output={})
                base.put('claims', obj('c', scene_ids=['s0'], predicate='other', roles={}, interval=[0., 3.],
                    observed_at=None, status='supported', source_type='visual', evidence_ids=['ev'], counterevidence_ids=[],
                    support_sets=[['ev']], verification={'result': 'supported', 'unresolved_conflicts': []}, duplicate_group=None))
                record = obj('build', status='complete', interval=[0., 6.])
                base.put('build_records', record)
                return ['ev'], record, []
        with tempfile.TemporaryDirectory() as folder, patch('time.monotonic', side_effect=lambda: clock[0]):
            ledger = BudgetLedger(600.)
            meter = Meter(FixtureBackend(base), ledger, DEFAULTS, folder)
            try:
                materializer = SimulatedObservation(base, meter, DEFAULTS, 'q', folder)
                result = materializer.execute(task, [{'interval': [0., 6.]}])
            finally:
                meter.close()
        self.assertLess(materializer.observation_cap, task['cost_cap'])
        assessment = next(c for c in ledger.calls if c['operation'] == 'assess')
        self.assertEqual(assessment['reservation'], 20.)
        self.assertEqual(result['task_status'], 'resolved')
        self.assertFalse(ledger.budget_violation)
    def test_cooperative_deadline_keeps_worker_and_loaded_model_usable(self):
        worker = ModelWorker(DEFAULTS)
        worker.process = Mock()
        worker.process.is_alive.return_value = True
        worker.connection = Mock()
        worker.connection.poll.return_value = True
        worker.connection.recv.side_effect = [(False, {'message': 'SoftDeadlineExceeded: stopped', 'soft_timeout': True, 'usage': {}}), (True, {'data': 'next call'})]
        worker.vlm_ready = True
        worker.start = Mock()
        worker.close = Mock()
        with self.assertRaises(ToolFailure):
            worker.call('extract', {}, 10.)
        self.assertFalse(worker.requires_vlm_warmup)
        worker.close.assert_not_called()
        self.assertEqual(worker.call('answer', {}, 10.)['data'], 'next call')
        self.assertEqual(len(worker.connection.send.call_args.args[0]), 3)

    def test_generation_stopping_criteria_obeys_deadline(self):
        criteria = deadline_stopping_criteria(100.)
        ids = torch.zeros((1, 2), dtype=torch.long)
        with patch('script_core.backend.time.monotonic', return_value=99.):
            self.assertFalse(criteria(ids, None).item())
        with patch('script_core.backend.time.monotonic', return_value=100.):
            self.assertTrue(criteria(ids, None).item())

    def test_tiny_rgb_detection_crop_uses_explicit_channel_order(self):
        from transformers import CLIPImageProcessor
        class Model:
            device, dtype = 'cpu', torch.float32
            def encode_image(self, pixels):
                self.channels = pixels.shape[1]
                return pixels.mean(dim=(-1, -2))
        backend = LocalModels(DEFAULTS)
        model = Model()
        backend.loaded['visual_encoder'] = model, CLIPImageProcessor()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'narrow.png'
            Image.new('RGB', (89, 1), 'red').save(path)
            result = backend.features([{'path': str(path)}])
        self.assertEqual(model.channels, 3)
        self.assertEqual(len(result), 1)

    def test_incremental_checkpoint_preserves_exports_and_deletes_removed_objects(self):
        base = fixture_memory()
        with tempfile.TemporaryDirectory() as folder, closing(Store(folder)) as store:
            store.checkpoint(base, {'state': {}})
            path = Path(folder)/'evidence.jsonl'
            original_mtime = path.stat().st_mtime_ns
            base.put('derived_claims', obj('c', node_type='query_fact', content={'note': 'checkpoint fixture'},
                support_sets=[['ev']], status='supported'))
            store.checkpoint(base, {'state': {'round': 1}})
            self.assertEqual(path.stat().st_mtime_ns, original_mtime)
            self.assertEqual(memory_hash(store.load()[0]), memory_hash(base))
            del base.tables['derived_claims']['c']
            store.checkpoint(base, {'state': {'round': 2}})
            self.assertEqual((Path(folder)/'derived_claims.jsonl').read_text(), '')
            self.assertEqual(memory_hash(store.load()[0]), memory_hash(base))
