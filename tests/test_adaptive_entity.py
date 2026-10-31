"""Partial-scope grounding, persisted source links, incremental refill and GPU queue."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from script_core import adaptive_entity as adaptive
from script_core.config import load_config
from script_core.schemas import output_schema, validate_output, restore_empty_arrays, decoder_schema
from script_core.state import write_json


def captions():
    return [dict(id='C0', interval=[0, 1], text='C holds a red metal box.'),
            dict(id='C1', interval=[1, 2], text='C opens the box with a knife.')]


def entity(ref='new', cid='C0', attributes=()):
    return dict(entity_ref=ref, label='box', source_caption_ids=[cid],
        attributes=list(attributes), observations=[dict(caption_id=cid, kind='state')])


def report(*rows, relations=()):
    return dict(entities=list(rows), relations=list(relations), unresolved='')


def memory():
    m = adaptive.new_memory('v', captions(), 'extract-v1')
    adaptive.rebuild_indexes(m)
    return m


def commit(m, r, key='first'):
    adaptive.commit_delta(m, r, captions(), 'S0', dict(request_key=key), 'call-1')


class PartialMemoryTests(unittest.TestCase):
    def test_observation_can_add_a_verified_mention_omitted_from_the_entity_source_list(self):
        m = memory()
        row = entity()
        row['observations'].append(dict(caption_id='C1', kind='action'))
        commit(m, report(row))
        self.assertEqual(m['entities']['E0']['mention_caption_ids'], ['C0', 'C1'])
        self.assertEqual(m['indexes']['caption_to_records']['C1']['entity_ids'], ['E0'])
        self.assertEqual(len(m['observations']), 2)

    def test_unsupported_optional_cause_stays_unknown_without_losing_sourced_observation(self):
        m = memory()
        row = entity(cid='C1')
        row['observations'][0].update(continuity='changed', cause=dict(action='opening',
            actor='C', tool='knife', basis='inferred_action', source_caption_ids=['C1']))
        commit(m, report(row))
        self.assertEqual(len(m['entities']), 1)
        observation = next(iter(m['observations'].values()))
        self.assertEqual(observation['cause']['basis'], 'unknown')
        self.assertIsNone(observation['cause']['action'])
        self.assertEqual(m['rejected_fields'][0]['cause']['action'], 'opening')
        self.assertEqual(m['rejected_fields'][0]['call_id'], 'call-1')

    def test_foreign_cause_citation_remains_an_atomic_hard_failure(self):
        m = memory()
        row = entity(cid='C1')
        row['observations'][0]['cause'] = dict(action='opens', actor='C', tool=None,
            basis='inferred_action', source_caption_ids=['FOREIGN'])
        before = deepcopy(m)
        with self.assertRaises(ValueError):
            commit(m, report(row))
        self.assertEqual(m, before)

    def test_live_constraint_parser_can_complete_reference_only_delta(self):
        from lmformatenforcer import JsonSchemaParser
        inp = dict(captions=captions(), entities=[], fields=['state'], page_size=3)
        value = report(entity())
        del value['entities'][0]['attributes']
        schema = decoder_schema(output_schema('adaptive_extract', inp))
        parser = JsonSchemaParser(schema)
        parser.config.force_json_field_order = True
        for char in json.dumps(value):
            self.assertIn(char, parser.get_allowed_characters())
            parser = parser.add_character(char)
        self.assertTrue(parser.can_end())

    def test_decoder_empty_attributes_are_restored_inside_entity_union(self):
        inp = dict(captions=captions(), entities=[], fields=['state'], page_size=3)
        value = report(entity())
        del value['entities'][0]['attributes']
        schema = output_schema('adaptive_extract', inp)
        restore_empty_arrays(value, schema)
        self.assertEqual(value['entities'][0]['attributes'], [])
        validate_output(value, schema)

    def test_schema_forbids_unrequested_attributes_and_foreign_sources(self):
        inp = dict(captions=captions(), entities=[], fields=['color'], page_size=3)
        schema = output_schema('adaptive_extract', inp)
        red = dict(field='color', value='red', source_caption_ids=['C0'])
        validate_output(report(entity(attributes=[red])), schema)
        for bad in (dict(red, field='material'), dict(red, source_caption_ids=['C99'])):
            with self.assertRaises(ValueError):
                validate_output(report(entity(attributes=[bad])), schema)
        inp['fields'] = ['relation']
        with self.assertRaises(ValueError):
            validate_output(report(entity(attributes=[red])), output_schema('adaptive_extract', inp))

    def test_bidirectional_indexes_use_original_text_and_fact_sources(self):
        m = memory()
        commit(m, report(entity()))
        self.assertEqual(m['indexes']['entity_to_captions']['E0'], ['C0'])
        self.assertEqual(m['indexes']['caption_to_records']['C0']['entity_ids'], ['E0'])
        self.assertEqual(m['indexes']['caption_to_records']['C0']['observation_ids'], ['O0'])
        self.assertEqual(m['source_captions']['C0'], captions()[0])

    def test_reload_and_supplement_same_source_preserves_instance_and_observation(self):
        m = memory()
        commit(m, report(entity()))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'memory.json'
            write_json(path, m)
            m = adaptive.load_memory(path, 'v', captions(), 'extract-v1')
        red = dict(field='color', value='red', source_caption_ids=['C0'])
        commit(m, report(entity('E0', attributes=[red])), key='color')
        self.assertEqual(list(m['entities']), ['E0'])
        self.assertEqual(len(m['observations']), 1)
        self.assertEqual(m['entities']['E0']['attributes'], [red])
        self.assertEqual(len(m['extraction_requests']), 2)
        adaptive.validate_memory(m, 'v', captions(), 'extract-v1')

    def test_foreign_unstated_or_unrelated_facts_fail_atomically(self):
        for attr in (dict(field='color', value='blue', source_caption_ids=['C0']),
                     dict(field='color', value='red', source_caption_ids=['C99'])):
            m = memory()
            before = deepcopy(m)
            with self.assertRaises(ValueError):
                commit(m, report(entity(attributes=[attr])))
            self.assertEqual(m, before)

    def test_same_name_does_not_merge_distinct_instances(self):
        m = memory()
        commit(m, report(entity(), entity()))
        self.assertEqual(len(m['entities']), 2)
        self.assertEqual(m['indexes']['caption_to_records']['C0']['entity_ids'], ['E0', 'E1'])

    def test_relation_indexes_link_both_entities_and_reject_collapsed_endpoints(self):
        m = memory()
        box = entity(cid='C1')
        knife = dict(entity(cid='C1'), label='knife')
        relation = dict(subject='B0', object='B1', predicate='with', source_caption_ids=['C1'])
        commit(m, report(box, knife, relations=[relation]))
        self.assertEqual(m['indexes']['caption_to_records']['C1']['relation_ids'], ['R0'])
        before = deepcopy(m)
        with self.assertRaises(ValueError):
            commit(m, report(relations=[dict(relation, subject='E0', object='E0')]))
        self.assertEqual(m, before)

    def test_late_backfill_keeps_original_time_without_inventing_state_transitions(self):
        m = memory()
        commit(m, report(entity(cid='C1')))
        commit(m, report(entity('E0', cid='C0')), key='earlier')
        context = adaptive.memory_context(m)
        self.assertEqual([s['source_caption_ids'] for s in context['states']], [['C0']])
        self.assertEqual(context['transitions'], [])
        self.assertEqual(len(context['unresolved_observations']), 1)
        self.assertEqual(context['unresolved_observations'][0]['observation']['caption_id'], 'C1')
        self.assertEqual(m['indexes']['entity_to_captions']['E0'], ['C0', 'C1'])

    def test_version_source_and_index_changes_are_rejected(self):
        original = memory()
        commit(original, report(entity()))
        for change in ('video', 'source', 'extractor', 'index'):
            m = deepcopy(original)
            args = ['v', captions(), 'extract-v1']
            if change == 'video': args[0] = 'another-video'
            if change == 'source': args[1][0]['text'] = 'C holds a blue box.'
            if change == 'extractor': args[2] = 'extract-v2'
            if change == 'index': m['indexes']['entity_to_captions']['E0'] = ['C1']
            with self.assertRaises(ValueError):
                adaptive.validate_memory(m, *args)

    def test_pipeline_persists_and_reuses_source_links_across_questions(self):
        caps = captions()
        packet = dict(captions=caps, frame_features=[[1., 0.], [0., 1.]], question=dict(question_id='q1', video_id='v',
            question='What happens to the box?', options=['opens', 'a', 'b', 'c', 'd']))
        directory = dict(captions=caps, scenes=[dict(id='S0', interval=[0, 2], center=[1., 0.],
            caption_ids=['C0', 'C1'], chronological_order=[0, 1], description='box')], pointers=[])
        cfg = load_config()
        cfg['query']['temporal_sparse_visual'] = False
        class Backend:
            def __init__(self):
                self.extracted = False
                self.calls = []
            def call(self, op, inp, timeout):
                self.calls.append((op, deepcopy(inp)))
                if op == 'scene_scope': return dict(data=dict(scope='local', reason='local'))
                if op == 'clip_text': return dict(vectors=[[1., 0.]]*6)
                if op == 'adaptive_next':
                    return dict(data=dict(sufficient=self.extracted, reason='read source', gap='missing field',
                        action='stop' if self.extracted else 'extract', scene_id='stop', caption_ids=[] if self.extracted else ['C0'],
                        entity_ids=[], focus=['box'], fields=['color'] if inp['entities'] else ['state']))
                if op == 'adaptive_extract':
                    self.extracted = True
                    attrs = [dict(field='color', value='red', source_caption_ids=['C0'])] if inp['entities'] else []
                    return dict(data=report(entity('E0' if inp['entities'] else 'new', attributes=attrs)))
                if op == 'temporal_qa_check': return dict(input_tokens=100)
                if op == 'videotree_qa': return dict(data=dict(selected_option='0'))
                raise AssertionError(op)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = Backend()
            first = adaptive.run(packet, directory, [], backend, cfg, root/'first',
                dict(online_seconds=600, max_scene_visits=4), root/'memory.json')
            self.assertIsNone(first['error'])
            backend.extracted = False
            packet['question'].update(question_id='q2', question='What color is the box?')
            second = adaptive.run(packet, directory, [], backend, cfg, root/'second',
                dict(online_seconds=600, max_scene_visits=4), root/'memory.json')
            self.assertIsNone(second['error'])
            self.assertEqual(second['reused_entity_count'], 1)
            self.assertEqual(second['retrieval']['source_index_priority_caption_ids'], ['C0'])
            self.assertEqual(json.loads((root/'memory.json').read_text())['entities']['E0']['attributes'][0]['value'], 'red')
            self.assertNotIn('caption_temporal_observe', [op for op, _ in backend.calls])


class QueueTests(unittest.TestCase):
    def test_idle_gpu_waits_for_source_assets(self):
        from script import run_adaptive_comparison as runner
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with patch.object(runner, 'verify'), patch.object(runner, 'missing_assets', return_value=['model.part']), \
                 patch.object(runner, 'inventory', return_value={'0': dict(uuid='GPU-test', memory_mib=0, utilization=0)}), \
                 patch.object(runner.subprocess, 'Popen') as launch, \
                 patch.object(runner.time, 'sleep', side_effect=[None, None, RuntimeError('end test')]):
                with self.assertRaisesRegex(RuntimeError, 'end test'):
                    runner.queue(out)
                launch.assert_not_called()
                self.assertEqual(json.loads((out/'queue_status.json').read_text())['state'], 'waiting_for_assets')

    def test_busy_gpu_is_left_alone(self):
        from script import run_adaptive_comparison as runner
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with patch.object(runner, 'verify'), patch.object(runner, 'missing_assets', return_value=[]), \
                 patch.object(runner, 'inventory', return_value={'0': dict(uuid='GPU-test', memory_mib=70000, utilization=90)}), \
                 patch.object(runner.subprocess, 'Popen') as launch, \
                 patch.object(runner.time, 'sleep', side_effect=RuntimeError('end test')):
                with self.assertRaisesRegex(RuntimeError, 'end test'):
                    runner.queue(out)
                launch.assert_not_called()
                self.assertEqual(json.loads((out/'queue_status.json').read_text())['state'], 'waiting_for_idle_gpu')

    def test_three_idle_samples_then_launch_frozen_worker_by_uuid(self):
        from script import run_adaptive_comparison as runner
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            process = Mock(pid=123, returncode=0)
            process.poll.side_effect = [None, 0]
            with patch.object(runner, 'verify'), patch.object(runner, 'missing_assets', return_value=[]), \
                 patch.object(runner, 'inventory', return_value={'0': dict(uuid='GPU-unit-test', memory_mib=0, utilization=0)}), \
                 patch.object(runner.subprocess, 'Popen', return_value=process) as launch, \
                 patch.object(runner.time, 'sleep'):
                self.assertEqual(runner.queue(out), 0)
                self.assertEqual(launch.call_args.kwargs['env']['CUDA_VISIBLE_DEVICES'], 'GPU-unit-test')
                self.assertIn(str(out/'source/script/run_adaptive_comparison.py'), launch.call_args.args[0])
                self.assertEqual(json.loads((out/'queue_status.json').read_text())['state'], 'complete')


if __name__ == '__main__':
    unittest.main()
