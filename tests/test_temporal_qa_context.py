"""Final temporal QA respects context limits without truncating source captions."""
from copy import deepcopy
import json
import unittest

from script_core.budget import ToolFailure
from script_core.compatibility import qa_inputs
from script_core.temporal_pipeline import compact_context, fit_qa_context


class CheckingMeter:
    def __init__(self,limit,error=None):
        self.limit=limit;self.error=error;self.inputs=[]

    def call(self,operation,inputs,**kwargs):
        self.inputs.append(deepcopy(inputs))
        if self.error:raise ToolFailure(self.error)
        size=len(inputs['prompts'][0])
        if size>self.limit:
            raise ToolFailure('ValueError: Original QA plus retrieved entity context exceeds input allowance')
        return dict(input_tokens=size),{}


class TemporalQAContextTests(unittest.TestCase):
    def setUp(self):
        self.query=dict(id='q',question='Where is the box?',options=['a','b','c','d','e'])
        self.caps=[dict(id=f'C{i}',interval=[i,i+1],text=f'ORIGINAL CAPTION {i}') for i in range(2)]
        self.context=dict(
            entities=[dict(id=f'E{i}',label='box',attributes=[dict(field='appearance',value='x'*1700,observed_at=[0])]) for i in range(2)],
            states=[dict(id=f'T{i}',entity_id=f'E{i}',relation_ids=['R0']) for i in range(2)],
            transitions=[dict(before='T0',after='T1')],
            relations=[dict(id='R0',subject='E0',object='E1')],
            lineage=[dict(child='E1',parent='E0')],
            context_selection=dict(total_entities=2,selected_entities=2,total_states=2,selected_states=2),
            evidence_policy='sampled assertions')
        self.base=qa_inputs(self.query,self.caps,[0,1])

    def test_overflow_removes_whole_entity_and_dependent_edges_with_explicit_counts(self):
        before=deepcopy(self.context)
        meter=CheckingMeter(len(self.base['prompts'][0])+3500)
        inp,view,tokens=fit_qa_context(self.query,self.caps,[0,1],self.context,meter)
        self.assertLessEqual(tokens,meter.limit)
        self.assertEqual([e['id'] for e in view['entities']],['E0'])
        self.assertEqual([s['id'] for s in view['states']],['T0'])
        self.assertEqual(view['states'][0]['relation_ids'],[])
        for field in ('relations','transitions','lineage'):self.assertEqual(view[field],[])
        self.assertEqual(view['context_selection']['token_budget_omitted_entity_ids'],['E1'])
        self.assertEqual(view['context_selection']['selected_entities'],1)
        self.assertEqual(self.context,before)
        self.assertEqual(inp['prompts'][0].split('\n\nTemporal entity memory')[0],self.base['prompts'][0])
        self.assertEqual(inp['head'],self.base['head'])

    def test_other_worker_failures_are_not_hidden_as_context_overflow(self):
        meter=CheckingMeter(999999,error='CUDA worker failed')
        with self.assertRaisesRegex(ToolFailure,'CUDA worker failed'):
            fit_qa_context(self.query,self.caps,[0,1],self.context,meter)
        self.assertEqual(len(meter.inputs),1)

    def test_visual_supplement_survives_entity_context_pruning(self):
        fact=dict(description='Visible supplemental detail',source_frame_ids=['F0'],interval=[0,0])
        meter=CheckingMeter(len(self.base['prompts'][0])+3900)
        inp,view,tokens=fit_qa_context(self.query,self.caps,[0,1],self.context,meter,visual_observations=[fact])
        self.assertIn('Visible supplemental detail',inp['prompts'][0])
        self.assertTrue(inp['prompts'][0].startswith(self.base['prompts'][0]))
        self.assertLessEqual(tokens,meter.limit)

    def test_caption_only_overflow_fails_without_truncation(self):
        meter=CheckingMeter(0)
        with self.assertRaises(ToolFailure):
            fit_qa_context(self.query,self.caps,[0,1],self.context,meter,use_memory=False)
        self.assertEqual(meter.inputs,[self.base])

    def test_even_empty_memory_cannot_trigger_silent_caption_truncation(self):
        meter=CheckingMeter(0)
        with self.assertRaises(ToolFailure):
            fit_qa_context(self.query,self.caps,[0,1],self.context,meter)
        self.assertEqual(len(meter.inputs),3)
        for inp in meter.inputs:
            self.assertTrue(inp['prompts'][0].startswith(self.base['prompts'][0]))

    def test_compaction_factors_attributes_but_keeps_all_observation_times(self):
        from script_core.temporal_entity import new_network,ingest,entity_context
        from tests.test_temporal_entity import row,report,frames
        net=new_network()
        ingest(net,report(row()),frames(0,2),'S0','b0',[])
        ingest(net,report(row('E0',1,'same')),frames(0,2),'S0','b1',['E0'])
        value=entity_context(net,self.query);before=deepcopy(value)
        compact=compact_context(value)
        self.assertEqual(len(compact['entities'][0]['attributes']),1)
        self.assertEqual(compact['entities'][0]['attributes'][0]['observed_at'],[0,1])
        self.assertEqual(value,before)

    def test_bounded_context_has_no_dangling_relations_and_keeps_source_network(self):
        from script_core.temporal_entity import new_network,ingest,entity_context
        from script_core.aligned_pipeline import context as aligned_context
        from tests.test_temporal_entity import row,report,frames
        net=new_network()
        relation=dict(subject='B0',object='B1',predicate='on',frame_id='F0',
                      update='present',source_frame_ids=['F0'])
        ingest(net,report(row(),row(value='blue'),relations=[relation]),frames(0,2),'S0','b0',[])
        net['lineage'].append(dict(child='E1',parent='E0',relation='part_of'))
        before=deepcopy(net)
        for view in [entity_context(net,self.query,limit=1),aligned_context(net,['E0'])]:
            self.assertEqual(view['relations'],[])
            self.assertEqual(view['lineage'],[])
            self.assertEqual(view['states'][0]['relation_ids'],[])
        self.assertEqual(net,before)


if __name__=='__main__':unittest.main()
