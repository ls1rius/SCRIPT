from copy import deepcopy
import random
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from script_core import merged_lazy_memory as core
from script_core.config import load_config
from script_core import partial_temporal
from script import controlled_memory_io as io
from tests.test_entity_evidence_qa import Backend as FBackend, fixture
from script_core.budget import ResourceExhausted


class Backend(FBackend):
    """Exercise I's H route without changing the frozen F test backend."""
    def source(self, inputs):
        data = super().source(inputs)
        if 'is wet' in inputs['unit']['text'] and 'states' in inputs['requested_fields']:
            data['states'] = [dict(entity='book', description='wet')]
        if 'was moved' in inputs['unit']['text']:
            data['actions'] = [dict(actor=None, verb='moved', roles=[
                dict(role='patient', value='box'), dict(role='instrument', value='crane')])]
        return data

    def call(self, operation, inputs, *args):
        if not operation.startswith('h_'):
            return super().call(operation, inputs, *args)
        self.calls.append((operation, deepcopy(inputs)))
        if operation == 'h_plan_fields':
            return dict(data=dict(fields=['actions.body_part', 'actions.instrument', 'states']))
        if self.exhausted:
            raise ResourceExhausted('No extraction budget')
        if self.infrastructure:
            raise RuntimeError('CUDA out of memory')
        if operation == 'h_extract_batch':
            return dict(items=[dict(data=self.source(i)) for i in inputs['requests']])
        if operation == 'h_extract_fields':
            return dict(data=self.source(inputs))
        raise AssertionError(operation)


def row(time, text, **evidence):
    return dict(caption_id=f'C{time}', frame_index=time, sample_interval=[time,time+1],
                evidence=dict(original_text=text, **evidence))


class MergedLazyMemoryTests(unittest.TestCase):
    def test_adjacent_intervals_compact_but_gaps_and_other_bodies_remain(self):
        payload = dict(observations=[row(165,'C walks.'),row(166,'C walks.'),
            row(167,'C stops.'),row(168,'C walks.'),row(169,'C walks.')])
        memory = core.materialize(payload)
        text, audit = core.render(memory)
        self.assertEqual(text, 'Frames [165,167), [168,170): C walks.\nFrame [167,168): C stops.')
        self.assertEqual(audit['rows'][0]['sample_intervals'],
                         [[165,166],[166,167],[168,169],[169,170]])
        self.assertEqual(audit['rows'][0]['display_intervals'], [[165,167],[168,170]])
        self.assertEqual(audit['rows'][0]['caption_ids'], ['C165','C166','C168','C169'])
        self.assertEqual(core.restore(memory), payload)
        self.assertEqual(audit['version'], core.RENDER_VERSION)

    def test_h_open_states_and_actorless_actions_survive_merge_and_restore(self):
        evidence = dict(states=[dict(entity='server', description='unreachable')],
                        actions=[dict(verb='moved', patient='box', instrument='crane')])
        payload = dict(observations=[dict(caption_id=f'C{i}', frame_index=i,
            sample_interval=[i,i+1], evidence=deepcopy(evidence)) for i in [0,10]])
        memory = core.materialize(payload)
        text, audit = core.render(memory)
        self.assertEqual(core.restore(memory), payload)
        self.assertEqual(memory['parent_memory_variant'], 'H')
        self.assertEqual(audit['rendered_rows'], 1)
        self.assertIn('description="unreachable"', text)
        self.assertNotIn('actor=', text)
        self.assertNotIn('dimension=', text)

    def test_legacy_fixed_states_and_i_v1_cannot_be_relabelled_as_h(self):
        payload = dict(observations=[row(0,'book wet', states=[
            dict(entity='book', dimension='wetness', value='wet')])])
        with self.assertRaisesRegex(ValueError, 'H open states'):
            core.materialize(payload)
        memory = core.materialize(dict(observations=[row(0,'book wet')]))
        memory['schema'] = 'merged-lazy-memory-I-v1'
        with self.assertRaisesRegex(ValueError, 'version'):
            core.validate(memory)

    def test_nonconsecutive_repeats_keep_all_times_and_restore_source_order(self):
        payload = dict(observations=[row(0,'C mixes flour.'),row(1,'C washes hands.'),
                                     row(100,'C mixes flour.')])
        before = deepcopy(payload)
        memory = core.materialize(payload)
        text, audit = core.render(memory)
        self.assertEqual(text, 'Frames [0,1), [100,101): C mixes flour.\nFrame [1,2): C washes hands.')
        self.assertEqual(core.restore(memory),before)
        self.assertEqual(payload,before)
        self.assertEqual(audit['covered_source_rows'],3)
        self.assertEqual(audit['rendered_rows'],2)
        self.assertFalse(audit['continuous_duration_inferred'])
        self.assertEqual(audit['rows'][0]['caption_ids'],['C0','C100'])

    def test_only_visible_body_is_factored_without_exposing_hidden_facts(self):
        payload = dict(observations=[row(0,'C touches the book.',
            actions=[dict(actor='C',verb='touches',patient='book')]),
            row(2,'C touches the book.',actions=[dict(actor='C',verb='opens',patient='door')])])
        memory = core.materialize(payload)
        text, audit = core.render(memory)
        self.assertEqual(audit['rendered_rows'],1)
        self.assertNotIn('door',text)
        self.assertEqual(core.restore(memory),payload)

    def test_change_and_return_are_discrete_observations_not_persistence(self):
        payload = dict(observations=[row(0,'door closed'),row(1,'door open'),row(90,'door closed')])
        text,audit = core.compact_description(payload)
        self.assertIn('Frames [0,1), [90,91): door closed',text)
        self.assertIn('Frame [1,2): door open',text)
        self.assertNotIn('[0,91)',text)
        self.assertFalse(audit['identities_merged'])
        self.assertFalse(audit['event_counts_inferred'])

    def test_negation_actor_tool_destination_and_wording_remain_distinct(self):
        values=['C opens door.','C never opens door.','X opens door.',
                'C opens door with key.','C puts book on shelf.',
                'C puts book on floor.','C moves book.','C shifts book.']
        text,audit = core.compact_description(dict(observations=[row(i,v) for i,v in enumerate(values)]))
        self.assertEqual(audit['rendered_rows'],len(values))
        for value in values:self.assertIn(value,text)

    def test_multiline_fallback_and_fact_field_order_round_trip(self):
        payload = dict(observations=[row(0,'C touches book.\nSecond original clause.'),
            row(2,'C touches book.\nSecond original clause.'),
            dict(caption_id='C3',frame_index=3,sample_interval=[3,4],
                 evidence=dict(actions=[dict(verb='moves',actor='C',patient='book')]))])
        memory=core.materialize(payload)
        text,audit=core.render(memory)
        self.assertIn('Second original clause.\nFrame [3,4):',text)
        self.assertIn('verb="moves"; actor="C"',text)
        self.assertEqual(audit['rows'][1]['prompt_line'],3)
        self.assertEqual(core.restore(memory),payload)

    def test_tampered_support_body_policy_and_source_are_rejected(self):
        payload=dict(observations=[row(0,'same'),row(1,'other'),row(2,'same')])
        original=core.materialize(payload)
        for change in ['drop','duplicate','body','policy','time']:
            memory=deepcopy(original)
            if change=='drop':memory['groups'][0]['observation_indices'].pop()
            if change=='duplicate':memory['groups'][1]['observation_indices']=[0,1]
            if change=='body':memory['groups'][0]['body']='different'
            if change=='policy':memory['policy']['continuous_duration_inferred']=True
            if change=='time':memory['observations'][0]['sample_interval']=[5,6]
            with self.subTest(change=change),self.assertRaises(ValueError):core.validate(memory,payload)

    def test_empty_and_invalid_source_payloads_fail(self):
        for payload in [dict(observations=[]),dict(observations=[row(0,'x'),row(0,'x')])]:
            with self.assertRaises(ValueError):core.materialize(payload)

    def test_mixed_sample_sequences_preserve_every_observation(self):
        rng=random.Random(91)
        for _ in range(25):
            times=sorted(rng.sample(range(500),40))
            source=dict(observations=[row(t,rng.choice(['C mixes.','C waits.','X moves.','C never mixes.'])) for t in times])
            memory=core.materialize(source)
            self.assertEqual(core.restore(memory),source)
            _,audit=core.render(memory)
            coverage=[c for group in audit['rows'] for c in group['caption_ids']]
            self.assertCountEqual(coverage,[r['caption_id'] for r in source['observations']])


class OnlineMergedLazyTests(unittest.TestCase):
    def test_live_h_schema_prompt_and_batch_dispatch_use_h_rules(self):
        from script_core import generalized_lazy_memory as h, schemas, prompts
        from script_core.backend import LocalModels
        inputs=dict(unit=dict(id='U0',text='The box was moved with a crane.'),
                    requested_fields=['actions.instrument','states'],source_type='caption')
        schema=schemas.output_schema(h.EXTRACT,inputs)
        self.assertEqual(set(schema['properties']['states']['items']['properties']),{'entity','description'})
        self.assertEqual(prompts.CONTRACTS[h.EXTRACT],h.EXTRACT_PROMPT)
        self.assertEqual(prompts.CONTRACTS[h.PLAN],h.PLAN_PROMPT)
        plan=schemas.output_schema(h.PLAN,{})['properties']['fields']['items']['enum']
        self.assertIn('states',plan)
        self.assertFalse(any(f.startswith('states.') for f in plan))
        captured=[]
        class Batch:
            def __init__(self,cfg,runtime,models):captured.append(runtime)
            def generate(self,requests):
                return [dict(data={},usage=dict(input_tokens=13,output_tokens=7))]
        models=LocalModels(load_config())
        with patch('script_core.batch_extraction.BatchExtractor',Batch):
            output=models.call('h_extract_batch',dict(requests=[inputs]))
        self.assertIs(captured[0].memory,h)
        self.assertEqual(output['usage'],dict(input_tokens=13,output_tokens=7))
        self.assertEqual(models.model_tokens['vlm'],13)
        with self.assertRaises(ValueError):
            models.call('h_extract_batch',dict(requests=[inputs],question='must not leak'))

    def test_online_i_merges_but_preserves_fixed_regions_and_source_blindness(self):
        packet,bundle=fixture()
        with tempfile.TemporaryDirectory() as td:
            backend=Backend()
            result=partial_temporal.run(packet,bundle,backend,load_config('configs/script_I.yaml'),td,
                                         dict(online_seconds=1500))
            self.assertIsNone(result['error'],result['error'])
            self.assertEqual(result['memory_variant'],'I')
            audit=result['qa_evidence']
            self.assertEqual(audit['visible_source_rows'],3)
            self.assertEqual(audit['rendered_rows'],2)
            self.assertIn('/I/h_script.sqlite3',audit['database'])
            self.assertEqual(audit['parent_memory_variant'],'H')
            self.assertEqual(audit['state_representation'],'entity_description')
            self.assertFalse(audit['fixed_state_dimensions'])
            self.assertTrue(any(op=='h_plan_fields' for op,_ in backend.calls))
            self.assertFalse(any(op.startswith('f_') for op,_ in backend.calls))
            answers=[x for op,x in backend.calls if op=='videotree_qa']
            self.assertEqual(len(answers),1)
            self.assertEqual(io.fixed(answers[0]),io.fixed(bundle['qa_input']))
            self.assertIn('Frames [1,3):',io.parts(answers[0])[1])
            self.assertNotIn('Unread secret',io.parts(answers[0])[1])
            for op,request in backend.calls:
                if op in ('h_extract_fields','h_extract_batch'):
                    requests = request['requests'] if op=='h_extract_batch' else [request]
                    for item in requests:
                        self.assertNotIn('question',item)
                        self.assertNotIn('options',item)
            self.assertTrue((Path(td)/'merged_lazy_memory.json').exists())
            self.assertTrue((Path(td)/'generalized_lazy_memory.json').exists())

    def test_online_i_preserves_legacy_i_cache_and_h_unknown_actor_storage(self):
        packet,bundle=fixture(selected=(0,))
        packet['captions'][0]['text']='#C The box was moved with a crane.'
        # Rebuild the upstream fixture after changing the source text.
        from script_core import adaptive_transition as partial
        from script_core.config import digest
        from script_core.temporal_pipeline import qa_context_inputs
        bundle['memory']=partial.new_memory('v',packet['captions'],'source')
        partial.rebuild_indexes(bundle['memory'])
        bundle['packet_hash']=digest(packet)
        bundle['qa_input']=qa_context_inputs(dict(packet['question'],id='q'),packet['captions'],[0],
            partial.memory_context(bundle['memory']),visual_observations=bundle['supplemental_visual']['observations'])
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);legacy=root/'cache/I/script.sqlite3';legacy.parent.mkdir(parents=True)
            legacy.write_bytes(b'legacy F-derived I database must stay untouched')
            backend=Backend()
            result=partial_temporal.run(packet,bundle,backend,load_config('configs/script_I.yaml'),root/'query',
                dict(online_seconds=1500,memory_cache_directory=str(root/'cache')))
            self.assertIsNone(result['error'],result['error'])
            self.assertEqual(legacy.read_bytes(),b'legacy F-derived I database must stay untouched')
            memory=io.read(root/'query/generalized_lazy_memory.json')
            action=memory['observations'][0]['evidence']['actions'][0]
            self.assertNotIn('actor',action)
            self.assertEqual(action['instrument'],'crane')

    def test_i_reuses_own_cache_and_does_not_write_f_database(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);backend=Backend();cfg=load_config('configs/script_I.yaml')
            settings=dict(online_seconds=1500,memory_cache_directory=str(root/'cache'))
            for i,selected in enumerate([(0,1,2),(1,2)]):
                packet,bundle=fixture(str(i),selected=selected)
                result=partial_temporal.run(packet,bundle,backend,cfg,root/str(i),settings)
                self.assertIsNone(result['error'],result['error'])
            self.assertEqual(result['qa_evidence']['cache']['missing_fields'],0)
            self.assertFalse((root/'cache/F').exists())

    def test_i_overflow_and_extraction_failures_do_not_switch_modes_or_retry(self):
        for flag in ['overflow','infrastructure','exhausted']:
            packet,bundle=fixture();backend=Backend(**{flag:True})
            with self.subTest(flag=flag),tempfile.TemporaryDirectory() as td:
                result=partial_temporal.run(packet,bundle,backend,load_config('configs/script_I.yaml'),td,
                                            dict(online_seconds=1500))
                if flag=='exhausted':
                    self.assertIsNone(result['error'],result['error'])
                    self.assertEqual(result['qa_evidence']['visible_source_rows'],3)
                    self.assertEqual(len([op for op,_ in backend.calls if op=='videotree_qa']),1)
                else:
                    self.assertIsNotNone(result['error'])
                    self.assertFalse(any(op=='videotree_qa' for op,_ in backend.calls))

    def test_cli_preserves_explicit_i_with_l_default(self):
        import script_qa
        class Stop(Exception):pass
        def preflight(cfg):
            self.assertEqual(cfg['query']['temporal_qa_evidence'],'merged_lazy_script')
            raise Stop()
        with patch.object(script_qa,'preflight',side_effect=preflight):
            for flags in (['--memory','I'], ['--config','configs/script_I.yaml']):
                with self.subTest(flags=flags), self.assertRaises(Stop):
                    script_qa.main(['query','--base','unused','--questions','unused','--out','unused'] + flags)
        self.assertEqual(load_config()['query']['temporal_qa_evidence'],'planned_lazy_script')


if __name__=='__main__':unittest.main()
