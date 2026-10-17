"""Regressions for observed pilot failures; these fixtures do not measure accuracy."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import torch
from PIL import Image

from test_script_core import fixture_base, fixture_memory, FixtureBackend
from script_core.accounting import ACTIVE
from script_core.budget import BudgetLedger, Meter, ToolFailure
from script_core.config import DEFAULTS, base_config, digest
from script_core.context import model_view, encode_with_frame_budget, assessment_inputs
from script_core.controller import answer_query, structural_gate
from script_core.evaluation import evaluate
from script_core.materialize import Materializer
from script_core.query_policy import normalize_plan, overview_question, checked_choice
from script_core.segmentation import windows
from script_core.state import obj, memory_hash, Store


def long_base():
    memory = fixture_memory(180.)
    ws = windows([0., 180.])
    ids = [f'w{i}' for i in range(len(ws))]
    memory.put('scenes', obj('s', video_id='v', interval=[0., 180.], start_frame=0,
        end_frame_exclusive=360, prev_scene_id=None, next_scene_id=None, window_ids=ids,
        occurrence_ids=[], claim_ids=[], summary_id='summary', boundary_config_hash='cfg'))
    memory.put('summaries', obj('summary', scene_id='s', text='coarse overview', place=None,
        entity_mentions=[], event_keywords=[], claim_ids=[], evidence_ids=['ev'], all_window_ids=ids,
        uncertainties=[], source_revision_hash='hash', status='current'))
    for wid, interval in zip(ids, ws):
        memory.put('windows', obj(wid, scene_id='s', interval=interval, frame_ids=[], caption='activity',
            evidence_ids=['ev'], coverage_ids=[], mention_ids=[], caption_status='ok'))
    docs = [{'id': 's', 'interval': [0., 180.], 'text': 'activity'}]
    meta = {'base_hash': memory_hash(memory), 'base_config_hash': digest(base_config(DEFAULTS)),
        'initialization_cost': .1, 'index': {'documents': docs, 'vectors': [[1., 0.]],
            'window_documents': [{'id': wid, 'interval': interval, 'text': 'activity'} for wid, interval in zip(ids, ws)],
            'window_vectors': [[1., 0.] for _ in ws]}}
    return memory, meta


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


class ScopeAndEvidenceTests(unittest.TestCase):
    def test_invalid_gate_uses_remaining_rounds_instead_of_stopping(self):
        base, meta = fixture_base()
        class BrokenGateOnce(FixtureBackend):
            gates = 0
            def call(self, operation, inputs, timeout):
                if operation == 'gate':
                    self.gates += 1
                    if self.gates == 1: raise ToolFailure('Invalid gate JSON: truncated response')
                return super().call(operation, inputs, timeout)
        with tempfile.TemporaryDirectory() as folder:
            result = answer_query(base, {'question_id': 'q', 'video_id': 'v', 'question': 'first',
                'options': ['a', 'b']}, meta, deepcopy(DEFAULTS), BrokenGateOnce(base), folder)
            self.assertEqual(len(lines(Path(folder)/'steps.jsonl')), 2)
        self.assertIsNone(result['error'])
        self.assertEqual(result['stop_decision']['reason'], 'protocol_sufficient')
        self.assertEqual(len(result['diagnostics']['gate_failures']), 1)

    def test_bad_local_goal_is_bounded_and_does_not_abort_the_next_candidate(self):
        base, meta = fixture_base()
        class BrokenGoalOnce(FixtureBackend):
            goals = 0
            def call(self, operation, inputs, timeout):
                if operation == 'goal':
                    self.goals += 1
                    if self.goals == 1: raise ToolFailure('Invalid goal JSON')
                return super().call(operation, inputs, timeout)
        with tempfile.TemporaryDirectory() as folder:
            result = answer_query(base, {'question_id': 'q', 'video_id': 'v', 'question': 'first',
                'options': ['a', 'b']}, meta, deepcopy(DEFAULTS), BrokenGoalOnce(base), folder)
            steps = lines(Path(folder)/'steps.jsonl')
        self.assertIsNone(result['error'])
        self.assertEqual(result['stop_decision']['reason'], 'protocol_sufficient')
        self.assertEqual([s['task_status'] for s in steps], ['blocked', 'resolved'])

    def test_global_language_keeps_explicit_exhaustive_and_terminal_requirements(self):
        for question in ('What is the main objective throughout the video?',
                         'Identify a recurring pattern and its significance.', '视频整体主要在做什么？'):
            self.assertTrue(overview_question(question))
        for question in ('How many times does the main actor open the drawer?',
                         'Where is the last object placed?', 'List every object throughout the video.',
                         '列出所有物体', 'What color is the main fabric?', 'List all utensils throughout the video.'):
            self.assertFalse(overview_question(question))

    def test_overview_parse_cannot_be_reduced_to_one_occurrence(self):
        report = {'requirements': [{'key': 'duplicate'}, {'key': 'duplicate'}],
                  'variables': [{'name': 'actor'}], 'ambiguities': []}
        normalized = normalize_plan(report, {'question': 'Identify a recurring pattern', 'scope': [0., 180.]})
        self.assertEqual(normalized['requirements'][0]['quantifier'], 'global')
        self.assertEqual(normalized['requirements'][0]['roles'], {})
        self.assertFalse(normalized['variables'])
        self.assertEqual(len(report['requirements']), 2)

    def test_temporal_pattern_needs_facts_from_distinct_intervals(self):
        base, _ = fixture_base()
        base.get('ev')['online'] = True
        for cid, interval in [('c1', [0., 2.]), ('c2', [7., 9.])]:
            base.put('claims', obj(cid, scene_ids=['s0'], predicate='other', roles={}, interval=interval,
                observed_at=None, status='supported', source_type='visual', evidence_ids=['ev'], counterevidence_ids=[],
                support_sets=[['ev']], verification={'result': 'supported'}, duplicate_group=None))
        base.put('requirements', obj('r', predicate='other', roles={}, scope=[0., 12.], quantifier='global',
            prerequisite_ids=[], acceptable_modalities=['visual'], answer_slot='temporal_pattern',
            support_sets=[['c1']], status='satisfied'))
        passed, gaps = structural_gate(base, {'scope': [0., 12.]}, [], [], DEFAULTS)
        self.assertFalse(passed)
        self.assertIn('temporal_comparison:r', gaps)
        base.update('requirements', 'r', support_sets=[['c1', 'c2']])
        self.assertTrue(structural_gate(base, {'scope': [0., 12.]}, [], [], DEFAULTS)[0])

    def test_choice_proof_requires_unique_option_and_valid_citations(self):
        gate = {'selected_option': '0', 'option_checks': [
            {'option_id': str(i), 'result': 'supported' if i == 0 else 'refuted',
             'basis': 'direct_support' if i == 0 else 'direct_contradiction', 'claim_ids': ['c'], 'reason': 'observed distinction'}
            for i in range(2)]}
        self.assertTrue(checked_choice(gate, ['a', 'b'], [{'id': 'c'}]))
        gate['option_checks'][1]['claim_ids'] = ['unseen']
        self.assertFalse(checked_choice(gate, ['a', 'b'], [{'id': 'c'}]))

    def test_overview_does_not_replace_a_speech_requirement_with_visual_evidence(self):
        report = {'requirements': [{'key': 'r', 'predicate': 'speech_claim', 'acceptable_modalities': ['subtitle']}],
                  'variables': [], 'ambiguities': []}
        self.assertEqual(normalize_plan(report, {'question': 'Summarize the overall speech', 'scope': [0., 12.]}), report)

    def test_overview_reaches_whole_timeline_within_existing_eight_rounds(self):
        base, meta = long_base()
        cfg = deepcopy(DEFAULTS)
        with tempfile.TemporaryDirectory() as folder:
            result = answer_query(base, {'question_id': 'q', 'video_id': 'v',
                'question': 'Describe the primary activity throughout the video', 'options': ['a', 'b']},
                meta, cfg, FixtureBackend(base), folder)
            steps = lines(Path(folder)/'steps.jsonl')
        self.assertIsNone(result['error'])
        self.assertEqual(result['stop_decision']['reason'], 'protocol_sufficient')
        self.assertEqual(len(steps), 6)
        self.assertLess(steps[0]['interval'][0], 30.)
        self.assertTrue(60. <= steps[1]['interval'][0] <= 120.)
        self.assertGreater(steps[2]['interval'][0], 150.)
        self.assertEqual(cfg['query']['global_task_limit'], 8)
        self.assertEqual(cfg['models'], DEFAULTS['models'])

    def test_remaining_rounds_recover_a_missing_temporal_fact_after_overview(self):
        base, meta = long_base()
        class SparseFacts(FixtureBackend):
            reads = 0
            def call(self, operation, inputs, timeout):
                out = super().call(operation, inputs, timeout)
                if operation == 'observe':
                    self.reads += 1
                    if self.reads not in (1, 7): out['data']['claims'] = []
                return out
        with tempfile.TemporaryDirectory() as folder:
            result = answer_query(base, {'question_id': 'q', 'video_id': 'v',
                'question': 'Identify a recurring pattern throughout the video', 'options': ['a', 'b']},
                meta, deepcopy(DEFAULTS), SparseFacts(base), folder)
            steps = lines(Path(folder)/'steps.jsonl')
        self.assertIsNone(result['error'])
        self.assertEqual(result['stop_decision']['evidence_status'], 'sufficient')
        self.assertEqual(len(steps), 7)

    def test_exhaustive_inspection_spreads_without_fabricating_full_coverage(self):
        base, meta = long_base()
        class Exhaustive(FixtureBackend):
            def call(self, operation, inputs, timeout):
                out = super().call(operation, inputs, timeout) if operation != 'coverage' else {'data': {
                    'visibility': 'unknown', 'result': 'unknown', 'min_target_short_side': None,
                    'task_verified': False, 'distinguishing_facts': [], 'source_frame_ids': [], 'unresolved_conflicts': []}}
                if operation == 'parse': out['data']['requirements'][0]['quantifier'] = 'all'
                return out
        with tempfile.TemporaryDirectory() as folder:
            result = answer_query(base, {'question_id': 'q', 'video_id': 'v', 'question': 'List every object',
                                        'options': ['a', 'b']}, meta, deepcopy(DEFAULTS), Exhaustive(base), folder)
            steps = lines(Path(folder)/'steps.jsonl')
        self.assertEqual(len(steps), 8)
        self.assertGreater(steps[2]['interval'][0], 150.)
        self.assertEqual(result['stop_decision']['evidence_status'], 'insufficient')

    def test_local_facts_reach_answerer_when_requirement_is_unresolved(self):
        base, meta = fixture_base()
        class Partial(FixtureBackend):
            answer_inputs = None
            def call(self, operation, inputs, timeout):
                out = super().call(operation, inputs, timeout)
                if operation == 'assess': out['data']['requirement_updates'] = []
                if operation == 'answer': self.answer_inputs = deepcopy(inputs)
                return out
        backend, cfg = Partial(base), deepcopy(DEFAULTS)
        cfg['query']['global_task_limit'] = 1
        with tempfile.TemporaryDirectory() as folder:
            result = answer_query(base, {'question_id': 'q', 'video_id': 'v', 'question': 'first',
                'options': ['a', 'b']}, meta, cfg, backend, folder)
        self.assertEqual(result['stop_decision']['evidence_status'], 'insufficient')
        self.assertEqual(len(backend.answer_inputs['claims']), 1)
        self.assertTrue(backend.answer_inputs['evidence'])
        self.assertEqual(backend.answer_inputs['requirements'][0]['status'], 'unresolved')

    def test_passed_flag_cannot_hide_an_unresolved_alternative(self):
        base, meta = fixture_base()
        class Ambiguous(FixtureBackend):
            def call(self, operation, inputs, timeout):
                out = super().call(operation, inputs, timeout)
                if operation == 'gate': out['data']['option_checks'][str(len(inputs['options'])-1)]['result'] = 'unresolved'
                return out
        with tempfile.TemporaryDirectory() as folder:
            result = answer_query(base, {'question_id': 'q', 'video_id': 'v', 'question': 'first',
                'options': ['a', 'b']}, meta, deepcopy(DEFAULTS), Ambiguous(base), folder)
            self.assertGreater(len(lines(Path(folder)/'steps.jsonl')), 1)
        self.assertIsNone(result['error'])
        self.assertEqual(result['stop_decision']['evidence_status'], 'insufficient')


class StateAndBatchTests(unittest.TestCase):
    def test_supported_role_outside_claim_interval_stays_unresolved(self):
        base, _ = fixture_base()
        for oid in ('actor', 'object'):
            base.put('occurrences', obj(oid, scene_id='s0', category='object', aliases=[], visible_intervals=[[0., .5]],
                boxes=[{'frame_id': 'v:F0', 'pts': 0., 'bbox': [.1, .1, .3, .3], 'detection_score': .9}],
                crop_refs=[], appearance_feature_ref='f', description='', evidence_ids=['ev'], entity_candidate_ids=[], stable_track=False))
        meter = Mock()
        meter.json.return_value = ({'result': 'supported', 'distinguishing_facts': ['fixture'], 'unresolved_conflicts': [],
            'source_frame_ids': ['v:F12'], 'process_visible': False}, {})
        with tempfile.TemporaryDirectory() as folder:
            claim = Materializer(base, meter, DEFAULTS, 'q', folder).verify_and_add_claim(
                {'id': 'task', 'scene_ids': ['s1'], 'extraction_scope': {'predicates': ['hold']}},
                {'predicate': 'hold', 'roles': {'actor': 'actor', 'object': 'object'}, 'interval': [6., 8.],
                 'source_frame_ids': ['v:F12'], 'description': 'Claim with an unobserved role'},
                base.get('ev'), list(base.tables['occurrences'].values()))
        self.assertEqual(claim['status'], 'unresolved')
        base.validate()

    def test_checkpoint_failure_releases_writer_and_meter(self):
        base, meta = fixture_base()
        previous = ACTIVE.get()
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(Store, 'checkpoint', side_effect=ValueError('fixture invariant failure')):
                result = answer_query(base, {'question_id': 'q', 'video_id': 'v', 'question': 'first',
                    'options': ['a', 'b']}, meta, deepcopy(DEFAULTS), FixtureBackend(base), folder)
            self.assertEqual(result['error']['code'], 'checkpoint_failed')
            self.assertTrue((Path(folder)/'result.json').exists())
            store = Store(folder)
            store.close()
        self.assertIs(ACTIVE.get(), previous)

    def test_cli_continues_after_one_unexpected_query_error(self):
        import script_qa
        base, meta = fixture_base()
        cfg, backend = deepcopy(DEFAULTS), FixtureBackend(base)
        backend.close = Mock()
        real_answer = answer_query
        def run(*args, **kwargs):
            if args[1]['question_id'] == 'bad': raise ValueError('Claim role was not localized in its actual inputs')
            return real_answer(*args, **kwargs)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            questions = root/'questions.jsonl'
            questions.write_text(''.join(json.dumps({'question_id': q, 'video_id': 'v', 'question': 'first',
                'options': ['a', 'b']})+'\n' for q in ('bad', 'good')))
            with patch.object(script_qa, 'preflight', return_value=(cfg, {})), \
                 patch.object(script_qa, 'load_base', return_value=(base, meta)), \
                 patch.object(script_qa, 'ModelWorker', return_value=backend), \
                 patch.object(script_qa, 'answer_query', side_effect=run):
                status = script_qa.main(['query', '--base', str(root/'base'), '--questions', str(questions), '--out', str(root/'runs')])
            self.assertEqual(status, 1)
            self.assertTrue((root/'runs'/script_qa.safe_directory_id('good')/'result.json').exists())
            self.assertTrue((root/'runs'/'query_failures'/(script_qa.safe_directory_id('bad')+'.json')).exists())
            self.assertEqual(len(json.loads((root/'runs'/'sequence_cost.json').read_text())['questions']), 2)
            labels = root/'labels.json'
            labels.write_text(json.dumps({'questions': [{'question_id': q, 'selected_option': '0'} for q in ('bad', 'good')]}))
            metrics = evaluate(root/'runs', labels, root/'metrics')
            self.assertEqual(len(metrics['groups']), 1)
            group = next(iter(metrics['groups'].values()))
            self.assertEqual(group['questions'], 2)
            self.assertEqual(group['accuracy'], .5)

    def test_pilot_score_counts_recorded_query_failure_as_completed_and_wrong(self):
        from test_pilot_comparison import pilot
        from script_qa import safe_directory_id
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pilot.write(root/'pilot_spec.json', {'shards': 1, 'comparison': 'fixture',
                'input_difference': 'fixture', 'cost_limitations': []})
            pilot.write(root/'evaluation_only.json', {'questions': [{'question_id': 'q', 'selected_option': '0'}]})
            shard = root/'shards'/'0'
            shard.mkdir(parents=True)
            (shard/'questions.jsonl').write_text(json.dumps({'question_id': 'q'})+'\n')
            pilot.write(shard/'script_runs'/'query_failures'/(safe_directory_id('q')+'.json'),
                {'error': {'code': 'query_failed'}, 'cost_summary': {'initialization': 0., 'online': 0.}})
            pilot.write(shard/'videotree_runs'/'q'/'failure.json', {'error': 'fixture failure'})
            result = pilot.score(root)
        self.assertTrue(result['complete'])
        self.assertEqual(result['groups']['SCRIPT']['final_accuracy'], 0.)
        self.assertEqual(result['groups']['SCRIPT']['errors'], 1)


class ContextBudgetTests(unittest.TestCase):
    def test_gate_and_goal_explanations_are_bounded_without_raising_token_limit(self):
        from script_core.schemas import output_schema, validate_output
        inputs = {'options': ['a', 'b'], 'option_ids': ['0', '1'], 'claims': [{'id': 'c'}]}
        schema = output_schema('gate', inputs)
        report = {'passed': False, 'unresolved': [], 'selected_option': None, 'option_checks': {
            '0': {'result': 'unresolved', 'basis': 'uncertain', 'claim_ids': ['c'], 'reason': 'x'*65},
            '1': {'result': 'unresolved', 'basis': 'uncertain', 'claim_ids': ['c'], 'reason': 'unknown'}}}
        with self.assertRaisesRegex(ValueError, 'string length'):
            validate_output(report, schema)
        report['option_checks']['0']['reason'] = 'x'*64
        validate_output(report, schema)
        with self.assertRaisesRegex(ValueError, 'string length'):
            validate_output({'goal': 'x'*161, 'acceptance_checks': []}, output_schema('goal', {}))
        self.assertEqual(DEFAULTS['models']['generation']['max_new_tokens'], 1024)

    def test_cold_preparation_is_separately_charged_to_same_budget(self):
        class Backend:
            requires_vlm_warmup = True
            def call(self, operation, inputs, timeout):
                if operation == 'prepare_vlm': self.requires_vlm_warmup = False
                return {'ready': True} if operation == 'prepare_vlm' else {'data': {}}
        with tempfile.TemporaryDirectory() as folder:
            ledger = BudgetLedger(600.)
            meter = Meter(Backend(), ledger, DEFAULTS, folder)
            try: meter.call('parse', {})
            finally: meter.close()
        self.assertEqual([c['operation'] for c in ledger.calls], ['prepare_vlm', 'parse'])
        self.assertEqual(ledger.total_cap, 600.)
        self.assertLess(ledger.actual, 600.)

    def test_timeout_leaves_paid_time_for_worker_shutdown_and_logging(self):
        clock = [100.]
        class Backend:
            def call(self, operation, inputs, timeout):
                clock[0] += timeout+.04
                raise TimeoutError('fixture cancelled with shutdown overhead')
        cfg = deepcopy(DEFAULTS)
        cfg['budget']['tool_call_cap'] = 2.
        cfg['query']['tool_transient_retries'] = 0
        with tempfile.TemporaryDirectory() as folder, patch('script_core.budget.time.monotonic', side_effect=lambda: clock[0]):
            ledger = BudgetLedger(600.)
            meter = Meter(Backend(), ledger, cfg, folder)
            try:
                with self.assertRaises(ToolFailure): meter.call('parse', {})
            finally: meter.close()
        self.assertLess(ledger.calls[0]['actual_cost'], ledger.calls[0]['reservation'])
        self.assertFalse(ledger.budget_violation)

    def test_assessment_keeps_all_claims_and_only_their_roles_and_existing_bindings(self):
        inputs = {'claims': [{'id': 'c', 'roles': {'object': 'target'}}],
                  'bindings': [{'anchor_occurrence_ids': ['bound'], 'entity_id': 'E'}],
                  'occurrences': [{'id': oid} for oid in ('target', 'bound', 'irrelevant')],
                  'identity_links': [{'occurrence_id': 'bound', 'entity_id': 'E'},
                                     {'occurrence_id': 'irrelevant', 'entity_id': 'other'}],
                  'entities': [{'id': 'E'}, {'id': 'other'}]}
        selected = assessment_inputs(inputs)
        self.assertEqual(selected['claims'], inputs['claims'])
        self.assertEqual([o['id'] for o in selected['occurrences']], ['target', 'bound'])
        self.assertEqual(selected['entities'], [{'id': 'E'}])
        self.assertEqual(selected['context_selection']['unreferenced_occurrences_omitted'], 1)
        self.assertEqual(assessment_inputs(selected), selected)

    def test_projection_keeps_fact_text_and_every_visible_occurrence_id(self):
        inputs = {'frames': [{'frame_id': 'F0', 'pts': 0.}], 'claims': [{'id': 'c', 'description': 'critical action',
            'roles': {'object': 'o'}, 'interval': [0., 1.], 'verification': {'distinguishing_facts': ['unique detail'], 'model_hash': 'huge'}}],
            'occurrences': [{'id': 'o', 'category': 'bag', 'description': 'red marker',
                'boxes': [{'frame_id': 'F0', 'bbox': [0., 0., 1., 1.]}], 'appearance_feature_ref': [1.]*10000}]}
        view = model_view(inputs)
        self.assertEqual(view['claims'][0]['description'], 'critical action')
        self.assertEqual(view['claims'][0]['verification']['distinguishing_facts'], ['unique detail'])
        self.assertEqual(view['occurrences'][0]['visible_frame_ids'], ['F0'])
        self.assertNotIn('appearance_feature_ref', view['occurrences'][0])
        self.assertIn('boxes', inputs['occurrences'][0])

    def test_exact_pixel_budget_preserves_all_frames_and_reports_actual_grids(self):
        class Encoding(dict):
            def __getattr__(self, key): return self[key]
        class Processor:
            image_processor = SimpleNamespace(merge_size=2, patch_size=14)
            def __init__(self): self.batch_counts = []
            def __call__(self, text, images, **kwargs):
                self.batch_counts.append(len(images))
                grids = torch.tensor([[1, im.height//14, im.width//14] for im in images])
                tokens = len(text[0])+sum(int(g.prod())//4 for g in grids)
                return Encoding(input_ids=torch.zeros((1, tokens), dtype=torch.long), image_grid_thw=grids)
        processor = Processor()
        images = [Image.new('RGB', (784, 448)) for _ in range(16)]
        encoded, views = encode_with_frame_budget(processor, 'complete question', images, 4000)
        self.assertLessEqual(encoded.input_ids.shape[-1], 4000)
        self.assertGreater(len(processor.batch_counts), 1)
        self.assertEqual(set(processor.batch_counts), {16})
        self.assertEqual(len(views), 16)
        self.assertLess(views[0]['width'], images[0].width)
        with self.assertRaisesRegex(ValueError, 'text records'):
            encode_with_frame_budget(processor, 'x'*5000, images, 4000)

    def test_long_assessment_batches_preserve_all_claims(self):
        class Backend:
            def call(self, operation, inputs, timeout):
                if len(inputs['claims']) > 2: raise ValueError('Context overflow; text records')
                return {'data': {'bindings': [], 'requirement_updates': [{'requirement_id': 'r', 'result': 'supported',
                    'claim_ids': [c['id'] for c in inputs['claims']], 'binding_variables': []}],
                    'candidate_discriminations': [], 'next_leads': [], 'identity_corrections': [], 'identity_operations': [], 'unresolved': []}}
        with tempfile.TemporaryDirectory() as folder:
            meter = Meter(Backend(), BudgetLedger(600.), DEFAULTS, folder)
            try:
                report, _ = meter.json('assess', {'claims': [{'id': f'c{i}'} for i in range(8)],
                    'requirements': [{'id': 'r'}]}, ('bindings', 'requirement_updates', 'candidate_discriminations', 'next_leads', 'unresolved'))
            finally: meter.close()
        self.assertEqual({c for u in report['requirement_updates'] for c in u['claim_ids']}, {f'c{i}' for i in range(8)})

    def test_replay_preserves_model_view_sizes_without_original_gpu_cost(self):
        from script_core.replay import ReplayBackend
        class ViewsBackend(FixtureBackend):
            def call(self, operation, inputs, timeout):
                out = super().call(operation, inputs, timeout)
                out['usage'] = {'model_frame_views': [{'frame_id': 'v:F0', 'width': 112, 'height': 56}],
                                'context_projection': 'facts_and_all_frames_v1'}
                return out
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            meter = Meter(ViewsBackend(fixture_memory()), BudgetLedger(600., calls_path=root/'calls.jsonl'), DEFAULTS, root/'raw_calls')
            frames = fixture_memory().get('ev')['frame_inputs'][:2]
            inputs = {'frames': frames, 'claim': {'roles': {}}, 'occurrences': []}
            try: meter.call('verify_claim', inputs)
            finally: meter.close()
            out = ReplayBackend(Mock(), root).call('verify_claim', inputs, 30.)
        self.assertEqual(out['usage']['gpu_resident_seconds'], 0.)
        self.assertEqual(out['usage']['model_frame_views'][0]['width'], 112)
