"""Exercise the actual Meter and pipeline at the r6 budget boundary."""
from copy import deepcopy
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from script_core.budget import (BudgetLedger, Meter, ModelWorker, ToolFailure,
                               ToolDeadlineExceeded, exploration_exhausted)
from script_core.config import load_config
from script_core.temporal_entity import SETTINGS, partition
from script_core.temporal_pipeline import fit_qa_context, run
from tests.test_temporal_entity import frames, row, report


class TimedBackend:
    """Simulate processing time without bypassing accounting or JSON validation."""
    def __init__(self, clock, stop_at='observation', fatal=False):
        self.clock=clock;self.stop_at=stop_at;self.fatal=fatal;self.seen=[]

    def call(self, op, inp, timeout):
        self.seen.append(op)
        if op=='clip_text':
            return dict(vectors=[[1.,0.]]*6,chunk_counts=[1]*6,content_token_counts=[1]*6)
        if op=='scene_scope':
            self.clock[0]+=1
            return dict(data=dict(scope='local',reason='one scene'))
        if op=='temporal_observe':
            if self.stop_at=='observation' and inp['entities']:
                self.clock[0]+=timeout-2
                if self.fatal:raise ToolFailure('CUDA out of memory')
                raise ToolDeadlineExceeded('SoftDeadlineExceeded: Processing deadline reached; worker retained',
                                           operation=op,worker_reusable=True)
            self.clock[0]+=40 if self.stop_at=='observation' else 60
            return dict(data=report(row()))
        if op=='temporal_review':
            self.clock[0]+=timeout-2
            raise ToolDeadlineExceeded('SoftDeadlineExceeded: stopped during navigation',
                                       operation=op,worker_reusable=True)
        if op=='temporal_qa_check':
            self.clock[0]+=.1
            return dict(input_tokens=100)
        if op=='videotree_qa':
            self.clock[0]+=2
            return dict(data=dict(selected_option='0',text='prediction: A'))
        raise AssertionError(op)


class TemporalBudgetCompletionTests(unittest.TestCase):
    def run_case(self, stop_at='observation', fatal=False):
        cfg=load_config();cfg['query']['temporal_entity_source']='images';cfg['budget'].update(tool_call_cap=60.,final_answer_reserve=20.)
        settings=dict(SETTINGS,online_seconds=90,frame_batch=1 if stop_at=='observation' else 6)
        caps=[dict(id=f'TS{i}',interval=[i,i+1],text='original caption') for i in range(2)]
        packet=dict(question=dict(question_id='v',video_id='v',question='Which box?',options=['a']*5))
        d=partition('v',[[1.,0.]]*2,caps,settings)
        clock=[100.];backend=TimedBackend(clock,stop_at,fatal)
        with tempfile.TemporaryDirectory() as tmp, patch('time.monotonic',side_effect=lambda:clock[0]):
            result=run(packet,d,frames(0,2),backend,cfg,tmp,'temporal_cross',settings)
            network=json.loads((Path(tmp)/'network.json').read_text())
        return result,network,backend

    def test_limited_observation_deadline_keeps_committed_graph_and_answers(self):
        result,net,backend=self.run_case()
        self.assertIsNone(result['error'],result['error'])
        self.assertEqual(result['answer']['selected_option'],'0')
        self.assertEqual(result['incomplete'][0]['stage'],'entity_network')
        self.assertEqual(list(net['entities']),['E0'])
        self.assertEqual(net['states']['T0']['source_frame_ids'],['F0'])
        self.assertEqual(backend.seen[-2:],['temporal_qa_check','videotree_qa'])
        self.assertFalse(result['cost_summary']['budget_violation'])
        self.assertLessEqual(result['cost_summary']['total'],90.)

    def test_exhausted_navigation_also_answers_from_existing_graph(self):
        result,net,backend=self.run_case('review')
        self.assertIsNone(result['error'],result['error'])
        self.assertEqual(result['incomplete'][0]['stage'],'navigation_review')
        self.assertEqual(len(net['entities']),1)
        self.assertEqual(backend.seen[-2:],['temporal_qa_check','videotree_qa'])

    def test_cuda_errors_are_not_disguised_as_partial_evidence(self):
        result,_,backend=self.run_case(fatal=True)
        self.assertIn('CUDA out of memory',result['error']['message'])
        self.assertNotIn('videotree_qa',backend.seen)

    def test_full_call_deadline_is_an_error_even_if_worker_survives(self):
        clock=[100.];backend=TimedBackend(clock)
        cfg=load_config();cfg['budget']['tool_call_cap']=60.
        with tempfile.TemporaryDirectory() as tmp,patch('time.monotonic',side_effect=lambda:clock[0]):
            meter=Meter(backend,BudgetLedger(600),cfg,tmp)
            try:
                with self.assertRaises(ToolDeadlineExceeded) as caught:
                    meter.call('temporal_observe',dict(entities=[dict(id='E0')]))
            finally:meter.close()
        self.assertFalse(caught.exception.budget_limited)
        self.assertFalse(exploration_exhausted(caught.exception))

    def test_final_context_check_can_use_answer_reserve_after_exploration(self):
        class Worker(ModelWorker):
            requires_vlm_warmup=False
            def call(self,op,inp,timeout):
                return dict(input_tokens=100)
        cfg=load_config();cfg['budget']['final_answer_reserve']=45.
        with tempfile.TemporaryDirectory() as tmp:
            ledger=BudgetLedger(1500,answer_reserve=45);ledger.actual=1455
            meter=Meter(Worker(cfg),ledger,cfg,tmp)
            try:
                _,_,tokens=fit_qa_context(dict(id='v',question='q',options=['a']*5),
                    [dict(text='source')],[0],{},meter,use_memory=False,final=True)
            finally:meter.close()
        self.assertEqual(tokens,100)
        self.assertIn('temporal_qa_check',[c['operation'] for c in ledger.calls])
        self.assertFalse(ledger.budget_violation)


if __name__=='__main__':unittest.main()
