"""Acceptance regressions for the remaining plan requirements; no simulated perception claims."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from test_script_core import fixture_base, fixture_memory, FixtureBackend
from script_core.config import DEFAULTS
from script_core.state import obj, memory_hash
from script_core.patches import prepare_identity_patch, commit_patch, store_identity_proof
from script_core.navigation import baseline_input, delivered_feedback
from script_core.accounting import cpu_phase, metered_cpu, ACTIVE
from script_core.budget import BudgetLedger, Meter, ToolFailure
from script_core.segmentation import impute_short_samples
from script_core.replay import ReplayBackend


def identity_memory():
    memory, _ = fixture_base()
    verify = {'result': 'supported', 'distinguishing_facts': ['anchor_registration: fixture'],
              'unresolved_conflicts': [], 'basis_types': [], 'input_evidence_ids': ['ev']}
    for oid, index in [('a', 0), ('b', 4), ('c', 8)]:
        memory.put('occurrences', obj(oid, scene_id='s0', category='bag', aliases=[],
            visible_intervals=[[index/2, index/2+.5]], boxes=[{'frame_id': f'v:F{index}', 'pts': index/2,
            'bbox': [.1, .1, .2, .2], 'detection_score': .9}], crop_refs=[], appearance_feature_ref='features',
            description=oid, evidence_ids=['ev'], entity_candidate_ids=[], stable_track=True))
    for eid, members in [('E1', ['a', 'b']), ('E2', ['c'])]:
        memory.put('entities', obj(eid, category='bag', aliases=[], descriptors=[],
            confirmed_occurrence_ids=members, candidate_link_ids=[], prototype_refs=[]))
        for oid in members:
            memory.put('identity_links', obj('L'+oid, occurrence_id=oid, entity_id=eid, status='confirmed',
                retrieval_score=1., evidence_ids=['ev'], cannot_link_refs=[], verification=verify))
    memory.put('derived_claims', obj('local', node_type='query_fact', content={}, support_sets=[['ev']], status='supported'))
    memory.put('bindings', obj('binding', variable='bag', entity_id='E2', anchor_occurrence_ids=['c'],
        support_sets=[['Lc']], status='confirmed'))
    memory.put('derived_claims', obj('answer_support', node_type='answer_candidate', content={},
        support_sets=[['binding', 'local']], status='supported'))
    memory.validate()
    return memory


def proof(memory, left, right, result):
    frames = memory.get('ev')['frame_inputs']
    report = {'result': result, 'distinguishing_facts': ['Distinctive fixture marker'], 'unresolved_conflicts': [],
              'basis_types': ['unique_marker'], 'source_frame_ids': [memory.get(o)['boxes'][0]['frame_id'] for o in (left, right)]}
    return store_identity_proof(memory, 'q', memory.get(left), memory.get(right), report, DEFAULTS, frames)['id']


class PatchTests(unittest.TestCase):
    def test_merge_revokes_dependent_answer_but_preserves_local_fact(self):
        memory = identity_memory()
        p = prepare_identity_patch(memory, 'q', 'merge', ['c'], 'E1', [proof(memory, 'c', 'a', 'same')], 'E2')
        commit_patch(memory, p, 'B3')
        self.assertEqual(memory.get('Lc')['entity_id'], 'E1')
        self.assertEqual(memory.get('E2')['status'], 'retired')
        self.assertEqual(set(memory.get('E1')['confirmed_occurrence_ids']), {'a', 'b', 'c'})
        self.assertFalse(memory.is_valid('answer_support'))
        self.assertTrue(memory.is_valid('local'))
        self.assertEqual(memory.get(p['id'])['status'], 'committed')

    def test_split_creates_new_cluster_and_cannot_link(self):
        memory = identity_memory()
        p = prepare_identity_patch(memory, 'q', 'split', ['b'], 'new', [proof(memory, 'b', 'a', 'different')], 'E1')
        commit_patch(memory, p, 'B3')
        self.assertEqual(memory.get('Lb')['entity_id'], 'new')
        self.assertEqual(memory.get('E1')['confirmed_occurrence_ids'], ['a'])
        self.assertEqual(memory.get('new')['confirmed_occurrence_ids'], ['b'])
        self.assertEqual(len(memory.tables['cannot_links']), 1)
        memory.validate()

    def test_transaction_rolls_back_new_entity_links_and_counters(self):
        memory = identity_memory()
        p = prepare_identity_patch(memory, 'q', 'split', ['b'], 'new', [proof(memory, 'b', 'a', 'different')], 'E1',
            summary_corrections=[{'summary_id': 's0:summary', 'affected_text': 'old', 'replacement_text': 'new',
                                  'evidence_ids': []}])
        before, counters = memory_hash(memory), deepcopy(memory.counters)
        with self.assertRaisesRegex(ValueError, 'Summary correction'):
            commit_patch(memory, p, 'B3')
        self.assertEqual(memory_hash(memory), before)
        self.assertEqual(memory.counters, counters)

    def test_stale_patch_and_b2_cannot_commit_cluster_changes(self):
        memory = identity_memory()
        p = prepare_identity_patch(memory, 'q', 'merge', ['c'], 'E1', [proof(memory, 'c', 'a', 'same')], 'E2')
        memory.update('entities', 'E1', descriptors=['changed'])
        before = memory_hash(memory)
        with self.assertRaisesRegex(ValueError, 'revision'):
            commit_patch(memory, p, 'B3')
        with self.assertRaisesRegex(ValueError, 'B3'):
            commit_patch(memory, p, 'B2')
        self.assertEqual(memory_hash(memory), before)

    def test_simultaneously_separate_instances_cannot_be_confirmed_same(self):
        memory = identity_memory()
        memory.update('occurrences', 'c', boxes=[dict(memory.get('a')['boxes'][0], bbox=[.7, .1, .8, .2])])
        with self.assertRaisesRegex(ValueError, 'separate'):
            proof(memory, 'c', 'a', 'same')

    def test_split_requires_different_evidence_and_complete_membership(self):
        memory = identity_memory()
        p = prepare_identity_patch(memory, 'q', 'split', ['b'], 'new', [proof(memory, 'b', 'a', 'same')], 'E1')
        before = memory_hash(memory)
        with self.assertRaisesRegex(ValueError, 'different'):
            commit_patch(memory, p, 'B3')
        self.assertEqual(memory_hash(memory), before)

    def test_source_revision_invalidates_proof_and_dependent_identity(self):
        memory = identity_memory()
        pid = proof(memory, 'c', 'a', 'same')
        proposal = prepare_identity_patch(memory, 'q', 'merge', ['c'], 'E1', [pid], 'E2')
        commit_patch(memory, proposal, 'B3')
        memory.update('occurrences', 'c', description='new observation changes source version')
        self.assertFalse(memory.is_valid(pid))
        self.assertFalse(memory.is_valid('Lc'))
        self.assertTrue(memory.is_valid('local'))
        memory.validate()

    def test_source_change_rejects_already_prepared_patch(self):
        memory = identity_memory()
        pid = proof(memory, 'c', 'a', 'same')
        proposal = prepare_identity_patch(memory, 'q', 'merge', ['c'], 'E1', [pid], 'E2')
        memory.update('occurrences', 'a', description='new information')
        before = memory_hash(memory)
        with self.assertRaisesRegex(ValueError, 'revision'):
            commit_patch(memory, proposal, 'B3')
        self.assertEqual(memory_hash(memory), before)


class BaselineAndAblationTests(unittest.TestCase):
    def test_baselines_share_tools_but_differ_in_routing_and_feedback(self):
        memory, _ = fixture_base()
        inputs = {'question': 'where?', 'requirement': {'predicate': 'at', 'scope': [0., 12.], 'quantifier': 'exists'},
                  'variables': [], 'bindings': [], 'candidate': {'id': 'candidate'}}
        result = {'task_status': 'partial', 'unresolved': [{'requirement_id': 'r', 'reason': 'occluded'}],
                  'next_leads': [], 'supported_claim_ids': [], 'refuted_claim_ids': [], 'evidence_ids': [], 'coverage_ids': []}
        payloads = [baseline_input(m, inputs, [{'id': 'candidate'}], memory, {'last_result': result}) for m in ('B0', 'B1', 'B1S')]
        self.assertEqual(payloads[0]['available_tools'], payloads[2]['available_tools'])
        self.assertNotEqual(payloads[0]['routing_policy'], payloads[1]['routing_policy'])
        self.assertIn('feedback_text', payloads[1])
        self.assertNotIn('requirements', payloads[1])
        self.assertIn('requirements', payloads[2])
        self.assertIn('previous_results', payloads[2])

    def test_masked_feedback_does_not_delete_original_facts_or_gaps(self):
        cfg = deepcopy(DEFAULTS)
        cfg['ablations'].update(feedback_unresolved=False, feedback_next_leads=False)
        original = {'unresolved': ['missing'], 'next_leads': ['lead'], 'supported_claim_ids': ['fact']}
        delivered = delivered_feedback(original, cfg)
        self.assertEqual(delivered['unresolved'], [])
        self.assertEqual(delivered['supported_claim_ids'], ['fact'])
        self.assertEqual(original['unresolved'], ['missing'])


class MissingVideoTests(unittest.TestCase):
    def test_only_short_gaps_are_imputed_without_evidence_frame_ids(self):
        memory = fixture_memory(8.)
        manifest = memory.manifest
        manifest['missing_intervals'] = [[1., 2.], [3., 6.]]
        for f in manifest['frames']:
            f['missing'] = any(a <= f['pts'] < b for a, b in manifest['missing_intervals'])
        out = impute_short_samples(manifest, [[1., 0.]]*4, [0., 2., 6., 7.], ['v:F0', 'v:F4', 'v:F12', 'v:F14'], 1.)
        i = out['pts'].index(1.)
        self.assertTrue(out['imputed'][i])
        self.assertIsNone(out['frame_ids'][i])
        self.assertEqual(out['missing_sample_pts'], [3., 4., 5.])

    def test_missing_frames_cannot_be_source_evidence(self):
        memory = fixture_memory()
        memory.manifest['frames'][0]['missing'] = True
        with self.assertRaisesRegex(ValueError, 'Missing or imputed'):
            memory.validate()

    def test_packet_decode_gap_yields_partial_manifest(self):
        from script_core.video import inspect_video
        import av
        from fractions import Fraction
        class Container:
            duration = 6*av.time_base
            streams = Mock(video=[Mock(duration=6, time_base=Fraction(1, 1))])
            def __enter__(self): return self
            def __exit__(self, *args): pass
        frames = [(float(i), [Mock(pts=i, time_base=Fraction(1, 1))], None) if i in (0, 4, 5)
                  else (float(i), [], 'decode failure') for i in range(6)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'video.mp4'
            path.write_bytes(b'fixture')
            with patch('av.open', return_value=Container()), patch('script_core.video.decode_packets', return_value=iter(frames)):
                manifest = inspect_video({'video_id': 'v', 'uri': str(path)}, {})
        self.assertEqual(manifest['build_status'], 'partial')
        self.assertEqual(manifest['missing_intervals'], [[1., 4.]])
        self.assertEqual(sum(f['missing'] for f in manifest['frames']), 3)


class AccountingTests(unittest.TestCase):
    def test_nested_phases_and_tools_are_charged_once_and_cache_work_is_visible(self):
        class Backend:
            def call(self, operation, inputs, timeout):
                time.sleep(.015)
                return {'data': {}, 'usage': {'cpu_seconds': 0., 'gpu_resident_seconds': 0.}}
        with tempfile.TemporaryDirectory() as directory:
            ledger = BudgetLedger(600.)
            meter = Meter(Backend(), ledger, DEFAULTS, directory)
            started = time.monotonic()
            try:
                with cpu_phase('outer_cpu'):
                    time.sleep(.004)
                    with cpu_phase('cache_lookup'):
                        time.sleep(.004)
                    meter.call('leaf', {})
                    time.sleep(.004)
            finally:
                meter.close()
            elapsed = time.monotonic()-started
        self.assertGreater(ledger.summary()['by_operation']['cache_lookup'], .003)
        self.assertLessEqual(ledger.actual, elapsed+.005)
        self.assertGreater(ledger.actual, .02)
        self.assertEqual(ledger.resources()['cpu_unmeasured_calls'], 0)


class ReplayTests(unittest.TestCase):
    def test_real_return_replay_remaps_refs_and_blocks_unseen_input(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'source'
            ledger = BudgetLedger(600., calls_path=source/'calls.jsonl')
            meter = Meter(FixtureBackend(fixture_memory()), ledger, DEFAULTS, source/'raw_calls')
            frames = fixture_memory().get('ev')['frame_inputs'][:2]
            try:
                meter.call('verify_claim', {'frames': frames, 'occurrences': [{'id': 'old:O'}],
                    'claim': {'roles': {'object': 'old:O'}}})
            finally:
                meter.close()
            replay = ReplayBackend(Mock(), source)
            out = replay.call('verify_claim', {'frames': frames, 'occurrences': [{'id': 'new:O'}],
                              'claim': {'roles': {'object': 'new:O'}}}, 60.)
            self.assertEqual(out['data']['result'], 'supported')
            self.assertIn('source_processing_seconds', out['replay'])
            with self.assertRaisesRegex(ToolFailure, 'replay miss'):
                replay.call('verify_claim', {'frames': frames[:1]}, 60.)
            raw_path = next((source/'raw_calls').glob('*.json'))
            raw_path.write_text('{}')
            with self.assertRaisesRegex(ToolFailure, 'changed'):
                replay.call('verify_claim', {'frames': frames, 'occurrences': [{'id': 'new:O'}],
                            'claim': {'roles': {'object': 'new:O'}}}, 60.)


class CompletionAcceptanceTests(unittest.TestCase):
    def test_wrong_initial_scene_keeps_another_route_available(self):
        from script_core.controller import answer_query
        base, meta = fixture_base()
        class MissFirst(FixtureBackend):
            def call(self, operation, inputs, timeout):
                if operation == 'observe' and inputs['interval'][0] < 6.:
                    return {'data': {'claims': [], 'uncertainties': ['nothing relevant in this scene']}}
                return super().call(operation, inputs, timeout)
        with tempfile.TemporaryDirectory() as directory:
            result = answer_query(base, {'question_id': 'q', 'video_id': 'v', 'question': 'first',
                                         'options': ['a', 'b']}, meta, deepcopy(DEFAULTS), MissFirst(base), directory)
            steps = [json.loads(line) for line in (Path(directory)/'steps.jsonl').read_text().splitlines()]
        self.assertEqual([s['interval'] for s in steps], [[0., 6.], [6., 12.]])
        self.assertEqual(result['stop_decision']['reason'], 'protocol_sufficient')

    def test_last_requires_all_later_units_and_unknown_reopens_it(self):
        from script_core.controller import structural_gate
        from script_core.coverage import coverage_record
        base, _ = fixture_base()
        verify = {'result': 'supported', 'unresolved_conflicts': [], 'distinguishing_facts': ['fixture']}
        base.put('claims', obj('event', scene_ids=['s0'], predicate='other', roles={}, interval=[0., 3.],
            observed_at=None, status='supported', source_type='visual', evidence_ids=['ev'], counterevidence_ids=[],
            support_sets=[['ev']], verification=verify, duplicate_group=None))
        base.put('requirements', obj('last', predicate='other', roles={}, scope=[0., 12.], quantifier='last',
            prerequisite_ids=[], acceptable_modalities=['visual'], answer_slot=None,
            support_sets=[['event']], status='satisfied'))
        passed, _ = structural_gate(base, {'scope':[0.,12.]}, [], [], DEFAULTS)
        self.assertFalse(passed)
        report = {'visibility': 'clear', 'result': 'no_change_seen', 'min_target_short_side': 50, 'task_verified': True}
        for i, core in enumerate(([3., 9.], [9., 12.])):
            ref = 'verify'+str(i)
            base.put('derived_claims', obj(ref, node_type='query_fact', content={}, support_sets=[['ev']], status='supported'))
            base.put('coverage', coverage_record('cov'+str(i), core, ['last'], base.get('ev'), ref, report, DEFAULTS))
        self.assertTrue(structural_gate(base, {'scope':[0.,12.]}, [], [], DEFAULTS)[0])
        base.revoke('verify1')
        self.assertFalse(structural_gate(base, {'scope':[0.,12.]}, [], [], DEFAULTS)[0])

    def test_partial_and_complete_low_frame_rate_coverage(self):
        from script_core.coverage import coverage_record
        source = fixture_memory().get('ev')
        source['frame_inputs'] = [f for f in source['frame_inputs'] if f['pts'] == int(f['pts'])]
        report = {'visibility':'clear','result':'no_change_seen','min_target_short_side':50,'task_verified':True}
        complete = coverage_record('c', [0.,6.], [], source, 'v', report, DEFAULTS, source['frame_inputs'])
        self.assertEqual(complete['level'], 'protocol_checked')
        self.assertTrue(complete['source_frame_rate_limited'])
        partial = deepcopy(source); partial['frame_inputs'] = partial['frame_inputs'][::2]
        incomplete = coverage_record('c', [0.,6.], [], partial, 'v', report, DEFAULTS, source['frame_inputs'])
        self.assertEqual(incomplete['level'], 'sampled')

    def test_global_midpoint_cannot_use_an_empty_package_envelope(self):
        from script_core.coverage import midpoint_observed
        ev = fixture_memory().get('ev')
        ev['frame_inputs'] = ev['frame_inputs'][:1]
        self.assertFalse(midpoint_observed(ev, 8., DEFAULTS))

    def test_build_failure_records_paid_calls_and_restores_meter(self):
        from script_core.build import build_video
        class FailedBackend:
            def call(self, operation, inputs, timeout):
                raise ValueError('video decode failed')
        previous = ACTIVE.get()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ToolFailure):
                build_video({'video_id':'v', 'uri':'missing.mp4'}, DEFAULTS, FailedBackend(), directory)
            failure = json.loads((Path(directory)/'build_failure.json').read_text())
            self.assertEqual(failure['error']['code'], 'build_failed')
            self.assertGreater(failure['cost_summary']['online'], 0.)
            self.assertFalse(list((Path(directory)/'raw_calls').glob('*.pending.json')))
        self.assertIs(ACTIVE.get(), previous)

    def test_color_and_material_have_different_target_signatures(self):
        from script_core.cache import signature
        variables = [{'name':'object','description':'the same bag'}]
        color = signature(variables, ['other'], [], {'answer_slot':'color'})
        material = signature(variables, ['other'], [], {'answer_slot':'material'})
        self.assertNotEqual(color, material)

    def test_segmentation_comparisons_keep_a_b_a_contiguous(self):
        from script_core.segmentation import comparison_partition
        cfg = deepcopy(DEFAULTS['segmentation']); cfg.update(solver='kmeans_runs', kmeans_k=2)
        x = [[1.,0.]]*3+[[0.,1.]]*3+[[1.,0.]]*3
        boundaries, metadata = comparison_partition(x, list(range(9)), list(range(9)), 9., cfg)
        self.assertEqual(len(boundaries), 3)
        self.assertEqual(metadata['continuous_runs'], 3)
        self.assertEqual(boundaries[0]['interval'][0], 0.)
        self.assertEqual(boundaries[-1]['interval'][1], 9.)
        cfg.update(solver='fixed_windows', fixed_window_seconds=3.)
        fixed, _ = comparison_partition(x, list(range(9)), list(range(9)), 9., cfg)
        self.assertEqual([r['interval'] for r in fixed], [[0.,3.],[3.,6.],[6.,9.]])

    def test_replay_only_replays_a_complete_controller_run(self):
        from script_core.controller import answer_query
        base, meta = fixture_base(); cfg = deepcopy(DEFAULTS)
        query = {'question_id':'q', 'video_id':'v', 'question':'first', 'options':['a','b']}
        with tempfile.TemporaryDirectory() as directory:
            source, destination = Path(directory)/'live', Path(directory)/'replayed'
            original = answer_query(base, query, meta, cfg, FixtureBackend(base), source)
            replay = ReplayBackend(Mock(), source, replay_only=True)
            repeated = answer_query(base, query, meta, cfg, replay, destination)
            self.assertIsNone(repeated['error'])
            self.assertEqual(repeated['answer']['selected_option'], original['answer']['selected_option'])
            self.assertEqual(repeated['stop_decision']['reason'], original['stop_decision']['reason'])
            self.assertTrue(replay.used)

    def test_live_and_replay_runs_are_not_paired_together(self):
        from script_core.controller import answer_query
        from script_core.evaluation import evaluate
        base, meta = fixture_base()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, method in enumerate(('B2', 'B1S')):
                cfg = deepcopy(DEFAULTS); cfg['method'] = method
                out = root/str(index)
                result = answer_query(base, {'question_id':'q','video_id':'v','question':'first','options':['a','b']},
                                      meta, cfg, FixtureBackend(base), out)
                (out/'run_config.json').write_text(json.dumps(cfg))
                if index:
                    result['experiment_mode'] = 'fixed_observation_replay'
                    (out/'result.json').write_text(json.dumps(result))
            annotations = root/'annotations.json'
            annotations.write_text(json.dumps({'questions':[{'question_id':'q','selected_option':'0','cluster_id':'v'}]}))
            metrics = evaluate(root, annotations, root/'metrics')
            self.assertEqual(len(metrics['groups']), 2)
            self.assertTrue(all(c['paired_questions'] == 0 for c in metrics['paired_bootstrap'].values()))


class FinalInvariantTests(unittest.TestCase):
    def test_roles_cannot_be_assembled_from_incompatible_transfers(self):
        from script_core.materialize import Materializer
        memory, _ = fixture_base()
        verify = {'result':'supported','distinguishing_facts':['anchor_registration: fixture'],
                  'unresolved_conflicts':[], 'process_visible':True}
        for i, oid in enumerate(('giver','receiver','wrong_giver','wrong_receiver','bag')):
            memory.put('occurrences', obj(oid, scene_id='s0', category='object', aliases=[], visible_intervals=[[0.,2.]],
                boxes=[{'frame_id':'v:F0','pts':0.,'bbox':[.1,.1,.2,.2],'detection_score':.9}],
                crop_refs=[], appearance_feature_ref='f', description=oid, evidence_ids=['ev'], entity_candidate_ids=[], stable_track=True))
            memory.put('entities', obj('E_'+oid, category='object', aliases=[], descriptors=[],
                confirmed_occurrence_ids=[oid], candidate_link_ids=[], prototype_refs=[]))
            memory.put('identity_links', obj('L_'+oid, occurrence_id=oid, entity_id='E_'+oid, status='confirmed',
                retrieval_score=1., evidence_ids=['ev'], cannot_link_refs=[], verification=verify))
        for variable in ('giver','receiver','bag'):
            memory.put('bindings', obj('B_'+variable, variable=variable, entity_id='E_'+variable,
                anchor_occurrence_ids=[variable], support_sets=[['L_'+variable]], status='confirmed'))
        for id_, actor, recipient in [('c1','giver','wrong_receiver'),('c2','wrong_giver','receiver'),('c3','giver','receiver')]:
            memory.put('claims', obj(id_, scene_ids=['s0'], predicate='transfer', roles={'actor':actor,'recipient':recipient,'object':'bag'},
                interval=[0.,2.], observed_at=None, status='supported', source_type='visual', evidence_ids=['ev'],
                counterevidence_ids=[], support_sets=[['ev']], verification=verify, duplicate_group=None))
        memory.put('requirements', obj('r', predicate='transfer', roles={'actor':'giver','recipient':'receiver','object':'bag'},
            scope=[0.,6.], quantifier='exists', prerequisite_ids=[], acceptable_modalities=['visual'], answer_slot=None,
            support_sets=[], status='unresolved'))
        task = {'id':'task','requirement_ids':['r'],'variables':[{'name':v} for v in ('giver','receiver','bag')]}
        report = {'bindings':[], 'requirement_updates':[{'requirement_id':'r','result':'supported','claim_ids':['c1','c2']}],
                  'candidate_discriminations':[], 'next_leads':[], 'unresolved':[]}
        with tempfile.TemporaryDirectory() as directory:
            materializer=Materializer(memory, Mock(), DEFAULTS, 'q', directory)
            materializer.integrate_assessment(task, report)
            self.assertFalse(memory.is_valid('r'))
            report['requirement_updates'][0]['claim_ids']=['c3']
            materializer.integrate_assessment(task, report)
            self.assertTrue(memory.is_valid('r'))
        memory.validate()

    def test_final_nested_cpu_work_uses_remaining_answer_allowance(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger=BudgetLedger(600., answer_reserve=30.); ledger.actual=570.
            meter=Meter(Mock(), ledger, DEFAULTS, directory)
            try:
                with cpu_phase('checkpoint_write', final=True):
                    with cpu_phase('object_validation'):
                        time.sleep(.065)
            finally:
                meter.close()
            self.assertFalse(ledger.budget_violation)
            self.assertGreater(ledger.actual, 570.)
            self.assertLess(ledger.actual, 600.)

    def test_completed_resume_does_not_call_models_or_overwrite_result(self):
        from script_core.controller import answer_query
        base, meta=fixture_base(); cfg=deepcopy(DEFAULTS)
        query={'question_id':'q','video_id':'v','question':'first','options':['a','b']}
        with tempfile.TemporaryDirectory() as directory:
            first=answer_query(base,query,meta,cfg,FixtureBackend(base),directory)
            path=Path(directory)/'result.json'; before=path.read_bytes()
            backend=FixtureBackend(base)
            resumed=answer_query(base,query,meta,cfg,backend,directory,resume=True)
            self.assertEqual(first,resumed)
            self.assertEqual(path.read_bytes(),before)
            self.assertEqual(backend.operations,[])

    def test_cached_pixels_are_redecoded_if_the_png_changes(self):
        import av
        import numpy as np
        from PIL import Image
        from script_core.video import inspect_video, read_frames
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'video.mp4'
            with av.open(str(path),'w') as container:
                stream=container.add_stream('libx264',rate=10)
                stream.width,stream.height,stream.pix_fmt=64,48,'yuv420p'
                for i in range(20):
                    frame=av.VideoFrame.from_ndarray(np.full((48,64,3),i*10,dtype=np.uint8),format='rgb24')
                    for packet in stream.encode(frame): container.mux(packet)
                for packet in stream.encode(): container.mux(packet)
            manifest=inspect_video({'video_id':'v','uri':str(path)}, {})
            original=read_frames(manifest,[0.,2.],Path(directory)/'frames',fps=2.)
            Image.new('RGB',(64,48),'white').save(original[0]['path'])
            refreshed=read_frames(manifest,[0.,2.],Path(directory)/'frames',fps=2.)
            self.assertEqual(refreshed[0]['content_hash'],original[0]['content_hash'])

    def test_runtime_schema_rejects_unread_references_and_wrong_types(self):
        from script_core.schemas import validate_output, output_schema
        schema=output_schema('verify_claim', {'frames':[{'frame_id':'real'}]})
        result={'result':'supported','distinguishing_facts':[],'unresolved_conflicts':[],
                'source_frame_ids':['unread'],'process_visible':False}
        with self.assertRaisesRegex(ValueError,'allowlist'): validate_output(result,schema)
        result['source_frame_ids']=['real']; result['process_visible']='yes'
        with self.assertRaisesRegex(ValueError,'expected'): validate_output(result,schema)


if __name__ == '__main__':
    unittest.main()
