from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from script_core import q_evidence as q
from script_core.config import digest
from script_core.o_batch_generation import validate_packet, messages_for
from script import run_q_experiment as runner
from script.scale_live_parallel_pool import read, write


CFG=dict(models=dict(device='cpu'),query=dict(tool_transient_retries=0),
         budget=dict(tool_call_cap=150,build_call_cap=600,final_answer_reserve=45),cache={})


def source(sid='S0', text='#J J does not open the box.'):
    return dict(id=sid,text=text,source_intervals=[[1,2],[5,6]])


def original():
    return dict(query_id='q',head='Choose one answer.',prompts=[
        '  Description: OLD\n\n###\n\n Questions: What happens? \n Options: \n A: First. \n B: Second. \n\n###\n\n The prediction is:'])


class Backend:
    def __init__(self, fail_second=False):
        self.calls=[];self.fail_second=fail_second
    def call(self, operation, inputs, timeout):
        self.calls.append((operation,deepcopy(inputs)))
        if operation==q.BATCH:
            validate_packet(inputs)
            return dict(items=[dict(source_id=r['source_id'],scope=r['scope'],request_hash=digest(r['request']),
                ended_normally=True,parse_error=None,data=dict(decision='keep')) for r in inputs['items']],
                usage=dict(input_tokens=10,output_tokens=4))
        if self.fail_second and operation=='qplus_answer':
            raise ValueError('Synthetic final reader failure')
        return dict(data=dict(selected_option='0',raw='prediction: A'),usage=dict(input_tokens=7,output_tokens=4))


class EvidenceTests(unittest.TestCase):
    def test_one_metadata_prefix_is_removed_reversibly(self):
        s=source(text='#C C puts "繫" on a shelf.');r=q.observation(s)
        self.assertEqual(r['text'],'C puts "繫" on a shelf.')
        self.assertEqual(r['source_text'][r['body_offset']:],r['text'])
        self.assertEqual(q.observation(source(text='C #tag stays.'))['text'],'C #tag stays.')

    def test_keep_preserves_negative_observation_uncertain_is_excluded(self):
        s=source();selected=q.selected_observations([s],{'S0':dict(decision='keep')})
        self.assertEqual(selected[0]['text'],'J does not open the box.')
        self.assertEqual(q.selected_observations([s],{'S0':dict(decision='uncertain')}),[])
        self.assertEqual(q.selected_observations([s],{'S0':dict(decision='drop')}),[])

    def test_missing_or_foreign_decision_rejected(self):
        for decisions in [{},{'S0':dict(decision='keep'),'S1':dict(decision='keep')}]:
            with self.assertRaises(ValueError):q.selected_observations([source()],decisions)

    def test_rendering_preserves_text_and_intervals_in_both_arms(self):
        s=source(text='#J J says "no".\nK may leave.');selected=q.selected_observations([s],{'S0':dict(decision='keep')})
        full=json.loads(q.present(selected).splitlines()[-1]);compact=q.present(selected,True).splitlines()[-1]
        self.assertEqual(full['text'],selected[0]['text']);self.assertEqual(full['intervals'],s['source_intervals'])
        self.assertTrue(compact.endswith(json.dumps(selected[0]['text'],ensure_ascii=False)))
        self.assertEqual(len(q.present(selected,True).splitlines()),2)

    def test_rewritten_observation_cannot_be_presented(self):
        selected=q.selected_observations([source()],{'S0':dict(decision='keep')});selected[0]['text']='invented'
        with self.assertRaises(ValueError):q.present(selected)

    def test_no_options_or_generated_plan_allowed(self):
        req=q.request_for(dict(question='What happens?'),[source()],0)
        for field in ['options','evidence_task','plan']:
            changed=dict(req,**{field:'forbidden'})
            with self.assertRaises(ValueError):q.validate_request(changed)

    def test_context_is_bounded_and_source_local(self):
        sources=[source(f'S{i}',f'#J J moves object {i}.') for i in range(5)]
        req=q.request_for(dict(question='What changes?'),sources,2)
        self.assertEqual([r['source_id'] for r in req['context']],['S1','S3'])
        self.assertNotIn('object 0',json.dumps(req));self.assertNotIn('object 4',json.dumps(req))
        self.assertEqual(len(q.request_for(dict(question='What changes?'),sources,0)['context']),1)

    def test_packet_rejects_cross_question_scope_or_identity(self):
        req=q.request_for(dict(question='What happens?'),[source()],0)
        packet=runner.packet('a',[req]);packet['items'][0]['scope']['question_id']='b'
        with self.assertRaises(ValueError):validate_packet(packet)
        packet=runner.packet('a',[req]);packet['items'][0]['source_id']='other'
        with self.assertRaises(ValueError):validate_packet(packet)

    def test_prompt_has_original_observation_and_no_options_plan(self):
        req=q.request_for(dict(question='What happens?'),[source()],0)
        messages,schema,wire,aliases=messages_for(q.FILTER,req)
        self.assertEqual(json.loads(messages[1]['content']),req)
        self.assertEqual(schema,q.schema());self.assertEqual(aliases,{})

    def test_strict_decision_output(self):
        for bad in [dict(decision='unknown'),dict(decision='keep',text='new fact')]:
            with self.assertRaises(ValueError):q.validate_output(bad,q.schema())

    def test_reader_fixed_question_and_options_unchanged(self):
        from script.controlled_memory_io import fixed
        selected=q.selected_observations([source()],{'S0':dict(decision='uncertain')})
        for compact in [False,True]:self.assertEqual(fixed(q.final_input(original(),selected,compact)),fixed(original()))


class RunnerTests(unittest.TestCase):
    def test_uncertain_is_archived_but_cannot_enter_either_reader(self):
        class MixedBackend(Backend):
            def call(self,operation,inputs,timeout):
                result=super().call(operation,inputs,timeout)
                if operation==q.BATCH:
                    for item,decision in zip(result['items'],['keep','uncertain','drop']):
                        item['data']['decision']=decision
                return result
        with tempfile.TemporaryDirectory() as td:
            target=Path(td)/'run';backend=MixedBackend()
            sources=[source('S0','KEPT_MARKER is observed.'),source('S1','UNCERTAIN_MARKER is observed.'),source('S2','DROPPED_MARKER is observed.')]
            row=runner.run_case(backend,CFG,dict(question_id='q',question='What happens?',options=['First.','Second.']),sources,original(),target)
            self.assertEqual(row['retained_observations'],1)
            self.assertEqual(read(target/'selection.json')['S1']['decision'],'uncertain')
            for arm in ['Q','Qplus']:
                text=json.dumps(read(target/arm/'final_input.json'))
                self.assertIn('KEPT_MARKER',text)
                self.assertNotIn('UNCERTAIN_MARKER',text)
                self.assertNotIn('DROPPED_MARKER',text)

    def test_full_pair_uses_only_filter_and_two_fresh_answers(self):
        with tempfile.TemporaryDirectory() as td:
            backend=Backend();sources=[source(f'S{i}',f'#J J moves object {i}.') for i in range(5)]
            row=runner.run_case(backend,CFG,dict(question_id='q',question='What happens?',options=['First.','Second.']),sources,original(),Path(td)/'run')
            self.assertTrue(row['complete']);self.assertEqual(row['retained_observations'],5)
            self.assertEqual([op for op,_ in backend.calls],[q.BATCH,q.BATCH,'q_answer','qplus_answer'])
            self.assertEqual(row['arms']['Q']['retained_evidence_hash'],row['arms']['Qplus']['retained_evidence_hash'])
            self.assertEqual(row['cost_summary']['total_cap'],3600);self.assertFalse(row['cost_summary']['budget_violation'])
            self.assertEqual([len(p['items']) for op,p in backend.calls if op==q.BATCH],[4,1])

    def test_empty_source_case_does_not_invent_extraction_or_filter_calls(self):
        with tempfile.TemporaryDirectory() as td:
            backend=Backend();row=runner.run_case(backend,CFG,dict(question_id='q',question='What happens?',options=['First.','Second.']),[],original(),Path(td)/'run')
            self.assertEqual([x[0] for x in backend.calls],['q_answer','qplus_answer']);self.assertEqual(row['retained_observations'],0)

    def test_failure_retains_first_answer_and_all_costs_without_retry(self):
        with tempfile.TemporaryDirectory() as td:
            target=Path(td)/'run';backend=Backend(True)
            with self.assertRaises(Exception):runner.run_case(backend,CFG,dict(question_id='q',question='What happens?',options=['First.','Second.']),[source()],original(),target)
            result=read(target/'result.json');self.assertFalse(result['complete']);self.assertIn('Q',result['arms']);self.assertTrue(result['error'])
            calls=[json.loads(x) for x in (target/'calls.jsonl').read_text().splitlines()]
            self.assertEqual(len(calls),3);self.assertTrue(calls[-1]['error'])
            with self.assertRaisesRegex(ValueError,'never silently'):runner.run_case(backend,CFG,{},[],{},target)

    def test_parallel_claims_are_unique(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td);ids=[f'q{i}' for i in range(80)];write(out/'cohort.json',ids);write(out/'claims.json',{})
            with ThreadPoolExecutor(max_workers=8) as pool:claimed=list(pool.map(lambda i:runner.claim(out,i),range(80)))
            self.assertEqual(len({x[1] for x in claimed}),80)
            write(out/'failure.json',dict(message='stop'))
            self.assertIsNone(runner.claim(out,0))

    def test_publish_is_atomic_and_preserves_original_raw_paths(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td);(out/'runs').mkdir();write(out/'claims.json',{'q':dict(state='running')})
            target=out/'attempts/q';write(target/'result.json',dict(complete=True,error=None,arms=dict(Q={},Qplus={})))
            write(target/'raw_calls/raw.json',dict(value='original'));runner.publish(out,'q',target)
            self.assertTrue(target.is_symlink());self.assertEqual(read(target/'raw_calls/raw.json'),dict(value='original'))
            self.assertEqual(read(out/'claims.json')['q']['state'],'complete')


if __name__=='__main__':unittest.main()
