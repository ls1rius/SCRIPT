"""Semantic categories survive assembly and support audited category ablations."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from script_core.config import load_config, validate_config
from script_core.memory_evidence import prepare_memory_input, qa_inputs
from script_core.typed_memory import attach_observation_types, typed_context, INFORMATION_TYPES
from script_core import partial_temporal
from tests.test_partial_temporal import fixture, Backend


class TypedMemoryTests(unittest.TestCase):
    def setUp(self):
        self.captions=[dict(id=f'C{i}',interval=[i,i+1],text=t) for i,t in enumerate([
            'The book is red and dusty.', 'C wipes the book with a cloth.',
            'The book is clean.', 'C places the book on a shelf.', 'UNREAD C leaves.'])]
        self.view=dict(source_type='caption',
            entities=[dict(id='E0',label='book',scene_ids=['S0'],attributes=[dict(field='color',
                value='red',source_caption_ids=['C0'],observed_at=[0])])],
            states=[dict(id=f'T{i}',entity_id='E0',scene_id='S0',description=self.captions[i]['text'],
                source_caption_ids=[f'C{i}'],relation_ids=[]) for i in range(4)],
            transitions=[dict(id='X0',entity_id='E0',before='T0',after='T2',
                cause=dict(action='wipes',actor='C',tool='cloth',basis='inferred_action',
                    source_caption_ids=['C1']),status='text_inferred')],
            relations=[],identities=[],lineage=[],context_selection={},
            source_observations=[dict(id=f'O{i}',entity_id='E0',caption_id=f'C{i}',
                kind=k,status='caption_asserted') for i,k in enumerate(['state','action','state'])])

    def test_action_is_not_mistaken_for_state_just_because_of_its_storage_container(self):
        before=deepcopy(self.view)
        typed,visual,audit=typed_context(self.captions,[0,1,2,3],self.view)
        self.assertEqual(len(typed['actions']),1)
        self.assertEqual(len(typed['states']),2)
        self.assertEqual(typed['actions'][0]['source_caption_ids'],['C1'])
        self.assertEqual(typed['actions'][0]['roles']['actor'],None)
        self.assertEqual(len(typed['unclassified']),1)
        self.assertIn('shelf',typed['unclassified'][0]['description'])
        self.assertTrue(all('description' not in slot for slot in typed['temporal_slots']))
        self.assertEqual(self.view,before)

    def test_attributes_are_atomic_and_keep_field_source_and_observation_time(self):
        typed,_,audit=typed_context(self.captions,[0,1,2,3],self.view)
        attr=typed['attributes'][0]
        self.assertEqual((attr['field'],attr['value'],attr['observed_at']),('color','red',[0]))
        self.assertEqual(attr['source_caption_ids'],['C0'])
        self.assertNotIn('attributes',typed['entities'][0])
        self.assertEqual(audit['attribute_fields'],{'color':1})

    def test_cause_is_separate_from_transition_and_keeps_inference_status(self):
        typed,_,_=typed_context(self.captions,[0,1,2,3],self.view)
        self.assertEqual(typed['transitions'][0]['transition_kind'],'state_change')
        self.assertNotIn('cause',typed['transitions'][0])
        cause=typed['causes'][0]
        self.assertEqual(cause['tool'],'cloth')
        self.assertEqual(cause['status'],'text_inferred')
        self.assertEqual(typed['transitions'][0]['cause_id'],cause['id'])

    def test_action_to_action_transition_is_a_sequence_not_a_physical_state_change(self):
        self.view['transitions'][0].update(before='T1',after='T2')
        typed,_,_=typed_context(self.captions,[0,1,2,3],self.view)
        self.assertEqual(typed['transitions'][0]['transition_kind'],'action_sequence')

    def test_category_exclusion_removes_records_quotes_and_matching_visual_type(self):
        self.view['transitions']=[]
        visual=[dict(kind='action',description='VISUAL C wipes.',source_frame_ids=['F1']),
                dict(kind='state',description='VISUAL book is clean.',source_frame_ids=['F2'])]
        included=[t for t in INFORMATION_TYPES if t!='action']
        typed,vis,audit=prepare_memory_input(self.captions,[0,1,2,3],self.view,visual,
            mode='typed_memory_quotes',included_types=included)
        inp=qa_inputs(dict(id='q',question='Task?',options=['a','b','c','d','e']),typed,audit,vis)
        self.assertEqual(typed['actions'],[])
        self.assertNotIn('C1',audit['quoted_caption_ids'])
        self.assertNotIn('C wipes the book with a cloth.',inp['prompts'][0])
        self.assertNotIn('VISUAL C wipes.',inp['prompts'][0])
        self.assertIn('VISUAL book is clean.',inp['prompts'][0])
        self.assertNotIn('available_caption_counts',inp['prompts'][0])

    def test_shared_sources_across_categories_are_explicit_in_ablation_audit(self):
        self.view['transitions'][0].update(before='T1',after='T2')
        included=[t for t in INFORMATION_TYPES if t!='action']
        typed,vis,audit=prepare_memory_input(self.captions,[0,1,2,3],self.view,
            mode='typed_memory_quotes',included_types=included)
        self.assertEqual(typed['actions'],[])
        overlap=audit['information_types']['retained_sources_overlapping_excluded_types']
        self.assertIn('C1',overlap)
        self.assertIn('transition',overlap['C1'])
        self.assertIn('action',overlap['C1'])

    def test_missing_original_kind_remains_unclassified(self):
        self.view.pop('source_observations')
        typed,_,_=typed_context(self.captions,[0,1,2,3],self.view)
        self.assertEqual(typed['actions'],[])
        self.assertEqual(typed['states'],[])
        self.assertEqual(len(typed['unclassified']),4)
        self.assertEqual(typed['transitions'][0]['transition_kind'],'unclassified_change')

    def test_unread_type_records_are_rejected(self):
        self.view['source_observations'][0]['caption_id']='C4'
        with self.assertRaisesRegex(ValueError,'unread or unknown'):
            typed_context(self.captions,[0,1,2,3],self.view)

    def test_original_tags_follow_canonical_identity_without_mutating_storage(self):
        packet,bundle=fixture();memory=partial_temporal.project(bundle['memory'])
        view=partial_temporal.context(memory,bundle['memory'],dict(question='',options=[]))
        before=deepcopy(memory)
        tagged=attach_observation_types(view,memory)
        self.assertEqual(len(tagged['source_observations']),5)
        self.assertTrue(all(o['kind']=='state' for o in tagged['source_observations']))
        self.assertEqual(memory,before)

    def test_tags_from_merged_instances_keep_canonical_and_original_entity_ids(self):
        _,bundle=fixture();memory=partial_temporal.project(bundle['memory'])
        memory['temporal_network']['aliases']['E1']='E0'
        view=dict(entities=[dict(id='E0',label='Alice')])
        tagged=attach_observation_types(view,memory)
        merged=[o for o in tagged['source_observations'] if o['original_entity_id']=='E1']
        self.assertEqual(len(merged),2)
        self.assertTrue(all(o['entity_id']=='E0' for o in merged))

    def test_type_ablation_with_no_retained_facts_records_evidence_gap(self):
        packet,bundle=fixture();backend=Backend();cfg=load_config('configs/script_typed_memory.yaml')
        cfg['query']['temporal_qa_information_types']=[]
        with tempfile.TemporaryDirectory() as folder:
            result=partial_temporal.run(packet,bundle,backend,cfg,folder,dict(online_seconds=1500))
        self.assertIsNone(result['error'],result['error'])
        self.assertTrue(result['qa_evidence']['no_retained_information'])
        self.assertTrue(any(x['stage']=='final_qa_evidence' for x in result['incomplete']))

    def test_live_pipeline_preserves_tags_and_generates_only_one_answer(self):
        packet,bundle=fixture();bundle['memory']['observations']['O1']['kind']='action'
        backend=Backend();before=deepcopy(bundle)
        with tempfile.TemporaryDirectory() as folder:
            result=partial_temporal.run(packet,bundle,backend,load_config('configs/script_typed_memory.yaml'),
                folder,dict(online_seconds=1500))
            context=json.loads((Path(folder)/'qa_context.json').read_text())
        self.assertIsNone(result['error'],result['error'])
        self.assertEqual(bundle,before)
        self.assertEqual(len(context['final_memory']['actions']),1)
        self.assertEqual(context['evidence_selection']['information_types']['retained_caption_counts']['action'],1)
        self.assertEqual(sum(op=='videotree_qa' for op,_ in backend.calls),1)
        self.assertTrue(result['method'].endswith('-typed-memory-quotes-v1'))
        self.assertTrue(all(not inp.get('frames') for op,inp in backend.calls))

    def test_type_filter_requires_typed_mode_and_rejects_unknown_or_duplicate_types(self):
        cfg=load_config('configs/script_typed_memory.yaml')
        for included in [['action','action'],['imagined_goal'],'action']:
            bad=deepcopy(cfg);bad['query']['temporal_qa_information_types']=included
            with self.assertRaises(ValueError):validate_config(bad)
        cfg['query']['temporal_qa_evidence']='memory_quotes'
        cfg['query']['temporal_qa_information_types']=['action']
        with self.assertRaisesRegex(ValueError,'filtering requires'):
            validate_config(cfg)


if __name__=='__main__':unittest.main()
