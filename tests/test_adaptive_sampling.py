"""Question-directed scene/frame selection and preserved scheme-4 evidence semantics."""
from copy import deepcopy
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from script_core.adaptive_sampling import Sampler
from script_core import adaptive_entity as adaptive
from script_core.config import load_config
from tests.test_adaptive_entity import captions, entity, memory, report, commit


def directory(features):
    return dict(captions=[dict(id=f'C{i}', interval=[i, i+1], text=f'C holds box {i}.')
                         for i in range(len(features))], scenes=[dict(id='S0', center=[1., 0.],
        interval=[0, len(features)], chronological_order=list(range(len(features))),
        caption_ids=[f'C{i}' for i in range(len(features))], description='box')], pointers=[])


class SamplerTests(unittest.TestCase):
    def test_representative_is_question_relevant_instead_of_nearest_cluster_center(self):
        features = [[1., 0.]]*5 + [[0., 1.]]
        sampler = Sampler(directory(features), features, [0., 1.])
        seen = set()
        self.assertEqual(sampler.read('S0', seen, count=1), ['C5'])
        self.assertNotIn('C0', seen)

    def test_gap_reranks_both_scenes_and_frames_with_original_question_retained(self):
        features = [[.6, -.8], [.5, .8660254]]
        d = directory(features)
        d['scenes'] = [dict(id=f'S{i}', center=f, interval=[i, i+1],
            chronological_order=[i], caption_ids=[f'C{i}'], description='hint') for i, f in enumerate(features)]
        sampler = Sampler(d, features, [1., 0.])
        self.assertEqual(sampler.rank_scenes()[0]['scene_id'], 'S0')
        sampler.set_gap('missing evidence', [0., 1.])
        self.assertEqual(sampler.rank_scenes()[0]['scene_id'], 'S1')
        self.assertGreater(sampler.query[0], sampler.query[1])

    def test_open_scene_does_not_establish_global_or_exhaustive_coverage(self):
        features = [[1., 0.]]*9
        sampler = Sampler(directory(features), features, [1., 0.])
        self.assertFalse(sampler.coverage(dict(scope='global'), {'C0'})['met'])
        self.assertTrue(sampler.coverage(dict(scope='global'), {'C0', 'C4', 'C8'})['met'])
        self.assertFalse(sampler.coverage(dict(scope='exhaustive'), {'C0', 'C4', 'C8'})['met'])
        self.assertTrue(sampler.coverage(dict(scope='exhaustive'), {f'C{i}' for i in range(9)})['met'])

    def test_expansion_reads_new_query_ranked_frames_and_temporal_neighbors(self):
        features = [[1., 0.]]*7
        features[3] = [0., 1.]
        sampler = Sampler(directory(features), features, [0., 1.])
        seen = set()
        self.assertEqual(sampler.read('S0', seen, count=1), ['C3'])
        expanded = sampler.read('S0', seen, count=2, temporal_context=True)
        self.assertEqual(set(expanded), {'C2', 'C4'})
        self.assertEqual(len(seen), 3)

    def test_coverage_expansion_restricts_to_actual_unread_time_bin(self):
        features = [[1., 0.]]*9
        sampler = Sampler(directory(features), features, [1., 0.])
        seen = {'C0'}
        row = sampler.coverage_candidates(dict(scope='global'), seen, ['S0'])[0]
        chosen = sampler.read('S0', seen, count=1, allowed_caption_ids=row['unread_caption_ids'])
        self.assertGreaterEqual(int(chosen[0][1:]), 3)

    def test_bad_grid_cannot_silently_fall_back_to_center_representatives(self):
        with self.assertRaises(ValueError):
            Sampler(directory([[1., 0.]]*3), [[1., 0.]], [1., 0.])

    def test_pipeline_planner_extractor_and_final_qa_never_receive_unread_scene_text(self):
        features = [[1., 0.]]*7 + [[0., 1.]]
        d = directory(features)
        packet = dict(captions=d['captions'], frame_features=features,
            question=dict(question_id='q', video_id='v', question='Which box is relevant?', options=['a']*5))
        cfg = load_config()
        cfg['query']['temporal_sparse_visual'] = False
        class Backend:
            def __init__(self): self.calls = []
            def call(self, op, inp, timeout):
                self.calls.append((op, deepcopy(inp)))
                if op == 'scene_scope': return dict(data=dict(scope='local', reason='local'))
                if op == 'clip_text': return dict(vectors=[[0., 1.]])
                if op == 'adaptive_next':
                    self_outer.assertEqual([c['id'] for c in inp['captions']], ['C7'])
                    return dict(data=dict(sufficient=True, reason='sufficient source', gap='', action='stop',
                        scene_id='stop', caption_ids=[], entity_ids=[], focus=[], fields=[]))
                if op == 'temporal_qa_check': return dict(input_tokens=100)
                if op == 'videotree_qa':
                    self_outer.assertIn('box 7', inp['prompts'][-1])
                    self_outer.assertNotIn('box 0', inp['prompts'][-1])
                    return dict(data=dict(selected_option='0'))
                raise AssertionError(op)
        self_outer = self
        with tempfile.TemporaryDirectory() as tmp:
            r = adaptive.run(packet, d, [], Backend(), cfg, Path(tmp),
                dict(online_seconds=600, max_scene_visits=4, adaptive_initial_frames=1))
            self.assertIsNone(r['error'], r['error'])
            self.assertEqual(r['selected_seconds'], [7])
            self.assertEqual(r['read_caption_count'], 1)


class Scheme4SemanticsTests(unittest.TestCase):
    def test_later_attribute_refill_cannot_erase_an_existing_change_or_known_cause(self):
        m = memory()
        commit(m, report(entity()))
        change = entity('E0', cid='C1')
        change['observations'][0].update(continuity='changed', cause=dict(action='opens', actor='C',
            tool='knife', basis='inferred_action', source_caption_ids=['C1']))
        commit(m, report(change), 'change')
        for continuity in ('uncertain', 'same'):
            with self.subTest(continuity=continuity):
                supplemented = deepcopy(m)
                refill = entity('E0', cid='C1', attributes=[dict(field='color', value='red', source_caption_ids=['C0'])])
                refill['observations'][0].update(continuity=continuity, cause=dict(action=None, actor=None,
                    tool=None, basis='unknown', source_caption_ids=[]))
                commit(supplemented, report(refill), 'color')
                self.assertEqual(len(supplemented['temporal_network']['transitions']), 1)
                self.assertEqual(supplemented['temporal_network']['transitions'][0]['cause']['action'], 'opens')

    def test_four_arm_worker_routes_cross_control_and_preserves_both_adaptive_variants(self):
        from script import run_adaptive_comparison as runner
        from script_core.state import write_json
        import json
        cfg = load_config()
        cost = dict(total=.25, by_operation={'caption_temporal_observe': .25}, call_count=1, budget_violation=False)
        shared_directory = dict(cost_summary=dict(total=.1, budget_violation=False))
        packets = {u: dict(question=dict(question_id=u, video_id=u, question='box?', options=['a']*5),
            captions=captions(), frames=[]) for u in ('v0', 'v1')}
        class Backend:
            def __init__(self, *args): pass
            def call(self, op, inp, timeout): return dict(ready=True, vectors=[[1., 0.]])
            def close(self): pass
        def result(arm, target, operation, tokens, selected):
            target.mkdir(parents=True, exist_ok=True)
            (target/'calls.jsonl').write_text(json.dumps(dict(operation=operation, output_tokens=tokens, input_tokens=10))+'\n')
            return dict(arm=arm, question_id=target.name, error=None, answer=dict(selected_option='0'),
                incomplete=[], inventory_incomplete=[], entity_count=1, selected_seconds=selected,
                cost_summary=deepcopy(cost))
        def original(packet, d, frames, backend, config, target, arm, settings):
            r = result(arm, target, 'caption_temporal_observe', 20, [0, 1])
            write_json(target/'evidence_bundle.json', dict(preparation_cost_summary=deepcopy(cost),
                selected_seconds=[0, 1], captions=packet['captions']))
            return r
        def cross(packet, bundle, backend, config, target, settings):
            self.assertNotIn('answer', bundle)
            self.assertEqual(bundle['selected_seconds'], [0, 1])
            return result('temporal_cross', target, 'scene_correspondence_lineage', 3, [0, 1])
        def improved(packet, d, frames, backend, config, target, settings, memory_path):
            return result('adaptive_scheme4', target, 'adaptive_extract', 8, [1])
        def transition(packet, d, frames, backend, config, target, settings, memory_path):
            return result('adaptive_partial', target, 'transition_extract', 12, [0, 1])
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            for uid, packet in packets.items():
                write_json(out/'inputs'/f'{uid}.json', packet)
            for name, value in [('cohort.json', list(packets)), ('questions.json', [p['question'] for p in packets.values()]),
                ('captions.json', {u: captions() for u in packets}), ('settings.json', {}),
                ('config.json', cfg), ('evaluation_only.json', {u: 0 for u in packets})]:
                write_json(out/name, value)
            for phase in ('smoke', 'formal'):
                for uid in packets:
                    write_json(out/phase/'directories'/uid/'directory.json', shared_directory)
            with patch.object(runner, 'verify'), patch.object(runner.subprocess, 'run'), \
                 patch('script_core.backend.preflight', return_value=(cfg, {})), \
                 patch('script_core.budget.ModelWorker', Backend), \
                 patch('script.run_four_arm_300_local.make_packet'), \
                 patch('script.run_temporal_entity.checked_packet', side_effect=lambda out, uid, **kw: packets[uid]), \
                 patch('script_core.temporal_pipeline.run', side_effect=original) as baseline, \
                 patch('script_core.temporal_pipeline.run_cross_replay', side_effect=cross) as replay, \
                 patch('script_core.adaptive_transition.run', side_effect=transition) as partial, \
                 patch('script_core.adaptive_entity.run', side_effect=improved) as new, \
                 patch.object(runner, 'source_link_probe', return_value=dict(passed=True)):
                self.assertEqual(runner.worker(out), 0)
                self.assertEqual((baseline.call_count, replay.call_count, partial.call_count, new.call_count), (4, 4, 4, 4))
            summary = json.loads((out/'report.json').read_text())
            self.assertEqual(summary['common_completed'], 2)
            self.assertEqual(summary['arms']['temporal_cross']['total_online_output_tokens'], 46)
            self.assertEqual(summary['arms']['temporal_cross']['executed_online_output_tokens'], 6)
            self.assertEqual(set(summary['arms']), {'summary_deep', 'temporal_cross', 'adaptive_partial', 'adaptive_scheme4'})
            self.assertEqual(summary['arms']['adaptive_partial']['mean_read_caption_count'], 2)
            self.assertEqual(summary['arms']['adaptive_scheme4']['mean_read_caption_count'], 1)
            self.assertTrue(json.loads((out/'verification/gate.json').read_text())['passed'])

    def test_explicit_change_and_cited_cause_use_original_scheme4_state_assembler(self):
        m = memory()
        commit(m, report(entity()))
        changed = entity('E0', cid='C1')
        changed['observations'][0].update(continuity='changed', cause=dict(action='opens', actor='C', tool='knife',
            basis='inferred_action', source_caption_ids=['C1']))
        commit(m, report(changed), 'change')
        net = m['temporal_network']
        self.assertEqual(len(net['states']), 2)
        self.assertEqual(len(net['transitions']), 1)
        self.assertEqual(net['transitions'][0]['cause']['action'], 'opens')
        self.assertEqual(net['transitions'][0]['cause']['source_caption_ids'], ['C1'])

    def test_cross_scene_match_retains_scheme4_identity_and_separate_state_history(self):
        m = memory()
        commit(m, report(entity()))
        adaptive.commit_delta(m, report(entity(cid='C1')), captions(), 'S1', dict(request_key='other'), 'call-2')
        class Meter:
            def json(self, op, inp, required):
                self_outer.assertEqual(op, 'scene_correspondence_lineage')
                self_outer.assertEqual(inp['question'], 'What happens to the box?')
                return dict(matches=[dict(left=dict(entity='E1', caption_ids=['C1']),
                    right=dict(entity='E0', caption_ids=['C0']), relation='same_instance', reason='supported text identity')]), {}
        self_outer = self
        adaptive.cross_align_partial(m, 'S1', dict(question='What happens to the box?'), 'box history', Meter())
        net = m['temporal_network']
        self.assertEqual(len(net['entities']), 1)
        self.assertEqual(len(net['states']), 2)
        self.assertEqual(net['transitions'], [])
        self.assertEqual(net['identities'][0]['decision'], 'same')
        self.assertFalse(net['identities'][0]['physical_identity_confirmed'])
        self.assertEqual(m['indexes']['entity_to_captions']['E1'], ['C0', 'C1'])
        self.assertEqual(len(adaptive.memory_context(m)['identities']), 1)

    def test_four_arm_order_keeps_original3_before_original4(self):
        from script.run_adaptive_comparison import ARMS, arm_order
        self.assertEqual(ARMS, ('summary_deep', 'temporal_cross', 'adaptive_partial', 'adaptive_scheme4'))
        for index in (0, 1, 2):
            order = arm_order(index)
            self.assertEqual(set(order), set(ARMS))
            self.assertLess(order.index('summary_deep'), order.index('temporal_cross'))


if __name__ == '__main__':
    unittest.main()
