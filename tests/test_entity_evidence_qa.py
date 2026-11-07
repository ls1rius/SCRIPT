"""E/F must run through the ordinary entry, preserve evidence, and answer once."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from script import controlled_memory_io as io
from script_core import adaptive_transition as partial, partial_temporal
from script_core.budget import ToolFailure, ResourceExhausted
from script_core.config import load_config, validate_config, digest
from script_core.temporal_pipeline import qa_context_inputs
from tests.test_generalized_entity_memory import report


def fixture(qid='q', question='Which hand is used?', selected=(0,1,2)):
    captions=[dict(id=f'C{i}',interval=[i,i+1],text=text) for i,text in enumerate([
        '#C C moves the red book with her left hand.','#C The book is wet.',
        '#C The book is wet.','#C Unread secret text.'])]
    packet=dict(captions=captions,question=dict(question_id=qid,video_id='v',question=question,
        options=['left hand','right hand','both','neither','unknown']))
    memory=partial.new_memory('v',captions,'source');partial.rebuild_indexes(memory)
    visual=[dict(description='An accepted visual observation.',source_frame_ids=['F0'],interval=[0,0])]
    qa=qa_context_inputs(dict(packet['question'],id=qid),captions,list(selected),
        partial.memory_context(memory),visual_observations=visual)
    bundle=dict(packet_hash=digest(packet),memory=memory,selected_seconds=list(selected),opened_scenes=[],
        supplemental_visual=dict(observations=visual,frame_ids=['F0']),source_incomplete=[],preparation_seconds=12.,qa_input=qa)
    return packet,bundle


class Backend:
    def __init__(self,overflow=False,exhausted=False,infrastructure=False):
        self.calls=[];self.overflow=overflow;self.exhausted=exhausted;self.infrastructure=infrastructure
    def source(self,inputs):
        text=inputs['unit']['text'];fields=inputs.get('requested_fields')
        data=report()
        if 'moves' in text:
            roles=[dict(role='patient',value='red book')]
            if fields is None or 'actions.body_part' in fields:roles.append(dict(role='body_part',value='left hand'))
            if fields is None or any(f.startswith('actions.') for f in fields):
                data['actions']=[dict(actor='C',verb='moves',roles=roles)]
        if 'is wet' in text and (fields is None or 'states.wetness' in fields):
            data['states']=[dict(entity='book',dimension='wetness',value='wet')]
        return data
    def call(self,operation,inputs,*args):
        self.calls.append((operation,deepcopy(inputs)))
        if operation=='f_plan_fields':return dict(data=dict(fields=['actions.body_part','states.wetness']))
        if operation in ('e_joint_extract','f_extract_fields','f_extract_batch'):
            if self.exhausted:raise ResourceExhausted('No extraction budget')
            if self.infrastructure:raise RuntimeError('CUDA out of memory')
            if operation=='f_extract_batch':return dict(items=[dict(data=self.source(i)) for i in inputs['requests']])
            return dict(data=self.source(inputs))
        if operation=='temporal_qa_check':
            if self.overflow:raise ToolFailure('Original QA plus retrieved entity context exceeds input allowance')
            return dict(input_tokens=250)
        if operation=='videotree_qa':return dict(data=dict(selected_option='0'))
        raise AssertionError(operation)


class EntityEvidenceQATests(unittest.TestCase):
    def test_python_cli_profiles_and_explicit_controls_agree(self):
        for path in [None,'configs/script.yaml','configs/script_local.yaml','configs/script_L.yaml']:
            self.assertEqual(load_config(path)['query']['temporal_qa_evidence'],'planned_lazy_script')
        self.assertEqual(load_config('configs/script_I.yaml')['query']['temporal_qa_evidence'],'merged_lazy_script')
        self.assertEqual(load_config('configs/script_F.yaml')['query']['temporal_qa_evidence'],'lazy_script')
        self.assertEqual(load_config('configs/script_E.yaml')['query']['temporal_qa_evidence'],'joint_entities')
        self.assertEqual(load_config('configs/script_D.yaml')['query']['temporal_qa_evidence'],'state_segments')
        self.assertEqual(load_config('configs/script_temporal_full.yaml')['query']['temporal_qa_evidence'],'full')
        for config in Path('configs').glob('*.yaml'):load_config(config)
        for key,value in [('entity_max_identity_gap_seconds',True),('field_extraction_batch_size',3),('memory_cache_directory',[])]:
            cfg=load_config();cfg['query'][key]=value
            with self.assertRaises(ValueError):validate_config(cfg)

    def test_both_modes_preserve_fixed_regions_sources_and_one_answer(self):
        packet,bundle=fixture();before=deepcopy(bundle)
        for variant,mode,filename in [('E','joint_entities','joint_entity_memory.json'),('F','lazy_script','lazy_script_memory.json')]:
            with self.subTest(variant=variant),tempfile.TemporaryDirectory() as td:
                backend=Backend();cfg=load_config(f'configs/script_{variant}.yaml')
                result=partial_temporal.run(packet,bundle,backend,cfg,td,dict(online_seconds=1500))
                self.assertIsNone(result['error'],result['error'])
                self.assertEqual(result['memory_variant'],variant)
                self.assertEqual(result['qa_evidence']['mode'],mode)
                self.assertEqual(result['qa_evidence']['source_coverage']['covered_captions'],3)
                self.assertTrue((Path(td)/filename).exists())
                answers=[inp for op,inp in backend.calls if op=='videotree_qa']
                self.assertEqual(len(answers),1);self.assertEqual(io.fixed(answers[0]),io.fixed(bundle['qa_input']))
                self.assertNotIn('Unread secret',io.parts(answers[0])[1])
                source_inputs=[inp for op,inp in backend.calls if op in ('e_joint_extract','f_extract_fields')]
                source_inputs += [item for op,inp in backend.calls if op=='f_extract_batch' for item in inp['requests']]
                self.assertTrue(source_inputs)
                for inputs in source_inputs:
                    self.assertNotIn('question',inputs);self.assertNotIn('options',inputs)
                    self.assertEqual(set(inputs['unit']),{'id','text'})
                self.assertEqual(bundle,before)
                if variant=='F':
                    self.assertTrue(any(op=='f_plan_fields' for op,_ in backend.calls))
                    self.assertTrue(any(op=='f_extract_batch' for op,_ in backend.calls))
                    body=io.parts(answers[0])[1];self.assertIn('Frame [1,2)',body);self.assertIn('Frame [2,3)',body)
                    self.assertIn('C moves red book with left hand.', body)
                    self.assertIn('#C The book is wet.', body)
                    self.assertNotIn('actor=', body)
                    self.assertNotIn('State:', body)
                    archive=json.loads((Path(td)/filename).read_text())
                    self.assertEqual(archive['observations'][0]['evidence']['actions'][0]['body_part'],'left hand')
                    self.assertEqual(result['qa_evidence']['fact_presentation'],'sentences')
                else:self.assertFalse(any(op.startswith('f_') for op,_ in backend.calls))

    def test_f_reuses_fields_across_questions_without_leaking_unselected_sources(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);settings=dict(online_seconds=1500,memory_cache_directory=str(root/'shared'))
            cfg=load_config('configs/script_F.yaml');backend=Backend()
            for qid,question,selected in [('q0','Which hand?',(0,1,2)),('q1','What is wet?',(1,2))]:
                packet,bundle=fixture(qid,question,selected)
                result=partial_temporal.run(packet,bundle,backend,cfg,root/qid,settings)
                self.assertIsNone(result['error'],result['error'])
                if qid=='q0':count=len([op for op,_ in backend.calls if op in ('f_extract_fields','f_extract_batch')])
            self.assertEqual(count,len([op for op,_ in backend.calls if op in ('f_extract_fields','f_extract_batch')]))
            self.assertEqual(result['qa_evidence']['cache']['missing_fields'],0)
            qa=[inp for op,inp in backend.calls if op=='videotree_qa'][-1]
            self.assertNotIn('moves',io.parts(qa)[1]);self.assertNotIn('Unread secret',io.parts(qa)[1])

    def test_overflow_fails_without_fallback_or_answer_and_infrastructure_does_not_retry(self):
        packet,bundle=fixture()
        for variant in ['E','F']:
            for flag in ['overflow','infrastructure']:
                with self.subTest(variant=variant,flag=flag),tempfile.TemporaryDirectory() as td:
                    backend=Backend(**{flag:True})
                    result=partial_temporal.run(packet,bundle,backend,load_config(f'configs/script_{variant}.yaml'),td,dict(online_seconds=1500))
                    self.assertIsNotNone(result['error']);self.assertFalse(any(op=='videotree_qa' for op,_ in backend.calls))
                    if flag=='infrastructure':
                        self.assertEqual(sum(op in ('e_joint_extract','f_extract_fields','f_extract_batch') for op,_ in backend.calls),1)

    def test_extraction_budget_failures_preserve_every_original_and_report_incomplete(self):
        packet,bundle=fixture()
        for variant in ['E','F']:
            with self.subTest(variant=variant),tempfile.TemporaryDirectory() as td:
                backend=Backend(exhausted=True)
                result=partial_temporal.run(packet,bundle,backend,load_config(f'configs/script_{variant}.yaml'),td,dict(online_seconds=1500))
                self.assertIsNone(result['error'],result['error'])
                self.assertEqual(result['incomplete'][0]['stage'],'memory_extraction')
                qa=next(inp for op,inp in backend.calls if op=='videotree_qa')
                for i in bundle['selected_seconds']:self.assertIn(packet['captions'][i]['text'],io.parts(qa)[1])

    def test_cli_switches_memory_before_preflight_and_rejects_conflicting_controls(self):
        import script_qa
        for variant,mode in [('E','joint_entities'),('F','lazy_script'),('I','merged_lazy_script'),('L','planned_lazy_script')]:
            class Stop(Exception):pass
            def preflight(cfg):
                self.assertEqual(cfg['query']['temporal_qa_evidence'],mode);raise Stop()
            with patch.object(script_qa,'preflight',side_effect=preflight):
                with self.assertRaises(Stop):script_qa.main(['query','--base','unused','--questions','unused','--out','unused','--memory',variant])
        with patch.object(script_qa,'preflight') as preflight:
            for args in [['--memory','E','--scheme4'],['--config','configs/script_scheme3.yaml','--memory','F']]:
                with self.assertRaises(ValueError):script_qa.main(['query','--base','unused','--questions','unused','--out','unused']+args)
            preflight.assert_not_called()

    def test_backend_batch_dispatch_preserves_runtime_schema_and_token_accounting(self):
        from script_core.backend import LocalModels
        from script_core import lazy_script_memory
        cfg=load_config();models=LocalModels(cfg)
        requests=[dict(unit=dict(id='U0',text='C moves the book.'),requested_fields=['actions.base'],source_type='caption')]
        captured=[]
        class Batch:
            def __init__(self,config,runtime,models):
                self.models=models
                captured.append(runtime)
            def generate(self,inputs):
                self_inputs=deepcopy(inputs)
                captured.append(self_inputs)
                return [dict(data=report(),usage=dict(input_tokens=13,output_tokens=7))]
        with patch('script_core.batch_extraction.BatchExtractor',Batch):
            value=models.call('f_extract_batch',dict(requests=requests))
        self.assertIs(captured[0].memory,lazy_script_memory)
        self.assertEqual(captured[1],requests)
        self.assertEqual(value['usage'],dict(input_tokens=13,output_tokens=7))
        self.assertEqual(models.model_tokens['vlm'],13)
        with self.assertRaises(ValueError):models.call('f_extract_batch',dict(requests=requests,question='secret'))

    def test_cache_is_invalidated_when_caption_timeline_is_revised(self):
        with tempfile.TemporaryDirectory() as td:
            backend=Backend();cfg=load_config('configs/script_F.yaml');settings=dict(online_seconds=1500,memory_cache_directory=td+'/shared')
            for index in range(2):
                packet,bundle=fixture(qid=str(index))
                if index:
                    packet['captions'][3]['text']='Changed unread caption in a new video revision.'
                    bundle['memory']=partial.new_memory('v',packet['captions'],'source')
                    partial.rebuild_indexes(bundle['memory'])
                    bundle['packet_hash']=digest(packet)
                value=partial_temporal.run(packet,bundle,backend,cfg,Path(td)/str(index),settings)
                self.assertIsNone(value['error'],value['error'])
                self.assertGreater(value['qa_evidence']['cache']['missing_fields'],0)

    def test_invalid_planning_output_does_not_repair_or_generate_an_answer(self):
        packet,bundle=fixture();backend=Backend();original=backend.call
        def call(operation,inputs,*args):
            if operation=='f_plan_fields':
                backend.calls.append((operation,deepcopy(inputs)))
                return dict(data=dict(fields=['states.nonexistent']))
            return original(operation,inputs,*args)
        backend.call=call
        with tempfile.TemporaryDirectory() as td:
            result=partial_temporal.run(packet,bundle,backend,load_config('configs/script_F.yaml'),td,dict(online_seconds=1500))
        self.assertIsNotNone(result['error'])
        self.assertEqual([op for op,_ in backend.calls],['f_plan_fields'])


if __name__=='__main__':unittest.main()
