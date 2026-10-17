"""Acceptance checks for SCRIPT's state machine; fixtures are not video truth."""
from copy import deepcopy
import itertools
import json
import math
from pathlib import Path
import tempfile
import unittest

import numpy as np

from script_core.budget import BudgetLedger, Meter, ResourceExhausted, ToolFailure
from script_core.cache import CacheManager, cache_key, safe_source
from script_core.config import DEFAULTS, base_config, digest, validate_config
from script_core.controller import structural_gate, answer_query
from script_core.coverage import complement, coverage_record
from script_core.evaluation import evidence_match, paired_cluster_bootstrap
from script_core.segmentation import contiguous_partition, frame_boundaries, sample_indices, windows
from script_core.state import Memory, Store, memory_hash, obj
from script_core.identity import commit_identity_patch
from script_core.tracking import track_detections, register_tracks


def fixture_memory(duration=12.):
    frames = [{'frame_id': f'v:F{i}', 'pts': i/2} for i in range(int(duration*2))]
    manifest = obj('v:manifest', video_id='v', uri='fixture.mp4', media_hash='test', duration=duration,
                   cutoff=duration, time_base='1/2', frames=frames, subtitle_uri=None, subtitle_hash=None,
                   build_status='complete', missing_intervals=[], config_hash='cfg')
    memory = Memory(manifest)
    ev = obj('ev', video_id='v', scene_ids=[], interval=[0., duration], source_type='visual',
             frame_inputs=[dict(f, width=768, height=432, crop=None) for f in frames], text_spans=[],
             observation='Synthetic state-machine source', input_hash='in', output_hash='out', observer_config_hash='cfg', call_id='c')
    memory.put('evidence', ev)
    return memory


class SegmentationTests(unittest.TestCase):
    def test_exact_dp_against_exhaustive(self):
        rng = np.random.default_rng(123)
        for n in range(2, 11):
            x = rng.normal(size=(n, 3))
            x /= np.linalg.norm(x, axis=1, keepdims=True)
            for penalty in (0., .2, 2., 5.):
                segments = contiguous_partition(x, penalty, 2)
                def objective(parts):
                    return sum(((x[a:b]-x[a:b].mean(axis=0))**2).sum() for a, b in parts)+penalty*(len(parts)-1)
                valid = []
                for bits in itertools.product((False, True), repeat=n-1):
                    boundaries = [0]+[i+1 for i, flag in enumerate(bits) if flag]+[n]
                    parts = list(zip(boundaries, boundaries[1:]))
                    if all(b-a >= 2 for a, b in parts):
                        valid.append(objective(parts))
                self.assertAlmostEqual(objective(segments), min(valid), places=7)

    def test_a_b_a_stays_contiguous_and_static_ties_choose_one(self):
        features = [[1., 0.]]*4+[[0., 1.]]*4+[[1., 0.]]*4
        self.assertEqual(contiguous_partition(features, .1), [(0, 4), (4, 8), (8, 12)])
        self.assertEqual(contiguous_partition([[1., 0.]]*10, 0.), [(0, 10)])
        self.assertEqual(contiguous_partition([[1., 0.]], 5.), [(0, 1)])

    def test_vfr_sampling_ties_and_boundaries(self):
        pts = [0., .3, .8, 1.2, 1.7, 2.4]
        self.assertEqual(sample_indices(pts, [0., 2.5], fps=1.), [0, 2, 4])
        boundaries = frame_boundaries([(0, 2), (2, 3)], [0., .8, 1.7], pts, 2.5)
        self.assertEqual(boundaries[0]['interval'], [0., 1.7])
        self.assertEqual(boundaries[-1]['end_frame_exclusive'], len(pts))
        self.assertEqual(windows([0., 20.]), [[0., 8.], [7., 15.], [14., 20.]])
        self.assertEqual(sample_indices([.7333333333333333, .7666666666666667], [.7, .8], count=1), [0])


class StateTests(unittest.TestCase):
    def test_and_or_revoke_preserves_local_fact(self):
        memory = fixture_memory()
        for id_ in ('F7', 'F12'):
            memory.put('derived_claims', obj(id_, node_type='query_fact', content={}, support_sets=[['ev']], status='supported'))
        for id_ in ('H7', 'H12'):
            memory.put('bindings', obj(id_, variable='bag', entity_id=None, anchor_occurrence_ids=[], support_sets=[['ev']], status='confirmed'))
        memory.put('derived_claims', obj('A7', node_type='answer_candidate', content={}, support_sets=[['H7', 'F7']], status='supported'))
        memory.put('derived_claims', obj('A', node_type='answer_candidate', content={}, support_sets=[['H7', 'F7'], ['H12', 'F12']], status='supported'))
        memory.revoke('H7')
        self.assertEqual(memory.get('A7')['status'], 'invalidated')
        self.assertTrue(memory.is_valid('F7'))
        self.assertTrue(memory.is_valid('A'))
        memory.revoke('H12')
        self.assertFalse(memory.is_valid('A'))
        memory.validate()

    def test_missing_ref_cycle_and_transaction_rollback(self):
        memory = fixture_memory()
        before = memory_hash(memory)
        with self.assertRaises(ValueError), memory.transaction():
            memory.put('derived_claims', obj('x', node_type='query_fact', content={}, support_sets=[['missing']], status='supported'))
        self.assertEqual(memory_hash(memory), before)
        memory.put('derived_claims', obj('x', node_type='query_fact', content={}, support_sets=[['y']], status='supported'))
        memory.put('derived_claims', obj('y', node_type='query_fact', content={}, support_sets=[['x']], status='supported'))
        with self.assertRaisesRegex(ValueError, 'cycle'):
            memory.validate()

    def test_checkpoint_round_trip(self):
        memory = fixture_memory()
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            store.checkpoint(memory, {'base_hash': memory_hash(memory)})
            restored, meta = store.load()
            self.assertEqual(memory_hash(restored), meta['base_hash'])
            store.close()

    def test_source_and_pts_cannot_be_fabricated(self):
        memory = fixture_memory()
        memory.get('ev')['frame_inputs'][0]['pts'] = .01
        with self.assertRaisesRegex(ValueError, 'PTS'):
            memory.validate()

    def test_two_confirmed_identities_rejected_and_patch_rolls_back(self):
        memory = fixture_memory()
        occurrence = obj('O', scene_id=None, category='bag', aliases=[], visible_intervals=[[0., .5]],
            boxes=[{'frame_id': 'v:F0', 'pts': 0., 'bbox': [.1, .1, .3, .3], 'detection_score': .9}],
            crop_refs=[], appearance_feature_ref='feature', description='bag', evidence_ids=['ev'],
            entity_candidate_ids=[], stable_track=False)
        memory.put('occurrences', occurrence)
        verification = {'result': 'supported', 'rule_version': 'test', 'model_hash': 'm', 'prompt_hash': 'p',
                        'input_evidence_ids': ['ev'], 'distinguishing_facts': ['anchor_registration: test'], 'unresolved_conflicts': []}
        for id_ in ('E1', 'E2'):
            memory.put('entities', obj(id_, category='bag', aliases=[], descriptors=[], confirmed_occurrence_ids=['O'], candidate_link_ids=[], prototype_refs=[]))
        memory.put('identity_links', obj('L1', occurrence_id='O', entity_id='E1', status='confirmed', retrieval_score=1.,
                                         evidence_ids=['ev'], cannot_link_refs=[], verification=verification))
        memory.validate()
        patch = obj('P', expected_revisions={'L1': 99}, changes=[], evidence_ids=['ev'], affected_ids=['L1'], status='proposed', reason='test')
        before = memory_hash(memory)
        with self.assertRaises(ValueError):
            commit_identity_patch(memory, patch, 'B3')
        self.assertEqual(memory_hash(memory), before)
        with self.assertRaises(ValueError):
            commit_identity_patch(memory, patch, 'B2')
        memory.put('identity_links', obj('L2', occurrence_id='O', entity_id='E2', status='confirmed', retrieval_score=1.,
                                         evidence_ids=['ev'], cannot_link_refs=[], verification=verification))
        with self.assertRaisesRegex(ValueError, 'Two confirmed'):
            memory.validate()

    def test_single_writer_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Store(directory)
            try:
                with self.assertRaisesRegex(ValueError, 'Another writer'):
                    Store(directory)
            finally:
                first.close()

    def test_dynamic_endpoints_do_not_prove_transfer(self):
        memory = fixture_memory()
        memory.get('ev')['frame_inputs'] = memory.get('ev')['frame_inputs'][:2]
        for id_ in ('actor', 'recipient', 'bag'):
            memory.put('occurrences', obj(id_, scene_id=None, category='object', aliases=[], visible_intervals=[[0., 1.]],
                boxes=[{'frame_id': 'v:F0', 'pts': 0., 'bbox': [.1, .1, .2, .2], 'detection_score': .9}],
                crop_refs=[], appearance_feature_ref='f', description='', evidence_ids=['ev'], entity_candidate_ids=[], stable_track=False))
        v = {'result': 'supported', 'rule_version': 'test', 'model_hash': 'm', 'prompt_hash': 'p', 'input_evidence_ids': ['ev'],
             'distinguishing_facts': ['endpoints'], 'unresolved_conflicts': [], 'process_visible': True}
        memory.put('claims', obj('c', scene_ids=[], predicate='transfer', roles={'actor': 'actor', 'recipient': 'recipient', 'object': 'bag'},
            interval=[0., 1.], observed_at=None, status='supported', source_type='visual', evidence_ids=['ev'], counterevidence_ids=[],
            support_sets=[['ev']], verification=v, duplicate_group=None))
        with self.assertRaisesRegex(ValueError, 'process'):
            memory.validate()


class CoverageCacheTests(unittest.TestCase):
    def test_sampling_cannot_upgrade_without_verified_visibility(self):
        memory = fixture_memory()
        record = coverage_record('cov', [0., 6.], [], memory.get('ev'), None,
                                 {'visibility': 'clear', 'result': 'no_change_seen', 'min_target_short_side': 50,
                                  'task_verified': True}, DEFAULTS)
        self.assertEqual(record['level'], 'sampled')
        self.assertEqual(complement([0., 12.], [[0., 4.], [3., 6.], [8., 12.]]), [[6., 8.]])

    def test_last_needs_post_event_coverage(self):
        memory = fixture_memory()
        requirement = obj('r', predicate='other', roles={}, scope=[0., 12.], quantifier='last', prerequisite_ids=[],
                          acceptable_modalities=['visual'], answer_slot='location', support_sets=[['ev']], status='satisfied')
        memory.put('requirements', requirement)
        query = obj('q', scope=[0., 12.])
        passed, gaps = structural_gate(memory, query, [], [], DEFAULTS)
        self.assertFalse(passed)
        self.assertIn('coverage:r', gaps)

    def test_scope_target_and_future_cache_rejection(self):
        memory = fixture_memory()
        cache = CacheManager(memory, 'q', [0., 12.], DEFAULTS)
        extraction = {'predicates': ['hold']}
        key = cache_key(memory.manifest, memory.get('ev')['frame_inputs'], 'model', 'prompt', {}, 'target', extraction)
        cache.save(key, 'outputs', 'in', 'model', 'prompt', {}, [], ['ev'], ['c'], 'target', extraction)
        self.assertIsNotNone(cache.lookup(key, 'outputs', 'target', extraction))
        self.assertIsNone(cache.lookup(key, 'outputs', 'target', {'predicates': ['transfer']}))
        self.assertIsNone(cache.lookup(key, 'outputs', None, extraction))
        cache.scope = [0., 5.]
        self.assertIsNone(cache.lookup(key, 'outputs', 'target', extraction))
        memory.put('bindings', obj('H', variable='x', entity_id=None, anchor_occurrence_ids=[], support_sets=[['ev']], status='confirmed'))
        self.assertFalse(safe_source(memory, 'H', [0., 12.], shared=True))

    def test_context_envelope_is_not_read_evidence(self):
        memory = fixture_memory()
        ev = deepcopy(memory.get('ev'))
        ev['frame_inputs'] = [ev['frame_inputs'][0]]
        self.assertFalse(evidence_match({'interval': [8., 10.]}, [ev]))


class TrackingBudgetTests(unittest.TestCase):
    def test_two_same_class_objects_and_overlap_ids(self):
        memory = fixture_memory()
        scene = obj('s', video_id='v', interval=[0., 12.], start_frame=0, end_frame_exclusive=24,
                    prev_scene_id=None, next_scene_id=None, window_ids=[], occurrence_ids=[], claim_ids=[],
                    summary_id='summary', boundary_config_hash='cfg')
        memory.put('scenes', scene)
        memory.put('summaries', obj('summary', scene_id='s'))
        ds = []
        for i in range(4):
            for left in (.1, .7):
                ds.append({'frame_id': f'v:F{i}', 'pts': i/2, 'bbox': [left, .1, left+.1, .3],
                           'score': .9, 'category': 'bag', 'aliases': [], 'crop_ref': f'{i}-{left}', 'feature': [1., 0.]})
        tracks = track_detections(ds, DEFAULTS['tracking'])
        self.assertEqual([len(t) for t in tracks], [4, 4])
        first = register_tracks(memory, tracks, 's', 'q', 'ev', DEFAULTS['tracking'])
        second = register_tracks(memory, tracks, 's', 'q', 'ev', DEFAULTS['tracking'])
        self.assertEqual([o['id'] for o in first], [o['id'] for o in second])
        self.assertEqual(len(memory.tables['occurrences']), 2)

    def test_reserve_never_spends_answer_budget(self):
        ledger = BudgetLedger(40., 5., 30.)
        self.assertEqual(ledger.remaining, 5.)
        self.assertEqual(ledger.allowance(60.), 5.)
        self.assertEqual(ledger.allowance(30., final=True), 30.)
        with self.assertRaises(ResourceExhausted):
            BudgetLedger(34., 5., 30.)

    def test_invalid_configuration_fails(self):
        cfg = deepcopy(DEFAULTS)
        cfg['construction']['strategy'] = 'eager'
        with self.assertRaises(ValueError):
            validate_config(cfg)

    def test_cluster_bootstrap_pairs_by_video(self):
        rows = [{'question_id': q, 'cluster_id': 'v', 'method': method, 'correct': method == 'B2'}
                for q in ('q1', 'q2') for method in ('B1S', 'B2')]
        score = paired_cluster_bootstrap(rows, repeats=50)
        self.assertEqual(score['difference'], 1.)
        self.assertEqual(score['independent_clusters'], 1)


class FixtureBackend:
    """Deterministic tool returns used solely for exercising the controller."""
    def __init__(self, memory):
        self.memory = memory
        self.operations = []

    def call(self, operation, inputs, timeout):
        self.operations.append(operation)
        frames = inputs.get('frames', [])
        if operation == 'parse':
            return {'data': {'requirements': [{'key': 'r', 'predicate': 'other', 'roles': {},
                'scope': inputs['scope'], 'quantifier': 'exists', 'prerequisites': [],
                'acceptable_modalities': ['visual'], 'answer_slot': 'answer'}], 'variables': [], 'ambiguities': []}}
        if operation == 'embed':
            return {'vectors': [[0., 1.] if 'second' in s else [1., 0.] for s in inputs['texts']]}
        if operation == 'retrieve':
            from script_core.retrieval import hybrid_search
            return {'data': hybrid_search(**inputs)}
        if operation in ('goal', 'flat_plan'):
            result = {'goal': inputs['question'], 'acceptance_checks': ['visible source supports answer']}
            if operation == 'flat_plan':
                result['candidate_id'] = inputs['candidates'][0]['id']
            return {'data': result}
        if operation == 'decode':
            return {'data': [dict(f, width=768, height=432, crop=None, path='fixture.png')
                             for f in self.memory.manifest['frames'] if inputs['interval'][0] <= f['pts'] < inputs['interval'][1]]}
        if operation == 'detect':
            return {'detections': []}
        if operation == 'supplement':
            return {'data': {'terms': [], 'source_frame_ids': []}}
        if operation == 'track':
            return {'data': []}
        if operation in ('extract', 'observe'):
            result = {'descriptions': [], 'claims': [{'predicate': 'other', 'roles': {},
                'interval': inputs['interval'], 'observed_at': None, 'source_type': 'visual',
                'source_frame_ids': [f['frame_id'] for f in frames[:4]], 'fact_type': 'state', 'description': 'Fixture observation'}],
                'missing_entity': [], 'uncertainties': []}
            if operation == 'observe':
                result = {k: result[k] for k in ('claims', 'uncertainties')}
            return {'data': result}
        if operation == 'verify_claim':
            return {'data': {'result': 'supported', 'distinguishing_facts': ['fixture'], 'unresolved_conflicts': [],
                             'source_frame_ids': [f['frame_id'] for f in frames[:4]], 'process_visible': False}}
        if operation == 'verify_claims':
            return {'data': {'checks': {key: {'result': 'supported', 'distinguishing_facts': ['fixture'],
                'unresolved_conflicts': [], 'source_frame_ids': candidate['source_frame_ids'], 'process_visible': False}
                for key, candidate in inputs['candidates'].items()}}}
        if operation == 'assess':
            return {'data': {'bindings': [], 'requirement_updates': [
                {'requirement_id': r['id'], 'result': 'supported', 'claim_ids': [c['id'] for c in inputs['claims'][:4]],
                 'binding_variables': []} for r in inputs['requirements']], 'candidate_discriminations': [],
                'next_leads': [], 'identity_corrections': [], 'identity_operations': [], 'unresolved': []}}
        if operation == 'gate':
            result = {'passed': True, 'unresolved': []}
            if inputs.get('options'):
                result.update(selected_option='0', option_checks={str(i):
                    {'result': 'supported' if i == 0 else 'refuted',
                     'basis': 'direct_support' if i == 0 else 'direct_contradiction',
                     'claim_ids': [c['id'] for c in inputs['claims'][:4]], 'reason': 'Fixture distinction'}
                    for i in range(len(inputs['options']))})
            return {'data': result}
        if operation == 'answer':
            answer = {'text': 'Fixture answer', 'selected_option': '0',
                'claim_ids': [c['id'] for c in inputs['claims'][:4]], 'evidence_ids': [e['id'] for e in inputs['evidence'][:4]]}
            if inputs.get('answer_mode') == 'choice' and inputs.get('options'):
                answer.update(evidence_summary='Fixture observation', option_alignment={key: {
                    'reason': 'Fixture does not distinguish options', 'claim_ids': [], 'match': 'unknown'
                } for key in inputs['option_ids']})
            return {'data': answer}
        raise AssertionError(f'Unexpected operation {operation}')


def fixture_base():
    memory = fixture_memory()
    for i, interval in enumerate(([0., 6.], [6., 12.])):
        sid, wid = f's{i}', f'w{i}'
        memory.put('scenes', obj(sid, video_id='v', interval=interval, start_frame=i*12, end_frame_exclusive=(i+1)*12,
            prev_scene_id='s0' if i else None, next_scene_id='s1' if not i else None, window_ids=[wid],
            occurrence_ids=[], claim_ids=[], summary_id=sid+':summary', boundary_config_hash='cfg'))
        memory.put('windows', obj(wid, scene_id=sid, interval=interval, frame_ids=[], caption='first' if not i else 'second',
                                 evidence_ids=['ev'], coverage_ids=[], mention_ids=[], caption_status='ok'))
        memory.put('summaries', obj(sid+':summary', scene_id=sid, text='first' if not i else 'second', place=None,
            entity_mentions=[], event_keywords=[], claim_ids=[], evidence_ids=['ev'], all_window_ids=[wid],
            uncertainties=[], source_revision_hash='hash', status='current'))
    docs = [{'id': 's0', 'interval': [0., 6.], 'text': 'first'}, {'id': 's1', 'interval': [6., 12.], 'text': 'second'}]
    ws = [dict(d, id=f'w{i}') for i, d in enumerate(docs)]
    metadata = {'base_hash': memory_hash(memory), 'base_config_hash': digest(base_config(DEFAULTS)),
                'initialization_cost': .1, 'index': {'documents': docs, 'vectors': [[1., 0.], [0., 1.]],
                                                   'window_documents': ws, 'window_vectors': [[1., 0.], [0., 1.]]}}
    return memory, metadata


class ControllerTests(unittest.TestCase):
    def test_claim_verifier_receives_only_referenced_entities_and_all_frames(self):
        from script_core.materialize import Materializer
        from unittest.mock import Mock
        base, _ = fixture_base()
        frames = base.get('ev')['frame_inputs']
        occurrences = [{'id': f'o{i}', 'boxes': [{'frame_id': frames[0]['frame_id'],
                        'pts': frames[0]['pts'], 'bbox': [.1, .1, .2, .2]}]} for i in range(32)]
        for occurrence in occurrences:
            base.put('occurrences', obj(occurrence['id'], **{k: v for k, v in occurrence.items() if k != 'id'}))
        meter = Mock()
        meter.json.return_value = ({'result': 'supported', 'distinguishing_facts': ['fixture'],
            'unresolved_conflicts': [], 'source_frame_ids': [frames[0]['frame_id']], 'process_visible': False}, {})
        with tempfile.TemporaryDirectory() as directory:
            materializer = Materializer(base, meter, DEFAULTS, 'q', directory)
            claim = materializer.verify_and_add_claim(
                {'id': 'task', 'scene_ids': ['s0'], 'extraction_scope': {'predicates': ['other']}},
                {'predicate': 'other', 'roles': {'object': 'o7'}, 'interval': [0., 6.],
                 'source_frame_ids': [frames[0]['frame_id']], 'fact_type': 'state', 'description': 'Fixture color'},
                base.get('ev'), occurrences)
        inputs = meter.json.call_args.args[1]
        self.assertEqual([o['id'] for o in inputs['occurrences']], ['o7'])
        self.assertEqual(inputs['frames'], frames)
        self.assertEqual(claim['roles'], {'object': 'o7'})

    def test_two_queries_reuse_m0_and_reset_details(self):
        base, meta = fixture_base()
        original = memory_hash(base)
        intervals = []
        with tempfile.TemporaryDirectory() as directory:
            for index, question in enumerate(('first', 'second')):
                backend = FixtureBackend(base)
                out = Path(directory) / str(index)
                result = answer_query(base, {'question_id': f'q{index}', 'video_id': 'v', 'question': question,
                                             'options': ['yes', 'no']}, meta, deepcopy(DEFAULTS), backend, out)
                self.assertIsNone(result['error'])
                self.assertEqual(result['stop_decision']['reason'], 'protocol_sufficient')
                steps = [json.loads(line) for line in (out / 'steps.jsonl').read_text().splitlines()]
                intervals.append(steps[0]['interval'])
                self.assertEqual(len(steps), 1)
                self.assertIn('observe', backend.operations)
                self.assertEqual(result['base_hash'], meta['base_hash'])
        self.assertEqual(intervals, [[0., 6.], [6., 12.]])
        self.assertEqual(memory_hash(base), original)
        self.assertFalse(base.tables['claims'])
        self.assertFalse(base.tables['occurrences'])

    def test_resource_stop_preserves_insufficient_and_choice(self):
        base, meta = fixture_base()
        cfg = deepcopy(DEFAULTS)
        cfg['query']['global_task_limit'] = 1
        class MissingBackend(FixtureBackend):
            def call(self, operation, inputs, timeout):
                if operation in ('extract', 'observe'):
                    missing = {'claims': [], 'uncertainties': ['occluded']}
                    if operation == 'extract':
                        missing.update(descriptions=[], missing_entity=[])
                    return {'data': missing}
                return super().call(operation, inputs, timeout)
        with tempfile.TemporaryDirectory() as directory:
            result = answer_query(base, {'question_id': 'q', 'video_id': 'v', 'question': 'first', 'options': ['a', 'b']},
                                  meta, cfg, MissingBackend(base), directory)
            self.assertEqual(result['stop_decision']['reason'], 'resource_stop')
            self.assertEqual(result['stop_decision']['evidence_status'], 'insufficient')
            self.assertEqual(result['answer']['selected_option'], '0')

    def test_annotation_leakage_and_infeasible(self):
        base, meta = fixture_base()
        with tempfile.TemporaryDirectory() as directory:
            query = {'question_id': 'q', 'video_id': 'v', 'question': 'first', 'total_cap': 1.}
            result = answer_query(base, query, meta, deepcopy(DEFAULTS), FixtureBackend(base), directory)
            self.assertEqual(result['error']['code'], 'infeasible')
            self.assertIsNone(result['stop_decision'])
            with self.assertRaises(ValueError):
                answer_query(base, dict(query, truth=0), meta, deepcopy(DEFAULTS), FixtureBackend(base), directory)

    def test_flat_structured_uses_global_interpretation(self):
        base, meta = fixture_base()
        cfg = deepcopy(DEFAULTS)
        cfg['method'] = 'B1S'
        backend = FixtureBackend(base)
        with tempfile.TemporaryDirectory() as directory:
            result = answer_query(base, {'question_id': 'q', 'video_id': 'v', 'question': 'first', 'options': ['yes', 'no']},
                                  meta, cfg, backend, directory)
            self.assertIsNone(result['error'])
            self.assertIn('flat_plan', backend.operations)
            self.assertNotIn('goal', backend.operations)
            self.assertEqual(result['stop_decision']['reason'], 'protocol_sufficient')

    def test_repeated_local_request_reuses_verified_output(self):
        from script_core.materialize import Materializer
        base, meta = fixture_base()
        base.put('requirements', obj('r', predicate='other', roles={}, scope=[0., 6.], quantifier='exists',
            prerequisite_ids=[], acceptable_modalities=['visual'], answer_slot=None, support_sets=[], status='unresolved'))
        task = obj('task', candidate_id=None, scene_ids=['s0'], allowed_interval=[0., 6.], requirement_ids=['r'],
            target_binding_ids=[], goal='first', acceptance_checks=[], route='summary', basis_refs=['w0'],
            relevant_versions={}, max_new_observations=1, cost_cap=60., extraction_scope={'predicates': ['other'],
            'target_occurrence_ids': [], 'include_context': True, 'mode': 'goal'}, target_signature='target', variables=[])
        base.put('tasks', task)
        backend = FixtureBackend(base)
        with tempfile.TemporaryDirectory() as directory:
            meter = Meter(backend, BudgetLedger(600., answer_reserve=30.), DEFAULTS, directory)
            materializer = Materializer(base, meter, DEFAULTS, 'q', directory)
            materializer.execute(task, [{'interval': [0., 6.]}])
            calls = backend.operations.count('observe')
            result = materializer.execute(task, [{'interval': [0., 6.]}])
            self.assertEqual(backend.operations.count('observe'), calls)
            self.assertTrue(result['cache_hit_ids'])

    def test_eager_is_a_separate_overlay(self):
        from script_core.strategies import build_eager
        base, meta = fixture_base()
        before = memory_hash(base)
        cfg = deepcopy(DEFAULTS)
        cfg['construction'].update(strategy='eager', prebuild_relations=True, prebuild_identity_links=True)
        with tempfile.TemporaryDirectory() as directory:
            overlay, cost = build_eager(base, cfg, FixtureBackend(base), directory)
            self.assertTrue(overlay.tables['claims'])
            self.assertTrue(cost['construction_complete'])
            self.assertGreater(cost['total'], 0.)
        self.assertEqual(memory_hash(base), before)
        self.assertFalse(base.tables['claims'])

    def test_sequence_protocol_requires_exact_question_order(self):
        from script_core.sequence import SequenceCache
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sequence.json'
            path.write_text(json.dumps({'video_id': 'v', 'cutoff': 12., 'question_order': ['q2', 'q1'],
                                         'cache_directory': str(Path(directory) / 'cache')}))
            with self.assertRaises(ValueError):
                SequenceCache(path, [{'question_id': 'q1', 'video_id': 'v'}, {'question_id': 'q2', 'video_id': 'v'}], DEFAULTS)


class VideoReaderTests(unittest.TestCase):
    def test_original_pts_legal_prefix_and_decode_cache(self):
        import av
        from script_core.video import inspect_video, read_frames
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.mp4'
            with av.open(str(path), 'w') as container:
                stream = container.add_stream('libx264', rate=10)
                stream.width, stream.height, stream.pix_fmt = 64, 48, 'yuv420p'
                for i in range(30):
                    frame = av.VideoFrame.from_ndarray(np.full((48, 64, 3), i*8, dtype=np.uint8), format='rgb24')
                    for packet in stream.encode(frame):
                        container.mux(packet)
                for packet in stream.encode():
                    container.mux(packet)
            manifest = inspect_video({'video_id': 'test', 'uri': str(path), 'cutoff': 1.5}, {})
            self.assertEqual(len(manifest['frames']), 15)
            first = read_frames(manifest, [0., 1.5], Path(directory) / 'frames', fps=2.)
            second = read_frames(manifest, [0., 1.5], Path(directory) / 'frames', fps=2.)
            self.assertEqual(first, second)
            self.assertEqual([f['pts'] for f in first], [0., .5, 1.])
            with self.assertRaises(ValueError):
                read_frames(manifest, [0., 2.], directory, fps=2.)


if __name__ == '__main__':
    unittest.main()
