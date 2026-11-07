import json
from pathlib import Path
import tempfile
import unittest

from script_core import q_repair as r, q_evidence as q
from script_core.config import digest
from script_core.schemas import validate_output
from script.q_repair_validation import source


class QRepairTests(unittest.TestCase):
    def test_window_scope_preserves_every_target_once_and_temporal_gaps(self):
        ss=[source('S'+str(i),'N moves part '+str(i)+'.',i if i<9 else 100+i) for i in range(15)]
        ww=r.windows('Describe the assembly.',ss)
        self.assertEqual([s['source_id'] for w in ww for s in w['observations']],[s['id'] for s in ss])
        self.assertTrue(all(len(w['observations'])<=8 and len(w['context'])<=4 for w in ww))
        self.assertTrue(all(max(s['intervals'][0][0] for s in w['observations'])-min(s['intervals'][0][0] for s in w['observations'])<=20 for w in ww))
        for w in ww:r.validate_request('q3_filter',w)
        ww[0]['options']=['hidden answer']
        with self.assertRaises(ValueError):r.validate_request('q3_filter',ww[0])

    def test_missing_time_cannot_be_certified_with_wrong_region_reference(self):
        req=dict(question='Compare the first half and second half.',options=['A','B'],
                 observations=r.views([source('S0','N opens a case.',0)]),regions=[dict(id='late',interval=[5,10])])
        value=dict(question_content=dict(status='supported',witness='S0'),late=dict(status='supported',witness='S0'))
        with self.assertRaises(ValueError):r.validate_verdict('q3_coverage',req,value)
        regions=r.temporal_regions(req['question'],[0,10],[])
        self.assertEqual([x['interval'] for x in regions],[[0,5],[5,10]])
        self.assertEqual(r.temporal_regions('What is the object?', [0,10],[]),[])

    def test_supported_and_missing_are_mutually_exclusive(self):
        req=dict(question='What happens?',options=['A','B'],option_index=0,observations=r.views([source('S0','An observation.',0)]))
        value=dict(status='supported',witnesses=dict(w0='S0',w1='none',w2='none',w3='none'),missing_kind='time',reason='partial')
        with self.assertRaises(ValueError):r.validate_verdict('q3_option_audit',req,value)
        value['missing_kind']='none';r.validate_verdict('q3_option_audit',req,value)
        value['witnesses']['w0']='foreign'
        with self.assertRaises(ValueError):r.validate_verdict('q3_option_audit',req,value)
        req['observations']=[]
        with self.assertRaises(ValueError):r.validate_verdict('q3_option_audit',req,value)

    def test_sufficiency_requires_candidate_all_alternatives_and_content(self):
        checks=[dict(status='supported'),dict(status='unresolved')]
        coverage=dict(question_content=dict(status='supported',witness='S0'))
        self.assertFalse(r.certificate(checks,coverage)['sufficient'])
        checks[1]['status']='contradicted'
        self.assertTrue(r.certificate(checks,coverage)['sufficient'])
        coverage['late']=dict(status='unresolved',witness='none')
        self.assertFalse(r.certificate(checks,coverage)['sufficient'])

    def test_m_legacy_contradiction_hole_is_rejected(self):
        from script_core.m_scene_memory import output_schema
        schema=output_schema('m_sufficiency_check',dict(captions=[dict(id='C0')]))
        bad=dict(reason='still missing',verdict='ruled_out',source_caption_ids=['C0'],missing_detail='unknown later phase')
        with self.assertRaises(ValueError):validate_output(bad,schema)
        bad['missing_detail']='';validate_output(bad,schema)

    def test_scene_read_adds_only_requested_literal_sources(self):
        archive=dict(captions=[dict(id='C0',text='N opens case.',interval=[0,1]),dict(id='C1',text='N may move the sensor.',interval=[8,9])],
            scenes=[dict(id='T0',caption_ids=['C0'],interval=[0,5],description='hint'),dict(id='T1',caption_ids=['C1'],interval=[5,10],description='hint')])
        ss=[source('S0','N opens case.',0)]
        expanded=r.read_scene(archive,ss,'T1')
        self.assertEqual(len(ss),1);self.assertEqual(expanded[1]['text'],archive['captions'][1]['text'])
        self.assertEqual(expanded[1]['source_intervals'],[[8,9]])
        self.assertFalse(r.available_scenes(archive,expanded))
        with self.assertRaises(ValueError):r.read_scene(archive,expanded,'T1')

    def test_batch_rejects_cross_question_and_option_leakage(self):
        from script_core.o_batch_generation import validate_packet
        req=r.windows('What is moved?',[source('S0','N moves a sensor.',0)])[0]
        scope=dict(question_id='a',condition='original_question_only')
        packet=dict(scope=scope,operation='q3_filter',items=[dict(source_id='W0',scope=dict(scope),request=req)])
        validate_packet(packet)
        packet['items'][0]['scope']['question_id']='b'
        with self.assertRaises(ValueError):validate_packet(packet)

    def test_full_pipeline_gap_read_keep_only_and_reader_has_no_audit_answer(self):
        from script.run_q_repair_experiment import run_case
        class Fake:
            def call(self,operation,payload,*args,**kwargs):
                if operation=='q3_independent_batch':
                    items=[]
                    for item in payload['items']:
                        req=item['request'];op=payload['operation'];obs=req.get('observations',[])
                        ids=[o['source_id'] for o in obs]
                        if op=='q3_filter':data={i:('uncertain' if i=='S1' else 'direct') for i in ids}
                        elif op=='q3_option_audit':
                            status=('supported' if req['option_index']==0 else 'contradicted') if 'R_C2' in ids else 'unresolved'
                            data=dict(status=status,witnesses=dict(w0='R_C2' if status!='unresolved' else 'none',w1='none',w2='none',w3='none'),
                                      missing_kind='none' if status!='unresolved' else 'time',reason='')
                        elif op=='q3_coverage':
                            data=dict(question_content=dict(status='supported' if 'R_C2' in ids else 'unresolved',witness='R_C2' if 'R_C2' in ids else 'none'))
                            for region in req['regions']:
                                eligible=[o['source_id'] for o in obs if any(a<region['interval'][1] and b>region['interval'][0] for a,b in o['intervals'])]
                                data[region['id']]=dict(status='supported' if eligible else 'unresolved',witness=eligible[0] if eligible else 'none')
                        elif op=='q3_navigate':data=dict(scene_id='T1')
                        else:raise AssertionError(op)
                        items.append(dict(source_id=item['source_id'],scope=item['scope'],request_hash=digest(req),data=data,parse_error=None,ended_normally=True))
                    return dict(items=items,usage=dict(input_tokens=1,output_tokens=1))
                if operation in ('q_answer','qplus_answer'):
                    prompt=payload['inputs']['prompts'][0]
                    assert 'INCIDENTAL_UNCERTAIN' not in prompt and 'option_checks' not in prompt and 'supported_options' not in prompt
                    assert 'red tray' in prompt
                    return dict(data=dict(selected_option='0'),usage=dict(input_tokens=1,output_tokens=1))
                raise AssertionError(operation)
        cfg=json.loads(Path('workdirs/Q-Qplus-O-source-300-20261105-r2/qa_config.json').read_text())
        question=dict(question_id='synthetic',question='Compare the first half and second half.',options=['red','blue'])
        original=dict(head='Choose an answer.',prompts=['  Description: old\n\n###\n\n Questions: Compare the first half and second half.\n Options: A: red B: blue\n\n###\n\nAnswer:'])
        archive=dict(scope=[0,10],inherited_N_budgeted_seconds=12,captions=[dict(id='C2',text='N places the sensor in the red tray.',interval=[8,9])],
                     scenes=[dict(id='T1',caption_ids=['C2'],interval=[5,10],description='later placement')])
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/'case'
            result=run_case(Fake(),cfg,question,[source('S0','N opens a case.',0),source('S1','INCIDENTAL_UNCERTAIN',1)],original,folder,archive,'coverage_repair')
            self.assertTrue(result['complete']);self.assertEqual(result['stop_reason'],'audited_sufficient')
            self.assertEqual(len(result['retrievals']),1)
            self.assertEqual(result['cost_summary']['shared_dependency_seconds'],12)
            self.assertEqual(result['retained_observations'],2)
            self.assertEqual(result['arms']['Q']['retained_evidence_hash'],result['arms']['Qplus']['retained_evidence_hash'])
            with self.assertRaises(ValueError):run_case(Fake(),cfg,question,[],original,folder)


if __name__=='__main__':unittest.main()
